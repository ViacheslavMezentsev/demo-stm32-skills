#!/usr/bin/env python3
"""Перечислить глобальные переменные прошивки в готовом для спецификации полей виде (CSV).

Назначение: избавить от ручного копания в артефактах сборки. По ELF (через GDB и отладочную информацию
DWARF) находит глобальные переменные, фильтрует по пути исходника (например, только `src/`), раскрывает
структуры на один уровень в скалярные поля (без детализации до бит) и пишет CSV со столбцами, которые прямо
ложатся в `fields.toml`:

    file, expr, type, size, kind, message, note

`expr` — выражение для поля (`&имя` или `&имя.поле`), `kind`/`message` — предложенные навыком. Пользователь
отмечает нужные строки и переносит expr/kind/message в спецификацию. Плата при этом не нужна — всё офлайн.

Пример:
    python list_signals.py --elf build/app.elf --path src/ --out signals.csv
"""
import argparse
import csv
import json
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from resolve_addresses import find_gdb  # noqa: E402

GDB_SCRIPT = r'''
import gdb, json, re

PATH_FILTER = %(path)r
rows = []
seen = set()

def kind_of(t):
    t = t.strip_typedefs()
    code = t.code
    if code == gdb.TYPE_CODE_FLT:
        return ("float32", "std_msgs/Float32") if t.sizeof == 4 else (None, None)
    if code in (gdb.TYPE_CODE_INT, gdb.TYPE_CODE_ENUM, gdb.TYPE_CODE_BOOL, gdb.TYPE_CODE_CHAR):
        if t.sizeof > 4:
            return (None, None)            # 64-битное не читается одним словом
        signed = False
        try:
            signed = t.is_signed
        except Exception:
            signed = str(t).find("unsigned") < 0 and code == gdb.TYPE_CODE_INT
        return ("int32", "std_msgs/Int32") if signed else ("uint32", "std_msgs/UInt32")
    if code == gdb.TYPE_CODE_PTR:
        return ("uint32", "std_msgs/UInt32")
    return (None, None)

def add(expr, typ, size, file_short):
    if expr in seen:
        return
    seen.add(expr)
    t = typ.strip_typedefs()
    kind, message = kind_of(t)
    if kind is None:
        return
    rows.append({"file": file_short, "expr": expr, "type": str(typ), "size": size,
                 "kind": kind, "message": message, "note": ""})

def resolve(name):
    for cand in (name, "&'(anonymous namespace)::%%s'" %% name, "'(anonymous namespace)::%%s'" %% name):
        try:
            val = gdb.parse_and_eval(cand if cand.startswith("&") else cand)
            return val
        except gdb.error:
            continue
    return None

text = gdb.execute("info variables", to_string=True)
cur_file = None
for line in text.splitlines():
    fm = re.match(r"File (.+):\s*$", line)
    if fm:
        cur_file = fm.group(1)
        continue
    if line.strip() == "" or line.startswith("Non-debugging") or line.startswith("All defined"):
        if line.startswith("Non-debugging"):
            cur_file = None
        continue
    if not cur_file or PATH_FILTER not in cur_file.replace("\\", "/"):
        continue
    decl = re.sub(r"^\s*\d+:\s*", "", line).strip().rstrip(";")
    if "(" in decl:                         # функции и указатели на функции пропускаем
        continue
    decl_noarr = re.sub(r"\[.*?\]", "", decl)
    ids = re.findall(r"[A-Za-z_]\w*", decl_noarr)
    if not ids:
        continue
    name = ids[-1]
    val = resolve(name)
    if val is None or val.address is None:
        continue
    t = val.type.strip_typedefs()
    base = "&" + name
    file_short = cur_file.replace("\\", "/")
    if t.code == gdb.TYPE_CODE_STRUCT:
        for field in t.fields():
            if field.bitsize:               # битовые поля не детализируем
                continue
            if field.name is None:
                continue
            add("&%%s.%%s" %% (name, field.name), field.type, field.type.sizeof, file_short)
    else:
        add(base, val.type, t.sizeof, file_short)

with open(%(out)r, "w") as handle:
    handle.write(json.dumps(rows))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--elf", required=True, help="ELF прошивки с отладочной информацией (-g)")
    parser.add_argument("--path", default="src/", help="фильтр по пути исходника (подстрока), по умолчанию src/")
    parser.add_argument("--out", default="", help="CSV (по умолчанию <каталог ELF>/signals.csv)")
    args = parser.parse_args(argv)

    elf = pathlib.Path(args.elf)
    if not elf.is_file():
        raise SystemExit("нет ELF %s" % elf)
    out = pathlib.Path(args.out) if args.out else elf.parent / "signals.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    gdb = find_gdb()
    rows_file = out.with_suffix(".rows.json")
    script = out.with_suffix(".gdb.py")
    script.write_text(GDB_SCRIPT % {"path": args.path, "out": str(rows_file)}, encoding="utf-8")
    result = subprocess.run([gdb, "-q", "-batch", "-ex", "set pagination off",
                             "-x", str(script), str(elf)], capture_output=True, text=True)
    if not rows_file.is_file():
        raise SystemExit("GDB не выдал список переменных:\n%s" % (result.stdout + result.stderr)[-600:])
    rows = json.loads(rows_file.read_text(encoding="utf-8"))
    rows_file.unlink(missing_ok=True)
    script.unlink(missing_ok=True)

    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file", "expr", "type", "size", "kind", "message", "note"])
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (r.get("file", ""), r["expr"])):
            writer.writerow({k: row.get(k, "") for k in writer.fieldnames})
    print("сигналов: %d -> %s" % (len(rows), out))
    print("  перенесите нужные строки (expr, kind, message) в спецификацию полей (fields.toml)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
