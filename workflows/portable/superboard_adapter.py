#!/usr/bin/env python3
"""
workflows/portable/superboard_adapter.py — Superboard Execution Adapter

Bridges the portable workflow core (coordinator, ledger, preflight, model routing,
github_pr_gate, telegram_notifier) with existing Superboard execution tooling
(scripts/super-qa-dispatch.sh, scripts/super-board-run.sh, super_board_runtime).

Core Architectural Invariants:
1. Prompt Instructions vs. Executable Tooling:
   The Superboard skill files (skills/super-build/SKILL.md, skills/super-qa/SKILL.md,
   skills/super-review/SKILL.md) are interactive prompt instructions designed for human/Claude
   orchestration sessions. They are not autonomous daemon binaries. The actual executable
   tooling provided by the repository consists of:
     - scripts/super-qa-dispatch.sh (exact-SHA detached worktree QA runner)
     - scripts/super-qa-file-bug.sh (structured bug filing)
     - scripts/super-board-wave-plan.sh (read-only wave planner via super_board_runtime.eligibility)
     - scripts/super-board-run.sh (legacy headless runner)
     - scripts/super_board_runtime/qa.py (exact-SHA merge handoff & status publisher)
     - scripts/super_board_runtime/config.py (config validator)
     - workflows/portable/github_pr_gate.py (deterministic CI status & review gate)
   This adapter orchestrates programmatic execution against these verified tools.

2. Minimal Adapter Call Points (Sequential Lifecycle Pipeline):
   Point 1: Request Intake & Eligibility (via RequestLedger and/or super_board_runtime.eligibility)
   Point 2: Connected-Service Preflight Gate (via PreflightEngine & ProjectConfig)
   Point 3: Explicit Capable Model & Role Dispatch Packet (via ResetAwareModelSelector)
   Point 4: Existing Worker Command Dispatch (via super-qa-dispatch.sh, safe worker task, or fake executor)
   Point 5: Evidence, QA & Review Gate Verification (exact-SHA binding, CI checks, independent review approval)
   Point 6: Concise Telegram Status Event (single-sentence canonical link via TelegramNotificationAdapter)

3. Separation of Merge & Deploy Authorization:
   The adapter automates build, QA, and review verification, transitioning requests through:
     pending -> implementation -> QA -> review -> awaiting authorization
   It strictly stops at 'awaiting authorization'. Auto-merge and auto-deploy are prohibited.
   Merge commits (--no-ff) and deployment staging promotion require explicit human operator action.

4. No Duplicate Scheduler:
   Single-step bounded execution. Operates sequentially without background daemon loops,
   thread pools, or competing queue managers.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# Ensure sibling portable workflow modules are importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    from coordinator import Coordinator, CoordinatorBoundaries, CoordinatorPacket, RequestSummary
    from ledger import RequestLedger, get_iso_timestamp
    from preflight import PreflightEngine, PreflightResult, check_preflight
    from model_routing import (
        HarnessDispatchPacket,
        ResetAwareModelSelector,
        RiskLevel,
        TaskType,
        VERIFIED_CONTEXT_WINDOWS,
    )
    from project_adapter import ProjectConfig, get_current_project_config, set_current_project_config
    from github_pr_gate import PRGateEvaluation, evaluate_pr_gate
    from telegram_notifier import NotificationEvent, TelegramNotificationAdapter
except ImportError as e:
    raise ImportError(f"superboard_adapter failed to import sibling portable modules: {e}")


@dataclass
class WorkerExecutionResult:
    """Result of worker command dispatch (real backend, safe probe, or fake fixture)."""
    stage: str                          # "build", "qa", "review", "probe"
    exit_code: int
    output: str
    head_sha: Optional[str] = None
    pr_url: Optional[str] = None
    command: List[str] = field(default_factory=list)
    is_fixture: bool = False
    fixture_label: Optional[str] = None
    # Provenance of the result. A probe proves the dispatch plumbing works; it proves
    # nothing about the request's acceptance criteria, so it may never advance state.
    is_probe: bool = False
    blocked_reason: Optional[str] = None
    backend_name: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_verifiable_evidence(self) -> bool:
        """
        True only for a successful real-backend execution carrying structured evidence.

        Fixtures and probes are excluded by construction: no amount of fixture output
        may be read as proof that real work happened.
        """
        return (
            not self.is_fixture
            and not self.is_probe
            and self.blocked_reason is None
            and self.exit_code == 0
            and bool(self.evidence)
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["is_verifiable_evidence"] = self.is_verifiable_evidence
        return d


@dataclass
class AdapterExecutionResult:
    """End-to-end result of an adapter step execution."""
    step_id: str
    request_id: Optional[str]
    stage: str
    status: str                         # "advanced", "blocked", "awaiting_authorization", "completed", "error", "done"
    status_reason: str
    next_action: str
    preflight_passed: bool
    dispatch_packet: Optional[Dict[str, Any]] = None
    worker_result: Optional[WorkerExecutionResult] = None
    gate_result: Optional[Dict[str, Any]] = None
    notification_receipt: Optional[Dict[str, Any]] = None
    boundaries: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.worker_result:
            d["worker_result"] = self.worker_result.to_dict()
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


class SuperboardExecutionAdapter:
    """
    Adapter integrating the portable workflow coordinator with the existing
    Superboard build/QA/review loop and execution tooling.
    """

    def __init__(
        self,
        coordinator: Optional[Coordinator] = None,
        state_dir: Optional[str] = None,
        config_path: Optional[str] = None,
        fake_executor: bool = False,
        fake_executor_fn: Optional[Callable[[Union[RequestSummary, Dict[str, Any]], str, HarnessDispatchPacket], WorkerExecutionResult]] = None,
        notify_telegram: bool = False,
        telegram_project: Optional[str] = None,
        telegram_dry_run: bool = True,
        telegram_send: bool = False,
        dry_run: bool = False,
        repo_root: Optional[str] = None,
        worker_backend: Optional[Any] = None,
    ):
        self.state_dir = os.path.abspath(state_dir) if state_dir else SCRIPT_DIR
        self.config_path = config_path
        self.fake_executor = fake_executor
        self.fake_executor_fn = fake_executor_fn
        # Real execution backend, duck-typed: .execute(request) -> outcome exposing
        # ok, stage, exit_code, command, head_sha, evidence, artifacts, blocked_reason,
        # backend_name. Kept deliberately separate from fake_executor_fn so a fixture
        # and a real run can never share a code path.
        self.worker_backend = worker_backend
        self.notify_telegram = notify_telegram
        self.telegram_project = telegram_project
        self.telegram_dry_run = telegram_dry_run
        self.telegram_send = telegram_send
        self.dry_run = dry_run
        self.repo_root = repo_root or os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))

        if coordinator:
            self.coordinator = coordinator
        else:
            self.coordinator = Coordinator(
                state_dir=self.state_dir,
                project_config=self.config_path,
                notify_telegram=self.notify_telegram,
                telegram_project=self.telegram_project,
                telegram_dry_run=self.telegram_dry_run,
                telegram_send=self.telegram_send,
            )

        if self.coordinator.project_config:
            self.project_config = self.coordinator.project_config
        elif self.config_path:
            self.project_config = set_current_project_config(self.config_path)
        else:
            self.project_config = get_current_project_config()
    def durable_upsert_bug(
        self,
        bug_id: str,
        prompt: str,
        reproduction_scenario: str,
        severity: str = "high",
        issue_number: Optional[int] = None,
        issue_url: Optional[str] = None,
        labels: Optional[List[str]] = None,
        owner: str = "BugIntake",
        task_type: str = "local_doc",
    ) -> Dict[str, Any]:
        """
        DURABLE BUG INTAKE INVARIANT:
        Every reported important bug must persist until QA specifically proves original reproduction gone.
        Intake must durable-upsert authoritative issue/card BEFORE dispatch, preserving original
        prompt, reproduction scenario, and severity. New prompts, compaction, or restarts cannot
        replace or delete an unresolved bug.
        """
        existing = None
        try:
            existing = self.coordinator.ledger.get_request(bug_id)
        except Exception:
            existing = None

        if existing:
            state = existing.get("state")
            if state != "done":
                return self.coordinator.ledger.update_request(
                    bug_id,
                    actor="BugIntake:Retain",
                    add_evidence={"summary": f"Retained unresolved bug '{bug_id}' in state '{state}' (original repro preserved)"},
                )

        criteria = [
            {
                "criterion": f"Original reproduction scenario proven absent: {reproduction_scenario}",
                "status": "pending",
                "evidence": "",
            },
            {
                "criterion": "No regressions introduced across adjacent contracts",
                "status": "pending",
                "evidence": "",
            },
        ]
        all_labels = ["type:bug", f"severity:{severity}"]
        if labels:
            all_labels.extend(labels)

        return self.coordinator.ledger.add_request(
            req_id=bug_id,
            prompt=prompt,
            session="durable-bug-intake",
            project=self.project_config.project_name or self.project_config.repo,
            acceptance_criteria=criteria,
            owner=owner,
            state="implementation",
            task_type=task_type,
            issue_number=issue_number,
            issue_url=issue_url,
            labels=all_labels,
        )

    def verify_bug_closure(
        self,
        req_id: str,
        tested_sha: str,
        reproduction_verified_absent: bool,
        regression_evidence: str,
        bug_type: str = "functional",
        desktop_after_url: Optional[str] = None,
        mobile_after_url: Optional[str] = None,
        desktop_before_url: Optional[str] = None,
        mobile_before_url: Optional[str] = None,
        before_unavailable_reason: Optional[str] = None,
        render_verified: bool = False,
        environment: str = "staging",
        user_explicit_disposition: Optional[str] = None,
        generic_suite_only: bool = False,
        no_repro_claimed: bool = False,
        deployed_signed_in_qa: bool = False,
    ) -> Tuple[bool, str]:
        """
        STRICT BUG CLOSURE INVARIANT:
        1. Every bug requires behavioral proof (command logs, test results, error trace gone).
           Screenshot alone is strictly insufficient for functional bugs.
        2. UI bugs require before/after visual assets tied to original reproduction, across desktop
           (1440px) and mobile (320px/390px) viewports on the exact head commit and environment.
           Images must be render-verified (commit-pinned/release/upload assets, no raw.githubusercontent).
           If 'before' is unavailable, it must be explicitly recorded as a documented limitation;
           never fabricated.
        3. Closure strictly binds exact original reproduction scenario + regression evidence + reviewed head.
           Cannot close via generic suite alone, related fix assumption, or 'no-repro'
           without explicit user disposition.
        """
        if generic_suite_only:
            return False, f"Cannot close bug '{req_id}': Generic test suite pass is not proof of specific reproduction absence."
        if no_repro_claimed and not user_explicit_disposition:
            return False, f"Cannot close bug '{req_id}': 'No-repro' claim requires explicit human user disposition before closure."
        if not reproduction_verified_absent:
            return False, f"Cannot close bug '{req_id}': Original reproduction scenario not proven absent on commit {tested_sha}."

        # 1. Behavioral proof required for all bugs (screenshots alone insufficient)
        if not regression_evidence or not regression_evidence.strip():
            return False, f"Cannot close bug '{req_id}': Specific behavioral reproduction regression proof (logs, traces, or test exit code) is required; screenshot alone is insufficient."

        # Closure must name the full authoritative commit, not a prefix, and it must be the
        # head the ledger is actually tracking — otherwise the proof describes another commit.
        if not tested_sha or not re.fullmatch(r"[0-9a-f]{40}", tested_sha.strip().lower()):
            return False, f"Cannot close bug '{req_id}': A full 40-character reviewed head commit SHA is required (got '{tested_sha}')."

        try:
            ledger_head = (self.coordinator.ledger.get_request(req_id) or {}).get("head")
        except Exception:
            ledger_head = None
        if ledger_head and ledger_head.strip().lower() != tested_sha.strip().lower():
            return False, (
                f"Cannot close bug '{req_id}': Closure evidence is bound to {tested_sha} but the "
                f"ledger's authoritative head is {ledger_head}; re-verify on the current head."
            )

        # 2. UI Bug Specific Visual Assets & Viewport Verification
        ui_evidence_parts = []
        if bug_type in ("ui", "both"):
            if not desktop_after_url or not desktop_after_url.strip():
                return False, f"Cannot close UI bug '{req_id}': Desktop after-fix visual asset URL (1440px) is mandatory."
            if not mobile_after_url or not mobile_after_url.strip():
                return False, f"Cannot close UI bug '{req_id}': Mobile after-fix visual asset URL (320px/390px) is mandatory."

            # Verify format: no prohibited raw.githubusercontent.com URLs (private repo 403/404)
            for url_name, url_val in [("desktop_after", desktop_after_url), ("mobile_after", mobile_after_url)]:
                if "raw.githubusercontent.com" in url_val:
                    return False, f"Cannot close UI bug '{req_id}': Prohibited raw.githubusercontent.com URL for {url_name} (fails on private repos); use commit-pinned or GitHub asset URL."

            # Before asset verification or explicit limitation
            if desktop_before_url and desktop_before_url.strip():
                if "raw.githubusercontent.com" in desktop_before_url:
                    return False, f"Cannot close UI bug '{req_id}': Prohibited raw.githubusercontent.com URL for desktop_before; use commit-pinned or GitHub asset URL."
                ui_evidence_parts.append(f"Desktop Before: {desktop_before_url.strip()}")
                if mobile_before_url:
                    ui_evidence_parts.append(f"Mobile Before: {mobile_before_url.strip()}")
            elif before_unavailable_reason and before_unavailable_reason.strip():
                ui_evidence_parts.append(f"Before Asset Limitation: {before_unavailable_reason.strip()} (explicitly documented, not fabricated)")
            else:
                return False, f"Cannot close UI bug '{req_id}': Before visual asset required, or explicit documented limitation if unavailable (do not fabricate)."

            if not render_verified:
                return False, f"Cannot close UI bug '{req_id}': Visual assets must be confirmed visibly rendered on {environment}."

            ui_evidence_parts.append(f"Desktop After (1440px): {desktop_after_url.strip()}")
            ui_evidence_parts.append(f"Mobile After (320px/390px): {mobile_after_url.strip()}")
            ui_evidence_parts.append(f"Render Verified: YES ({environment})")

        # A user-facing flow on a deployed environment must be proven under a real signed-in
        # session; an unauthenticated pass never reaches the flow. Backend-only defects are
        # excluded: their proof is the behavioral regression evidence already required above.
        # Checked after the asset gates so the more specific evidence gap is reported first.
        if (
            bug_type in ("ui", "both")
            and environment not in ("local", "harness")
            and not deployed_signed_in_qa
        ):
            return False, (
                f"Cannot close bug '{req_id}': Persistent signed-in QA on '{environment}' is required "
                "for a user-facing flow; an unauthenticated pass does not exercise it."
            )

        ui_section = f" UI Assets: [{', '.join(ui_evidence_parts)}]." if ui_evidence_parts else ""
        evidence_str = (
            f"Reproduction scenario specifically proven absent on reviewed head {tested_sha} ({environment}). "
            f"Behavioral Evidence: {regression_evidence.strip()}."
            f"{ui_section} "
            f"Signed-in QA: {'verified' if deployed_signed_in_qa else 'not required for ' + environment}. "
            f"[Empirical observation: specific reproduction scenario failed to trigger on reviewed head {tested_sha}; no mathematical proof of global absence claimed.]"
        )
        return True, evidence_str

    @staticmethod
    def _resolve_task_type(routing: Any, stage: str) -> TaskType:
        """
        Task type as classified by the coordinator, falling back to the stage.

        The coordinator surfaces `task_type` on its routing status when it has classified
        the request (including DEEP_REASONING, which the stage alone cannot express).
        """
        declared = getattr(routing, "task_type", None) if routing is not None else None
        if declared:
            if isinstance(declared, TaskType):
                return declared
            try:
                return TaskType(str(declared))
            except ValueError:
                pass
        return TaskType.STRONG_REVIEW if stage == "review" else TaskType.ROUTINE_EXECUTION

    @staticmethod
    def _resolve_risk_level(routing: Any, req: Union[RequestSummary, Dict[str, Any]]) -> RiskLevel:
        """
        Risk as classified by the coordinator, then the request, and only then LOW.

        Defaulting straight to LOW is what dropped HIGH-risk classifications, so an
        explicit signal from either source is always preferred.
        """
        declared = getattr(routing, "risk_level", None) if routing is not None else None
        if not declared:
            declared = getattr(req, "risk_level", None) or (
                req.get("risk_level") if isinstance(req, dict) else None
            )
        if declared:
            if isinstance(declared, RiskLevel):
                return declared
            try:
                return RiskLevel(str(declared).lower())
            except ValueError:
                pass
        return RiskLevel.LOW

    def _determine_stage_for_request(self, req: Union[RequestSummary, Dict[str, Any]]) -> str:
        """Map ledger request state to existing Superboard worker lifecycle stage."""
        state = getattr(req, "state", None) or (req.get("state") if isinstance(req, dict) else "")
        if state in ("pending", "implementation"):
            return "build"
        elif state == "QA":
            return "qa"
        elif state == "review":
            return "review"
        elif state == "awaiting authorization":
            return "awaiting_authorization"
        elif state in ("integration", "live verification", "done"):
            return "done"
        return "build"

    def default_fake_executor(
        self,
        req: Union[RequestSummary, Dict[str, Any]],
        stage: str,
        dispatch: HarnessDispatchPacket,
        target_sha: Optional[str] = None,
    ) -> WorkerExecutionResult:
        """
        Default fake executor producing labeled fixture results to safely prove
        coordinator gates, role routing, and ledger transitions without mutating external state.
        """
        req_id = getattr(req, "id", None) or (req.get("id") if isinstance(req, dict) else "req-unknown")
        req_head = getattr(req, "head", None) or (req.get("head") if isinstance(req, dict) else None)
        req_issue = getattr(req, "issue_number", None) or (req.get("issue_number") if isinstance(req, dict) else 74)
        sha = target_sha or req_head or f"simulated_sha_{stage}_{req_id[-8:]}"
        role = dispatch.recommendation.get("agent_role", "worker") if hasattr(dispatch, "recommendation") else getattr(dispatch, "recommended_role", "worker")
        model = dispatch.recommendation.get("model", "default") if hasattr(dispatch, "recommendation") else getattr(dispatch, "recommended_model", "default")
        fixture_label = f"[FIXTURE_EXECUTION_RESULT] request={req_id} stage={stage} role={role} model={model}"
        output = (
            f"{fixture_label}\n"
            f"Stage: {stage}\n"
            f"Head SHA: {sha}\n"
            f"Model Assigned: {model} (role: {role})\n"
            f"Status: advanced (simulated successful {stage} execution)\n"
        )
        return WorkerExecutionResult(
            stage=stage,
            exit_code=0,
            output=output,
            head_sha=sha,
            pr_url=f"https://github.com/{self.project_config.repo}/pull/{req_issue or 74}",
            command=["fake-executor", f"--stage={stage}", f"--request={req_id}"],
            is_fixture=True,
            fixture_label=fixture_label,
        )

    def execute_real_safe_worker(
        self,
        req: Union[RequestSummary, Dict[str, Any]],
        stage: str,
        dispatch: HarnessDispatchPacket,
    ) -> WorkerExecutionResult:
        """
        Executes a real, harmless worker task (such as super_board_runtime.config validation
        or git status probe) to verify actual subprocess dispatch, exit-code handling, and output capture.
        """
        python_exe = sys.executable
        req_id = getattr(req, "id", None) or (req.get("id") if isinstance(req, dict) else "req-unknown")
        req_head = getattr(req, "head", None) or (req.get("head") if isinstance(req, dict) else None)
        req_issue = getattr(req, "issue_number", None) or (req.get("issue_number") if isinstance(req, dict) else 74)

        # Harmless command: validate project config via super_board_runtime or verify clean git status
        if self.config_path and os.path.exists(self.config_path):
            cmd = [
                python_exe,
                os.path.join(self.repo_root, "scripts", "super-board-config.py"),
                "validate",
                "--config",
                self.config_path,
                "--json",
            ]
        else:
            cmd = ["git", "status", "--porcelain"]

        try:
            res = subprocess.run(
                cmd,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            sha = req_head or "current_local_head"
            output = f"[REAL_WORKER_EXECUTION] cmd={' '.join(cmd)}\nExit: {res.returncode}\nStdout: {res.stdout.strip()}\nStderr: {res.stderr.strip()}"
            return WorkerExecutionResult(
                stage=stage,
                exit_code=res.returncode,
                output=output,
                head_sha=sha,
                pr_url=f"https://github.com/{self.project_config.repo}/pull/{req_issue}" if req_issue else None,
                command=cmd,
                is_fixture=False,
                is_probe=True,
                backend_name="safe-probe",
            )
        except Exception as e:
            return WorkerExecutionResult(
                stage=stage,
                exit_code=1,
                output=f"[REAL_WORKER_ERROR] Failed to execute {cmd}: {e}",
                command=cmd,
                is_fixture=False,
                is_probe=True,
                backend_name="safe-probe",
                blocked_reason=f"Safe probe failed to execute {cmd}: {e}",
            )

    def _blocked_result(
        self,
        stage: str,
        reason: str,
        head_sha: Optional[str] = None,
        command: Optional[List[str]] = None,
    ) -> WorkerExecutionResult:
        """A dispatch that could not really run. Never carries advanceable evidence."""
        return WorkerExecutionResult(
            stage=stage,
            exit_code=1,
            output=f"[DISPATCH_BLOCKED] {reason}",
            head_sha=head_sha,
            command=command or [],
            is_fixture=False,
            blocked_reason=reason,
        )

    def dispatch_via_backend(
        self,
        req: Union[RequestSummary, Dict[str, Any]],
        stage: str,
        dispatch: HarnessDispatchPacket,
        target_sha: Optional[str] = None,
    ) -> WorkerExecutionResult:
        """
        Execute the stage through the configured real worker backend.

        The backend is duck-typed; only the published outcome field names are read, so no
        import of the backend module is required. Any failure, missing structured evidence,
        or head mismatch surfaces as a blocked result rather than a passable one.
        """
        req_id = getattr(req, "id", None) or (req.get("id") if isinstance(req, dict) else "req-unknown")
        req_head = getattr(req, "head", None) or (req.get("head") if isinstance(req, dict) else None)
        req_issue = getattr(req, "issue_number", None) or (req.get("issue_number") if isinstance(req, dict) else None)
        req_prompt = getattr(req, "prompt", None) or (req.get("prompt") if isinstance(req, dict) else "")
        criteria = getattr(req, "pending_criteria", None) or (
            req.get("pending_criteria") if isinstance(req, dict) else []
        )
        expected_sha = target_sha or req_head

        recommendation = dispatch.recommendation if isinstance(dispatch.recommendation, dict) else {}
        req_task_type = getattr(req, "task_type", None) or (
            req.get("task_type") if isinstance(req, dict) else None
        )
        worker_request = {
            "request_id": req_id,
            "stage": stage,
            "task_type": req_task_type,
            "head_sha": expected_sha,
            "model": recommendation.get("selected_model"),
            "agent_role": recommendation.get("agent_role"),
            "repo_root": self.repo_root,
            "issue_url": (
                f"https://github.com/{self.project_config.repo}/issues/{req_issue}" if req_issue else None
            ),
            "pr_url": None,
            "prompt": req_prompt,
            "criteria": list(criteria or []),
        }

        try:
            outcome = self.worker_backend.execute(worker_request)
        except Exception as e:
            return self._blocked_result(
                stage,
                f"Worker backend raised while executing stage '{stage}': {e}",
                head_sha=expected_sha,
            )

        def field_of(name: str, default: Any = None) -> Any:
            if isinstance(outcome, dict):
                return outcome.get(name, default)
            return getattr(outcome, name, default)

        blocked_reason = field_of("blocked_reason")
        ok = bool(field_of("ok", False))
        observed_sha = field_of("head_sha") or expected_sha
        command = list(field_of("command") or [])
        evidence = field_of("evidence") or {}
        backend_name = field_of("backend_name") or type(outcome).__name__
        exit_code = field_of("exit_code")
        exit_code = int(exit_code) if isinstance(exit_code, int) else (0 if ok else 1)
        artifacts = list(field_of("artifacts") or [])

        if not ok:
            return self._blocked_result(
                stage,
                blocked_reason or f"Worker backend '{backend_name}' reported failure for stage '{stage}'",
                head_sha=observed_sha,
                command=command,
            )

        # Head expectations differ by stage. A build produces work, so its observed head is
        # necessarily a NEW commit and becomes the authoritative head. QA and review must
        # verify the exact commit they were dispatched for, so any movement there voids the
        # evidence.
        if stage in ("qa", "review"):
            if expected_sha and observed_sha and expected_sha != observed_sha:
                return self._blocked_result(
                    stage,
                    (
                        f"Worker backend executed against head {observed_sha} but stage '{stage}' "
                        f"was dispatched for {expected_sha}; evidence is not head-bound"
                    ),
                    head_sha=observed_sha,
                    command=command,
                )
        elif stage == "build":
            # A build that moved nothing and produced nothing has not proved any work.
            if expected_sha and observed_sha == expected_sha and not artifacts:
                return self._blocked_result(
                    stage,
                    (
                        f"Build left head at {expected_sha} and produced no artifacts; "
                        "no work was performed"
                    ),
                    head_sha=observed_sha,
                    command=command,
                )

        if not evidence:
            return self._blocked_result(
                stage,
                f"Worker backend '{backend_name}' returned no structured evidence for stage '{stage}'",
                head_sha=observed_sha,
                command=command,
            )

        return WorkerExecutionResult(
            stage=stage,
            exit_code=exit_code,
            output=json.dumps({"backend": backend_name, "evidence": evidence, "artifacts": artifacts}, default=str),
            head_sha=observed_sha,
            command=command,
            is_fixture=False,
            is_probe=False,
            backend_name=backend_name,
            evidence=evidence,
        )

    def dispatch_qa_script(
        self,
        req: Union[RequestSummary, Dict[str, Any]],
        stage: str,
        target_sha: Optional[str] = None,
    ) -> Optional[WorkerExecutionResult]:
        """
        Dispatch the existing Superboard QA script for the QA stage.

        Returns None when the script is not present, so the caller can continue down the
        precedence chain. A dry run is marked as a probe: it proves dispatch works and
        nothing about the acceptance criteria.
        """
        qa_script = os.path.join(self.repo_root, "scripts", "super-qa-dispatch.sh")
        if not os.path.exists(qa_script):
            return None

        req_issue = getattr(req, "issue_number", None) or (req.get("issue_number") if isinstance(req, dict) else None)
        req_head = getattr(req, "head", None) or (req.get("head") if isinstance(req, dict) else None)
        expected_sha = target_sha or req_head

        cmd = [
            "bash",
            qa_script,
            "--config", self.config_path or ".claude/super-board/configs/default.json",
            "--issue-url", f"https://github.com/{self.project_config.repo}/issues/{req_issue or 1}",
        ]
        if req_issue:
            cmd.extend(["--pull-request", f"https://github.com/{self.project_config.repo}/pull/{req_issue}"])
        if self.dry_run:
            cmd.append("--dry-run")
        if expected_sha:
            cmd.extend(["--expected-sha", expected_sha])

        try:
            res = subprocess.run(cmd, cwd=self.repo_root, capture_output=True, text=True, timeout=60)
        except Exception as e:
            return self._blocked_result(
                stage,
                f"Error dispatching {qa_script}: {e}",
                head_sha=expected_sha,
                command=cmd,
            )

        output = res.stdout + ("\n" + res.stderr if res.stderr else "")
        if res.returncode != 0:
            return self._blocked_result(
                stage,
                f"QA script exited {res.returncode}",
                head_sha=expected_sha,
                command=cmd,
            )

        return WorkerExecutionResult(
            stage=stage,
            exit_code=0,
            output=output,
            head_sha=expected_sha,
            command=cmd,
            is_fixture=False,
            is_probe=self.dry_run,
            backend_name="super-qa-dispatch.sh",
            evidence={} if self.dry_run else {
                "backend": "super-qa-dispatch.sh",
                "stage": stage,
                "head_sha": expected_sha,
                "exit_code": 0,
                "output_tail": output[-2000:],
            },
        )

    def dispatch_worker(
        self,
        req: Union[RequestSummary, Dict[str, Any]],
        stage: str,
        dispatch: HarnessDispatchPacket,
        target_sha: Optional[str] = None,
        real_worker: bool = False,
    ) -> WorkerExecutionResult:
        """
        Dispatch the stage, in strict precedence:
          1. Explicit fixture executor (`fake_executor_fn` / `fake_executor`) — test fixtures only.
          2. Configured real worker backend.
          3. Explicit `real_worker` safe probe — plumbing proof only, never advanceable.
          4. Otherwise BLOCKED.

        There is deliberately no implicit fixture fallback: a missing backend is reported as a
        blocker, because silently substituting a fixture is what let simulated output be read
        as real proof.
        """
        if self.fake_executor_fn:
            return self.fake_executor_fn(req, stage, dispatch)
        if self.fake_executor:
            return self.default_fake_executor(req, stage, dispatch, target_sha=target_sha)

        if self.worker_backend is not None:
            return self.dispatch_via_backend(req, stage, dispatch, target_sha=target_sha)

        if stage == "qa":
            qa_result = self.dispatch_qa_script(req, stage, target_sha=target_sha)
            if qa_result is not None:
                return qa_result

        if real_worker:
            return self.execute_real_safe_worker(req, stage, dispatch)

        req_head = getattr(req, "head", None) or (req.get("head") if isinstance(req, dict) else None)
        return self._blocked_result(
            stage,
            (
                f"No worker backend configured for stage '{stage}'. Construct the adapter with "
                "worker_backend=<backend> for real execution, or pass fake_executor=True to run "
                "an explicitly labelled fixture. Refusing to substitute a fixture implicitly."
            ),
            head_sha=target_sha or req_head,
        )

    def verify_and_advance_request(
        self,
        req: Union[RequestSummary, Dict[str, Any]],
        stage: str,
        worker_res: WorkerExecutionResult,
    ) -> Tuple[str, str, Optional[Dict[str, Any]]]:
        """
        Point 5: Evidence, QA & Review Gate Eligibility.
        Verifies execution result, enforces head-bound SHA checks, and advances request state.

        INVIOLABLE SAFETY INVARIANTS:
        1. Only a real backend execution carrying structured evidence may advance state.
           A fixture or a probe never advances a request, whatever its output says.
        2. When a request passes review verification, it transitions strictly to
           'awaiting authorization'. It NEVER auto-merges or transitions to 'integration'.
        """
        req_id = getattr(req, "id", None) or (req.get("id") if isinstance(req, dict) else "req-unknown")
        req_state = getattr(req, "state", None) or (req.get("state") if isinstance(req, dict) else "unknown")
        req_head = getattr(req, "head", None) or (req.get("head") if isinstance(req, dict) else None)

        gate_result: Dict[str, Any] = {
            "verified": False,
            "gate_type": stage,
            "tested_sha": worker_res.head_sha,
            "head_bound": True,
            "evidence_source": worker_res.backend_name
            or ("fixture" if worker_res.is_fixture else "unknown"),
        }

        if worker_res.blocked_reason:
            gate_result["blocked_reason"] = worker_res.blocked_reason
            return req_state, f"Stage '{stage}' blocked: {worker_res.blocked_reason}", gate_result

        if worker_res.exit_code != 0:
            reason = f"Worker {stage} execution failed with exit code {worker_res.exit_code}"
            gate_result["error"] = worker_res.output
            return req_state, reason, gate_result

        # Bug retention: a defect whose reproduction is not proven absent must reopen, so
        # this is evaluated before the generic provenance gate. Reopening only ever moves a
        # request backwards, so it can never manufacture a false completion.
        is_bug = (
            getattr(req, "task_type", None) == "bug"
            or (isinstance(req, dict) and req.get("task_type") == "bug")
            or "bug" in req_id.lower()
        )
        if is_bug and stage in ("qa", "review"):
            # Closure requires a structured, explicit reproduction verdict from the backend.
            # Prose in the output and fixture provenance are both worthless here:
            # keyword-matching "simulated successful" is how simulated runs closed real defects.
            evidence = worker_res.evidence or {}
            repro = evidence.get("reproduction") if isinstance(evidence.get("reproduction"), dict) else {}
            verdict = str(repro.get("verdict") or evidence.get("reproduction_verdict") or "").strip().lower()
            scenario = str(repro.get("scenario") or evidence.get("reproduction_scenario") or "").strip()

            if not (verdict == "absent" and scenario):
                if not verdict:
                    why = (
                        "no structured reproduction verdict "
                        "(evidence.reproduction.verdict) was returned"
                    )
                elif verdict != "absent":
                    why = f"reproduction verdict is '{verdict}', not 'absent'"
                else:
                    why = "reproduction verdict lacks the original scenario it was run against"

                self.coordinator.ledger.update_request(
                    req_id,
                    state="implementation",
                    actor="SuperboardAdapter:Reopen",
                    add_evidence={
                        "summary": (
                            f"QA failed to prove original reproduction absent on "
                            f"{worker_res.head_sha or 'unknown'}: {why}; reopened to implementation"
                        ),
                        "details": worker_res.output,
                        "head": worker_res.head_sha,
                    },
                )
                gate_result["verified"] = False
                gate_result["reopened"] = True
                gate_result["repro_refused"] = why
                return (
                    "implementation",
                    (
                        f"Original reproduction scenario not proven absent on "
                        f"{worker_res.head_sha}: {why}; reopened to implementation"
                    ),
                    gate_result,
                )

            gate_result["reproduction_scenario"] = scenario

        # Provenance gate: simulated or probe output is not evidence about this request.
        if not worker_res.is_verifiable_evidence:
            if worker_res.is_fixture:
                why = (
                    f"fixture result '{worker_res.fixture_label or 'unlabelled'}' is not evidence "
                    "of real execution"
                )
            elif worker_res.is_probe:
                why = "safe probe proves dispatch plumbing only, not the acceptance criteria"
            else:
                why = "worker returned no structured evidence"
            gate_result["advance_refused"] = why
            return (
                req_state,
                f"Stage '{stage}' did not advance: {why}. Request remains in '{req_state}'.",
                gate_result,
            )

        # Head binding applies to the verification stages. QA and review must describe the
        # exact commit the ledger tracks; a build legitimately produces a new head, which is
        # recorded below as the request's new authoritative head.
        if (
            stage in ("qa", "review")
            and req_head
            and worker_res.head_sha
            and req_head != worker_res.head_sha
        ):
            gate_result["head_bound"] = False
            gate_result["advance_refused"] = "head mismatch"
            return (
                req_state,
                (
                    f"Stage '{stage}' did not advance: evidence is bound to {worker_res.head_sha} "
                    f"but the ledger head is {req_head}."
                ),
                gate_result,
            )

        # Update evidence in ledger
        evidence_note = (
            f"{stage} verified via {worker_res.backend_name or 'worker execution'} "
            f"on commit {worker_res.head_sha or 'unknown'}"
        )
        if stage == "build":
            # Advance: implementation -> QA
            new_state = "QA"
            self.coordinator.ledger.update_request(
                req_id,
                state=new_state,
                head=worker_res.head_sha or req_head,
                actor="SuperboardAdapter:Build",
                add_evidence={"summary": evidence_note, "details": worker_res.output, "head": worker_res.head_sha},
            )
            gate_result["verified"] = True
            gate_result["new_state"] = new_state
            return new_state, f"Build completed successfully; advanced to {new_state}", gate_result

        elif stage == "qa":
            # Advance: QA -> review
            new_state = "review"
            self.coordinator.ledger.update_request(
                req_id,
                state=new_state,
                head=worker_res.head_sha or req_head,
                actor="SuperboardAdapter:QA",
                add_evidence={"summary": evidence_note, "details": worker_res.output, "head": worker_res.head_sha},
            )
            gate_result["verified"] = True
            gate_result["new_state"] = new_state
            return new_state, f"Exact-SHA QA verified; advanced to {new_state}", gate_result

        elif stage == "review":
            # Review complete -> STOP at 'awaiting authorization' (Human gate!)
            new_state = "awaiting authorization"
            self.coordinator.ledger.update_request(
                req_id,
                state=new_state,
                head=worker_res.head_sha or req_head,
                actor="SuperboardAdapter:Review",
                add_evidence={"summary": evidence_note, "details": worker_res.output, "head": worker_res.head_sha},
            )
            gate_result["verified"] = True
            gate_result["new_state"] = new_state
            gate_result["human_authorization_required"] = True
            return new_state, f"Independent review verified; request is awaiting human authorization to merge", gate_result

        return req_state, f"Stage {stage} concluded with state {req_state}", gate_result

    def emit_telegram_event(
        self,
        req: Optional[Union[RequestSummary, Dict[str, Any]]],
        status: str,
        stage: str,
        summary: str,
    ) -> Optional[Dict[str, Any]]:
        """Point 6: Emit concise single-sentence status event via TelegramNotificationAdapter."""
        if not self.notify_telegram:
            return None

        event_type = "milestone"
        if status in ("blocked", "error"):
            event_type = "blocker"
        elif status == "awaiting_authorization":
            event_type = "completion"
        elif status == "done":
            event_type = "completion"

        req_id = (getattr(req, "id", None) or (req.get("id") if isinstance(req, dict) else None)) if req else "req-superboard-adapter"
        req_issue = (getattr(req, "issue_number", None) or (req.get("issue_number") if isinstance(req, dict) else None)) if req else 75
        link = f"https://github.com/{self.project_config.repo}/issues/{req_issue or 75}" if req else f"https://github.com/{self.project_config.repo}"

        event = NotificationEvent(
            event_type=event_type,
            project=self.project_config.project_name or self.project_config.repo,
            request_id=req_id,
            summary=summary,
            canonical_link=link,
            metadata={"stage": stage, "status": status},
        )

        adapter = TelegramNotificationAdapter()
        dry_run_mode = self.telegram_dry_run or (not self.telegram_send)
        receipt = adapter.notify(event, dry_run=dry_run_mode)
        return asdict(receipt)

    def run_step(
        self,
        request_id: Optional[str] = None,
        target_sha: Optional[str] = None,
        real_worker: bool = False,
    ) -> AdapterExecutionResult:
        """
        Executes a complete single, bounded coordinator-to-adapter step:
          1. Request Intake & Eligibility Check
          2. Preflight Gate
          3. Model/Role Dispatch Packet
          4. Existing Worker Command Dispatch
          5. Evidence, QA & Review Gate Verification
          6. Concise Telegram Event
          7. Strict Boundary Assertion (execution_dispatched=True, auto_merge=False)
        """
        step_id = f"step_{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        # 1. Evaluate Coordinator Step (Intake, Preflight, Model Routing)
        packet = self.coordinator.evaluate_step(request_id=request_id)

        # Handle terminal or non-actionable coordinator states
        if packet.status in ("done", "completed"):
            return AdapterExecutionResult(
                step_id=step_id,
                request_id=None,
                stage="none",
                status="done",
                status_reason=packet.status_reason,
                next_action=packet.next_action,
                preflight_passed=packet.preflight.passed,
                boundaries=asdict(packet.boundaries),
            )

        if not packet.request:
            return AdapterExecutionResult(
                step_id=step_id,
                request_id=None,
                stage="none",
                status=packet.status,
                status_reason=packet.status_reason,
                next_action=packet.next_action,
                preflight_passed=packet.preflight.passed,
                boundaries=asdict(packet.boundaries),
            )

        req = packet.request
        stage = self._determine_stage_for_request(req)

        # 2. Check Human Authorization Gate (Inviolable Human Gate: No Auto-Merge)
        if stage == "awaiting_authorization" or getattr(req, "state", None) == "awaiting authorization":
            reason = f"Request '{req.id}' is awaiting explicit human operator authorization to merge"
            receipt = self.emit_telegram_event(req, "awaiting_authorization", "awaiting_authorization", reason)
            return AdapterExecutionResult(
                step_id=step_id,
                request_id=req.id,
                stage="awaiting_authorization",
                status="awaiting_authorization",
                status_reason=reason,
                next_action="Human operator must verify evidence and perform merge commit (--no-ff)",
                preflight_passed=True,
                notification_receipt=receipt,
                boundaries=asdict(packet.boundaries),
            )

        # 3. Check Preflight Gate
        if not packet.preflight.passed:
            blocker_summary = f"Preflight gate blocked request '{req.id}': {'; '.join(packet.preflight.blockers)}"
            receipt = self.emit_telegram_event(req, "blocked", "preflight", blocker_summary)
            return AdapterExecutionResult(
                step_id=step_id,
                request_id=req.id,
                stage="preflight",
                status="blocked",
                status_reason=blocker_summary,
                next_action="Resolve staging infrastructure blocker before continuing",
                preflight_passed=False,
                notification_receipt=receipt,
                boundaries=asdict(packet.boundaries),
            )

        # Build the HarnessDispatchPacket from the coordinator's classification. The
        # coordinator is the single routing authority: it already classified task type and
        # risk from the request's prompt, labels and state. Re-deriving them here used to
        # silently downgrade every non-review stage to routine_execution/low and discard a
        # HIGH-risk decision, so the classification is read from the coordinator's routing
        # and only falls back when that routing was not evaluated.
        selector = ResetAwareModelSelector(
            snapshot=self.coordinator.resolve_balance_adapter(),
        )
        routing = packet.routing
        task_type = self._resolve_task_type(routing, stage)
        risk_level = self._resolve_risk_level(routing, req)
        dispatch_packet = selector.dispatch(
            task_type=task_type,
            risk_level=risk_level,
            context_tokens=4000,
            head_sha=getattr(req, "head", None) or (req.get("head") if isinstance(req, dict) else None),
        )

        # The coordinator's model choice wins over a re-selection, so a divergence can never
        # quietly hand the worker a weaker model than the one the coordinator authorised.
        if routing and getattr(routing, "evaluated", False) and routing.recommended_model:
            recommendation = (
                dispatch_packet.recommendation
                if isinstance(dispatch_packet.recommendation, dict)
                else {}
            )
            if recommendation.get("selected_model") != routing.recommended_model:
                recommendation["adapter_reselected_model"] = recommendation.get("selected_model")
                recommendation["selected_model"] = routing.recommended_model
                recommendation["model_authority"] = "coordinator"
                if routing.recommended_role:
                    recommendation["agent_role"] = routing.recommended_role
                dispatch_packet.recommendation = recommendation

        # 4. Dispatch Worker Command (Fake, Safe-Probe, or Real)
        worker_res = self.dispatch_worker(
            req=req,
            stage=stage,
            dispatch=dispatch_packet,
            target_sha=target_sha,
            real_worker=real_worker,
        )

        # 5. Verify Evidence & Advance Request State
        new_state, transition_reason, gate_result = self.verify_and_advance_request(
            req=req,
            stage=stage,
            worker_res=worker_res,
        )

        # 6. Concise Telegram Event
        telegram_summary = f"Superboard request '{req.id}' {transition_reason}"
        receipt = self.emit_telegram_event(req, new_state, stage, telegram_summary)

        # 7. Explicit Boundaries with execution_dispatched=True
        boundaries = asdict(packet.boundaries)
        boundaries["execution_dispatched"] = True
        boundaries["auto_merge_allowed"] = False
        boundaries["auto_deploy_allowed"] = False
        boundaries["self_spawn_loop"] = False

        status = "advanced"
        if new_state == "awaiting authorization":
            status = "awaiting_authorization"
        elif worker_res.exit_code != 0:
            status = "error"

        return AdapterExecutionResult(
            step_id=step_id,
            request_id=req.id,
            stage=stage,
            status=status,
            status_reason=transition_reason,
            next_action=f"Continue execution in {new_state}" if status != "awaiting_authorization" else "Awaiting human merge authorization",
            preflight_passed=True,
            dispatch_packet=dispatch_packet.to_dict(),
            worker_result=worker_res,
            gate_result=gate_result,
            notification_receipt=receipt,
            boundaries=boundaries,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Superboard Execution Adapter: Bridges portable coordinator with existing Superboard loop"
    )
    parser.add_argument("--config", default=None, help="Path to Superboard project config JSON")
    parser.add_argument("--state-dir", default=None, help="Directory containing ledger.json and state")
    parser.add_argument("--request-id", default=None, help="Target specific request ID in ledger")
    parser.add_argument("--stage", default=None, choices=["build", "qa", "review"], help="Force specific worker stage")
    parser.add_argument("--fake-executor", action="store_true", default=True, help="Use fake executor with labeled fixture output")
    parser.add_argument("--real-worker", action="store_true", help="Execute safe harmless real worker command")
    parser.add_argument("--notify-telegram", action="store_true", help="Enable Telegram notifications")
    parser.add_argument("--telegram-dry-run", action="store_true", default=True, help="Dry-run Telegram notification")
    parser.add_argument("--telegram-send", action="store_true", help="Send live Telegram notification")
    parser.add_argument("--dry-run", action="store_true", help="Dry run without mutating git worktrees")
    parser.add_argument("--json", action="store_true", help="Output JSON result")
    parser.add_argument("--summary", action="store_true", help="Output human-readable summary")
    return parser


def format_adapter_summary(res: AdapterExecutionResult) -> str:
    lines = [
        "=" * 70,
        "SUPERBOARD EXECUTION ADAPTER - STEP EXECUTION RESULT",
        "=" * 70,
        f"Step ID:         {res.step_id}",
        f"Request ID:      {res.request_id or 'None'}",
        f"Stage:           {res.stage.upper()}",
        f"Status:          {res.status.upper()}",
        f"Reason:          {res.status_reason}",
        f"Next Action:     {res.next_action}",
        "-" * 70,
        f"Preflight:       {'PASSED' if res.preflight_passed else 'BLOCKED'}",
    ]
    if res.dispatch_packet:
        dp = res.dispatch_packet
        lines.extend([
            f"Assigned Model:  {dp.get('recommended_model')} (role: {dp.get('recommended_role')})",
            f"Context Window:  {dp.get('context_window')} tokens",
        ])
    if res.worker_result:
        wr = res.worker_result
        lines.extend([
            f"Worker Command:  {' '.join(wr.command) if wr.command else 'N/A'}",
            f"Worker Exit:     {wr.exit_code}",
            f"Worker Head SHA: {wr.head_sha or 'N/A'}",
            f"Is Fixture:      {wr.is_fixture} ({wr.fixture_label or 'none'})",
        ])
    if res.gate_result:
        gr = res.gate_result
        lines.extend([
            f"Gate Verified:   {gr.get('verified')} (head_bound: {gr.get('head_bound')})",
            f"Human Auth Req:  {gr.get('human_authorization_required', False)}",
        ])
    lines.extend([
        "-" * 70,
        "BOUNDARIES:",
        f"  Execution Dispatched:   {res.boundaries.get('execution_dispatched', False)}",
        f"  Auto-Merge Allowed:     {res.boundaries.get('auto_merge_allowed', False)}",
        f"  Auto-Deploy Allowed:    {res.boundaries.get('auto_deploy_allowed', False)}",
        f"  Self-Spawn Loop:        {res.boundaries.get('self_spawn_loop', False)}",
        "=" * 70,
    ])
    return "\n".join(lines)


def main():
    parser = build_parser()
    args = parser.parse_args()

    adapter = SuperboardExecutionAdapter(
        state_dir=args.state_dir,
        config_path=args.config,
        fake_executor=args.fake_executor and not args.real_worker,
        notify_telegram=args.notify_telegram,
        telegram_dry_run=args.telegram_dry_run,
        telegram_send=args.telegram_send,
        dry_run=args.dry_run,
    )

    res = adapter.run_step(
        request_id=args.request_id,
        real_worker=args.real_worker,
    )

    if args.json or not args.summary:
        print(res.to_json())
    else:
        print(format_adapter_summary(res))


if __name__ == "__main__":
    main()
