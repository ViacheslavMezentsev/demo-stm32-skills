"""Проверяемое завершение RTT через GDB; алгоритм тестируется без платы."""
import json
import pathlib
import struct


def cleanup(target, layout, saved, report, max_steps=512):
    timer, nvic = layout["timer"], layout["nvic"]
    mask = 1 << nvic["bit"]
    active = nvic["iser"] + 0x200
    def check(address, expected, bits=0xFFFFFFFF):
        value = target.read(address)
        if value & bits != expected & bits:
            raise RuntimeError("readback 0x%08X: 0x%08X, expected 0x%08X mask 0x%08X" %
                               (address, value, expected, bits))
        return value
    report["pc_before"] = target.pc()
    report["stage"] = "disable_source"
    # Ядро остановлено GDB. Остановить и источник, и доставку, прежде чем менять VTOR.
    for address, value in ((timer["cr1"], 0), (timer["dier"], 0), (timer["sr"], 0),
                           (nvic["icer"], mask), (nvic["icpr"], mask)):
        target.write(address, value)
    check(timer["cr1"], 0, timer["bit_cen"])
    check(timer["dier"], 0, timer["bit_uie"])
    check(timer["sr"], 0, 1)
    check(nvic["iser"], 0, mask)
    check(nvic["ispr"], 0, mask)
    report["stage"] = "drain_handler"
    report["steps"] = 0
    report["active_before"] = target.read(active)
    while target.read(active) & mask:
        pc = target.pc() & ~1
        report["last_pc"] = pc
        # Не пытаться выводить прошивку из Default_Handler или неизвестного вложенного ISR.
        if not layout["stub"] <= pc < layout["params"]:
            raise RuntimeError("active timer outside RTT stub at 0x%08X; reset required" % pc)
        if report["steps"] >= max_steps:
            raise RuntimeError("RTT handler did not exit within instruction budget")
        target.step()
        report["steps"] += 1
    report["active_after"] = check(active, 0, mask)
    check(nvic["ispr"], 0, mask)
    # Повторная проверка после step: неизвестная команда/помеха не должна дать ложный успех.
    check(nvic["iser"], 0, mask)
    report["stage"] = "restore"
    target.write(layout["vtor"], saved["vtor"])
    report["vtor"] = check(layout["vtor"], saved["vtor"])
    # Меняем только принадлежащий нам бит, не откатываем изменения других периферий прошивкой.
    reg, bit = layout["rcc"]["enable_reg"], layout["rcc"]["enable_bit"]
    restored = (target.read(reg) & ~bit) | (saved["rcc"] & bit)
    target.write(reg, restored)
    report["rcc"] = check(reg, restored)
    check(active, 0, mask)
    report["stage"] = "detach"
    target.detach()  # штатное возобновление; ни reset, ни return, ни запись PC
    report["registers_restored"] = True


def gdb_main(config_path):
    import gdb
    cfg = json.loads(pathlib.Path(config_path).read_text(encoding="utf-8"))
    report = {"ok": False, "registers_restored": False, "stage": "connect"}
    class Target:
        def read(self, address):
            return struct.unpack("<I", gdb.selected_inferior().read_memory(address, 4).tobytes())[0]
        def write(self, address, value):
            gdb.selected_inferior().write_memory(address, struct.pack("<I", value & 0xFFFFFFFF))
        def pc(self):
            return int(gdb.parse_and_eval("$pc"))
        def step(self):
            gdb.execute("stepi", to_string=True)
        def detach(self):
            gdb.execute("detach", to_string=True)
    try:
        gdb.execute("set pagination off")
        gdb.execute("set confirm off")
        gdb.execute("set remotetimeout 5")
        gdb.execute("set mem inaccessible-by-default off")
        gdb.execute("target extended-remote " + cfg["endpoint"], to_string=True)
        gdb.execute("monitor halt", to_string=True)
        cleanup(Target(), cfg["layout"], cfg["saved"], report)
        report["ok"] = True
    except Exception as error:
        report["error"] = str(error)
        try:
            gdb.execute("monitor halt", to_string=True)
            gdb.execute("disconnect", to_string=True)
        except Exception:
            pass
    finally:
        pathlib.Path(cfg["result"]).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    gdb.execute("quit " + ("0" if report["ok"] else "1"))
