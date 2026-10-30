#!/usr/bin/env python3
"""Проверка сценария gdb-py по правилам стиля: имена вместо магических чисел, восстановление, итог.

Навык stm32-gdbpy-scenario, раздел 3.5; маршруты поиска имён — references/elf-symbols.md.
Сценарий не исполняется: файл разбирается модулем ast. С --elf имена из read/write/reach/parse_and_eval
сверяются с ELF: через GDB (whatis, info scope — глобальные, статические, поля, локальные после reach)
или, без GDB, по таблице символов.

  python check_scenario.py my_scenario.py
  python check_scenario.py my_scenario.py --elf build/app.elf          # и имена по ELF
  python check_scenario.py my_scenario.py --elf build/app.elf --json-out build/style.json

Отметки разработчика (причина обязательна, отметка — в той же строке или строкой выше):
  # scenario: allow-offset: <причина>      смещение разрешено разработчиком (раскладка зафиксирована)
  # scenario: allow-address: <причина>     адрес числом разрешён (нет символа, регистр без SVD)
  # scenario: addresses-allowed: <причина>  в начале файла: сценарий без ELF, по адресам

Код возврата: 0 — ошибок нет, 1 — есть ошибки (с --strict и предупреждения), 2 — файл не разобран.
"""
import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
RANGES = (  # (начало, конец, правило, что это)
    (0x08000000, 0x1FF00000, "SC01", "адрес во Flash"),
    (0x20000000, 0x40000000, "SC01", "адрес в ОЗУ"),
    (0x40000000, 0x60000000, "SC02", "адрес регистра периферии"),
    (0x1FF00000, 0x20000000, "SC03", "адрес системной области"),
    (0xE0000000, 0x100000000, "SC03", "адрес системного регистра ядра"),
)
HINT = {
    "SC01": "обращайтесь по имени из ELF: read(\"obj.field\"), int(gdb.parse_and_eval(\"&obj.field\"))",
    "SC02": "имя регистра — из SVD или через дескриптор HAL (htim2.Instance->CNT); иначе константа с источником",
    "SC03": "вынесите в именованную константу модуля с источником в комментарии (ARMv7-M ARM, RM серии)",
}
EXPR_CALLS = {"read", "write", "reach", "parse_and_eval", "core_running"}
MARK = re.compile(r"#\s*scenario:\s*(allow-offset|allow-address|addresses-allowed)\s*:?\s*(.*)$")
HEX_IN_TEXT = re.compile(r"\b0x[0-9a-fA-F]{5,8}\b")
OFFSET_IN_TEXT = (re.compile(r"&\s*[A-Za-z_][\w.:\[\]>-]*\s*\)?\s*[+-]\s*(?:0x[0-9a-fA-F]+|\d+)"),
                  re.compile(r"\(\s*(?:const\s+|unsigned\s+)*(?:char|u?int8_t)\s*\*\s*\)"))
STAND_DATA = ((re.compile(r"\b[0-9A-Fa-f]{24}\b"), "серийник пробника"),
              (re.compile(r"\b(?!127\.|0\.0\.0\.0)(?:\d{1,3}\.){3}\d{1,3}\b"), "IP-адрес"),
              (re.compile(r"[A-Za-z]:\\\\?Users\\\\?|/home/\w+|/Users/\w+"), "личный путь"))
ADDRESS_NAME = re.compile(r"addr|address|base|adr|ptr", re.I)


class Checker(object):
    def __init__(self, path):
        self.path = Path(path)
        self.text = self.path.read_text(encoding="utf-8")
        self.lines = self.text.splitlines()
        self.tree = ast.parse(self.text, str(path))
        self.findings = []
        self.parents = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        head = "\n".join(self.lines[:40])
        found = re.search(r"#\s*scenario:\s*addresses-allowed\s*:\s*(\S.*)", head)
        self.addresses_allowed = found.group(1).strip() if found else None
        self.docstrings = set()
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and \
                        isinstance(first.value.value, str):
                    self.docstrings.add(first.value)

    def add(self, level, rule, line, message):
        self.findings.append({"level": level, "rule": rule, "line": line, "message": message})

    def mark(self, line, kind):
        """Причина из отметки разработчика в строке или строкой выше; пустая причина не считается."""
        for number in (line, line - 1):
            if 1 <= number <= len(self.lines):
                found = MARK.search(self.lines[number - 1])
                if found and found.group(1) == kind:
                    if not found.group(2).strip():
                        self.add("warning", "SC00", number, "отметка %s без причины не действует" % kind)
                        return None
                    return found.group(2).strip()
        return None

    def named_constant(self, node):
        """Литерал — значение константы модуля ИМЯ = 0x...; True и признак комментария рядом."""
        parent = self.parents.get(node)
        if isinstance(parent, ast.UnaryOp):
            parent = self.parents.get(parent)
        if isinstance(parent, ast.Assign) and isinstance(self.parents.get(parent), ast.Module) and \
                all(isinstance(t, ast.Name) and t.id.isupper() for t in parent.targets):
            line = self.lines[node.lineno - 1]
            above = self.lines[node.lineno - 2].strip() if node.lineno > 1 else ""
            return True, "#" in line or above.startswith("#")
        return False, False

    # ------------------------------------------------------------------ правила
    def check_addresses(self):
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Constant) or node in self.docstrings:
                continue
            values = []
            if isinstance(node.value, int) and not isinstance(node.value, bool):
                values = [node.value]
            elif isinstance(node.value, str):
                values = [int(v, 16) for v in HEX_IN_TEXT.findall(node.value)]
            for value in values:
                for low, high, rule, what in RANGES:
                    if low <= value < high:
                        self.address(node, value, rule, what)
                        break

    def address(self, node, value, rule, what):
        line = node.lineno
        text = "%s 0x%08X числом" % (what, value)
        if self.addresses_allowed:
            return self.add("info", rule, line, text + " (сценарий по адресам: %s)" % self.addresses_allowed)
        if self.mark(line, "allow-address"):
            return self.add("info", rule, line, text + " — разрешено разработчиком")
        named, commented = self.named_constant(node)
        if rule == "SC03" and named:
            if not commented:
                self.add("warning", rule, line, text + ": у константы нет источника в комментарии")
            return
        if rule == "SC02" and named:
            return self.add("warning", rule, line, text + " в константе: " + HINT[rule])
        self.add("error", rule, line, text + ": " + HINT[rule])

    def check_offsets(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in self.docstrings:
                if any(p.search(node.value) for p in OFFSET_IN_TEXT) and "sizeof" not in node.value:
                    self.offset(node.lineno, "смещение в выражении %r" % node.value[:60])
            elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
                sides = (node.left, node.right)
                constant = [s for s in sides if constant_int(s)]
                other = [s for s in sides if not constant_int(s)]
                if constant and other and address_like(other[0]) and not array_step(node):
                    self.offset(node.lineno, "адрес плюс число: %s" % ast.unparse(node)[:60])

    def offset(self, line, text):
        reason = self.mark(line, "allow-offset")
        if reason:
            return self.add("info", "SC04", line, text + " — разрешено разработчиком: " + reason)
        self.add("error", "SC04", line, text + ": поля и элементы — по имени (obj.field, arr[i]); смещение "
                 "допустимо для массива или упорядоченного набора (база + i * sizeof) либо с отметкой "
                 "# scenario: allow-offset: <причина>")

    def check_stand_data(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in self.docstrings:
                for pattern, what in STAND_DATA:
                    if pattern.search(node.value):
                        self.add("error", "SC05", node.lineno, "%s в сценарии: окружение — из переменных "
                                 "SCENARIO_* и отчёта environment.py, а не из файла" % what)

    def check_calls(self):
        calls = [n for n in ast.walk(self.tree) if isinstance(n, ast.Call)]
        names = [call_name(c) for c in calls]
        if "write" in names and "restore" not in names:
            self.add("error", "SC07", next(c.lineno for c in calls if call_name(c) == "write"),
                     "write() без restore(): записанное не вернётся и не будет проверено чтением")
        for call in calls:
            name = call_name(call)
            first = call.args[0] if call.args else None
            text = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else ""
            if name == "execute" and re.match(r"\s*set\s+var", text):
                self.add("warning", "SC07", call.lineno, "set var мимо write(): исходное значение не запомнится")
            if name == "sleep" and not inside_function(self, call):
                self.add("warning", "SC06", call.lineno, "ожидание по часам хоста: ждите по счётчику цели "
                         "(тики, номер кадра) или reach с условием")
            if name == "reach" and re.search(r"\.(c|cc|cpp|h|hpp|s):\d+", text):
                self.add("warning", "SC11", call.lineno, "точка по номеру строки сдвигается с правкой исходника: "
                         "лучше функция или метка")
            if name == "check" and len(call.args) > 1:
                for literal in magic_values(call.args[1]):
                    self.add("warning", "SC09", call.lineno, "ожидаемое значение %s числом: возьмите его из "
                             "enum прошивки или именованной константы с ссылкой на требование" % literal)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value is True:
                self.add("warning", "SC08", node.lineno, "while True: нужен потолок итераций (for _ in range(N))")
        if "SCENARIO_RESULT" not in self.text:
            self.add("error", "SC12", 1, "итог не пишется в SCENARIO_RESULT: сценарий не от шаблона")

    def expressions(self):
        """[(строка, функция, выражение, функции reach выше по файлу)] для сверки с ELF."""
        out, reached = [], []
        calls = sorted((n for n in ast.walk(self.tree) if isinstance(n, ast.Call)), key=lambda n: (n.lineno, n.col_offset))
        for call in calls:
            name = call_name(call)
            first = call.args[0] if call.args else None
            if name not in EXPR_CALLS or not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            text = first.value
            if "%" in text or "{" in text or re.search(r"\.(c|cc|cpp|h|hpp|s):\d+", text):
                continue
            out.append((call.lineno, name, text, list(reached)))
            if name == "reach":
                reached.append(text)
        return out


def call_name(call):
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def constant_int(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value != 0
    if isinstance(node, ast.BinOp):
        return constant_int(node.left) and constant_int(node.right)
    return False


def address_like(node):
    """Выражение похоже на адрес: имя с addr/base, вызов с parse_and_eval("&..."), read_running(...)."""
    text = ast.unparse(node)
    if isinstance(node, ast.Name):
        return bool(ADDRESS_NAME.search(node.id))
    return "parse_and_eval(" in text and "&" in text or bool(ADDRESS_NAME.search(text.split("(")[0]))


def array_step(node):
    """База + индекс * шаг: проход по массиву или упорядоченному набору."""
    text = ast.unparse(node)
    return "sizeof" in text or any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mult) and
                                   any(isinstance(s, ast.Name) for s in (n.left, n.right)) for n in ast.walk(node))


def inside_function(checker, node):
    parent = checker.parents.get(node)
    while parent is not None:
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return True
        parent = checker.parents.get(parent)
    return False


def magic_values(node):
    out = []
    for compare in (n for n in ast.walk(node) if isinstance(n, ast.Compare)):
        for side in [compare.left] + compare.comparators:
            if isinstance(side, ast.Constant) and isinstance(side.value, (int, float)) and \
                    not isinstance(side.value, bool) and side.value not in (0, 1, -1):
                out.append(repr(side.value))
    return out


# ------------------------------------------------------------------ сверка имён с ELF
GDB_PROBE = r'''
import gdb, json, os, re
items = json.load(open(os.environ["CHECK_ITEMS"], encoding="utf-8"))
out = []
for line, kind, expr, reached in items:
    entry = {"line": line, "expr": expr}
    try:
        entry["type"] = gdb.execute("whatis " + expr, to_string=True).split("=", 1)[-1].strip()
    except gdb.error as error:
        entry["error"] = str(error)
        name = re.match(r"[&*(\s]*([A-Za-z_]\w*)", expr)
        for func in reached:
            try:
                scope = gdb.execute("info scope " + func, to_string=True)
            except gdb.error:
                continue
            if name and re.search(r"Symbol %s is" % re.escape(name.group(1)), scope):
                entry["local_of"] = func
                break
    out.append(entry)
json.dump(out, open(os.environ["CHECK_OUT"], "w", encoding="utf-8"))
'''


def resolve_with_gdb(gdb, elf, items):
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        (folder / "items.json").write_text(json.dumps(items), encoding="utf-8")
        (folder / "probe.py").write_text(GDB_PROBE, encoding="utf-8")
        env = dict(__import__("os").environ, CHECK_ITEMS=str(folder / "items.json"), CHECK_OUT=str(folder / "out.json"))
        subprocess.run([gdb, "-nx", "-batch", str(elf), "-ex", "source " + (folder / "probe.py").as_posix()],
                       env=env, capture_output=True, text=True, timeout=120)
        return json.loads((folder / "out.json").read_text(encoding="utf-8"))


def mangled(parts):
    return "_ZN" + "".join("%d%s" % (len(p), p) for p in parts) + "E"


def check_names(checker, elf_path, gdb):
    items = checker.expressions()
    if not items:
        return "имён для сверки нет"
    if gdb:
        for entry, (line, kind, expr, _) in zip(resolve_with_gdb(gdb, elf_path, items), items):
            if "error" not in entry:
                continue
            if entry.get("local_of"):
                checker.add("info", "SC10", line, "%s — локальная переменная %s: доступна только в её кадре "
                            "после reach(%r)" % (expr, entry["local_of"], entry["local_of"]))
            else:
                checker.add("error", "SC10", line, "%s не разрешается по ELF: %s" % (expr, entry["error"].strip()))
        return "сверка через GDB (whatis, info scope)"
    sys.path.insert(0, str(HERE))
    import elfimage
    symbols = elfimage.Elf(elf_path).symbols()
    for line, kind, expr, _ in items:
        base = re.sub(r"\(\s*[\w\s:]+\*+\s*\)", "", expr)          # приведения (T *)
        base = re.sub(r"^[&*(\s]+", "", base)
        base = re.sub(r"^'[^']+'::", "", base)
        path = re.match(r"([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)", base)
        if not path:
            continue
        parts = path.group(1).split("::")
        # C: имя как есть; C++: _ZN<части>E (пространства имён, члены), _ZL<имя> (static в файле).
        known = parts[-1] in symbols or "_ZL%d%s" % (len(parts[-1]), parts[-1]) in symbols or \
            any(s.startswith(mangled(parts)[:-1]) for s in symbols if s.startswith("_Z"))
        if not known:
            checker.add("warning", "SC10", line, "%s нет в таблице символов: локальная, константа enum или "
                        "опечатка; точнее — с GDB (--gdb)" % parts[-1])
    return "сверка по таблице символов (GDB не найден)"


def check(path, elf=None, gdb=None):
    checker = Checker(path)
    checker.check_addresses()
    checker.check_offsets()
    checker.check_stand_data()
    checker.check_calls()
    names = check_names(checker, elf, gdb) if elf else None
    checker.findings.sort(key=lambda f: (f["line"], f["rule"]))
    return {"scenario": str(path), "names": names, "findings": checker.findings,
            "errors": sum(f["level"] == "error" for f in checker.findings),
            "warnings": sum(f["level"] == "warning" for f in checker.findings)}


LEVELS = {"error": "ОШИБКА", "warning": "внимание", "info": "сведения"}


def print_result(result, quiet=False):
    print("check_scenario: %s" % result["scenario"])
    for item in result["findings"]:
        if quiet and item["level"] == "info":
            continue
        print("  %s %s строка %d: %s" % (LEVELS[item["level"]], item["rule"], item["line"], item["message"]))
    if result["names"]:
        print("  имена: %s" % result["names"])
    print("итог: ошибок %d, предупреждений %d" % (result["errors"], result["warnings"]))


def find_gdb_quiet(explicit):
    sys.path.insert(0, str(HERE))
    try:
        from run_scenario import find_gdb
        return find_gdb(explicit)
    except SystemExit:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", nargs="+", type=Path)
    parser.add_argument("--elf", type=Path, help="сверить имена с ELF")
    parser.add_argument("--gdb", help="GDB с Python для сверки имён (иначе поиск как у run_scenario.py)")
    parser.add_argument("--no-gdb", action="store_true", help="сверять имена только по таблице символов")
    parser.add_argument("--strict", action="store_true", help="предупреждения тоже дают код 1")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    gdb = None if args.no_gdb or not args.elf else find_gdb_quiet(args.gdb)
    results = []
    try:
        for path in args.scenario:
            results.append(check(path, args.elf, gdb))
            print_result(results[-1])
    except (OSError, SyntaxError) as error:
        print("check_scenario: файл не разобран: %s" % error)
        return 2
    if args.json_out:
        args.json_out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    bad = sum(r["errors"] + (r["warnings"] if args.strict else 0) for r in results)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
