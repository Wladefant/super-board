#!/usr/bin/env python3
"""
Request Ledger Utility (workflows/ledger.py)

Machine-local durable request ledger and restart recovery cache using Python standard library.
Pure standard library implementation with no harness or framework imports.

Architecture:
  - GitHub Issues & Superboard are the shared authoritative system of record.
  - Local JSON ledger is a machine-local, atomic, crash-resilient recovery cache.
  - Enforces strict invariants:
      * Explicit transition graphs distinguishing deployable vs local-doc/harness tasks
        (explicitly not-applicable deployment stages, no fake deployments).
      * Dependency existence, cycle detection, and prerequisite state checks.
      * Head-bound acceptance criteria and evidence binding; full invalidation of
        criteria statuses and proofs on git head change.
      * Validated GitHub proof required for 'done' (must match repository, valid URL,
        and actual fetched remote proof when remote verification is claimed).
      * Authorization provenance tracking (no CLI default operator or self-authorization).
      * Decision API with authorized responder verification, option validation, and
        no unresolved blocker wipe bypass.
      * Re-entrant cross-platform advisory locking (msvcrt on Windows, fcntl on POSIX)
        preserving process isolation and crash safety.
      * Atomic writes (tempfile + os.replace on same filesystem).
      * Configurable state directory and ledger paths.
"""

import argparse
import datetime
import json
import os
import re
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

VALID_STATES = [
    "pending",
    "implementation",
    "QA",
    "review",
    "awaiting authorization",
    "integration",
    "live verification",
    "done",
]

DEPLOYMENT_STATES = ["integration", "live verification"]

# States that assert QA and review already completed. From here on, an unverified, missing
# or stale acceptance criterion is a contradiction of the state itself, not a pending note.
VERIFICATION_COMPLETE_STATES = [
    "awaiting authorization",
    "integration",
    "live verification",
    "done",
]

TASK_TYPES = ["deployable", "local_doc", "local", "doc", "harness"]

#: Label marking a request that may only be worked when it is named explicitly.
#:
#: The ledger holds requests a human asked for and requests a machine opened on
#: their behalf, and the difference matters at selection time. An automatically
#: created request is real work with a real owner, but nobody scoped it, so a
#: loop that picks "whatever is runnable next" must not pick it up on its own.
#: Carrying that as a label rather than a new field keeps it durable, visible in
#: every existing read path, and free for any caller to set on its own requests.
EXPLICIT_SELECTION_LABEL = "selection:explicit-only"


def requires_explicit_selection(request: Optional[Mapping[str, Any]]) -> bool:
    """
    Whether this request may only be worked when a caller names it.

    Read by every implicit selector. An explicitly named request is never filtered
    by this: naming it *is* the explicit selection the label asks for.
    """
    if not request:
        return False
    labels = ((request.get("superboard") or {}).get("labels")) or []
    return EXPLICIT_SELECTION_LABEL in [str(l) for l in labels]

# Strict state transition graph for deployable tasks (full 8-state model).
# 'awaiting authorization' asserts that implementation, QA and review are complete, so it is
# reachable only from 'review'. Allowing it directly from 'implementation' or 'QA' let a
# request reach the pre-merge gate having never been verified.
ALLOWED_TRANSITIONS_DEPLOYABLE: Dict[str, List[str]] = {
    "pending": ["implementation"],
    "implementation": ["QA"],
    "QA": ["review", "implementation"],
    "review": ["awaiting authorization", "QA", "implementation"],
    "awaiting authorization": ["integration", "implementation"],
    "integration": ["live verification", "implementation"],
    "live verification": ["done", "implementation"],
    "done": [],  # Terminal state
}

# Strict state transition graph for local-doc / harness tasks
# Deployment stages ('integration', 'live verification') are explicitly NOT applicable.
ALLOWED_TRANSITIONS_LOCAL_DOC: Dict[str, List[str]] = {
    "pending": ["implementation"],
    "implementation": ["QA"],
    "QA": ["review", "implementation"],
    "review": ["done", "QA", "implementation", "awaiting authorization"],
    "awaiting authorization": ["done", "implementation"],
    "done": [],  # Terminal state
}

# Default backwards-compatible alias
ALLOWED_TRANSITIONS = ALLOWED_TRANSITIONS_DEPLOYABLE

DEFAULT_LEDGER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ledger.json"
)
try:
    from project_adapter import get_current_project_config
    _proj_cfg = get_current_project_config()
    DEFAULT_REPO = _proj_cfg.repo
    DEFAULT_SUPERBOARD_PROJECT_NUM = _proj_cfg.project_number
except ImportError:
    DEFAULT_REPO = "Bavariance/polysimulator"
    DEFAULT_SUPERBOARD_PROJECT_NUM = 1
DEFAULT_SUPERBOARD_PROJECT_ID = "PVT_kwDODpNYWs4BIXPZ"


def get_iso_timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def validate_github_url(url: Optional[str], expected_repo: Optional[str] = None) -> bool:
    """Validate that URL is a well-formed GitHub URL matching expected repository."""
    if not url or not isinstance(url, str):
        return False
    url_clean = url.strip()

    if expected_repo:
        repo_escaped = re.escape(expected_repo.strip().lower())
        gh_pattern = rf"^https://github\.com/{repo_escaped}/(pull/\d+(#issuecomment-\d+)?|issues/\d+(#issuecomment-\d+)?|commit/[0-9a-fA-F]{{7,64}})$"
        raw_pattern = rf"^https://raw\.githubusercontent\.com/{repo_escaped}/.+$"
    else:
        gh_pattern = r"^https://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/(pull/\d+(#issuecomment-\d+)?|issues/\d+(#issuecomment-\d+)?|commit/[0-9a-fA-F]{7,64})$"
        raw_pattern = r"^https://raw\.githubusercontent\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/.+$"

    asset_pattern = r"^https://github-production-user-asset-6210df\.s3\.amazonaws\.com/.+$"

    if re.match(gh_pattern, url_clean, re.IGNORECASE):
        return True
    if re.match(raw_pattern, url_clean, re.IGNORECASE):
        return True
    if re.match(asset_pattern, url_clean, re.IGNORECASE):
        return True

    return False


def verify_remote_proof(url: str, timeout: float = 5.0) -> bool:
    """Attempt to verify proof URL exists remotely; returns False if unreachable or non-200."""
    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Ledger-Verifier/1.0"},
            method="HEAD",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 301, 302, 307, 308)
    except Exception:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Ledger-Verifier/1.0"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status in (200, 301, 302, 307, 308)
        except Exception:
            return False


def match_decision_option(answer: str, option: str) -> bool:
    """Match decision answer against an option without false positive substring matching."""
    ans = answer.strip().lower()
    opt = option.strip().lower()
    if ans == opt:
        return True

    # Check option key/prefix (e.g. 'A' from 'A: Park and Idle Wait' or 'A - Park')
    opt_key = opt.split(":")[0].split("-")[0].strip()
    ans_tokens = set(re.findall(r"\b\w+\b", ans))
    if opt_key and opt_key in ans_tokens:
        return True

    # Check if full option occurs at word boundary
    pattern = r"\b" + re.escape(opt) + r"\b"
    if re.search(pattern, ans):
        return True

    return False


def normalize_acceptance_criteria(
    acceptance_criteria: Any, head: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Normalize acceptance criteria into the ledger's stored criterion shape.

    Accepts the shapes callers actually pass:
      * a description string, which is what `--criteria '["a","b"]'` produces and
        what the CLI's own help documents as valid;
      * a mapping using either 'description' or the 'criterion' alias for the text
        (the alias is what the durable bug intake writes);
      * an already-stored criterion, which round-trips unchanged.

    A description string used to reach a mapping-only normalizer and die with
    "'str' object has no attribute 'get'": an internal type error surfaced as if
    the ledger itself were broken, from documented input. Anything genuinely
    unusable now names its position and its type instead.
    """
    if acceptance_criteria is None:
        return []
    if isinstance(acceptance_criteria, (str, bytes, dict)):
        raise ValueError(
            "Acceptance criteria must be a list of criteria, not a single "
            f"{type(acceptance_criteria).__name__}."
        )

    normalized: List[Dict[str, Any]] = []
    for i, criterion in enumerate(acceptance_criteria):
        default_id = f"AC-{i+1}"
        if isinstance(criterion, str):
            text = criterion.strip()
            if not text:
                raise ValueError(
                    f"Acceptance criterion {i+1} is an empty string; a criterion the request "
                    "will be verified against needs a description."
                )
            criterion = {"id": default_id, "description": text}
        elif not isinstance(criterion, dict):
            raise ValueError(
                f"Acceptance criterion {i+1} must be a description string or a mapping with a "
                f"'description' (or 'criterion') field, got {type(criterion).__name__}."
            )

        description = criterion.get("description")
        if description is None or not str(description).strip():
            description = criterion.get("criterion")
        status = criterion.get("status", "pending")
        verified = status == "verified"
        normalized.append({
            "id": str(criterion.get("id") or criterion.get("criterion_id") or default_id),
            "description": "" if description is None else str(description),
            "status": status,
            "evidence": criterion.get("evidence", ""),
            "head": head if verified else None,
            "verified_head": head if verified else None,
            "verified_at": criterion.get("verified_at"),
            "verified_by": criterion.get("verified_by"),
        })
    return normalized


class FileLock:
    """
    Cross-platform re-entrant advisory file lock using msvcrt (Windows) or fcntl (POSIX).
    Preserves operating system process lock isolation and kernel-level crash safety
    while allowing nested acquisition within the same thread.
    """

    _tls = threading.local()

    def __init__(self, lock_path: str, timeout: float = 15.0, retry_interval: float = 0.05):
        self.lock_path = os.path.abspath(lock_path)
        self.timeout = timeout
        self.retry_interval = retry_interval
        self.fd = None

    def acquire(self):
        depth = getattr(self._tls, f"depth_{self.lock_path}", 0)
        if depth > 0:
            setattr(self._tls, f"depth_{self.lock_path}", depth + 1)
            self.fd = getattr(self._tls, f"fd_{self.lock_path}", None)
            return self

        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        start_time = time.time()
        while True:
            try:
                # Open with a+b so file is created if missing
                self.fd = open(self.lock_path, "a+b")
                # Ensure at least 1 byte exists to lock region
                self.fd.seek(0, os.SEEK_END)
                if self.fd.tell() == 0:
                    self.fd.write(b"L")
                    self.fd.flush()

                self.fd.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(self.fd.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                setattr(self._tls, f"depth_{self.lock_path}", 1)
                setattr(self._tls, f"fd_{self.lock_path}", self.fd)
                return self
            except (OSError, IOError):
                if self.fd:
                    try:
                        self.fd.close()
                    except Exception:
                        pass
                    self.fd = None
                if time.time() - start_time >= self.timeout:
                    raise TimeoutError(
                        f"Timed out after {self.timeout}s waiting for lock: {self.lock_path}"
                    )
                time.sleep(self.retry_interval)

    def release(self):
        depth = getattr(self._tls, f"depth_{self.lock_path}", 0)
        if depth > 1:
            setattr(self._tls, f"depth_{self.lock_path}", depth - 1)
            return

        fd = getattr(self._tls, f"fd_{self.lock_path}", self.fd)
        if fd:
            try:
                fd.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            finally:
                try:
                    fd.close()
                except Exception:
                    pass

        setattr(self._tls, f"depth_{self.lock_path}", 0)
        setattr(self._tls, f"fd_{self.lock_path}", None)
        self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class RequestLedger:
    """
    Durable JSON request ledger manager with exclusive locking, atomic updates,
    and strict invariant enforcement.
    """

    def __init__(
        self,
        ledger_path: Optional[str] = None,
        state_dir: Optional[str] = None,
    ):
        if ledger_path:
            self.ledger_path = os.path.abspath(ledger_path)
        elif state_dir:
            self.ledger_path = os.path.abspath(os.path.join(state_dir, "ledger.json"))
        elif os.environ.get("LEDGER_PATH"):
            self.ledger_path = os.path.abspath(os.environ["LEDGER_PATH"])
        elif os.environ.get("STATE_DIR"):
            self.ledger_path = os.path.abspath(os.path.join(os.environ["STATE_DIR"], "ledger.json"))
        else:
            self.ledger_path = DEFAULT_LEDGER_PATH

        self.lock_path = self.ledger_path + ".lock"

    def _load_data_unlocked(self) -> Dict[str, Any]:
        if not os.path.exists(self.ledger_path):
            return {
                "version": 2,
                "role": "local_recovery_cache",
                "authority": "github_issues_and_superboard",
                "created_at": get_iso_timestamp(),
                "updated_at": get_iso_timestamp(),
                "requests": {},
            }
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {
                        "version": 2,
                        "role": "local_recovery_cache",
                        "authority": "github_issues_and_superboard",
                        "updated_at": get_iso_timestamp(),
                        "requests": {},
                    }
                return json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"Corrupt ledger file at {self.ledger_path}: {e}")

    def _save_data_unlocked(self, data: Dict[str, Any]):
        data["updated_at"] = get_iso_timestamp()
        dir_name = os.path.dirname(self.ledger_path)
        os.makedirs(dir_name, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(
            prefix=".tmp_ledger_", dir=dir_name, text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.ledger_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            raise

    @staticmethod
    def _validate_head_sha(sha: Optional[str]) -> bool:
        if not sha:
            return True
        if not isinstance(sha, str) or not sha.strip():
            raise ValueError("Git HEAD reference cannot be empty.")
        cleaned = sha.strip()
        if any(c in cleaned for c in " \t\n\r;$\"\'<>|"):
            raise ValueError(f"Invalid characters in git HEAD reference: '{cleaned}'")
        return True

    @staticmethod
    def _detect_dependency_cycles(
        existing_requests: Dict[str, Any], new_or_updated_id: str, new_deps: List[str]
    ):
        adj: Dict[str, List[str]] = {}
        for rid, rdata in existing_requests.items():
            adj[rid] = list(rdata.get("dependencies", []))
        adj[new_or_updated_id] = list(new_deps)

        visited: Dict[str, int] = {}  # 0 = unvisited, 1 = visiting, 2 = visited

        def dfs(node: str, path: List[str]):
            visited[node] = 1
            for neighbor in adj.get(node, []):
                if neighbor not in adj:
                    continue
                if visited.get(neighbor) == 1:
                    cycle_str = " -> ".join(path + [neighbor])
                    raise ValueError(f"Circular dependency detected: {cycle_str}")
                if visited.get(neighbor, 0) == 0:
                    dfs(neighbor, path + [neighbor])
            visited[node] = 2

        for node in list(adj.keys()):
            if visited.get(node, 0) == 0:
                dfs(node, [node])

    def add_request(
        self,
        req_id: str,
        prompt: str,
        session: str,
        project: str,
        acceptance_criteria: List[Dict[str, Any]],
        owner: str,
        dependencies: Optional[List[str]] = None,
        head: Optional[str] = None,
        state: str = "pending",
        task_type: str = "deployable",
        next_action: Optional[str] = None,
        authorization_required: bool = True,
        github_repo: Optional[str] = None,
        issue_number: Optional[int] = None,
        issue_url: Optional[str] = None,
        superboard_project: Optional[Union[int, str]] = None,
        superboard_card: Optional[str] = None,
        superboard_status: Optional[str] = None,
        labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if not req_id or not req_id.strip():
            raise ValueError("Request ID cannot be empty.")
        if state not in VALID_STATES:
            raise ValueError(f"Invalid initial state: '{state}'. Valid states: {VALID_STATES}")
        if task_type not in TASK_TYPES:
            raise ValueError(f"Invalid task_type: '{task_type}'. Valid types: {TASK_TYPES}")

        is_local = task_type in ["local_doc", "local", "doc", "harness"]
        if is_local and state in DEPLOYMENT_STATES:
            raise ValueError(
                f"Deployment stage '{state}' is not applicable for {task_type} tasks (no fake deployments)."
            )

        if head is not None:
            self._validate_head_sha(head)

        deps = dependencies or []
        # Validated before the lock: a malformed criterion is a rejected request, not a
        # half-written store.
        norm_criteria = normalize_acceptance_criteria(acceptance_criteria, head)

        with FileLock(self.lock_path):
            data = self._load_data_unlocked()
            if req_id in data["requests"]:
                raise ValueError(f"Request '{req_id}' already exists in ledger.")

            # Validate dependencies existence
            for dep in deps:
                if dep not in data["requests"]:
                    raise ValueError(
                        f"Cannot add request '{req_id}': Dependency '{dep}' does not exist in ledger."
                    )

            # Validate cycles
            self._detect_dependency_cycles(data["requests"], req_id, deps)

            # If initial state is not pending, dependencies must be done
            if state != "pending":
                for dep in deps:
                    dep_req = data["requests"].get(dep)
                    if not dep_req or dep_req.get("state") != "done":
                        raise ValueError(
                            f"Cannot add request '{req_id}' in state '{state}': "
                            f"Dependency '{dep}' is not 'done' (current state: '{dep_req.get('state') if dep_req else 'missing'}')."
                        )

            now = get_iso_timestamp()
            repo = github_repo or DEFAULT_REPO
            calc_issue_url = issue_url
            if issue_number and not calc_issue_url:
                calc_issue_url = f"https://github.com/{repo}/issues/{issue_number}"

            record = {
                "id": req_id,
                "prompt": prompt,
                "session": session,
                "project": project,
                "task_type": task_type,
                "deployment_applicable": not is_local,
                "acceptance_criteria": norm_criteria,
                "owner": owner,
                "dependencies": deps,
                "head": head,
                "evidence": [],
                "authorization": {
                    "required": authorization_required,
                    "status": "pending",
                    "authorized_by": None,
                    "authorized_at": None,
                    "provenance": None,
                    "notes": "",
                },
                "github": {
                    "repo": repo,
                    "issue_number": int(issue_number) if issue_number is not None else None,
                    "issue_url": calc_issue_url,
                    "proof_url": None,
                    "proof_verified": False,
                },
                "superboard": {
                    "project_number": int(superboard_project) if str(superboard_project).isdigit() else DEFAULT_SUPERBOARD_PROJECT_NUM,
                    "project_id": DEFAULT_SUPERBOARD_PROJECT_ID,
                    "item_id": superboard_card,
                    "status": superboard_status or "Backlog",
                    "labels": labels or [],
                },
                "decisions": [],
                "decision_blockers": [],
                "state": state,
                "blocker": None,
                "next_action": next_action or "Begin implementation",
                "created_at": now,
                "updated_at": now,
                "history": [
                    {
                        "timestamp": now,
                        "from_state": None,
                        "to_state": state,
                        "actor": owner,
                        "reason": "Request created",
                    }
                ],
            }

            data["requests"][req_id] = record
            self._save_data_unlocked(data)
            return record

    def update_request(
        self,
        req_id: str,
        state: Optional[str] = None,
        owner: Optional[str] = None,
        head: Optional[str] = None,
        task_type: Optional[str] = None,
        dependencies: Optional[List[str]] = None,
        blocker: Optional[str] = None,
        clear_blocker: bool = False,
        next_action: Optional[str] = None,
        criterion_update: Optional[Dict[str, Any]] = None,
        add_evidence: Optional[Dict[str, Any]] = None,
        authorization_update: Optional[Dict[str, Any]] = None,
        github_update: Optional[Dict[str, Any]] = None,
        superboard_update: Optional[Dict[str, Any]] = None,
        add_decision: Optional[Dict[str, Any]] = None,
        resolve_decision: Optional[Dict[str, Any]] = None,
        add_decision_blocker: Optional[str] = None,
        clear_decision_blocker: Optional[str] = None,
        actor: Optional[str] = None,
        reason: str = "Update",
    ) -> Dict[str, Any]:
        with FileLock(self.lock_path):
            data = self._load_data_unlocked()
            if req_id not in data["requests"]:
                raise KeyError(f"Request '{req_id}' not found in ledger.")

            req = data["requests"][req_id]
            prev_state = req["state"]
            old_head = req.get("head")
            now = get_iso_timestamp()
            effective_actor = actor or "unspecified"

            # Ensure schema backward-compatibility
            if "github" not in req:
                req["github"] = {
                    "repo": DEFAULT_REPO,
                    "issue_number": None,
                    "issue_url": None,
                    "proof_url": None,
                    "proof_verified": False,
                }
            if "superboard" not in req:
                req["superboard"] = {
                    "project_number": DEFAULT_SUPERBOARD_PROJECT_NUM,
                    "project_id": DEFAULT_SUPERBOARD_PROJECT_ID,
                    "item_id": None,
                    "status": "Backlog",
                    "labels": [],
                }
            if "decisions" not in req:
                req["decisions"] = []
            if "decision_blockers" not in req:
                req["decision_blockers"] = []
            if "task_type" not in req:
                req["task_type"] = "deployable"
            if "deployment_applicable" not in req:
                req["deployment_applicable"] = req["task_type"] not in ["local_doc", "local", "doc", "harness"]

            if task_type is not None:
                if task_type not in TASK_TYPES:
                    raise ValueError(f"Invalid task_type: '{task_type}'. Valid types: {TASK_TYPES}")
                req["task_type"] = task_type
                req["deployment_applicable"] = task_type not in ["local_doc", "local", "doc", "harness"]

            # Update dependencies if specified
            if dependencies is not None:
                for dep in dependencies:
                    if dep not in data["requests"]:
                        raise ValueError(f"Dependency '{dep}' does not exist in ledger.")
                self._detect_dependency_cycles(data["requests"], req_id, dependencies)
                req["dependencies"] = dependencies

            # 1. Update Head & Invalidate Head-Bound Evidence, Criteria, Proofs, and State
            if head is not None and head != old_head:
                self._validate_head_sha(head)
                req["head"] = head
                invalidated_ev_count = 0
                for ev in req.get("evidence", []):
                    if ev.get("head") and ev.get("head") != head:
                        ev["stale"] = True
                        invalidated_ev_count += 1

                # Invalidate ALL acceptance criteria: reset verified to pending and mark evidence stale
                crit_reset_count = 0
                for c in req.get("acceptance_criteria", []):
                    if c.get("status") == "verified":
                        c["status"] = "pending"
                        c["verified_head"] = None
                        c["head"] = None
                        c["verified_at"] = None
                        c["verified_by"] = None
                        old_evidence = c.get("evidence", "")
                        if not old_evidence.startswith("[STALE"):
                            c["evidence"] = f"[STALE - requires re-verification on head {head}] {old_evidence}".strip()
                        crit_reset_count += 1

                # Invalidate GitHub proof on head change!
                proof_invalidated = False
                gh = req.get("github", {})
                if gh.get("proof_verified"):
                    gh["proof_verified"] = False
                    gh["proof_invalidated_at"] = now
                    gh["proof_invalidation_reason"] = f"Invalidated on head change from '{old_head}' to '{head}'"
                    proof_invalidated = True

                # If in active verification stages: head change invalidates those stages and forces back to implementation
                if prev_state in ["QA", "review", "awaiting authorization", "integration", "live verification"]:
                    req["state"] = "implementation"
                    inv_reason = (
                        f"Head changed from '{old_head}' to '{head}'. "
                        f"Invalidated {invalidated_ev_count} evidence record(s); "
                        f"reset {crit_reset_count} verified criteria to pending; "
                        f"{'invalidated GitHub proof; ' if proof_invalidated else ''}"
                        f"reset state from '{prev_state}' to 'implementation'."
                    )
                    req["history"].append({
                        "timestamp": now,
                        "from_state": prev_state,
                        "to_state": "implementation",
                        "actor": effective_actor,
                        "reason": inv_reason,
                    })
                    req["next_action"] = f"Re-verify implementation on new head '{head}'."
                    prev_state = "implementation"

            # 2. Update owner
            if owner is not None:
                req["owner"] = owner

            # 3. Update Blocker
            if clear_blocker:
                req["blocker"] = None
            elif blocker is not None:
                req["blocker"] = blocker

            # 4. Update Criterion
            if criterion_update:
                c_id = str(criterion_update.get("id"))
                found = False
                for c in req["acceptance_criteria"]:
                    if c["id"] == c_id:
                        found = True
                        if "status" in criterion_update:
                            c["status"] = criterion_update["status"]
                        if "evidence" in criterion_update:
                            c["evidence"] = criterion_update["evidence"]
                        if "description" in criterion_update:
                            c["description"] = criterion_update["description"]
                        if criterion_update.get("status") == "verified":
                            c["verified_at"] = now
                            c["verified_by"] = effective_actor
                            # Bind criterion to current HEAD
                            c["head"] = req.get("head")
                            c["verified_head"] = req.get("head")
                        elif criterion_update.get("status") == "pending":
                            c["verified_at"] = None
                            c["verified_by"] = None
                            c["head"] = None
                            c["verified_head"] = None
                        break
                if not found:
                    raise KeyError(f"Criterion '{c_id}' not found on request '{req_id}'.")

            # 5. Add Evidence
            if add_evidence:
                ev_id = add_evidence.get("id") or f"ev-{len(req['evidence'])+1}"
                ev_entry = {
                    "id": ev_id,
                    "criterion_id": add_evidence.get("criterion_id"),
                    "head": add_evidence.get("head", req.get("head")),
                    "type": add_evidence.get("type", "automated"),
                    "summary": add_evidence.get("summary", ""),
                    "details": add_evidence.get("details", ""),
                    "recorded_by": add_evidence.get("recorded_by", effective_actor),
                    "recorded_at": now,
                    "stale": False,
                }
                req["evidence"].append(ev_entry)

            # 6. Update Authorization (Strict provenance: ledger records authorization provenance only, cannot mint it)
            if authorization_update:
                auth = req["authorization"]
                auth_status = authorization_update.get("status")
                auth_by = authorization_update.get("authorized_by")
                auth_prov = authorization_update.get("provenance")
                auth_notes = authorization_update.get("notes", "")

                if auth_status == "authorized":
                    if not auth_by or not str(auth_by).strip():
                        raise ValueError(
                            "Cannot authorize: Explicit 'authorized_by' is required. "
                            "Ledger records authorization provenance only, cannot mint it."
                        )
                    # Self-authorization check: owner cannot self-authorize
                    owner = req.get("owner")
                    if owner and str(auth_by).strip().lower() == str(owner).strip().lower():
                        raise ValueError(
                            f"Self-authorization rejected: Owner '{owner}' cannot self-authorize request '{req_id}'."
                        )
                    # Unprovenanced operator check
                    if str(auth_by).strip().lower() == "operator" and not auth_prov and not auth_notes:
                        raise ValueError(
                            "Cannot mint operator authorization without provenance. "
                            "Provide 'provenance' or 'notes' detailing verified human operator authority."
                        )
                    auth["status"] = "authorized"
                    auth["authorized_by"] = auth_by
                    auth["authorized_at"] = now
                    auth["provenance"] = auth_prov or auth_notes or "explicit_authorization"
                    auth["notes"] = auth_notes
                elif auth_status == "denied":
                    auth["status"] = "denied"
                    auth["authorized_by"] = auth_by or effective_actor
                    auth["authorized_at"] = now
                    auth["notes"] = auth_notes
                else:
                    for k, v in authorization_update.items():
                        auth[k] = v

            # 7. Update GitHub metadata & validate proof URL format and matching repository
            if github_update:
                gh = req["github"]
                for k, v in github_update.items():
                    gh[k] = v

                expected_repo = gh.get("repo") or DEFAULT_REPO
                if gh.get("proof_url"):
                    if not validate_github_url(gh["proof_url"], expected_repo=expected_repo):
                        raise ValueError(
                            f"Invalid GitHub proof URL: '{gh['proof_url']}'. "
                            f"Must be a well-formed GitHub URL matching repository '{expected_repo}'."
                        )
                    # Remote verification check
                    if github_update.get("remote_verify") or github_update.get("verify_remote"):
                        remote_ok = verify_remote_proof(gh["proof_url"])
                        if not remote_ok:
                            gh["proof_verified"] = False
                            gh["proof_remote_status"] = "unverified"
                            raise ValueError(
                                f"Remote verification failed: Proof URL '{gh['proof_url']}' could not be fetched or verified remotely."
                            )
                        gh["proof_verified"] = True
                        gh["proof_remote_status"] = "verified_remote"

                if gh.get("issue_number") and not gh.get("issue_url"):
                    gh["issue_url"] = f"https://github.com/{expected_repo}/issues/{gh['issue_number']}"

            # 8. Update Superboard metadata
            if superboard_update:
                sb = req["superboard"]
                for k, v in superboard_update.items():
                    if k == "labels" and isinstance(v, list):
                        sb["labels"] = sorted(list(set(sb.get("labels", []) + v)))
                    else:
                        sb[k] = v

            # 9. Manage Decisions & Decision Blockers (Strict responder check & option validation)
            if add_decision:
                d_id = add_decision.get("id") or f"DEC-{len(req['decisions'])+1}"
                dec_entry = {
                    "id": d_id,
                    "issue_number": add_decision.get("issue_number") or req["github"].get("issue_number"),
                    "question": add_decision.get("question", ""),
                    "options": add_decision.get("options", []),
                    "status": "pending",
                    "authorized_responder": add_decision.get("authorized_responder", "user"),
                    "answer": None,
                    "comment_id": None,
                    "resolved_at": None,
                    "blocks_action": add_decision.get("blocks_action", "implementation"),
                }
                req["decisions"].append(dec_entry)
                if add_decision.get("blocks", True):
                    if d_id not in req["decision_blockers"]:
                        req["decision_blockers"].append(d_id)

            if resolve_decision:
                d_id = resolve_decision.get("id")
                found = False
                for d in req["decisions"]:
                    if d["id"] == d_id:
                        found = True
                        auth_resp = d.get("authorized_responder")
                        if auth_resp:
                            cleaned_actor = (effective_actor or "").lower().strip().lstrip("@")
                            cleaned_auth = auth_resp.lower().strip().lstrip("@")
                            if cleaned_actor != cleaned_auth:
                                raise ValueError(
                                    f"Cannot resolve decision '{d_id}': Actor '{effective_actor}' is not authorized. "
                                    f"Authorized responder is '{auth_resp}'."
                                )

                        # Validate answer against options
                        opts = d.get("options")
                        ans_raw = str(resolve_decision.get("answer") or "").strip()
                        if opts and isinstance(opts, list) and len(opts) > 0:
                            matched_opt = None
                            for opt in opts:
                                if match_decision_option(ans_raw, str(opt)):
                                    matched_opt = opt
                                    break
                            if not matched_opt:
                                raise ValueError(
                                    f"Cannot resolve decision '{d_id}': Answer '{resolve_decision.get('answer')}' is not among valid options: {opts}"
                                )
                            ans_raw = matched_opt

                        prov_type = resolve_decision.get("provenance_type", "human_operator")
                        d["status"] = "resolved" if prov_type != "synthetic_test" else "synthetic_test_recorded"
                        d["answer"] = ans_raw
                        d["comment_id"] = resolve_decision.get("comment_id")
                        d["provenance_type"] = prov_type
                        d["resolved_at"] = now
                        d["resolved_by"] = effective_actor

                        # Synthetic test probe check: do NOT unblock decision blocker on real requests!
                        if prov_type == "synthetic_test":
                            d["notes"] = "Synthetic test probe recorded; does not unblock real task"
                        else:
                            if d_id in req["decision_blockers"]:
                                req["decision_blockers"].remove(d_id)
                        break
                if not found:
                    raise KeyError(f"Decision '{d_id}' not found on request '{req_id}'.")

            if add_decision_blocker:
                if add_decision_blocker not in req["decision_blockers"]:
                    req["decision_blockers"].append(add_decision_blocker)

            # Strict blocker clear: remove unresolved blocker wipe bypass!
            if clear_decision_blocker:
                if clear_decision_blocker == "all":
                    pending_blockers = []
                    for d in req.get("decisions", []):
                        if d["id"] in req.get("decision_blockers", []) and d.get("status") != "resolved":
                            pending_blockers.append(d["id"])
                    if pending_blockers:
                        raise ValueError(
                            f"Cannot clear all decision blockers: Decision(s) {pending_blockers} are unresolved (status is pending). "
                            f"Resolve each decision via resolve_decision before clearing blockers."
                        )
                    req["decision_blockers"] = []
                else:
                    target_dec = next((d for d in req.get("decisions", []) if d["id"] == clear_decision_blocker), None)
                    if target_dec and target_dec.get("status") != "resolved":
                        raise ValueError(
                            f"Cannot clear decision blocker '{clear_decision_blocker}': Decision is unresolved (status: '{target_dec.get('status')}'). "
                            f"Resolve the decision first via resolve_decision."
                        )
                    if clear_decision_blocker in req["decision_blockers"]:
                        req["decision_blockers"].remove(clear_decision_blocker)

            # 10. Update Next Action
            if next_action is not None:
                req["next_action"] = next_action

            def require_current_head_stage_evidence(stage_name: str) -> None:
                current_head = str(req.get("head") or "").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{40}", current_head):
                    raise ValueError(
                        f"Cannot advance from '{stage_name}': a full current 40-character head SHA is required."
                    )

                stage_type = f"{stage_name.lower()}_verification"
                stage_evidence = [
                    ev for ev in req.get("evidence", [])
                    if not ev.get("stale")
                    and str(ev.get("head") or "").strip().lower() == current_head
                    and ev.get("type") == stage_type
                    and str(ev.get("summary") or "").strip()
                    and str(ev.get("details") or "").strip()
                ]
                if not stage_evidence:
                    raise ValueError(
                        f"Cannot advance from '{stage_name}': genuine current-head evidence "
                        f"of type '{stage_type}' is required for {current_head}."
                    )

                criteria = req.get("acceptance_criteria", [])
                if not criteria:
                    raise ValueError(
                        f"Cannot advance from '{stage_name}': no acceptance criteria are defined."
                    )
                for criterion in criteria:
                    if (
                        criterion.get("status") != "verified"
                        or not str(criterion.get("evidence") or "").strip()
                        or str(criterion.get("evidence") or "").startswith("[STALE")
                        or str(criterion.get("verified_head") or "").strip().lower() != current_head
                    ):
                        raise ValueError(
                            f"Cannot advance from '{stage_name}': acceptance criterion "
                            f"'{criterion.get('id')}' lacks fresh verified evidence on {current_head}."
                        )

            # 11. State Transition & Strict Invariants
            target_state = state or prev_state
            if target_state != prev_state:
                if target_state not in VALID_STATES:
                    raise ValueError(f"Invalid target state: '{target_state}'. Valid: {VALID_STATES}")

                # Determine allowed transitions based on task_type
                task_t = req.get("task_type", "deployable")
                is_local = task_t in ["local_doc", "local", "doc", "harness"] or not req.get("deployment_applicable", True)

                if is_local:
                    if target_state in DEPLOYMENT_STATES:
                        raise ValueError(
                            f"Deployment stage '{target_state}' is not applicable for {task_t} tasks. "
                            f"Local-doc / harness tasks do not use fake deployment stages (integration, live verification)."
                        )
                    allowed_next = ALLOWED_TRANSITIONS_LOCAL_DOC.get(prev_state, [])
                else:
                    allowed_next = ALLOWED_TRANSITIONS_DEPLOYABLE.get(prev_state, [])

                if target_state not in allowed_next:
                    raise ValueError(
                        f"Illegal state transition from '{prev_state}' to '{target_state}' for {task_t} task. "
                        f"Allowed transitions from '{prev_state}': {allowed_next}"
                    )

                if target_state == "review":
                    require_current_head_stage_evidence("QA")
                elif target_state == "awaiting authorization":
                    require_current_head_stage_evidence("review")

                # Check dependencies
                for dep_id in req.get("dependencies", []):
                    dep_req = data["requests"].get(dep_id)
                    if not dep_req or dep_req.get("state") != "done":
                        raise ValueError(
                            f"Cannot transition '{req_id}' to '{target_state}': "
                            f"Dependency '{dep_id}' is not 'done' (current: '{dep_req.get('state') if dep_req else 'missing'}')."
                        )

                # Strict Rule: No Auto-Merge. Transition to 'integration' requires explicit authorization.
                if target_state == "integration":
                    auth = req.get("authorization", {})
                    if auth.get("status") != "authorized":
                        raise ValueError(
                            f"Cannot transition to 'integration': Explicit authorization required (no auto-merge allowed). "
                            f"Current authorization status: '{auth.get('status')}'."
                        )
                    if not auth.get("authorized_by"):
                        raise ValueError(
                            "Cannot transition to 'integration': 'authorized_by' is not recorded in authorization."
                        )

                # Strict Rule: Cannot advance or complete while decision blockers exist
                if target_state in ["integration", "live verification", "done"] and req.get("decision_blockers"):
                    raise ValueError(
                        f"Cannot transition to '{target_state}': Unresolved decision blocker(s): {req['decision_blockers']}"
                    )

                # Strict Rule: Per-criterion fresh evidence + Validated GitHub proof required for 'done'
                if target_state == "done":
                    if req.get("blocker"):
                        raise ValueError(
                            f"Cannot transition to 'done': Active blocker exists: '{req.get('blocker')}'"
                        )
                    if req.get("decision_blockers"):
                        raise ValueError(
                            f"Cannot transition to 'done': Active decision blockers: {req.get('decision_blockers')}"
                        )
                    criteria = req.get("acceptance_criteria", [])
                    if not criteria:
                        raise ValueError("Cannot transition to 'done': No acceptance criteria defined.")

                    curr_head = req.get("head")
                    for c in criteria:
                        c_id = c.get("id")
                        c_status = c.get("status")
                        c_evidence = (c.get("evidence") or "").strip()
                        if c_status != "verified":
                            raise ValueError(
                                f"Cannot transition to 'done': Acceptance criterion '{c_id}' "
                                f"status is '{c_status}', expected 'verified'."
                            )
                        if not c_evidence or c_evidence.startswith("[STALE"):
                            raise ValueError(
                                f"Cannot transition to 'done': Acceptance criterion '{c_id}' "
                                f"lacks explicit fresh evidence (found empty or stale evidence)."
                            )
                        # Head freshness check: if request tracks a head, criterion must match that head
                        if curr_head and c.get("verified_head") and c.get("verified_head") != curr_head:
                            raise ValueError(
                                f"Cannot transition to 'done': Acceptance criterion '{c_id}' "
                                f"was verified on head '{c.get('verified_head')}', but current head is '{curr_head}'."
                            )

                    # Strict Invariant: Don't mark completion absent verified well-formed GitHub proof matching repo
                    gh = req.get("github", {})
                    proof_url = gh.get("proof_url")
                    expected_repo = gh.get("repo") or DEFAULT_REPO
                    if not gh.get("proof_verified") or not validate_github_url(proof_url, expected_repo=expected_repo):
                        raise ValueError(
                            f"Cannot transition to 'done': Well-formed verified GitHub proof URL matching repository '{expected_repo}' is required. "
                            f"GitHub issues + Superboard are shared source of truth; local ledger is recovery cache only. "
                            f"Set proof via --github-proof <url> and --verify-github-proof."
                        )

                req["state"] = target_state
                req["history"].append({
                    "timestamp": now,
                    "from_state": prev_state,
                    "to_state": target_state,
                    "actor": effective_actor,
                    "reason": reason,
                })
            elif reason:
                req["history"].append({
                    "timestamp": now,
                    "from_state": prev_state,
                    "to_state": prev_state,
                    "actor": effective_actor,
                    "reason": reason,
                })

            req["updated_at"] = now
            self._save_data_unlocked(data)
            return req

    def add_decision(
        self,
        req_id: str,
        question: str,
        options: Optional[List[str]] = None,
        decision_id: Optional[str] = None,
        blocks: bool = True,
        blocks_action: str = "implementation",
        authorized_responder: str = "user",
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add an asynchronous decision/question to a request."""
        dec_add = {
            "id": decision_id,
            "question": question,
            "options": options or [],
            "blocks_action": blocks_action,
            "authorized_responder": authorized_responder,
            "blocks": blocks,
        }
        return self.update_request(
            req_id=req_id,
            add_decision=dec_add,
            actor=actor,
            reason=f"Added decision {decision_id or ''}".strip(),
        )

    def resolve_decision(
        self,
        req_id: str,
        decision_id: str,
        answer: str,
        comment_id: Optional[Union[str, int]] = None,
        provenance_type: str = "human_operator",
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve an asynchronous decision/question with strict responder verification."""
        dec_res = {
            "id": decision_id,
            "answer": answer,
            "comment_id": comment_id,
            "provenance_type": provenance_type,
        }
        return self.update_request(
            req_id=req_id,
            resolve_decision=dec_res,
            actor=actor,
            reason=f"Resolved decision {decision_id}",
        )

    def add_decision_blocker(
        self, req_id: str, decision_id: str, actor: Optional[str] = None
    ) -> Dict[str, Any]:
        """Mark a decision as actively blocking the request."""
        return self.update_request(
            req_id=req_id,
            add_decision_blocker=decision_id,
            actor=actor,
            reason=f"Added decision blocker {decision_id}",
        )

    def clear_decision_blocker(
        self, req_id: str, decision_id: str = "all", actor: Optional[str] = None
    ) -> Dict[str, Any]:
        """Clear a specific decision blocker or all blockers (requires resolved status)."""
        return self.update_request(
            req_id=req_id,
            clear_decision_blocker=decision_id,
            actor=actor,
            reason=f"Cleared decision blocker {decision_id}",
        )

    def get_request(self, req_id: str) -> Dict[str, Any]:
        with FileLock(self.lock_path):
            data = self._load_data_unlocked()
            if req_id not in data["requests"]:
                raise KeyError(f"Request '{req_id}' not found.")
            return data["requests"][req_id]

    def list_requests(
        self, state: Optional[str] = None, owner: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        with FileLock(self.lock_path):
            data = self._load_data_unlocked()
            results = []
            for req in data["requests"].values():
                if state and req.get("state") != state:
                    continue
                if owner and req.get("owner") != owner:
                    continue
                results.append(req)
            return results

    def check_request(self, req_id: str) -> Dict[str, Any]:
        """Perform health, integrity, and source-of-truth invariant checks on a request."""
        with FileLock(self.lock_path):
            data = self._load_data_unlocked()
            if req_id not in data["requests"]:
                raise KeyError(f"Request '{req_id}' not found.")

            req = data["requests"][req_id]
            issues = []
            warnings = []

            # Check dependencies
            unresolved_deps = []
            for dep_id in req.get("dependencies", []):
                dep_req = data["requests"].get(dep_id)
                if not dep_req:
                    unresolved_deps.append(f"{dep_id} (missing)")
                elif dep_req.get("state") != "done":
                    unresolved_deps.append(f"{dep_id} ({dep_req.get('state')})")
            if unresolved_deps:
                issues.append(f"Unresolved dependencies: {', '.join(unresolved_deps)}")

            # Check general blockers
            if req.get("blocker"):
                issues.append(f"Active blocker: {req['blocker']}")

            # Check decision blockers
            dec_blockers = req.get("decision_blockers", [])
            if dec_blockers:
                issues.append(f"Active decision blocker(s) awaiting human response: {dec_blockers}")

            # Check criteria
            curr_head = req.get("head")
            unverified_crit = []
            no_evidence_crit = []
            stale_crit = []
            for c in req.get("acceptance_criteria", []):
                if c.get("status") != "verified":
                    unverified_crit.append(c["id"])
                c_ev = (c.get("evidence") or "").strip()
                if not c_ev:
                    no_evidence_crit.append(c["id"])
                elif c_ev.startswith("[STALE"):
                    stale_crit.append(c["id"])
                if curr_head and c.get("verified_head") and c.get("verified_head") != curr_head:
                    stale_crit.append(f"{c['id']} (verified on {c.get('verified_head')})")

            req_state_now = req.get("state")
            if req_state_now in VERIFICATION_COMPLETE_STATES:
                # These states all assert that QA and review already succeeded, so an
                # unverified or stale criterion is a contradiction, not a note. Reporting it
                # as a warning is how a request sat at the merge gate looking HEALTHY with
                # every criterion still pending.
                if unverified_crit:
                    issues.append(f"State is '{req_state_now}' but criteria unverified: {unverified_crit}")
                if no_evidence_crit:
                    issues.append(f"State is '{req_state_now}' but criteria missing evidence: {no_evidence_crit}")
                if stale_crit:
                    issues.append(f"State is '{req_state_now}' but criteria have stale evidence: {stale_crit}")
                if req_state_now == "done":
                    gh = req.get("github", {})
                    expected_repo = gh.get("repo") or DEFAULT_REPO
                    if not (gh.get("proof_verified") and validate_github_url(gh.get("proof_url"), expected_repo=expected_repo)):
                        issues.append(f"State is 'done' but missing verified well-formed GitHub proof URL for '{expected_repo}'.")
            else:
                if unverified_crit:
                    warnings.append(f"Pending criteria ({len(unverified_crit)}/{len(req.get('acceptance_criteria', []))}): {unverified_crit}")
                if stale_crit:
                    warnings.append(f"Stale criteria requiring re-verification: {stale_crit}")

            # Check stale evidence
            stale_ev = [ev["id"] for ev in req.get("evidence", []) if ev.get("stale")]
            if stale_ev:
                warnings.append(f"Contains {len(stale_ev)} stale evidence records: {stale_ev}")

            # Check task_type consistency
            task_t = req.get("task_type", "deployable")
            if task_t in ["local_doc", "local", "doc", "harness"] and req.get("state") in DEPLOYMENT_STATES:
                issues.append(f"Task type is '{task_t}' but state is in deployment stage '{req.get('state')}'.")

            # Check authorization for integration
            if req.get("state") == "awaiting authorization":
                warnings.append("Awaiting operator authorization before integration.")
            elif req.get("state") == "integration":
                auth = req.get("authorization", {})
                if auth.get("status") != "authorized":
                    issues.append("In integration state without authorized status.")

            status = "HEALTHY" if not issues else "BLOCKED"
            return {
                "id": req_id,
                "status": status,
                "state": req.get("state"),
                "task_type": task_t,
                "owner": req.get("owner"),
                "issues": issues,
                "warnings": warnings,
                "next_action": req.get("next_action"),
                "github": req.get("github", {}),
                "superboard": req.get("superboard", {}),
                "decision_blockers": dec_blockers,
            }

    def next_actions(self, req_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Identify next actionable requests and next steps."""
        with FileLock(self.lock_path):
            data = self._load_data_unlocked()
            candidates = []

            target_reqs = (
                [data["requests"][req_id]]
                if req_id and req_id in data["requests"]
                else list(data["requests"].values())
            )

            for req in target_reqs:
                r_id = req["id"]
                state = req["state"]
                if state == "done":
                    continue

                # Check deps
                blocked_by_deps = []
                for dep_id in req.get("dependencies", []):
                    dep = data["requests"].get(dep_id)
                    if not dep or dep.get("state") != "done":
                        blocked_by_deps.append(dep_id)

                dec_blockers = req.get("decision_blockers", [])
                is_blocked = bool(req.get("blocker") or blocked_by_deps or dec_blockers)

                blocker_parts = []
                if req.get("blocker"):
                    blocker_parts.append(req["blocker"])
                if blocked_by_deps:
                    blocker_parts.append(f"Dependencies incomplete: {', '.join(blocked_by_deps)}")
                if dec_blockers:
                    blocker_parts.append(f"Awaiting human decision on: {', '.join(dec_blockers)}")
                blocker_desc = "; ".join(blocker_parts) if blocker_parts else None

                # Determine recommended next step
                rec_step = req.get("next_action") or "Advance workflow"
                if dec_blockers:
                    rec_step = f"Await human decision for {', '.join(dec_blockers)} via GitHub issue."
                elif blocked_by_deps:
                    rec_step = f"Wait for dependencies ({', '.join(blocked_by_deps)}) to complete."
                elif req.get("blocker"):
                    rec_step = f"Resolve blocker: {req.get('blocker')}"
                elif state == "pending":
                    rec_step = "Transition to 'implementation'."
                elif state == "awaiting authorization":
                    rec_step = "Obtain operator authorization with verified provenance."
                elif state == "integration":
                    rec_step = "Perform manual integration (no auto-merge) and advance to 'live verification'."

                candidates.append({
                    "id": r_id,
                    "state": state,
                    "task_type": req.get("task_type", "deployable"),
                    "owner": req.get("owner"),
                    "runnable": not is_blocked,
                    "blocker": blocker_desc,
                    "decision_blockers": dec_blockers,
                    "next_action": rec_step,
                    "head": req.get("head"),
                    "github_issue": req.get("github", {}).get("issue_url"),
                    "superboard_status": req.get("superboard", {}).get("status"),
                    "acceptance_criteria_pending": [
                        c["id"] for c in req.get("acceptance_criteria", []) if c.get("status") != "verified"
                    ],
                })

            candidates.sort(key=lambda x: (not x["runnable"], x["id"]))
            return candidates

    def recover(self) -> Dict[str, Any]:
        """Perform restart recovery purely by reading the disk ledger."""
        with FileLock(self.lock_path):
            data = self._load_data_unlocked()
            active_requests = {}
            summary = {
                "total_requests": len(data["requests"]),
                "active_count": 0,
                "done_count": 0,
                "blocked_count": 0,
                "decision_blocked_count": 0,
            }

            for r_id, req in data["requests"].items():
                state = req["state"]
                if state == "done":
                    summary["done_count"] += 1
                    continue

                summary["active_count"] += 1
                unmet_deps = [
                    d for d in req.get("dependencies", [])
                    if d not in data["requests"] or data["requests"][d].get("state") != "done"
                ]
                dec_blockers = req.get("decision_blockers", [])
                if dec_blockers:
                    summary["decision_blocked_count"] += 1

                has_blocker = bool(req.get("blocker") or unmet_deps or dec_blockers)
                if has_blocker:
                    summary["blocked_count"] += 1

                active_requests[r_id] = {
                    "id": r_id,
                    "state": state,
                    "task_type": req.get("task_type", "deployable"),
                    "owner": req.get("owner"),
                    "head": req.get("head"),
                    "blocked": has_blocker,
                    "blocker": req.get("blocker"),
                    "unmet_dependencies": unmet_deps,
                    "decision_blockers": dec_blockers,
                    "next_action": req.get("next_action"),
                    "github_issue": req.get("github", {}).get("issue_url"),
                    "superboard_status": req.get("superboard", {}).get("status"),
                    "pending_criteria": [
                        c["id"] for c in req.get("acceptance_criteria", []) if c.get("status") != "verified"
                    ],
                    "stale_evidence_count": sum(1 for ev in req.get("evidence", []) if ev.get("stale")),
                }

            return {
                "timestamp": get_iso_timestamp(),
                "ledger_path": self.ledger_path,
                "role": data.get("role", "local_recovery_cache"),
                "authority": data.get("authority", "github_issues_and_superboard"),
                "summary": summary,
                "active_requests": active_requests,
            }


# ----------------------------------------------------------------------
# CLI Interface
# ----------------------------------------------------------------------

def parse_criteria_arg(arg: str) -> List[Any]:
    """
    Parse the `--criteria` argument into criteria entries.

    Parsing only: entries stay in whatever documented shape the caller wrote
    (description strings or mappings) and `normalize_acceptance_criteria` remains
    the single normalization authority. A JSON-looking argument that is not valid
    JSON is reported as that, rather than as a decoder error with no context.
    """
    arg = arg.strip()
    if arg.startswith("[") or arg.startswith("{"):
        try:
            parsed = json.loads(arg)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"--criteria starts with '{arg[0]}' so it was read as JSON, but it is not valid "
                f"JSON: {e}. Pass a JSON array of description strings or criterion objects, or "
                "comma-separated descriptions."
            )
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
        raise ValueError(
            f"--criteria parsed as JSON {type(parsed).__name__}; expected an array of criteria "
            "or a single criterion object."
        )
    lines = [line.strip() for line in arg.replace("\n", ",").split(",") if line.strip()]
    return [{"id": f"AC-{i+1}", "description": line} for i, line in enumerate(lines)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Machine-local durable request ledger utility (recovery cache for GitHub + Superboard)"
    )
    parser.add_argument(
        "--ledger", default=None, help=f"Path to ledger JSON (default: {DEFAULT_LEDGER_PATH})"
    )
    parser.add_argument(
        "--state-dir", default=None, help="Directory containing ledger.json (alternative to --ledger)"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # ADD
    p_add = subparsers.add_parser("add", help="Add a new request to the ledger")
    p_add.add_argument("--id", required=True, help="Request ID (e.g. req-001)")
    p_add.add_argument("--prompt", required=True, help="Prompt or task goal")
    p_add.add_argument("--session", required=True, help="Session UUID")
    p_add.add_argument("--project", required=True, help="Project name or path")
    p_add.add_argument(
        "--criteria",
        required=True,
        help="Acceptance criteria: JSON string or comma-separated descriptions",
    )
    p_add.add_argument("--owner", required=True, help="Owner lane/agent")
    p_add.add_argument("--dependencies", default="", help="Comma-separated dependency request IDs")
    p_add.add_argument("--head", default=None, help="Git HEAD SHA or commit reference")
    p_add.add_argument("--state", default="pending", choices=VALID_STATES, help="Initial state")
    p_add.add_argument("--task-type", default="deployable", choices=TASK_TYPES, help="Task type (deployable vs local_doc)")
    p_add.add_argument("--next-action", default=None, help="Initial next action")
    p_add.add_argument(
        "--no-auth-required", action="store_true", help="Flag if authorization is not required"
    )
    p_add.add_argument("--github-repo", default=DEFAULT_REPO, help="GitHub repository")
    p_add.add_argument("--issue-number", type=int, default=None, help="Linked GitHub issue number")
    p_add.add_argument("--issue-url", default=None, help="Linked GitHub issue URL")
    p_add.add_argument("--superboard-project", default=DEFAULT_SUPERBOARD_PROJECT_NUM, help="Superboard project number")
    p_add.add_argument("--superboard-card", default=None, help="Superboard item/card ID")
    p_add.add_argument("--superboard-status", default="Backlog", help="Superboard status column")
    p_add.add_argument("--labels", default="", help="Comma-separated labels")

    # UPDATE
    p_upd = subparsers.add_parser("update", help="Update a request in the ledger")
    p_upd.add_argument("id", help="Request ID")
    p_upd.add_argument("--state", choices=VALID_STATES, help="Target state")
    p_upd.add_argument("--owner", help="Assign new owner")
    p_upd.add_argument("--head", help="Set/update HEAD SHA (invalidates head-bound evidence, criteria & proofs)")
    p_upd.add_argument("--task-type", choices=TASK_TYPES, help="Update task type")
    p_upd.add_argument("--dependencies", default=None, help="Update comma-separated dependency IDs")
    p_upd.add_argument("--blocker", help="Set blocker text")
    p_upd.add_argument("--clear-blocker", action="store_true", help="Clear current blocker")
    p_upd.add_argument("--next-action", help="Update next action description")
    p_upd.add_argument("--actor", default=None, help="Actor recording the change (no default operator trust)")
    p_upd.add_argument("--reason", default="Update", help="Reason for change")

    # Criterion update flags
    p_upd.add_argument("--criterion-id", help="Criterion ID to update")
    p_upd.add_argument("--criterion-status", choices=["pending", "in_progress", "verified", "failed"])
    p_upd.add_argument("--criterion-evidence", help="Evidence string for criterion")

    # Add evidence flags
    p_upd.add_argument("--add-evidence", action="store_true", help="Add new evidence record")
    p_upd.add_argument("--evidence-type", default="automated", help="Type of evidence")
    p_upd.add_argument("--evidence-summary", default="", help="Summary of evidence")
    p_upd.add_argument("--evidence-details", default="", help="Full evidence details or artifact path")
    p_upd.add_argument("--evidence-crit", default=None, help="Criterion ID linked to evidence")

    # Authorization flags (explicit provenance required, no CLI operator trust)
    p_upd.add_argument("--authorize", action="store_true", help="Grant authorization")
    p_upd.add_argument("--deny-auth", action="store_true", help="Deny authorization")
    p_upd.add_argument("--authorized-by", default=None, help="Authorizer name (mandatory when --authorize is passed)")
    p_upd.add_argument("--auth-provenance", default=None, help="Authorization provenance (e.g. comment ID, PR URL)")
    p_upd.add_argument("--auth-notes", default="", help="Authorization notes")

    # GitHub & Superboard update flags
    p_upd.add_argument("--github-proof", default=None, help="GitHub proof URL (PR, comment, commit, or asset)")
    p_upd.add_argument("--verify-github-proof", action="store_true", help="Mark GitHub proof verified")
    p_upd.add_argument("--remote-verify", action="store_true", help="Actually fetch and verify proof URL remotely")
    p_upd.add_argument("--issue-number", type=int, default=None, help="Update linked GitHub issue number")
    p_upd.add_argument("--issue-url", default=None, help="Update linked GitHub issue URL")
    p_upd.add_argument("--superboard-card", default=None, help="Update Superboard item/card ID")
    p_upd.add_argument("--superboard-status", default=None, help="Update Superboard status column")
    p_upd.add_argument("--labels", default=None, help="Comma-separated labels to append")

    # Decision flags
    p_upd.add_argument("--add-decision-question", default=None, help="Ask async question / decision")
    p_upd.add_argument("--decision-id", default=None, help="Explicit decision ID")
    p_upd.add_argument("--decision-options", default="", help="Comma-separated decision options")
    p_upd.add_argument("--decision-blocks-action", default="implementation", help="Action blocked by decision")
    p_upd.add_argument("--decision-authorized-responder", default="user", help="Authorized responder")
    p_upd.add_argument("--resolve-decision", default=None, help="Resolve decision ID")
    p_upd.add_argument("--decision-answer", default=None, help="Answer for resolved decision")
    p_upd.add_argument("--decision-comment-id", default=None, help="GitHub comment ID of answer")
    p_upd.add_argument("--decision-provenance-type", default="human_operator", help="Provenance type (human_operator vs synthetic_test)")
    p_upd.add_argument("--add-decision-blocker", default=None, help="Add decision ID to blockers")
    p_upd.add_argument("--clear-decision-blocker", default=None, help="Clear decision blocker ID or 'all'")

    # CHECK
    p_chk = subparsers.add_parser("check", help="Verify request health and invariants")
    p_chk.add_argument("id", nargs="?", default=None, help="Request ID (omit to check all)")
    p_chk.add_argument("--strict", action="store_true", help="Exit non-zero if issues found")
    p_chk.add_argument("--json", action="store_true", help="Output JSON")

    # NEXT
    p_nxt = subparsers.add_parser("next", help="Get next actionable tasks")
    p_nxt.add_argument("id", nargs="?", default=None, help="Optional request ID filter")
    p_nxt.add_argument("--json", action="store_true", help="Output JSON")

    # LIST
    p_lst = subparsers.add_parser("list", help="List requests")
    p_lst.add_argument("--state", choices=VALID_STATES, help="Filter by state")
    p_lst.add_argument("--owner", help="Filter by owner")
    p_lst.add_argument("--json", action="store_true", help="Output JSON")

    # SHOW
    p_shw = subparsers.add_parser("show", help="Show full details of a request")
    p_shw.add_argument("id", help="Request ID")
    p_shw.add_argument("--json", action="store_true", help="Output JSON")

    # RECOVER
    p_rec = subparsers.add_parser("recover", help="Restart recovery reading disk ledger")
    p_rec.add_argument("--json", action="store_true", help="Output JSON")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    ledger = RequestLedger(ledger_path=args.ledger, state_dir=args.state_dir)

    try:
        if args.command == "add":
            deps = [d.strip() for d in args.dependencies.split(",") if d.strip()]
            criteria = parse_criteria_arg(args.criteria)
            lbls = [l.strip() for l in args.labels.split(",") if l.strip()]
            req = ledger.add_request(
                req_id=args.id,
                prompt=args.prompt,
                session=args.session,
                project=args.project,
                acceptance_criteria=criteria,
                owner=args.owner,
                dependencies=deps,
                head=args.head,
                state=args.state,
                task_type=args.task_type,
                next_action=args.next_action,
                authorization_required=not args.no_auth_required,
                github_repo=args.github_repo,
                issue_number=args.issue_number,
                issue_url=args.issue_url,
                superboard_project=args.superboard_project,
                superboard_card=args.superboard_card,
                superboard_status=args.superboard_status,
                labels=lbls,
            )
            print(f"[OK] Added request '{req['id']}' in state '{req['state']}' (type='{req['task_type']}')")

        elif args.command == "update":
            crit_upd = None
            if args.criterion_id:
                crit_upd = {"id": args.criterion_id}
                if args.criterion_status:
                    crit_upd["status"] = args.criterion_status
                if args.criterion_evidence:
                    crit_upd["evidence"] = args.criterion_evidence

            ev_upd = None
            if args.add_evidence:
                ev_upd = {
                    "type": args.evidence_type,
                    "summary": args.evidence_summary,
                    "details": args.evidence_details,
                    "criterion_id": args.evidence_crit,
                }

            auth_upd = None
            if args.authorize:
                if not args.authorized_by:
                    raise ValueError(
                        "Cannot authorize via CLI: Explicit --authorized-by is required. "
                        "Ledger records authorization provenance only, cannot mint it."
                    )
                auth_upd = {
                    "status": "authorized",
                    "authorized_by": args.authorized_by,
                    "provenance": args.auth_provenance or args.auth_notes,
                    "notes": args.auth_notes,
                }
            elif args.deny_auth:
                auth_upd = {
                    "status": "denied",
                    "authorized_by": args.authorized_by or args.actor or "unspecified",
                    "notes": args.auth_notes,
                }

            gh_upd = None
            if args.github_proof or args.verify_github_proof or args.issue_number or args.issue_url or args.remote_verify:
                gh_upd = {}
                if args.github_proof:
                    gh_upd["proof_url"] = args.github_proof
                if args.verify_github_proof:
                    gh_upd["proof_verified"] = True
                if args.remote_verify:
                    gh_upd["remote_verify"] = True
                if args.issue_number:
                    gh_upd["issue_number"] = args.issue_number
                if args.issue_url:
                    gh_upd["issue_url"] = args.issue_url

            sb_upd = None
            if args.superboard_card or args.superboard_status or args.labels:
                sb_upd = {}
                if args.superboard_card:
                    sb_upd["item_id"] = args.superboard_card
                if args.superboard_status:
                    sb_upd["status"] = args.superboard_status
                if args.labels:
                    sb_upd["labels"] = [l.strip() for l in args.labels.split(",") if l.strip()]

            dec_add = None
            if args.add_decision_question:
                opts = [o.strip() for o in args.decision_options.split(",") if o.strip()]
                dec_add = {
                    "id": args.decision_id,
                    "question": args.add_decision_question,
                    "options": opts,
                    "blocks_action": args.decision_blocks_action,
                    "authorized_responder": args.decision_authorized_responder,
                }

            dec_res = None
            if args.resolve_decision:
                dec_res = {
                    "id": args.resolve_decision,
                    "answer": args.decision_answer,
                    "comment_id": args.decision_comment_id,
                    "provenance_type": args.decision_provenance_type,
                }

            deps_upd = None
            if args.dependencies is not None:
                deps_upd = [d.strip() for d in args.dependencies.split(",") if d.strip()]

            req = ledger.update_request(
                req_id=args.id,
                state=args.state,
                owner=args.owner,
                head=args.head,
                task_type=args.task_type,
                dependencies=deps_upd,
                blocker=args.blocker,
                clear_blocker=args.clear_blocker,
                next_action=args.next_action,
                criterion_update=crit_upd,
                add_evidence=ev_upd,
                authorization_update=auth_upd,
                github_update=gh_upd,
                superboard_update=sb_upd,
                add_decision=dec_add,
                resolve_decision=dec_res,
                add_decision_blocker=args.add_decision_blocker,
                clear_decision_blocker=args.clear_decision_blocker,
                actor=args.actor,
                reason=args.reason,
            )
            print(f"[OK] Updated request '{req['id']}': state='{req['state']}', owner='{req['owner']}'")

        elif args.command == "check":
            req_ids = [args.id] if args.id else [r["id"] for r in ledger.list_requests()]
            results = [ledger.check_request(rid) for rid in req_ids]

            if args.json:
                print(json.dumps(results, indent=2))
            else:
                has_blocked = False
                for res in results:
                    gh_iss = res.get("github", {}).get("issue_url") or "No issue linked"
                    sb_stat = res.get("superboard", {}).get("status") or "-"
                    print(f"Request: {res['id']} | Status: {res['status']} | State: {res['state']} | Type: {res.get('task_type')} | Owner: {res['owner']}")
                    print(f"  GitHub: {gh_iss} | Superboard Status: {sb_stat}")
                    if res["decision_blockers"]:
                        print(f"  Decision Blockers: {res['decision_blockers']}")
                    if res["issues"]:
                        has_blocked = True
                        for iss in res["issues"]:
                            print(f"  [ERROR] {iss}")
                    if res["warnings"]:
                        for wrn in res["warnings"]:
                            print(f"  [WARN]  {wrn}")
                    print(f"  Next Action: {res['next_action']}")
                    print("-" * 60)

                if args.strict and has_blocked:
                    sys.exit(1)

        elif args.command == "next":
            actions = ledger.next_actions(args.id)
            if args.json:
                print(json.dumps(actions, indent=2))
            else:
                if not actions:
                    print("No actionable requests found.")
                for act in actions:
                    flag = "[RUNNABLE]" if act["runnable"] else "[BLOCKED]"
                    if act.get("decision_blockers"):
                        flag = "[BLOCKED BY DECISION]"
                    print(f"{flag} {act['id']} ({act['state']}, type={act.get('task_type')}) - Owner: {act['owner']}")
                    if act["blocker"]:
                        print(f"   Reason: {act['blocker']}")
                    print(f"   Action: {act['next_action']}")
                    if act.get("github_issue"):
                        print(f"   GitHub Issue: {act['github_issue']}")
                    if act["acceptance_criteria_pending"]:
                        print(f"   Pending Criteria: {', '.join(act['acceptance_criteria_pending'])}")
                    print("-" * 60)

        elif args.command == "list":
            reqs = ledger.list_requests(state=args.state, owner=args.owner)
            if args.json:
                print(json.dumps(reqs, indent=2))
            else:
                print(f"{'ID':<30} {'STATE':<18} {'TYPE':<12} {'OWNER':<18} {'HEAD':<10} {'ISSUE':<8} {'BLOCKER'}")
                print("=" * 115)
                for r in reqs:
                    blk = r.get("blocker") or (f"Dec: {r['decision_blockers']}" if r.get("decision_blockers") else "-")
                    head = r.get("head") or "-"
                    iss = str(r.get("github", {}).get("issue_number") or "-")
                    t_type = r.get("task_type", "deployable")
                    print(f"{r['id']:<30} {r['state']:<18} {t_type:<12} {r['owner']:<18} {head:<10} {iss:<8} {blk}")

        elif args.command == "show":
            req = ledger.get_request(args.id)
            if args.json:
                print(json.dumps(req, indent=2))
            else:
                print(f"Request ID:    {req['id']}")
                print(f"Task Type:     {req.get('task_type', 'deployable')}")
                print(f"Prompt:        {req['prompt']}")
                print(f"Session:       {req['session']}")
                print(f"Project:       {req['project']}")
                print(f"State:         {req['state']}")
                print(f"Owner:         {req['owner']}")
                print(f"Head:          {req['head']}")
                print(f"Blocker:       {req['blocker']}")
                print(f"Next Action:   {req['next_action']}")
                print(f"Authorization: {req['authorization']}")
                print(f"Dependencies:  {req['dependencies']}")
                print(f"GitHub:        {json.dumps(req.get('github', {}), indent=2)}")
                print(f"Superboard:    {json.dumps(req.get('superboard', {}), indent=2)}")
                if req.get("decisions"):
                    print(f"Decisions:     {json.dumps(req['decisions'], indent=2)}")
                if req.get("decision_blockers"):
                    print(f"Decision Blk:  {req['decision_blockers']}")
                print("\nAcceptance Criteria:")
                for c in req.get("acceptance_criteria", []):
                    head_info = f" (head: {c.get('verified_head')})" if c.get("verified_head") else ""
                    print(f"  - [{c['status']}] {c['id']}{head_info}: {c['description']}")
                    if c.get("evidence"):
                        print(f"      Evidence: {c['evidence']}")
                print(f"\nEvidence Records ({len(req.get('evidence', []))}):")
                for ev in req.get("evidence", []):
                    stale_mark = " [STALE]" if ev.get("stale") else ""
                    print(f"  - {ev['id']} ({ev['type']}){stale_mark}: {ev['summary']}")
                    if ev.get("details"):
                        print(f"      {ev['details']}")

        elif args.command == "recover":
            rec = ledger.recover()
            if args.json:
                print(json.dumps(rec, indent=2))
            else:
                sumry = rec["summary"]
                print(f"Ledger Path: {rec['ledger_path']}")
                print(f"Role: {rec['role']} | Shared Authority: {rec['authority']}")
                print(f"Recovery at: {rec['timestamp']}")
                print(f"Total: {sumry['total_requests']} | Active: {sumry['active_count']} | Done: {sumry['done_count']} | Blocked: {sumry['blocked_count']} | Decision Blocked: {sumry['decision_blocked_count']}\n")
                print("Active Recovery Work Queue:")
                for rid, info in rec["active_requests"].items():
                    status = "[BLOCKED]" if info["blocked"] else "[READY]"
                    if info["decision_blockers"]:
                        status = "[BLOCKED BY DECISION]"
                    print(f"  {status} {rid} ({info['state']}, type={info.get('task_type')}) | Owner: {info['owner']}")
                    if info.get("github_issue"):
                        print(f"      GitHub Issue: {info['github_issue']}")
                    if info.get("superboard_status"):
                        print(f"      Superboard Status: {info['superboard_status']}")
                    if info["blocker"]:
                        print(f"      Blocker: {info['blocker']}")
                    if info["decision_blockers"]:
                        print(f"      Decision Blockers: {info['decision_blockers']}")
                    if info["unmet_dependencies"]:
                        print(f"      Unmet Dependencies: {info['unmet_dependencies']}")
                    print(f"      Next Action: {info['next_action']}")
                    if info["pending_criteria"]:
                        print(f"      Pending Criteria: {info['pending_criteria']}")
                    if info["stale_evidence_count"] > 0:
                        print(f"      Stale Evidence Count: {info['stale_evidence_count']}")
                    print("  " + "-" * 50)

    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
