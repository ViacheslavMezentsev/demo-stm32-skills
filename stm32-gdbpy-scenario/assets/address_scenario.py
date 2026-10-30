"""Пример по шаблону без ELF: сценарий по адресам для любой платы с Cortex-M.

Сверяет DEV_ID (SCENARIO_DEV_ID, адрес — SCENARIO_IDCODE), проверяет остановку ядра при
подключении GDB, сброс с остановкой на векторе сброса (SCENARIO_FLASH — начало Flash)
и шаг stepi. После сценария плата продолжает работу с начала прошивки.
"""
# scenario: addresses-allowed: пример без ELF — символов нет, только адреса и регистры
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


def monitor(command):
    # Команда GDB-сервера (monitor); после неё кэш регистров GDB устарел.
    text = gdb.execute("monitor " + command, to_string=True)
    gdb.execute("maintenance flush register-cache")
    return text


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


# Регистр ядра: адрес задан архитектурой (ARMv7-M ARM, C1.6.2 DHCSR).
DHCSR = 0xE000EDF0
DHCSR_S_HALT = 1 << 17
# Таблица векторов — упорядоченный набор: слово 0 — начальный SP, слово 1 — вектор сброса.
VECTOR_SP, VECTOR_RESET = 0, 1
WORD = 4

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
    # Без ELF: только адреса и регистры, один сценарий для любой платы с Cortex-M.
    flash = int(os.environ.get("SCENARIO_FLASH", "0x08000000"), 0)
    idcode = int(os.environ.get("SCENARIO_IDCODE", "0xE0042000"), 0)
    expected = os.environ.get("SCENARIO_DEV_ID")

    step("identity")
    dev_id = read("*(unsigned int *)%d" % idcode) & 0xFFF
    record("dev_id", "0x%03X" % dev_id)
    if expected:
        check("DEV_ID %s" % expected, dev_id == int(expected, 0))

    step("attach")
    check("core halted after attach", read("*(unsigned int *)%d" % DHCSR) & DHCSR_S_HALT)

    step("reset halt")
    monitor(reset_halt)
    pc = read("$pc")
    check("PC at reset vector", pc == read("*(unsigned int *)%d" % (flash + VECTOR_RESET * WORD)) & ~1)
    check("SP from vector table", read("$sp") == read("*(unsigned int *)%d" % (flash + VECTOR_SP * WORD)))

    step("stepi")
    gdb.execute("stepi 3")
    check("PC moved after stepi", read("$pc") != pc)
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
