"""Собрать образ перехвата SysTick для PoC RTT: таблица векторов в SRAM + заглушка.

Идея — как инъекция в чужой процесс на ПК: **прошивка в флеше не меняется**, но её поведение меняется,
потому что в цепочку вставляется посредник. На Cortex-M для этого есть штатный механизм: регистр
`SCB->VTOR` указывает, откуда брать таблицу векторов. Копируем таблицу в SRAM, подменяем в копии
`SysTick`, а прежний обработчик вызываем из заглушки — штатное поведение сохраняется.

Заглушка на каждом тике (1 кГц) кладёт в кольцевой RTT-буфер 4 байта: маркер 'T' и номер тика. Значит
данные в канале RTT появляются **сами**, без участия отладчика — это и есть поставщик, которого не
хватало пассивной инъекции блока.

Раскладка образа (всё в свободной SRAM, выше .bss и ниже стека):

    0x2001E000  управляющий блок RTT (48 байт)
    0x2001E100  кольцевой буфер RTT (1024 байта)
    0x2001F000  таблица векторов (копия .isr_vector, 103 записи = 412 байт)
    0x2001F200  заглушка SysTick (код из hook.bin, ~76 байт)

VTOR требует выравнивания таблицы на 128 байт (минимум 32 слова) — 0x2001F000 подходит.

Использование:
    python build_hook_image.py --elf build/firmware.elf --stub build/hook.bin \
        --out build/hook-image.bin
"""
import argparse
import pathlib
import struct
import subprocess

# Раскладка образа в SRAM.
RTT_CB = 0x2001E000
RTT_BUFFER = 0x2001E100
RTT_SIZE = 1024
RTT_WROFF = 36
VECTORS = 0x2001F000
STUB = 0x2001F200
IMAGE_ADDRESS = RTT_CB
IMAGE_END = STUB + 0x200          # с запасом: заглушка 76 байт, но резервируем больше

# Вектор SysTick — исключение номер 15, то есть смещение 15 * 4 = 60.
SYSTICK_VECTOR_OFFSET = 60


def nm(elf, symbol):
    """Адрес символа из ELF."""
    output = subprocess.run(["arm-none-eabi-nm", str(elf)], capture_output=True, text=True)
    for line in output.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == symbol:
            return int(parts[0], 16)
    raise ValueError("символ не найден: %s" % symbol)


def vector_table(elf):
    """Прочитать .isr_vector из ELF: список адресов обработчиков.

    Через временный файл, а не stdout: `objcopy -O binary ... -` не отдаёт данные в поток, а пишет
    файл с именем «-» (проверено — на выходе пусто).
    """
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as handle:
        temporary = pathlib.Path(handle.name)
    try:
        output = subprocess.run(["arm-none-eabi-objcopy", "-O", "binary",
                                 "--only-section=.isr_vector", str(elf), str(temporary)],
                                capture_output=True)
        if output.returncode != 0 or not temporary.is_file():
            raise ValueError("не удалось прочитать .isr_vector: %s"
                             % output.stderr.decode("utf-8", "replace")[:200])
        data = temporary.read_bytes()
    finally:
        temporary.unlink(missing_ok=True)
    words = [struct.unpack_from("<I", data, offset)[0] for offset in range(0, len(data), 4)]
    return words


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--elf", default="build/firmware.elf")
    parser.add_argument("--stub", default="build/hook.bin")
    parser.add_argument("--out", default="build/hook-image.bin")
    parser.add_argument("--payload", default="RTT via SysTick hook: no firmware change\n")
    args = parser.parse_args()

    stub = pathlib.Path(args.stub).read_bytes()
    if len(stub) > 0x200:
        raise ValueError("заглушка больше отведённого места")

    # Таблица векторов из прошивки — берём как есть, чтобы все остальные обработчики остались штатными.
    words = vector_table(args.elf)
    original_systick = words[SYSTICK_VECTOR_OFFSET // 4]
    print("исходный SysTick_Handler: 0x%08X (из таблицы .isr_vector)" % original_systick)

    # Подменяем только SysTick на заглушку в SRAM. Остальные векторы не трогаем.
    vectors = list(words)
    vectors[SYSTICK_VECTOR_OFFSET // 4] = STUB | 1     # бит Thumb обязателен
    print("подменённый вектор SysTick: 0x%08X (заглушка в SRAM)" % vectors[SYSTICK_VECTOR_OFFSET // 4])

    # Образ: всё от RTT_CB до IMAGE_END заполняется нулями, потом накладываются части.
    size = IMAGE_END - IMAGE_ADDRESS
    image = bytearray(size)

    def place(address, data):
        offset = address - IMAGE_ADDRESS
        image[offset:offset + len(data)] = data

    # 1. Управляющий блок RTT с приветствием в буфере: полезно видеть, что канал уже готов.
    payload = args.payload.encode("utf-8")
    block = bytearray(48)
    ident = b"SEGGER RTT"
    block[0:16] = ident + b"\0" * (16 - len(ident))
    struct.pack_into("<i", block, 16, 1)          # MaxNumUpBuffers
    struct.pack_into("<i", block, 20, 0)          # MaxNumDownBuffers
    struct.pack_into("<I", block, 24 + 0, 0)      # sName
    struct.pack_into("<I", block, 24 + 4, RTT_BUFFER)
    struct.pack_into("<I", block, 24 + 8, RTT_SIZE)
    struct.pack_into("<I", block, 24 + 12, 0)     # WrOff — заглушка начнёт писать с нуля
    struct.pack_into("<I", block, 24 + 16, 0)     # RdOff
    struct.pack_into("<I", block, 24 + 20, 0)     # Flags
    place(RTT_CB, bytes(block))
    # 2. Заглушка.
    place(STUB, stub)
    # 3. Таблица векторов (копия с подменённым SysTick).
    table = b"".join(struct.pack("<I", word) for word in vectors)
    place(VECTORS, table)

    pathlib.Path(args.out).write_bytes(bytes(image))
    print("образ: %s (%d байт)" % (args.out, len(image)))
    print("раскладка:")
    print("  управляющий блок RTT: 0x%08X" % RTT_CB)
    print("  кольцевой буфер:      0x%08X (%d байт)" % (RTT_BUFFER, RTT_SIZE))
    print("  таблица векторов:     0x%08X (%d записей, %d байт)"
          % (VECTORS, len(vectors), len(table)))
    print("  заглушка SysTick:     0x%08X (%d байт)" % (STUB, len(stub)))
    print("  VTOR выставить в:     0x%08X" % VECTORS)
    # Проверки, без которых образ бессмысленен.
    if len(stub) == 0:
        raise ValueError("пустая заглушка")
    if image[:10] != b"SEGGER RTT":
        raise ValueError("ID блока RTT записан неверно")
    if not (0x20000000 <= STUB < 0x20020000):
        raise ValueError("заглушка вне SRAM")


if __name__ == "__main__":
    main()
