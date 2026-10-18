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
import re
import socket
import struct
import subprocess
import sys
import time

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10
    import tomli as tomllib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import record_diag as diag  # noqa: E402
from rtt_frames import FrameReader  # noqa: E402

RTT_SERVER_PORT = 19021
SEQ_TOPIC = "/_diag/rtt/seq"
TICK_TOPIC = "/_diag/rtt/tick_ms"


def validate_topics(layout):
    topics = [field["topic"] for field in layout["fields"]]
    if len(set(topics)) != len(topics) or set(topics) & {SEQ_TOPIC, TICK_TOPIC}:
        raise ValueError("топики полей должны быть уникальными и не занимать /_diag/rtt/seq, /_diag/rtt/tick_ms")


def mdw(telnet, address):
    """Прочитать слово по адресу через OpenOCD. Разбирает строку '0x...: значение', игнорируя
    асинхронные сообщения и управляющие символы (telnet OpenOCD любит их подмешивать)."""
    text = telnet.command("mdw 0x%08X" % address) or ""
    # Значение принимается только из строки с тем же адресом: чужой ответ не выдаётся за наш.
    match = re.search(r"0x0*%x:\s*([0-9a-f]{1,8})\b" % address, text, re.IGNORECASE)
    if match:
        return int(match.group(1), 16)
    raise RuntimeError("mdw 0x%08X не вернул значение: %r" % (address, text[-60:]))


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
    if getattr(args, "timer", ""):
        command += ["--timer", args.timer]
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
    """Записать поля, исходные seq/tick и итоговый JSON, включая частичный сеанс."""
    validate_topics(layout)
    out = pathlib.Path(out)
    reader = FrameReader(layout)
    stats = reader.stats
    stats.update(written=0, metadata_written=0, out_of_range=0)
    report = {
        "schema": 1, "backend": "rtt-timer", "record": out.name, "board": layout.get("board"),
        "seq_topic": SEQ_TOPIC, "tick_topic": TICK_TOPIC,
        "seq_bits": 16, "tick_bits": 32, "tick_unit": "ms",
        "loss_scope": "between_received_frames_modulo_65536",
        "target_dropped": None,
        "requested_seconds": args.seconds,
        "timer": layout["timer"], "frame": layout["frame"],
        "buffer_bytes": layout["rtt"]["size"],
        "rtt_poll_ms": max(getattr(args, "rtt_poll_ms", 10), 1),
        "stop_reason": "error", "record_closed": False, "stats": stats,
    }
    storage = sock = None
    started = time.monotonic()
    try:
        cb, size = layout["rtt"]["control_block"], layout["rtt"]["size"]
        telnet.command('rtt setup 0x%08X 0x%X "%s"' % (cb, size, layout["rtt"]["id"]))
        start = telnet.command("rtt start") or ""
        if "found" not in start.lower() and "Control block" not in start:
            raise RuntimeError("OpenOCD не нашёл управляющий блок RTT: %s" % start.strip()[-300:])
        telnet.command("rtt polling_interval %d" % report["rtt_poll_ms"])
        telnet.command("rtt server start %d 0" % RTT_SERVER_PORT)
        if args.record == "mcap":
            storage = diag.McapWriter(out, chunk_messages=args.chunk_size)
        else:
            storage = diag.BagWriter(out, compression=None if args.compress == "none" else args.compress)
        for field in layout["fields"]:
            storage.add_connection(field["topic"], field["message"])
        for topic in (SEQ_TOPIC, TICK_TOPIC):
            storage.add_connection(topic, "std_msgs/UInt32")
        sock = socket.create_connection((args.host, RTT_SERVER_PORT), timeout=5)
        sock.settimeout(1.0)
        deadline = time.monotonic() + args.seconds
        report["stop_reason"] = "duration"
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                report["stop_reason"] = "eof"
                break
            for seq, tick, values in reader.feed(chunk):
                sec, nsec = tick // 1000, (tick % 1000) * 1_000_000
                # seq is payload, not the writer's independent per-channel sequence.
                storage.write(SEQ_TOPIC, seq, sec, nsec)
                storage.write(TICK_TOPIC, tick, sec, nsec)
                stats["metadata_written"] += 2
                for topic, message, value, in_range in values:
                    if not in_range:
                        stats["out_of_range"] += 1
                        continue
                    storage.write(topic, value, sec, nsec)
                    stats["written"] += 1
    except KeyboardInterrupt:
        report["stop_reason"] = "interrupt"
        print("  остановлено досрочно (Ctrl+C)")
    except Exception as error:
        report["stop_reason"] = "error"
        report["error_type"] = type(error).__name__
        raise
    finally:
        stats["bad_tail"] = len(reader.buffer)
        report["elapsed_host_seconds"] = time.monotonic() - started
        try:
            if sock is not None:
                sock.close()
        finally:
            try:
                if storage is not None:
                    storage.close()
                    report["record_closed"] = True
            except Exception as error:
                report["stop_reason"] = "error"
                report["close_error_type"] = type(error).__name__
                raise
            finally:
                report_path = out.with_suffix(".rtt.json")
                temporary = report_path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temporary.replace(report_path)
    return stats


def apb1_timer_clock(hclk, ppre):
    """Частота таймера на APB1 по SystemCoreClock (HCLK) и полю PPRE1 из RCC_CFGR.

    Правило STM32: если предделитель APB равен 1, таймер тактируется PCLK; иначе — вдвое от PCLK.
    PPRE1 < 4 означает деление на 1 (бит 2 сброшен).
    """
    div = 1 if ppre < 4 else (1 << ((ppre & 3) + 1))
    return hclk if div == 1 else hclk * 2 // div, div


def load_board(args):
    boards = tomllib.loads((HERE / "boards.toml").read_text(encoding="utf-8"))
    board = args.board.lower()
    if board not in boards:
        raise SystemExit("нет стенда %r в boards.toml (есть: %s)"
                         % (board, ", ".join(k for k in boards if k != "defaults")))
    cfg = dict(boards["defaults"])
    cfg.update(boards[board])
    if "unsupported" not in cfg:
        import build_rtt_image
        cfg = build_rtt_image.select_timer(cfg, boards, getattr(args, "timer", ""))
    return board, cfg


def elf_facts(elf, toolchain):
    """Из ELF: вершина стека (слово 0 таблицы векторов), конец занятой RAM, адрес SystemCoreClock."""
    tc = pathlib.Path(toolchain)
    sections = subprocess.run([str(tc / "arm-none-eabi-objdump"), "-h", str(elf)],
                              capture_output=True, text=True).stdout
    ram_used = 0
    vec_section = None
    for match in re.finditer(r"^\s*\d+\s+(\S+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s+", sections, re.M):
        name, size, vma = match.group(1), int(match.group(2), 16), int(match.group(3), 16)
        if 0x20000000 <= vma < 0x20100000 and size:        # секции в SRAM (.data, .bss)
            ram_used = max(ram_used, vma + size)
        if 0x08000000 <= vma < 0x08100000 and size and vec_section is None:
            vec_section = name
    sp_top = None
    if vec_section:
        binary = subprocess.run([str(tc / "arm-none-eabi-objcopy"), "-O", "binary",
                                 "--only-section=" + vec_section, str(elf), "/dev/stdout"],
                                capture_output=True).stdout
        if len(binary) >= 4:
            sp_top = struct.unpack_from("<I", binary, 0)[0]
    scc = None
    for line in subprocess.run([str(tc / "arm-none-eabi-nm"), str(elf)], capture_output=True, text=True).stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "SystemCoreClock":
            scc = int(parts[0], 16)
    # Начальное значение SystemCoreClock из ELF (инициализатор .data) — надёжная оценка частоты без цели.
    scc_init = None
    if scc is not None:
        gdb = tc / "arm-none-eabi-gdb"
        out = subprocess.run([str(gdb), "-q", "-batch", "-ex", "print/d SystemCoreClock", str(elf)],
                             capture_output=True, text=True).stdout
        found = re.search(r"=\s*(\d+)", out)
        if found:
            scc_init = int(found.group(1))
    return sp_top, ram_used, scc, scc_init


def doctor(args):
    """Проверка окружения rtt-timer: окно SRAM по ELF (офлайн) и частота таймера, свобода TIM2 (по цели)."""
    board, cfg = load_board(args)
    print("доктор rtt-timer (%s):" % cfg.get("mcu", board))
    if "unsupported" in cfg:
        print("  НЕ поддержан: %s" % cfg["unsupported"])
        return 1
    if not args.toolchain:
        print("  нужен --toolchain (каталог bin arm-none-eabi)")
        return 1
    elf = diag.find_elf(args.elf)
    checks = []
    checks.append(("Python 3.9+", sys.version_info >= (3, 9), sys.version.split()[0]))
    checks.append(("ELF прошивки", elf.is_file(), str(elf)))

    suggested_base = suggested_clock = None
    scc = scc_init = None
    if elf.is_file():
        sp_top, ram_used, scc, scc_init = elf_facts(elf, args.toolchain)
        ram_base, ram_end = cfg["ram_base"], cfg["ram_base"] + cfg["ram_kib"] * 1024
        # окно: над занятой RAM и ниже стека (с запасом 2 КиБ под стек)
        base = (ram_used + 0xFF) & ~0xFF if ram_used else ram_base
        ceiling = (sp_top - 2048) if sp_top else ram_end
        needed = ((args.buffer // 4) * 4) + 2048          # буфер + запас под служебные области
        fits = base + needed <= min(ram_end, ceiling)
        suggested_base = base
        checks.append(("Окно SRAM (над данными, ниже стека)", fits,
                       "--sram-base 0x%08X; данные до 0x%08X, стек с 0x%08X, образу ~%d байт"
                       % (base, ram_used, sp_top or ram_end, needed)))
        # Частоту таймера оцениваем по ELF (инициализатор SystemCoreClock) — надёжно, без цели. При тактировании
        # от PLL значение в ELF занижено; реальную частоту уточняет проверка по цели ниже.
        if scc_init:
            suggested_clock = scc_init
            checks.append(("Частота таймера (из ELF)", True,
                           "--timer-clock-hz %d (SystemCoreClock из ELF; при PLL на цели выше — см. ниже)" % scc_init))
        else:
            checks.append(("Частота таймера", False, "нет SystemCoreClock в ELF: задайте --timer-clock-hz вручную"))

    # По цели (best-effort): уточнить частоту и проверить свободу TIM2. Чтение ненадёжно, если ядро стоит на
    # сбросе (.data не скопирована) — поэтому это уточнение, а не основной источник. Ядро не трогаем (без reset).
    details = []
    telnet = process = None
    try:
        if args.openocd or args.use_running:
            process = diag.start_openocd(args, pathlib.Path(args.out or "rtt").with_suffix(".doctor.openocd.log")) \
                if not args.use_running else None
            telnet = diag.Telnet(args.host, args.port)
            if elf.is_file() and scc:
                try:
                    hclk = mdw(telnet, scc)
                    cfgr = mdw(telnet, cfg["rcc_cfgr"])
                    timer_clk, div = apb1_timer_clock(hclk, (cfgr >> cfg["ppre1_shift"]) & 0x7)
                    if 1_000_000 <= timer_clk <= 300_000_000 and hclk != scc:
                        suggested_clock = timer_clk
                        checks.append(("Частота таймера (по цели)", True,
                                       "--timer-clock-hz %d (SystemCoreClock %d, APB1 /%d)" % (timer_clk, hclk, div)))
                    else:
                        details.append("по цели SystemCoreClock прочитался недостоверно (0x%08X): беру значение из ELF"
                                       % hclk)
                except (OSError, RuntimeError) as error:
                    details.append("частоту по цели прочитать не удалось (%s): беру из ELF" % str(error).splitlines()[0][:50])
            apb1 = mdw(telnet, cfg["rcc_enable_reg"])
            cr1 = mdw(telnet, cfg["timer_base"] + cfg["reg_cr1"])
            iser = mdw(telnet, cfg["nvic_iser_base"] + 4 * (cfg["timer_irq"] // 32))
            running = bool(cr1 & cfg["bit_cen"]) or bool(iser & (1 << (cfg["timer_irq"] % 32)))
            checks.append(("%s свободен" % cfg["timer"], not running,
                           "включён прошивкой (CEN/IRQ)" if running else
                           ("тактирование уже включено" if apb1 & cfg["rcc_enable_bit"] else "свободен")))
        else:
            details.append("цель не опрашивалась: нет --openocd и не задан --use-running (офлайн-проверки выше)")
    except (OSError, SystemExit, RuntimeError) as error:
        details.append("цель недоступна (%s): проверки по цели пропущены" % str(error).splitlines()[0][:80])
    finally:
        if telnet:
            telnet.close()
        diag.stop_openocd(process)

    failed = 0
    for name, ok, detail in checks:
        failed += 0 if ok else 1
        print("  %-38s %-4s %s" % (name, "ок" if ok else "НЕТ", str(detail)[:84]))
    for line in details:
        print("  " + line)
    if suggested_base is not None:
        hint = "--timer-clock-hz %d" % suggested_clock if suggested_clock else "--timer-clock-hz <из даташита>"
        print("\n  запись: --sram-base 0x%08X %s" % (suggested_base, hint))
    print("\n  провалено: %d из %d" % (failed, len(checks)))
    return 0 if failed == 0 else 1


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

    validate_topics(layout)  # до запуска OpenOCD и установки перехвата

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

    print("статистика RTT: %s" % out.with_suffix(".rtt.json"))
    print("готово: %s (%d байт)" % (out, out.stat().st_size))
    print("  кадров %d, записано %d, потери (оценка) %d, разрывов seq %d, вне диапазона %d, ресинхр. %d"
          % (stats["frames"], stats["written"], stats["dropped_est"], stats["seq_gaps"],
             stats["out_of_range"], stats["resyncs"]))
    print("  служебных сообщений seq/tick: %d" % stats["metadata_written"])
    if stats["dropped_est"]:
        print("  потери кадров: цель пишет быстрее, чем хост читает кольцо — увеличьте --buffer или снизьте --rate")
    return 0 if stats["written"] else 1
