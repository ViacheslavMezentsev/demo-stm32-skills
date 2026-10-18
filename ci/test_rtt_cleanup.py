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
          "rcc": {"enable_reg": 5, "enable_bit": 2}, "tick_address": 6}
SAVED = {"vtor": 0x08000000, "rcc": 1}
MASK = 1 << 29


class Target:
    def __init__(self, steps=3, pc=0x20001000, ignore=None):
        self.memory = {1: 1, 2: 1, 3: 1, 4: 0x20002000, 5: 0x103,
                       0x100: MASK, 0x200: MASK, 0x300: MASK if steps else 0,
                       6: 10, 0xE000ED04: 0, 0xE000EDF0: 1 << 17}
        self.steps = steps
        self.address = pc
        self.ignore = ignore
        self.detached = False
        self.writes = []
        self.quiesced = False
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
                assert self.quiesced, "VTOR restored before live quiescence"
            self.memory[address] = value
    def pc(self):
        return self.address
    def step(self):
        assert self.memory[4] == 0x20002000, "handler must retain SRAM vector"
        self.steps -= 1
        if self.steps == 0:
            self.memory[0x300] = 0
    def run_window(self):
        assert self.memory[4] == 0x20002000, "late IRQ must retain SRAM vector"
        assert self.memory[1] == self.memory[2] == self.memory[3] == 0
        assert self.memory[0x100] == self.memory[0x200] == 0
        self.quiesced = True
        self.memory[6] += 250
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

    def test_late_irq_after_resume_is_drained_before_restore(self):
        class LateIRQ(Target):
            def run_window(self):
                super().run_window()
                self.memory[0x300] = MASK
                self.steps = 2
        target, report = LateIRQ(steps=0), {}
        cleanup(target, LAYOUT, SAVED, report)
        self.assertEqual(report["active_before"], 0)
        self.assertEqual(report["active_after_quiesce"], MASK)
        self.assertEqual(report["steps"], 2)
        self.assertTrue(target.detached)

    def test_failed_quiescence_preserves_vector(self):
        for fault in ("tick", "handler", "halt"):
            class FailedWindow(Target):
                def run_window(self):
                    super().run_window()
                    if fault == "tick":
                        self.memory[6] -= 250
                    elif fault == "handler":
                        self.memory[0x300] = MASK
                        self.address = 0x08000878
                    else:
                        self.memory[0xE000EDF0] = 0
            target = FailedWindow(steps=0)
            with self.subTest(fault=fault), self.assertRaises(RuntimeError):
                cleanup(target, LAYOUT, SAVED, {})
            self.assertNotIn(4, target.writes)
            self.assertFalse(target.detached)


class HostChecks(unittest.TestCase):
    def test_post_resume_stuck_tick_or_active_irq_is_failure(self):
        import rtt_timer as rt
        for stuck, active, ignore_resume, expected in (
                (False, False, False, True), (True, False, False, False),
                (False, True, False, False), (False, False, True, False)):
            class Telnet:
                tick_reads = 0
                halted = True  # OpenOCD может оставить ядро остановленным после detach
                commands = None
                def __init__(self):
                    self.commands = []
                def command(self, text):
                    self.commands.append(text)
                    if text == "resume" and not ignore_resume:
                        self.halted = False
                    if text == "halt":
                        self.halted = True
                    if not text.startswith("mdw"):
                        return ""
                    address = int(text.split()[1], 16)
                    if address == 6:
                        self.tick_reads += 1
                        value = 20 if stuck or self.halted else 20 + self.tick_reads
                    else:
                        value = {0x300: MASK if active else 0, 0x100: 0, 0x200: 0,
                                 4: SAVED["vtor"], 0xE000EDF0: (1 << 17) if self.halted else 0}.get(address, 0)
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
                    telnet = Telnet()
                    report = rt.teardown(telnet, layout, SAVED, args, out, "gdb")
                self.assertEqual(report["ok"], expected)
                self.assertTrue(report["resume_requested"])
                self.assertEqual(report["dhcsr_before_resume"], 1 << 17)
                self.assertEqual(telnet.commands.count("resume"), 1)
                self.assertEqual(telnet.halted, not expected)
                summary = json.loads(out.with_suffix(".rtt.json").read_text())
                self.assertEqual(summary["teardown"]["ok"], expected)

    def test_failed_gdb_never_resumes(self):
        import rtt_timer as rt
        for code, ok, restored in ((1, False, False), (0, False, True), (0, True, False)):
            commands = []
            telnet = SimpleNamespace(command=lambda text: commands.append(text))
            with tempfile.TemporaryDirectory() as directory:
                out = pathlib.Path(directory) / "record.mcap"
                def run_gdb(*args, **kwargs):
                    out.with_suffix(".teardown.json").write_text(json.dumps(
                        {"ok": ok, "registers_restored": restored}))
                    return SimpleNamespace(returncode=code)
                args = SimpleNamespace(host="127.0.0.1", cleanup_gdb_port=3334)
                with patch.object(rt.subprocess, "run", side_effect=run_gdb):
                    report = rt.teardown(telnet, LAYOUT, SAVED, args, out, "gdb")
                self.assertFalse(report["ok"])
                self.assertNotIn("resume", commands)
                self.assertIn("halt", commands)


if __name__ == "__main__":
    unittest.main()
