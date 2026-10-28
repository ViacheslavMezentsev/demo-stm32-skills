"""Шаблон сценария gdb-py: один прогон — одно действие — проверяемый итог.

Исполняется внутри GDB с Python (`source`), обычно через scripts/run_scenario.py, который
задаёт SCENARIO_GDB_HOST/PORT, SCENARIO_RESULT и SCENARIO_ENVIRONMENT. Копируйте файл и
меняйте только раздел «Сценарий»; вспомогательные функции не трогайте без причины.
"""
import json
import os
import traceback

import gdb

checks = {}
records = []
originals = {}


def step(name):
    # Метка шага в журнале: при TIMEOUT запускатель покажет, где встали.
    print("STEP " + name, flush=True)


def check(name, value):
    checks[name] = bool(value)
    print(("PASS " if value else "FAIL ") + name, flush=True)


def record(name, data):
    records.append({"name": name, "data": data})


def read(expr):
    return int(gdb.parse_and_eval(expr))


def write(expr, value):
    # Исходное значение запоминается один раз и возвращается в finish().
    if expr not in originals:
        originals[expr] = read(expr)
    gdb.execute("set var %s = %d" % (expr, value))


def reach(symbol, condition=None):
    # Временная точка перед continue; условие — атрибутом, не аргументом конструктора.
    point = gdb.Breakpoint(symbol, internal=True, temporary=True)
    if condition:
        point.condition = condition
    gdb.execute("continue")
    frame = gdb.newest_frame().name() or ""
    return frame.removesuffix("()") == symbol.removesuffix("()")


def restore():
    # Возврат записанного и проверка чтением: без неё восстановление не доказано.
    for expr, value in originals.items():
        gdb.execute("set var %s = %d" % (expr, value))
    return all(read(expr) == value for expr, value in originals.items())


environment = os.environ.get("SCENARIO_ENVIRONMENT", "hardware")
status, restored = "ERROR", None
try:
    gdb.execute("set pagination off")
    gdb.execute("set confirm off")
    gdb.execute("set remotetimeout 15")
    gdb.execute("target remote %s:%s" % (os.environ.get("SCENARIO_GDB_HOST", "127.0.0.1"),
                                         os.environ["SCENARIO_GDB_PORT"]))

    # ---------------------------------------------------------------- Сценарий
    step("main")
    check("main reached", reach("main"))
    # Пример: исходное состояние, действие, наблюдаемый результат, соседний признак.
    # before = read("g_app.counter")
    # write("g_app.mode", 1)
    # check("handler reached", reach("app_handle_mode"))
    # check("counter changed", read("g_app.counter") != before)
    # check("error flag untouched", read("g_app.error") == 0)
    # ------------------------------------------------------------------------

    status = "PASS" if checks and all(checks.values()) else "FAIL"
except Exception:
    traceback.print_exc()
    status = "ERROR"
finally:
    try:
        restored = restore() if originals else None
    except Exception:
        traceback.print_exc()
        restored = False
    if restored is False:
        status = "ERROR"
    with open(os.environ["SCENARIO_RESULT"], "w", encoding="utf-8") as stream:
        json.dump({"environment": environment, "status": status, "checks": checks,
                   "records": records, "restored": restored}, stream, ensure_ascii=False, indent=2)
    if environment == "hardware":
        try:
            # Плату оставляем работающей: detach при остановленном ядре оставит её в halt.
            gdb.execute("continue &")
            gdb.execute("detach")
        except Exception:
            traceback.print_exc()
    gdb.execute("quit " + ("0" if status == "PASS" else "1"))
