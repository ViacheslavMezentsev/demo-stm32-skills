#!/usr/bin/env python3
"""Общие модули навыков: один оригинал, побайтные копии в других навыках.

Навык копируют в проект отдельной папкой, поэтому общий код лежит в каждом навыке, который им
пользуется. Правится только оригинал; копии обновляет этот скрипт, а CI (задание Kits) проверяет,
что они не разошлись.

    python3 ci/sync_shared.py            # скопировать оригиналы во все навыки из списка
    python3 ci/sync_shared.py --check    # только сравнить; код 1, если копия отличается
"""
import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Оригинал (каталог навыка) -> файлы -> навыки, в которые они копируются.
SHARED = {
    "stm32-ros-alt-channel/scripts": {
        "files": [
            "bag_writer.py", "mcap_writer.py", "msg_types.py", "svd_lookup.py", "resolve_addresses.py",
            "openocd_link.py", "field_values.py", "gdb_tools.py", "process_tools.py",
        ],
        "copies": ["stm32-diag-record/scripts"],
    },
}


def pairs():
    for source, spec in SHARED.items():
        for name in spec["files"]:
            for target in spec["copies"]:
                yield ROOT / source / name, ROOT / target / name


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="не копировать, только проверить совпадение")
    args = parser.parse_args(argv)
    differing = []
    for source, target in pairs():
        data = source.read_bytes()
        if target.is_file() and target.read_bytes() == data:
            continue
        if args.check:
            differing.append(target.relative_to(ROOT).as_posix())
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        print("обновлён %s" % target.relative_to(ROOT).as_posix())
    if differing:
        print("копии отличаются от оригинала: %s" % ", ".join(differing))
        print("правьте оригинал и запустите: python3 ci/sync_shared.py")
        return 1
    if args.check:
        print("копии совпадают с оригиналами (%d файлов)" % sum(1 for _ in pairs()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
