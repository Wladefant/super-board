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
  scan-source  report subprocess/spawn/exec calls with no CREATE_NO_WINDOW / windowsHide;
               exit 1 on any (a call may carry a `hidden-window-ok: <reason>` comment)

The test for "console host" is the executable's PE subsystem (CUI = console), not a name list,
so a new tool is caught without editing this file. wscript.exe and pythonw.exe are GUI programs.
"""

from __future__ import annotations

import argparse
import ast
import ctypes
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


class AuditError(RuntimeError):
    """The audit could not see the task list. It must fail, never pass."""


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
        return True, "executable not found; cannot prove it is hidden"
    sub = pe_subsystem(exe)
    if sub == SUBSYSTEM_CUI:
        return True, f"{os.path.basename(exe)} is a console program"
    if sub is None:
        return True, f"{os.path.basename(exe)} is not a readable PE file; cannot prove it is hidden"
    return False, "GUI program"


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
    error: str = ""


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
    matched = set()
    for m in re.finditer(r"<!--\s*(.*?)\s*-->\s*(<Task\b.*?</Task>)", blob, re.S):
        name, body = m.group(1), m.group(2)
        matched.add(name)
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            tasks.append(TaskInfo(name, True, True, [], body, error=f"task XML does not parse: {exc}"))
            continue
        logon = root.findtext("t:Principals/t:Principal/t:LogonType", default="", namespaces=NS)
        enabled = root.findtext("t:Settings/t:Enabled", default="true", namespaces=NS).strip().lower() != "false"
        actions = [
            TaskAction(e.findtext("t:Command", default="", namespaces=NS), e.findtext("t:Arguments", default="", namespaces=NS))
            for e in root.findall("t:Actions/t:Exec", NS)
        ]
        tasks.append(TaskInfo(name, logon == "InteractiveToken", enabled, actions, body))
    for m in re.finditer(r"<!--\s*(\\[^>]*?)\s*-->", blob):
        if m.group(1) not in matched:
            tasks.append(TaskInfo(m.group(1), True, True, [], "", error="task header has no complete <Task> body"))
    return tasks


def load_tasks() -> List[TaskInfo]:
    proc = _run(["schtasks", "/query", "/xml", "ONE"], timeout=180)
    if proc.returncode != 0:
        raise AuditError(f"schtasks /query failed with exit {proc.returncode}: {proc.stderr.decode('utf-8', 'replace').strip()[:300]}")
    blob = proc.stdout.decode("utf-16") if proc.stdout[:2] == b"\xff\xfe" else proc.stdout.decode("utf-8", "replace")
    tasks = parse_tasks_xml(blob)
    if not tasks:
        raise AuditError("schtasks /query returned no tasks; refusing to report a clean audit")
    return tasks


def is_ours_to_audit(task: TaskInfo) -> bool:
    """Windows' own tasks (\\Microsoft\\...) are not ours; everything else is judged by its action."""
    return not task.name.startswith("\\Microsoft\\")


def audit_tasks(tasks: Iterable[TaskInfo], include_disabled: bool = False) -> List[Finding]:
    found: List[Finding] = []
    for task in tasks:
        if not is_ours_to_audit(task):
            continue
        if task.error:
            found.append(Finding(task.name, "", "", task.error, task.enabled))
            continue
        if not task.interactive:
            continue
        if not task.enabled and not include_disabled:
            continue
        for act in task.actions:
            bad, reason = classify_command(act.command)
            if bad:
                found.append(Finding(task.name, act.command, act.arguments, reason, task.enabled))
    return found


TR_LIMIT = 261  # schtasks /tr rejects longer strings


def split_command_line(line: str) -> List[str]:
    """argv as CommandLineToArgvW (and so every Windows program) would split `line`."""
    if not line.strip():
        return []
    if sys.platform == "win32":
        shell32 = ctypes.windll.shell32
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
        n = ctypes.c_int(0)
        arr = shell32.CommandLineToArgvW("x " + line, ctypes.byref(n))
        try:
            return [arr[k] for k in range(1, n.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(arr)
    import shlex
    return shlex.split(line, posix=False)


def _launcher_arg(arg: str) -> str:
    """One argv entry as run_hidden.vbs receives it. WScript cannot carry a literal quote,
    so `"` travels as %22 and `%` as %25; the launcher decodes both. Empty or spaced
    entries are wrapped in quotes."""
    enc = arg.replace("%", "%25").replace('"', "%22")
    return f'"{enc}"' if enc == "" or " " in enc or "\t" in enc else enc


def _launcher_args(command_line: str) -> str:
    return " ".join(_launcher_arg(a) for a in split_command_line(command_line))


def hidden_tr(command_line: str) -> str:
    """The schtasks /tr string that runs `command_line` with window style 0 (installers use this).

    command_line is the program path, quoted if it has spaces, plus its arguments. The caller
    runs ensure_launcher() before it registers the task. Raises ValueError past schtasks' limit.
    """
    tr = f'wscript.exe "{LAUNCHER}" {_launcher_args(command_line)}'
    if len(tr) > TR_LIMIT:
        raise ValueError(f"schtasks /tr is {len(tr)} characters; the limit is {TR_LIMIT}. Shorten the command line.")
    return tr


def hidden_exec(command: str, arguments: str, launcher: Path = LAUNCHER) -> Tuple[str, str]:
    """The (Command, Arguments) pair that runs the same program with window style 0."""
    head = _launcher_arg(command.strip().strip('"'))
    tail = _launcher_args(arguments)
    return WSCRIPT, f'"{launcher}" {head} {tail}'.rstrip()


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

PY_SPAWN = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
OS_SPAWN = {"system", "popen"}
JS_SPAWN = re.compile(r"\b(spawn|spawnSync|execFile|execFileSync|exec|execSync)\s*\(")
ALLOW_MARK = "hidden-window-ok:"  # a call carrying this comment, with a reason, is skipped


def _marked(lines: List[str], first: int, last: int) -> bool:
    return any(ALLOW_MARK in ln for ln in lines[max(0, first - 2):last])


def spawn_calls(tree: ast.AST) -> List[Tuple[ast.Call, str, bool]]:
    """Every process-starting call in a parsed file: (call node, name, cannot_take_flags)."""
    mods, funcs, os_mods, os_funcs = set(), {}, set(), {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "subprocess":
                    mods.add(a.asname or "subprocess")
                elif a.name == "os":
                    os_mods.add(a.asname or "os")
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            for a in node.names:
                if node.module == "subprocess" and a.name in PY_SPAWN:
                    funcs[a.asname or a.name] = a.name
                elif node.module == "os" and a.name in OS_SPAWN:
                    os_funcs[a.asname or a.name] = a.name
    found: List[Tuple[ast.Call, str, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            if fn.value.id in mods and fn.attr in PY_SPAWN:
                found.append((node, f"subprocess.{fn.attr}", False))
            elif fn.value.id in os_mods and fn.attr in OS_SPAWN:
                found.append((node, f"os.{fn.attr}", True))
        elif isinstance(fn, ast.Name):
            if fn.id in funcs:
                found.append((node, f"subprocess.{funcs[fn.id]}", False))
            elif fn.id in os_funcs:
                found.append((node, f"os.{os_funcs[fn.id]}", True))
    return found


def scan_python(path: Path) -> List[Tuple[int, str]]:
    """Lines that start a process without creationflags, or that the scan cannot read."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        return [] if ALLOW_MARK in text else [(getattr(exc, "lineno", None) or 1, "unparseable file: not scanned")]
    hits: List[Tuple[int, str]] = []
    for node, what, flagless in spawn_calls(tree):
        if _marked(lines, node.lineno, getattr(node, "end_lineno", node.lineno)):
            continue
        if flagless:
            hits.append((node.lineno, f"{what} cannot hide its window; use subprocess with creationflags"))
        elif not any(k.arg == "creationflags" for k in node.keywords):
            hits.append((node.lineno, what + (" with **kwargs (flags unproven)" if any(k.arg is None for k in node.keywords) else "")))
    return sorted(hits)


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
        line_no = text.count("\n", 0, m.start()) + 1
        if "windowsHide" not in span and ALLOW_MARK not in span and ALLOW_MARK not in "\n".join(text.splitlines()[max(0, line_no - 2):line_no]):
            hits.append((line_no, m.group(1)))
    return hits


def scan_source(roots: Iterable[Path]) -> List[Tuple[str, int, str]]:
    out: List[Tuple[str, int, str]] = []
    skip = {"node_modules", ".git", ".artifacts", "output"}
    for root in roots:
        files = [root] if root.is_file() else (p for p in root.rglob("*") if p.is_file())
        for p in files:
            if skip & set(p.parts):
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
    try:
        findings = audit_tasks(load_tasks(), include_disabled=args.include_disabled)
    except AuditError as exc:
        print(f"hidden_window_audit: cannot audit: {exc}", file=sys.stderr)
        return 2
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
    if sys.platform != "win32":
        print("hidden_window_audit fix: Windows only", file=sys.stderr)
        return 2
    try:
        tasks = load_tasks()
    except AuditError as exc:
        print(f"hidden_window_audit: cannot fix: {exc}", file=sys.stderr)
        return 2
    broken = [t for t in tasks if is_ours_to_audit(t) and t.error]
    for t in broken:
        print(f"CANNOT FIX {t.name}: {t.error}", file=sys.stderr)
    todo = [t for t in tasks if is_ours_to_audit(t) and not t.error and t.interactive and any(classify_command(a.command)[0] for a in t.actions)]
    if args.only:
        todo = [t for t in todo if t.name.lstrip("\\") in args.only]
    if not todo:
        print("nothing to fix")
        return 1 if broken else 0
    if not args.dry_run:
        ensure_launcher()
    rc = 1 if broken else 0
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
    return 1 if hits and not args.report_only else 0


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
    s.add_argument("--report-only", action="store_true", help="print findings but exit 0")
    s.set_defaults(fn=cmd_scan)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
