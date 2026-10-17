"""Собрать бинарный образ управляющего блока RTT для инъекции в SRAM.

Зачем бинарник, а не команды GDB: запись байтов строк через `set {char[...]}` в GDB ненадёжна (попытка
присвоить строку удобной переменной `$text` даёт не то, что ожидается, и цикл пишет мусор). Готовый
файл и `restore binary` записывают ровно те байты, которые мы посчитали, и это проверяемо на стороне
хоста.

Структура (все поля little-endian, указатели — 4 байта, как на Cortex-M):

    SEGGER_RTT_CB (48 байт при 1 up-буфере и 0 down-буферах)
      +0  char acID[16]              "SEGGER RTT" + нули
      +16 int  MaxNumUpBuffers       1
      +20 int  MaxNumDownBuffers     0
      +24 aUp[0].sName               0 (имя не обязательно)
      +28 aUp[0].pBuffer             адрес кольцевого буфера
      +32 aUp[0].SizeOfBuffer        размер буфера
      +36 aUp[0].WrOff               сколько записано (хост увидит как непрочитанное)
      +40 aUp[0].RdOff               0
      +44 aUp[0].Flags               0 (NO_BLOCK_SKIP)

Затем идёт сам кольцевой буфер с полезной нагрузкой.

Использование:
    python build_block.py --out build/rtt-block.bin --address 0x2001E000
"""
import argparse
import pathlib
import struct

# Смещения в управляющем блоке.
ID_OFFSET = 0
UP_COUNT_OFFSET = 16
DOWN_COUNT_OFFSET = 20
UP0_OFFSET = 24
BLOCK_SIZE = 48


def build(address, buffer_address, buffer_size, payload, name=b"Terminal"):
    """Собрать образ блока и буфера; вернуть (байты, block, buffer).

    Между концом блока и началом буфера может быть зазор (буфер удобно выравнивать) — образ должен
    покрывать его целиком, иначе `restore binary` запишет буфер не туда, куда указывает `pBuffer`.
    """
    if len(payload) >= buffer_size:
        raise ValueError("полезная нагрузка не помещается в буфер")
    gap = buffer_address - (address + BLOCK_SIZE)
    if gap < 0:
        raise ValueError("буфер пересекается с блоком: адрес буфера слишком низкий")
    block = bytearray(BLOCK_SIZE + gap)
    # acID: ровно 16 байт
    ident = b"SEGGER RTT"
    block[ID_OFFSET:ID_OFFSET + 16] = ident + b"\0" * (16 - len(ident))
    struct.pack_into("<i", block, UP_COUNT_OFFSET, 1)
    struct.pack_into("<i", block, DOWN_COUNT_OFFSET, 0)
    # aUp[0]
    struct.pack_into("<I", block, UP0_OFFSET + 0, 0)              # sName = NULL
    struct.pack_into("<I", block, UP0_OFFSET + 4, buffer_address)  # pBuffer
    struct.pack_into("<I", block, UP0_OFFSET + 8, buffer_size)     # SizeOfBuffer
    struct.pack_into("<I", block, UP0_OFFSET + 12, len(payload))   # WrOff
    struct.pack_into("<I", block, UP0_OFFSET + 16, 0)              # RdOff
    struct.pack_into("<I", block, UP0_OFFSET + 20, 0)              # Flags
    # Сам буфер: полезная нагрузка и нули до размера буфера.
    buffer = bytearray(buffer_size)
    buffer[:len(payload)] = payload
    return bytes(block) + bytes(buffer), block, buffer


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address", default="0x2001E000", help="адрес блока в SRAM")
    parser.add_argument("--buffer", default="0x2001E100", help="адрес кольцевого буфера")
    parser.add_argument("--size", type=int, default=1024, help="размер кольцевого буфера")
    parser.add_argument("--out", default="build/rtt-block.bin")
    parser.add_argument("--payload", default="RTT injected into SRAM: no firmware change\n")
    parser.add_argument("--payload-file", help="взять полезную нагрузку из файла")
    args = parser.parse_args()

    address = int(args.address, 0)
    buffer_address = int(args.buffer, 0)
    payload = (pathlib.Path(args.payload_file).read_bytes() if args.payload_file
               else args.payload.encode("utf-8"))

    image, block, buffer = build(address, buffer_address, args.size, payload)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(image)

    print("адрес блока:      0x%08X" % address)
    print("адрес буфера:     0x%08X" % buffer_address)
    print("размер буфера:    %d байт" % args.size)
    print("полезная нагрузка: %d байт: %r" % (len(payload), payload))
    print("WrOff:            %d" % len(payload))
    print("весь образ:       %d байт (блок %d + зазор %d + буфер %d)"
          % (len(image), BLOCK_SIZE, buffer_address - (address + BLOCK_SIZE), args.size))
    print("образ:            %s (%d байт)" % (out, len(image)))
    # Проверка, что ID лег именно туда, где его ищет OpenOCD.
    assert block[:10] == b"SEGGER RTT", "ID блока записан неверно"
    print("проверка ID:      ок — первые 10 байт 'SEGGER RTT'")
    # Адреса-указатели должны попадать в SRAM.
    for value, role in ((struct.unpack_from("<I", block, UP0_OFFSET + 4)[0], "pBuffer"),):
        if not 0x20000000 <= value < 0x20020000:
            raise ValueError("%s вне SRAM: 0x%08X" % (role, value))
    print("проверка SRAM:    ок — указатели внутри 0x20000000..0x20020000")


if __name__ == "__main__":
    main()
