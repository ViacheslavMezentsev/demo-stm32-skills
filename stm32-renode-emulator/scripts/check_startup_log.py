#!/usr/bin/env python3
"""Проверка стартового лога прошивки, снятого в эмуляторе.

Задача проверки — убедиться, что в эмуляторе (QEMU или Renode) получен **полный**
стартовый лог и что диагностика соответствует именно этой среде. Если лога нет,
он оборван или в нём чужие для среды значения (например, CPUID Cortex-M7 вместо
Cortex-M4), дальше проверять в эмуляторе нечего.

Ожидания лежат в TOML-файле прошивки (образец — assets/expected.example.toml):
  [require].lines — регулярные выражения, каждое обязано найтись в логе;
  [forbid].lines  — выражения, которых быть не должно;
  first_line/last_line — первая и последняя содержательные строки;
  min_lines       — минимальное число строк (защита от обрыва).
Подстановки {version}, {build}, {revision}, {crc32}, {size} берутся из самого
ELF (.fw_version, .checksum) и из размера собранного образа: проверяется, что
эмулятор напечатал данные именно этой сборки.

Запуск: python3 scripts/check_startup_log.py --log <лог> --elf <elf> --expected <toml>
Код возврата: 0 — PASS; 1 — FAIL (подробности в журнале и в report JSON).
"""
import argparse
import json
import re
import struct
import subprocess
import sys
from pathlib import Path

PREFIX = re.compile(r"^\[\s*(\d+\.\d+)\]\s?")


def elf_data(elf: Path, out_dir: Path, objcopy: str) -> dict:
    """Версия, CRC32 и размер образа из ELF (те же секции, что проверяет CI)."""
    data = {"version": None, "build": None, "revision": None, "crc32": None, "size": None}
    out_dir.mkdir(parents=True, exist_ok=True)

    def section(name: str) -> bytes:
        target = out_dir / (name.lstrip(".") + ".bin")
        result = subprocess.run([objcopy, "-O", "binary", "-j", name, str(elf), str(target)],
                                capture_output=True)
        if result.returncode != 0 or not target.exists():
            return b""
        return target.read_bytes()

    firmware = section(".fw_version")
    if len(firmware) >= 8:
        major, minor, patch, _res, build, revision = struct.unpack("<BBBBHH", firmware[:8])
        data.update(version=f"{major}.{minor}.{patch}", build=build, revision=revision)

    checksum = section(".checksum")
    if len(checksum) >= 4:
        data["crc32"] = f"0x{int.from_bytes(checksum[:4], 'little'):08X}"

    image = out_dir / "image.bin"
    result = subprocess.run([objcopy, "-O", "binary", str(elf), str(image)], capture_output=True)
    if result.returncode == 0 and image.exists():
        data["size"] = image.stat().st_size

    return data


def read_log(path: Path) -> tuple[list[str], list[str], int]:
    """Строки без префикса времени, строки с префиксом и число строк без префикса."""
    plain, stamped, unstamped = [], [], 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        match = PREFIX.match(raw)
        if match:
            stamped.append(raw)
        else:
            unstamped += 1
        plain.append(PREFIX.sub("", raw))
    return plain, stamped, unstamped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--elf", required=True, type=Path)
    parser.add_argument("--expected", required=True, type=Path)
    parser.add_argument("--report", type=Path, help="куда записать JSON-отчёт")
    parser.add_argument("--objcopy", default="arm-none-eabi-objcopy")
    args = parser.parse_args()

    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # Python 3.8-3.10: pip install tomli
        import tomli as tomllib

    expected = tomllib.loads(args.expected.read_text(encoding="utf-8"))
    report = {
        "status": "FAIL",
        "log": str(args.log),
        "elf": str(args.elf),
        "expected": str(args.expected),
        "emulator": expected.get("emulator"),
        "machine": expected.get("machine"),
        "errors": [],
        "require": [],
        "forbid": [],
    }

    if not args.log.exists() or args.log.stat().st_size == 0:
        report["errors"].append(
            "стартовый лог не получен — дальнейшие проверки в эмуляторе не имеют смысла")
        finish(report, args)
        return 1

    # Промежуточные .bin от objcopy — в отдельный подкаталог, чтобы в артефактах
    # прогона рядом со стартовым логом лежали только лог и JSON-отчёт.
    elf = elf_data(args.elf, args.log.parent / "check-tmp", args.objcopy)
    report.update({k: elf[k] for k in ("version", "build", "revision", "crc32", "size")})

    lines, stamped, unstamped = read_log(args.log)
    report["lines"] = len(lines)
    report["lines_with_time"] = len(stamped)
    report["lines_without_time"] = unstamped

    substitutions = {
        "{version}": re.escape(str(elf["version"])),
        "{build}": re.escape(str(elf["build"])),
        "{revision}": re.escape(str(elf["revision"])),
        "{crc32}": re.escape(str(elf["crc32"])),
        "{size}": re.escape(str(elf["size"])),
    }

    def expand(pattern: str) -> str:
        for key, value in substitutions.items():
            pattern = pattern.replace(key, value)
        return pattern

    minimum = expected.get("min_lines")
    if minimum and len(lines) < minimum:
        report["errors"].append(f"в логе {len(lines)} строк, ожидалось не меньше {minimum}")

    for key, target in (("first_line", "первая"), ("last_line", "последняя")):
        pattern = expected.get(key)
        if pattern and lines:
            ok = re.search(expand(pattern), lines[0] if key == "first_line" else lines[-1])
            if not ok:
                report["errors"].append(
                    f"{target} строка лога не совпала с ожиданием: {pattern}")

    for pattern in expected.get("require", {}).get("lines", []):
        regex = expand(pattern)
        found = next((line for line in lines if re.search(regex, line)), None)
        report["require"].append({"pattern": pattern, "matched": found is not None, "line": found})
        if found is None:
            report["errors"].append(f"в логе нет обязательной строки: {pattern}")

    for pattern in expected.get("forbid", {}).get("lines", []):
        regex = expand(pattern)
        hits = [line for line in lines if re.search(regex, line)]
        report["forbid"].append({"pattern": pattern, "matches": hits})
        if hits:
            report["errors"].append(f"в логе найдена запрещённая строка: {pattern} → {hits[0]!r}")

    if unstamped:
        report["errors"].append(f"{unstamped} строк без префикса времени: лог не от sys_log")

    print(f"Эмулятор: {report['emulator']} ({report['machine']}), строк в логе: {len(lines)} "
          f"(с префиксом времени: {len(stamped)})")
    print(f"Образ: v{elf['version']} сборка {elf['build']}.{elf['revision']}, "
          f"CRC32 {elf['crc32']}, размер {elf['size']} Б")
    for item in report["require"]:
        print(f"  [{'OK ' if item['matched'] else 'НЕТ'}] {item['pattern']}")

    if report["errors"]:
        print("ОШИБКА: стартовый лог не соответствует ожиданиям:")
        for error in report["errors"]:
            print("  - " + error)
    else:
        report["status"] = "PASS"
        print("OK: стартовый лог получен полностью и соответствует среде")

    finish(report, args)
    return 0 if report["status"] == "PASS" else 1


def finish(report: dict, args) -> None:
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
