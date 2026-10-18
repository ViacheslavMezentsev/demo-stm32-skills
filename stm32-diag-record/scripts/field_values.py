"""Общее декодирование полей памяти, без зависимости от ROS и транспорта."""
import struct


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
        # Поле короче слова (маска): знак берётся из старшего бита поля, а не слова.
        width = (mask >> shift).bit_length() if mask != 0xFFFFFFFF else 32
        return value - (1 << width) if width and value & (1 << (width - 1)) else value
    return value
