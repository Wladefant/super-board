import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock
import sys
sys.path.insert(0, os.path.dirname(__file__))
import build_slot


class TestStaggerBackfill(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='slot-stagger-backfill-')
        self.addCleanup(self.temp.cleanup)
        self.clock = [1000.0]
        self.manager = build_slot.BuildSlotManager(run_dir=self.temp.name, acquisition_stagger=45)
        for patcher in (
            mock.patch('build_slot.time.time', side_effect=lambda: self.clock[0]),
            mock.patch('build_slot.time.sleep', side_effect=lambda delay: self.clock.__setitem__(0, self.clock[0]+delay)),
            mock.patch('build_slot.get_available_ram_gib', return_value=16.0),
            mock.patch('build_slot.get_system_ram_percent', return_value=50.0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        build_slot._write_json_atomic(os.path.join(self.temp.name, build_slot.LAST_ACQUIRED_FILE_NAME),
                                     {'acquired_at_epoch': 998.0, 'owner': 'previous-heavy'})

    def head(self, mem=3.0):
        self.manager.enqueue('head', os.getpid(), token='head', job_class='heavy', mem_gib=mem, enqueued_at=900.0)
        return self.manager._read_queue()[0].copy()

    def test_medium_backfills_stagger_blocked_heavy_head(self):
        original = self.head()
        self.assertTrue(self.manager.acquire('medium', token='medium', timeout=.05,
                                            poll_interval=.01, job_class='medium'))
        self.assertTrue(self.manager.is_held_by('medium'))
        self.assertEqual(self.manager._read_queue()[0], original)
        self.assertEqual(self.manager._read_last_acquired_at(), 998.0)
        self.assertTrue(self.manager.release('medium', token='medium'))

    def test_heavy_backfill_cannot_skip_stagger_even_with_smaller_reservation(self):
        self.head(8.0)
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertFalse(self.manager.acquire('heavy-small', token='heavy-small', timeout=.05,
                                                 poll_interval=.01, job_class='heavy', mem_gib=1.0))
        self.assertIn('stagger', output.getvalue())
        self.assertFalse(self.manager.is_held_by('heavy-small'))

    def test_every_queue_head_keeps_stagger(self):
        for job_class in ['heavy', 'medium', 'light']:
            with self.subTest(job_class=job_class), redirect_stderr(io.StringIO()):
                acquired = self.manager.acquire('head', token='head', timeout=.05,
                                                poll_interval=.01, job_class=job_class)
                if acquired:
                    self.manager.release('head', token='head')
                self.assertFalse(acquired)

    def test_backfill_still_obeys_memory_floor(self):
        self.head()
        with mock.patch('build_slot.get_available_ram_gib', return_value=4.0), redirect_stderr(io.StringIO()):
            self.assertFalse(self.manager.acquire('medium', token='medium', timeout=.05,
                                                 poll_interval=.01, job_class='medium'))
        self.assertFalse(self.manager.is_held_by('medium'))

    def test_stagger_elapsed_restores_head_priority(self):
        self.head()
        self.clock[0] = 1043.0
        with redirect_stderr(io.StringIO()):
            self.assertFalse(self.manager.acquire('medium', token='medium', timeout=.05,
                                                 poll_interval=.01, job_class='medium'))
        self.assertTrue(self.manager.acquire('head', token='head', timeout=.05, job_class='heavy'))
        self.assertTrue(self.manager.release('head', token='head'))


    def test_heavy_explicit_memory_keeps_four_point_five_gib_floor_and_default(self):
        self.clock[0] = 1044.0
        cases = [
            (None, 4.5),
            (2.0, 4.5),
            (3.0, 4.5),
            (8.0, 8.0),
        ]
        for declared, expected in cases:
            with self.subTest(declared=declared, expected=expected):
                if declared is None:
                    args = build_slot.parse_args(['run', '--class', 'heavy', 'build', '--', 'python', '-V'])
                else:
                    args = build_slot.parse_args(['run', '--class', 'heavy', '--mem-gib', str(declared), 'build', '--', 'python', '-V'])
                self.assertEqual(args.job_class, 'heavy')
                self.assertEqual(args.mem_gib, declared)
                self.assertEqual(args.cmd, ['python', '-V'])
                self.assertTrue(self.manager.acquire('build', token='build', timeout=.05,
                                                    job_class=args.job_class, mem_gib=args.mem_gib))
                slot_info = self.manager._read_slot_info(0)
                self.assertEqual(slot_info['mem_gib'], expected)
                self.assertEqual(slot_info['job_class'], 'heavy')
                self.assertTrue(self.manager.release('build', token='build'))
                self.clock[0] += 46

    def test_browser_runs_beside_heavy_build_when_ram_fits(self):
        self.clock[0] = 1044.0
        self.assertTrue(self.manager.acquire('build', token='build', timeout=.05,
                                            job_class='heavy', mem_gib=3.0))
        self.clock[0] += 46
        self.assertTrue(self.manager.acquire('browser', token='browser', timeout=.05, job_class='browser'))
        browser = next(self.manager._read_slot_info(i) for i,p in enumerate(self.manager.slot_dirs)
                       if os.path.isdir(p) and self.manager._read_slot_info(i)['owner'] == 'browser')
        self.assertEqual(browser['mem_gib'], 1.1)
        self.assertEqual(self.manager._memory_budget()['heavy_jobs'], 1)
        self.assertTrue(self.manager.release('browser', token='browser'))
        self.assertTrue(self.manager.release('build', token='build'))

    def test_browser_classification_and_cli(self):
        self.assertEqual(build_slot.classify_command(['node', 'playwright', 'qa']), 'browser')
        self.assertEqual(build_slot.classify_command(['node', 'next', 'build', 'playwright']), 'heavy')
        args = build_slot.parse_args(['run', '--class', 'browser', 'qa', '--', 'python', '-V'])
        self.assertEqual(args.job_class, 'browser')
        self.assertEqual(build_slot.MEMORY_RAMP_SECONDS['browser'], 60.0)

    def test_browser_class_refuses_next_build_before_launch(self):
        output = io.StringIO()
        with mock.patch('build_slot._WindowsJobProcess') as child, redirect_stderr(output):
            self.assertEqual(self.manager.run_command('qa', ['node', 'next', 'build'], job_class='browser'), 1)
            child.assert_not_called()
        self.assertIn('browser class cannot build or serve Next', output.getvalue())


if __name__ == '__main__':
    unittest.main()
