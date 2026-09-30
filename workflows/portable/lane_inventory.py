#!/usr/bin/env python3
"""
lane_inventory.py - Mechanically verified lane inventory and crash recovery audit.

Part of portable workflow core in Wladefant/super-board.
References:
  - Superboard Issue #227 (High-Trust Agent Architecture & Verification Skills)
  - Profile AGENTS.md §2 (Never Lose Started Lanes & Bounded Concurrency)

Enforces mechanical verification of every worktree, branch, HEAD SHA, and PR:
  1. Worktree must exist on disk and be inside a valid git worktree.
  2. Branch must match `git -C <wt> rev-parse --abbrev-ref HEAD` (never regexes or guessed names).
  3. PR must be queried via `gh api` / `gh pr view` and confirmed OPEN and matching the worktree branch.
  4. Merged, closed, missing, or mismatched items are explicitly tagged UNVERIFIED.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SHARED_ROOT_NAMES = {
    ".wt-merge-recovery-20260922-main2",
    "polysimulator",
    "super-board",
    "veyyon",
}


def normalize_path(path_str: str, base_dir: str = "C:/Users/wkiri/development") -> str:
    """Normalize path string with forward slashes and absolute resolution."""
    if not path_str:
        return ""
    cleaned = path_str.strip().strip("'\"").replace("\\", "/")
    p = Path(cleaned)
    if not p.is_absolute():
        p = Path(base_dir) / cleaned
    return p.as_posix()


def verify_worktree(path_str: str, base_dir: str = "C:/Users/wkiri/development") -> Dict[str, Any]:
    """Mechanically verify that a directory exists, is a git worktree, and query its branch and SHA."""
    if not path_str:
        return {
            "verified": False,
            "path": None,
            "branch": None,
            "sha": None,
            "short_sha": None,
            "repo": None,
            "dirty": False,
            "reason": "No worktree path provided",
        }

    full_path = normalize_path(path_str, base_dir=base_dir)
    p = Path(full_path)

    if not p.exists():
        return {
            "verified": False,
            "path": full_path,
            "branch": None,
            "sha": None,
            "short_sha": None,
            "repo": None,
            "dirty": False,
            "reason": f"Directory does not exist: {full_path}",
        }

    if not p.is_dir():
        return {
            "verified": False,
            "path": full_path,
            "branch": None,
            "sha": None,
            "short_sha": None,
            "repo": None,
            "dirty": False,
            "reason": f"Path is not a directory: {full_path}",
        }

    # Fast check: .git exists as a file (worktree) or dir (main repo)
    git_marker = p / ".git"
    if not git_marker.exists():
        return {
            "verified": False,
            "path": full_path,
            "branch": None,
            "sha": None,
            "short_sha": None,
            "repo": None,
            "dirty": False,
            "reason": f"Not a git repository/worktree (no .git): {full_path}",
        }

    # If .git is a file (worktree), read gitdir to verify it points to a valid git repo
    if git_marker.is_file():
        try:
            content = git_marker.read_text(encoding="utf-8").strip()
            if not content.startswith("gitdir:"):
                return {
                    "verified": False,
                    "path": full_path,
                    "branch": None,
                    "sha": None,
                    "short_sha": None,
                    "repo": None,
                    "dirty": False,
                    "reason": f"Corrupt .git pointer file in {full_path}",
                }
        except Exception as e:
            return {
                "verified": False,
                "path": full_path,
                "branch": None,
                "sha": None,
                "short_sha": None,
                "repo": None,
                "dirty": False,
                "reason": f"Cannot read .git file: {e}",
            }

    # Query branch
    try:
        res_branch = subprocess.run(
            ["git", "-C", full_path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        branch = res_branch.stdout.strip()
    except Exception:
        branch = "unknown"

    # Query HEAD SHA
    try:
        res_sha = subprocess.run(
            ["git", "-C", full_path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        sha = res_sha.stdout.strip()
    except Exception:
        sha = "unknown"

    # Query dirty status
    try:
        res_status = subprocess.run(
            ["git", "-C", full_path, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        dirty = bool(res_status.stdout.strip())
    except Exception:
        dirty = False

    # Query remote repo
    repo = None
    try:
        res_remote = subprocess.run(
            ["git", "-C", full_path, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        remote_url = res_remote.stdout.strip()
        m = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", remote_url)
        if m:
            repo = m.group(1)
    except Exception:
        pass

    return {
        "verified": True,
        "path": full_path,
        "branch": branch,
        "sha": sha,
        "short_sha": sha[:8] if len(sha) >= 8 else sha,
        "repo": repo or "unknown",
        "dirty": dirty,
        "reason": None,
    }


_GH_TOKEN: Optional[str] = None
PR_CACHE: Dict[Tuple[str, int], Dict[str, Any]] = {}


def get_gh_token() -> Optional[str]:
    """Get GitHub token from env or gh auth token."""
    global _GH_TOKEN
    if _GH_TOKEN:
        return _GH_TOKEN
    t = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if t:
        _GH_TOKEN = t
        return _GH_TOKEN
    try:
        res = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
        if res.returncode == 0 and res.stdout.strip():
            _GH_TOKEN = res.stdout.strip()
            return _GH_TOKEN
    except Exception:
        pass
    return None


def query_github_pr(repo: str, pr_number: int) -> Dict[str, Any]:
    """Query PR details from GitHub API via urllib, fallback to gh api."""
    import urllib.error
    import urllib.request

    token = get_gh_token()
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    headers = {
        "User-Agent": "lane-inventory-audit/1.0",
        "Accept": "application/vnd.github.v3+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {
                "number": data.get("number", pr_number),
                "state": data.get("state", "unknown"),
                "merged": data.get("merged_at") is not None,
                "title": data.get("title", ""),
                "headRefName": data.get("head", {}).get("ref", ""),
                "headRefOid": data.get("head", {}).get("sha", ""),
                "url": data.get("html_url", url),
            }
    except Exception:
        # Fallback to gh api (also enables unittest mock of subprocess.run)
        cmd = [
            "gh",
            "api",
            f"repos/{repo}/pulls/{pr_number}",
            "--jq",
            "{number: .number, state: .state, merged: (.merged_at != null), title: .title, headRefName: .head.ref, headRefOid: .head.sha, url: .html_url}",
        ]
        try:
            env = dict(os.environ, GH_PAGER="", PAGER="cat")
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                stdin=subprocess.DEVNULL,
                env=env,
            )
            if res.returncode != 0:
                return {"error": res.stderr.strip() or f"PR #{pr_number} not found in {repo}"}
            return json.loads(res.stdout)
        except Exception as e2:
            return {"error": f"PR check failed for #{pr_number} in {repo}: {e2}"}


def verify_pr(repo: str, pr_number: int, expected_branch: Optional[str] = None) -> Dict[str, Any]:
    """Mechanically verify a GitHub PR using query_github_pr and check state and branch."""
    if not repo or not pr_number:
        return {"verified": False, "reason": "Missing repo or PR number", "number": pr_number}

    cache_key = (repo, pr_number)
    if cache_key in PR_CACHE:
        data = PR_CACHE[cache_key]
    else:
        data = query_github_pr(repo, pr_number)
        PR_CACHE[cache_key] = data

    if "error" in data:
        return {
            "verified": False,
            "reason": data["error"][:120],
            "number": pr_number,
            "repo": repo,
        }
    raw_state = data.get("state", "unknown")
    is_merged = data.get("merged", False) or raw_state.upper() == "MERGED"
    title = data.get("title", "")
    url = data.get("url", f"https://github.com/{repo}/pull/{pr_number}")
    head_branch = data.get("headRefName", "")
    head_sha = data.get("headRefOid", "")

    if is_merged:
        return {
            "verified": False,
            "state": "MERGED",
            "number": pr_number,
            "title": title,
            "url": url,
            "head_branch": head_branch,
            "head_sha": head_sha,
            "reason": f"PR #{pr_number} is MERGED ({title})",
        }

    if raw_state.lower() == "closed":
        return {
            "verified": False,
            "state": "CLOSED",
            "number": pr_number,
            "title": title,
            "url": url,
            "head_branch": head_branch,
            "head_sha": head_sha,
            "reason": f"PR #{pr_number} is CLOSED ({title})",
        }

    if expected_branch and head_branch and head_branch != expected_branch:
        return {
            "verified": False,
            "state": raw_state.upper(),
            "number": pr_number,
            "title": title,
            "url": url,
            "head_branch": head_branch,
            "head_sha": head_sha,
            "reason": f"PR #{pr_number} branch mismatch: {head_branch} vs {expected_branch}",
        }

    return {
        "verified": True,
        "state": "OPEN",
        "number": pr_number,
        "title": title,
        "url": url,
        "head_branch": head_branch,
        "head_sha": head_sha,
        "reason": None,
    }


def extract_lane_telemetry(session_path: Path) -> Dict[str, Any]:
    """Extract operational facts, tool calls, and candidate worktree/PR targets from session JSONL."""
    lines_raw = session_path.read_text(encoding="utf-8", errors="replace").splitlines()
    events = []
    for l in lines_raw:
        if not l.strip():
            continue
        try:
            events.append(json.loads(l))
        except Exception:
            pass

    name = session_path.stem
    last_ts = events[-1].get("timestamp", "N/A") if events else "N/A"

    model = "unknown"
    for e in events:
        if e.get("type") == "model_change":
            model = e.get("model", model)
            break
        elif "model" in e:
            model = e.get("model", model)
            break

    user_prompt = ""
    for e in events:
        if e.get("message", {}).get("role") == "user":
            content = e.get("message", {}).get("content", [])
            if isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        user_prompt = part.get("text", "")
            elif isinstance(content, str):
                user_prompt = content
            break

    explicit_set_cwd: Optional[str] = None
    tool_cwds: List[str] = []
    candidate_prs: List[Tuple[str, int]] = []
    has_yield = False
    last_action = None

    for e in events:
        msg = e.get("message", {})
        role = msg.get("role")
        if role == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "toolCall":
                        tname = part.get("name")
                        args = part.get("arguments", {})
                        if tname == "yield":
                            has_yield = True
                        if tname == "set_cwd" and args.get("path"):
                            explicit_set_cwd = args["path"]
                            tool_cwds.append(args["path"])
                        if tname == "bash":
                            if args.get("cwd"):
                                tool_cwds.append(args["cwd"])
                            cmd = args.get("command", "")
                            last_action = f"bash: {cmd[:150]}"
                            # Check git -C <path> or cd <path>
                            for gc in re.findall(r'git\s+-C\s+([A-Za-z0-9_./:-]+)', cmd):
                                tool_cwds.append(gc.strip("'\""))
                            for cd_path in re.findall(r'cd\s+["\']?([A-Za-z0-9_./:-]+)["\']?', cmd):
                                tool_cwds.append(cd_path)
                            # Check gh pr view
                            for pr_m in re.finditer(r'gh\s+pr\s+view\s+(\d+)(?:.*?-R\s+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?', cmd):
                                p_num = int(pr_m.group(1))
                                p_repo = pr_m.group(2) or "Bavariance/polysimulator"
                                candidate_prs.append((p_repo, p_num))
                        elif tname == "read":
                            p_path = args.get("path", "")
                            last_action = f"read: {p_path[:150]}"
                            for pr_m in re.finditer(r'pr://(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/)?(\d+)', p_path):
                                p_repo = pr_m.group(1) or "Bavariance/polysimulator"
                                p_num = int(pr_m.group(2))
                                candidate_prs.append((p_repo, p_num))
                        elif tname == "edit":
                            last_action = f"edit: {args.get('path', '')[:150]}"
                        elif tname == "task":
                            tasks = args.get("tasks", [])
                            last_action = f"task spawn: {[t.get('name') for t in tasks]}"
                        elif tname == "eval":
                            last_action = f"eval: {args.get('title', '')} {args.get('code', '')[:80]}".strip()

    # Prompt hints
    prompt_wts: List[str] = []
    for m in re.finditer(r'(?:Worktree|worktree|directory|repo)[:\s]+([C|c]:[/\\][A-Za-z0-9_./-]+|\.[A-Za-z0-9_./-]+)', user_prompt):
        prompt_wts.append(m.group(1))

    prompt_branches: List[str] = []
    for m in re.finditer(r'(?:branch|Branch)[:\s]+`?([A-Za-z0-9_./-]+)`?', user_prompt):
        b = m.group(1).rstrip(",.")
        if b not in ("main", "staging", "user", "Goal", "Post"):
            prompt_branches.append(b)

    for m in re.finditer(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)', user_prompt):
        candidate_prs.append((m.group(1), int(m.group(2))))

    for m in re.finditer(r'PR\s+https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)', user_prompt):
        candidate_prs.append((m.group(1), int(m.group(2))))

    for m in re.finditer(r'PR\s+#?(\d+)', user_prompt):
        candidate_prs.append(("Bavariance/polysimulator", int(m.group(1))))

    prompt_issues: List[str] = []
    for m in re.finditer(r'#(\d+)', user_prompt):
        prompt_issues.append(m.group(1))

    # Reverse order tool_cwds so the latest tool cwd comes first
    unique_tool_cwds = list(dict.fromkeys(reversed(tool_cwds)))
    unique_prs = list(dict.fromkeys(candidate_prs))

    return {
        "name": name,
        "model": model,
        "status": "FINISHED" if has_yield else "STOPPED",
        "last_ts": last_ts,
        "last_action": last_action or "stopped mid-turn",
        "user_prompt": user_prompt,
        "explicit_set_cwd": explicit_set_cwd,
        "tool_cwds": unique_tool_cwds,
        "prompt_wts": prompt_wts,
        "prompt_branches": prompt_branches,
        "candidate_prs": unique_prs,
        "prompt_issues": prompt_issues,
    }


def audit_lane(session_path: Path, dev_root: str = "C:/Users/wkiri/development") -> Dict[str, Any]:
    """Audit a single lane session file, mechanically verifying worktree and PR claims."""
    telemetry = extract_lane_telemetry(session_path)

    verified_wt: Optional[Dict[str, Any]] = None
    wt_failure_reason: Optional[str] = None

    # Step 1: If the lane explicitly executed set_cwd, that is its authoritative work directory.
    if telemetry.get("explicit_set_cwd"):
        set_cwd_path = telemetry["explicit_set_cwd"]
        res = verify_worktree(set_cwd_path, base_dir=dev_root)
        if res["verified"]:
            verified_wt = res
        else:
            wt_failure_reason = res["reason"]

    # Step 2: If no verified explicit set_cwd, check tool_cwds that are dedicated worktrees (not shared roots)
    if not verified_wt and not wt_failure_reason:
        dedicated_cwds = [
            c for c in telemetry["tool_cwds"]
            if Path(normalize_path(c, dev_root)).name not in SHARED_ROOT_NAMES
        ]
        for cand in dedicated_cwds:
            res = verify_worktree(cand, base_dir=dev_root)
            if res["verified"]:
                verified_wt = res
                break

    # Step 3: If still none, check prompt worktrees
    if not verified_wt and not wt_failure_reason:
        for cand in telemetry["prompt_wts"]:
            res = verify_worktree(cand, base_dir=dev_root)
            if res["verified"]:
                verified_wt = res
                break
            elif not wt_failure_reason:
                wt_failure_reason = res["reason"]

    # Step 4: If still none, check shared roots or fallback
    if not verified_wt and not wt_failure_reason:
        for cand in telemetry["tool_cwds"]:
            res = verify_worktree(cand, base_dir=dev_root)
            if res["verified"]:
                verified_wt = res
                break
            elif not wt_failure_reason:
                wt_failure_reason = res["reason"]

    # Assemble worktree and branch fields
    if verified_wt:
        worktree_field = verified_wt["path"]
        branch_field = verified_wt["branch"]
        head_sha_field = verified_wt["short_sha"]
        repo = verified_wt["repo"]
    else:
        raw_wt = (
            telemetry["explicit_set_cwd"]
            or (telemetry["prompt_wts"][0] if telemetry["prompt_wts"] else None)
            or (telemetry["tool_cwds"][0] if telemetry["tool_cwds"] else "none")
        )
        reason_msg = wt_failure_reason or f"missing worktree: {raw_wt}"
        worktree_field = f"UNVERIFIED ({reason_msg})"
        branch_field = "UNVERIFIED"
        head_sha_field = "N/A"
        repo = "Bavariance/polysimulator"

    # Step 5: Verify candidate PRs
    verified_pr_entry: Optional[str] = None
    pr_unverified_reasons: List[str] = []

    for p_repo, p_num in telemetry["candidate_prs"]:
        # If repo from worktree is known and candidate repo is default, prioritize worktree repo
        active_repo = repo if (repo and repo != "unknown") else p_repo
        expected_b = branch_field if branch_field != "UNVERIFIED" else None
        res_pr = verify_pr(active_repo, p_num, expected_branch=expected_b)
        if res_pr["verified"]:
            verified_pr_entry = f"[{p_num}]({res_pr['url']})"
            break
        else:
            pr_unverified_reasons.append(res_pr["reason"])

    if verified_pr_entry:
        pr_field = verified_pr_entry
    elif pr_unverified_reasons:
        pr_field = f"UNVERIFIED ({pr_unverified_reasons[0]})"
    else:
        pr_field = "N/A"

    return {
        "name": telemetry["name"],
        "model": telemetry["model"],
        "status": telemetry["status"],
        "last_ts": telemetry["last_ts"],
        "worktree": worktree_field,
        "branch": branch_field,
        "pr": pr_field,
        "head_sha": head_sha_field,
        "last_action": telemetry["last_action"],
        "issues": telemetry["prompt_issues"],
    }


def format_markdown_table(audited_lanes: List[Dict[str, Any]]) -> str:
    """Format audited lanes into the standard GitHub/Veyyon Master Top-Level Lane Inventory table."""
    lines = [
        "| Lane | Model | Status | Last Active (UTC) | Worktree / Branch | PR / Issues | Head SHA | Last Action (<=200 chars) | Next Action |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for item in audited_lanes:
        name = f"**{item['name']}**"
        model = item["model"].split("/")[-1] if "/" in item["model"] else item["model"]
        model_str = f"`{model}`"
        status_str = f"**{item['status']}**" if item["status"] == "STOPPED" else item["status"]

        ts = item["last_ts"]
        if "T" in ts:
            time_part = ts.split("T")[1].split(".")[0]
        else:
            time_part = ts

        wt_branch = f"`{item['worktree']}`<br>`{item['branch']}`" if "<br>" not in item["worktree"] else item["worktree"]
        pr_issues = item["pr"]
        if item.get("issues"):
            issues_str = ", ".join(f"#{i}" for i in item["issues"][:3])
            pr_issues += f"<br>({issues_str})"

        head_sha = f"`{item['head_sha']}`" if item["head_sha"] != "N/A" else "N/A"
        last_action = item["last_action"][:200].replace("\r", " ").replace("\n", " ").replace("|", "\\|")
        next_action = "Resume with mechanical verification of worktree and branch."

        lines.append(
            f"| {name} | {model_str} | {status_str} | `{time_part}` | {wt_branch} | {pr_issues} | {head_sha} | {last_action} | {next_action} |"
        )

    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Mechanically verified lane inventory generator")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # audit subcommand
    audit_parser = subparsers.add_parser("audit", help="Audit lane session files")
    audit_parser.add_argument("--session-dir", type=str, default=None, help="Directory containing session JSONL files")
    audit_parser.add_argument("--lanes", type=str, default=None, help="Comma-separated list of lane names to audit")
    audit_parser.add_argument("--output", type=str, default=None, help="Output markdown or JSON file path")
    audit_parser.add_argument("--json", action="store_true", help="Output JSON instead of Markdown")

    # verify-wt subcommand
    wt_parser = subparsers.add_parser("verify-wt", help="Verify a worktree path")
    wt_parser.add_argument("path", type=str, help="Path to worktree")

    # verify-pr subcommand
    pr_parser = subparsers.add_parser("verify-pr", help="Verify a GitHub PR")
    pr_parser.add_argument("repo", type=str, help="GitHub repository (owner/repo)")
    pr_parser.add_argument("number", type=int, help="PR number")
    pr_parser.add_argument("--branch", type=str, default=None, help="Expected headRefName")

    args = parser.parse_args(argv)

    if args.subcommand == "verify-wt":
        res = verify_worktree(args.path)
        print(json.dumps(res, indent=2))
        return 0 if res["verified"] else 1

    if args.subcommand == "verify-pr":
        res = verify_pr(args.repo, args.number, expected_branch=args.branch)
        print(json.dumps(res, indent=2))
        return 0 if res["verified"] else 1

    if args.subcommand == "audit":
        if not args.session_dir:
            session_dir = Path("C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/-development-polysimulator/2026-08-28T17-33-52-246Z_01a0496f-64f6-733e-a9a6-89f15fc2a437")
        else:
            session_dir = Path(args.session_dir)

        if not session_dir.exists():
            sys.stderr.write(f"Session directory not found: {session_dir}\n")
            return 1

        lane_names = [s.strip() for s in args.lanes.split(",")] if args.lanes else None

        audited: List[Dict[str, Any]] = []
        if lane_names:
            for lname in lane_names:
                lfile = session_dir / f"{lname}.jsonl"
                if lfile.exists():
                    audited.append(audit_lane(lfile))
                else:
                    sys.stderr.write(f"Lane file not found: {lfile}\n")
        else:
            for f in session_dir.glob("*.jsonl"):
                if not f.name.startswith("__"):
                    audited.append(audit_lane(f))

        if args.json:
            out_str = json.dumps(audited, indent=2)
        else:
            out_str = format_markdown_table(audited)

        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(out_str, encoding="utf-8")
            print(f"Wrote audit output to {out_path}")
        else:
            print(out_str)

        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
