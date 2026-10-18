#!/usr/bin/env python3
"""Сборка образа SRAM для режима --backend rtt-timer навыка stm32-diag-record.

Готовит ОДИН бинарный образ для свободного окна SRAM, который заливается в цель и даёт аддитивную
диагностику без правки флеша:

  * управляющий блок RTT (ID "DIAG RTT") и кольцевой буфер;
  * копия таблицы векторов прошивки с подменённым вектором свободного таймера (на заглушку), под VTOR;
  * код заглушки timer_hook.S (одинаков для Cortex-M3/M4) и её блок параметров;
  * таблица адресов полей (N, затем N выровненных адресов) и счётчики (кадры, потери, окно тиков).

Образ непрерывный: заливается одной командой, а регистры таймера/NVIC/VTOR настраиваются отдельно (это
делает rtt_timer.py). Заглушка и данные различаются только таблицей полей и адресами — код один на все платы.

Адреса полей и метки времени берутся из addresses.json (его готовит resolve_addresses.py по ELF и SVD).
Длина таблицы векторов и её исходные байты берутся из ELF прошивки (секция векторов).
"""
import argparse
import json
import pathlib
import re
import struct
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10
    import tomli as tomllib

HERE = pathlib.Path(__file__).resolve().parent
RTT_ID = b"DIAG RTT"               # отличается от "SEGGER RTT" прошивки, чтобы OpenOCD нашёл именно наш блок
CB_SIZE = 48                       # acID[16] + MaxUp/MaxDown + один up-буфер (sName,pBuffer,Size,WrOff,RdOff,Flags)
HEADER_LEN = 8                     # кадр: u16 magic, u16 seq, u32 tick
VECTOR_NAMES = (".isr_vector", ".vectors", ".vector_table", ".intvec", ".vectors_flash")


def next_pow2(value):
    power = 1
    while power < value:
        power <<= 1
    return power


def align_up(value, alignment):
    return (value + alignment - 1) & ~(alignment - 1)


def assemble_stub(core, workdir):
    """Собрать timer_hook.S во флэт-бинарник; вернуть (байты, смещение блока params)."""
    obj, elf, binary = workdir / "stub.o", workdir / "stub.elf", workdir / "stub.bin"
    gcc_bin = pathlib.Path(sys.argv[0]).parent  # не используется; инструменты берутся из --toolchain
    as_tool = TOOLCHAIN / "arm-none-eabi-as"
    ld_tool = TOOLCHAIN / "arm-none-eabi-ld"
    objcopy = TOOLCHAIN / "arm-none-eabi-objcopy"
    nm_tool = TOOLCHAIN / "arm-none-eabi-nm"
    cpu = "cortex-m3" if core in ("cortex-m3", "cortex-m0", "cortex-m0plus") else "cortex-m4"
    subprocess.run([str(as_tool), "-mcpu=" + cpu, "-mthumb", str(HERE / "timer_hook.S"), "-o", str(obj)], check=True)
    subprocess.run([str(ld_tool), "-Ttext=0", "-e", "timer_hook", str(obj), "-o", str(elf)], check=True)
    subprocess.run([str(objcopy), "-O", "binary", "--only-section=.text", str(elf), str(binary)], check=True)
    symbols = subprocess.run([str(nm_tool), str(elf)], capture_output=True, text=True, check=True).stdout
    params_offset = None
    for line in symbols.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "params":
            params_offset = int(parts[0], 16)
    if params_offset is None:
        raise SystemExit("в собранной заглушке нет символа params")
    return binary.read_bytes(), params_offset


def read_vectors(elf):
    """Адрес и байты таблицы векторов прошивки из ELF (для копии под VTOR)."""
    objdump = subprocess.run([str(TOOLCHAIN / "arm-none-eabi-objdump"), "-h", str(elf)],
                             capture_output=True, text=True, check=True).stdout
    sections = {}
    order = []
    for match in re.finditer(r"^\s*\d+\s+(\S+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s+", objdump, re.M):
        name, size, vma = match.group(1), int(match.group(2), 16), int(match.group(3), 16)
        sections[name] = (vma, size)
        order.append(name)
    chosen = next((name for name in VECTOR_NAMES if name in sections and sections[name][1]), None)
    if chosen is None:
        flash = [(vma, name) for name, (vma, size) in sections.items() if size and 0x08000000 <= vma < 0x08100000]
        if not flash:
            raise SystemExit("не нашёл секцию таблицы векторов в ELF (искал %s и флеш-секции)" % ", ".join(VECTOR_NAMES))
        chosen = min(flash)[1]
    vma, size = sections[chosen]
    workbin = elf.parent / "vectors.bin"
    subprocess.run([str(TOOLCHAIN / "arm-none-eabi-objcopy"), "-O", "binary",
                    "--only-section=" + chosen, str(elf), str(workbin)], check=True)
    data = workbin.read_bytes()
    if len(data) < size:
        data = data + b"\x00" * (size - len(data))
    return chosen, vma, data[:size]


def main(argv=None):
    global TOOLCHAIN
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--elf", required=True, help="ELF прошивки (для таблицы векторов)")
    parser.add_argument("--addresses", required=True, help="addresses.json от resolve_addresses.py")
    parser.add_argument("--board", required=True, help="стенд из boards.toml (f411ce, f103c8, ...)")
    parser.add_argument("--boards", default=str(HERE / "boards.toml"), help="файл параметров стендов")
    parser.add_argument("--toolchain", required=True, help="каталог bin тулчейна arm-none-eabi")
    parser.add_argument("--sram-base", required=True, help="начало свободного окна SRAM (hex), напр. 0x2001E000")
    parser.add_argument("--buffer", type=int, default=4096, help="размер кольцевого буфера, байт (кратно кадру)")
    parser.add_argument("--tick-address", help="адрес метки времени (hex); иначе берётся поле tick из addresses.json")
    parser.add_argument("--timer-clock-hz", type=int, help="тактовая частота таймера (иначе из boards.toml)")
    parser.add_argument("--rate", type=float, default=1000.0, help="желаемая частота выборки, Гц (приблизительно)")
    parser.add_argument("--out", required=True, help="куда писать образ (.bin); layout рядом (.layout.json)")
    args = parser.parse_args(argv)

    TOOLCHAIN = pathlib.Path(args.toolchain)
    boards = tomllib.loads(pathlib.Path(args.boards).read_text(encoding="utf-8"))
    board = args.board.lower()
    if board not in boards:
        raise SystemExit("нет стенда %r в %s (есть: %s)" % (board, args.boards, ", ".join(k for k in boards if k != "defaults")))
    cfg = dict(boards["defaults"])
    cfg.update(boards[board])
    if "unsupported" in cfg:
        raise SystemExit("стенд %s не поддержан: %s" % (board, cfg["unsupported"]))

    data = json.loads(pathlib.Path(args.addresses).read_text(encoding="utf-8"))
    fields = [f for f in data["fields"] if f.get("role") != "tick"]
    tick_fields = [f for f in data["fields"] if f.get("role") == "tick"]
    if args.tick_address:
        tick_addr = int(args.tick_address, 16)
    elif tick_fields:
        tick_addr = tick_fields[0]["address"]
    else:
        raise SystemExit("нет адреса метки времени: задайте --tick-address или поле role=\"tick\" в спецификации")
    if not fields:
        raise SystemExit("нет полей для записи")
    n = len(fields)
    frame_len = HEADER_LEN + 4 * n

    buffer_size = max(args.buffer // frame_len, 2) * frame_len  # кратно кадру, минимум два кадра
    stub_blob, params_offset = assemble_stub(cfg["core"], pathlib.Path(args.out).resolve().parent)
    vec_name, vec_vma, vec_bytes = read_vectors(pathlib.Path(args.elf))

    base = int(args.sram_base, 16)
    cursor = base

    def place(size, alignment):
        nonlocal cursor
        cursor = align_up(cursor, alignment)
        addr = cursor
        cursor += size
        return addr

    cb_addr = place(CB_SIZE, 16)
    buf_addr = place(buffer_size, 16)
    counters_addr = place(16, 4)                       # counter, dropped, first_tick, last_tick
    fieldtab_addr = place(4 + 4 * n, 4)
    stub_addr = place(len(stub_blob), 4)
    params_addr = stub_addr + params_offset
    vec_align = next_pow2(max(len(vec_bytes), 512))
    vec_addr = place(len(vec_bytes), vec_align)
    end = cursor
    total = end - base

    # Регионы в едином образе (нули по умолчанию).
    image = bytearray(total)

    def put(addr, blob):
        off = addr - base
        image[off:off + len(blob)] = blob

    # Управляющий блок RTT: один up-буфер, down нет.
    cb = bytearray(CB_SIZE)
    cb[0:len(RTT_ID)] = RTT_ID
    struct.pack_into("<II", cb, 16, 1, 0)              # MaxNumUpBuffers=1, MaxNumDownBuffers=0
    struct.pack_into("<IIIIII", cb, 24, 0, buf_addr, buffer_size, 0, 0, 0)  # sName,pBuffer,Size,WrOff,RdOff,Flags
    put(cb_addr, cb)

    # Таблица полей: N, затем выровненные адреса (слово читается целиком; сдвиг/маску применяет хост).
    table = struct.pack("<I", n) + b"".join(struct.pack("<I", f["address"] & ~3) for f in fields)
    put(fieldtab_addr, table)

    # Заглушка + её параметры.
    params = struct.pack("<8I", cfg["timer_base"] + cfg["reg_sr"], cb_addr, buf_addr, buffer_size,
                         counters_addr, fieldtab_addr, frame_len, tick_addr)
    blob = bytearray(stub_blob)
    blob[params_offset:params_offset + 32] = params
    put(stub_addr, bytes(blob))

    # Копия таблицы векторов с подменённым вектором таймера.
    exception = 16 + cfg["timer_irq"]
    vec = bytearray(vec_bytes)
    slot = 4 * exception
    if slot + 4 > len(vec):
        raise SystemExit("таблица векторов (%d байт) короче вектора исключения %d" % (len(vec), exception))
    struct.pack_into("<I", vec, slot, stub_addr | 1)   # бит Thumb
    put(vec_addr, bytes(vec))

    pathlib.Path(args.out).write_bytes(image)

    # Частота: PSC/ARR под желаемый rate (примерно; точное время — из метки tick).
    clock = args.timer_clock_hz or cfg["tim_clock_hz"]
    ticks = max(int(round(clock / args.rate)), 2)
    psc = 0
    arr = ticks - 1
    while arr > 0xFFFF:
        psc += 1
        arr = max(int(round(clock / ((psc + 1) * args.rate))) - 1, 1)
    actual_hz = clock / ((psc + 1) * (arr + 1))

    layout = {
        "schema": 1,
        "board": board, "core": cfg["core"], "mcu": cfg.get("mcu"),
        "sram_base": base, "image_size": total, "end": end,
        "rtt": {"id": RTT_ID.decode(), "control_block": cb_addr, "buffer": buf_addr, "size": buffer_size},
        "counters": counters_addr, "field_table": fieldtab_addr,
        "stub": stub_addr, "params": params_addr, "vectors": {"address": vec_addr, "source": vec_name,
                                                              "length": len(vec_bytes), "firmware_vma": vec_vma},
        "frame": {"len": frame_len, "header": HEADER_LEN, "magic": 0xD1A6, "count": n},
        "tick_address": tick_addr,
        "timer": {"name": cfg["timer"], "base": cfg["timer_base"], "irq": cfg["timer_irq"], "exception": exception,
                  "sr": cfg["timer_base"] + cfg["reg_sr"], "cr1": cfg["timer_base"] + cfg["reg_cr1"],
                  "dier": cfg["timer_base"] + cfg["reg_dier"], "egr": cfg["timer_base"] + cfg["reg_egr"],
                  "psc_reg": cfg["timer_base"] + cfg["reg_psc"], "arr_reg": cfg["timer_base"] + cfg["reg_arr"],
                  "psc": psc, "arr": arr, "bit_cen": cfg["bit_cen"], "bit_uie": cfg["bit_uie"], "bit_ug": cfg["bit_ug"],
                  "clock_hz": clock, "actual_hz": round(actual_hz, 3)},
        "rcc": {"enable_reg": cfg["rcc_enable_reg"], "enable_bit": cfg["rcc_enable_bit"]},
        "nvic": {"iser": cfg["nvic_iser_base"] + 4 * (cfg["timer_irq"] // 32), "bit": cfg["timer_irq"] % 32,
                 "icer": (cfg["nvic_iser_base"] + 0x80) + 4 * (cfg["timer_irq"] // 32),
                 "ispr": (cfg["nvic_iser_base"] + 0x100) + 4 * (cfg["timer_irq"] // 32),
                 "icpr": (cfg["nvic_iser_base"] + 0x180) + 4 * (cfg["timer_irq"] // 32)},
        "vtor": cfg["vtor"],
        "fields": [{"topic": f["topic"], "message": f["message"], "kind": f.get("kind", "uint32"),
                    "byte_offset": f["address"] & 3, "shift": f.get("shift", 0), "mask": f.get("mask", 0xFFFFFFFF),
                    "min": f.get("min"), "max": f.get("max")} for f in fields],
    }
    layout_path = pathlib.Path(args.out).with_suffix(".layout.json")
    layout_path.write_text(json.dumps(layout, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("образ %d байт [0x%08X..0x%08X] -> %s" % (total, base, end, args.out))
    print("  поля %d, кадр %d байт, буфер %d байт (%d кадров)" % (n, frame_len, buffer_size, buffer_size // frame_len))
    print("  таймер %s: PSC=%d ARR=%d -> ~%.1f Гц (точное время по метке tick)" % (cfg["timer"], psc, arr, actual_hz))
    print("  layout -> %s" % layout_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
