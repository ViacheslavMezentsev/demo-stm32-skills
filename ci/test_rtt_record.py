"""RTT recording evidence: synthetic transport, real writers, no probe access."""
import collections
import json
import pathlib
import socket
import struct
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stm32-diag-record/scripts/rtt"))
import rtt_timer as rt
from rtt_frames import build_frame


class Telnet:
    def command(self, command):
        return "Control block found" if command == "rtt start" else ""


class Stream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False

    def settimeout(self, seconds):
        pass

    def recv(self, size):
        item = next(self.chunks, b"")
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


def messages(path):
    # Independent readers are required in Emulation; Kits needs only stdlib.
    if "--readers" in sys.argv:
        if path.suffix == ".mcap":
            from mcap.reader import make_reader
            with path.open("rb") as stream:
                return [(c.topic, m.log_time, struct.unpack("<I", m.data)[0])
                        for _, c, m in make_reader(stream).iter_messages(log_time_order=False)]
        from rosbags.rosbag1 import Reader
        with Reader(path) as reader:
            return [(c.topic, t, struct.unpack("<I", data)[0]) for c, t, data in reader.messages()]
    data = path.read_bytes()
    assert data[:8] == data[-8:] == b"\x89MCAP0\r\n"
    channels, result = {}, []
    def records(data):
        offset = 0
        while offset < len(data):
            op, size = struct.unpack_from("<BQ", data, offset)
            body = data[offset + 9:offset + 9 + size]
            assert len(body) == size
            offset += 9 + size
            if op == 4:
                cid, _, length = struct.unpack_from("<HHI", body)
                channels[cid] = body[8:8 + length].decode()
            elif op == 6:
                assert struct.unpack_from("<I", body, 28)[0] == 0
                records(body[40:])
            elif op == 5:
                cid, _, stamp, _, value = struct.unpack("<HIQQI", body)
                result.append((channels[cid], stamp, value))
    records(data[8:-8])
    return result


class RecordTests(unittest.TestCase):
    def run_record(self, end=b"", fmt="mcap", chunks=None, seconds=2):
        layout = {"frame": {"len": 12, "header": 8, "count": 1},
                  "fields": [{"topic": "/signal", "message": "std_msgs/UInt32", "kind": "uint32", "max": 10}],
                  "rtt": {"control_block": 0x20000100, "size": 120, "id": "DIAG RTT"},
                  "timer": {"name": "TIM3", "actual_hz": 1000}}
        raw = b"".join(build_frame(s, t, [v]) for s, t, v in
                       [(65534, 0xFFFFFFFF, 1), (65535, 0xFFFFFFFF, 99), (0, 0, 3), (3, 3, 4)])
        stream = Stream(chunks if chunks is not None else [raw[:7], socket.timeout(), raw[7:] + b"\xa6", end])
        args = SimpleNamespace(record=fmt, chunk_size=3, compress="bz2", host="localhost", seconds=seconds, rtt_poll_ms=10)
        with tempfile.TemporaryDirectory() as directory:
            out = pathlib.Path(directory) / ("capture." + fmt)
            with patch.object(rt.socket, "create_connection", return_value=stream):
                if isinstance(end, OSError):
                    with self.assertRaises(OSError):
                        rt.read_stream(Telnet(), layout, args, out)
                else:
                    rt.read_stream(Telnet(), layout, args, out)
            self.assertTrue(stream.closed)
            report = json.loads(out.with_suffix(".rtt.json").read_text())
            return report, messages(out)

    def test_seq_tick_loss_and_partial_exit(self):
        formats = ("mcap", "bag") if "--readers" in sys.argv else ("mcap",)
        for fmt in formats:
            for end, reason in [(b"", "eof"), (KeyboardInterrupt(), "interrupt"), (ConnectionResetError(), "error")]:
                with self.subTest(fmt=fmt, reason=reason):
                    report, data = self.run_record(end, fmt)
                    stats = report["stats"]
                    self.assertEqual(report["stop_reason"], reason)
                    self.assertTrue(report["record_closed"])
                    self.assertIsNone(report["target_dropped"])
                    self.assertEqual((stats["frames"], stats["written"], stats["metadata_written"]), (4, 3, 8))
                    self.assertEqual((stats["seq_gaps"], stats["dropped_est"], stats["bad_tail"]), (1, 2, 1))
                    self.assertEqual((stats["tick_repeats"], stats["tick_wraps"]), (1, 1))
                    # Bag's reader sorts timestamps, so compare paired time/value multisets.
                    self.assertEqual(collections.Counter((t, v) for topic, t, v in data if topic == rt.SEQ_TOPIC),
                                     collections.Counter([(0xFFFFFFFF * 1000000, 65534), (0xFFFFFFFF * 1000000, 65535), (0, 0), (3000000, 3)]))
                    self.assertEqual(sorted(v for topic, _, v in data if topic == "/signal"), [1, 3, 4])
                    self.assertEqual(sum(topic == rt.TICK_TOPIC for topic, _, _ in data), 4)

    def test_empty_duration(self):
        report, data = self.run_record(seconds=0)
        self.assertEqual(report["stop_reason"], "duration")
        self.assertEqual(report["stats"]["frames"], 0)
        self.assertIsNone(report["stats"]["first_seq"])
        self.assertEqual(data, [])

    def test_reserved_topics(self):
        for topics in [[rt.SEQ_TOPIC], [rt.TICK_TOPIC], ["/same", "/same"]]:
            with self.assertRaises(ValueError):
                rt.validate_topics({"fields": [{"topic": t} for t in topics]})

    def test_connect_failure_closes_file_and_reports(self):
        with patch.object(rt.socket, "create_connection", side_effect=ConnectionRefusedError()):
            with tempfile.TemporaryDirectory() as directory:
                out = pathlib.Path(directory) / "failed.mcap"
                layout = {"frame": {"len": 8, "header": 8, "count": 0}, "fields": [],
                          "rtt": {"control_block": 0, "size": 32, "id": "DIAG RTT"}, "timer": {}}
                args = SimpleNamespace(record="mcap", chunk_size=3, seconds=1, host="localhost")
                with self.assertRaises(ConnectionRefusedError):
                    rt.read_stream(Telnet(), layout, args, out)
                report = json.loads(out.with_suffix(".rtt.json").read_text())
                self.assertEqual(report["stop_reason"], "error")
                self.assertTrue(report["record_closed"])
                self.assertEqual(report["stats"]["frames"], 0)
                self.assertEqual(messages(out), [])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
