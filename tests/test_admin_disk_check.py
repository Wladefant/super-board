import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import admin_disk_check as c  # noqa: E402


class DiskCheckTests(unittest.TestCase):
    def test_alerts_once_below_15(self):
        self.assertEqual(c.decide(False, 14.9), (True, "alert"))
        self.assertEqual(c.decide(True, 10.0), (True, None))

    def test_hysteresis_band_is_silent(self):
        self.assertEqual(c.decide(True, 17.0), (True, None))
        self.assertEqual(c.decide(False, 17.0), (False, None))
        self.assertEqual(c.decide(False, 15.0), (False, None))

    def test_recovers_once_above_20(self):
        self.assertEqual(c.decide(True, 20.1), (False, "recovered"))
        self.assertEqual(c.decide(True, 20.0), (True, None))

    def test_free_pct(self):
        self.assertAlmostEqual(c.free_pct({"totalDisk": "200", "diskUsed": "170"}), 15.0)


if __name__ == "__main__":
    unittest.main()
