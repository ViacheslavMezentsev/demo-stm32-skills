"""Разрешение адресов полей: символика ELF через gdb-py3 и регистры периферии через SVD.

ЗАЧЕМ. Для маршрута через OpenOCD читать можно не только переменные прошивки, но и регистры периферии.
Их имена удобнее брать из SVD (`resources/*.svd`), который уже лежит в проекте, чем помнить сырые
адреса. Поэтому поле в `fields.toml` может задаваться двумя способами:

    expr = "&uwTick"                          — символика из ELF (считает GDB, офлайн)
    expr = "svd:ADC1.DR"                      — регистр периферии (считает SVD, без GDB и без цели)
    expr = "svd:ADC1.SR.EOC"                  — поле регистра: дополнительно получаем сдвиг и маску
    expr = "(char*)&sensors + (long)&(...)"   — поле структуры (смещение по DWARF)

Выражения `svd:` считаются локально, остальные — в `gdb-py3` по отладочной информации ELF (без платы).
Результат складывается в JSON для ноды/записи.

Использование:
    python resolve_addresses.py                       # поля по умолчанию
    python resolve_addresses.py --svd resources/STM32G474.svd
    python resolve_addresses.py --target 127.0.0.1:3333   # адреса, вычисляемые во время работы
"""
import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from svd_lookup import SvdLookup

try:
    import tomllib
except ModuleNotFoundError:                       # Python < 3.11
    import tomli as tomllib

DEFAULT_ELF = os.environ.get("FIRMWARE_ELF", "build/firmware.elf")
# GDB с Python: xPack (Windows/Linux) или gdb-multiarch из дистрибутива Linux.
GDB_NAMES = ("arm-none-eabi-gdb-py3", "arm-none-eabi-gdb", "gdb-multiarch")


def find_gdb():
    """Найти gdb с поддержкой Python: $GDB, затем PATH, затем $STM32_TOOLCHAIN_PATH/bin."""
    import shutil

    if os.environ.get("GDB"):
        return os.environ["GDB"]
    toolchain = os.environ.get("STM32_TOOLCHAIN_PATH")
    for name in GDB_NAMES:
        found = shutil.which(name)
        if not found and toolchain:
            found = shutil.which(name, path=str(pathlib.Path(toolchain) / "bin"))
        if found and gdb_has_python(found):
            return found
    raise SystemExit("не найден GDB с Python (%s); задайте GDB=<путь>" % ", ".join(GDB_NAMES))


def gdb_has_python(gdb):
    """Есть ли в этой сборке GDB поддержка Python (xPack без -py3 её не имеет)."""
    try:
        result = subprocess.run([gdb, "-q", "-batch", "-ex", "python print(42)"],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "42" in result.stdout


def build_gdb_script(fields, elf, out_path, target=None):
    """Скрипт для gdb-py3: вычислить адреса и записать промежуточный JSON."""
    lines = [
        "import gdb, json",
        "spec = json.loads(%r)" % json.dumps(fields),
        "result = {'fields': []}",
    ]
    if target:
        lines.append("gdb.execute('target remote %s', to_string=True)" % target)
    lines += [
        "for item in spec:",
        "    value = gdb.parse_and_eval(item['expr'])",
        "    entry = {k: item[k] for k in ('topic', 'message', 'kind') if k in item}",
        "    entry['address'] = int(value)",
        "    entry['expr'] = item['expr']",
        "    for key in ('min', 'max', 'shift', 'mask'):",
        "        if key in item:",
        "            entry[key] = item[key]",
        "    result['fields'].append(entry)",
        "with open(%r, 'w', encoding='utf-8') as handle:" % str(out_path),
        "    json.dump(result, handle, ensure_ascii=False, indent=2)",
        "",
    ]
    return "\n".join(lines)


def resolve(elf, spec_path, svd_path, target=None):
    """Вернуть (данные спецификации, список полей с адресами)."""
    spec = tomllib.loads(pathlib.Path(spec_path).read_text(encoding="utf-8"))
    fields = spec.get("field", [])
    if not fields:
        raise ValueError("в %s нет ни одного [[field]]" % spec_path)

    # Поля различаются по номеру в спецификации, а не по выражению: два поля с одним `expr`
    # (например, одно значение в разных топиках) остаются двумя полями.
    svd_indices = [i for i, f in enumerate(fields) if str(f.get("expr", "")).startswith("svd:")]
    gdb_indices = [i for i in range(len(fields)) if i not in svd_indices]

    resolved = {}
    if svd_indices:
        if not svd_path:
            raise ValueError("поля svd: требуют SVD-файла: --svd или STM32_SVD")
        lookup = SvdLookup(svd_path)
        for index in svd_indices:
            item = fields[index]
            address, size_bits, shift, mask = lookup.resolve(item["expr"][4:])
            entry = {k: item[k] for k in ("topic", "message", "kind") if k in item}
            entry.update(address=address, expr=item["expr"], size_bits=size_bits)
            if shift or mask != 0xFFFFFFFF:
                entry.update(shift=shift, mask=mask)
            for key in ("min", "max"):
                if key in item:
                    entry[key] = item[key]
            resolved[index] = entry

    if gdb_indices:
        gdb = find_gdb()
        # Путь промежуточного JSON создаём заранее: скрипт для GDB должен содержать его явно
        # (заглушка с последующей заменой ненадёжна — GDB писал в файл с «пустым» путём).
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
            intermediate = pathlib.Path(handle.name)
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(build_gdb_script([fields[i] for i in gdb_indices], elf, intermediate, target))
            script = pathlib.Path(handle.name)
        try:
            result = subprocess.run([gdb, "-q", "-batch", str(elf), "-x", str(script)],
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace")
        finally:
            script.unlink(missing_ok=True)
        try:
            payload = json.loads(intermediate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ValueError("GDB не вычислил адреса:\n%s"
                             % (result.stdout + result.stderr)[-800:])
        finally:
            intermediate.unlink(missing_ok=True)
        for index, entry in zip(gdb_indices, payload["fields"]):
            resolved[index] = entry

    ordered = [resolved[i] for i in range(len(fields))]
    data = {"schema": 1, "elf": str(elf),
            "elf_sha256": hashlib.sha256(pathlib.Path(elf).read_bytes()).hexdigest(),
            "svd": str(svd_path) if svd_path else None, "fields": ordered}
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--elf", default=DEFAULT_ELF)
    parser.add_argument("--spec", default=str(HERE / "fields.toml"))
    parser.add_argument("--svd", default=os.environ.get("STM32_SVD"),
                        help="SVD-файл MCU (по умолчанию $STM32_SVD); нужен для полей svd:...")
    parser.add_argument("--out", default=str(HERE / "addresses.json"))
    parser.add_argument("--target", help="host:port живой цели (нужно для адресов из кучи)")
    args = parser.parse_args()

    elf = pathlib.Path(args.elf)
    if not elf.is_file():
        print("ОШИБКА: нет ELF %s" % elf)
        return 1

    try:
        data = resolve(elf, args.spec, args.svd, args.target)
    except (OSError, ValueError) as error:
        print("ОШИБКА: %s" % error)
        return 1

    out = pathlib.Path(args.out)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("ELF:   %s" % elf)
    print("SVD:   %s" % (pathlib.Path(args.svd).name if args.svd else "не задан (полей svd: нет)"))
    print("режим: %s" % (("живая цель " + args.target) if args.target
                         else "офлайн по ELF (плата не нужна)"))
    print("\nадреса полей:")
    print("  %-28s %-18s %-10s %s" % ("топик", "тип", "адрес", "выражение"))
    for field in data["fields"]:
        extra = ""
        if "shift" in field:
            extra = " (сдвиг %d, маска 0x%08X)" % (field["shift"], field["mask"])
        print("  %-28s %-18s 0x%08X %s%s"
              % (field["topic"], field["message"], field["address"], field["expr"][:40], extra))
    print("\nзаписано: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
