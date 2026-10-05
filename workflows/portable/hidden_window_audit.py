#!/usr/bin/env python3
"""
hidden_window_audit.py - keep every unattended job off the operator's screen.

Operator rule (profile AGENTS.md section 13 item 10): no agent opens a window on the
operator's desktop. A scheduled task whose action is a console program (python.exe, cmd.exe,
a .cmd or .bat file, powershell.exe, node.exe, git.exe ...) opens a console window every time it
fires, because the task runs in the interactive session.

Verbs:
  audit        list scheduled tasks whose action runs a console host in the interactive session;
               exit 1 when there are any (the install step runs this)
  fix          re-register each offending task behind run_hidden.vbs (window style 0);
               --dry-run prints the plan
  watch        poll the desktop for NEW visible console windows for N seconds and log each one
               (process, parent chain, command line, all redacted); exit 1 when any appear
  scan-source  report subprocess/spawn/exec calls with no CREATE_NO_WINDOW / windowsHide

The test for "console host" is the executable's PE subsystem (CUI = console), not a name list,
so a new tool is caught without editing this file. wscript.exe and pythonw.exe are GUI programs.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

CREATE_NO_WINDOW = 0x08000000
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
HERE = Path(__file__).resolve().parent
HOME = Path(os.path.expanduser("~"))
RUN_DIR = HOME / ".veyyon" / "run"
LAUNCHER = RUN_DIR / "run_hidden.vbs"
LAUNCHER_SOURCE = HERE / "run_hidden.vbs"
WSCRIPT = r"%SystemRoot%\System32\wscript.exe"
CONSOLE_SCRIPT_SUFFIXES = (".bat", ".cmd")
CONSOLE_WINDOW_CLASSES = ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS")
SUBSYSTEM_CUI = 3


def _run(argv: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Every subprocess here is hidden: this script must never be the source of a window."""
    return subprocess.run(argv, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)


# --------------------------------------------------------------------------- redaction

_SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(token|secret|password|passwd|apikey|api[_-]key|pat|key)(\s*[=:]\s*|\s+)\S{6,}"),
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),
    re.compile(r"\b[A-Za-z0-9+/_\-]{40,}={0,2}"),
]


def redact(text: str) -> str:
    """Mask anything that looks like a credential. Paths and flags stay readable."""
    for pat in _SECRET_PATTERNS:
        if pat.groups >= 1 and pat.pattern.startswith("(?i)(token"):
            text = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
        elif pat.pattern.startswith(r"://"):
            text = pat.sub("://[redacted]@", text)
        else:
            text = pat.sub("[redacted]", text)
    return text


# --------------------------------------------------------------------------- console-host test

def pe_subsystem(path: str) -> Optional[int]:
    """Subsystem field of a Windows executable (2 = GUI, 3 = console), or None if unreadable."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(0x40)
            if head[:2] != b"MZ":
                return None
            (e_lfanew,) = struct.unpack_from("<I", head, 0x3C)
            fh.seek(e_lfanew)
            nt = fh.read(24 + 70)
            if nt[:4] != b"PE\0\0":
                return None
            (sub,) = struct.unpack_from("<H", nt, 24 + 68)
            return sub
    except OSError:
        return None


def resolve_exe(command: str) -> Optional[str]:
    """Expand env vars and PATH-search the way CreateProcess would for a task action."""
    cmd = os.path.expandvars(command.strip().strip('"'))
    if os.path.isabs(cmd) and os.path.exists(cmd):
        return cmd
    found = shutil.which(cmd)
    if found:
        return found
    if not os.path.splitext(cmd)[1]:
        return shutil.which(cmd + ".exe")
    return None


def classify_command(command: str) -> Tuple[bool, str]:
    """(runs_console_host, reason) for one task action command."""
    cmd = os.path.expandvars(command.strip().strip('"'))
    if cmd.lower().endswith(CONSOLE_SCRIPT_SUFFIXES):
        return True, "batch file runs in cmd.exe"
    exe = resolve_exe(cmd)
    if exe is None:
        return False, "executable not found; not judged"
    sub = pe_subsystem(exe)
    if sub == SUBSYSTEM_CUI:
        return True, f"{os.path.basename(exe)} is a console program"
    return False, "GUI program" if sub is not None else "not a PE file; not judged"


# --------------------------------------------------------------------------- scheduled tasks

@dataclass
class TaskAction:
    command: str
    arguments: str


@dataclass
class TaskInfo:
    name: str
    interactive: bool
    enabled: bool
    actions: List[TaskAction] = field(default_factory=list)
    xml: str = ""


@dataclass
class Finding:
    task: str
    command: str
    arguments: str
    reason: str
    enabled: bool


def parse_tasks_xml(blob: str) -> List[TaskInfo]:
    """Split `schtasks /query /xml ONE` output (one <Task> per `<!-- \\name -->` comment)."""
    tasks: List[TaskInfo] = []
    for m in re.finditer(r"<!--\s*(.*?)\s*-->\s*(<Task\b.*?</Task>)", blob, re.S):
        name, body = m.group(1), m.group(2)
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            continue
        logon = root.findtext("t:Principals/t:Principal/t:LogonType", default="", namespaces=NS)
        enabled = root.findtext("t:Settings/t:Enabled", default="true", namespaces=NS).strip().lower() != "false"
        actions = [
            TaskAction(e.findtext("t:Command", default="", namespaces=NS), e.findtext("t:Arguments", default="", namespaces=NS))
            for e in root.findall("t:Actions/t:Exec", NS)
        ]
        tasks.append(TaskInfo(name, logon == "InteractiveToken", enabled, actions, body))
    return tasks


def load_tasks() -> List[TaskInfo]:
    proc = _run(["schtasks", "/query", "/xml", "ONE"], timeout=180)
    blob = proc.stdout.decode("utf-16") if proc.stdout[:2] == b"\xff\xfe" else proc.stdout.decode("utf-8", "replace")
    return parse_tasks_xml(blob)


def is_ours_to_audit(task: TaskInfo) -> bool:
    """Windows' own tasks (\\Microsoft\\...) are not ours; everything else is judged by its action."""
    return not task.name.startswith("\\Microsoft\\")


def audit_tasks(tasks: Iterable[TaskInfo], include_disabled: bool = False) -> List[Finding]:
    found: List[Finding] = []
    for task in tasks:
        if not is_ours_to_audit(task) or not task.interactive:
            continue
        if not task.enabled and not include_disabled:
            continue
        for act in task.actions:
            bad, reason = classify_command(act.command)
            if bad:
                found.append(Finding(task.name, act.command, act.arguments, reason, task.enabled))
    return found


def hidden_tr(command_line: str) -> str:
    """The schtasks /tr string that runs `command_line` with window style 0 (installers use this).

    command_line is the program path, quoted if it has spaces, plus its arguments. The caller
    runs ensure_launcher() before it registers the task.
    """
    return f'wscript.exe "{LAUNCHER}" {command_line}'


def hidden_exec(command: str, arguments: str, launcher: Path = LAUNCHER) -> Tuple[str, str]:
    """The (Command, Arguments) pair that runs the same program with window style 0."""
    original = f'"{command}"' if " " in command and not command.startswith('"') else command
    return WSCRIPT, f'"{launcher}" {original} {arguments}'.rstrip()


def rewrite_task_xml(xml: str, launcher: Path = LAUNCHER) -> str:
    """Point every console-host <Exec> of a task XML at the hidden launcher."""
    def sub(m: "re.Match[str]") -> str:
        block = m.group(0)
        cmd = re.search(r"<Command>(.*?)</Command>", block, re.S)
        args = re.search(r"<Arguments>(.*?)</Arguments>", block, re.S)
        c = _unescape(cmd.group(1)) if cmd else ""
        a = _unescape(args.group(1)) if args else ""
        if not classify_command(c)[0]:
            return block
        new_c, new_a = hidden_exec(c.strip(), a, launcher)
        out = f"<Command>{_escape(new_c)}</Command>"
        out += f"<Arguments>{_escape(new_a)}</Arguments>"
        wd = re.search(r"<WorkingDirectory>.*?</WorkingDirectory>", block, re.S)
        return "<Exec>" + out + (wd.group(0) if wd else "") + "</Exec>"
    return re.sub(r"<Exec>.*?</Exec>", sub, xml, flags=re.S)


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _unescape(s: str) -> str:
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


def ensure_launcher() -> Path:
    LAUNCHER.parent.mkdir(parents=True, exist_ok=True)
    src = LAUNCHER_SOURCE.read_text(encoding="utf-8")
    if not LAUNCHER.exists() or LAUNCHER.read_text(encoding="utf-8") != src:
        LAUNCHER.write_text(src, encoding="utf-8")
    return LAUNCHER


def register_xml(name: str, xml: str) -> Tuple[bool, str]:
    if not xml.lstrip().startswith("<?xml"):
        xml = '<?xml version="1.0" encoding="UTF-16"?>\n' + xml
    with tempfile.NamedTemporaryFile("wb", suffix=".xml", delete=False) as fh:
        fh.write(b"\xff\xfe" + xml.encode("utf-16-le"))
        path = fh.name
    try:
        proc = _run(["schtasks", "/create", "/xml", path, "/tn", name.lstrip("\\"), "/f"])
        return proc.returncode == 0, (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
    finally:
        os.unlink(path)


# --------------------------------------------------------------------------- desktop watch

def _user32():
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u.EnumWindows.argtypes = [proc_t, wintypes.LPARAM]
    return ctypes, wintypes, u, proc_t


def visible_windows() -> Dict[int, Tuple[str, str, int]]:
    """hwnd -> (class, title, owner pid) for every visible top-level window."""
    ctypes, wintypes, u, proc_t = _user32()
    out: Dict[int, Tuple[str, str, int]] = {}

    def cb(hwnd, _lp):
        if u.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, cls, 256)
            title = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(hwnd, title, 256)
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out[int(hwnd)] = (cls.value, title.value, pid.value)
        return True

    u.EnumWindows(proc_t(cb), 0)
    return out


def is_console_window(cls: str) -> bool:
    return cls in CONSOLE_WINDOW_CLASSES


def describe_process(pid: int) -> Dict[str, object]:
    import psutil
    try:
        p = psutil.Process(pid)
        chain: List[Dict[str, object]] = []
        cur: Optional["psutil.Process"] = p
        for _ in range(4):
            if cur is None:
                break
            try:
                chain.append({"pid": cur.pid, "name": cur.name(), "cmdline": redact(" ".join(cur.cmdline()))[:400]})
                cur = cur.parent()
            except psutil.Error:
                break
        kids = []
        for c in p.children(recursive=False)[:5]:
            try:
                kids.append({"pid": c.pid, "name": c.name(), "cmdline": redact(" ".join(c.cmdline()))[:400]})
            except psutil.Error:
                pass
        return {"process": chain[0] if chain else {"pid": pid}, "ancestors": chain[1:], "children": kids}
    except psutil.Error as exc:
        return {"process": {"pid": pid}, "error": type(exc).__name__}


def watch(seconds: float, log_path: Path, interval: float = 0.05) -> List[Dict[str, object]]:
    """Log every console window that appears after the baseline. Returns the events."""
    baseline = set(visible_windows())
    seen = set(baseline)
    events: List[Dict[str, object]] = []
    log_path.parent.mkdir(parents=True, exist_ok=True)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for hwnd, (cls, title, pid) in visible_windows().items():
            if hwnd in seen:
                continue
            seen.add(hwnd)
            if not is_console_window(cls):
                continue
            ev = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "class": cls, "title": redact(title), **describe_process(pid)}
            events.append(ev)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev) + "\n")
        time.sleep(interval)
    return events


# --------------------------------------------------------------------------- source scan

PY_SPAWN = {"run", "Popen", "call", "check_call", "check_output"}
JS_SPAWN = re.compile(r"\b(spawn|spawnSync|execFile|execFileSync|exec|execSync)\s*\(")


def scan_python(path: Path) -> List[Tuple[int, str]]:
    """Lines that call subprocess.run/Popen/... without creationflags."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    hits: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr in PY_SPAWN and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess":
            if not any(k.arg == "creationflags" or k.arg is None for k in node.keywords):
                hits.append((node.lineno, f"subprocess.{fn.attr}"))
    return hits


def _call_span(text: str, start: int) -> str:
    depth = 0
    for i in range(start, min(len(text), start + 4000)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:start + 4000]


def scan_js(path: Path) -> List[Tuple[int, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    hits: List[Tuple[int, str]] = []
    for m in JS_SPAWN.finditer(text):
        before = text[max(0, m.start() - 1):m.start()]
        if before == ".":
            lead = text[max(0, m.start() - 12):m.start()]
            if "child_process." not in lead and "cp." not in lead and "Bun." not in lead:
                continue
        span = _call_span(text, m.end() - 1)
        if "windowsHide" not in span:
            hits.append((text.count("\n", 0, m.start()) + 1, m.group(1)))
    return hits


def scan_source(roots: Iterable[Path]) -> List[Tuple[str, int, str]]:
    out: List[Tuple[str, int, str]] = []
    skip = {"node_modules", ".git", ".artifacts", "output"}
    for root in roots:
        files = [root] if root.is_file() else (p for p in root.rglob("*") if p.is_file())
        for p in files:
            if skip & set(p.parts) or p.name.startswith("test_") or ".test." in p.name:
                continue
            if p.suffix == ".py":
                hits = scan_python(p)
            elif p.suffix in (".js", ".mjs", ".cjs", ".ts"):
                hits = scan_js(p)
            else:
                continue
            out.extend((str(p), ln, what) for ln, what in hits)
    return out


# --------------------------------------------------------------------------- CLI

def cmd_audit(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        print("hidden_window_audit: not Windows, nothing to audit")
        return 0
    findings = audit_tasks(load_tasks(), include_disabled=args.include_disabled)
    if args.json:
        print(json.dumps([f.__dict__ for f in findings], indent=2))
    for f in findings:
        print(f"VISIBLE-WINDOW {f.task}: {f.command} {f.arguments[:120]} ({f.reason})", file=sys.stderr)
    if findings:
        print(f"{len(findings)} scheduled task(s) open a console window. Run: python {Path(__file__).name} fix", file=sys.stderr)
        return 1
    print("hidden_window_audit: 0 scheduled tasks run a console host in the interactive session")
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    tasks = load_tasks()
    todo = [t for t in tasks if is_ours_to_audit(t) and t.interactive and any(classify_command(a.command)[0] for a in t.actions)]
    if args.only:
        todo = [t for t in todo if t.name.lstrip("\\") in args.only]
    if not todo:
        print("nothing to fix")
        return 0
    if not args.dry_run:
        ensure_launcher()
    rc = 0
    for t in todo:
        new_xml = rewrite_task_xml(t.xml)
        if args.dry_run:
            print(f"WOULD FIX {t.name}")
            continue
        ok, msg = register_xml(t.name, new_xml)
        print(f"{'FIXED' if ok else 'FAILED'} {t.name} {msg}")
        rc |= 0 if ok else 1
    return rc


def cmd_watch(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        return 0
    events = watch(args.seconds, Path(args.log))
    print(json.dumps({"seconds": args.seconds, "new_console_windows": len(events), "events": events}, indent=2))
    return 1 if events else 0


def cmd_scan(args: argparse.Namespace) -> int:
    hits = scan_source(Path(r) for r in args.roots)
    for f, ln, what in hits:
        print(f"{f}:{ln}: {what} without CREATE_NO_WINDOW/windowsHide")
    print(f"{len(hits)} unflagged spawn call(s)")
    return 1 if hits and args.strict else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="verb", required=True)
    a = sub.add_parser("audit")
    a.add_argument("--json", action="store_true")
    a.add_argument("--include-disabled", action="store_true")
    a.set_defaults(fn=cmd_audit)
    f = sub.add_parser("fix")
    f.add_argument("--dry-run", action="store_true")
    f.add_argument("--only", action="append", default=[], help="task name without leading backslash")
    f.set_defaults(fn=cmd_fix)
    w = sub.add_parser("watch")
    w.add_argument("--seconds", type=float, default=300)
    w.add_argument("--log", default=str(RUN_DIR / "hidden-window-watch.jsonl"))
    w.set_defaults(fn=cmd_watch)
    s = sub.add_parser("scan-source")
    s.add_argument("roots", nargs="+")
    s.add_argument("--strict", action="store_true")
    s.set_defaults(fn=cmd_scan)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
