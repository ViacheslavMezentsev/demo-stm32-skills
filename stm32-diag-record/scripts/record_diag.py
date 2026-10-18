#!/usr/bin/env python3
"""Запись диагностики с платы без ROS: SWD-пробник -> OpenOCD (или Renode) -> файл bag или mcap.

НАЗНАЧЕНИЕ. Снять выбранные поля прошивки в файл для офлайн-разбора (PlotJuggler, `mcap info`,
`rosbag info`). ROS не нужен ни для записи, ни для чтения: форматы пишут `bag_writer.py` и
`mcap_writer.py` на стандартной библиотеке.

ЦЕПОЧКА. Пробник SWD (ST-Link, CMSIS-DAP, J-Link и др.) -> OpenOCD (telnet) -> чтение 32-битных слов по адресам -> сериализация
сообщений -> файл. Адреса считаются офлайн по ELF (и SVD), плата при этом не нужна. OpenOCD
подключается без сброса: прошивка продолжает работать (сброс — только по ключу --reset).

ДРУГИЕ MCU. Cortex-M не только STM32: пробник и цель задаются конфигурациями OpenOCD (--interface,
--target, --openocd-command), совместимость проверяет --doctor с теми же ключами.

БЕЗ ПЛАТЫ. `--backend renode` читает те же адреса из работающего Renode через его telnet-монитор
(`renode --port 1234`, команда `sysbus ReadDoubleWord`); Renode запускается отдельно.

КЛЮЧИ СОВПАДАЮТ С НАВЫКОМ stm32-ros-alt-channel там, где смысл общий (--out, --format/--record,
--seconds, --elf, --svd, --doctor, --dryrun).

ПРИМЕРЫ.
    python record_diag.py --doctor
    python record_diag.py --dryrun --seconds 10
    python record_diag.py --record mcap --seconds 30
    python record_diag.py --record bag --seconds 30 --compress bz2 --out build/my.bag
    python record_diag.py --target target/stm32f1x.cfg --serial <серийный номер> --record mcap
    python record_diag.py --backend renode --port 1234 --record mcap --seconds 10
    python record_diag.py --interface interface/cmsis-dap.cfg --target target/nrf52.cfg --doctor
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bag_writer import BagWriter  # noqa: E402
from mcap_writer import McapWriter  # noqa: E402

KINDS = ("uint32", "int32", "float32", "uint32_seconds")
WORD_LINE = re.compile(r"^0x([0-9a-fA-F]+):((?:\s+[0-9a-fA-F]{8})+)", re.M)
CLUSTER_BYTES = 256  # соседние адреса в пределах окна читаются одной командой mdw
DEFAULT_PORTS = {"openocd": 4444, "renode": 1234}
RENODE_PROMPT = re.compile(r"(?:^|\n)\(([^()\n]*)\) ")
ADAPTER_HINTS = ("stlink", "st-link", "cmsis-dap", "j-link", "jlink", "serial", "target voltage", "vtarget",
                 "hardware version", "fw version", "firmware version", "ftdi", "xds110", "picoprobe", "adapter")
TARGET_ROW = re.compile(r"^\s*\d+\*?\s+(\S+)\s+(\S+)\s+\S+\s+\S+\s+(\S+)\s*$", re.M)
RENODE_VALUE = re.compile(r"ReadDoubleWord 0x([0-9A-Fa-f]+)[^\n]*\n\s*(0x[0-9A-Fa-f]+)\s*\n")


# ---------------------------------------------------------------- окружение

def find_elf(explicit):
    """ELF из ключа, иначе первый подходящий в build/ текущего каталога."""
    if explicit:
        return pathlib.Path(explicit)
    candidates = sorted(pathlib.Path("build").glob("*/firmware.elf")) + sorted(pathlib.Path("build").glob("*/*.elf"))
    return candidates[0] if candidates else pathlib.Path("build/firmware.elf")


def openocd_base(args):
    """Пробник, цель и дополнительные команды; сетевые порты только на 127.0.0.1."""
    command = [args.openocd, "-f", args.interface]
    if args.serial:
        command += ["-c", "adapter serial %s" % args.serial]
    command += ["-f", args.target]
    for extra in args.openocd_command:
        command += ["-c", extra]
    return command + ["-c", "bindto 127.0.0.1", "-c", "gdb_port disabled", "-c", "tcl_port disabled"]


def openocd_command(args):
    command = openocd_base(args) + ["-c", "telnet_port %d" % args.port, "-c", "init"]
    if args.reset:
        command += ["-c", "reset run"]
    return command


def openocd_probe_command(args, address=None):
    """Разовая проверка доктора: init, таблица целей, при наличии адреса — одно слово без остановки ядра."""
    command = openocd_base(args) + ["-c", "telnet_port disabled", "-c", "init", "-c", "targets"]
    if address is not None:
        command += ["-c", "mdw 0x%08X" % (address & ~3)]
    return command + ["-c", "shutdown"]


def parse_openocd(text):
    """Разбор вывода OpenOCD для доктора: пробник, ядро, цели, ошибки, прочитанное слово."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    result = {
        "missing": re.findall(r"Can't find (\S+)", text),
        "errors": [line for line in lines if "Error:" in line],
        "adapter": [line for line in lines if line.startswith(("Info", "Warn")) and
                    any(hint in line.lower() for hint in ADAPTER_HINTS)],
        "core": None, "targets": [], "word": None,
    }
    core = re.search(r"(Cortex-M\S*)(?:\s+r\d+p\d+)?\s+processor detected", text)
    if core:
        result["core"] = core.group(1)
    if "TargetName" in text:
        result["targets"] = [{"name": name, "type": kind, "state": state}
                             for name, kind, state in TARGET_ROW.findall(text.split("TargetName", 1)[1])]
    word = WORD_LINE.search(text)
    if word:
        result["word"] = (int(word.group(1), 16), int(word.group(2).split()[0], 16))
    result["examined"] = "Examination succeed" in text or any(
        target["state"] in ("running", "halted", "reset") for target in result["targets"])
    return result


# ---------------------------------------------------------------- адреса

def resolve(args, elf, out_dir):
    """Пересчитать адреса по ELF и SVD (офлайн). Возвращает файл адресов и список полей."""
    out = pathlib.Path(args.addresses) if args.addresses else out_dir / "addresses.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(HERE / "resolve_addresses.py"), "--elf", str(elf.resolve()),
               "--spec", str(pathlib.Path(args.fields).resolve()), "--out", str(out.resolve())]
    if args.svd:
        command += ["--svd", str(pathlib.Path(args.svd).resolve())]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not out.is_file():
        raise SystemExit("не удалось вычислить адреса:\n%s" % (result.stdout + result.stderr)[-700:])
    fields = json.loads(out.read_text(encoding="utf-8"))["fields"]
    for field in fields:
        kind = field.get("kind", "uint32")
        if kind not in KINDS:
            raise SystemExit("поле %s: неизвестный kind %r (допустимы: %s)" % (field["topic"], kind, ", ".join(KINDS)))
    return out, fields


# ---------------------------------------------------------------- OpenOCD

def start_openocd(args, log_path):
    if args.use_running:
        return None
    log = log_path.open("w", encoding="utf-8", errors="replace")
    process = subprocess.Popen(openocd_command(args), stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            socket.create_connection((args.host, args.port), timeout=1).close()
            return process
        except OSError:
            time.sleep(0.5)
    stop_openocd(process)
    raise SystemExit("OpenOCD не поднялся на порт %d (журнал: %s)" % (args.port, log_path))


def stop_openocd(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class Telnet(object):
    """Минимальный клиент telnet OpenOCD: команда -> текст ответа до приглашения '> '."""

    def __init__(self, host, port, timeout=2.0):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(timeout)
        self.buffer = b""
        self._read_until_prompt()  # приветствие и согласование telnet

    @staticmethod
    def _clean(data):
        # Последовательности согласования telnet (IAC = 0xFF) и управляющие символы не нужны.
        data = re.sub(rb"\xff[\xfb-\xfe].|\xff[\xf0-\xfa]", b"", data)
        return data.decode("utf-8", "replace").replace("\r", "").replace("\x00", "")

    def _read_until_prompt(self):
        while True:
            text = self._clean(self.buffer)
            position = text.rfind("\n> ")
            if position >= 0 or text.startswith("> ") or text.endswith("> "):
                self.buffer = b""
                return text
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                self.buffer = b""
                return None
            if not chunk:
                raise ConnectionError("OpenOCD закрыл соединение")
            self.buffer += chunk

    def command(self, line):
        self.sock.sendall((line + "\n").encode("ascii"))
        return self._read_until_prompt()

    def close(self):
        self.sock.close()


def read_words(telnet, addresses):
    """Слова по выровненным адресам: соседние читаются одной командой mdw. Возвращает {адрес: слово}."""
    words = {}
    pending = sorted(set(addresses))
    while pending:
        start = pending[0]
        group = [a for a in pending if a - start < CLUSTER_BYTES]
        pending = pending[len(group):]
        count = (group[-1] - start) // 4 + 1
        text = telnet.command("mdw 0x%08X %d" % (start, count))
        if not text:
            continue
        for match in WORD_LINE.finditer(text):
            base = int(match.group(1), 16)
            for index, value in enumerate(match.group(2).split()):
                words[base + 4 * index] = int(value, 16)
    return words


class OpenOcdReader(object):
    """Чтение слов через telnet OpenOCD (mdw)."""

    def __init__(self, host, port):
        self.telnet = Telnet(host, port)

    def read_words(self, addresses):
        return read_words(self.telnet, addresses)

    def close(self):
        self.telnet.close()


class RenodeReader(object):
    """Чтение слов через telnet-монитор Renode: эмуляция при этом не останавливается.

    Команды `sysbus ReadDoubleWord` отправляются пакетом, ответы разбираются по эху адреса.
    """

    def __init__(self, host, port, machine="", timeout=2.0):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(timeout)
        time.sleep(0.3)
        self._drain()  # приветствие и вывод команд, выполненных до подключения
        if machine:
            self.sock.sendall(('mach set "%s"\n' % machine).encode("utf-8"))
        else:
            self.sock.sendall(b"\n")
        text = self._read_prompts(1)
        names = RENODE_PROMPT.findall(text or "")
        self.machine = names[-1] if names else ""
        if not self.machine:
            self.close()
            raise SystemExit("монитор Renode не ответил на %s:%d" % (host, port))
        if self.machine == "monitor":
            self.close()
            raise SystemExit("в мониторе Renode не выбрана машина: --machine ИМЯ (как в mach create)")

    def _drain(self):
        self.sock.settimeout(0.2)
        try:
            while self.sock.recv(65536):
                pass
        except socket.timeout:
            pass
        finally:
            self.sock.settimeout(2.0)

    def _read_prompts(self, count):
        """Текст до `count` приглашений вида '(машина) ' или до тайм-аута."""
        data = b""
        while True:
            text = Telnet._clean(data)
            if len(RENODE_PROMPT.findall(text)) >= count:
                return text
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return text
            if not chunk:
                raise ConnectionError("Renode закрыл соединение")
            data += chunk

    def read_words(self, addresses):
        pending = sorted(set(addresses))
        lines = "".join("sysbus ReadDoubleWord 0x%08X\n" % address for address in pending)
        self.sock.sendall(lines.encode("ascii"))
        text = self._read_prompts(len(pending))
        return {int(address, 16): int(value, 16) for address, value in RENODE_VALUE.findall(text)}

    def close(self):
        self.sock.close()


def open_reader(args):
    if args.backend == "renode":
        return RenodeReader(args.host, args.port, args.machine)
    return OpenOcdReader(args.host, args.port)


def aligned_needed(address):
    base = address & ~3
    return [base] if base == address else [base, base + 4]


def word_at(words, address):
    """32-битное слово little-endian по любому адресу (невыровненный собирается из двух)."""
    base, offset = address & ~3, address & 3
    if offset == 0:
        return words.get(base)
    low, high = words.get(base), words.get(base + 4)
    if low is None or high is None:
        return None
    return ((low | (high << 32)) >> (8 * offset)) & 0xFFFFFFFF


def convert(word, kind, shift=0, mask=0xFFFFFFFF):
    if kind == "float32":
        return struct.unpack("<f", struct.pack("<I", word))[0]
    value = (word & mask) >> shift
    if kind == "uint32_seconds":
        return value // 1000
    if kind == "int32":
        return value - (1 << 32) if value & 0x80000000 else value
    return value


# ---------------------------------------------------------------- доктор

def doctor(args):
    """Совместимость окружения, пробника и цели с теми же ключами, что и у записи.

    Ядро не останавливается и не сбрасывается (если конфигурация цели сама не сбрасывает при подключении).
    """
    checks = [("Python 3.9+", sys.version_info >= (3, 9), sys.version.split()[0])]
    elf = find_elf(args.elf)
    checks.append(("ELF прошивки", elf.is_file(), str(elf)))
    checks.append(("Спецификация полей", pathlib.Path(args.fields).is_file(), str(args.fields)))
    checks.append(("SVD (только для полей svd:)", True, str(args.svd or "не задан: поля svd: недоступны")))
    address = None
    if elf.is_file() and pathlib.Path(args.fields).is_file():
        try:
            saved, args.addresses = args.addresses, ""
            try:
                with tempfile.TemporaryDirectory() as work:
                    _out, fields = resolve(args, elf, pathlib.Path(work))
            finally:
                args.addresses = saved
            address = fields[0]["address"] if fields else None
            checks.append(("Адреса полей (GDB по ELF)", bool(fields), "полей %d, первое %s = 0x%08X"
                           % (len(fields), fields[0]["topic"], address) if fields else "полей нет"))
        except SystemExit as error:
            checks.append(("Адреса полей (GDB по ELF)", False, str(error).splitlines()[-1]))

    details = []
    if args.backend == "renode":
        name = "Монитор Renode %s:%d" % (args.host, args.port)
        try:
            reader = RenodeReader(args.host, args.port, args.machine)
            checks.append((name, True, "машина %s" % reader.machine))
            if address is not None:
                words = reader.read_words(aligned_needed(address))
                value = word_at(words, address)
                checks.append(("Чтение памяти на ходу", value is not None,
                               "0x%08X = 0x%08X" % (address, value) if value is not None else "нет ответа"))
            reader.close()
        except (OSError, SystemExit) as error:
            checks.append((name, False, str(error) if isinstance(error, SystemExit) else
                           "не слушает: renode --port %d" % args.port))
    elif not args.openocd:
        checks.append(("OpenOCD", False, "нет в PATH: --openocd"))
    elif args.use_running:
        try:
            socket.create_connection((args.host, args.port), timeout=1).close()
            checks.append(("OpenOCD telnet %s:%d" % (args.host, args.port), True, "слушает"))
        except OSError:
            checks.append(("OpenOCD telnet %s:%d" % (args.host, args.port), False, "не слушает: --port"))
    else:
        checks.append(("OpenOCD", True, args.openocd))
        try:
            run = subprocess.run(openocd_probe_command(args, address), capture_output=True, text=True, timeout=60)
            text = run.stdout + run.stderr
        except (OSError, subprocess.SubprocessError) as error:
            text = "Error: openocd не запустился: %s" % error
        info = parse_openocd(text)
        checks.append(("Конфигурации OpenOCD", not info["missing"],
                       "найдены" if not info["missing"] else "нет: %s" % ", ".join(info["missing"])))
        if not info["missing"]:
            adapter_error = [line for line in info["errors"] if not info["examined"]]
            checks.append(("Пробник (%s)" % args.interface, bool(info["adapter"]) or info["examined"],
                           (info["adapter"] or adapter_error or ["нет ответа"])[0][:70]))
            state = ", ".join("%s %s" % (t["name"], t["state"]) for t in info["targets"]) or "нет"
            checks.append(("Цель (%s)" % args.target, info["examined"],
                           "%s; %s" % (info["core"] or "ядро не определено", state)))
            if address is not None and info["examined"]:
                checks.append(("Чтение памяти на ходу", info["word"] is not None,
                               "0x%08X = 0x%08X" % info["word"] if info["word"] else "mdw не ответил"))
        details = info["adapter"] + info["errors"]

    print("доктор (%s; ядро не останавливается, плата не сбрасывается):" % args.backend)
    failed = 0
    for name, ok, detail in checks:
        failed += 0 if ok else 1
        print("  %-34s %-4s %s" % (name, "ок" if ok else "НЕТ", detail[:80]))
    if details:
        print("\n  OpenOCD:")
        for line in details[:12]:
            print("    " + line[:110])
    if failed and args.backend == "openocd":
        print("\n  проверьте --interface, --target, --serial; дополнительные команды OpenOCD — --openocd-command")
    print("\n  провалено: %d из %d" % (failed, len(checks)))
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------- запись

def record(args, fields, out):
    writer_class = McapWriter if args.record == "mcap" else BagWriter
    needed = [a for field in fields for a in aligned_needed(field["address"])]
    stats = {"cycles": 0, "written": 0, "read_failures": 0, "out_of_range": 0, "interrupted": False}
    storage = None
    reader = open_reader(args)
    try:
        if args.record == "mcap":
            storage = writer_class(out, chunk_messages=args.chunk_size)
        else:
            storage = writer_class(out, compression=None if args.compress == "none" else args.compress)
        for field in fields:
            storage.add_connection(field["topic"], field["message"])
        print("запись %.0f с, %.0f Гц -> %s" % (args.seconds, args.rate, out.name))
        period = 1.0 / args.rate
        started = time.monotonic()
        try:
            while time.monotonic() - started < args.seconds:
                cycle = time.monotonic()
                words = reader.read_words(needed)
                moment = time.time()  # метка хоста; для точного времени добавьте счётчик платы в поля
                stats["cycles"] += 1
                for field in fields:
                    word = word_at(words, field["address"])
                    if word is None:
                        stats["read_failures"] += 1
                        continue
                    value = convert(word, field.get("kind", "uint32"), field.get("shift", 0), field.get("mask", 0xFFFFFFFF))
                    if (field.get("min") is not None and value < field["min"]) or \
                       (field.get("max") is not None and value > field["max"]):
                        stats["out_of_range"] += 1
                        continue
                    storage.write(field["topic"], value, int(moment), int((moment % 1) * 1e9))
                    stats["written"] += 1
                left = period - (time.monotonic() - cycle)
                if left > 0:
                    time.sleep(left)
        except KeyboardInterrupt:
            stats["interrupted"] = True  # досрочная остановка: записанное сохраняется
        stats["elapsed"] = time.monotonic() - started
    finally:
        if storage is not None:
            storage.close()
        reader.close()
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fields", default=str(HERE / "fields.toml"), help="спецификация полей (TOML)")
    parser.add_argument("--elf", default=os.environ.get("FIRMWARE_ELF", ""), help="ELF (по умолчанию $FIRMWARE_ELF или build/)")
    parser.add_argument("--svd", default=os.environ.get("STM32_SVD", ""), help="SVD для полей svd: (по умолчанию $STM32_SVD)")
    parser.add_argument("--addresses", default="", help="куда сохранить JSON адресов (по умолчанию рядом с записью)")
    parser.add_argument("--serial", default=os.environ.get("STLINK_SERIAL", ""), help="серийный номер пробника ($STLINK_SERIAL)")
    parser.add_argument("--interface", default="interface/stlink.cfg", help="конфигурация пробника OpenOCD (stlink, cmsis-dap, jlink, ...)")
    parser.add_argument("--target", default="target/stm32f4x.cfg", help="конфигурация цели OpenOCD (target/stm32f1x.cfg, target/nrf52.cfg, ...)")
    parser.add_argument("--openocd-command", action="append", default=[], metavar="CMD",
                        help="команда OpenOCD после конфигурации цели, до init (можно несколько раз)")
    parser.add_argument("--reset", action="store_true", help="сбросить цель после подключения (по умолчанию — без сброса)")
    parser.add_argument("--record", "--format", dest="record", choices=["bag", "mcap"], default="mcap")
    parser.add_argument("--out", default="", help="файл записи (по умолчанию <каталог ELF>/diag/record-<дата>.<формат>)")
    parser.add_argument("--seconds", type=float, default=10.0, help="длительность записи")
    parser.add_argument("--rate", type=float, default=10.0, help="частота опроса, Гц")
    parser.add_argument("--compress", choices=["none", "bz2"], default="none", help="сжатие чанка bag")
    parser.add_argument("--chunk-size", type=int, default=1000, help="сообщений на чанк mcap")
    parser.add_argument("--backend", choices=["openocd", "renode"], default="openocd",
                        help="откуда читать: OpenOCD (плата) или монитор Renode (эмуляция)")
    parser.add_argument("--machine", default="", help="Renode: имя машины, если в мониторе их несколько")
    parser.add_argument("--host", default="127.0.0.1", help="адрес telnet OpenOCD или монитора Renode")
    parser.add_argument("--port", type=int, default=None, help="порт telnet: OpenOCD 4444, Renode 1234")
    parser.add_argument("--openocd", default=shutil.which("openocd") or "", help="путь к openocd")
    parser.add_argument("--use-running", action="store_true", help="не запускать OpenOCD, подключиться к работающему")
    parser.add_argument("--doctor", action="store_true", help="проверка окружения без записи")
    parser.add_argument("--dryrun", action="store_true", help="показать план без записи")
    args = parser.parse_args(argv)

    if args.rate <= 0 or args.seconds <= 0:
        parser.error("--rate и --seconds должны быть положительными")
    if args.port is None:
        args.port = DEFAULT_PORTS[args.backend]
    if args.backend == "renode":
        args.use_running = True  # Renode запускается отдельно: OpenOCD не нужен
    if args.doctor:
        return doctor(args)
    if not args.openocd and not args.use_running:
        print("ОШИБКА: openocd не найден в PATH (--openocd или --use-running)")
        return 1
    elf = find_elf(args.elf)
    if not elf.is_file():
        print("ОШИБКА: нет ELF %s (укажите --elf)" % elf)
        return 1
    if not pathlib.Path(args.fields).is_file():
        print("ОШИБКА: нет файла полей %s" % args.fields)
        return 1

    out = pathlib.Path(args.out) if args.out else \
        elf.parent / "diag" / ("record-%s.%s" % (time.strftime("%Y%m%d-%H%M%S"), args.record))
    out = out.resolve()

    if args.dryrun:
        print("=== DRYRUN: ничего не пишется, OpenOCD не запускается ===")
        print("  ELF:      %s" % elf)
        print("  поля:     %s" % args.fields)
        print("  формат:   %s -> %s" % (args.record, out))
        print("  длина:    %.0f с, частота %.0f Гц" % (args.seconds, args.rate))
        if args.backend == "renode":
            print("  Renode:   монитор %s:%d (запуск отдельно: renode --port %d)" % (args.host, args.port, args.port))
        elif args.use_running:
            print("  OpenOCD:  уже запущен, telnet %s:%d" % (args.host, args.port))
        else:
            print("  OpenOCD:  %s" % " ".join(openocd_command(args)))
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    addresses, fields = resolve(args, elf, out.parent)
    print("адреса: %s (полей %d)" % (addresses, len(fields)))

    process = start_openocd(args, out.with_suffix(".openocd.log"))
    try:
        stats = record(args, fields, out)
    finally:
        stop_openocd(process)

    achieved = stats["cycles"] / stats["elapsed"] if stats["elapsed"] > 0 else 0.0
    print("готово: %s (%d байт)" % (out, out.stat().st_size))
    print("  сообщений %d, циклов %d, частота %.1f Гц из %.1f, сбоев чтения %d, вне диапазона %d%s"
          % (stats["written"], stats["cycles"], achieved, args.rate, stats["read_failures"], stats["out_of_range"],
             ", остановлено досрочно" if stats["interrupted"] else ""))
    if achieved < 0.9 * args.rate and stats["cycles"] > 1:
        print("  ВНИМАНИЕ: частота ниже заданной: меньше полей, ближе адреса или ниже --rate")
    mcap = shutil.which("mcap")
    if args.record == "mcap" and mcap:
        info = subprocess.run([mcap, "info", str(out)], capture_output=True, text=True)
        for line in info.stdout.splitlines():
            if line.startswith(("messages:", "channels:")):
                print("  " + line)
    return 0 if stats["written"] else 1


if __name__ == "__main__":
    sys.exit(main())
