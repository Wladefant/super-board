#!/usr/bin/env python3
r"""Remove a git worktree without following links into shared trees (workflows/wt_remove.py).

Git for Windows (checked with 2.49.0) follows directory junctions during
``git worktree remove --force`` and deletes the *target's* contents. Lane
worktrees link ``frontend/node_modules`` (PolySimulator) or ``node_modules``
(Veyyon) to a tree every other lane shares, so one careless removal empties it
for everyone (PolySimulator incidents 2026-09-27 ~13:50Z and ~16:35-16:55Z).

This tool removes a worktree in the only safe order:

1. refuse the main worktree and any path that is not a linked worktree root;
2. refuse when another worktree's junction points *into* this one (removing it
   would break those lanes), unless ``--orphan-dependents`` is given;
3. refuse, before touching anything, a worktree git would not remove: a locked one,
   or without ``--force`` one with modified or untracked files (the links this tool
   unlinks aside) or with a tracked link, which unlinking would itself turn into a change;
   also refuse, even with ``--force``, a worktree whose HEAD is not reachable from an
   integration ref (``origin/staging``, ``origin/main``, ``staging``, ``main``): removing
   it would orphan unmerged commits. ``--allow-unmerged`` overrides that refusal;
4. delete every junction/symlink inside the worktree as a link (``RemoveDirectoryW``
   on the link, never recursing into it), then verify none is left;
5. only then run ``git --git-dir=<common> worktree remove <top>``.

Usage:
    python wt_remove.py <worktree> [--force] [--dry-run] [--orphan-dependents] [--allow-unmerged] [--json]

``--force`` is passed through to ``git worktree remove`` (needed for dirty
worktrees). Exit codes: 0 removed (or dry run clean), 2 refused, 1 error.

The helpers here (reparse-point walk, junction targets, worktree discovery,
process working directories) are shared with ``polysim_frontend_deps.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Iterator

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_DIRECTORY = 0x10

HOME = Path(os.path.expanduser("~"))
# Directories whose children are lane worktrees / checkouts that may hold links.
DEFAULT_SCAN_ROOTS = (
    HOME / "development",
    HOME / ".veyyon" / "profiles" / "default" / "wt",
)
# Relative paths inside a worktree where lanes put shared-tree links.
LINK_SLOTS = ("frontend/node_modules", "node_modules", "frontend/.next/cache")
# Integration branches checked for reachability before worktree removal (Issue #334)
INTEGRATION_REFS = ("origin/staging", "origin/main", "staging", "main")


# ---------------------------------------------------------------------------
# Path and link helpers
# ---------------------------------------------------------------------------


def norm(path: os.PathLike[str] | str) -> str:
    r"""Absolute, case-folded, backslash path without a ``\\?\`` prefix."""
    s = os.fspath(path)
    if s.startswith("\\\\?\\"):
        s = s[4:]
    return os.path.normcase(os.path.abspath(s))


def is_within(child: str, parent: str) -> bool:
    child_n, parent_n = norm(child), norm(parent)
    return child_n == parent_n or child_n.startswith(parent_n.rstrip("\\/") + os.sep)


def is_link(path: os.PathLike[str] | str) -> bool:
    """True for junctions and symlinks (any reparse point), without following it."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _verbatim_to_abs(raw: str) -> str:
    r"""Plain absolute path for a verbatim link target: ``\\?\C:\x`` loses its
    ``\\?\`` prefix and ``\\?\UNC\server\share`` becomes ``\\server\share``."""
    if raw.startswith("\\\\?\\UNC\\"):
        return "\\\\" + raw[8:]
    if raw.startswith("\\\\?\\"):
        return raw[4:]
    return raw


def link_target(path: os.PathLike[str] | str) -> str | None:
    """Normalized target of a junction/symlink, or None when it is not readable."""
    try:
        raw = os.readlink(path)
    except (OSError, ValueError):
        return None
    raw = _verbatim_to_abs(raw)
    if not os.path.isabs(raw):
        raw = os.path.join(os.path.dirname(os.fspath(path)), raw)
    return norm(raw)


def unlink_link(path: os.PathLike[str] | str) -> None:
    """Delete a link itself. Directory links go through rmdir, which never recurses."""
    st = os.lstat(path)
    if getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_DIRECTORY or stat.S_ISDIR(st.st_mode):
        os.rmdir(path)
    else:
        os.unlink(path)


def iter_links(root: os.PathLike[str] | str, errors: list[str] | None = None) -> Iterator[str]:
    """Yield every junction/symlink under ``root`` without descending into any of them.

    A directory that cannot be scanned (permissions) is reported in ``errors`` when given,
    instead of being silently skipped: a link hiding there must not make a removal or the
    leftover check look clean.
    """
    stack = [os.fspath(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if is_link(entry.path):
                        yield entry.path
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        continue
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            if errors is not None:
                errors.append(f"{current}: {exc}")
            continue


# ---------------------------------------------------------------------------
# Worktree discovery and dependents
# ---------------------------------------------------------------------------


def candidate_worktrees(roots: Iterable[Path] = DEFAULT_SCAN_ROOTS) -> Iterator[Path]:
    """Direct children of the scan roots that look like checkouts (have ``.git``)."""
    for root in roots:
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False) and os.path.lexists(os.path.join(entry.path, ".git")):
                        yield Path(entry.path)
        except OSError:
            continue


def slot_links(roots: Iterable[Path] = DEFAULT_SCAN_ROOTS) -> Iterator[tuple[Path, str | None]]:
    """(link path, normalized target) for every shared-tree link slot in known worktrees."""
    for wt in candidate_worktrees(roots):
        for slot in LINK_SLOTS:
            p = wt / slot
            if is_link(p):
                yield p, link_target(p)


def dependents_of(path: os.PathLike[str] | str, roots: Iterable[Path] = DEFAULT_SCAN_ROOTS) -> list[tuple[str, str]]:
    """Links outside ``path`` whose target lies inside ``path``."""
    out: list[tuple[str, str]] = []
    for link, target in slot_links(roots):
        if target and is_within(target, os.fspath(path)) and not is_within(os.fspath(link), os.fspath(path)):
            out.append((os.fspath(link), target))
    return out


# ---------------------------------------------------------------------------
# Process working directories (Windows, ctypes; no psutil dependency)
# ---------------------------------------------------------------------------


def process_paths(stats: dict | None = None) -> list[tuple[int, int, str, str, str]]:
    """(pid, parent pid, exe name, cwd, command line) for every readable process. Windows only.

    ``stats``, when given, receives ``platform``, ``total`` and ``read`` so callers can tell
    a full scan from a partial one (non-Windows, or processes that cannot be opened).
    """
    if sys.platform != "win32":
        if stats is not None:
            stats.update(platform=sys.platform, total=None, read=0)
        return []
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")

    TH32CS_SNAPPROCESS = 0x2
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    class PROCESS_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("Reserved1", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("Reserved2", ctypes.c_void_p * 2),
            ("UniqueProcessId", ctypes.c_void_p),
            ("Reserved3", ctypes.c_void_p),
        ]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

    def read(handle: int, addr: int, size: int) -> bytes | None:
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        if not kernel32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)) or got.value != size:
            return None
        return buf.raw

    def read_unicode_string(handle: int, addr: int) -> str:
        head = read(handle, addr, 16)  # USHORT Length, USHORT Max, pad, PWSTR Buffer (x64)
        if not head:
            return ""
        length = int.from_bytes(head[0:2], "little")
        buf_ptr = int.from_bytes(head[8:16], "little")
        if not length or not buf_ptr:
            return ""
        data = read(handle, buf_ptr, length)
        return data.decode("utf-16-le", "replace") if data else ""

    results: list[tuple[int, int, str, str, str]] = []
    total = 0
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        if stats is not None:
            stats.update(platform="win32", total=None, read=0)
        return results
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            total += 1
            pid, ppid, name = entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile
            handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid) if pid else None
            if handle:
                try:
                    pbi = PROCESS_BASIC_INFORMATION()
                    if ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None) == 0 and pbi.PebBaseAddress:
                        params = read(handle, pbi.PebBaseAddress + 0x20, 8)
                        if params:
                            pp = int.from_bytes(params, "little")
                            cwd = read_unicode_string(handle, pp + 0x38)
                            cmd = read_unicode_string(handle, pp + 0x70)
                            results.append((pid, ppid, name, cwd, cmd))
                finally:
                    kernel32.CloseHandle(handle)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    if stats is not None:
        stats.update(platform="win32", total=total, read=len(results))
    return results


def _cmd_mentions(cmd: str, target: str) -> bool:
    """True when ``target`` appears in ``cmd`` at a path boundary. A sibling directory with
    a shared prefix (``...\\wt-foo-2`` naming vs ``...\\wt-foo``) never matches: the match
    must end at a separator, quote, ``=`` or whitespace, or the string edge."""
    cmdn = os.path.normcase(cmd.replace("/", "\\"))
    start = 0
    while True:
        i = cmdn.find(target, start)
        if i < 0:
            return False
        j = i + len(target)
        before = cmdn[i - 1] if i else ""
        after = cmdn[j] if j < len(cmdn) else ""
        if (not before or before in '\\"= \t') and (not after or after in '\\"= \t'):
            return True
        start = i + 1


def busy_processes(path: os.PathLike[str] | str, procs: list[tuple[int, int, str, str, str]] | None = None) -> list[tuple[int, str, str]]:
    """Processes whose cwd lies inside ``path`` or whose command line names it.

    The calling process and its whole ancestor chain are skipped: a wrapper such as
    ``cmd /c python wt_remove.py <wt>`` repeats the path in its own command line, and
    so do the py launcher and venv redirectors.
    """
    target = norm(path)
    rows = list(procs) if procs is not None else process_paths()
    parents = {pid: ppid for pid, ppid, *_rest in rows if ppid}
    own = os.getpid()
    ancestors: set[int] = set()
    cursor = own
    while cursor in parents and cursor not in ancestors:
        cursor = parents[cursor]
        ancestors.add(cursor)
    hits: list[tuple[int, str, str]] = []
    for pid, _ppid, name, cwd, cmd in rows:
        if pid == own or pid in ancestors:
            continue
        if (cwd and is_within(cwd, target)) or _cmd_mentions(cmd, target):
            hits.append((pid, name, cwd))
    return hits


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def git(args: list[str], cwd: str | None = None, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def worktree_facts(path: str) -> tuple[str, str, str]:
    """(toplevel, git-dir, common-dir) as normalized absolute paths."""
    r = git(["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir", "--git-common-dir"], cwd=path)
    if r.returncode != 0:
        raise RuntimeError(f"not a git worktree: {path}: {r.stderr.strip()}")
    top, gdir, common = (norm(line) for line in r.stdout.strip().splitlines()[:3])
    return top, gdir, common


def is_locked(top: str) -> bool:
    """True when ``git worktree lock`` holds the worktree rooted at ``top``."""
    r = git(["worktree", "list", "--porcelain"], cwd=top)
    if r.returncode != 0:
        raise RuntimeError(f"git worktree list failed: {r.stderr.strip()}")
    for block in r.stdout.split("\n\n"):
        lines = block.splitlines()
        if lines and lines[0].startswith("worktree ") and norm(lines[0][len("worktree "):]) == top:
            return any(line == "locked" or line.startswith("locked ") for line in lines[1:])
    return False


def _holds_only_links(root: str) -> bool:
    """True when every entry under ``root`` (never descending into links) is itself a link,
    so unlinking the links leaves nothing git would call dirty. Unreadable -> False."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if is_link(entry.path):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        return False
        except OSError:
            return False
    return True


def check_merged_into(top: str, head: str) -> list[str]:
    """Return existing integration refs that contain head (from which head is reachable)."""
    merged: list[str] = []
    for ref in INTEGRATION_REFS:
        chk = git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=top)
        if chk.returncode == 0:
            anc = git(["merge-base", "--is-ancestor", head, ref], cwd=top)
            if anc.returncode == 0:
                merged.append(ref)
    return merged


def dirty_paths(top: str, links: list[str]) -> list[str]:
    """What makes ``git worktree remove`` refuse without --force, once ``links`` are unlinked:
    modified or untracked paths other than those links, plus any link git tracks (unlinking a
    tracked link deletes a tracked file)."""
    rel = {os.path.normcase(os.path.relpath(link, top)): link for link in links}
    r = git(["status", "--porcelain=v1", "-z"], cwd=top)
    if r.returncode != 0:
        raise RuntimeError(f"git status failed: {r.stderr.strip()}")
    entries = r.stdout.split("\0")
    dirty, i = [], 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        if entry[0] in "RC":  # a rename/copy carries its source path as the next entry
            i += 1
        path = entry[3:]
        if os.path.normcase(os.path.normpath(path.rstrip("/"))) in rel:
            continue
        if path.endswith("/") and _holds_only_links(os.path.join(top, path.rstrip("/\\"))):
            continue  # untracked directory whose only content is a link this tool unlinks
        dirty.append(path)
    if rel:
        tracked = git(["--literal-pathspecs", "ls-files", "-z", "--", *(os.path.relpath(link, top) for link in links)], cwd=top)
        if tracked.returncode != 0:
            raise RuntimeError(f"git ls-files failed: {tracked.stderr.strip()}")
        dirty += [f"{p} (tracked link)" for p in tracked.stdout.split("\0") if p]
    return dirty


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def remove_worktree(
    path: str,
    force: bool,
    dry_run: bool,
    orphan_dependents: bool,
    roots: Iterable[Path] = DEFAULT_SCAN_ROOTS,
    allow_unmerged: bool = False,
) -> dict:
    report: dict = {"worktree": norm(path), "unlinked": [], "dependents": [], "busy": [], "removed": False}
    if not os.path.isdir(path):
        report["error"] = "path does not exist or is not a directory"
        report["refused"] = True
        return report
    top, gdir, common = worktree_facts(path)
    if top != norm(path):
        report.update(refused=True, error=f"path is not a worktree root (toplevel is {top})")
        return report
    if gdir == common:
        report.update(refused=True, error="refusing to remove the main worktree")
        return report

    r_head = git(["rev-parse", "HEAD"], cwd=top)
    if r_head.returncode != 0:
        report.update(refused=True, error=f"unable to determine HEAD of worktree: {r_head.stderr.strip()}")
        return report
    head = r_head.stdout.strip()
    report["head"] = head

    r_sym = git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=top)
    branch = r_sym.stdout.strip() if r_sym.returncode == 0 else "(detached)"
    report["branch"] = branch

    merged_into = check_merged_into(top, head)
    report["merged_into"] = merged_into

    report["dependents"] = [{"link": link, "target": target} for link, target in dependents_of(path, roots)]
    if report["dependents"] and not orphan_dependents:
        report.update(refused=True, error="other worktrees link into this one; relink them first (polysim_frontend_deps.py link) or pass --orphan-dependents")
        return report

    scan: dict = {}
    report["busy"] = [{"pid": pid, "name": name, "cwd": cwd} for pid, name, cwd in busy_processes(path, process_paths(scan))]
    report["process_scan"] = {
        "platform": scan.get("platform", sys.platform),
        "read": scan.get("read", 0),
        "total": scan.get("total"),
        "partial": scan.get("total") is None or scan.get("read", 0) < scan.get("total", 0),
    }
    if report["busy"]:
        report.update(refused=True, error="processes are running inside this worktree; stop them first")
        return report

    scan_errors: list[str] = []
    links = list(iter_links(path, scan_errors))
    if scan_errors:
        report["scan_errors"] = scan_errors
        if not force:
            report.update(refused=True, error="some directories could not be scanned for links; resolve the errors or pass --force")
            return report
    if is_locked(top):
        report.update(refused=True, error="the worktree is locked (git worktree unlock it first)")
        return report
    if not merged_into and not allow_unmerged:
        existing_refs = [
            ref for ref in INTEGRATION_REFS
            if git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=top).returncode == 0
        ]
        ref_desc = f"any existing integration ref ({', '.join(existing_refs)})" if existing_refs else f"any integration ref ({', '.join(INTEGRATION_REFS)})"
        report.update(
            refused=True,
            error=f"HEAD {head} ({branch}) is not reachable from {ref_desc}; refusing to remove worktree (pass --allow-unmerged to override)",
        )
        return report
    if not force:
        report["dirty"] = dirty_paths(top, links)
        if report["dirty"]:
            report.update(refused=True, error="the worktree has changes git would refuse to drop; commit or discard them, or pass --force")
            return report
    for link in links:
        target = link_target(link)
        if dry_run:
            report["unlinked"].append({"link": link, "target": target})
            continue
        try:
            unlink_link(link)
        except OSError as exc:
            report["error"] = f"failed to unlink {link}: {exc}"
            break
        report["unlinked"].append({"link": link, "target": target})
    if dry_run:
        report["dry_run"] = True
        return report
    leftover = list(iter_links(path, scan_errors))
    if scan_errors:
        report["scan_errors"] = scan_errors
    if leftover or report.get("error"):
        if leftover and "error" not in report:
            report["error"] = f"links still present after unlinking: {leftover[:5]}"
        return report

    args = ["--git-dir", common, "worktree", "remove"] + (["--force"] if force else []) + [top]
    r = git(args, cwd=common, timeout=600)
    report["git"] = {"returncode": r.returncode, "output": (r.stdout + r.stderr).strip()[-2000:]}
    report["removed"] = r.returncode == 0 and not os.path.exists(path)
    if not report["removed"]:
        report["error"] = "git worktree remove failed; all links were already unlinked, so shared trees are safe"
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("worktree")
    ap.add_argument("--force", action="store_true", help="pass --force to git worktree remove")
    ap.add_argument("--dry-run", action="store_true", help="list what would be unlinked; change nothing")
    ap.add_argument("--orphan-dependents", action="store_true", help="remove even if other worktrees link into this one")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--allow-unmerged", action="store_true", help="remove even when HEAD is not reachable from an integration ref (its commits become unreachable)")
    ap.add_argument(
        "--scan-root",
        action="append",
        type=Path,
        help="directory whose children are worktrees to check for links into this one (repeatable; default: ~/development and the profile wt dir)",
    )
    args = ap.parse_args(argv)

    try:
        roots = tuple(args.scan_root) if args.scan_root else DEFAULT_SCAN_ROOTS
        report = remove_worktree(os.path.abspath(args.worktree), args.force, args.dry_run, args.orphan_dependents, roots, allow_unmerged=args.allow_unmerged)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        report = {"worktree": args.worktree, "error": str(exc)}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for item in report.get("unlinked", []):
            verb = "would unlink" if args.dry_run else "unlinked"
            print(f"{verb}: {item['link']} -> {item['target']}")
        for dep in report.get("dependents", []):
            print(f"dependent: {dep['link']} -> {dep['target']}")
        for item in report.get("dirty", []):
            print(f"dirty: {item}")
        for proc in report.get("busy", []):
            print(f"busy: pid {proc['pid']} {proc['name']} cwd={proc['cwd']}")
        if report.get("branch"):
            print(f"branch: {report['branch']}")
        if report.get("head"):
            print(f"head: {report['head']}")
        if "merged_into" in report:
            refs = ", ".join(report["merged_into"]) if report["merged_into"] else "(none)"
            print(f"merged into: {refs}")
        for err in report.get("scan_errors", []):
            print(f"scan error: {err}", file=sys.stderr)
        scan = report.get("process_scan")
        if scan and scan.get("partial"):
            total = scan.get("total")
            print(f"note: process scan was partial (platform={scan['platform']}, read={scan['read']} of {total if total is not None else 'n/a'} processes)", file=sys.stderr)
        if report.get("git"):
            print(report["git"]["output"])
        if report.get("error"):
            print(f"ERROR: {report['error']}", file=sys.stderr)
        elif report.get("removed"):
            print(f"removed {report['worktree']}")
        elif args.dry_run:
            print("dry run: nothing changed")
    if report.get("refused"):
        return 2
    if report.get("error"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
