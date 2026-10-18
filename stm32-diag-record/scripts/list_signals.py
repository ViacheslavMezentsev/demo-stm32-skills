#!/usr/bin/env python3
"""Перечислить глобальные переменные прошивки в готовом для спецификации полей виде (CSV).

Назначение: избавить от ручного копания в артефактах сборки. По ELF (через GDB и отладочную информацию
DWARF) находит глобальные переменные, фильтрует по пути исходника (например, только `src/`), раскрывает
структуры до заданной глубины в скалярные поля (без детализации до бит) и пишет CSV со столбцами, которые
прямо ложатся в `fields.toml`:

    file, expr, type, address, size, kind, message, mask, note

В CSV попадают только выражения, проверенные в GDB: `expr` вычисляется тем же `gdb.parse_and_eval`, что
и при записи (resolve_addresses.py), и обязан дать адрес самой переменной этого файла. Для переменных во
внутреннем пространстве имён C++ и одноимённых `static` из разных файлов подбирается рабочая форма
(`&'(anonymous namespace)::имя'`, `&'файл.c'::имя`). `mask` задан для полей короче 32 бит — его нужно
перенести в спецификацию вместе с `expr`, `kind`, `message`, иначе в значение попадут соседние байты.
Плата при этом не нужна — всё офлайн.

Глубина раскрытия (`--depth`): 0 — только скалярные переменные, 1 — поля структур, 2 (по умолчанию) —
поля вложенных структур и т. д. Массивы, объединения и битовые поля не раскрываются (видно в сводке).

Пример:
    python list_signals.py --elf build/app.elf --path src/ --depth 2 --out signals.csv
"""
import argparse
import csv
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from resolve_addresses import find_gdb  # noqa: E402

COLUMNS = ["file", "expr", "type", "address", "size", "kind", "message", "mask", "note"]

# Скрипт для gdb-py3. Параметры приходят через переменную PARAMS_FILE (задаётся до -x), без подстановки
# строк в текст скрипта.
GDB_SCRIPT = r'''
import gdb, json, os, re

P = json.load(open(PARAMS_FILE, encoding="utf-8"))
PATH_FILTER, DEPTH = P["path"], P["depth"]
rows, skipped, seen = [], [], set()


def norm(path):
    return path.replace("\\", "/")


def signed(t):
    try:
        return bool(t.is_signed)
    except (AttributeError, ValueError):
        name = str(t)
        return t.code == gdb.TYPE_CODE_INT and "unsigned" not in name and not name.startswith(("uint", "bool", "_Bool"))


def kind_of(t):
    t = t.strip_typedefs()
    if t.code == gdb.TYPE_CODE_FLT:
        return ("float32", "std_msgs/Float32") if t.sizeof == 4 else (None, None)
    if t.code in (gdb.TYPE_CODE_INT, gdb.TYPE_CODE_ENUM, gdb.TYPE_CODE_BOOL, gdb.TYPE_CODE_CHAR):
        if t.sizeof > 4:
            return (None, None)                   # 64-битное не читается одним словом
        return ("int32", "std_msgs/Int32") if signed(t) else ("uint32", "std_msgs/UInt32")
    if t.code == gdb.TYPE_CODE_PTR:
        return ("uint32", "std_msgs/UInt32")
    return (None, None)


def skip(expr, reason):
    skipped.append({"expr": expr, "reason": reason})


def evaluates_to(expr, address):
    try:
        return int(gdb.parse_and_eval(expr)) == address
    except gdb.error:
        return False


def emit(expr, typ, address, file_short):
    if expr in seen:
        return
    seen.add(expr)
    t = typ.strip_typedefs()
    kind, message = kind_of(t)
    if kind is None:
        skip(expr, "тип %s не читается одним словом" % t)
        return
    if not evaluates_to(expr, address):       # та же проверка, что сделает resolve_addresses.py
        skip(expr, "выражение не проверено в GDB")
        return
    size = t.sizeof
    mask = "0x%X" % ((1 << (8 * size)) - 1) if size < 4 else ""
    rows.append({"file": file_short, "expr": expr, "type": str(typ), "address": "0x%08X" % address,
                 "size": size, "kind": kind, "message": message, "mask": mask, "note": ""})


def walk(path, typ, address, file_short, level):
    t = typ.strip_typedefs()
    if t.code == gdb.TYPE_CODE_STRUCT:
        if level >= DEPTH:
            skip("&" + path, "структура глубже --depth %d" % DEPTH)
            return
        for field in t.fields():
            if field.name is None or field.is_base_class or not hasattr(field, "bitpos"):
                continue                       # безымянные, базовые классы, статические члены
            if field.bitsize:
                skip("&%s.%s" % (path, field.name), "битовое поле")
                continue
            walk("%s.%s" % (path, field.name), field.type, address + field.bitpos // 8, file_short, level + 1)
    elif t.code == gdb.TYPE_CODE_ARRAY:
        skip("&" + path, "массив")
    elif t.code == gdb.TYPE_CODE_UNION:
        skip("&" + path, "объединение")
    else:
        emit("&" + path, typ, address, file_short)


def symbol_for(qname, file_name):
    """Символ переменной именно этого файла: среди одноимённых static выбирается по таблице файла."""
    found = []
    try:
        found += list(gdb.lookup_static_symbols(qname))
    except (AttributeError, gdb.error):
        pass
    try:
        g = gdb.lookup_global_symbol(qname)
        if g is not None:
            found.append(g)
    except gdb.error:
        pass
    ambiguous = len(found) > 1
    for s in found:
        if s.symtab is not None and file_name in (norm(s.symtab.fullname()), norm(s.symtab.filename)):
            return s, ambiguous
    # Глобальная переменная, объявленная в заголовке: «File» указывает на заголовок, символ — на .c/.cpp.
    return (found[0] if len(found) == 1 else None), ambiguous


def base_expression(qname, file_name, address, ambiguous):
    """Форма имени, которую GDB вычисляет в адрес именно этой переменной.

    Одноимённые переменные из разных файлов (static) получают только форму с файлом: голое имя GDB
    разрешает по «текущему файлу», а он в разных сеансах GDB разный — при записи адрес был бы чужим."""
    simple = qname.split("::")[-1]
    candidates = []
    if not ambiguous:
        if "(" not in qname:
            candidates.append(qname)
        candidates.append("'%s'" % qname)
    candidates.append("'%s'::%s" % (os.path.basename(file_name), simple))
    for cand in candidates:
        if evaluates_to("&" + cand, address):
            return cand
    return None


NAME = re.compile(r"((?:\(anonymous namespace\)::|[A-Za-z_]\w*::)*[A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*$")
text = gdb.execute("info variables", to_string=True)
cur_file = None
for line in text.splitlines():
    fm = re.match(r"File (.+):\s*$", line)
    if fm:
        cur_file = norm(fm.group(1))
        continue
    if line.startswith("Non-debugging"):
        cur_file = None
        continue
    if not cur_file or not line.strip() or PATH_FILTER not in cur_file:
        continue
    decl = re.sub(r"^\s*\d+:\s*", "", line).strip().rstrip(";")
    if "(" in decl.replace("(anonymous namespace)", ""):
        continue                               # функции и указатели на функции
    match = NAME.search(decl)
    if not match:
        continue
    qname = match.group(1)
    sym, ambiguous = symbol_for(qname, cur_file)
    if sym is None:
        skip(qname, "символ не найден однозначно")
        continue
    try:
        address = int(sym.value().address)
    except (gdb.error, TypeError):
        skip(qname, "нет адреса (не в памяти)")
        continue
    base = base_expression(qname, cur_file, address, ambiguous)
    if base is None:
        skip(qname, "выражение не проверено в GDB")
        continue
    walk(base, sym.type, address, cur_file, 0)

with open(P["out"], "w", encoding="utf-8") as handle:
    json.dump({"rows": rows, "skipped": skipped}, handle)
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--elf", required=True, help="ELF прошивки с отладочной информацией (-g)")
    parser.add_argument("--path", default="src/", help="фильтр по пути исходника (подстрока), по умолчанию src/")
    parser.add_argument("--depth", type=int, default=2,
                        help="глубина раскрытия структур: 0 — только скаляры, 1 — поля, 2 — вложенные (по умолчанию)")
    parser.add_argument("--out", default="", help="CSV (по умолчанию <каталог ELF>/signals.csv)")
    args = parser.parse_args(argv)
    if args.depth < 0:
        parser.error("--depth не может быть отрицательным")

    elf = pathlib.Path(args.elf)
    if not elf.is_file():
        raise SystemExit("нет ELF %s" % elf)
    out = pathlib.Path(args.out) if args.out else elf.parent / "signals.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    gdb = find_gdb()
    rows_file = out.with_suffix(".rows.json")
    params = out.with_suffix(".params.json")
    script = out.with_suffix(".gdb.py")
    params.write_text(json.dumps({"path": args.path.replace("\\", "/"), "depth": args.depth,
                                  "out": str(rows_file)}), encoding="utf-8")
    script.write_text(GDB_SCRIPT, encoding="utf-8")
    result = subprocess.run([gdb, "-q", "-batch", "-ex", "set pagination off",
                             "-ex", "python PARAMS_FILE = %r" % str(params),
                             "-x", str(script), str(elf)], capture_output=True, text=True)
    try:
        if not rows_file.is_file():
            raise SystemExit("GDB не выдал список переменных:\n%s" % (result.stdout + result.stderr)[-800:])
        data = json.loads(rows_file.read_text(encoding="utf-8"))
    finally:
        for temp in (rows_file, params, script):
            temp.unlink(missing_ok=True)
    rows, skipped = data["rows"], data["skipped"]

    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (r["file"], int(r["address"], 16), r["expr"])):
            writer.writerow({k: row.get(k, "") for k in COLUMNS})
    print("сигналов: %d (все выражения проверены в GDB) -> %s" % (len(rows), out))
    if skipped:
        reasons = {}
        for item in skipped:
            reasons.setdefault(item["reason"], []).append(item["expr"])
        print("  пропущено %d:" % len(skipped))
        for reason, exprs in sorted(reasons.items()):
            shown = ", ".join(exprs[:4]) + (", ..." if len(exprs) > 4 else "")
            print("    %-36s %3d  %s" % (reason, len(exprs), shown))
        if any(item["reason"].startswith("структура глубже") for item in skipped):
            print("  вложенные структуры раскрывает больший --depth")
    print("  перенесите нужные строки (expr, kind, message и mask, если задан) в спецификацию полей")
    return 0


if __name__ == "__main__":
    sys.exit(main())
