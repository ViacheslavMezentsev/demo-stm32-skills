"""Реестр стандартных типов std_msgs: md5, определение, формат сериализации.

md5 и определения взяты у самого ROS (`rosmsg md5`) на стенде, а не по памяти: если они разойдутся с
ROS, rosbag не сопоставит сообщения с типом. Проверено 17.10.2026 для всех типов ниже.

`struct` — код для `struct.pack("<" + struct, value)`; для String сериализация особая (uint32 длина +
UTF-8), поэтому вместо кода стоит None.
"""
import struct

SCHEMA_ENCODING = "ros1msg"     # как MCAP описывает схемы ROS1
MESSAGE_ENCODING = "ros1"       # как MCAP описывает сериализацию сообщений ROS1

TYPES = {
    "std_msgs/Bool": {"md5": "8b94c1b53db61fb6aed406028ad6332a", "field": "bool", "struct": "?"},
    "std_msgs/Int8": {"md5": "27ffa0c9c4b8fb8492252bcad9e5c57b", "field": "int8", "struct": "b"},
    "std_msgs/Int16": {"md5": "8524586e34fbd7cb1c08c5f5f1ca0e57", "field": "int16", "struct": "h"},
    "std_msgs/Int32": {"md5": "da5909fbe378aeaf85e547e830cc1bb7", "field": "int32", "struct": "i"},
    "std_msgs/Int64": {"md5": "34add168574510e6e17f5d23ecc077ef", "field": "int64", "struct": "q"},
    "std_msgs/UInt8": {"md5": "7c8164229e7d2c17eb95e9231617fdee", "field": "uint8", "struct": "B"},
    "std_msgs/UInt16": {"md5": "1df79edf208b629fe6b81923a544552d", "field": "uint16", "struct": "H"},
    "std_msgs/UInt32": {"md5": "304a39449588c7f8ce2df6e8001c5fce", "field": "uint32", "struct": "I"},
    "std_msgs/UInt64": {"md5": "1b2a79973e8bf53d7b53acb71299cb57", "field": "uint64", "struct": "Q"},
    "std_msgs/Float32": {"md5": "73fcbf46b49191e672908e50842a83d4", "field": "float32", "struct": "f"},
    "std_msgs/Float64": {"md5": "fdb28210bfa9d7c91146260178d9a584", "field": "float64", "struct": "d"},
    "std_msgs/String": {"md5": "992ce8a1687cec8c8bd883ec73ca41d1", "field": "string", "struct": None},
}

# Соответствие «вид значения в спецификации поля» -> тип сообщения std_msgs.
# Нужно, чтобы в fields.toml можно было писать kind = "float32" и получить std_msgs/Float32.
KIND_TO_MESSAGE = {
    "bool": "std_msgs/Bool",
    "int8": "std_msgs/Int8", "int16": "std_msgs/Int16", "int32": "std_msgs/Int32",
    "int64": "std_msgs/Int64",
    "uint8": "std_msgs/UInt8", "uint16": "std_msgs/UInt16", "uint32": "std_msgs/UInt32",
    "uint64": "std_msgs/UInt64",
    "float32": "std_msgs/Float32", "float64": "std_msgs/Float64",
    "string": "std_msgs/String",
    # Счётчик времени: значение уже в секундах, упаковывается как uint32.
    "uint32_seconds": "std_msgs/UInt32",
}


def definition(message):
    """Текст .msg — идёт в connection-запись rosbag и в схему MCAP."""
    return "%s data\n" % TYPES[message]["field"]


def md5(message):
    return TYPES[message]["md5"]


def serialize(message, value):
    """Сериализовать значение в payload сообщения ROS1."""
    spec = TYPES[message]
    if spec["struct"] is None:                     # String
        encoded = str(value).encode("utf-8")
        return struct.pack("<I", len(encoded)) + encoded
    if spec["struct"] == "?":
        return struct.pack("<?", bool(value))
    if spec["struct"] in "fd":
        return struct.pack("<" + spec["struct"], float(value))
    if spec["struct"] in "bhiq":
        return struct.pack("<" + spec["struct"], int(value))
    return struct.pack("<" + spec["struct"], int(value) & ((1 << (8 * struct.calcsize(spec["struct"]))) - 1))
