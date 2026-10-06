import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import admin_digest as d  # noqa: E402

METRICS = {"cpu": "8.71", "memUsed": "37.57", "memUsedGB": "5.87", "memTotal": "15.62",
           "uptime": 627921, "diskUsed": "67.84", "totalDisk": "192.69"}


class AdminDigestTests(unittest.TestCase):
    def test_host_line_reports_ram_disk_free_cpu(self):
        line = d.host_line(METRICS)
        self.assertIn("RAM 38%", line)
        self.assertIn("disk 65% free", line)
        self.assertIn("CPU 9%", line)
        self.assertIn("up 7 d", line)

    def test_bad_containers_only_restarting_or_unhealthy(self):
        ps = "a|Up 3 hours\nb|Restarting (1) 5 seconds ago\nc|Up 2 hours (unhealthy)\nd|Exited (0) 1 hour ago"
        self.assertEqual(d.bad_containers(ps), ["b: Restarting (1) 5 seconds ago", "c: Up 2 hours (unhealthy)"])

    def test_is_up_boundaries(self):
        self.assertTrue(d.is_up(401))
        self.assertFalse(d.is_up(502))
        self.assertFalse(d.is_up(0))

    def test_message_clean_and_degraded(self):
        ok = d.build_message("RAM 1%", [], [], 24)
        self.assertIn("all 24 up", ok)
        self.assertIn("none restarting", ok)
        bad = d.build_message(None, None, [("svc<x>", 502)], 24)
        self.assertIn("metrics unavailable", bad)
        self.assertIn("could not read docker ps", bad)
        self.assertIn("23 of 24 up", bad)
        self.assertIn("svc&lt;x&gt;", bad)
        self.assertIn(d.DOKPLOY_ADMIN, bad)


if __name__ == "__main__":
    unittest.main()
