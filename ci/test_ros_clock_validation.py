"""Selection uses real ROS topics; clock diagnostics keep independent timescales."""
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "stm32-ros-alt-channel/scripts"))
import ros1_channel as channel
from host_ref_probe import summarize

class ValidationTests(unittest.TestCase):
    def test_select(self):
        listing = "/rosout\n/rosout_agg\n/bp/adc\n/bp/tick_ms\n/swd/bp/tick_ms\n"
        self.assertEqual(channel.select_hz_topic(listing), "/bp/tick_ms")
        self.assertEqual(channel.select_hz_topic(listing, "/bp/adc"), "/bp/adc")
        self.assertEqual(channel.select_hz_topic(listing, explicit="/swd/bp/tick_ms"), "/swd/bp/tick_ms")
        self.assertIsNone(channel.select_hz_topic(listing, "/missing"))
        self.assertIsNone(channel.select_hz_topic(listing, explicit="/missing"))
        self.assertIsNone(channel.select_hz_topic("/rosout\n/rosout_agg"))
        self.assertEqual(channel.select_hz_topic("/native/one"), "/native/one")
        many = "\n".join("/sensor/%d" % n for n in range(20))
        self.assertEqual(channel.select_hz_topic(many, explicit="/sensor/19"), "/sensor/19")

    def test_frequency_timeout_and_errors(self):
        for code, output, expected in [(124,"average rate: 9.998\n",True),
                                        (0,"average rate: 10.000\n",True),
                                        (124,"no new messages",False),
                                        (1,"average rate: 10.000",False)]:
            with patch.object(channel, "wsl", return_value=subprocess.CompletedProcess([],code,output,"")) as run:
                self.assertEqual(channel.measure_frequency("/bp/tick_ms")[0], expected)
                self.assertIn("rostopic hz /bp/tick_ms", run.call_args.args[0])
        with patch.object(channel,"wsl") as run:
            self.assertFalse(channel.measure_frequency(None)[0])
            run.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux ABI")
    def test_timex_full_buffer_and_read_only_query(self):
        import ctypes
        from wsl_tick import Timex, read_timex
        self.assertEqual(ctypes.sizeof(Timex), 208)
        self.assertEqual(Timex().modes, 0)
        self.assertGreater(read_timex()["tick"], 0)

    def test_clock_rates_and_steps(self):
        result = summarize(dict(host=10,real=20,mono=30,raw=40),
                           dict(host=110,real=120,mono=122,raw=140),2,0.001)
        self.assertEqual(result["ratios_to_host"],dict(real=1,mono=0.92,raw=1))
        self.assertEqual(result["realtime_steps"],2)
        with self.assertRaises(ValueError):
            summarize(dict.fromkeys(("host","real","mono","raw"),0),
                      dict.fromkeys(("host","real","mono","raw"),0),0,0)

if __name__ == "__main__":
    unittest.main()
