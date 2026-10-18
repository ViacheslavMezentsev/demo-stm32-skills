#!/usr/bin/env python3
"""Запись выбранных ROS1-топиков в bag или mcap. Запускается внутри WSL.

ПОЧЕМУ AnyMsg. Мы не хотим зависеть от конкретных типов сообщений: `rospy.AnyMsg` отдаёт уже
сериализованные байты и `_connection_header` с полями `type`, `md5sum`, `message_definition`. Этого
достаточно, чтобы зарегистрировать топик в нашем писателе и записать payload как есть — поэтому можно
писать **любой** топик прошивки, а не только те, что есть в реестре `msg_types`.

ПАМЯТЬ. Сообщения копятся в памяти и пишутся при закрытии: так проще гарантировать корректный индекс.
Для диагностических записей в минуты это нормально; для длинных сессий делайте несколько файлов.

ЗАПУСК:
    python3 recorder_node.py --format mcap --out /mnt/d/.../board.mcap --topics all --seconds 10
    python3 recorder_node.py --format bag --out /mnt/d/.../batt.bag \\
        --topics /board/sensor/batt_v,/board/sys/uptime_s
"""
import argparse
import pathlib
import signal
import sys
import threading

import rospy
from rospy.msg import AnyMsg

# Писатели лежат рядом (в том же каталоге проекта, доступном из WSL через /mnt/...).
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bag_writer import BagWriter          # noqa: E402
from mcap_writer import McapWriter        # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--format", choices=["bag", "mcap"], default="bag")
    parser.add_argument("--out", required=True)
    parser.add_argument("--topics", default="all",
                        help="список через запятую или all (все, что публикуются)")
    parser.add_argument("--seconds", type=float, default=0.0, help="0 — писать до остановки")
    parser.add_argument("--compress", choices=["none", "bz2"], default="none",
                        help="сжатие чанка для bag (bz2 — stdlib)")
    parser.add_argument("--chunk-size", type=int, default=1000,
                        help="сообщений на чанк для mcap")
    args = parser.parse_args()

    rospy.init_node("stm32_recorder", anonymous=False)

    wanted = None if args.topics.strip().lower() == "all" else \
        {t.strip() for t in args.topics.split(",") if t.strip()}

    if wanted is None:
        published = rospy.client.get_published_topics()
        # all — все топики прошивки: служебные топики ROS (/rosout, /rosout_agg) не пишутся.
        selected = {name: message_type for name, message_type in published
                    if not name.startswith("/rosout")}
    else:
        published = dict(rospy.client.get_published_topics())
        selected = {name: published.get(name, "?") for name in wanted}

    if not selected:
        print("ОШИБКА: не найдено топиков для записи (мастер пуст?)", file=sys.stderr)
        return 1

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "mcap":
        storage = McapWriter(out, chunk_messages=args.chunk_size)
    else:
        storage = BagWriter(out, compression=None if args.compress == "none"
                            else args.compress)
    registered = set()
    started = rospy.Time.now()
    finished = threading.Event()

    def make_callback(name):
        """Тип и определение берём из connection header, payload пишем как есть."""
        def callback(message):
            header = getattr(message, "_connection_header", {}) or {}
            stamp = rospy.Time.now()
            if name not in registered:
                storage.add_connection(name, header.get("type", "unknown"),
                                       md5sum=header.get("md5sum", ""),
                                       definition=header.get("message_definition", ""))
                registered.add(name)
            storage.write_bytes(name, message._buff, stamp.secs, stamp.nsecs)
            if args.seconds and (stamp - started).to_sec() >= args.seconds:
                finished.set()
        return callback

    subscribers = [rospy.Subscriber(name, AnyMsg, make_callback(name), queue_size=50)
                   for name in sorted(selected)]

    print("запись %d топиков в %s (%s)" % (len(selected), out, args.format))
    for name in sorted(selected):
        print("  %-40s %s" % (name, selected[name]))

    def stop(*_ignored):
        finished.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if args.seconds:
        rospy.Timer(rospy.Duration(args.seconds), lambda _event: finished.set(), oneshot=True)

    while not finished.is_set() and not rospy.is_shutdown():
        rospy.sleep(0.2)

    for subscriber in subscribers:
        subscriber.unregister()
    storage.close()
    print("готово: %s (%d байт), топиков записано %d"
          % (out, out.stat().st_size, len(registered)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
