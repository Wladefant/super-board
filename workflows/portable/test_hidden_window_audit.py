"""Behavior tests for hidden_window_audit: what counts as a window-opening task, how a fix
rewrites it, and what the source scan flags."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hidden_window_audit as hwa  # noqa: E402

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
        "subprocess.Popen(['x'], **opts)\n"
        "subprocess.check_output(['y'], timeout=3)\n"
    )
    assert [(ln, what) for ln, what in hwa.scan_python(f)] == [(2, "subprocess.run"), (5, "subprocess.check_output")]


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
