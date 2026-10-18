"""Regressions for the functional Renode recording assertions, without the emulator."""
import unittest
from run_checks import validate_renode_record


def samples():
    values = {"/probe/tick_ms": [978, 1235, 1414, 1625, 1886, 1948],
              "/probe/loop_counter": [1163266, 1655087, 2011208, 2208363, 2509144, 2813058],
              "/probe/pin_frame": [0, 0, 1, 1, 0, 0]}
    values.update({"/other/%d" % n: [0] * 6 for n in range(5)})
    return values


class RecordingChecks(unittest.TestCase):
    def test_slow_runner(self):
        validate_renode_record(samples())

    def test_two_complete_cycles(self):
        validate_renode_record({k: v[:2] for k, v in samples().items()})

    def test_incomplete_or_too_short(self):
        for length in (0, 1):
            with self.assertRaisesRegex(RuntimeError, "complete cycles"):
                validate_renode_record({k: v[:length] for k, v in samples().items()})
        values = samples()
        values["/probe/pin_frame"].pop()
        with self.assertRaisesRegex(RuntimeError, "complete cycles"):
            validate_renode_record(values)

    def test_stalled_or_backwards_counters(self):
        for topic in ("/probe/tick_ms", "/probe/loop_counter"):
            for data in ([1] * 6, [1, 2, 3, 2, 4, 5]):
                values = samples()
                values[topic] = data
                with self.subTest(topic=topic, data=data), self.assertRaisesRegex(RuntimeError, "counter"):
                    validate_renode_record(values)

    def test_missing_topic_or_invalid_pin(self):
        values = samples()
        del values["/probe/tick_ms"]
        with self.assertRaisesRegex(RuntimeError, "topics"):
            validate_renode_record(values)
        values = samples()
        values["/probe/pin_frame"][-1] = 2
        with self.assertRaisesRegex(RuntimeError, "pin_frame"):
            validate_renode_record(values)


if __name__ == "__main__":
    unittest.main()
