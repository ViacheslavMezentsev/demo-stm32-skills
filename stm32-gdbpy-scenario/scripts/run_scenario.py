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

Сценарий получает окружение SCENARIO_GDB_HOST, SCENARIO_GDB_PORT, SCENARIO_RESULT и
SCENARIO_ENVIRONMENT и пишет JSON в SCENARIO_RESULT (шаблон — assets/scenario_template.py).
Итог прогона — <out>/run.json. Код возврата: 0 — PASS, 1 — FAIL, 2 — ERROR или TIMEOUT.
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
    """(процесс или None, host, port, окружение, описание)."""
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
    if args.ssh:
        return remote_openocd(args, out, port)
    openocd = find_tool(args.openocd, "OPENOCD", ("openocd",))
    process, log = start([openocd] + openocd_args(args, port), out / "server.log")
    log.close()
    wait_ready(process, out / "server.log", "for gdb connections")
    return process, "127.0.0.1", port, args.environment or "hardware", "openocd " + " ".join(args.openocd_config)


def openocd_args(args, port):
    """Интерфейс, выбор пробника, цель; сервер только на 127.0.0.1, без telnet и Tcl."""
    configs = list(args.openocd_config)
    command = ["-f", configs.pop(0)]
    if args.adapter_serial:
        command += ["-c", "adapter serial " + args.adapter_serial]
    if args.adapter_usb_location:
        command += ["-c", "adapter usb location " + args.adapter_usb_location]
    for config in configs:
        command += ["-f", config]
    command += ["-c", "bindto 127.0.0.1", "-c", "telnet_port disabled", "-c", "tcl_port disabled"]
    return command + (["-c", "gdb_port %d" % port] if port else [])


def remote_path(path):
    """Путь для оболочки узла: ~/ раскрывается в $HOME, остальное экранируется."""
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
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


def remote_openocd(args, out, local_port):
    remote = Remote(args, out)
    openocd = args.openocd or "openocd"
    # Свободный порт на узле выбирает сам узел; nohup — чтобы сервер пережил выход из SSH.
    server = " ".join([remote_path(openocd)] + [shlex.quote(part) for part in openocd_args(args, None)])
    script = ("p=$(python3 -c 'import socket;s=socket.socket();s.bind((\"127.0.0.1\",0));print(s.getsockname()[1])' "
              "2>/dev/null || echo $((30000 + $$ % 20000))); log=/tmp/gdbpy-ocd-$$.log; "
              "nohup " + server + " -c \"gdb_port $p\" > $log 2>&1 < /dev/null & pid=$!; "
              "for i in $(seq 100); do grep -q 'for gdb connections' $log && break; "
              "kill -0 $pid 2>/dev/null || break; sleep 0.1; done; echo PID=$pid PORT=$p LOG=$log; "
              "grep -q 'for gdb connections' $log && echo READY || cat $log")
    answer = remote.ssh(script, timeout=40).stdout
    match = re.search(r"PID=(\d+) PORT=(\d+) LOG=(\S+)", answer)
    if match:
        remote.pid, remote.log = int(match.group(1)), match.group(3)
    if not match or "READY" not in answer:
        remote.stop()
        raise RuntimeError("OpenOCD на узле не стал готов: " + answer.strip()[-400:])
    command = ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10",
               "-L", "%d:127.0.0.1:%s" % (local_port, match.group(2))]
    command += (["-i", args.ssh_key] if args.ssh_key else []) + [args.ssh]
    remote.tunnel, log = start(command, out / "tunnel.log")
    log.close()
    # Туннель проверяется живым процессом, а не пробным подключением: оно заняло бы GDB-порт OpenOCD.
    time.sleep(2)
    if remote.tunnel.poll() is not None:
        remote.stop()
        raise RuntimeError("SSH-туннель не поднялся: " + (out / "tunnel.log").read_text(errors="replace")[-300:])
    return remote, "127.0.0.1", local_port, args.environment or "hardware", "ssh %s openocd" % args.ssh


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
    try:
        server, host, port, environment, described = backend(args, out)
        summary.update(environment=environment, backend=described)
        env = dict(os.environ, SCENARIO_GDB_HOST=host, SCENARIO_GDB_PORT=str(port),
                   SCENARIO_RESULT=str(out / "scenario.json"), SCENARIO_ENVIRONMENT=environment)
        # Список аргументов без оболочки: путь с пробелом не разваливает -ex "source ...".
        # Без ELF GDB работает по адресам: символов нет, сценарий читает память и регистры.
        command = [gdb, "-nx", "-batch"] + ([str(args.elf)] if args.elf else [])
        command += ["-ex", "source " + Path(args.scenario).resolve().as_posix()]
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
        (out / "run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, passed in summary["checks"].items():
        print(("PASS " if passed else "FAIL ") + name)
    tail = " (последний шаг: %s)" % summary["last_step"] if summary["status"] == "TIMEOUT" else ""
    print("%s %s%s" % (summary["status"], out / "run.json", tail))
    return {"PASS": 0, "FAIL": 1}.get(summary["status"], 2)


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
    parser.add_argument("--renode", help="исполняемый файл Renode")
    parser.add_argument("--renode-line", action="append", default=[], help="строка монитора Renode до LoadELF")
    parser.add_argument("--openocd", help="исполняемый файл OpenOCD")
    parser.add_argument("--adapter-serial", help="серийный номер пробника для OpenOCD")
    parser.add_argument("--adapter-usb-location", help="адрес USB пробника <bus>-<port[.port]> вместо серийника")
    parser.add_argument("--ssh", help="user@host: OpenOCD на узле стенда и SSH-туннель к нему")
    parser.add_argument("--ssh-key", help="ключ SSH для --ssh")
    parser.add_argument("--check-gdb", action="store_true", help="только проверить GDB с Python")
    args = parser.parse_args()
    if args.check_gdb:
        print("GDB с Python: " + find_gdb(args.gdb))
        return 0
    if not (args.scenario and args.out):
        parser.error("нужны --scenario и --out")
    if not (args.renode_platform or args.openocd_config or args.attach):
        parser.error("выберите маршрут: --renode-platform, --openocd-config или --attach")
    if args.renode_platform and not args.elf:
        parser.error("для Renode нужен --elf")
    if args.ssh and not args.openocd_config:
        parser.error("--ssh работает вместе с --openocd-config")
    if (args.elf and not args.elf.is_file()) or not args.scenario.is_file():
        parser.error("нет файла ELF или сценария")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
