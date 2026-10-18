"""State-machine tests of GDB cleanup: no hardware required."""
import pathlib
import sys
import unittest
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "stm32-diag-record/scripts/rtt"))
from rtt_cleanup import cleanup

LAYOUT = {"timer": {"cr1": 1, "dier": 2, "sr": 3, "bit_cen": 1, "bit_uie": 1},
          "nvic": {"iser": 0x100, "icer": 0x180, "ispr": 0x200, "icpr": 0x280, "bit": 29},
          "stub": 0x20001000, "params": 0x20001100, "vtor": 4,
          "rcc": {"enable_reg": 5, "enable_bit": 2}}
SAVED = {"vtor": 0x08000000, "rcc": 1}
MASK = 1 << 29


class Target:
    def __init__(self, steps=3, pc=0x20001000, ignore=None):
        self.memory = {1: 1, 2: 1, 3: 1, 4: 0x20002000, 5: 0x103,
                       0x100: MASK, 0x200: MASK, 0x300: MASK if steps else 0}
        self.steps = steps
        self.address = pc
        self.ignore = ignore
        self.detached = False
        self.writes = []
    def read(self, address):
        return self.memory[address]
    def write(self, address, value):
        self.writes.append(address)
        if address == self.ignore:
            return
        if address == 0x180:
            self.memory[0x100] &= ~value
        elif address == 0x280:
            self.memory[0x200] &= ~value
        else:
            if address == 4:
                assert self.memory[0x300] == 0, "VTOR restored with active exception"
            self.memory[address] = value
    def pc(self):
        return self.address
    def step(self):
        assert self.memory[4] == 0x20002000, "handler must retain SRAM vector"
        self.steps -= 1
        if self.steps == 0:
            self.memory[0x300] = 0
    def detach(self):
        self.detached = True


class CleanupTests(unittest.TestCase):
    def test_active_and_thread_mode(self):
        for steps in (0, 3):
            target, report = Target(steps=steps), {}
            cleanup(target, LAYOUT, SAVED, report)
            self.assertEqual(report["steps"], steps)
            self.assertTrue(target.detached)
            self.assertTrue(report["registers_restored"])
            self.assertEqual(target.memory[5], 0x101)  # preserve new unrelated RCC bit
            self.assertEqual(target.memory[4], SAVED["vtor"])
    def test_default_handler_is_not_forced_to_return(self):
        target = Target(pc=0x08000878)
        with self.assertRaisesRegex(RuntimeError, "outside RTT"):
            cleanup(target, LAYOUT, SAVED, {})
        self.assertNotIn(4, target.writes)
        self.assertFalse(target.detached)
    def test_bounded_wait(self):
        target = Target(steps=100)
        with self.assertRaisesRegex(RuntimeError, "instruction budget"):
            cleanup(target, LAYOUT, SAVED, {}, max_steps=4)
        self.assertNotIn(4, target.writes)
        self.assertFalse(target.detached)
    def test_ignored_register_writes_fail_closed(self):
        for address in (1, 2, 3, 0x180, 0x280, 4, 5):
            target = Target(steps=0, ignore=address)
            with self.subTest(address=address), self.assertRaisesRegex(RuntimeError, "readback"):
                cleanup(target, LAYOUT, SAVED, {})
            self.assertFalse(target.detached)


class HostChecks(unittest.TestCase):
    def test_post_resume_stuck_tick_or_active_irq_is_failure(self):
        import rtt_timer as rt
        for stuck, active, expected in ((False, False, True), (True, False, False), (False, True, False)):
            class Telnet:
                tick_reads = 0
                def command(self, text):
                    if not text.startswith("mdw"):
                        return ""
                    address = int(text.split()[1], 16)
                    if address == 6:
                        self.tick_reads += 1
                        value = 20 if stuck else 20 + self.tick_reads
                    else:
                        value = {0x300: MASK if active else 0, 0x100: 0, 0x200: 0,
                                 4: SAVED["vtor"], 0xE000EDF0: 0}.get(address, 0)
                    return "0x%08x: %08x" % (address, value)
            with tempfile.TemporaryDirectory() as directory:
                out = pathlib.Path(directory) / "record.mcap"
                result = out.with_suffix(".teardown.json")
                def run_gdb(*args, **kwargs):
                    result.write_text(json.dumps({"ok": True, "registers_restored": True}))
                    return SimpleNamespace(returncode=0)
                layout = dict(LAYOUT, tick_address=6)
                args = SimpleNamespace(host="127.0.0.1", cleanup_gdb_port=3334)
                with patch.object(rt.subprocess, "run", side_effect=run_gdb), patch.object(rt.time, "sleep"):
                    report = rt.teardown(Telnet(), layout, SAVED, args, out, "gdb")
                self.assertEqual(report["ok"], expected)
                summary = json.loads(out.with_suffix(".rtt.json").read_text())
                self.assertEqual(summary["teardown"]["ok"], expected)


if __name__ == "__main__":
    unittest.main()
