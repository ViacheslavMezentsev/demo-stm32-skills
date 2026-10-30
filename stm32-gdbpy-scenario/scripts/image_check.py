"""Сверка образа во Flash с ELF до сценария; исполняется внутри GDB с Python (source).

Запускается из run_scenario.py отдельным сеансом GDB, до сценария. Читает память цели через
GDB-сервер (пакеты m) и сравнивает с секциями ELF в области кода. Алгоритм контрольной суммы
на цели не запускается (verify_image и compare-sections на Cortex-M исполняют код в ОЗУ цели и
портят состояние работающей прошивки), поэтому ОЗУ не затрагивается; ядро остановлено только
на время чтения — подключение GDB и так его останавливает.

Переменные: IMAGE_ELF, IMAGE_MODE (full | build-id), IMAGE_HELPERS (каталог elfimage.py),
IMAGE_RESULT (JSON итога), а также SCENARIO_GDB_HOST/PORT, SCENARIO_SETUP, SCENARIO_FINISH.
"""
import json
import os
import sys
import time
import traceback

import gdb

sys.path.insert(0, os.environ["IMAGE_HELPERS"])
import elfimage  # noqa: E402

result = {"status": "unknown", "mode": os.environ.get("IMAGE_MODE", "full")}
started = time.monotonic()
connected = False
try:
    gdb.execute("set pagination off")
    gdb.execute("set confirm off")
    gdb.execute("set remotetimeout 15")
    # Читать цель, а не файл ELF, даже для секций только для чтения.
    gdb.execute("set trust-readonly-sections off")
    gdb.execute("target remote %s:%s" % (os.environ.get("SCENARIO_GDB_HOST", "127.0.0.1"),
                                         os.environ["SCENARIO_GDB_PORT"]))
    connected = True
    for command in filter(None, os.environ.get("SCENARIO_SETUP", "").split("|")):
        gdb.execute(command)
    elf = elfimage.Elf(os.environ["IMAGE_ELF"])
    inferior = gdb.selected_inferior()

    def read(address, length):
        return inferior.read_memory(address, length).tobytes()

    identity = elf.build_id() if result["mode"] == "build-id" else None
    if identity:
        address, expected = identity
        actual = read(address, len(expected))
        result.update(method="build-id", build_id=expected.hex(), checked_bytes=len(expected),
                      status="match" if actual == expected else "mismatch",
                      first_diff=None if actual == expected else
                      {"section": ".note.gnu.build-id", "address": "0x%08X" % address,
                       "expected": expected.hex(), "actual": actual.hex()})
    else:
        if result["mode"] == "build-id":
            result["note"] = "в ELF нет build-id в области кода: сверка всех секций"
        result.update(elfimage.compare(read, elf.flash_sections()), method="sections")
except Exception as error:
    traceback.print_exc()
    result.update(status="unknown", error="%s: %s" % (type(error).__name__, str(error).strip()[:300]))
finally:
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    with open(os.environ["IMAGE_RESULT"], "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    if connected:
        try:
            # Как в шаблоне сценария: плата продолжает работу после отключения.
            for command in os.environ.get("SCENARIO_FINISH", "continue &|detach").split("|"):
                gdb.execute(command)
        except Exception:
            traceback.print_exc()
    gdb.execute("quit 0")
