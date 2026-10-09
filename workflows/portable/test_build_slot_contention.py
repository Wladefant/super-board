"""Slot progress must not wait for a released owner's queue cleanup."""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import build_slot


class TestReleaseContention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = build_slot.BuildSlotManager(run_dir=self.tmp.name, max_slots=1, acquisition_stagger=0)
        self.env = mock.patch.dict(os.environ, {'BUILD_SLOT_RAM_PERCENT': '80', 'BUILD_SLOT_AVAILABLE_GIB': '64'})
        self.env.start()
        self.started = threading.Event()
        self.finish = threading.Event()
        self.errors = []

    def tearDown(self):
        self.finish.set()
        if hasattr(self, 'thread'):
            self.thread.join(10)
            self.assertFalse(self.thread.is_alive())
        self.env.stop()
        self.tmp.cleanup()

    def block_release_cleanup(self):
        os.mkdir(self.manager.slot_dirs[0])
        self.manager._write_slot_info(0, 'releasing', os.getpid(), token='released')

        def slow_cleanup(*args, **kwargs):
            self.started.set()
            self.finish.wait(10)

        def release():
            try:
                with mock.patch.object(self.manager, '_dequeue_best_effort', side_effect=slow_cleanup):
                    self.manager.release('releasing', token='released')
            except Exception as error:
                self.errors.append(error)

        self.thread = threading.Thread(target=release)
        self.thread.start()
        self.assertTrue(self.started.wait(3))

    def test_dead_owner_reclaimed_during_release_queue_contention(self):
        self.block_release_cleanup()
        os.mkdir(self.manager.slot_dirs[0])
        self.manager._write_slot_info(0, 'dead', 99999999, token='dead', wrapper_pid=99999999)
        with mock.patch.object(build_slot, '_windows_job_alive', return_value=False):
            reclaimed = self.manager.check_stale_and_reclaim()
        self.assertTrue(reclaimed)
        self.assertFalse(os.path.exists(self.manager.slot_dirs[0]))

    def test_live_owner_preserved_during_release_queue_contention(self):
        self.block_release_cleanup()
        os.mkdir(self.manager.slot_dirs[0])
        self.manager._write_slot_info(0, 'live', os.getpid(), token='live', wrapper_pid=os.getpid())
        self.assertFalse(self.manager.check_stale_and_reclaim())
        self.assertEqual(self.manager._read_slot_info(0)['owner'], 'live')

    def test_fifo_waiter_acquires_during_release_queue_contention(self):
        self.block_release_cleanup()
        self.manager.enqueue('first', os.getpid(), token='first', enqueued_at=time.time() - 1)
        self.manager.enqueue('second', os.getpid(), token='second', enqueued_at=time.time())
        started = time.monotonic()
        self.assertFalse(self.manager.acquire('second', pid=os.getpid(), token='second', timeout=0.15, poll_interval=0.02))
        self.assertTrue(self.manager.acquire('first', pid=os.getpid(), token='first', timeout=0.5, poll_interval=0.02))
        self.assertEqual(self.manager._read_slot_info(0)['owner'], 'first')
        self.assertLess(time.monotonic() - started, 2, 'FIFO grant waited for unrelated release cleanup')


if __name__ == '__main__':
    unittest.main()
