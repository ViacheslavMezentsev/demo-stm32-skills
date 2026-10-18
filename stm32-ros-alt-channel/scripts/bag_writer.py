"""Минимальный писатель ROS1 bag (формат v2.0) на стандартной библиотеке.

ЗАЧЕМ. ROS1 под Windows не поддерживается (официально: «Microsoft has stopped supporting this method»),
а MSYS2-сборка Noetic — тяжёлый source-build. Но **bag — это просто файл** с документированным форматом,
и его можно писать без ROS вообще. Так запись получается нативным решением под Windows с нулевыми
зависимостями: только Python и этот модуль.

ФОРМАТ (v2.0). Файл начинается с magic-строки, дальше идут записи:

    запись = header_len(u32) | header | data_len(u32) | data
    header = набор полей: field_len(u32) | "имя=значение"

Обязательное поле `op` задаёт тип записи:

    op=0x03  bag header   index_pos(u64), conn_count(u32), chunk_count(u32)
    op=0x05  chunk        compression("none"), size(u32 — длина несжатых данных)
    op=0x07  connection   conn(u32), topic;  data — описание типа (topic/type/md5sum/message_definition)
    op=0x02  message      conn(u32), time(u64);  data — сериализованное сообщение
    op=0x04  index data   ver(u32), conn(u32), count(u32);  data — (time, offset) на каждое сообщение
    op=0x06  chunk info   ver(u32), chunk_pos(u64), start_time, end_time, count(u32); data — (conn, count)

Порядок в файле (проверено по исходнику rosbag, `Bag._Reader.start_reading`):

    magic
    bag header            index_pos = смещение ПЕРВОЙ connection-записи в конце файла
    chunk                 данные: connection-записи + message-записи
    index data (op=4)     СРАЗУ за чанком — читатель ищет их от позиции чанка
    connection (op=7)     ровно conn_count штук, начиная с index_pos
    chunk info (op=6)     ровно chunk_count штук, после connection-записей

Первая попытка ставила index data туда, где читатель ждёт connection, и rosbag объявлял bag
неиндексированным (`Expected op code: CONNECTION, got INDEX_DATA`). Ошибка была скрыта общей обёрткой
`except Exception: raise ROSBagUnindexedException()`, поэтому искать пришлось подменой исключения.

Что НЕ поддерживается сознательно: сжатие (bz2/lz4), несколько чанков, сервисы, latched-топики.
Для «записать сигналы с платы и открыть в rosbag/PlotJuggler» этого достаточно.
"""
import bz2
import struct

import msg_types

MAGIC = b"#ROSBAG V2.0\n"

# op-коды записей
OP_MESSAGE_DATA = 0x02
OP_BAG_HEADER = 0x03
OP_INDEX_DATA = 0x04
OP_CHUNK = 0x05
OP_CHUNK_INFO = 0x06
OP_CONNECTION = 0x07

# Стандартные описания используемых типов берутся из общего реестра `msg_types` (md5 и определения
# сверены с ROS через `rosmsg md5`), иначе rosbag не сопоставит сообщения с типом.


def _field(name, value):
    """Поле заголовка: длина + "имя=значение"."""
    encoded = name.encode("ascii") + b"=" + value
    return struct.pack("<I", len(encoded)) + encoded


def _record(fields, data=b""):
    """Собрать запись: header_len | header | data_len | data."""
    header = b"".join(_field(name, value) for name, value in fields)
    return struct.pack("<I", len(header)) + header + struct.pack("<I", len(data)) + data


def _time(seconds, nanoseconds=0):
    """ros::Time в формате bag: uint64, где младшие 32 бита — секунды, старшие — наносекунды."""
    return struct.pack("<Q", int(seconds) | (int(nanoseconds) << 32))


class BagWriter:
    """Писатель bag v2.0 без зависимости от ROS (сжатие чанка — bz2 из stdlib, по выбору).

    Сообщения копятся в памяти и пишутся одним чанком при close(): для длинных записей лучше mcap.
    """

    def __init__(self, path, compression=None):
        self.path = path
        self.file = open(path, "wb")
        self.file.write(MAGIC)
        self.header_pos = self.file.tell()
        # bag header пишем сразу, index_pos заполним в close(): размер фиксирован, правим на месте.
        self.file.write(_record([
            ("op", bytes([OP_BAG_HEADER])),
            ("index_pos", struct.pack("<Q", 0)),
            ("conn_count", struct.pack("<I", 0)),
            ("chunk_count", struct.pack("<I", 0)),
        ]))
        self.connections = {}       # topic -> {id, message, kind}
        self.compression = compression
        self.messages = []          # (topic, seconds, nanoseconds, payload)
        self.chunk_pos = None
        self.closed = False

    def add_connection(self, topic, message, kind=None, md5sum=None, definition=None):
        """Зарегистрировать топик.

        `md5sum` и `definition` можно передать явно — это нужно для произвольных типов, которых нет в
        реестре `msg_types` (например, собственные сообщения прошивки). Тогда данные пишутся через
        `write_bytes` уже сериализованными.
        """
        if md5sum is None or definition is None:
            if message not in msg_types.TYPES:
                raise ValueError("тип не в реестре, передайте md5sum и definition: %s" % message)
            md5sum = md5sum or msg_types.md5(message)
            definition = definition or msg_types.definition(message)
        if topic not in self.connections:
            self.connections[topic] = {"id": len(self.connections) + 1, "message": message,
                                       "kind": kind, "md5": md5sum, "definition": definition}

    def write_bytes(self, topic, payload, seconds, nanoseconds=0):
        """Добавить сообщение с ГОТОВЫМ payload (для произвольных типов и записи любых топиков)."""
        if topic not in self.connections:
            raise ValueError("топик не зарегистрирован: %s" % topic)
        self.messages.append((topic, int(seconds), int(nanoseconds), bytes(payload)))

    def write(self, topic, value, seconds, nanoseconds=0):
        """Добавить сообщение (сохраняется в памяти, пишется при close())."""
        if topic not in self.connections:
            raise ValueError("топик не зарегистрирован: %s" % topic)
        self.messages.append((topic, int(seconds), int(nanoseconds),
                             msg_types.serialize(self.connections[topic]["message"], value)))

    def _connection_record(self, topic):
        info = self.connections[topic]
        md5 = info["md5"]
        definition = info["definition"]
        # Данные connection-записи — такое же полевое кодирование, что и заголовок.
        body = b"".join([
            _field("topic", topic.encode("utf-8")),
            _field("type", info["message"].encode("utf-8")),
            _field("md5sum", md5.encode("ascii")),
            _field("message_definition", definition.encode("utf-8")),
        ])
        return _record([("op", bytes([OP_CONNECTION])),
                        ("conn", struct.pack("<I", info["id"])),
                        ("topic", topic.encode("utf-8"))], body)

    def close(self):
        """Записать chunk, индексы и починить bag header."""
        if self.closed:
            return
        # Тело чанка: сначала все connection-записи, затем сообщения (порядок не важен, важен offset).
        body = bytearray()
        for topic in self.connections:
            body += self._connection_record(topic)

        # offset каждого сообщения считается от начала данных чанка — так требует index data.
        per_connection = {topic: [] for topic in self.connections}
        start_time = end_time = None
        for topic, seconds, nanoseconds, payload in self.messages:
            offset = len(body)
            stamp = (topic, start_time, end_time)
            body += _record([("op", bytes([OP_MESSAGE_DATA])),
                             ("conn", struct.pack("<I", self.connections[topic]["id"])),
                             ("time", _time(seconds, nanoseconds))], payload)
            per_connection[topic].append((_time(seconds, nanoseconds), offset))
            if start_time is None or (seconds, nanoseconds) < start_time:
                start_time = (seconds, nanoseconds)
            if end_time is None or (seconds, nanoseconds) > end_time:
                end_time = (seconds, nanoseconds)

        # bz2 из стандартной библиотеки: файл читает rosbag в WSL/Linux, а writer работает и в
        # Windows. Поле `size` — размер ДО сжатия (так его понимает rosbag).
        self.chunk_pos = self.file.tell()
        if self.compression == "bz2":
            chunk_name, chunk_data = b"bz2", bz2.compress(bytes(body), 9)
        else:
            chunk_name, chunk_data = b"none", bytes(body)
        self.file.write(_record([("op", bytes([OP_CHUNK])),
                                 ("compression", chunk_name),
                                 ("size", struct.pack("<I", len(body)))], chunk_data))

        # index data идут СРАЗУ за чанком: читатель находит их, перейдя на позицию чанка и пропустив
        # саму запись чанка. Если положить их в конец файла, rosbag не увидит индекс.
        for topic, entries in per_connection.items():
            data = bytearray()
            for stamp, offset in entries:
                data += stamp + struct.pack("<I", offset)
            self.file.write(_record([("op", bytes([OP_INDEX_DATA])),
                                     ("ver", struct.pack("<I", 1)),
                                     ("conn", struct.pack("<I", self.connections[topic]["id"])),
                                     ("count", struct.pack("<I", len(entries)))], bytes(data)))

        # index_pos указывает на первую connection-запись: с этого места читатель читает
        # conn_count описаний типов, а затем chunk_count записей chunk info.
        index_pos = self.file.tell()
        for topic in self.connections:
            self.file.write(self._connection_record(topic))

        # chunk info: какие соединения и сколько сообщений в этом чанке.
        data = bytearray()
        used = 0
        for topic, entries in per_connection.items():
            if not entries:
                continue
            data += struct.pack("<I", self.connections[topic]["id"])
            data += struct.pack("<I", len(entries))
            used += 1
        self.file.write(_record([("op", bytes([OP_CHUNK_INFO])),
                                 ("ver", struct.pack("<I", 1)),
                                 ("chunk_pos", struct.pack("<Q", self.chunk_pos)),
                                 ("start_time", _time(*(start_time or (0, 0)))),
                                 ("end_time", _time(*(end_time or (0, 0)))),
                                 ("count", struct.pack("<I", used))], bytes(data)))

        # Починить bag header: вписать index_pos и счётчики.
        self.file.seek(self.header_pos)
        self.file.write(_record([
            ("op", bytes([OP_BAG_HEADER])),
            ("index_pos", struct.pack("<Q", index_pos)),
            ("conn_count", struct.pack("<I", len(self.connections))),
            ("chunk_count", struct.pack("<I", 1)),
        ]))
        self.file.close()
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
