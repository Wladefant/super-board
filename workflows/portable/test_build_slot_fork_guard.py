"""Fork children must not inherit a parent's reentrant guard permission."""
import multiprocessing as mp
import os
import tempfile
import unittest

from workflows.portable import build_slot


def _enter_in_child(path, started, entered, result):
    started.set()
    with build_slot._transition_guard(path, timeout=2.0):
        entered.set()
        result.put(os.getpid())


@unittest.skipUnless('fork' in mp.get_all_start_methods(), 'POSIX fork only')
class TestForkGuard(unittest.TestCase):
    def test_fork_child_waits_for_parent_guard(self):
        ctx = mp.get_context('fork')
        with tempfile.TemporaryDirectory() as run_dir:
            path = os.path.join(run_dir, 'build-slot.guard')
            started, entered, result = ctx.Event(), ctx.Event(), ctx.Queue()
            child = ctx.Process(target=_enter_in_child, args=(path, started, entered, result))
            try:
                with build_slot._transition_guard(path):
                    child.start()
                    self.assertTrue(started.wait(2.0), 'child starts guard attempt')
                    self.assertFalse(entered.wait(0.2), 'child cannot inherit parent reentry permission')
                self.assertTrue(entered.wait(2.0), 'child acquires after parent unlocks')
                child.join(2.0)
                self.assertFalse(child.is_alive(), 'child completes within deadline')
                self.assertEqual(child.exitcode, 0)
                self.assertEqual(result.get(timeout=1.0), child.pid)
            finally:
                if child.pid is not None:
                    if child.is_alive():
                        child.terminate()
                    child.join(2.0)
                result.close()
                result.join_thread()


if __name__ == '__main__':
    unittest.main()
