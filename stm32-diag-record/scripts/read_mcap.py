#!/usr/bin/env python3
"""Чтение MCAP без пакета mcap: каналы, сообщения std_msgs и сводка — для файлов этого навыка.

Навык stm32-diag-record. Понимает записи Schema, Channel, Message и несжатые Chunk (так пишет
mcap_writer.py). Сжатые чанки (zstd, lz4) — через пакет mcap или CLI `mcap cat`.

  python read_mcap.py record.mcap                  # сводка: тема, тип, сообщений, первое и последнее значение
  python read_mcap.py record.mcap --topic /x/tick  # сообщения темы: время, значение
  python read_mcap.py record.mcap --csv out.csv    # все сообщения: topic,log_time_ns,value
"""
import argparse
import csv
import struct
import sys

MAGIC = b"\x89MCAP0\r\n"
OP_SCHEMA, OP_CHANNEL, OP_MESSAGE, OP_CHUNK = 0x03, 0x04, 0x05, 0x06
STRUCTS = {"Bool": "?", "Int8": "b", "Int16": "h", "Int32": "i", "Int64": "q", "UInt8": "B", "UInt16": "H",
           "UInt32": "I", "UInt64": "Q", "Float32": "f", "Float64": "d"}


class Reader(object):
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, size):
        chunk = self.data[self.pos:self.pos + size]
        self.pos += size
        return chunk

    def u16(self):
        return struct.unpack("<H", self.take(2))[0]

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.take(8))[0]

    def string(self):
        return self.take(self.u32()).decode("utf-8", "replace")


def records(data):
    """(opcode, тело) подряд; несжатые чанки раскрываются."""
    reader = Reader(data)
    while reader.pos + 9 <= len(data):
        opcode = data[reader.pos]
        reader.pos += 1
        body = reader.take(reader.u64())
        if opcode == OP_CHUNK:
            inner = Reader(body)
            inner.u64(), inner.u64(), inner.u64(), inner.u32()
            compression = inner.string()
            if compression:
                raise SystemExit("сжатый чанк (%s): нужен пакет mcap или CLI `mcap cat`" % compression)
            for item in records(inner.take(inner.u64())):
                yield item
        else:
            yield opcode, body


def load(path):
    with open(path, "rb") as stream:
        data = stream.read()
    if not data.startswith(MAGIC):
        raise SystemExit("не MCAP: %s" % path)
    schemas, channels, messages = {}, {}, []
    for opcode, body in records(data[len(MAGIC):]):
        reader = Reader(body)
        if opcode == OP_SCHEMA:
            schema_id, name = reader.u16(), reader.string()
            schemas[schema_id] = name
        elif opcode == OP_CHANNEL:
            channel_id, schema_id, topic = reader.u16(), reader.u16(), reader.string()
            channels[channel_id] = (topic, schema_id)
        elif opcode == OP_MESSAGE:
            channel_id, _, log_time, _ = reader.u16(), reader.u32(), reader.u64(), reader.u64()
            messages.append((channel_id, log_time, body[reader.pos:]))
    return schemas, channels, messages


def value(type_name, payload):
    short = type_name.rsplit("/", 1)[-1]
    if short == "String":
        return payload[4:4 + struct.unpack_from("<I", payload)[0]].decode("utf-8", "replace")
    if short in STRUCTS:
        return struct.unpack_from("<" + STRUCTS[short], payload)[0]
    return payload.hex()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mcap")
    parser.add_argument("--topic")
    parser.add_argument("--csv")
    args = parser.parse_args()
    schemas, channels, messages = load(args.mcap)
    rows = [(channels[c][0], schemas.get(channels[c][1], "?"), t, value(schemas.get(channels[c][1], "?"), p))
            for c, t, p in messages if c in channels]
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["topic", "log_time_ns", "value"])
            writer.writerows((topic, stamp, data) for topic, _, stamp, data in rows)
        print("записано %d сообщений: %s" % (len(rows), args.csv))
    elif args.topic:
        for topic, _, stamp, data in rows:
            if topic == args.topic:
                print("%.6f %s" % (stamp / 1e9, data))
    else:
        topics = {}
        for topic, type_name, stamp, data in rows:
            topics.setdefault((topic, type_name), []).append((stamp, data))
        for (topic, type_name), items in sorted(topics.items()):
            span = (items[-1][0] - items[0][0]) / 1e9
            print("%-28s %-18s %6d сообщ., %.3f с, первое %s, последнее %s"
                  % (topic, type_name, len(items), span, items[0][1], items[-1][1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
