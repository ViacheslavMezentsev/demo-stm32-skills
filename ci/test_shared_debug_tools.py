"""Both standalone skill copies and ROS consumer against an actual fake TCP server."""
import importlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL = os.environ.get("SHARED_TEST_SKILL", "stm32-ros-alt-channel")
sys.path.insert(0, str(ROOT / SKILL / "scripts"))
from fake_openocd import FakeOpenOcd
from openocd_link import Telnet, mdw, OpenOcdReader
from field_values import convert, aligned_needed, word_at
import gdb_tools
import process_tools


def ros_module():
    # Only middleware is replaced. The real telemetry main, socket and decoding run unchanged.
    class Message:
        def __init__(self, data):
            self.data = data
    sys.modules["rospy"] = types.ModuleType("rospy")
    sys.modules["std_msgs"] = types.ModuleType("std_msgs")
    sys.modules["std_msgs.msg"] = types.SimpleNamespace(Float32=Message, Int32=Message, UInt32=Message)
    sys.path.insert(0, str(ROOT / "stm32-ros-alt-channel/scripts"))
    return importlib.import_module("telemetry_node")


class SharedTests(unittest.TestCase):
    def test_transport_noise_and_unaligned_fields(self):
        memory = {0x100: lambda n: 0xFFFF1234, 0x104: lambda n: 0x5678}
        with FakeOpenOcd(memory, noise={0: "before", 1: "midecho"}) as server:
            reader = OpenOcdReader("127.0.0.1", server.port)
            try:
                self.assertEqual(mdw(reader.telnet, 0x100), 0xFFFF1234)
                word = word_at(reader.read_words(aligned_needed(0x102)), 0x102)
                self.assertEqual(word, 0x5678FFFF)
                self.assertEqual(convert(word, "int32", mask=0xFFFF), -1)
                self.assertEqual(convert(word, "uint32", 16, 0xFFFF0000), 0x5678)
                self.assertIsNone(word_at({0x100: 1}, 0x102))
            finally:
                reader.close()

    def test_ros_publishes_masked_unaligned_and_shifted_fields(self):
        module = ros_module()
        fields = [dict(topic="/signed", address=0x102, kind="int32", message="std_msgs/Int32", mask=65535),
                  dict(topic="/shift", address=0x100, kind="uint32", message="std_msgs/UInt32", mask=0xFF00, shift=8)]
        for field in fields:
            field["expr"] = "test"
        published = {}
        ros = module.rospy
        ros.init_node = ros.loginfo = lambda *a, **k: None
        ros.Publisher = lambda topic, *a, **k: types.SimpleNamespace(publish=lambda msg: published.setdefault(topic, []).append(msg.data))
        ros.Rate = lambda *a: types.SimpleNamespace(sleep=lambda: None)
        ros.is_shutdown = iter([False, True]).__next__
        with tempfile.TemporaryDirectory() as tmp, FakeOpenOcd({0x100: lambda n: 0xFFFF1234, 0x104: lambda n: 0x5678}, noise={0: "before", 1: "midecho"}) as server:
            spec = pathlib.Path(tmp) / "spec.json"
            spec.write_text(json.dumps({"fields": fields}))
            original = module.OpenOcdLink
            with patch.object(sys, "argv", ["telemetry_node.py", str(spec)]), patch.object(module, "OpenOcdLink", side_effect=lambda: original(port=server.port)):
                self.assertEqual(module.main(), 0)
        self.assertEqual(published, {"/signed": [-1], "/shift": [0x12]})

    def test_gdb_explicit_is_checked_without_fallback(self):
        with patch.object(gdb_tools, "gdb_has_python", return_value=False), patch.dict(os.environ, {"GDB": "wrong"}):
            with self.assertRaises(SystemExit):
                gdb_tools.find_gdb()
        with patch.object(gdb_tools, "gdb_has_python", return_value=True):
            self.assertEqual(gdb_tools.find_gdb(explicit="chosen"), "chosen")
        with patch.object(gdb_tools.subprocess, "run", side_effect=subprocess.TimeoutExpired("gdb", 15)):
            self.assertFalse(gdb_tools.gdb_has_python("slow"))

    def test_own_process_termination_escalates(self):
        from unittest.mock import Mock
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("child", 5), 0]
        process_tools.stop_process(child)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        process_tools.stop_process(None)

    def test_swd_prefix_preserves_masks(self):
        ros_module()
        import ros1_channel as channel
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "fields.toml").write_text('[[field]]\ntopic="/x"\nmessage="std_msgs/UInt32"\nkind="uint32"\nexpr="&x"\nmask=65280\nshift=8\n')
            with patch.object(channel, "ROOT", root), patch.object(channel, "ROS_ALT", root):
                result = channel.prepare_swd_spec(types.SimpleNamespace(elf="build/test.elf"))
            import tomllib
            field = tomllib.loads(result.read_text())["field"][0]
            self.assertEqual((field["topic"], field["mask"], field["shift"]), ("/swd/x", 65280, 8))

    def test_ros_cleanup_preserves_borrowed_openocd(self):
        from unittest.mock import Mock
        ros_module()
        import ros1_channel as channel
        with patch.object(channel, "port_open", return_value=True), patch.object(channel, "spawn") as spawn:
            channel.start_openocd()
            spawn.assert_not_called()
        child = Mock()
        child.poll.return_value = None
        with patch.object(channel, "_processes", [child]), \
             patch.object(channel, "wsl", return_value=types.SimpleNamespace(stdout="")), \
             patch.object(channel.subprocess, "run") as powershell:
            channel.stop_all()
        child.terminate.assert_called_once()
        self.assertNotIn("Get-Process openocd", powershell.call_args.args[0][-1])

    @unittest.skipUnless(os.environ.get("PROBE_ADDRESSES"), "requires resolved stm32-skills-probe ELF")
    def test_both_consumers_with_resolved_probe_addresses(self):
        fields = json.loads(pathlib.Path(os.environ["PROBE_ADDRESSES"]).read_text())["fields"]
        addresses = sorted({a for f in fields for a in aligned_needed(f["address"])})
        memory = {a: (lambda n, value=i+123: value) for i, a in enumerate(addresses)}
        node = ros_module()
        with FakeOpenOcd(memory, noise={0: "before", 1: "midecho"}) as server:
            ros_reader = node.OpenOcdLink(port=server.port)
            diag_reader = OpenOcdReader("127.0.0.1", server.port)
            try:
                words = diag_reader.read_words(addresses)
                for field in fields:
                    self.assertEqual(ros_reader.read_word(field["address"]), word_at(words, field["address"]))
            finally:
                ros_reader.close()
                diag_reader.close()


if __name__ == "__main__":
    unittest.main()
