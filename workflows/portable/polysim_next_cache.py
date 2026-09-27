#!/usr/bin/env python3
r"""Per-worktree Next.js build cache for PolySimulator lanes (workflows/polysim_next_cache.py).

Why (traces of 20 lane builds, Next 14.2.35): ``verify-typescript-setup`` takes 100-200 s on
every build because Next writes ``.next/cache/.tsbuildinfo`` with ``program.emit()`` BEFORE it
asks for diagnostics (``lib/typescript/runTypeCheck.js``), so the buildinfo never records a
checked file. And ``build_slot.py prep-cache`` junctions every ``.next/cache`` to one shared
``~/.veyyon/run/next-cache``, whose 3.4 GB of webpack packs are keyed by absolute path to other
worktrees: they never hit and only fill the build's heap.

``prepare`` gives the worktree its own ``.next/cache`` (a junction is removed as a link, never
followed), drops a webpack cache holding another worktree's modules, seeds ``.tsbuildinfo``
from the nearest prepared worktree with the same lockfile, and runs Next's type check with the
diagnostics queried before emit, so ``next build`` only re-checks what changed. ``sweep`` does
the cleanup for every idle worktree and empties the shared store once nothing links to it.
Files are copied, never linked. A worktree a process uses (``wt_remove.busy_processes``) is
not touched.

    python polysim_next_cache.py prepare --worktree <wt> [--source <wt>] [--force] [--json]
    python polysim_next_cache.py sweep [--apply] [--json]

Exit status: 0 ok; 1 type errors (prepare); 2 bad input; 3 worktree busy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wt_remove as wtr  # noqa: E402  (sibling module; path set above)

HOME = wtr.HOME
POLYSIM_CLONE = Path(os.environ.get("POLYSIM_CLONE", HOME / "development" / "polysimulator"))
SHARED_STORE = Path(os.environ.get("POLYSIM_SHARED_NEXT_CACHE", HOME / ".veyyon" / "run" / "next-cache"))
TSBUILDINFO = ".tsbuildinfo"
WARM_MARKER = ".tsbuildinfo.warm.json"
# `<dir>\frontend\` inside a webpack pack names the worktree the entry belongs to.
PACK_WORKTREE = re.compile(rb"[\\/]([^\\/\x00\"'|!]{1,120})[\\/]+frontend[\\/]+")

# Runs Next's own tsconfig resolution and required options, so the buildinfo matches what
# `next build` reads, but queries diagnostics before emit so the buildinfo records them.
TYPECHECK_JS = r"""
const path = require("path");
const fe = process.cwd();
const req = (m) => require(path.join(fe, "node_modules", m));
const ts = req("typescript");
const { getTypeScriptConfiguration } = req("next/dist/lib/typescript/getTypeScriptConfiguration");
const { getRequiredConfiguration } = req("next/dist/lib/typescript/writeConfigurationDefaults");
(async () => {
  const cfg = await getTypeScriptConfiguration(ts, path.join(fe, "tsconfig.json"));
  const options = {
    ...getRequiredConfiguration(ts), ...cfg.options,
    declarationMap: false, emitDeclarationOnly: false, noEmit: true,
    composite: false, incremental: true, tsBuildInfoFile: path.join(fe, ".next", "cache", ".tsbuildinfo"),
  };
  const program = ts.createIncrementalProgram({ rootNames: cfg.fileNames, options });
  const ignored = /[\\/]__(?:tests|mocks)__[\\/]|(?<=[\\/.])(?:spec|test)\.[^\\/]+$/;
  const errors = ts.getPreEmitDiagnostics(program).filter(
    (d) => d.category === ts.DiagnosticCategory.Error && !(d.file && ignored.test(d.file.fileName)));
  program.emit();
  for (const d of errors.slice(0, 30)) {
    const at = d.file ? `${path.relative(fe, d.file.fileName)}:${d.file.getLineAndCharacterOfPosition(d.start).line + 1} ` : "";
    console.error(`${at}TS${d.code}: ${ts.flattenDiagnosticMessageText(d.messageText, "\n")}`);
  }
  console.log(JSON.stringify({ files: cfg.fileNames.length, errors: errors.length }));
  process.exit(errors.length ? 1 : 0);
})().catch((e) => { console.error(e && e.stack || String(e)); process.exit(2); });
"""



def git(args: list[str], cwd: Path) -> str | None:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
    return r.stdout.strip() if r.returncode == 0 else None


def list_worktrees(clone: Path) -> list[Path]:
    out = git(["worktree", "list", "--porcelain"], clone) or ""
    return [Path(line[len("worktree "):]) for line in out.splitlines() if line.startswith("worktree ")]


def lock_key(worktree: Path) -> str | None:
    """Same key as polysim_frontend_deps.py: sha256 of the LF-normalized lockfile, 16 hex."""
    try:
        raw = (worktree / "frontend" / "package-lock.json").read_bytes()
    except OSError:
        return None
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()[:16]


def cache_dir(worktree: Path) -> Path:
    return worktree / "frontend" / ".next" / "cache"


def commit_distance(clone: Path, a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    out = git(["rev-list", "--count", "--left-right", f"{a}...{b}"], clone)
    if not out:
        return None
    left, right = out.split()
    return int(left) + int(right)



def pack_worktrees(webpack: Path) -> set[str]:
    """Worktree directory names the webpack cache holds modules for (read from index packs)."""
    names: set[str] = set()
    for index in webpack.glob("*/index.pack"):
        try:
            data = index.read_bytes()
        except OSError:
            continue
        names.update(m.group(1).decode("utf-8", "replace").lower() for m in PACK_WORKTREE.finditer(data))
    return names


def tree_size(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.stat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def warm_marker(worktree: Path) -> dict | None:
    try:
        return json.loads((cache_dir(worktree) / WARM_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def rank_sources(target: Path, clone: Path, only: Path | None = None) -> list[dict]:
    """Prepared worktrees (checked .tsbuildinfo) with the target's lockfile, nearest commit first."""
    key, head = lock_key(target), git(["rev-parse", "HEAD"], target)
    rows = []
    for wt in [only] if only else list_worktrees(clone):
        marker = warm_marker(wt)
        if wtr.norm(wt) == wtr.norm(target) or marker is None or marker.get("lock_key") != key:
            continue
        if (cache_dir(wt) / TSBUILDINFO).is_file():
            rows.append({"worktree": str(wt), "head": marker.get("head"), "distance": commit_distance(clone, marker.get("head"), head)})
    return sorted(rows, key=lambda r: r["distance"] if r["distance"] is not None else 1 << 30)


def own_cache_dir(worktree: Path) -> dict:
    """Replace a linked .next/cache with a real directory. The link target is not touched."""
    cache = cache_dir(worktree)
    if wtr.is_link(cache):
        target = wtr.link_target(cache)
        wtr.unlink_link(cache)
        cache.mkdir()
        return {"cache": "unlinked", "was_linked_to": target}
    cache.mkdir(parents=True, exist_ok=True)
    return {"cache": "own"}


def drop_foreign_webpack(worktree: Path) -> dict:
    """Delete the worktree's webpack cache when it holds another worktree's modules."""
    webpack = cache_dir(worktree) / "webpack"
    if wtr.is_link(cache_dir(worktree)) or not webpack.is_dir():
        return {"webpack": "none"}
    names = pack_worktrees(webpack)
    foreign = sorted(names - {worktree.name.lower()})
    if not foreign:
        return {"webpack": "own", "webpack_worktrees": sorted(names)}
    size = tree_size(webpack)
    shutil.rmtree(webpack)
    return {"webpack": "dropped", "webpack_worktrees": sorted(names), "freed_bytes": size}


def seed_tsbuildinfo(worktree: Path, clone: Path, source: Path | None) -> dict:
    cache = cache_dir(worktree)
    if warm_marker(worktree) is not None and (cache / TSBUILDINFO).is_file():
        return {"tsbuildinfo": "own"}
    best = next(iter(rank_sources(worktree, clone, source)), None)
    if best is None:
        # A buildinfo `next build` wrote itself holds no diagnostics but still saves parsing.
        return {"tsbuildinfo": "kept unchecked" if (cache / TSBUILDINFO).is_file() else "cold"}
    tmp = cache / f"{TSBUILDINFO}.seed-{os.getpid()}"
    shutil.copy2(cache_dir(Path(best["worktree"])) / TSBUILDINFO, tmp)
    os.replace(tmp, cache / TSBUILDINFO)
    return {"tsbuildinfo": "seeded", "source": best["worktree"], "source_head": best["head"], "distance": best["distance"]}


def typecheck(worktree: Path) -> dict:
    fe = worktree / "frontend"
    started = time.monotonic()
    r = subprocess.run(["node", "-e", TYPECHECK_JS], cwd=str(fe), capture_output=True, text=True, stdin=subprocess.DEVNULL)
    seconds = round(time.monotonic() - started, 1)
    if r.returncode not in (0, 1):
        raise RuntimeError(f"type check did not run (exit {r.returncode}): {r.stderr.strip()[-2000:]}")
    summary = json.loads(r.stdout.strip().splitlines()[-1])
    result = {"typecheck_s": seconds, "files": summary["files"], "type_errors": summary["errors"]}
    if r.returncode == 1:
        result["type_error_lines"] = r.stderr.strip().splitlines()
        return result
    marker = {
        "head": git(["rev-parse", "HEAD"], worktree),
        "lock_key": lock_key(worktree),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    (cache_dir(worktree) / WARM_MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return result



def emit(args: argparse.Namespace, result: dict, lines: list[str], code: int) -> int:
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("\n".join(lines))
    return code


def cmd_prepare(args: argparse.Namespace, clone: Path) -> int:
    wt = Path(os.path.abspath(args.worktree))
    if not (wt / "frontend" / "node_modules" / "next" / "package.json").is_file():
        print(f"error: {wt}/frontend/node_modules has no next; link it first (polysim_frontend_deps.py link {wt})", file=sys.stderr)
        return 2
    if not args.force:
        holders = wtr.busy_processes(wt, wtr.process_paths())
        if holders:
            return emit(args, {"status": "busy", "processes": holders},
                        [f"busy: {wt} is in use by " + ", ".join(f"{name} {pid}" for pid, name, _cwd in holders)], 3)
    result: dict = {"worktree": str(wt)}
    result.update(own_cache_dir(wt))
    result.update(drop_foreign_webpack(wt))
    result.update(seed_tsbuildinfo(wt, clone, Path(os.path.abspath(args.source)) if args.source else None))
    result.update(typecheck(wt))
    code = 1 if result["type_errors"] else 0
    result["status"] = "type-errors" if code else "ready"
    lines = [f"{result['status']}: {wt}",
             f"  .next/cache: {result['cache']}" + (f" (was linked to {result['was_linked_to']})" if "was_linked_to" in result else ""),
             f"  webpack: {result['webpack']}" + (f" ({result['freed_bytes'] / 1e9:.2f} GB for {', '.join(result['webpack_worktrees'])})" if result["webpack"] == "dropped" else ""),
             f"  .tsbuildinfo: {result['tsbuildinfo']}" + (f" from {result['source']} ({result['distance']} commits apart)" if result["tsbuildinfo"] == "seeded" else ""),
             f"  type check: {result['typecheck_s']}s, {result['files']} files, {result['type_errors']} errors"]
    lines += [f"    {line}" for line in result.get("type_error_lines", [])]
    return emit(args, result, lines, code)


def cmd_sweep(args: argparse.Namespace, clone: Path) -> int:
    rows = []
    linked_to_store = []
    procs = wtr.process_paths()
    for wt in list_worktrees(clone):
        cache = cache_dir(wt)
        linked = wtr.is_link(cache)
        webpack = cache / "webpack"
        if not linked and not webpack.is_dir():
            continue
        row: dict = {"worktree": str(wt), "linked_to": wtr.link_target(cache) if linked else None}
        if not linked:
            names = pack_worktrees(webpack)
            row["foreign"] = sorted(names - {wt.name.lower()})
            if not row["foreign"]:
                continue
        holders = wtr.busy_processes(wt, procs)
        if holders:
            row["action"] = "skip: busy (" + ", ".join(f"{name} {pid}" for pid, name, _cwd in holders) + ")"
            if linked and row["linked_to"] == wtr.norm(SHARED_STORE):
                linked_to_store.append(str(wt))
        elif args.apply:
            row.update(own_cache_dir(wt) if linked else drop_foreign_webpack(wt))
            row["action"] = "unlinked" if linked else "dropped foreign webpack"
        else:
            row["action"] = "would unlink" if linked else "would drop foreign webpack"
        rows.append(row)
    store = {"path": str(SHARED_STORE), "still_linked_by": linked_to_store}
    store_webpack = SHARED_STORE / "webpack"
    if store_webpack.is_dir():
        store["webpack_bytes"] = tree_size(store_webpack)
        if linked_to_store:
            store["action"] = "keep: still linked by a busy worktree"
        elif args.apply:
            shutil.rmtree(store_webpack)
            store["action"] = "deleted webpack packs"
        else:
            store["action"] = "would delete webpack packs"
    result = {"applied": args.apply, "worktrees": rows, "shared_store": store}
    lines = [f"{r['action']}: {r['worktree']}" + (f" -> {r['linked_to']}" if r["linked_to"] else f" (modules of {', '.join(r['foreign'])})") for r in rows]
    if "action" in store:
        lines.append(f"{store['action']}: {store_webpack} ({store['webpack_bytes'] / 1e9:.2f} GB)")
    return emit(args, result, lines or ["nothing to do"], 0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clone", default=str(POLYSIM_CLONE), help="PolySimulator clone whose worktrees are scanned")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="own cache dir, drop foreign webpack packs, warm and run the type check")
    p.add_argument("--worktree", required=True)
    p.add_argument("--source", help="seed .tsbuildinfo from this prepared worktree")
    p.add_argument("--force", action="store_true", help="run even when a process uses the worktree")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("sweep", help="unlink shared-cache junctions and drop foreign webpack packs in idle worktrees")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    clone = Path(args.clone)
    if args.command == "sweep":
        return cmd_sweep(args, clone)
    if not (Path(os.path.abspath(args.worktree)) / "frontend" / "package-lock.json").is_file():
        print(f"error: {args.worktree} has no frontend/package-lock.json", file=sys.stderr)
        return 2
    return cmd_prepare(args, clone)


if __name__ == "__main__":
    sys.exit(main())
