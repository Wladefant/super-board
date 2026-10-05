#!/usr/bin/env python3
"""close_guard.py - refuse to close an issue that is not proven done.

Policy (profile AGENTS.md section 5, "Close Only When Every Box Is Proven";
https://github.com/Wladefant/super-board/issues/440):

  1. An issue with any unchecked `- [ ]` box in its body is not closed.
  2. An issue created in the same run, or less than MIN_AGE_SECONDS ago, is not
     closed. A plan issue created and closed seconds later is not a result.
  3. The only way around either rule is an explicit force reason. The reason is
     recorded in a comment on the issue before the close goes ahead. If the
     comment cannot be posted, the close is refused.

The module is pure standard library. Network access (`gh api`) happens only in
`fetch_issue` / `post_comment`, and both take a timeout. Callers inject fakes.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

MIN_AGE_SECONDS = 600  # 10 minutes

# A markdown task-list box that is still open: `- [ ]`, `* [ ]`, `+ [ ]`, `1. [ ]`.
UNCHECKED_BOX_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+\[ \](?:[ \t].*)?$", re.MULTILINE)
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$", re.MULTILINE | re.DOTALL)

PLAN_LABELS = ("kind:plan", "kind:research", "plan", "research")
PLAN_TITLE_RE = re.compile(r"(?i)(?:^|[\s\[(:\-])(plan|research|proposal)(?:$|[\s\]):\-])")

# Issues created by this process: (repo, number). Closing one of these in the
# same run is refused whatever the clock says.
_CREATED_THIS_RUN: Set[Tuple[str, int]] = set()


class CloseRefused(ValueError):
    """Raised when the close guard blocks a close."""


def note_created(repo: str, issue_number: int) -> None:
    """Record that this process created the issue (blocks a same-run close)."""
    _CREATED_THIS_RUN.add((repo, int(issue_number)))


def _reset_created_for_tests() -> None:
    _CREATED_THIS_RUN.clear()


def unchecked_boxes(body: Optional[str]) -> List[str]:
    """Return the unchecked task-list lines in a body (fenced code is ignored)."""
    text = _FENCE_RE.sub("", body or "")
    return [m.group(0).strip() for m in UNCHECKED_BOX_RE.finditer(text)]


def parse_github_time(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def age_seconds(created_at: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    created = parse_github_time(created_at)
    if created is None:
        return None
    return ((now or datetime.now(timezone.utc)) - created).total_seconds()


def is_plan_issue(title: str, labels: List[str]) -> bool:
    lowered = {str(l).lower() for l in labels}
    if any(l in lowered for l in PLAN_LABELS):
        return True
    return bool(PLAN_TITLE_RE.search(title or ""))


def issue_labels(issue: Dict[str, Any]) -> List[str]:
    return [l.get("name", "") if isinstance(l, dict) else str(l) for l in issue.get("labels") or []]


def close_blockers(
    issue: Dict[str, Any],
    repo: Optional[str] = None,
    now: Optional[datetime] = None,
) -> List[str]:
    """Return the reasons this issue must not be closed now (empty list = may close).

    A missing or unparseable creation time is a blocker: the guard fails closed.
    """
    reasons: List[str] = []
    boxes = unchecked_boxes(issue.get("body"))
    if boxes:
        shown = "; ".join(boxes[:3]) + (f"; +{len(boxes) - 3} more" if len(boxes) > 3 else "")
        reasons.append(f"{len(boxes)} unchecked box(es) in the body: {shown}")

    number = issue.get("number")
    if repo and number is not None and (repo, int(number)) in _CREATED_THIS_RUN:
        reasons.append("issue was created in this same run")

    age = age_seconds(issue.get("created_at"), now)
    if age is None:
        reasons.append("creation time unknown (guard fails closed)")
    elif age < MIN_AGE_SECONDS:
        reasons.append(f"issue is only {int(age)}s old (minimum {MIN_AGE_SECONDS}s)")
    return reasons


def _run_gh(args: List[str], timeout_sec: int, input_text: Optional[str] = None) -> Tuple[int, str, str]:
    try:
        res = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            input=input_text,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:  # timeout, missing gh
        return 1, "", str(e)
    return res.returncode, res.stdout or "", res.stderr or ""


def fetch_issue(repo: str, issue_number: int, timeout_sec: int = 15) -> Dict[str, Any]:
    """Fetch one issue via `gh api`. Raises RuntimeError on any failure (fail closed)."""
    rc, out, err = _run_gh(["api", f"repos/{repo}/issues/{int(issue_number)}"], timeout_sec)
    if rc != 0 or not out.strip():
        raise RuntimeError(f"gh api issue {repo}#{issue_number} failed: {(err or out).strip()}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gh api issue {repo}#{issue_number} returned invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"gh api issue {repo}#{issue_number} returned {type(data).__name__}")
    return data


def post_comment(repo: str, issue_number: int, body: str, timeout_sec: int = 20) -> None:
    """Post an issue comment. Raises RuntimeError on failure."""
    rc, out, err = _run_gh(
        ["issue", "comment", str(int(issue_number)), "-R", repo, "--body-file", "-"],
        timeout_sec,
        input_text=body,
    )
    if rc != 0:
        raise RuntimeError(f"gh issue comment {repo}#{issue_number} failed: {(err or out).strip()}")


def force_close_comment(reasons: List[str], force_reason: str, actor: Optional[str]) -> str:
    lines = [
        "### Close guard overridden (`--force-close`)",
        "",
        f"- **Reason given:** {force_reason}",
        f"- **Actor:** {actor or 'unknown'}",
        "- **Guard findings that were overridden:**",
    ]
    lines += [f"  - {r}" for r in reasons]
    lines += ["", "Rule: profile AGENTS.md section 5, https://github.com/Wladefant/super-board/issues/440"]
    return "\n".join(lines)


def guard_close(
    repo: str,
    issue_number: int,
    *,
    force_reason: Optional[str] = None,
    actor: Optional[str] = None,
    fetcher: Optional[Callable[[str, int], Dict[str, Any]]] = None,
    commenter: Optional[Callable[[str, int, str], None]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Decide whether `repo#issue_number` may be closed.

    Returns {"allowed": True, "forced": bool, "overridden": [...]} or raises
    CloseRefused. Fetch failures raise CloseRefused (fail closed).
    """
    try:
        issue = (fetcher or fetch_issue)(repo, int(issue_number))
    except Exception as e:
        raise CloseRefused(
            f"Cannot close {repo}#{issue_number}: could not read the issue ({e}). Close guard fails closed."
        ) from e
    if not isinstance(issue, dict):
        raise CloseRefused(f"Cannot close {repo}#{issue_number}: issue lookup returned {type(issue).__name__}.")

    reasons = close_blockers(issue, repo=repo, now=now)
    if not reasons:
        return {"allowed": True, "forced": False, "overridden": []}

    reason = (force_reason or "").strip()
    if not reason:
        raise CloseRefused(
            f"Cannot close {repo}#{issue_number}: "
            + "; ".join(reasons)
            + ". Tick every box with proof and wait out the age floor, or pass an explicit "
            "--force-close <reason> (recorded in a comment)."
        )

    try:
        (commenter or post_comment)(repo, int(issue_number), force_close_comment(reasons, reason, actor))
    except Exception as e:
        raise CloseRefused(
            f"Cannot force-close {repo}#{issue_number}: the override could not be recorded in a comment ({e})."
        ) from e
    return {"allowed": True, "forced": True, "overridden": reasons}
