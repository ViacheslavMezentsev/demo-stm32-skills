#!/usr/bin/env python3
"""Аддитивная диагностика через RTT и свободный таймер (--backend rtt-timer навыка stm32-diag-record).

ПРОТОТИП. Снимает поля прошивки быстрее базового режима: выборку делает заглушка в SRAM по прерыванию
свободного таймера и пишет кадры в кольцевой буфер RTT, а хост читает их через `rtt server` OpenOCD. Код
прошивки во флеше не меняется; вектор таймера берётся из копии таблицы в SRAM (VTOR). Это АДДИТИВНЫЙ приём,
а не базовое чтение: он включает таймер, занимает прерывание и окно SRAM, поэтому меняет поведение цели.
Перехват всегда снимается в конце (в том числе по Ctrl+C): таймер и NVIC выключаются, VTOR и RCC
восстанавливаются.

ПОСЛЕДОВАТЕЛЬНОСТЬ. resolve_addresses -> build_rtt_image -> OpenOCD: halt, залить образ, настроить таймер и
VTOR, resume -> rtt setup/start/server -> чтение TCP :19021 -> запись mcap/bag с метками по tick прошивки.

Поддержаны Cortex-M3/M4 стендов stm32-gdbtest (f103c8, f401cc, f411ce, f429zi, at32f403a); f030r8 (M0) —
нет VTOR, режим недоступен.
"""
import json
import pathlib
import socket
import struct
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import record_diag as diag  # noqa: E402
from rtt_frames import FrameReader  # noqa: E402

RTT_SERVER_PORT = 19021


def mdw(telnet, address):
    text = telnet.command("mdw 0x%08X" % address) or ""
    for line in text.splitlines():
        parts = line.replace(":", "").split()
        if len(parts) >= 2:
            try:
                return int(parts[1], 16)
            except ValueError:
                continue
    raise RuntimeError("mdw 0x%08X не вернул значение: %r" % (address, text))


def mww(telnet, address, value):
    telnet.command("mww 0x%08X 0x%08X" % (address, value & 0xFFFFFFFF))


def set_bits(telnet, address, mask):
    mww(telnet, address, mdw(telnet, address) | mask)


def resolve_tick(args, elf, workdir):
    """Адрес метки времени: --tick-address (hex) или выражение --tick-expr по ELF."""
    if args.tick_address:
        return int(args.tick_address, 16)
    spec = workdir / "tick.toml"
    spec.write_text('[[field]]\ntopic = "/tick"\nmessage = "std_msgs/UInt32"\nkind = "uint32"\nexpr = "%s"\n'
                    % args.tick_expr, encoding="utf-8")
    out = workdir / "tick.json"
    command = [sys.executable, "-B", str(HERE.parent / "resolve_addresses.py"), "--elf", str(elf),
               "--spec", str(spec), "--out", str(out)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not out.is_file():
        raise SystemExit("не удалось вычислить адрес метки времени (--tick-expr %s):\n%s"
                         % (args.tick_expr, (result.stdout + result.stderr)[-500:]))
    return json.loads(out.read_text(encoding="utf-8"))["fields"][0]["address"]


def build_image(args, elf, addresses, tick_address, workdir):
    out = workdir / "rtt.bin"
    command = [sys.executable, "-B", str(HERE / "build_rtt_image.py"), "--elf", str(elf),
               "--addresses", str(addresses), "--board", args.board, "--boards", str(HERE / "boards.toml"),
               "--toolchain", args.toolchain, "--sram-base", args.sram_base, "--buffer", str(args.buffer),
               "--tick-address", "0x%08X" % tick_address, "--rate", str(args.rate), "--out", str(out)]
    if args.timer_clock_hz:
        command += ["--timer-clock-hz", str(args.timer_clock_hz)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not out.with_suffix(".layout.json").is_file():
        raise SystemExit("сборка образа не удалась:\n%s" % (result.stdout + result.stderr)[-700:])
    print(result.stdout.rstrip())
    return out, json.loads(out.with_suffix(".layout.json").read_text(encoding="utf-8"))


def install(telnet, layout, image_path):
    """Залить образ и включить перехват. Возвращает исходные VTOR и RCC для восстановления."""
    timer, nvic = layout["timer"], layout["nvic"]
    saved = {"vtor": mdw(telnet, layout["vtor"]), "rcc": mdw(telnet, layout["rcc"]["enable_reg"])}
    telnet.command("halt")
    telnet.command("load_image {%s} 0x%08X bin" % (image_path.as_posix(), layout["sram_base"]))
    set_bits(telnet, layout["rcc"]["enable_reg"], layout["rcc"]["enable_bit"])   # тактирование таймера
    mww(telnet, timer["cr1"], 0)
    mww(telnet, timer["psc_reg"], timer["psc"])
    mww(telnet, timer["arr_reg"], timer["arr"])
    mww(telnet, timer["egr"], timer["bit_ug"])          # загрузить PSC/ARR
    mww(telnet, timer["sr"], 0)                          # снять флаг обновления
    mww(telnet, timer["dier"], timer["bit_uie"])        # разрешить прерывание по обновлению
    mww(telnet, layout["vtor"], layout["vectors"]["address"])
    mww(telnet, nvic["iser"], 1 << nvic["bit"])          # включить IRQ таймера
    mww(telnet, timer["cr1"], timer["bit_cen"])          # запустить таймер последним
    telnet.command("resume")
    return saved


def teardown(telnet, layout, saved):
    """Снять перехват: остановить таймер и прерывание, вернуть VTOR и RCC."""
    timer, nvic = layout["timer"], layout["nvic"]
    try:
        telnet.command("rtt server stop %d" % RTT_SERVER_PORT)
        telnet.command("rtt stop")
    except OSError:
        pass
    telnet.command("halt")
    mww(telnet, timer["cr1"], 0)                          # стоп таймер
    mww(telnet, timer["dier"], 0)
    mww(telnet, nvic["icer"], 1 << nvic["bit"])           # выключить IRQ
    mww(telnet, nvic["icpr"], 1 << nvic["bit"])           # снять pending
    mww(telnet, layout["vtor"], saved["vtor"])
    mww(telnet, layout["rcc"]["enable_reg"], saved["rcc"])
    telnet.command("resume")


def read_stream(telnet, layout, args, out):
    """Запустить RTT-сервер, прочитать поток, записать файл. Возвращает статистику."""
    cb, size = layout["rtt"]["control_block"], layout["rtt"]["size"]
    telnet.command('rtt setup 0x%08X 0x%X "%s"' % (cb, size, layout["rtt"]["id"]))
    start = telnet.command("rtt start") or ""
    if "found" not in start.lower() and "Control block" not in start:
        raise SystemExit("OpenOCD не нашёл управляющий блок RTT:\n%s" % start.strip()[-300:])
    telnet.command("rtt polling_interval %d" % max(getattr(args, "rtt_poll_ms", 10), 1))  # чаще опрос — выше слив
    telnet.command("rtt server start %d 0" % RTT_SERVER_PORT)
    reader = FrameReader(layout)
    writer_class = diag.McapWriter if args.record == "mcap" else diag.BagWriter
    if args.record == "mcap":
        storage = writer_class(out, chunk_messages=args.chunk_size)
    else:
        storage = writer_class(out, compression=None if args.compress == "none" else args.compress)
    for field in layout["fields"]:
        storage.add_connection(field["topic"], field["message"])
    written = out_of_range = 0
    sock = socket.create_connection((args.host, RTT_SERVER_PORT), timeout=5)
    sock.settimeout(1.0)
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            for seq, tick, values in reader.feed(chunk):
                sec, nsec = tick // 1000, (tick % 1000) * 1_000_000   # метка по tick прошивки (мс)
                for topic, message, value, in_range in values:
                    if not in_range:
                        out_of_range += 1
                        continue
                    storage.write(topic, value, sec, nsec)
                    written += 1
    except KeyboardInterrupt:
        print("  остановлено досрочно (Ctrl+C)")
    finally:
        sock.close()
        storage.close()
    stats = dict(reader.stats, written=written, out_of_range=out_of_range)
    return stats


def run(args):
    """Вызывается из record_diag при --backend rtt-timer."""
    if not args.board:
        raise SystemExit("для --backend rtt-timer нужен --board (f411ce, f103c8, f401cc, f429zi, at32f403a)")
    if not args.sram_base:
        raise SystemExit("нужен --sram-base: свободное окно SRAM (hex), см. --doctor и карту памяти ELF")
    if not args.toolchain:
        raise SystemExit("нужен --toolchain: каталог bin тулчейна arm-none-eabi (для сборки заглушки)")
    elf = diag.find_elf(args.elf)
    if not elf.is_file():
        raise SystemExit("нет ELF %s (--elf)" % elf)
    out = pathlib.Path(args.out) if args.out else elf.parent / "diag" / (
        "rtt-%s.%s" % (time.strftime("%Y%m%d-%H%M%S"), args.record))
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    workdir = out.parent

    addresses, _fields = diag.resolve(args, elf, workdir)
    tick_address = resolve_tick(args, elf, workdir)
    image_path, layout = build_image(args, elf, addresses, tick_address, workdir)

    if args.dryrun:
        print("=== DRYRUN rtt-timer: образ собран, цель не трогается ===")
        print("  образ:   %s (%d байт, SRAM 0x%08X..0x%08X)" % (image_path, layout["image_size"],
              layout["sram_base"], layout["end"]))
        print("  таймер:  %s IRQ %d, ~%.1f Гц; VTOR -> 0x%08X" % (layout["timer"]["name"],
              layout["timer"]["irq"], layout["timer"]["actual_hz"], layout["vectors"]["address"]))
        print("  RTT:     блок 0x%08X, буфер 0x%08X (%d байт), сервер :%d"
              % (layout["rtt"]["control_block"], layout["rtt"]["buffer"], layout["rtt"]["size"], RTT_SERVER_PORT))
        return 0

    process = diag.start_openocd(args, out.with_suffix(".openocd.log"))
    telnet = diag.Telnet(args.host, args.port)
    saved = None
    try:
        print("АДДИТИВНЫЙ режим: включаю таймер %s и перехват вектора (VTOR). Снимается в конце."
              % layout["timer"]["name"])
        saved = install(telnet, layout, image_path)
        print("запись %.0f с, ~%.0f Гц -> %s" % (args.seconds, layout["timer"]["actual_hz"], out.name))
        stats = read_stream(telnet, layout, args, out)
    finally:
        if saved is not None:
            try:
                teardown(telnet, layout, saved)
                print("перехват снят: таймер остановлен, VTOR и RCC восстановлены")
            except OSError as error:
                print("ВНИМАНИЕ: не удалось снять перехват (%s). Снимите сбросом платы." % error)
        telnet.close()
        diag.stop_openocd(process)

    print("готово: %s (%d байт)" % (out, out.stat().st_size))
    print("  кадров %d, записано %d, потери (оценка) %d, разрывов seq %d, вне диапазона %d, ресинхр. %d"
          % (stats["frames"], stats["written"], stats["dropped_est"], stats["seq_gaps"],
             stats["out_of_range"], stats["resyncs"]))
    if stats["dropped_est"]:
        print("  потери кадров: цель пишет быстрее, чем хост читает кольцо — увеличьте --buffer или снизьте --rate")
    return 0 if stats["written"] else 1
