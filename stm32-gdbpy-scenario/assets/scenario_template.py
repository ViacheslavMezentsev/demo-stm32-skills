"""Шаблон сценария gdb-py: один прогон — одно действие — проверяемый итог.

Исполняется внутри GDB с Python (`source`), обычно через scripts/run_scenario.py. Копируйте файл
и меняйте только раздел «Сценарий»; вспомогательные функции не трогайте без причины.

Переменные запускателя (полная таблица — SKILL.md, раздел 3.3):
  SCENARIO_GDB_HOST, SCENARIO_GDB_PORT  куда подключаться (target remote)
  SCENARIO_RESULT                       файл итога JSON (обязателен)
  SCENARIO_ENVIRONMENT                  emulator | hardware; записать в итог то же значение
  SCENARIO_SERVER                       openocd | jlink | renode | attach
  SCENARIO_RESET_HALT                   сброс с остановкой на языке сервера
  SCENARIO_SETUP, SCENARIO_FINISH       команды GDB после подключения и на выходе, через «|»
"""
import contextlib
import json
import os
import re
import time
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


def monitor(command):
    # Команда GDB-сервера (monitor); после неё кэш регистров GDB устарел.
    text = gdb.execute("monitor " + command, to_string=True)
    gdb.execute("maintenance flush register-cache")
    return text


# Регистр ядра, а не объект прошивки: адрес задан архитектурой (ARMv7-M ARM, C1.6.2 DHCSR).
DHCSR = 0xE000EDF0
DHCSR_S_HALT = 1 << 17

SERVER = os.environ.get("SCENARIO_SERVER", "openocd")
# Команды окна работы на языке сервера: OpenOCD — resume/halt/mdw, J-Link GDB Server — go/halt/memU32.
WINDOW = {"openocd": ("resume", "halt", "mdw 0x%08X"), "jlink": ("go", "halt", "memU32 0x%08X")}


@contextlib.contextmanager
def running_window():
    """Ядро работает внутри блока, GDB по-прежнему считает цель остановленной.

    Подключение GDB останавливает цель. Внутри окна допустимы только read_running() и monitor():
    чтения GDB (read, reach, parse_and_eval) — после выхода, когда ядро снова остановлено.
    Пара monitor resume/halt не рассинхронизирует GDB: он всё время видит остановленную цель.
    """
    if SERVER not in WINDOW:
        raise RuntimeError("окно работы без остановки нужно OpenOCD или J-Link GDB Server, а сервер: " + SERVER)
    monitor(WINDOW[SERVER][0])
    try:
        yield
    finally:
        monitor(WINDOW[SERVER][1])


def read_running(address):
    """32-битное слово по адресу чтением по SWD (на работающем ядре — внутри running_window)."""
    text = gdb.execute("monitor " + WINDOW[SERVER][2] % address, to_string=True)
    # OpenOCD: «0x20000000: 0000abcd», J-Link: «... (Data = 0x0000ABCD)»; значение — последнее слово.
    return int(re.findall(r"\b(?:0x)?([0-9a-fA-F]{8})\b", text)[-1], 16)


def core_running(counter_address, pause=0.2):
    """Ядро работает: счётчик (например, &uwTick) растёт, бит S_HALT регистра DHCSR сброшен.

    Окно работы открывается и закрывается внутри: после вызова цель снова остановлена для GDB.
    """
    with running_window():
        first = read_running(counter_address)
        time.sleep(pause)
        second = read_running(counter_address)
        dhcsr = read_running(DHCSR)
    record("core_running", {"counter": [first, second], "dhcsr": "0x%08X" % dhcsr})
    return second != first and not dhcsr & DHCSR_S_HALT


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
# Сброс с остановкой на языке сервера: OpenOCD — reset halt, J-Link GDB Server — reset.
reset_halt = os.environ.get("SCENARIO_RESET_HALT", "reset halt")
status, restored = "ERROR", None
try:
    gdb.execute("set pagination off")
    gdb.execute("set confirm off")
    gdb.execute("set remotetimeout 15")
    gdb.execute("target remote %s:%s" % (os.environ.get("SCENARIO_GDB_HOST", "127.0.0.1"),
                                         os.environ["SCENARIO_GDB_PORT"]))
    # Подготовка сервера от запускателя (J-Link: monitor flash breakpoints = 0 — только аппаратные точки).
    for command in filter(None, os.environ.get("SCENARIO_SETUP", "").split("|")):
        gdb.execute(command)

    # ---------------------------------------------------------------- Сценарий
    if environment == "hardware":
        # На плате GDB подключается к работающей прошивке: main уже пройден, начинаем со сброса.
        monitor(reset_halt)
    step("main")
    check("main reached", reach("main"))
    # Пример: исходное состояние, действие, наблюдаемый результат, соседний признак.
    # Имена — из ELF (references/elf-symbols.md), ожидаемые числа — из enum прошивки или констант
    # с ссылкой на требование; проверка стиля — scripts/check_scenario.py.
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
            # OpenOCD: continue & и detach; J-Link GDB Server: monitor go и disconnect.
            for command in os.environ.get("SCENARIO_FINISH", "continue &|detach").split("|"):
                gdb.execute(command)
        except Exception:
            traceback.print_exc()
    gdb.execute("quit " + ("0" if status == "PASS" else "1"))
