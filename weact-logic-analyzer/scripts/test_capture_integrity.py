"""Regression tests for successful exit with incomplete or malformed capture."""
import pathlib
import tempfile
import unittest
import zipfile
from unittest.mock import Mock, patch
from weact_la import LogicAnalyzer, LAError, TriggerTimeout

class CaptureIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "capture.sr"
        self.la = LogicAnalyzer("100k")
        self.la.path = str(self.path)
        self.la.expected_samples = 100
        self.la.proc = Mock(returncode=0)
        self.la.proc.communicate.return_value = ("", "")

    def write_capture(self, n=100, rate="100 kHz"):
        with zipfile.ZipFile(self.path, "w") as z:
            z.writestr("metadata", "[device 1]\nsamplerate=" + rate +
                       "\nunitsize=1\nprobe1=D0\ncapturefile=logic-1\n")
            z.writestr("logic-1-1", bytes(n))

    def test_complete(self):
        self.write_capture()
        self.assertEqual(self.la.collect().n, 100)

    def test_short_zero_exit_with_and_without_warning(self):
        for warning in ("", "Device only sent 50 samples."):
            with self.subTest(warning=warning):
                self.write_capture(50)
                self.la.proc = Mock(returncode=0)
                self.la.proc.communicate.return_value = ("", warning)
                with self.assertRaisesRegex(LAError, "50.*100"):
                    self.la.collect()
                self.assertTrue(self.path.exists())

    def test_trigger_warning_with_complete_file(self):
        self.write_capture()
        self.la.proc.communicate.return_value = ("", "Device only sent 90 samples.")
        with patch("sys.stderr"):
            self.assertEqual(self.la.collect().n, 100)

    def test_excess_samples(self):
        self.write_capture(101)
        with self.assertRaises(LAError): self.la.collect()

    def test_wrong_rate(self):
        self.write_capture(rate="1 MHz")
        with self.assertRaises(LAError): self.la.collect()

    def test_corrupt_archive(self):
        self.path.write_bytes(b"broken")
        with self.assertRaises(LAError): self.la.collect()

    def test_missing_file(self):
        with self.assertRaises(TriggerTimeout): self.la.collect()

    def test_nonzero_exit(self):
        self.write_capture()
        self.la.proc.returncode = 1
        with self.assertRaises(LAError): self.la.collect()

    def test_invalid_samples_before_launch(self):
        for n in (0, -1, 1.5):
            with patch("weact_la.subprocess.Popen") as spawn:
                self.la.proc = None
                with self.assertRaises(ValueError): self.la.arm(self.path, samples=n)
                spawn.assert_not_called()

if __name__ == "__main__":
    unittest.main()
