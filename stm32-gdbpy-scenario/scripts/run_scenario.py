#!/usr/bin/env python3
"""Запуск одного сценария gdb-py с ограниченным временем и уборкой своих процессов.

Навык stm32-gdbpy-scenario. Скрипт поднимает GDB-сервер (Renode или OpenOCD) либо
подключается к готовому порту, запускает GDB с Python в пакетном режиме, ждёт итог
сценария не дольше бюджета и останавливает только процессы, которые запустил сам.

  # эмулятор: Renode поднимает GDB-сервер на свободном порту
  python run_scenario.py --elf build/app.elf --scenario my_scenario.py \\
      --renode-platform emu/renode/board.repl --out build/scenario

  # локальный ST-Link: OpenOCD на 127.0.0.1
  python run_scenario.py --elf build/app.elf --scenario my_scenario.py \\
      --openocd-config interface/stlink.cfg --openocd-config target/stm32f4x.cfg --out build/scenario

  # готовый сервер или SSH-туннель к удалённому OpenOCD
  python run_scenario.py --elf build/app.elf --scenario my_scenario.py --attach 127.0.0.1:13333 --out build/scenario

  # только проверить, что GDB понимает Python
  python run_scenario.py --check-gdb

Сценарий получает переменные SCENARIO_* (таблица — SKILL.md, раздел 3.3) и пишет JSON в
SCENARIO_RESULT (шаблон — assets/scenario_template.py). Итог прогона — <out>/run.json, на экране —
сводка «статус, проверки, время, причина». Код возврата: 0 — PASS, 1 — FAIL, 2 — ERROR или TIMEOUT.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time

GDB_NAMES = ("arm-none-eabi-gdb-py3", "arm-none-eabi-gdb", "gdb-multiarch")
# Как в навыке stm32-renode-emulator: синхронный журнал Renode не теряет строк.
RENODE_CONFIG = "[general]\nuse-synchronous-logging = True\ncollapse-repeated-log-entries = False\n"
READY_S = 30
STEP = re.compile(r"^STEP (.+)$", re.M)


def gdb_has_python(path):
    """True, если GDB выполняет Python: обычный arm-none-eabi-gdb часто собран без него."""
    try:
        out = subprocess.run([path, "-nx", "-q", "-batch", "-ex", "python print(6 * 7)"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.stdout.strip().splitlines()[-1:] == ["42"]


def find_gdb(explicit):
    candidates = [explicit, os.environ.get("GDB")]
    root = os.environ.get("ARM_TOOLCHAIN_ROOT") or os.environ.get("STM32_TOOLCHAIN_PATH")
    suffix = ".exe" if os.name == "nt" else ""
    for name in GDB_NAMES:
        if root:
            candidates.append(str(Path(root) / "bin" / (name + suffix)))
        candidates.append(shutil.which(name))
    tried = []
    for candidate in candidates:
        if not candidate or candidate in tried:
            continue
        tried.append(candidate)
        if Path(candidate).is_file() and gdb_has_python(candidate):
            return candidate
        if candidate == explicit:
            raise SystemExit("GDB без Python или не найден: %s" % explicit)
    raise SystemExit("GDB с Python не найден (%s); задайте --gdb, GDB или ARM_TOOLCHAIN_ROOT" % ", ".join(GDB_NAMES))


def find_tool(explicit, env, names, windows_default=None):
    candidates = [explicit, os.environ.get(env)] + [shutil.which(name) for name in names]
    if os.name == "nt" and windows_default:
        candidates.append(str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / windows_default))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise SystemExit("%s не найден: задайте --%s или %s" % (names[0], names[0], env))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def resc_path(path):
    value = Path(path).resolve().as_posix()
    if any(c in value for c in ('"', "\n", "\r")):
        raise SystemExit("Путь не подходит для скрипта Renode: " + value)
    return "@" + value.replace(" ", "\\ ")


def start(command, log_path):
    """Процесс в своей группе: уборка не задевает чужие отладчики и серверы."""
    log = log_path.open("w", encoding="utf-8")
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, **flags), log


def stop(process):
    if isinstance(process, Remote):
        process.stop()
        return
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=15)


def wait_ready(process, log_path, marker):
    """Готовность — по строке журнала сервера; пробное TCP-подключение заняло бы единственный GDB-сеанс."""
    deadline = time.monotonic() + READY_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("сервер завершился до готовности, код %s" % process.returncode)
        if marker in log_path.read_text(encoding="utf-8", errors="replace"):
            return
        time.sleep(0.1)
    raise RuntimeError("сервер не стал готов за %d с" % READY_S)


def backend(args, out):
    """(процесс или None, host, port, окружение, описание); args.server_kind — тип сервера для сценария."""
    args.server_kind = "attach" if args.attach else "renode" if args.renode_platform else \
        "jlink" if args.jlink_device else "openocd"
    if args.attach:
        host, _, port = args.attach.rpartition(":")
        return None, host or "127.0.0.1", int(port), args.environment or "hardware", "attach " + args.attach
    port = free_port()
    if args.renode_platform:
        renode = find_tool(args.renode, "RENODE_BINARY", ("renode",), "Renode/renode.exe")
        (out / "renode.config").write_text(RENODE_CONFIG, encoding="utf-8")
        lines = ['mach create "scenario"', "machine LoadPlatformDescription " + resc_path(args.renode_platform)]
        lines += args.renode_line + ["sysbus LoadELF " + resc_path(args.elf), "machine StartGdbServer %d" % port]
        (out / "server.resc").write_text("\n".join(lines) + "\n", encoding="utf-8")
        command = [renode, "--disable-gui", "--console", "--plain", "--config", str(out / "renode.config"),
                   "--execute", "include " + resc_path(out / "server.resc")]
        process, log = start(command, out / "server.log")
        log.close()
        wait_ready(process, out / "server.log", "GDB server")
        return process, "127.0.0.1", port, args.environment or "emulator", "renode " + Path(args.renode_platform).name
    executable, argv, marker, reset, label = server_spec(args)
    if args.ssh:
        server, host, port = remote_server(args, out, port, executable, argv, marker)
    else:
        command = [find_server(args, executable)] + [part.replace("{PORT}", str(port)) for part in argv]
        args.commands["server"] = subprocess.list2cmdline(command)
        if args.print_command:
            print("SERVER " + args.commands["server"], flush=True)
        server, log = start(command, out / "server.log")
        log.close()
        wait_ready(server, out / "server.log", marker)
        host = "127.0.0.1"
    args.reset_halt = reset
    return server, host, port, args.environment or "hardware", label


def server_spec(args):
    """(исполняемый файл, аргументы с {PORT}, строка готовности, команда сброса с остановкой, описание)."""
    if args.jlink_device:
        argv = ["-device", args.jlink_device, "-if", args.jlink_interface, "-speed", str(args.jlink_speed),
                "-port", "{PORT}", "-swoport", "0", "-telnetport", "0", "-RTTTelnetPort", "0",
                "-localhostonly", "1", "-nogui", "-strict", "-timeout", "5000", "-noir", "-noreset", "-nohalt",
                "-nosinglerun"]
        argv += ["-USB", args.adapter_serial] if args.adapter_serial else []
        # monitor reset у J-Link GDB Server сбрасывает и оставляет ядро остановленным.
        # Как в stm32-gdbtest: только аппаратные точки (иначе сервер пишет точки во Flash), выход — go и disconnect.
        args.server_setup, args.server_finish = "monitor flash breakpoints = 0", "monitor go|disconnect"
        return args.jlink_gdbserver, argv, "Waiting for GDB connection", "reset", "jlink " + args.jlink_device
    argv = openocd_args(args, None) + ["-c", "gdb_port {PORT}"]
    return args.openocd, argv, "for gdb connections", "reset halt", "openocd " + " ".join(args.openocd_config)


def find_server(args, executable):
    if args.jlink_device:
        return find_tool(executable, "JLINK_GDBSERVER", ("JLinkGDBServerCLExe", "JLinkGDBServerCL"),
                         "SEGGER/JLink/JLinkGDBServerCL.exe")
    return find_tool(executable, "OPENOCD", ("openocd",))


def openocd_args(args, port):
    """Интерфейс, выбор пробника, цель; сервер только на 127.0.0.1, без telnet и Tcl."""
    configs = list(args.openocd_config)
    command = ["-f", configs.pop(0)]
    if args.openocd_transport:
        # J-Link и CMSIS-DAP по умолчанию идут по JTAG; ST-Link (hla) транспорт не выбирает.
        command += ["-c", "transport select " + args.openocd_transport]
    for item in args.openocd_set:
        name, _, value = item.partition("=")
        command += ["-c", "set %s %s" % (name, value)]
    if args.adapter_serial:
        command += ["-c", "adapter serial " + args.adapter_serial]
    if args.adapter_usb_location:
        command += ["-c", "adapter usb location " + args.adapter_usb_location]
    for config in configs:
        command += ["-f", config]
    for item in args.openocd_command:
        command += ["-c", item]
    command += ["-c", "bindto 127.0.0.1", "-c", "telnet_port disabled", "-c", "tcl_port disabled"]
    return command + (["-c", "gdb_port %d" % port] if port else [])


HOME_PREFIXES = ("~/", "$HOME/", "${HOME}/")


def remote_path(path):
    """Путь для оболочки узла: ~/, $HOME/ и ${HOME}/ раскрываются в домашний каталог, остальное экранируется."""
    for prefix in HOME_PREFIXES:
        if path.startswith(prefix):
            return '"$HOME"/' + shlex.quote(path[len(prefix):])
    return shlex.quote(path)


class Remote(object):
    """OpenOCD на узле стенда и SSH-туннель к нему; stop() убирает только их."""

    def __init__(self, args, out):
        self.args, self.out, self.pid, self.log, self.tunnel = args, out, None, None, None

    def ssh(self, command, timeout=30):
        base = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        base += (["-i", self.args.ssh_key] if self.args.ssh_key else []) + [self.args.ssh, command]
        return subprocess.run(base, capture_output=True, text=True, timeout=timeout, errors="replace")

    def poll(self):
        return None if self.pid else 0

    def stop(self):
        stop(self.tunnel)
        if self.pid:
            # Только свой процесс по pid; журнал сервера сохраняется рядом с итогом прогона.
            text = self.ssh("kill %d 2>/dev/null; sleep 0.5; kill -0 %d 2>/dev/null && kill -9 %d; cat %s; rm -f %s"
                            % (self.pid, self.pid, self.pid, self.log, self.log)).stdout
            (self.out / "server.log").write_text(text, encoding="utf-8")
            self.pid = None


def remote_server(args, out, local_port, executable, argv, marker):
    remote = Remote(args, out)
    default = "/opt/SEGGER/JLink/JLinkGDBServerCLExe" if args.jlink_device else "openocd"
    # Аргумент с {PORT} подставляется оболочкой узла; остальные экранируются как есть.
    parts = ['"%s"' % part.replace("{PORT}", "$p") if "{PORT}" in part else shlex.quote(part) for part in argv]
    program = remote_path(executable or default)
    server = " ".join([program] + parts)
    args.commands["server"] = "ssh %s %s" % (args.ssh, server)
    if args.print_command:
        print("SERVER ssh %s %s" % (args.ssh, server), flush=True)
    # До запуска: исполняемый файл есть на узле. Иначе ждать готовности бессмысленно.
    found = remote.ssh("command -v %s >/dev/null 2>&1 && echo FOUND || echo MISSING" % program).stdout
    if "FOUND" not in found:
        raise RuntimeError("GDB-сервер не найден на узле %s: %s (укажите абсолютный путь, ~/ или $HOME/; "
                           "проверка: ssh %s 'command -v <путь>')" % (args.ssh, executable or default, args.ssh))
    # Свободный порт выбирает сам узел; nohup — чтобы сервер пережил выход из SSH.
    script = ("p=$(python3 -c 'import socket;s=socket.socket();s.bind((\"127.0.0.1\",0));print(s.getsockname()[1])' "
              "2>/dev/null || echo $((30000 + $$ % 20000))); log=/tmp/gdbpy-server-$$.log; "
              "nohup " + server + " > $log 2>&1 < /dev/null & pid=$!; "
              "for i in $(seq 150); do grep -q " + shlex.quote(marker) + " $log && break; "
              "kill -0 $pid 2>/dev/null || break; sleep 0.1; done; echo PID=$pid PORT=$p LOG=$log; "
              "grep -q " + shlex.quote(marker) + " $log && echo READY || cat $log")
    answer = remote.ssh(script, timeout=40).stdout
    match = re.search(r"PID=(\d+) PORT=(\d+) LOG=(\S+)", answer)
    if match:
        remote.pid, remote.log = int(match.group(1)), match.group(3)
    if not match or "READY" not in answer:
        remote.stop()
        raise RuntimeError("GDB-сервер на узле не стал готов: " + answer.strip()[-400:])
    command = ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10",
               "-L", "%d:127.0.0.1:%s" % (local_port, match.group(2))]
    command += (["-i", args.ssh_key] if args.ssh_key else []) + [args.ssh]
    remote.tunnel, log = start(command, out / "tunnel.log")
    log.close()
    # Туннель проверяется живым процессом, а не пробным подключением: оно заняло бы GDB-порт сервера.
    time.sleep(2)
    if remote.tunnel.poll() is not None:
        remote.stop()
        raise RuntimeError("SSH-туннель не поднялся: " + (out / "tunnel.log").read_text(errors="replace")[-300:])
    return remote, "127.0.0.1", local_port


def run(args):
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit("Каталог прогона должен быть новым или пустым: %s" % out)
    out.mkdir(parents=True, exist_ok=True)
    gdb = find_gdb(args.gdb)
    started = time.monotonic()
    summary = {"status": "ERROR", "environment": None, "checks": {}, "records": [],
               "started_utc": datetime.now(timezone.utc).isoformat(), "elf": str(args.elf) if args.elf else None,
               "scenario": str(args.scenario), "gdb": gdb, "timeout_s": args.timeout}
    server = None
    args.commands = summary["commands"] = {}
    try:
        server, host, port, environment, described = backend(args, out)
        summary.update(environment=environment, backend=described)
        env = dict(os.environ, SCENARIO_GDB_HOST=host, SCENARIO_GDB_PORT=str(port),
                   SCENARIO_RESULT=str(out / "scenario.json"), SCENARIO_ENVIRONMENT=environment,
                   SCENARIO_RESET_HALT=getattr(args, "reset_halt", None) or args.reset_command or "reset halt",
                   SCENARIO_SETUP=getattr(args, "server_setup", ""),
                   SCENARIO_FINISH=getattr(args, "server_finish", "continue &|detach"),
                   SCENARIO_SERVER=args.server_kind)
        # Список аргументов без оболочки: путь с пробелом не разваливает -ex "source ...".
        # Без ELF GDB работает по адресам: символов нет, сценарий читает память и регистры.
        command = [gdb, "-nx", "-batch"] + ([str(args.elf)] if args.elf else [])
        command += ["-ex", "source " + Path(args.scenario).resolve().as_posix()]
        args.commands["gdb"] = subprocess.list2cmdline(command)
        if args.print_command:
            print("GDB " + args.commands["gdb"], flush=True)
        with (out / "gdb.log").open("w", encoding="utf-8") as log:
            flags = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                     else {"start_new_session": True})
            gdb_process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                           stderr=subprocess.STDOUT, **flags)
            try:
                summary["gdb_returncode"] = gdb_process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                stop(gdb_process)
                summary["status"] = "TIMEOUT"
        text = (out / "gdb.log").read_text(encoding="utf-8", errors="replace")
        steps = STEP.findall(text)
        summary["last_step"] = steps[-1] if steps else None
        if summary["status"] != "TIMEOUT":
            result = json.loads((out / "scenario.json").read_text(encoding="utf-8"))
            if result.get("environment") != environment:
                raise RuntimeError("сценарий записал другое окружение: %s" % result.get("environment"))
            summary.update(checks=result.get("checks", {}), records=result.get("records", []),
                           restored=result.get("restored"), status=result.get("status", "ERROR"))
            # Код GDB и итог сценария должны согласоваться; иначе итог не доказан.
            if summary["status"] not in ("PASS", "FAIL", "ERROR") or \
                    (summary["status"] == "PASS") != (summary["gdb_returncode"] == 0):
                summary["status"] = "ERROR"
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        summary["error"] = "%s: %s" % (type(error).__name__, error)
        summary["status"] = "ERROR" if summary["status"] != "TIMEOUT" else "TIMEOUT"
    finally:
        stop(server)
        summary["elapsed_s"] = round(time.monotonic() - started, 3)
        if summary["status"] == "ERROR" and not summary.get("error"):
            summary["error"] = first_cause(out)
        (out / "run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, passed in summary["checks"].items():
        print(("PASS " if passed else "FAIL ") + name)
    print(summary_line(summary))
    print("прогон: %s" % (out / "run.json"))
    return {"PASS": 0, "FAIL": 1}.get(summary["status"], 2)


def first_cause(out):
    """Первая причина ERROR из журналов: исключение сценария и первая ошибка сервера."""
    causes = []
    # У сервера сначала — отказ опознания цели: он объясняет остальные ошибки журнала.
    for name, patterns in (("сценарий", [r"^(?:gdb\.error|\w+(?:Error|Exception)): .+$"]),
                           ("сервер", [r"^\w+ ?: .*examination failed.*$", r"^Error: .+$"])):
        path = out / ("gdb.log" if name == "сценарий" else "server.log")
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        found = next(filter(None, (re.search(p, text, re.M) for p in patterns)), None)
        if found:
            causes.append("%s: %s" % (name, found.group(0).strip()))
    return "; ".join(causes) or None


def summary_line(summary):
    """Сводка одной строкой: статус, проверки, время, причина отказа или последний шаг."""
    checks = summary.get("checks") or {}
    line = "%s: проверок %d/%d, %.1f с" % (summary["status"], sum(1 for ok in checks.values() if ok), len(checks),
                                         summary.get("elapsed_s", 0))
    if summary.get("restored") is not None:
        line += ", восстановление %s" % ("подтверждено" if summary["restored"] else "НЕ подтверждено")
    if summary["status"] == "TIMEOUT":
        line += "; последний шаг: %s" % summary.get("last_step")
    elif summary.get("error"):
        line += "; причина: %s" % summary["error"].strip().splitlines()[0][:300]
    elif summary["status"] == "FAIL":
        line += "; не прошли: %s" % ", ".join(name for name, ok in checks.items() if not ok)
    return line


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--elf", type=Path, help="ELF прошивки (без него — сценарий по адресам)")
    parser.add_argument("--scenario", type=Path)
    parser.add_argument("--out", type=Path, help="новый каталог прогона")
    parser.add_argument("--gdb", help="GDB с Python (иначе GDB, ARM_TOOLCHAIN_ROOT, PATH)")
    parser.add_argument("--timeout", type=float, default=60, help="бюджет GDB, с (по умолчанию 60)")
    parser.add_argument("--environment", choices=("emulator", "hardware"), help="метка окружения в итоге")
    route = parser.add_mutually_exclusive_group()
    route.add_argument("--renode-platform", type=Path, help="модель .repl: Renode с GDB-сервером")
    route.add_argument("--openocd-config", action="append", default=[], help="конфиг OpenOCD (повторяемый)")
    route.add_argument("--attach", help="host:port готового GDB-сервера или туннеля")
    route.add_argument("--jlink-device", help="имя МК для J-Link GDB Server (например, STM32F103C8, AT32F403ACGU7)")
    parser.add_argument("--renode", help="исполняемый файл Renode")
    parser.add_argument("--renode-line", action="append", default=[], help="строка монитора Renode до LoadELF")
    parser.add_argument("--openocd", help="исполняемый файл OpenOCD")
    parser.add_argument("--adapter-serial", help="серийный номер пробника (OpenOCD; для J-Link — номер J-Link)")
    parser.add_argument("--openocd-command", action="append", default=[],
                        help="команда OpenOCD после конфигов цели, например \"gdb_memory_map disable\"")
    parser.add_argument("--openocd-transport", choices=("swd", "jtag"), help="transport select (J-Link, CMSIS-DAP)")
    parser.add_argument("--openocd-set", action="append", default=[], metavar="NAME=VALUE",
                        help="переменная Tcl до конфигов цели, например CPUTAPID=0x2ba01477")
    parser.add_argument("--jlink-gdbserver", help="JLinkGDBServerCL(Exe); на узле — путь на узле")
    parser.add_argument("--jlink-interface", default="SWD", choices=("SWD", "JTAG"))
    parser.add_argument("--jlink-speed", type=int, default=1000, help="частота SWD, кГц")
    parser.add_argument("--reset-command", help="команда monitor для сброса с остановкой при --attach")
    parser.add_argument("--adapter-usb-location", help="адрес USB пробника <bus>-<port[.port]> вместо серийника")
    parser.add_argument("--ssh", help="user@host: OpenOCD на узле стенда и SSH-туннель к нему")
    parser.add_argument("--ssh-key", help="ключ SSH для --ssh")
    parser.add_argument("--check-gdb", action="store_true", help="только проверить GDB с Python")
    parser.add_argument("--print-command", action="store_true",
                        help="печатать команды сервера и GDB до запуска (они всегда пишутся в run.json)")
    args = parser.parse_args()
    if args.check_gdb:
        print("GDB с Python: " + find_gdb(args.gdb))
        return 0
    if not (args.scenario and args.out):
        parser.error("нужны --scenario и --out")
    if not (args.renode_platform or args.openocd_config or args.attach or args.jlink_device):
        parser.error("выберите маршрут: --renode-platform, --openocd-config, --jlink-device или --attach")
    if args.renode_platform and not args.elf:
        parser.error("для Renode нужен --elf")
    if args.ssh and not (args.openocd_config or args.jlink_device):
        parser.error("--ssh работает вместе с --openocd-config или --jlink-device")
    if (args.elf and not args.elf.is_file()) or not args.scenario.is_file():
        parser.error("нет файла ELF или сценария")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
