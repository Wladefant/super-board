"""Behavior tests for hidden_window_audit: what counts as a window-opening task, how a fix
rewrites it, and what the source scan flags."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hidden_window_audit as hwa  # noqa: E402

HERE = Path(__file__).resolve().parent
WIN = sys.platform == "win32"
SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"


def task_xml(name: str, command: str, arguments: str = "", logon: str = "InteractiveToken", enabled: bool = True) -> str:
    return f"""<!-- {name} -->
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals><Principal id="Author"><LogonType>{logon}</LogonType></Principal></Principals>
  <Settings><Enabled>{'true' if enabled else 'false'}</Enabled></Settings>
  <Actions Context="Author"><Exec><Command>{command}</Command><Arguments>{arguments}</Arguments></Exec></Actions>
</Task>
"""


def audit(blob: str, **kw):
    return hwa.audit_tasks(hwa.parse_tasks_xml("<Tasks>" + blob + "</Tasks>"), **kw)


def test_batch_file_task_in_interactive_session_is_flagged() -> None:
    found = audit(task_xml("\\SuperboardUpkeepHourly", r"C:\run\upkeep_hourly.cmd"))
    assert [f.task for f in found] == ["\\SuperboardUpkeepHourly"]


def test_only_interactive_enabled_non_windows_tasks_are_judged() -> None:
    blob = (
        task_xml("\\Microsoft\\Windows\\X", r"C:\run\a.cmd")
        + task_xml("\\SystemJob", r"C:\run\b.cmd", logon="ServiceAccount")
        + task_xml("\\Off", r"C:\run\c.bat", enabled=False)
    )
    assert audit(blob) == []
    assert [f.task for f in audit(blob, include_disabled=True)] == ["\\Off"]


@pytest.mark.skipif(not WIN, reason="PE subsystem check needs Windows executables")
def test_console_and_gui_programs_are_told_apart_by_their_subsystem() -> None:
    assert hwa.classify_command(sys.executable)[0] is True
    assert hwa.classify_command(str(SYSTEM32 / "cmd.exe"))[0] is True
    assert hwa.classify_command(str(SYSTEM32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"))[0] is True
    assert hwa.classify_command(str(SYSTEM32 / "wscript.exe"))[0] is False
    assert hwa.classify_command(str(SYSTEM32 / "notepad.exe"))[0] is False


@pytest.mark.skipif(not WIN, reason="needs Windows executables")
def test_fix_rewrites_a_console_task_so_the_audit_passes_and_keeps_the_command() -> None:
    original = task_xml("\\T", str(SYSTEM32 / "cmd.exe"), r'/c echo "a b" c')
    launcher = Path(r"C:\Users\x\.veyyon\run\run_hidden.vbs")
    fixed = hwa.rewrite_task_xml(original, launcher)
    assert audit(fixed) == []
    tasks = hwa.parse_tasks_xml("<Tasks>" + fixed + "</Tasks>")
    act = tasks[0].actions[0]
    assert act.command.lower().endswith("wscript.exe")
    assert act.arguments == f'"{launcher}" {SYSTEM32 / "cmd.exe"} /c echo "a b" c'


@pytest.mark.skipif(not WIN, reason="needs Windows executables")
def test_fix_leaves_a_gui_task_untouched() -> None:
    xml = task_xml("\\Ok", str(SYSTEM32 / "wscript.exe"), r'"C:\x\run_hidden.vbs" "C:\a.cmd"')
    assert hwa.rewrite_task_xml(xml) == xml


def test_hidden_exec_quotes_a_command_path_with_spaces() -> None:
    cmd, args = hwa.hidden_exec(r"C:\Program Files\Git\cmd\git.exe", "pull", Path(r"C:\l\run_hidden.vbs"))
    assert args == r'"C:\l\run_hidden.vbs" "C:\Program Files\Git\cmd\git.exe" pull'
    assert cmd.lower().endswith("wscript.exe")


def test_redact_masks_tokens_but_keeps_paths_and_flags() -> None:
    line = r"python C:\x\gardener.py --live --token ghp_" + "a" * 30 + " https://u:pw@host/x password=hunter22"
    out = hwa.redact(line)
    assert "ghp_" not in out and "hunter22" not in out and "u:pw@" not in out
    assert r"C:\x\gardener.py --live" in out


def test_python_scan_flags_calls_without_creationflags(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(
        "import subprocess\n"
        "subprocess.run(['git'])\n"
        "subprocess.run(['git'], creationflags=0x08000000)\n"
        "subprocess.check_output(['y'], timeout=3)\n"
    )
    assert [(ln, what) for ln, what in hwa.scan_python(f)] == [(2, "subprocess.run"), (4, "subprocess.check_output")]


def test_python_scan_sees_aliases_from_imports_os_system_and_kwargs_splat(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(
        "import subprocess as sp\n"
        "import os\n"
        "from subprocess import run, Popen as P\n"
        "sp.run(['a'])\n"
        "run(['b'])\n"
        "P(['c'])\n"
        "os.system('d')\n"
        "sp.Popen(['e'], **opts)\n"
        "sp.run(['f'], creationflags=1)\n"
    )
    hits = hwa.scan_python(f)
    assert [ln for ln, _ in hits] == [4, 5, 6, 7, 8]
    assert "os.system" in hits[3][1] and "**kwargs" in hits[4][1]


def test_python_scan_allows_a_call_with_a_marked_reason(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("import subprocess\nsubprocess.run(['a'], **o)  # hidden-window-ok: o carries creationflags\n")
    assert hwa.scan_python(f) == []


def test_unparseable_python_file_is_a_finding_not_a_clean_scan(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    f.write_text("def broken(:\n")
    assert [what for _, what in hwa.scan_python(f)] == ["unparseable file: not scanned"]


def test_source_scan_covers_test_files(tmp_path: Path) -> None:
    (tmp_path / "test_x.py").write_text("import subprocess\nsubprocess.run(['a'])\n")
    (tmp_path / "y.test.ts").write_text("Bun.spawnSync(['a']);\n")
    assert sorted(Path(p).name for p, _, _ in hwa.scan_source([tmp_path])) == ["test_x.py", "y.test.ts"]


def test_scan_source_command_fails_by_default_and_report_only_passes(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import subprocess\nsubprocess.run(['a'])\n")
    assert hwa.main(["scan-source", str(tmp_path)]) == 1
    assert hwa.main(["scan-source", "--report-only", str(tmp_path)]) == 0


def test_repository_has_no_unhidden_process_spawns() -> None:
    root = HERE.parents[1]
    roots = [root / d for d in ("workflows", "scripts", "packages", "managed-skills", "infra", "skills", "tools", "tests") if (root / d).exists()]
    assert hwa.scan_source(roots) == []


def test_malformed_task_xml_is_a_finding() -> None:
    blob = "<Tasks><!-- \\Broken -->\n<Task version=\"1.2\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\"><Actions></Task>\n</Tasks>"
    found = hwa.audit_tasks(hwa.parse_tasks_xml(blob))
    assert [f.task for f in found] == ["\\Broken"] and "does not parse" in found[0].reason


def test_task_header_without_a_task_body_is_a_finding() -> None:
    found = hwa.audit_tasks(hwa.parse_tasks_xml("<Tasks><!-- \\Half -->\n<Task version=\"1.2\">"))
    assert [f.task for f in found] == ["\\Half"]


def test_unresolvable_executable_is_flagged_not_skipped() -> None:
    bad, reason = hwa.classify_command(r"C:\no\such\dir\tool.exe")
    assert bad is True and "cannot prove" in reason


class _Proc:
    def __init__(self, rc: int, out: bytes = b"", err: bytes = b"") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_empty_schtasks_output_fails_the_audit(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(hwa, "_run", lambda argv, timeout=60: _Proc(0, b""))
    monkeypatch.setattr(sys, "platform", "win32")
    assert hwa.main(["audit"]) == 2
    assert "cannot audit" in capsys.readouterr().err


def test_failed_schtasks_query_fails_the_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hwa, "_run", lambda argv, timeout=60: _Proc(1, b"", b"Access is denied"))
    monkeypatch.setattr(sys, "platform", "win32")
    assert hwa.main(["audit"]) == 2


def test_fix_refuses_to_run_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert hwa.main(["fix", "--dry-run"]) == 2


def test_hidden_tr_rejects_a_command_past_the_schtasks_limit() -> None:
    with pytest.raises(ValueError, match="261"):
        hwa.hidden_tr("python.exe " + "x" * 300)


@pytest.mark.skipif(not WIN, reason="runs wscript.exe")
@pytest.mark.parametrize(
    "arg_line, expected",
    [
        (r'plain "a b" ""', ["plain", "a b", ""]),
        (r'"say \"hi\"" 100%', ['say "hi"', "100%"]),
        (r'"C:\dir with space\\" x', ["C:\\dir with space\\", "x"]),
        (r"%22 %25", ["%22", "%25"]),
    ],
)
def test_hidden_launcher_hands_the_child_the_exact_argv(tmp_path: Path, arg_line: str, expected: list) -> None:
    import json
    import subprocess

    out = tmp_path / "argv.json"
    child = tmp_path / "child.py"
    child.write_text(f"import sys, json\njson.dump(sys.argv[1:], open({str(out)!r}, 'w'))\n")
    _, args = hwa.hidden_exec(sys.executable, f'"{child}" {arg_line}', HERE / "run_hidden.vbs")
    subprocess.run(f"wscript.exe {args}", timeout=60, creationflags=0x08000000, stdin=subprocess.DEVNULL)
    assert json.loads(out.read_text()) == expected


def test_js_scan_flags_calls_without_windows_hide(tmp_path: Path) -> None:
    f = tmp_path / "a.mjs"
    f.write_text(
        "import { execFileSync, spawn } from 'node:child_process';\n"
        "execFileSync('gh', ['api', 'x'], { encoding: 'utf8' });\n"
        "execFileSync('gh', ['api', 'y'], {\n  encoding: 'utf8',\n  windowsHide: true,\n});\n"
        "spawn('git', ['status']);\n"
        "re.exec('abc');\n"
    )
    assert [(ln, what) for ln, what in hwa.scan_js(f)] == [(2, "execFileSync"), (7, "spawn")]
