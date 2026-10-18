#!/usr/bin/env python3
"""Разбор потока кадров RTT-заглушки (--backend rtt-timer навыка stm32-diag-record).

Кадр: u16 magic=0xD1A6, u16 seq, u32 tick, затем N слов по адресам полей (см. build_rtt_image.py).
Поток читается кусками (TCP от `rtt server` OpenOCD), границы кусков произвольны. Разбор:

  * синхронизация по magic; кадр фиксированной длины;
  * контроль seq: разрыв нумерации — это потери (цель пропустила запись в заполненное кольцо или потерян кадр в потоке);
  * значение поля: слово по выровненному адресу, затем сдвиг на byte_offset и преобразование по kind
    (та же логика, что у основного режима: convert из record_diag).

Класс не зависит от аппаратуры и проверяется в CI на синтетическом потоке.
"""
import pathlib
import struct
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from record_diag import convert  # noqa: E402

MAGIC = 0xD1A6
MAGIC_LE = struct.pack("<H", MAGIC)


class FrameReader(object):
    """Поиск кадров в потоке байтов и преобразование в значения полей."""

    def __init__(self, layout):
        frame = layout["frame"]
        self.frame_len = frame["len"]
        self.count = frame["count"]
        self.fields = layout["fields"]
        if self.frame_len != frame["header"] + 4 * self.count:
            raise ValueError("длина кадра не совпадает с числом полей")
        self.buffer = b""
        self.last_seq = None
        self.stats = {"frames": 0, "resyncs": 0, "seq_gaps": 0, "dropped_est": 0, "bad_tail": 0,
                      "first_seq": None, "last_seq": None, "first_tick": None, "last_tick": None,
                      "tick_repeats": 0, "tick_wraps": 0, "tick_backwards": 0,
                      "max_tick_step_ms": 0}

    def _parse_frame(self, chunk):
        magic, seq, tick = struct.unpack_from("<HHI", chunk, 0)
        words = struct.unpack_from("<%dI" % self.count, chunk, 8)
        values = []
        for field, word in zip(self.fields, words):
            raw = (word >> (8 * field.get("byte_offset", 0))) & 0xFFFFFFFF
            value = convert(raw, field.get("kind", "uint32"), field.get("shift", 0), field.get("mask", 0xFFFFFFFF))
            lo, hi = field.get("min"), field.get("max")
            in_range = (lo is None or value >= lo) and (hi is None or value <= hi)
            values.append((field["topic"], field["message"], value, in_range))
        return seq, tick, values

    def feed(self, data):
        """Добавить кусок байтов; вернуть список кадров (seq, tick, [(topic, message, value, in_range)])."""
        self.buffer += data
        frames = []
        while True:
            start = self.buffer.find(MAGIC_LE)
            if start < 0:
                self.buffer = self.buffer[-1:]  # хвост может быть началом magic
                break
            if start > 0:
                self.stats["resyncs"] += 1
                self.buffer = self.buffer[start:]
            if len(self.buffer) < self.frame_len:
                break
            seq, tick, values = self._parse_frame(self.buffer[:self.frame_len])
            self.buffer = self.buffer[self.frame_len:]
            if self.last_seq is not None:
                gap = (seq - self.last_seq - 1) & 0xFFFF
                if gap:
                    self.stats["seq_gaps"] += 1
                    self.stats["dropped_est"] += gap
            previous_tick = self.stats["last_tick"]
            if previous_tick is None:
                self.stats["first_seq"], self.stats["first_tick"] = seq, tick
            else:
                step = (tick - previous_tick) & 0xFFFFFFFF
                if step == 0:
                    self.stats["tick_repeats"] += 1
                elif step >= 0x80000000:
                    self.stats["tick_backwards"] += 1
                else:
                    self.stats["tick_wraps"] += int(tick < previous_tick)
                    self.stats["max_tick_step_ms"] = max(self.stats["max_tick_step_ms"], step)
            self.stats["last_seq"], self.stats["last_tick"] = seq, tick
            self.last_seq = seq
            self.stats["frames"] += 1
            frames.append((seq, tick, values))
        return frames


def build_frame(seq, tick, words):
    """Собрать байты кадра как это делает заглушка (для тестов)."""
    return struct.pack("<HHI", MAGIC, seq & 0xFFFF, tick & 0xFFFFFFFF) + b"".join(
        struct.pack("<I", w & 0xFFFFFFFF) for w in words)
