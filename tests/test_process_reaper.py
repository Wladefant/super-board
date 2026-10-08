"""Tests for process_reaper.py: what is reaped and what is kept, on a fake process table."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workflows" / "portable"))
import process_reaper as pr

NOW = 1_000_000.0
RUNNER = r"C:\Users\x\miniconda3\python.exe -u C:\Users\x\AppData\Local\Temp\veyyon-python-runner\runner-abc123.py"
NEXT = r"C:\Program Files\nodejs\node.exe C:\wt\frontend\node_modules\next\dist\bin\next dev -p 3101"


def proc(pid, ppid, name, cmd="", age_s=3600.0, cpu=1.0, rss=30 * 1048576) -> pr.Proc:
    return pr.Proc(pid, ppid, name, cmd, NOW - age_s, cpu, rss)


def table(*procs: pr.Proc) -> List[pr.Proc]:
    # explorer is a live non-veyyon root so chains can end in a live process
    return [proc(4, 0, "system"), proc(10, 4, "explorer.exe"), proc(100, 10, "veyyon.exe"), *procs]


def actions(decisions: List[pr.Decision]) -> Dict[int, str]:
    return {d.proc.pid: d.action for d in decisions}


def run_classify(procs, resample=None, state=None, **kw):
    return pr.classify(procs, resample or {}, state or {}, NOW, **kw)


# ---------- runners ----------


def test_live_busy_runner_is_kept() -> None:
    procs = table(proc(200, 100, "python.exe", RUNNER, cpu=5.0))
    d, _ = run_classify(procs, resample={200: 7.5}, state={"200:%d" % int(NOW - 3600): {"cpu": 5.0, "since": NOW - 3000}})
    assert actions(d)[200] == "keep"
    assert "busy" in d[0].reason


def test_runner_with_dead_parent_is_reaped() -> None:
    procs = table(proc(201, 99999, "python.exe", RUNNER))  # parent 99999 does not exist
    d, _ = run_classify(procs)
    assert actions(d)[201] == "reap"
    assert "orphan" in d[0].reason


def test_runner_with_reused_parent_pid_is_reaped() -> None:
    # the parent pid exists but started after the runner, so it is another process
    procs = table(proc(202, 150, "python.exe", RUNNER, age_s=3600), proc(150, 10, "veyyon.exe", age_s=60))
    d, _ = run_classify(procs)
    assert actions(d)[202] == "reap"


def test_idle_runner_with_live_owner_waits_then_is_reaped() -> None:
    procs = table(proc(203, 100, "python.exe", RUNNER, cpu=2.0))
    key = "203:%d" % int(NOW - 3600)
    d, st = run_classify(procs, state={})
    assert actions(d)[203] == "keep"  # first sighting: no history yet
    assert st[key]["since"] == NOW
    d, _ = run_classify(procs, state={key: {"cpu": 2.0, "since": NOW - 10 * 60}})
    assert actions(d)[203] == "keep"  # idle 10 min < 20
    d, _ = run_classify(procs, state={key: {"cpu": 2.0, "since": NOW - 25 * 60}})
    assert actions(d)[203] == "reap"
    assert "idle 25 min" in d[0].reason


def test_runner_cpu_change_resets_idle_clock() -> None:
    procs = table(proc(204, 100, "python.exe", RUNNER, cpu=3.0))
    key = "204:%d" % int(NOW - 3600)
    d, st = run_classify(procs, state={key: {"cpu": 2.0, "since": NOW - 60 * 60}})
    assert actions(d)[204] == "keep"
    assert st[key] == {"cpu": 3.0, "since": NOW}


def test_young_runner_is_kept_even_if_orphan() -> None:
    procs = table(proc(205, 99999, "python.exe", RUNNER, age_s=30))
    d, _ = run_classify(procs)
    assert actions(d)[205] == "keep"


# ---------- dev servers and shells ----------


def test_dev_server_orphan_is_reaped_but_live_veyyon_child_is_kept() -> None:
    procs = table(
        proc(300, 99998, "node.exe", NEXT),  # ancestor chain ends at a missing process
        proc(301, 100, "node.exe", NEXT),  # veyyon.exe is alive
        proc(302, 10, "node.exe", NEXT),  # owned by a live non-veyyon process
    )
    a = actions(run_classify(procs)[0])
    assert a == {300: "reap", 301: "keep", 302: "keep"}


def test_orphan_chain_through_dead_shell_is_reaped() -> None:
    procs = table(
        proc(310, 99997, "cmd.exe", r"C:\Windows\system32\cmd.exe /c next dev"),
        proc(311, 310, "node.exe", NEXT),
    )
    a = actions(run_classify(procs)[0])
    assert a == {310: "reap", 311: "reap"}


def test_shell_with_a_kept_child_is_kept() -> None:
    procs = table(
        proc(320, 99996, "cmd.exe", r"cmd.exe /c build.cmd"),
        proc(321, 320, "node.exe", NEXT, cpu=1.0),
    )
    a = actions(run_classify(procs, resample={321: 4.0})[0])  # child is busy
    assert a == {320: "keep", 321: "keep"}


def test_typed_cmd_and_conhost_are_never_reaped() -> None:
    procs = table(
        proc(330, 99995, "cmd.exe", "cmd.exe"),  # interactive prompt, not started by a tool
        proc(331, 99994, "conhost.exe", r"\??\C:\WINDOWS\system32\conhost.exe 0x4"),
    )
    d, _ = run_classify(procs)
    assert 330 not in actions(d) and 331 not in actions(d)


# ---------- never touch unknown processes ----------


def test_unknown_processes_are_not_in_the_decisions() -> None:
    procs = table(
        proc(400, 99993, "python.exe", r"python.exe C:\work\train.py"),  # python, but not a runner
        proc(401, 99992, "node.exe", r"node.exe C:\work\server.js"),  # node, but not a dev server
        proc(402, 99991, "notepad.exe", "notepad.exe"),
        proc(403, 99990, "python.exe", RUNNER.replace("veyyon-python-runner", "other-runner")),
    )
    d, _ = run_classify(procs)
    assert d == []


def test_protected_pid_is_kept() -> None:
    procs = table(proc(500, 99989, "python.exe", RUNNER))
    d, _ = run_classify(procs, protected={500})
    assert actions(d)[500] == "keep"


# ---------- run(): dry run, live, cap, PID reuse, log ----------


def _run(tmp_path, procs_first, procs_second=None, live=False, max_kills=25):
    calls = iter([procs_first, procs_second or procs_first])
    killed: List[int] = []
    result = pr.run(
        live, max_kills=max_kills, sample_s=0,
        log_path=tmp_path / "reaper.jsonl", state_path=tmp_path / "state.json",
        lister=lambda: next(calls), killer=lambda pid: killed.append(pid) or True,
        sleeper=lambda s: None, clock=lambda: NOW,
    )
    return result, killed


def test_dry_run_kills_nothing_and_logs(tmp_path) -> None:
    procs = table(proc(600, 99988, "python.exe", RUNNER, rss=50 * 1048576))
    result, killed = _run(tmp_path, procs)
    assert killed == []
    assert result["mode"] == "dry-run" and result["reap_count"] == 1
    assert result["would_free_mb"] == 50.0 and result["freed_mb"] == 0.0
    line = json.loads((tmp_path / "reaper.jsonl").read_text().splitlines()[-1])
    assert line["mode"] == "dry-run" and line["targets"][0]["pid"] == 600


def test_live_run_kills_only_orphan_and_counts_freed_memory(tmp_path) -> None:
    procs = table(
        proc(610, 99987, "python.exe", RUNNER, rss=40 * 1048576),  # orphan
        proc(611, 100, "python.exe", RUNNER, cpu=1.0),  # live owner, busy below
        proc(612, 99986, "notepad.exe", "notepad.exe"),  # unknown
    )
    second = [pr.Proc(p.pid, p.ppid, p.name, p.cmd, p.created, p.cpu + (2.0 if p.pid == 611 else 0), p.rss) for p in procs]
    result, killed = _run(tmp_path, procs, second, live=True)
    assert killed == [610]
    assert result["reap_count"] == 1 and result["freed_mb"] == 40.0


def test_cap_limits_kills_per_run(tmp_path) -> None:
    procs = table(*[proc(700 + i, 99000 + i, "python.exe", RUNNER) for i in range(5)])
    result, killed = _run(tmp_path, procs, live=True, max_kills=2)
    assert len(killed) == 2 and result["capped"] == 3


def test_process_that_exited_or_was_replaced_before_kill_is_not_killed(tmp_path) -> None:
    first = table(proc(710, 99985, "python.exe", RUNNER))
    second = table()  # gone on the second listing
    result, killed = _run(tmp_path, first, second, live=True)
    assert killed == []
    assert result["reaped"][0]["action"] == "gone-before-kill"
    # same pid, new process (different start time)
    second = table(proc(710, 99985, "python.exe", RUNNER, age_s=10))
    _, killed = _run(tmp_path, first, second, live=True)
    assert killed == []


# ---------- veyyon daemon brokers (https://github.com/Wladefant/veyyon/issues/513) ----------

BROKER = r"C:\Users\x\AppData\Local\veyyon\veyyon.exe __veyyon_worker_daemon_broker"


def test_broker_with_dead_parent_is_reaped() -> None:
    procs = table(proc(800, 99970, "veyyon.exe", BROKER, age_s=3600))  # parent 99970 does not exist
    d, _ = run_classify(procs)
    assert actions(d)[800] == "reap"
    assert "orphan broker" in d[0].reason and d[0].group == "broker"


def test_broker_with_live_parent_is_kept_even_when_many() -> None:
    procs = table(*[proc(810 + i, 100, "veyyon.exe", BROKER, age_s=3000) for i in range(40)])
    d, _ = run_classify(procs)
    assert len(d) == 40 and all(x.action == "keep" for x in d)
    assert "alive" in d[0].reason


def test_broker_whose_parent_pid_was_reused_is_reaped() -> None:
    # pid 150 exists but started after the broker, so it is not the broker's parent
    procs = table(proc(860, 150, "veyyon.exe", BROKER, age_s=3600), proc(150, 10, "veyyon.exe", age_s=60))
    d, _ = run_classify(procs)
    assert actions(d)[860] == "reap"


def test_young_or_busy_orphan_broker_is_kept() -> None:
    procs = table(
        proc(870, 99971, "veyyon.exe", BROKER, age_s=600),  # younger than 30 min
        proc(871, 99972, "veyyon.exe", BROKER, age_s=3600, cpu=1.0),  # busy below
    )
    d, _ = run_classify(procs, resample={871: 4.0})
    assert actions(d) == {870: "keep", 871: "keep"}


def test_main_session_and_look_alikes_are_never_reaped_as_brokers() -> None:
    procs = table(
        proc(880, 99973, "veyyon.exe", r"C:\Users\x\AppData\Local\veyyon\veyyon.exe"),  # a Main, dead parent
        proc(881, 99974, "veyyon.exe", r"veyyon.exe __veyyon_worker_js_eval_process"),  # other worker
        proc(882, 99975, "python.exe", "python.exe __veyyon_worker_daemon_broker"),  # wrong executable
        proc(883, 99976, "veyyon.exe", r"veyyon.exe --note __veyyon_worker_daemon_broker_x"),  # longer token
    )
    d, _ = run_classify(procs)
    assert d == []


def test_live_run_kills_only_the_orphan_broker(tmp_path) -> None:
    procs = table(
        proc(890, 99977, "veyyon.exe", BROKER, age_s=7200, rss=190 * 1048576),  # orphan
        proc(891, 100, "veyyon.exe", BROKER, age_s=3000),  # live Main parent
    )
    result, killed = _run(tmp_path, procs, live=True)
    assert killed == [890]
    assert result["by_group"] == {"broker": 1} and result["freed_mb"] == 190.0
