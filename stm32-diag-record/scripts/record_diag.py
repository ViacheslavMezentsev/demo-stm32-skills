#!/usr/bin/env python3
"""Запись диагностики с платы без ROS: ST-Link -> OpenOCD -> файл bag или mcap.

НАЗНАЧЕНИЕ. Снять выбранные поля прошивки в файл для офлайн-разбора (PlotJuggler, `mcap info`,
`rosbag info`). ROS не нужен ни для записи, ни для чтения: форматы пишут `bag_writer.py` и
`mcap_writer.py` на стандартной библиотеке.

ЦЕПОЧКА. ST-Link (SWD) -> OpenOCD (telnet) -> чтение 32-битных слов по адресам -> сериализация
сообщений -> файл. Адреса считаются офлайн по ELF (и SVD), плата при этом не нужна. OpenOCD
подключается без сброса: прошивка продолжает работать (сброс — только по ключу --reset).

КЛЮЧИ СОВПАДАЮТ С НАВЫКОМ stm32-ros-alt-channel там, где смысл общий (--out, --format/--record,
--seconds, --elf, --svd, --doctor, --dryrun).

ПРИМЕРЫ.
    python record_diag.py --doctor
    python record_diag.py --dryrun --seconds 10
    python record_diag.py --record mcap --seconds 30
    python record_diag.py --record bag --seconds 30 --compress bz2 --out build/my.bag
    python record_diag.py --target target/stm32f1x.cfg --serial <серийный номер> --record mcap
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
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bag_writer import BagWriter  # noqa: E402
from mcap_writer import McapWriter  # noqa: E402

KINDS = ("uint32", "int32", "float32", "uint32_seconds")
WORD_LINE = re.compile(r"^0x([0-9a-fA-F]+):((?:\s+[0-9a-fA-F]{8})+)", re.M)
CLUSTER_BYTES = 256  # соседние адреса в пределах окна читаются одной командой mdw


# ---------------------------------------------------------------- окружение

def find_elf(explicit):
    """ELF из ключа, иначе первый подходящий в build/ текущего каталога."""
    if explicit:
        return pathlib.Path(explicit)
    candidates = sorted(pathlib.Path("build").glob("*/firmware.elf")) + sorted(pathlib.Path("build").glob("*/*.elf"))
    return candidates[0] if candidates else pathlib.Path("build/firmware.elf")


def list_probes(args):
    """Подключённые пробники: сведения печатает `init` интерфейса (без цели, ядро не трогается)."""
    try:
        result = subprocess.run([args.openocd, "-f", args.interface, "-c", "init", "-c", "shutdown"],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        return ["openocd не запустился: %s" % error]
    lines = [line.strip() for line in (result.stdout + result.stderr).splitlines()]
    probes = [line for line in lines if "stlink" in line.lower() or "serial" in line.lower()]
    return probes[:6] or ["пробник не найден: подключён ли отладчик?"]


def openocd_command(args):
    command = [args.openocd, "-f", args.interface]
    if args.serial:
        command += ["-c", "adapter serial %s" % args.serial]
    command += ["-f", args.target, "-c", "bindto 127.0.0.1", "-c", "gdb_port disabled", "-c", "tcl_port disabled",
                "-c", "telnet_port %d" % args.port, "-c", "init"]
    if args.reset:
        command += ["-c", "reset run"]
    return command


# ---------------------------------------------------------------- адреса

def resolve(args, elf, out_dir):
    """Пересчитать адреса по ELF и SVD (офлайн). Возвращает список полей с адресами."""
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
    checks = [("Python 3.9+", sys.version_info >= (3, 9), sys.version.split()[0]),
              ("OpenOCD", bool(args.openocd), args.openocd or "нет в PATH: --openocd")]
    elf = find_elf(args.elf)
    checks.append(("ELF прошивки", elf.is_file(), str(elf)))
    checks.append(("Спецификация полей", pathlib.Path(args.fields).is_file(), str(args.fields)))
    checks.append(("SVD (только для полей svd:)", True, str(args.svd or "не задан: поля svd: недоступны")))
    free = True
    try:
        socket.create_connection((args.host, args.port), timeout=1).close()
        free = args.use_running
    except OSError:
        free = not args.use_running
    checks.append(("Порт telnet %d" % args.port, free,
                   "OpenOCD уже слушает" if args.use_running else "свободен или занят: --use-running/--port"))

    print("доктор окружения (ядро не останавливается, плата не сбрасывается):")
    failed = 0
    for name, ok, detail in checks:
        failed += 0 if ok else 1
        print("  %-30s %-4s %s" % (name, "ок" if ok else "НЕТ", detail[:70]))
    if args.openocd and not args.use_running:
        print("\n  пробники (%s):" % args.interface)
        for line in list_probes(args):
            print("    " + line[:100])
        print("  без --serial OpenOCD берёт первый найденный")
    print("\n  провалено: %d из %d" % (failed, len(checks)))
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------- запись

def record(args, fields, out):
    writer_class = McapWriter if args.record == "mcap" else BagWriter
    needed = [a for field in fields for a in aligned_needed(field["address"])]
    stats = {"cycles": 0, "written": 0, "read_failures": 0, "out_of_range": 0, "interrupted": False}
    storage = None
    telnet = Telnet(args.host, args.port)
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
                words = read_words(telnet, needed)
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
        telnet.close()
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fields", default=str(HERE / "fields.toml"), help="спецификация полей (TOML)")
    parser.add_argument("--elf", default=os.environ.get("FIRMWARE_ELF", ""), help="ELF (по умолчанию $FIRMWARE_ELF или build/)")
    parser.add_argument("--svd", default=os.environ.get("STM32_SVD", ""), help="SVD для полей svd: (по умолчанию $STM32_SVD)")
    parser.add_argument("--addresses", default="", help="куда сохранить JSON адресов (по умолчанию рядом с записью)")
    parser.add_argument("--serial", default=os.environ.get("STLINK_SERIAL", ""), help="серийный номер пробника ($STLINK_SERIAL)")
    parser.add_argument("--interface", default="interface/stlink.cfg", help="конфигурация пробника OpenOCD")
    parser.add_argument("--target", default="target/stm32f4x.cfg", help="конфигурация цели OpenOCD (F1: target/stm32f1x.cfg)")
    parser.add_argument("--reset", action="store_true", help="сбросить цель после подключения (по умолчанию — без сброса)")
    parser.add_argument("--record", "--format", dest="record", choices=["bag", "mcap"], default="mcap")
    parser.add_argument("--out", default="", help="файл записи (по умолчанию <каталог ELF>/diag/record-<дата>.<формат>)")
    parser.add_argument("--seconds", type=float, default=10.0, help="длительность записи")
    parser.add_argument("--rate", type=float, default=10.0, help="частота опроса, Гц")
    parser.add_argument("--compress", choices=["none", "bz2"], default="none", help="сжатие чанка bag")
    parser.add_argument("--chunk-size", type=int, default=1000, help="сообщений на чанк mcap")
    parser.add_argument("--host", default="127.0.0.1", help="адрес telnet OpenOCD (для --use-running)")
    parser.add_argument("--port", type=int, default=4444, help="порт telnet OpenOCD")
    parser.add_argument("--openocd", default=shutil.which("openocd") or "", help="путь к openocd")
    parser.add_argument("--use-running", action="store_true", help="не запускать OpenOCD, подключиться к работающему")
    parser.add_argument("--doctor", action="store_true", help="проверка окружения без записи")
    parser.add_argument("--dryrun", action="store_true", help="показать план без записи")
    args = parser.parse_args(argv)

    if args.rate <= 0 or args.seconds <= 0:
        parser.error("--rate и --seconds должны быть положительными")
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
        if args.use_running:
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
