#!/usr/bin/env python3
"""Exact-SHA QA ledger, failure disposition, and evidence binding.

QA proves one thing: *this exact commit* passed. Everything here exists to stop
the two ways that claim quietly becomes a lie:

1. **Testing something other than what was resolved.** The head SHA is read from
   the linked pull request and recorded BEFORE any command runs. The tests then
   execute in a detached, per-item **locked** worktree created from that SHA. A
   mutable branch checkout is never QA authority — a branch can move under the
   test run and the evidence would name a commit that was never tested.
2. **Publishing a result for a commit that has since moved.** The head is reread
   AFTER the run. Success publishes only when the reread SHA equals the tested
   SHA; anything else discards the result. A later head inherits nothing.

A missing, ambiguous, changed, or unreadable head refuses to run — the pipeline
never falls back to "whatever is checked out".

The worktree and the lock are released on EVERY terminal path: success, test
failure, an exception, a stale head, and a signal. `locked_qa_worktree` installs
SIGINT/SIGTERM handlers for the duration of the block precisely so an operator's
Ctrl-C cannot leave a lock behind that blocks the next run forever.

On failure the card never merges and never moves to Done. It moves to:

  Building   the current worker can repair it;
  Blocked    external input is required;
  Blocked    + exactly one structured follow-up issue, when the failure is
             outside the current acceptance criteria.

An unrecognised failure kind fails closed to Blocked with no follow-up.

CLI:

    python -m super_board_runtime.qa resolve --pull-request URL [--payload FILE]
    python -m super_board_runtime.qa disposition --failure-kind KIND --config CFG

Exit 0 success, 64 invalid invocation, 65 invalid configuration or input.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

try:  # normal package import
    from . import EXIT_CONFIG, EXIT_OK, EXIT_USAGE
    from .config import ConfigError, NormalizedConfig, load_and_validate_config
except ImportError:  # executed as a plain file path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from super_board_runtime import EXIT_CONFIG, EXIT_OK, EXIT_USAGE
    from super_board_runtime.config import (
        ConfigError,
        NormalizedConfig,
        load_and_validate_config,
    )

#: The one SHA-bound required check QA publishes. Bound to the tested commit,
#: never to a branch name.
QA_CHECK_CONTEXT = "superboard/exact-sha-qa"

#: The three dispositions a QA failure may carry. Anything else fails closed.
QA_FAILURE_KINDS: tuple[str, ...] = ("repairable", "external-input", "outside-acceptance")

#: The statuses whose recorded `tested_sha` is rechecked against the live head
#: on every pass. A card parked anywhere else is not claiming passing evidence.
QA_FRESHNESS_STATUSES: tuple[str, ...] = ("QA", "Review")

#: The only statuses a QA failure may produce. `Done` is structurally absent:
#: the runtime never merges and never completes work.
QA_FAILURE_STATUSES: tuple[str, ...] = ("Building", "Blocked")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class QaError(Exception):
    """Invalid QA input or a refused QA precondition. Maps to exit code 65."""

    exit_code = EXIT_CONFIG

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# ───────────────────────────── head resolution ─────────────────────────────


@dataclass(frozen=True)
class PullRequestHead:
    pull_request_url: str
    pull_request_node_id: Optional[str]
    head_ref: Optional[str]
    head_sha: str
    base_ref: Optional[str]
    is_draft: bool
    mergeable: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


def _gh_pull_request_view(url: str) -> Any:
    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            url,
            "--json",
            "url,id,headRefName,headRefOid,baseRefName,isDraft,mergeable",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def resolve_pull_request_head(
    pr_url: str,
    *,
    fetch: Optional[Callable[[str], Any]] = None,
    expected_sha: Optional[str] = None,
) -> PullRequestHead:
    """Read the pull request's `headRefOid` and record it.

    ``expected_sha`` turns this into the *reread*: a head that moved since the
    recorded SHA raises ``qa-head-changed`` rather than quietly re-pointing QA
    at a commit nobody tested.
    """
    if not isinstance(pr_url, str) or not pr_url.strip():
        raise QaError("qa-pull-request-url-invalid", "a pull request URL is required")
    fetch = _gh_pull_request_view if fetch is None else fetch
    try:
        payload = fetch(pr_url)
    except Exception as exc:  # a failed lookup is never permissive
        raise QaError(
            "qa-pull-request-unresolved",
            f"the linked pull request could not be resolved: {exc}",
        ) from exc
    if not isinstance(payload, Mapping):
        raise QaError(
            "qa-pull-request-unresolved",
            "the linked pull request could not be resolved; refusing to test an unknown commit",
        )

    raw_sha = payload.get("headRefOid")
    if raw_sha is None or (isinstance(raw_sha, str) and not raw_sha.strip()):
        raise QaError(
            "qa-head-missing",
            "the pull request carries no headRefOid; QA never falls back to the current checkout",
        )
    if not isinstance(raw_sha, str) or not _SHA_RE.match(raw_sha.strip().lower()):
        raise QaError(
            "qa-head-invalid",
            "headRefOid must be a 40-character commit SHA; a ref name is not testable evidence",
        )
    head_sha = raw_sha.strip().lower()

    if expected_sha is not None:
        if not isinstance(expected_sha, str) or not _SHA_RE.match(expected_sha.strip().lower()):
            raise QaError("qa-head-invalid", "the recorded tested SHA is not a 40-character SHA")
        if head_sha != expected_sha.strip().lower():
            raise QaError(
                "qa-head-changed",
                "the pull request head moved since the tested SHA was recorded; "
                "reconcile the issue/pull-request linkage and run QA again",
            )

    is_draft = payload.get("isDraft")
    return PullRequestHead(
        pull_request_url=str(payload.get("url") or pr_url),
        pull_request_node_id=payload.get("id") if isinstance(payload.get("id"), str) else None,
        head_ref=payload.get("headRefName") if isinstance(payload.get("headRefName"), str) else None,
        head_sha=head_sha,
        base_ref=payload.get("baseRefName") if isinstance(payload.get("baseRefName"), str) else None,
        is_draft=bool(is_draft) if isinstance(is_draft, bool) else False,
        mergeable=payload.get("mergeable") if isinstance(payload.get("mergeable"), str) else None,
    )


def resolve_linked_pull_request(issue_url: str, linked: Sequence[str]) -> str:
    """Return the single pull request linked to an issue.

    Zero links and more than one link both refuse: QA that guesses which pull
    request an issue means is QA that can attest to the wrong commit.
    """
    urls = [u.strip() for u in (linked or ()) if isinstance(u, str) and u.strip()]
    unique = list(dict.fromkeys(urls))
    if not unique:
        raise QaError(
            "qa-linkage-missing",
            f"no pull request is linked to {issue_url}; QA has nothing to bind evidence to",
        )
    if len(unique) > 1:
        raise QaError(
            "qa-linkage-ambiguous",
            f"{len(unique)} pull requests are linked to {issue_url}; reconcile the linkage first",
        )
    return unique[0]


# ───────────────────────────── locked worktree ─────────────────────────────


@dataclass(frozen=True)
class QaWorktree:
    path: Path
    lock_path: Path
    tested_sha: str
    detached: bool


def _default_git(argv: Sequence[str]) -> None:
    subprocess.run(["git", *argv], check=True, capture_output=True, text=True, timeout=600)


@contextmanager
def locked_qa_worktree(
    *,
    root: Path,
    item_key: str,
    tested_sha: str,
    checkout: str = "detached",
    git: Optional[Callable[[Sequence[str]], Any]] = None,
    remote: str = "origin",
) -> Iterator[QaWorktree]:
    """Isolated, locked, detached worktree at exactly ``tested_sha``.

    Released on every terminal path — success, failure, exception, stale head,
    and signal. The signal handlers are installed only for the duration of the
    block and restored on the way out.
    """
    if checkout != "detached":
        raise QaError(
            "qa-mutable-checkout-refused",
            "QA authority is a detached worktree at the tested SHA; a mutable branch "
            "checkout can move under the run and would attest to an untested commit",
        )
    if not isinstance(tested_sha, str) or not _SHA_RE.match(tested_sha.strip().lower()):
        raise QaError("qa-head-invalid", "the tested SHA must be a 40-character commit SHA")
    sha = tested_sha.strip().lower()
    if not isinstance(item_key, str) or not item_key.strip():
        raise QaError("qa-item-key-invalid", "a per-item lock key is required")
    key = re.sub(r"[^A-Za-z0-9._-]", "-", item_key.strip())

    git = _default_git if git is None else git
    root = Path(root)
    lock_dir = root / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{key}.lock"
    worktree_path = root / "worktrees" / f"{key}-{sha[:12]}"

    try:
        handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise QaError(
            "qa-worktree-locked",
            f"another QA run already holds the lock for {item_key}; refusing to run two "
            f"attestations for one item",
        ) from exc
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"item_key": item_key, "tested_sha": sha, "pid": os.getpid()}, sort_keys=True
            )
        )

    previous_handlers: list[tuple[int, Any]] = []

    def _on_signal(signum, frame):  # pragma: no cover - exercised by real signals only
        raise KeyboardInterrupt(f"QA interrupted by signal {signum}")

    for signum in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if signum is None:
            continue
        try:
            previous_handlers.append((signum, signal.signal(signum, _on_signal)))
        except (ValueError, OSError, AttributeError):
            # Not the main thread, or the platform has no such signal. The
            # finally block below still runs for every exception path.
            pass

    created = False
    try:
        git(["fetch", remote, sha])
        git(["worktree", "add", "--detach", str(worktree_path), sha])
        created = True
        yield QaWorktree(
            path=worktree_path, lock_path=lock_path, tested_sha=sha, detached=True
        )
    finally:
        for signum, handler in previous_handlers:
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        if created:
            try:
                git(["worktree", "remove", "--force", str(worktree_path)])
            except Exception:
                pass
        shutil.rmtree(worktree_path, ignore_errors=True)
        try:
            lock_path.unlink()
        except OSError:
            pass


# ───────────────────────────── ledger ─────────────────────────────


@dataclass(frozen=True)
class QaResult:
    issue_url: str
    issue_node_id: Optional[str]
    pull_request_url: str
    pull_request_node_id: Optional[str]
    tested_sha: str
    current_head_sha: str
    selected_base_branch: Optional[str]
    branch_declaration: Optional[str]
    result: str
    failure_kind: Optional[str]
    started_at: str
    completed_at: str
    check_url: Optional[str] = None
    sanitized_evidence_url: Optional[str] = None


@dataclass(frozen=True)
class QaLedgerEntry:
    schema_version: int
    issue_url: str
    issue_node_id: Optional[str]
    pull_request_url: str
    pull_request_node_id: Optional[str]
    tested_sha: str
    current_head_sha: str
    selected_base_branch: Optional[str]
    branch_declaration: Optional[str]
    check_context: str
    check_url: Optional[str]
    sanitized_evidence_url: Optional[str]
    result: str
    invalidated: bool
    started_at: str
    completed_at: str

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)


def _normalize_sha(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.match(value.strip().lower()):
        raise QaError(reason, "a 40-character commit SHA is required")
    return value.strip().lower()


def record_qa_result(result: QaResult) -> QaLedgerEntry:
    """Turn one QA run into its ledger entry.

    The reread head is compared with the tested SHA here, once, so no caller can
    forget to. A mismatch discards the result: the entry is recorded as
    ``discarded`` and ``invalidated``, and publication refuses it.
    """
    tested = _normalize_sha(result.tested_sha, "qa-head-invalid")
    current = _normalize_sha(result.current_head_sha, "qa-head-invalid")
    moved = tested != current
    outcome = result.result
    if outcome not in ("success", "failure"):
        raise QaError("qa-result-invalid", "a QA result is either 'success' or 'failure'")
    if moved:
        outcome = "discarded"
    return QaLedgerEntry(
        schema_version=1,
        issue_url=result.issue_url,
        issue_node_id=result.issue_node_id,
        pull_request_url=result.pull_request_url,
        pull_request_node_id=result.pull_request_node_id,
        tested_sha=tested,
        current_head_sha=current,
        selected_base_branch=result.selected_base_branch,
        branch_declaration=result.branch_declaration,
        check_context=QA_CHECK_CONTEXT,
        check_url=result.check_url,
        sanitized_evidence_url=result.sanitized_evidence_url,
        result=outcome,
        invalidated=moved,
        started_at=result.started_at,
        completed_at=result.completed_at,
    )


def publish_qa_status(
    entry: QaLedgerEntry,
    *,
    writer: Callable[[Mapping[str, Any]], Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Publish the SHA-bound status for a successful, non-invalidated entry.

    The status is created on the **tested** commit, never on a branch, so a
    later head inherits nothing. A dry run issues zero GitHub writes.
    """
    if entry.invalidated or entry.result == "discarded":
        raise QaError(
            "qa-result-discarded",
            "the pull request head moved during the run; the result attests to a commit "
            "that is no longer the head and must not be published",
        )
    if entry.result != "success":
        raise QaError("qa-result-not-success", "only a successful QA run publishes a passing status")
    payload = {
        "context": entry.check_context,
        "description": f"exact-SHA QA passed on {entry.tested_sha[:12]}",
        "sha": entry.tested_sha,
        "state": "success",
        "target_url": entry.sanitized_evidence_url,
    }
    if dry_run:
        return {"dry_run": True, "github_writes": 0, "payload": payload, "published": False}
    response = writer(payload)
    url = response.get("url") if isinstance(response, Mapping) else None
    return {
        "dry_run": False,
        "github_writes": 1,
        "payload": payload,
        "published": True,
        "url": url,
    }


def inherited_check_state(entry: QaLedgerEntry, head_sha: Any) -> Optional[str]:
    """The state a given head may claim from this entry.

    Only the tested commit itself. Any other head — including a descendant —
    inherits nothing, which is the entire point of binding evidence to a SHA.
    """
    if entry.invalidated or entry.result != "success":
        return None
    try:
        candidate = _normalize_sha(head_sha, "qa-head-invalid")
    except QaError:
        return None
    return "success" if candidate == entry.tested_sha else None


# ───────────────────────────── freshness ─────────────────────────────


@dataclass(frozen=True)
class QaFreshness:
    fresh: bool
    invalidated: bool
    tested_sha: str
    current_head_sha: Optional[str]
    next_status: Optional[str]
    pending_status_sha: Optional[str]
    reason_code: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


def requires_freshness_check(status: Any) -> bool:
    """True for the statuses that claim passing QA evidence."""
    return isinstance(status, str) and status.strip() in QA_FRESHNESS_STATUSES


def pending_check_for_head(head_sha: Any) -> dict[str, Any]:
    """The pending status a newly-arrived head carries until it is tested itself."""
    sha = _normalize_sha(head_sha, "qa-head-invalid")
    return {
        "context": QA_CHECK_CONTEXT,
        "description": "exact-SHA QA required for this commit",
        "sha": sha,
        "state": "pending",
    }


def validate_qa_freshness(entry: QaLedgerEntry, current_head_sha: Any) -> QaFreshness:
    """Compare a recorded `tested_sha` with the live head.

    A changed head is not a warning: the evidence describes a commit that is no
    longer the head, so the card goes back to QA and the new head starts
    pending. An unreadable head fails closed the same way — never fresh.
    """
    try:
        current = _normalize_sha(current_head_sha, "qa-head-invalid")
    except QaError:
        return QaFreshness(
            fresh=False,
            invalidated=True,
            tested_sha=entry.tested_sha,
            current_head_sha=None,
            next_status="QA",
            pending_status_sha=None,
            reason_code="qa-head-unreadable",
        )
    if entry.invalidated or entry.result != "success":
        return QaFreshness(
            fresh=False,
            invalidated=True,
            tested_sha=entry.tested_sha,
            current_head_sha=current,
            next_status="QA",
            pending_status_sha=current,
            reason_code="qa-evidence-not-success",
        )
    if current != entry.tested_sha:
        return QaFreshness(
            fresh=False,
            invalidated=True,
            tested_sha=entry.tested_sha,
            current_head_sha=current,
            next_status="QA",
            pending_status_sha=current,
            reason_code="qa-head-moved",
        )
    return QaFreshness(
        fresh=True,
        invalidated=False,
        tested_sha=entry.tested_sha,
        current_head_sha=current,
        next_status=None,
        pending_status_sha=None,
        reason_code=None,
    )


def invalidate_qa_entry(entry: QaLedgerEntry, current_head_sha: Any) -> QaLedgerEntry:
    """Return a NEW entry marking the evidence invalidated.

    The original is left exactly as it was recorded. "What did we test, and
    when" has to stay answerable after the head moves, so the ledger appends an
    invalidation rather than rewriting history.
    """
    current = _normalize_sha(current_head_sha, "qa-head-invalid")
    return QaLedgerEntry(
        schema_version=entry.schema_version,
        issue_url=entry.issue_url,
        issue_node_id=entry.issue_node_id,
        pull_request_url=entry.pull_request_url,
        pull_request_node_id=entry.pull_request_node_id,
        tested_sha=entry.tested_sha,
        current_head_sha=current,
        selected_base_branch=entry.selected_base_branch,
        branch_declaration=entry.branch_declaration,
        check_context=entry.check_context,
        check_url=entry.check_url,
        sanitized_evidence_url=entry.sanitized_evidence_url,
        result="invalidated",
        invalidated=True,
        started_at=entry.started_at,
        completed_at=entry.completed_at,
    )


# ───────────────────────────── merge handoff ─────────────────────────────


@dataclass(frozen=True)
class MergeHandoffDecision:
    merge_ready: bool
    reason_code: Optional[str]
    tested_sha: str
    current_head_sha: Optional[str]
    check_context: str = QA_CHECK_CONTEXT

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


def validate_merge_handoff(
    entry: QaLedgerEntry, head: PullRequestHead, check_conclusion: Any
) -> MergeHandoffDecision:
    """The last gate before a human merges. Read-only, and never fails open.

    Rereads nothing itself — the caller supplies the freshly-read head and the
    conclusion of the SHA-bound required check — so this function issues zero
    writes and zero API calls. `merge_ready` is true only when the live head is
    the tested commit AND the required check on that commit concluded success.
    """
    current = head.head_sha if isinstance(head, PullRequestHead) else None

    def refuse(reason: str) -> MergeHandoffDecision:
        return MergeHandoffDecision(False, reason, entry.tested_sha, current)

    if entry.invalidated or entry.result == "discarded" or entry.result == "invalidated":
        return refuse("qa-evidence-invalidated")
    if entry.result != "success":
        return refuse("qa-evidence-not-success")
    try:
        current = _normalize_sha(current, "qa-head-invalid")
    except QaError:
        return refuse("head-moved")
    if current != entry.tested_sha:
        return refuse("head-moved")
    if not isinstance(check_conclusion, str) or not check_conclusion.strip():
        return refuse("check-missing")
    if check_conclusion.strip().lower() != "success":
        return refuse("check-not-success")
    return MergeHandoffDecision(True, None, entry.tested_sha, current)


# ───────────────────────────── failure disposition ─────────────────────────────


@dataclass(frozen=True)
class QaFailureDisposition:
    next_status: str
    follow_up_issue_required: bool
    reason_code: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


def disposition_qa_failure(
    result: QaResult, config: NormalizedConfig
) -> QaFailureDisposition:
    """Where a failed QA run sends the card. Never Done, never a merge."""
    if result.result != "failure":
        raise QaError(
            "qa-result-not-a-failure",
            "disposition_qa_failure only applies to a failed QA run",
        )
    kind = result.failure_kind if isinstance(result.failure_kind, str) else None
    if kind == "repairable":
        disposition = QaFailureDisposition("Building", False, "qa-failure-repairable")
    elif kind == "external-input":
        disposition = QaFailureDisposition("Blocked", False, "qa-failure-external-input")
    elif kind == "outside-acceptance":
        disposition = QaFailureDisposition("Blocked", True, "qa-failure-outside-acceptance")
    else:
        # Fail closed. An unclassified failure parks the card for a human rather
        # than guessing that the current worker can repair it.
        disposition = QaFailureDisposition("Blocked", False, "qa-failure-kind-unknown")
    if disposition.next_status not in QA_FAILURE_STATUSES:  # pragma: no cover - guard
        raise QaError("qa-disposition-invalid", "a QA failure may only move to Building or Blocked")
    return disposition


def build_follow_up_issue(result: QaResult, config: NormalizedConfig) -> dict[str, Any]:
    """The one structured follow-up an out-of-scope QA failure files.

    Carries identifiers and the tested SHA only. No logs, no command output, no
    environment: everything that could carry a secret is published through the
    sanitizer instead (`super-board-publish.py`).
    """
    return {
        "body": "\n".join(
            [
                "QA failed outside the current acceptance criteria.",
                "",
                f"- Source issue: {result.issue_url}",
                f"- Pull request: {result.pull_request_url}",
                f"- Tested SHA: `{result.tested_sha}`",
                f"- Base branch: {result.selected_base_branch}",
                f"- Required check: `{QA_CHECK_CONTEXT}`",
                "",
                "The original issue stays Blocked. Evidence is published separately",
                "through the sanitizing publication boundary.",
            ]
        ),
        "repo": config.repo_remote,
        "title": f"QA follow-up: out-of-scope failure on {result.tested_sha[:12]}",
    }


def file_qa_failure(
    result: QaResult,
    config: NormalizedConfig,
    *,
    issue_writer: Callable[[Mapping[str, Any]], Any],
    dry_run: bool = False,
) -> QaFailureDisposition:
    """Apply the disposition, filing at most one follow-up issue.

    Exactly one write when the failure is outside the acceptance criteria and
    the run is not a dry run; zero writes otherwise.
    """
    disposition = disposition_qa_failure(result, config)
    if disposition.follow_up_issue_required and not dry_run:
        issue_writer(build_follow_up_issue(result, config))
    return disposition


def utc_now() -> str:
    """RFC3339 UTC timestamp, the only time format the evidence records use."""
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ───────────────────────────── CLI ─────────────────────────────


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        print(f"super-board-qa: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="super_board_runtime.qa", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    resolve = sub.add_parser("resolve", help="resolve and record the exact head SHA")
    resolve.add_argument("--pull-request", required=True)
    resolve.add_argument("--payload", default=None, help="read the PR payload from a file, not gh")
    resolve.add_argument("--expected-sha", default=None, help="reread: refuse a changed head")
    resolve.add_argument("--checkout", default="detached", help="QA authority checkout mode")

    handoff = sub.add_parser(
        "merge-handoff", help="read-only: may this item be reported merge-ready?"
    )
    handoff.add_argument("--ledger", required=True, help="the QA ledger entry JSON file")
    handoff.add_argument("--pull-request", required=True)
    handoff.add_argument("--payload", default=None, help="read the PR payload from a file, not gh")
    handoff.add_argument(
        "--check-conclusion",
        default=None,
        help=f"conclusion of the {QA_CHECK_CONTEXT} status on the tested SHA",
    )

    disposition = sub.add_parser("disposition", help="where a failed QA run sends the card")
    disposition.add_argument("--config", required=True)
    disposition.add_argument("--issue-url", required=True)
    disposition.add_argument("--pull-request", required=True)
    disposition.add_argument("--tested-sha", required=True)
    disposition.add_argument("--failure-kind", default=None)
    disposition.add_argument("--dry-run", action="store_true")
    return parser


def _load_config_or_exit(path: str) -> NormalizedConfig:
    try:
        return load_and_validate_config(Path(path))
    except ConfigError as exc:
        print(f"super-board-qa: invalid config: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": exc.reason}, sort_keys=True), file=sys.stderr)
        raise SystemExit(EXIT_CONFIG) from exc


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "resolve":
        fetch = None
        if args.payload:
            payload_path = Path(args.payload)

            def fetch(_url):  # noqa: F811 - deliberate local rebinding
                return json.loads(payload_path.read_text(encoding="utf-8"))

        try:
            if args.checkout != "detached":
                raise QaError(
                    "qa-mutable-checkout-refused",
                    "QA authority is a detached worktree at the tested SHA",
                )
            head = resolve_pull_request_head(
                args.pull_request, fetch=fetch, expected_sha=args.expected_sha
            )
        except QaError as exc:
            print(f"super-board-qa: {exc}", file=sys.stderr)
            print(json.dumps({"ok": False, "reason": exc.reason}, sort_keys=True), file=sys.stderr)
            return exc.exit_code
        body = head.to_dict()
        body.update(
            {
                "check_context": QA_CHECK_CONTEXT,
                "checkout": args.checkout,
                "ok": True,
                "resolved_at": utc_now(),
                "tested_sha": head.head_sha,
            }
        )
        print(json.dumps(body, sort_keys=True))
        return EXIT_OK

    if args.command == "merge-handoff":
        fetch = None
        if args.payload:
            payload_path = Path(args.payload)

            def fetch(_url):  # noqa: F811 - deliberate local rebinding
                return json.loads(payload_path.read_text(encoding="utf-8"))

        try:
            raw = json.loads(Path(args.ledger).read_text(encoding="utf-8"))
            entry = QaLedgerEntry(**raw)
            head = resolve_pull_request_head(args.pull_request, fetch=fetch)
        except QaError as exc:
            # An unreadable head is not merge-ready; report it, never fail open.
            body = MergeHandoffDecision(False, exc.reason, str(raw.get("tested_sha", "")), None)
            print(json.dumps({**body.to_dict(), "ok": False}, sort_keys=True), file=sys.stderr)
            print(f"super-board-qa: {exc}", file=sys.stderr)
            return exc.exit_code
        except (OSError, TypeError, ValueError) as exc:
            print(f"super-board-qa: unreadable QA ledger entry: {exc}", file=sys.stderr)
            print(
                json.dumps({"ok": False, "reason": "qa-ledger-invalid"}, sort_keys=True),
                file=sys.stderr,
            )
            return EXIT_CONFIG
        decision = validate_merge_handoff(entry, head, args.check_conclusion)
        print(json.dumps({**decision.to_dict(), "ok": True}, sort_keys=True))
        return EXIT_OK

    config = _load_config_or_exit(args.config)
    result = QaResult(
        issue_url=args.issue_url,
        issue_node_id=None,
        pull_request_url=args.pull_request,
        pull_request_node_id=None,
        tested_sha=args.tested_sha,
        current_head_sha=args.tested_sha,
        selected_base_branch=config.base_branch,
        branch_declaration=config.base_branch,
        result="failure",
        failure_kind=args.failure_kind,
        started_at=utc_now(),
        completed_at=utc_now(),
    )
    writes: list[Any] = []
    try:
        disposition = file_qa_failure(
            result, config, issue_writer=writes.append, dry_run=args.dry_run
        )
    except QaError as exc:
        print(f"super-board-qa: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": exc.reason}, sort_keys=True), file=sys.stderr)
        return exc.exit_code
    body = disposition.to_dict()
    body.update(
        {
            "dry_run": bool(args.dry_run),
            "github_writes": len(writes),
            "ok": True,
            "tested_sha": args.tested_sha,
        }
    )
    # The follow-up is emitted as a payload, never written from here: every
    # GitHub-bound byte leaves through `super-board-publish.py` so exactly one
    # sanitizer sees it.
    if disposition.follow_up_issue_required:
        follow_up = build_follow_up_issue(result, config)
        body["follow_up"] = {
            "surface": "bug-report",
            "text": f"# {follow_up['title']}\n\n{follow_up['body']}",
            "title": follow_up["title"],
        }
    print(json.dumps(body, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "QA_CHECK_CONTEXT",
    "QA_FAILURE_KINDS",
    "QA_FAILURE_STATUSES",
    "QA_FRESHNESS_STATUSES",
    "MergeHandoffDecision",
    "PullRequestHead",
    "QaError",
    "QaFailureDisposition",
    "QaFreshness",
    "QaLedgerEntry",
    "QaResult",
    "QaWorktree",
    "build_follow_up_issue",
    "disposition_qa_failure",
    "file_qa_failure",
    "inherited_check_state",
    "invalidate_qa_entry",
    "locked_qa_worktree",
    "pending_check_for_head",
    "publish_qa_status",
    "record_qa_result",
    "requires_freshness_check",
    "resolve_linked_pull_request",
    "resolve_pull_request_head",
    "utc_now",
    "validate_merge_handoff",
    "validate_qa_freshness",
]
