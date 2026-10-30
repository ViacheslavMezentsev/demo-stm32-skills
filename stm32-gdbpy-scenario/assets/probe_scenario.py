"""Пример по шаблону: прошивка-зонд stm32-skills-probe, действие с отложенным эффектом.

Период кадров probe_frame_period_ms меняется со 100 на 20 мс. Прошивка уже запланировала
следующий кадр по старому периоду, поэтому первый интервал после записи остаётся 100 мс,
и только следующий — 20 мс: запись переменной не равна её потреблению. Затем период
возвращается и проверяется чтением. Сборка с PROBE_EXIT_MS=500 укладывается: последний
нужный кадр приходит примерно на 440 мс.
"""
import json
import os
import traceback

import gdb

checks = {}
records = []
originals = {}
breakpoints = []  # точки сценария: снимаются в finally, даже если сценарий прервался


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
    breakpoints.append(point)
    gdb.execute("continue")
    frame = gdb.newest_frame().name() or ""
    return plain(frame) == plain(symbol)


def plain(name):
    # Без str.removesuffix (3.9): GDB на узле бывает собран с Python 3.8.
    return name[:-2] if name.endswith("()") else name


def restore():
    # Возврат записанного и проверка чтением: без неё восстановление не доказано.
    for expr, value in originals.items():
        gdb.execute("set var %s = %d" % (expr, value))
    return all(read(expr) == value for expr, value in originals.items())


# Ожидаемые значения — из описания зонда, а не из ответа платы (stm32-skills-probe, src/probe.h).
PROBE_STATE_READY = 0xA5    # probe_state после инициализации
PERIOD_DEFAULT_MS = 100     # период кадров по умолчанию
PERIOD_FAST_MS = 20         # новый период, который задаёт сценарий

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
    check("probe_state not set before init", read("probe_state") != PROBE_STATE_READY)

    step("loop")
    check("probe_loop reached", reach("probe_loop"))
    check("probe_state set by init", read("probe_state") == PROBE_STATE_READY)
    work = read("probe_work_iterations")

    # Вход в кадр k+1 читает время кадра k: probe_telemetry.tick пишется внутри функции.
    def frame_tick():
        check_name = "probe_emit_frame reached (%d)" % (len(records) + 1)
        check(check_name, reach("probe_emit_frame"))
        value = read("probe_telemetry.tick")
        record("frame_tick", value)
        return value

    step("baseline")
    reach("probe_emit_frame")
    first, second = frame_tick(), frame_tick()
    check("baseline period 100 ms", second - first == PERIOD_DEFAULT_MS)

    step("action")
    write("probe_frame_period_ms", PERIOD_FAST_MS)
    third, fourth, fifth = frame_tick(), frame_tick(), frame_tick()
    check("frame already scheduled keeps 100 ms", fourth - third == PERIOD_DEFAULT_MS)
    check("next frame uses 20 ms", fifth - fourth == PERIOD_FAST_MS)
    check("work iterations untouched", read("probe_work_iterations") == work)
    check("probe_state untouched", read("probe_state") == PROBE_STATE_READY)
    # ------------------------------------------------------------------------

    status = "PASS" if checks and all(checks.values()) else "FAIL"
except Exception:
    traceback.print_exc()
    status = "ERROR"
finally:
    for point in breakpoints:
        if point.is_valid():
            point.delete()
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
            # OpenOCD: monitor resume и detach; J-Link GDB Server: monitor go и disconnect.
            for command in os.environ.get("SCENARIO_FINISH", "monitor resume|detach").split("|"):
                gdb.execute(command)
        except Exception:
            traceback.print_exc()
    gdb.execute("quit " + ("0" if status == "PASS" else "1"))
