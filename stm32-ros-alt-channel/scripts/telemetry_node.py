#!/usr/bin/env python3
"""Альтернативный канал в ROS1: топики из памяти цели. Адреса приходят из JSON, не из кода.

Данные не «передаются» из прошивки — они уже лежат в её RAM. Нода читает эту память через telnet OpenOCD
(`mdw`) и публикует обычным `rospy`. Ни `ros_lib`, ни rosserial-рукопожатия, ни UART, ни правки прошивки
не требуется.

ЧТО ЧИТАЕТСЯ. Список полей задаётся в `fields.toml`, адреса вычисляет `resolve_addresses.py` по
отладочной информации ELF (**без подключения к цели**) и складывает в `addresses.json`. Нода только
использует готовый JSON — поэтому после пересборки прошивки достаточно перезапустить резолвер, а не
править код и не искать смещения руками.

ЗАПУСК (на стенде, где есть ROS-мастер):
    python3 telemetry_node.py /tmp/addresses.json
"""
import json
import pathlib
import sys

from openocd_link import OpenOcdReader, Telnet
from field_values import aligned_needed, word_at, convert

import rospy
from std_msgs.msg import Float32, Int32, UInt32

OPENOCD_HOST = "127.0.0.1"
OPENOCD_TELNET = 4444
RATE_HZ = 10.0
DEFAULT_SPEC = "/tmp/addresses.json"

# Тип сообщения из спецификации -> класс сообщения ROS.
MESSAGES = {
    "std_msgs/UInt32": UInt32,
    "std_msgs/Float32": Float32,
    "std_msgs/Int32": Int32,
}


class OpenOcdLink(OpenOcdReader):
    """ROS-совместимый адаптер общего транспорта OpenOCD."""
    def __init__(self, host=OPENOCD_HOST, port=OPENOCD_TELNET, timeout=3.0):
        self.telnet = Telnet(host, port, timeout)

    def read_word(self, address):
        return word_at(self.read_words(aligned_needed(address)), address)


def load_spec(path):
    """Прочитать JSON с адресами и проверить, что все типы сообщений поддерживаются."""
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    fields = data.get("fields", [])
    if not fields:
        raise ValueError("в файле нет полей")
    for field in fields:
        if field.get("message") not in MESSAGES:
            raise ValueError("неподдерживаемый тип сообщения: %s" % field.get("message"))
    return data


def main():
    spec_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SPEC
    try:
        spec = load_spec(spec_path)
    except (OSError, ValueError) as error:
        print("ОШИБКА спецификации %s: %s" % (spec_path, error), file=sys.stderr)
        print("Сначала выполните resolve_addresses.py — он создаст JSON с адресами.", file=sys.stderr)
        return 1

    rospy.init_node("stm32_alt_channel", anonymous=False)
    publishers = {field["topic"]: rospy.Publisher(field["topic"], MESSAGES[field["message"]],
                                                 queue_size=10)
                  for field in spec["fields"]}

    try:
        link = OpenOcdLink()
    except OSError as error:
        rospy.logerr("нет связи с OpenOCD %s:%d — %s", OPENOCD_HOST, OPENOCD_TELNET, error)
        return 1

    rospy.loginfo("канал открыт: %d полей, %.0f Гц, ELF %s",
                  len(spec["fields"]), RATE_HZ, spec.get("elf_sha256", "")[:16])
    for field in spec["fields"]:
        rospy.loginfo("  %-28s 0x%08X  %s", field["topic"], field["address"], field["expr"][:48])

    rate = rospy.Rate(RATE_HZ)
    previous = {}
    published = skipped = 0
    try:
        while not rospy.is_shutdown():
            for field in spec["fields"]:
                word = link.read_word(field["address"])
                if word is None:
                    skipped += 1
                    continue
                value = convert(word, field["kind"], field.get("shift", 0), field.get("mask", 0xFFFFFFFF))
                # Правдоподобность: отсекает чтение «не той» памяти.
                low, high = field.get("min"), field.get("max")
                if (low is not None and value < low) or (high is not None and value > high):
                    skipped += 1
                    continue
                # Для счётчиков времени следим за монотонностью (сброс возможен при перезагрузке).
                if field["kind"] == "uint32_seconds":
                    last = previous.get(field["topic"])
                    if last is not None and value < last and last - value < 5:
                        skipped += 1
                        continue
                    previous[field["topic"]] = value
                publishers[field["topic"]].publish(MESSAGES[field["message"]](data=value))
                published += 1
            rate.sleep()
    except KeyboardInterrupt:
        pass
    finally:
        link.close()
        rospy.loginfo("опубликовано %d значений, пропущено %d", published, skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
