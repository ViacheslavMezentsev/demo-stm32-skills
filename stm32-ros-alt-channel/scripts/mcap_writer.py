"""Минимальный писатель MCAP (спецификация mcap.dev/spec) на стандартной библиотеке.

ЗАЧЕМ. rosbag — формат ROS1; MCAP — современный контейнер (Foxglove), его читает PlotJuggler под
Windows напрямую, а `mcap` CLI умеет проверять и конвертировать. Писать MCAP проще, чем rosbag: нет
хитрого порядка записей, всё описано в спецификации явно.

СТРУКТУРА ФАЙЛА (см. спецификацию):

    <magic><Header><Data: Schema, Channel, Chunk><Message Index × N><Data End>
    <Summary: Schema × N, Channel × N, Chunk Index, Statistics><Summary Offset × N><Footer><magic>

Почему так, а не «просто сообщения подряд»: спецификация разрешает писать Message вне чанков, но тогда
их нельзя проиндексировать, и читатели, работающие по индексу (PlotJuggler, `mcap info`), могут их не
увидеть. Поэтому пишем **один чанк без сжатия** + Message Index + Chunk Index + Summary: это
канонический, полностью индексированный вид.

ТИПЫ. Схемы пишутся как ROS1: schema encoding `ros1msg` с текстом `.msg`, message encoding `ros1`.
Так файл остаётся «ROS1-совместимым»: те же топики и определения, что у прошивки.

Что не поддерживается сознательно: сжатие (zstd/lz4), несколько чанков, вложения, метаданные.
"""
import struct

import msg_types

MAGIC = b"\x89MCAP0\r\n"

OP_HEADER = 0x01
OP_FOOTER = 0x02
OP_SCHEMA = 0x03
OP_CHANNEL = 0x04
OP_MESSAGE = 0x05
OP_CHUNK = 0x06
OP_MESSAGE_INDEX = 0x07
OP_CHUNK_INDEX = 0x08
OP_STATISTICS = 0x0B
OP_SUMMARY_OFFSET = 0x0E
OP_DATA_END = 0x0F


def _u16(value):
    return struct.pack("<H", value)


def _u32(value):
    return struct.pack("<I", value)


def _u64(value):
    return struct.pack("<Q", value)


def _string(text):
    encoded = text.encode("utf-8")
    return _u32(len(encoded)) + encoded


def _bytes(data):
    return _u32(len(data)) + data


def _map(mapping):
    """Map<k, v> сериализуется как uint32 байтовая длина + пары ключ/значение."""
    body = b"".join(_string(str(key)) + _string(str(value)) for key, value in mapping.items())
    return _u32(len(body)) + body


def _map_u16_u64(mapping):
    body = b"".join(_u16(key) + _u64(value) for key, value in mapping.items())
    return _u32(len(body)) + body


def _record(opcode, payload):
    """Запись MCAP: opcode(1) | длина(uint64) | содержимое."""
    return bytes([opcode]) + _u64(len(payload)) + payload


class McapWriter:
    """Писатель MCAP: один чанк без сжатия, с индексами и summary."""

    def __init__(self, path, profile="ros1", library="firmware mcap_writer",
                 chunk_messages=1000):
        self.path = path
        self.file = open(path, "wb")
        self.file.write(MAGIC)
        self.file.write(_record(OP_HEADER, _string(profile) + _string(library)))
        self.schemas = {}          # тип сообщения -> id схемы
        self.channels = {}         # топик -> {"id", "schema_id", "message"}
        self.messages = []         # (channel_id, sequence, log_time_ns, payload)
        self.sequence = {}
        # Сколько сообщений класть в один чанк: длинные записи делятся
        # на несколько чанков, и читатель может грузить их по частям.
        self.chunk_messages = max(1, int(chunk_messages))
        self.closed = False

    # ---------- регистрация ----------

    def _write_schema(self, message, definition=None):
        # Схема уникальна в пределах типа: имя типа = имя схемы, данные = текст .msg.
        if message in self.schemas:
            return self.schemas[message]
        schema_id = len(self.schemas) + 1
        text = definition or msg_types.definition(message)
        payload = _u16(schema_id) + _string(message) + _string(msg_types.SCHEMA_ENCODING) \
            + _bytes(text.encode("utf-8"))
        self.file.write(_record(OP_SCHEMA, payload))
        self.schemas[message] = schema_id
        return schema_id

    def _write_channel(self, topic, message, md5sum=None, definition=None):
        if topic in self.channels:
            return self.channels[topic]
        schema_id = self._write_schema(message, definition)
        channel_id = len(self.channels) + 1
        md5 = md5sum or msg_types.md5(message)
        payload = _u16(channel_id) + _u16(schema_id) + _string(topic) \
            + _string(msg_types.MESSAGE_ENCODING) + _map({"md5sum": md5})
        self.file.write(_record(OP_CHANNEL, payload))
        self.channels[topic] = {"id": channel_id, "schema_id": schema_id, "message": message,
                                "md5": md5, "definition": definition}
        self.sequence[channel_id] = 0
        return self.channels[topic]

    def add_connection(self, topic, message, kind=None, md5sum=None, definition=None):
        """Зарегистрировать топик.

        `md5sum` и `definition` можно передать явно — для произвольных типов, которых нет в реестре
        `msg_types` (например, собственных сообщений прошивки). Данные тогда пишутся через
        `write_bytes` уже сериализованными.
        """
        if md5sum is None or definition is None:
            if message not in msg_types.TYPES:
                raise ValueError("тип не в реестре, передайте md5sum и definition: %s" % message)
            md5sum = md5sum or msg_types.md5(message)
            definition = definition or msg_types.definition(message)
        self._write_channel(topic, message, md5sum, definition)

    # ---------- сообщения ----------

    def write_bytes(self, topic, payload, seconds, nanoseconds=0):
        """Добавить сообщение с готовым payload (для произвольных типов)."""
        channel = self.channels.get(topic)
        if channel is None:
            raise ValueError("топик не зарегистрирован: %s" % topic)
        channel_id = channel["id"]
        sequence = self.sequence[channel_id]
        self.sequence[channel_id] = sequence + 1
        stamp = int(seconds) * 1_000_000_000 + int(nanoseconds)
        self.messages.append((channel_id, sequence, stamp, bytes(payload)))

    def write(self, topic, value, seconds, nanoseconds=0):
        channel = self.channels.get(topic)
        if channel is None:
            raise ValueError("топик не зарегистрирован: %s" % topic)
        channel_id = channel["id"]
        sequence = self.sequence[channel_id]
        self.sequence[channel_id] = sequence + 1
        stamp = int(seconds) * 1_000_000_000 + int(nanoseconds)
        payload = msg_types.serialize(channel["message"], value)
        self.messages.append((channel_id, sequence, stamp, payload))

    # ---------- закрытие ----------

    def close(self):
        if self.closed:
            return

        # 1. Чанки: внутри только Message-записи (схемы и каналы уже в data-секции).
        # Сообщения режутся на пачки по self.chunk_messages; на каждую — свой чанк и свой индекс.
        chunks = []
        for begin in range(0, len(self.messages), self.chunk_messages):
            batch = self.messages[begin:begin + self.chunk_messages]
            records = bytearray()
            per_channel = {}
            for channel_id, sequence, stamp, payload in batch:
                offset = len(records)
                records += _record(OP_MESSAGE,
                                   _u16(channel_id) + _u32(sequence) + _u64(stamp)
                                   + _u64(stamp) + payload)
                per_channel.setdefault(channel_id, []).append((stamp, offset))
            chunks.append({"batch": batch, "records": records, "per_channel": per_channel,
                           "start": min(m[2] for m in batch),
                           "end": max(m[2] for m in batch)})

        start_time = min((m[2] for m in self.messages), default=0)
        end_time = max((m[2] for m in self.messages), default=0)

        # 1a. Каждый чанк и его Message Index пишутся подряд: читатель находит индекс, перейдя на
        # позицию чанка и пропустив его запись.
        for chunk in chunks:
            chunk["chunk_start"] = self.file.tell()
            payload = (_u64(chunk["start"]) + _u64(chunk["end"]) + _u64(len(chunk["records"]))
                       + _u32(0) + _string("") + _u64(len(chunk["records"])) + bytes(chunk["records"]))
            self.file.write(_record(OP_CHUNK, payload))
            chunk["chunk_length"] = self.file.tell() - chunk["chunk_start"]

            index_offsets = {}
            index_total_start = self.file.tell()
            for channel_id, entries in chunk["per_channel"].items():
                index_offsets[channel_id] = self.file.tell()
                body = b"".join(_u64(stamp) + _u64(offset) for stamp, offset in entries)
                self.file.write(_record(OP_MESSAGE_INDEX,
                                        _u16(channel_id) + _u32(len(body)) + body))
            chunk["index_offsets"] = index_offsets
            chunk["index_length"] = self.file.tell() - index_total_start
            chunk["size"] = len(chunk["records"])

        # 3. Конец данных.
        self.file.write(_record(OP_DATA_END, _u32(0)))

        # 4. Summary: сгруппировано по opcode — схемы, каналы, Chunk Index, Statistics.
        summary_start = self.file.tell()
        groups = {}

        group_start = self.file.tell()
        for message, schema_id in self.schemas.items():
            definition = msg_types.definition(message).encode("utf-8")
            self.file.write(_record(OP_SCHEMA, _u16(schema_id) + _string(message)
                                    + _string(msg_types.SCHEMA_ENCODING) + _bytes(definition)))
        groups[OP_SCHEMA] = group_start

        group_start = self.file.tell()
        for topic, channel in self.channels.items():
            self.file.write(_record(OP_CHANNEL, _u16(channel["id"]) + _u16(channel["schema_id"])
                                    + _string(topic) + _string(msg_types.MESSAGE_ENCODING)
                                    + _map({"md5sum": msg_types.md5(channel["message"])})))
        groups[OP_CHANNEL] = group_start

        group_start = self.file.tell()
        for chunk in chunks:
            chunk_index = (_u64(chunk["start"]) + _u64(chunk["end"]) + _u64(chunk["chunk_start"])
                           + _u64(chunk["chunk_length"]) + _map_u16_u64(chunk["index_offsets"])
                           + _u64(chunk["index_length"]) + _string("")
                           + _u64(chunk["size"]) + _u64(chunk["size"]))
            self.file.write(_record(OP_CHUNK_INDEX, chunk_index))
        groups[OP_CHUNK_INDEX] = group_start

        group_start = self.file.tell()
        counts = {}
        for batch in self.messages:
            counts[batch[0]] = counts.get(batch[0], 0) + 1
        statistics = (_u64(len(self.messages)) + _u16(len(self.schemas)) + _u32(len(self.channels))
                      + _u32(0) + _u32(0) + _u32(len(chunks))
                      + _u64(start_time) + _u64(end_time) + _map_u16_u64(counts))
        self.file.write(_record(OP_STATISTICS, statistics))
        groups[OP_STATISTICS] = group_start

        # 5. Summary Offset: по одной записи на группу, чтобы читатель прыгал сразу к нужному opcode.
        summary_offset_start = self.file.tell()
        for opcode, start in groups.items():
            end = min([s for s in groups.values() if s > start], default=self.file.tell())
            payload = bytes([opcode]) + _u64(start) + _u64(end - start)
            self.file.write(_record(OP_SUMMARY_OFFSET, payload))

        # 6. Footer: адреса секций + CRC (0 — не считаем).
        self.file.write(_record(OP_FOOTER,
                                _u64(summary_start) + _u64(summary_offset_start) + _u32(0)))
        self.file.write(MAGIC)
        self.file.close()
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
