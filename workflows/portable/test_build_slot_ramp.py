import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock
import sys
sys.path.insert(0, os.path.dirname(__file__))
import build_slot


class TestRampBudget(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='slot-ramp-')
        self.addCleanup(self.temp.cleanup)
        self.clock = [1000.0]
        self.manager = build_slot.BuildSlotManager(run_dir=self.temp.name, acquisition_stagger=0)
        for patcher in (
            mock.patch('build_slot.time.time', side_effect=lambda: self.clock[0]),
            mock.patch('build_slot.time.sleep', side_effect=lambda delay: self.clock.__setitem__(0, self.clock[0]+delay)),
            mock.patch('build_slot.get_available_ram_gib', return_value=5.0),
            mock.patch('build_slot.get_system_ram_percent', return_value=50.0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def hold(self, job_class, age, stamp=None):
        mem = build_slot.MEMORY_RESERVATIONS[job_class]
        os.makedirs(self.manager.slot_dirs[0], exist_ok=True)
        self.manager._write_slot_info(0, 'held', os.getpid(), token='held', job_class=job_class, mem_gib=mem)
        info = self.manager._read_slot_info(0)
        info['acquired_at_epoch'] = self.clock[0]-age if stamp is None else stamp
        info['acquired_at'] = 'invalid'
        build_slot._write_json_atomic(os.path.join(self.manager.slot_dirs[0], 'info.json'), info)
        return mem

    def test_per_class_ramp_boundaries_keep_floor(self):
        for job_class, window in [('heavy', 300), ('medium', 120), ('light', 60), ('browser', 60)]:
            for age, charge in [(window-.001, True), (window, False), (window+1, False)]:
                with self.subTest(job_class=job_class, age=age):
                    mem = self.hold(job_class, age)
                    budget = self.manager._memory_budget()
                    self.assertEqual(budget['reserved_gib'], mem)
                    self.assertAlmostEqual(budget['free_budget_gib'], 5.0-3.0-(mem if charge else 0.0))

    def test_second_medium_acquires_after_first_ramp(self):
        self.hold('medium', 121)
        self.assertTrue(self.manager.acquire('second', token='second', timeout=.05, poll_interval=.01, job_class='medium'))
        self.assertTrue(self.manager.is_held_by('held'))
        self.assertTrue(self.manager.release('second', token='second'))

    def test_second_medium_waits_during_first_ramp(self):
        self.hold('medium', 119)
        with redirect_stderr(io.StringIO()):
            self.assertFalse(self.manager.acquire('second', token='second', timeout=.05, poll_interval=.01, job_class='medium'))
        self.assertFalse(self.manager.is_held_by('second'))

    def test_heavy_cap_survives_ramp_expiry(self):
        self.hold('heavy', 301)
        with mock.patch('build_slot.get_system_ram_percent', return_value=88.0):
            budget = self.manager._memory_budget()
            self.assertEqual(budget['heavy_jobs'], 1)
            self.assertIn('heavy cap', self.manager._resource_refusal('heavy', 4.5, budget))
        # At low RAM (50%), heavy cap triggers when two heavy jobs are held:
        budget_two = dict(budget, heavy_jobs=2, ram_percent=50.0)
        self.assertIn('heavy cap', self.manager._resource_refusal('heavy', 4.5, budget_two))

    def test_missing_corrupt_and_future_time_keep_full_charge(self):
        for stamp in ['bad', float('nan'), float('inf'), 1001.0]:
            with self.subTest(stamp=stamp):
                self.hold('heavy', 301, stamp=stamp)
                self.assertEqual(self.manager._memory_budget()['free_budget_gib'], -2.5)
        self.hold('heavy', 301)
        info = self.manager._read_slot_info(0)
        info.pop('acquired_at_epoch')
        info.pop('acquired_at')
        build_slot._write_json_atomic(os.path.join(self.manager.slot_dirs[0], 'info.json'), info)
        self.assertEqual(self.manager._memory_budget()['free_budget_gib'], -2.5)


if __name__ == '__main__':
    unittest.main()
