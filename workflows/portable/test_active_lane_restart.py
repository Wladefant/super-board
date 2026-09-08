"""Real Windows/POSIX child plus fresh-interpreter native-lane recovery.

The portable guard owns durable task handles, not OS process supervision. This
proves adoption of a recorded live lane without duplicate dispatch; it does not
claim PID discovery or hot-reloading code inside an already running harness.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from test_recurrence_guard import make_repo
from worker_backend import WorkerBackend


class TestActiveLaneRestart(unittest.TestCase):
    def test_fresh_interpreter_adopts_live_lane_and_preserves_guard(self):
        with tempfile.TemporaryDirectory(prefix="active_lane_restart_") as root:
            repo = os.path.join(root, "repo")
            head = make_repo(repo)
            state = os.path.join(root, "state")
            backend = WorkerBackend(state_dir=state)
            request = dict(request_id="req-live-child", stage="review",
                           repo_root=repo, head_sha=head)
            ticket = backend.prepare_native(request)
            self.assertEqual(ticket.state, "prepared")
            child_script = os.path.join(root, "child.py")
            with open(child_script, "w", encoding="utf-8") as stream:
                stream.write("import os, sys\nprint(os.getpid(), flush=True)\nsys.stdin.readline()\n")
            child = subprocess.Popen([sys.executable, "-u", child_script],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(int(child.stdout.readline()), child.pid)
                handle = f"agent://fixture-child-{child.pid}"
                backend.record_native_dispatch(ticket.run_id, handle)
                del backend
                restart_script = os.path.join(root, "restart.py")
                with open(restart_script, "w", encoding="utf-8") as stream:
                    stream.write(
                        "import json, sys\n"
                        "sys.path.insert(0, sys.argv[1])\n"
                        "from worker_backend import WorkerBackend, WorkerBackendError\n"
                        "from recurrence_guard import RecurrenceGuard\n"
                        "backend = WorkerBackend(state_dir=sys.argv[2])\n"
                        "request = json.loads(sys.argv[3])\n"
                        "ticket = backend.prepare_native(request)\n"
                        "try:\n"
                        "    backend.retry_native(ticket.run_id)\n"
                        "except WorkerBackendError:\n"
                        "    refused = True\n"
                        "else:\n"
                        "    refused = False\n"
                        "guard = RecurrenceGuard(state_dir=sys.argv[2])\n"
                        "print(json.dumps(dict(run_id=ticket.run_id, state=ticket.state, "
                        "handle=ticket.task_handle, retry_refused=refused, "
                        "observations=len(guard.list_signatures()))))\n"
                    )
                resumed = subprocess.run(
                    [sys.executable, restart_script, os.path.dirname(__file__), state,
                     json.dumps(request)], capture_output=True, text=True, timeout=30)
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertEqual(json.loads(resumed.stdout), dict(
                    run_id=ticket.run_id, state="background_dispatched", handle=handle,
                    retry_refused=True, observations=0))
                self.assertIsNone(child.poll(), "adoption must not terminate the active lane")
                runs = os.path.join(state, "worker_runs")
                self.assertEqual([name for name in os.listdir(runs)
                                  if os.path.isdir(os.path.join(runs, name))], [ticket.run_id])
            finally:
                child.communicate("stop\n", timeout=10)
            self.assertEqual(child.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
