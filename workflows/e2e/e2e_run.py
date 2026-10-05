#!/usr/bin/env python3
"""Run `e2e run` the Superboard way (https://github.com/Wladefant/super-board/issues/476).

  python e2e_run.py --dir <project> [--record] [--timeout SEC] [--no-slot] [-- e2e run args...]

Default is cached replay: cache 'read-only', no model key in the environment, so a cache miss
fails instead of spending tokens. `--record` turns on 'read-write' and passes the OpenCode Go key
to the child process only; the key comes from the shared auth store and is masked in all output.

Always: E2E_TELEMETRY_DISABLED=1, DO_NOT_TRACK=1, no E2E_OAUTH_CREDENTIALS, no console window,
a timeout on the child, and the run goes through build_slot.py so heavy runs serialize.
It never calls `e2e --version`, `e2e init` or `e2e login`.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_guard  # noqa: E402

HERE = Path(__file__).resolve().parent
PINS = json.loads((HERE / "pins.json").read_text(encoding="utf-8"))
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BUILD_SLOT = Path.home() / ".veyyon" / "workflows" / "build_slot.py"
BIN = Path("node_modules") / "e2e" / "dist" / "cli" / "bin.js"


def read_model_key(store: Optional[Path] = None) -> Optional[str]:
    """Read the OpenCode Go API key from the shared auth store, read-only."""
    path = store or Path(PINS["model"]["authStore"]).expanduser()
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        row = con.execute(
            "SELECT json_extract(data,'$.key') FROM auth_credentials "
            "WHERE provider=? AND credential_type='api_key' AND disabled_cause IS NULL LIMIT 1",
            (PINS["model"]["authProvider"],),
        ).fetchone()
    finally:
        con.close()
    return row[0] if row and row[0] else None


def build_env(record: bool, base: Dict[str, str], key: Optional[str]) -> Dict[str, str]:
    env = dict(base)
    env.update(PINS["env"])
    env.pop("E2E_OAUTH_CREDENTIALS", None)
    env.pop("E2E_MODEL_API_KEY", None)
    if record:
        env["E2E_CACHE_MODE"] = PINS["cacheModeRecord"]
        if key:
            env["E2E_MODEL_API_KEY"] = key
    else:
        env["E2E_CACHE_MODE"] = PINS["cacheModeDefault"]
    return env


def mask(text: str, key: Optional[str]) -> str:
    return text.replace(key, "***") if key else text


def preflight(app_url: str, extra_hosts: List[str]) -> Optional[str]:
    """Refuse a start URL, or any redirect hop from it, outside the allow-list."""
    return e2e_guard.check_redirects(app_url, ["localhost", "127.0.0.1", *extra_hosts])


def disable_telemetry(entry: Path, project: Path, env: Dict[str, str]) -> None:
    """`e2e telemetry disable` (writes the per-machine opt-out). A failure is logged, the env switch still holds."""
    try:
        subprocess.run(
            ["node", str(entry), "telemetry", "disable"], cwd=str(project), env=env,
            capture_output=True, text=True, timeout=30, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"e2e_run: telemetry disable not confirmed ({exc}); E2E_TELEMETRY_DISABLED=1 still set", file=sys.stderr)


def kill_tree(pid: int) -> None:
    """Kill the whole process tree (build_slot.py, its shell, node, the browser)."""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        pass


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: List[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="project directory holding e2e.config.ts and node_modules")
    ap.add_argument("--record", action="store_true", help="read-write cache and the model key (spends tokens)")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--slot-name", default="e2e-run")
    ap.add_argument("--no-slot", action="store_true", help="skip build_slot.py (tests only)")
    ap.add_argument("--app-url", help="sets APP_URL for the config's host guard")
    ap.add_argument("--allow-host", action="append", default=[], help="extra staging host (exact or dot-suffix); repeatable")
    args = ap.parse_args(argv)

    project = Path(args.dir).resolve()
    entry = project / BIN
    if not entry.exists():
        print(f"e2e_run: {entry} not found; install the pinned packages first (workflows/e2e/pins.json)", file=sys.stderr)
        return 2
    key = read_model_key() if args.record else None
    if args.record and not key:
        print("e2e_run: --record needs the opencode-go API key in the shared auth store", file=sys.stderr)
        return 2
    env = build_env(args.record, dict(os.environ), key)
    if args.app_url:
        env["APP_URL"] = args.app_url

    cmd = ["node", str(entry), "run", *extra]
    if not args.no_slot:
        cmd = [sys.executable, str(BUILD_SLOT), "run", args.slot_name, "--timeout", str(args.timeout), "--cwd", str(project), "--", *cmd]
    pre = preflight(args.app_url or env.get("APP_URL") or "http://127.0.0.1:3000", args.allow_host)
    if pre:
        print(f"e2e_run: refused: {pre}", file=sys.stderr)
        return 2
    disable_telemetry(entry, project, env)
    popen = subprocess.Popen(
        cmd, cwd=str(project), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=CREATE_NO_WINDOW,
    )
    try:
        out, err = popen.communicate(timeout=args.timeout + 60)
    except subprocess.TimeoutExpired:
        kill_tree(popen.pid)
        popen.communicate()
        print(f"e2e_run: timed out after {args.timeout}s", file=sys.stderr)
        return 124
    sys.stdout.write(mask(out, key))
    sys.stderr.write(mask(err, key))
    return popen.returncode



if __name__ == "__main__":
    sys.exit(main())
