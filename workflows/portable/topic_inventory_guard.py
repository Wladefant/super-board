#!/usr/bin/env python3
"""
topic_inventory_guard.py - Deterministic coverage and inventory invariant engine.

Enforces lossless task inventory preservation, topic ownership, active worker
coverage, worker floor compliance, and generation of actionable next-ready assignments.

Strict invariants enforced:
1. Lossless Additive Inventory: All baseline items across snapshot boards, scope verification,
   open issue inventory, and ledger are reconciled additively. Zero items may be silently dropped.
   Distinguishes 342 machine todos from cross-source references.
2. Host-Observed Running Native Worker Coverage: Every unfinished, runnable topic MUST have at least one
   actively RUNNING native worker. Idle, parked, prepared tickets, reminder daemons, and completed workers
   do NOT count as active. Static assigned owner text on a task does not satisfy coverage without
   a host-observed running native worker.
3. Worker Floor: At least 7 actively RUNNING useful workers (ceiling 20) when >= 7 runnable independent units
   of work exist, unless an explicit measured RAM capacity ceiling (>= 95%) or explicit capacity limit applies.
4. Authorization Integrity: Active workers must be assigned to authorized tasks/targets; unauthorized
   tasks or forbidden targets (production/merge) are blocked.
5. Actionable Next-Ready Assignments: Priority is given to achieving one running worker per
   runnable topic before stacking extra workers in an already-covered topic.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# ONLY host-observed "running" status counts as active per live operator policy.
# Idle, parked, prepared, completed, or reminder daemons are strictly INACTIVE.
VALID_ACTIVE_STATUSES = {"running"}
INACTIVE_STATUSES = {
    "idle", "parked", "completed", "stopped", "terminated", "prepared",
    "prepared_ticket", "reminder_daemon", "simulated_active", "fake"
}

# Known topic keywords for mapping native agent names to functional topics
TOPIC_KEYWORDS_MAP: Dict[str, str] = {
    "gui": "Desktop GUI",
    "desktop": "Desktop GUI",
    "webprovider": "Desktop GUI",
    "chatgpt": "Desktop GUI",
    "design": "UX/design",
    "conversion": "UX/design",
    "motion": "Motion",
    "telegram": "Telegram",
    "decision": "Decisions",
    "staging": "Staging",
    "pooler": "Staging",
    "workflow": "Workflow",
    "gate": "Workflow",
    "concurrent": "Workflow",
    "todo": "Workflow",
    "issue": "Preserved GitHub issue inventory",
    "migration": "Preserved GitHub issue inventory",
    "dedicated": "Preserved GitHub issue inventory",
    "operator": "Operator accountability",
    "accountability": "Operator accountability",
    "live": "Live operator corrections",
}

DEFAULT_TOPIC_ISSUES: Dict[str, str] = {
    "Desktop GUI": "https://github.com/Bavariance/polysimulator/issues/4582",
    "UX/design": "https://github.com/Bavariance/polysimulator/issues/4582",
    "Motion": "https://github.com/Bavariance/polysimulator/issues/4582",
    "Telegram": "https://github.com/Bavariance/polysimulator/issues/4582",
    "Decisions": "https://github.com/Bavariance/polysimulator/issues/4582",
    "Staging": "https://github.com/Bavariance/polysimulator/issues/4574",
    "Workflow": "https://github.com/Bavariance/polysimulator/issues/4582",
    "Operator accountability": "https://github.com/Bavariance/polysimulator/issues/4582",
    "Live operator corrections": "https://github.com/Bavariance/polysimulator/issues/4582",
    "Preserved GitHub issue inventory": "https://github.com/Bavariance/polysimulator/issues/4543",
    "Agent system change": "https://github.com/Bavariance/polysimulator/issues/4543",
    "Recovered authorized work": "https://github.com/Bavariance/polysimulator/issues/4582",
    "Motion acceptance details": "https://github.com/Bavariance/polysimulator/issues/4582",
    "Issue backlog": "https://github.com/Bavariance/polysimulator/issues/4543",
    "Operator follow-through": "https://github.com/Bavariance/polysimulator/issues/4582",
}

# Explicitly cancelled/dropped tasks whose history must be preserved, not restored as pending
EXPLICITLY_DROPPED_TASKS: Dict[str, str] = {
    "record operator choice a for ci connectivity": "Explicitly dropped per live operator clarification (example choice, not confirmed decision); preserved as historical cancellation.",
}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def canonical_item_key(content: str, topic: Optional[str] = None) -> str:
    """Derive canonical identifier from item content and topic."""
    text = re.sub(r"\s+", " ", content.strip().lower())
    text = re.sub(r"^issue\s*#?\d+\s*:\s*", "", text)
    return f"{topic.strip().lower()}::{text}" if topic else text


@dataclass
class InventoryItem:
    """A preserved task or issue in the durable topic inventory."""
    id: str
    content: str
    phase: str
    status: str = "pending"  # pending | in_progress | completed | blocked | cancelled
    owner: Optional[str] = None
    issue_url: Optional[str] = None
    issue_number: Optional[int] = None
    blocker_reason: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def is_completed(self) -> bool:
        return self.status in ("completed", "done", "verified")

    @property
    def is_cancelled(self) -> bool:
        return self.status in ("cancelled", "dropped", "dropped_by_operator")

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocker_reason) or self.status == "blocked"

    @property
    def is_runnable(self) -> bool:
        return not self.is_completed and not self.is_cancelled and not self.is_blocked


@dataclass
class NativeWorker:
    """A classified native actor from the host session roster."""
    id: str
    status: str
    is_active: bool  # True ONLY if status == "running" and useful subagent
    assigned_topic: Optional[str] = None
    assigned_task_id: Optional[str] = None
    is_authorized: bool = True
    role: str = "sub"  # "main" = orchestrator; "sub" / "task" = worker
    raw_status: str = ""
    rejection_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def is_useful_worker(self) -> bool:
        return self.is_active and self.role != "main" and self.is_authorized


@dataclass
class CoverageViolation:
    """A reported breach of topic coverage, inventory preservation, or worker policies."""
    kind: str  # DROPPED_TASK | UNCOVERED_RUNNABLE_TOPIC | WORKER_FLOOR_DEFICIT | UNAUTHORIZED_ACTIVE_WORKER | UNLINKED_TOPIC_ISSUE | ABSENT_OWNER | FAKE_WORKER_REJECTED | IDLE_WORKER_REJECTED
    message: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class NextAssignment:
    """An actionable assignment recommendation for Main."""
    topic: str
    task_id: str
    content: str
    recommended_role: str = "fast"
    issue_url: Optional[str] = None
    criteria: List[str] = field(default_factory=list)
    priority: int = 1  # 1 = uncovered runnable topic; 2 = secondary worker on multi-task topic

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GuardReport:
    """Full deterministic inventory and coverage audit result."""
    ok: bool
    timestamp: str
    total_tasks: int
    completed_tasks: int
    cancelled_tasks: int
    runnable_tasks: int
    blocked_tasks: int
    active_workers_count: int  # ONLY running useful workers
    idle_workers_count: int
    parked_workers_count: int
    fake_workers_rejected_count: int
    required_floor: int
    floor_satisfied: bool
    capacity_exception: Optional[str] = None
    violations: List[CoverageViolation] = field(default_factory=list)
    next_assignments: List[NextAssignment] = field(default_factory=list)
    uncovered_topics: List[str] = field(default_factory=list)
    covered_topics: List[str] = field(default_factory=list)
    blocked_topics: List[Dict[str, Any]] = field(default_factory=list)
    reconciled_inventory_count: int = 0
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


class TopicInventoryGuard:
    """
    Executable coverage and inventory invariant engine.
    Integrates cleanly with ledger and continuation driver.
    """

    def __init__(
        self,
        baseline_sources: Optional[Sequence[Union[str, Dict[str, Any]]]] = None,
        min_worker_floor: int = 7,
        max_worker_ceiling: int = 20,
        ram_ceiling_pct: float = 95.0,
        default_topic_issues: Optional[Dict[str, str]] = None,
    ):
        self.baseline_sources = list(baseline_sources or [])
        self.min_worker_floor = min_worker_floor
        self.max_worker_ceiling = max_worker_ceiling
        self.ram_ceiling_pct = ram_ceiling_pct
        self.topic_issues = dict(default_topic_issues or DEFAULT_TOPIC_ISSUES)

        # Incorporate dedicated topic issue migration if available
        migration_candidates = [
            os.path.join(SCRIPT_DIR, "dedicated-topic-issue-migration.json"),
            os.path.expanduser("~/.veyyon/workflows/dedicated-topic-issue-migration.json"),
        ]
        for mc in migration_candidates:
            if os.path.exists(mc):
                try:
                    with open(mc, "r", encoding="utf-8") as f:
                        m_data = json.load(f)
                    for req_id, top_info in m_data.get("topics", {}).items():
                        url = top_info.get("dedicated_issue_url")
                        if url:
                            if "gui" in req_id:
                                self.topic_issues["Desktop GUI"] = url
                            elif "design" in req_id:
                                self.topic_issues["UX/design"] = url
                            elif "motion" in req_id:
                                self.topic_issues["Motion"] = url
                            elif "telegram" in req_id:
                                self.topic_issues["Telegram"] = url
                            elif "decision" in req_id:
                                self.topic_issues["Decisions"] = url
                            elif any(k in req_id for k in ("workflow", "recurrence", "todo", "scope")):
                                self.topic_issues["Workflow"] = url
                except Exception:
                    pass

    def parse_inventory_source(self, source: Union[str, Dict[str, Any]]) -> List[InventoryItem]:
        """Parse tasks/issues from a file path or in-memory dictionary."""
        data: Dict[str, Any]
        if isinstance(source, str):
            if not os.path.exists(source):
                return []
            try:
                with open(source, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                raise ValueError(f"Failed to load inventory source at {source}: {e}")
        elif isinstance(source, dict):
            data = source
        else:
            return []

        items: List[InventoryItem] = []

        # 1. Format: Machine board view (full-topic-board-current.json or scope details)
        phases = data.get("phases") or data.get("details", {}).get("phases")
        if isinstance(phases, list):
            for p_idx, phase in enumerate(phases):
                p_name = phase.get("name") or f"Phase-{p_idx}"
                tasks = phase.get("tasks", [])
                for t_idx, t in enumerate(tasks):
                    content = t.get("content", "").strip()
                    if not content:
                        continue
                    status = t.get("status", "pending")
                    blocker_reason = t.get("blocker_reason")

                    # Check for explicitly cancelled tasks per operator clarification
                    c_norm = content.strip().lower()
                    if c_norm in EXPLICITLY_DROPPED_TASKS or status in ("cancelled", "dropped", "dropped_by_operator"):
                        norm_status = "cancelled"
                        blocker_reason = blocker_reason or EXPLICITLY_DROPPED_TASKS.get(
                            c_norm, "Explicitly cancelled per operator clarification."
                        )
                    elif status in ("completed", "done"):
                        norm_status = "completed"
                    elif status in ("in_progress", "active"):
                        norm_status = "in_progress"
                    elif status == "blocked" or blocker_reason:
                        norm_status = "blocked"
                    else:
                        norm_status = "pending"

                    task_id = t.get("id") or f"task-{p_idx}-{t_idx}"
                    items.append(InventoryItem(
                        id=task_id,
                        content=content,
                        phase=p_name,
                        status=norm_status,
                        owner=t.get("owner"),
                        issue_url=t.get("issue_url") or self.topic_issues.get(p_name),
                        blocker_reason=blocker_reason,
                        metadata={"source_type": "board", "phase_index": p_idx},
                    ))
            return items

        # 2. Format: Open issue inventory (open-issue-inventory.json)
        if "issues" in data and isinstance(data["issues"], list):
            for issue in data["issues"]:
                num = issue.get("number")
                title = issue.get("title", "").strip()
                content = f"Issue #{num}: {title}" if num else title
                url = issue.get("url")
                domain = issue.get("domain") or "Preserved GitHub issue inventory"
                items.append(InventoryItem(
                    id=issue.get("id") or f"issue-{num}",
                    content=content,
                    phase="Preserved GitHub issue inventory",
                    status="pending" if issue.get("state") == "OPEN" else "completed",
                    owner=(issue.get("assignees") or [None])[0],
                    issue_url=url,
                    issue_number=num,
                    metadata={"source_type": "github_issues", "domain": domain},
                ))
            return items

        # 3. Format: Ledger data (ledger.json)
        if "requests" in data and isinstance(data["requests"], dict):
            for rid, req in data["requests"].items():
                prompt = req.get("prompt", rid)
                state = req.get("state", "pending")
                norm_status = "completed" if state == "done" else ("blocked" if req.get("blocker") else "in_progress")
                items.append(InventoryItem(
                    id=rid,
                    content=prompt,
                    phase=req.get("topic") or req.get("section") or "Recovered authorized work",
                    status=norm_status,
                    owner=req.get("owner"),
                    issue_url=req.get("github", {}).get("issue_url"),
                    blocker_reason=req.get("blocker"),
                    dependencies=req.get("dependencies", []),
                    metadata={"source_type": "ledger", "state": state},
                ))
            return items

        return items

    def reconcile_sources_additively(
        self,
        sources: Optional[Sequence[Union[str, Dict[str, Any]]]] = None,
        current_inventory: Optional[Sequence[InventoryItem]] = None,
    ) -> Tuple[List[InventoryItem], List[CoverageViolation]]:
        """
        Reconcile full source inventory additively.
        Preserves every task from baseline sources; reports any dropped items.
        """
        all_sources = list(sources or self.baseline_sources)
        baseline_items: List[InventoryItem] = []
        for src in all_sources:
            baseline_items.extend(self.parse_inventory_source(src))

        canonical_map: Dict[str, InventoryItem] = {}
        for item in baseline_items:
            key = canonical_item_key(item.content, item.phase)
            if key not in canonical_map:
                canonical_map[key] = item
            else:
                existing = canonical_map[key]
                # Status precedence: cancelled > completed > blocked > in_progress > pending
                if item.is_cancelled:
                    existing.status = "cancelled"
                    existing.blocker_reason = item.blocker_reason or existing.blocker_reason
                elif item.is_completed and not existing.is_cancelled:
                    existing.status = item.status
                if item.owner and not existing.owner:
                    existing.owner = item.owner
                if item.issue_url and not existing.issue_url:
                    existing.issue_url = item.issue_url
                if item.blocker_reason and not existing.blocker_reason:
                    existing.blocker_reason = item.blocker_reason

        reconciled_list = list(canonical_map.values())
        violations: List[CoverageViolation] = []

        if current_inventory is not None:
            current_keys = {canonical_item_key(item.content, item.phase): item for item in current_inventory}
            for key, baseline_item in canonical_map.items():
                if key not in current_keys:
                    violations.append(CoverageViolation(
                        kind="DROPPED_TASK",
                        message=f"Task missing/dropped from inventory: [{baseline_item.id}] '{baseline_item.content}' in phase '{baseline_item.phase}'.",
                        details={"item_id": baseline_item.id, "content": baseline_item.content, "phase": baseline_item.phase},
                    ))

        return reconciled_list, violations

    def map_worker_to_topic(self, worker_id: str, assigned_topic: Optional[str] = None) -> Optional[str]:
        """Infer or confirm which topic a native worker is assigned to."""
        if assigned_topic:
            return assigned_topic

        w_lower = worker_id.lower()
        for kw, topic_name in TOPIC_KEYWORDS_MAP.items():
            if kw in w_lower:
                return topic_name

        return None

    def classify_roster(
        self,
        roster: Sequence[Dict[str, Any]],
        authorized_task_ids: Optional[Set[str]] = None,
    ) -> Tuple[List[NativeWorker], List[NativeWorker], List[NativeWorker], List[CoverageViolation]]:
        """
        Classify running (active) vs idle vs parked/fake native workers.
        ONLY status == 'running' counts as active. Idle, parked, completed,
        reminder daemons, and prepared tickets are strictly non-active.
        Returns: (active_running, idle_workers, parked_workers, violations)
        """
        active_workers: List[NativeWorker] = []
        idle_workers: List[NativeWorker] = []
        parked_workers: List[NativeWorker] = []
        violations: List[CoverageViolation] = []

        for entry in roster:
            w_id = entry.get("id") or entry.get("name") or ""
            raw_status = str(entry.get("status", "")).strip().lower()
            role = entry.get("role", "sub")

            if not w_id:
                violations.append(CoverageViolation(
                    kind="FAKE_WORKER_REJECTED",
                    message="Roster entry missing valid worker identifier.",
                    details=entry,
                ))
                continue

            # Check for fake / unsupported / daemon / prepared status
            if raw_status in ("prepared", "prepared_ticket", "reminder_daemon", "simulated_active", "fake"):
                violations.append(CoverageViolation(
                    kind="FAKE_WORKER_REJECTED",
                    message=f"Worker '{w_id}' declared unsupported/fake active status '{raw_status}'. Prepared tickets and reminder daemons cannot count as active.",
                    details={"worker_id": w_id, "status": raw_status},
                ))
                continue

            # Check for idle: IDLE IS NOT ACTIVE per operator instruction
            if raw_status == "idle":
                idle_workers.append(NativeWorker(
                    id=w_id,
                    status="idle",
                    is_active=False,
                    assigned_topic=self.map_worker_to_topic(w_id, entry.get("topic")),
                    assigned_task_id=entry.get("task_id"),
                    role=role,
                    raw_status=raw_status,
                    rejection_reason="Worker is idle, not actively executing. Operator requires actively running useful workers.",
                ))
                continue

            # Check for parked / completed / stopped
            if raw_status in ("parked", "completed", "stopped", "terminated"):
                parked_workers.append(NativeWorker(
                    id=w_id,
                    status=raw_status,
                    is_active=False,
                    assigned_topic=self.map_worker_to_topic(w_id, entry.get("topic")),
                    assigned_task_id=entry.get("task_id"),
                    role=role,
                    raw_status=raw_status,
                    rejection_reason="Worker is parked/completed, not actively executing.",
                ))
                continue

            # Check for running: the ONLY valid active status
            if raw_status in VALID_ACTIVE_STATUSES:
                topic = self.map_worker_to_topic(w_id, entry.get("topic"))
                task_id = entry.get("task_id")
                is_auth = True

                # Check task authorization
                if authorized_task_ids is not None and task_id:
                    if task_id not in authorized_task_ids:
                        is_auth = False
                        violations.append(CoverageViolation(
                            kind="UNAUTHORIZED_ACTIVE_WORKER",
                            message=f"Active worker '{w_id}' is executing unauthorized task '{task_id}'.",
                            details={"worker_id": w_id, "task_id": task_id, "topic": topic},
                        ))

                # Check for prohibited targets (e.g. production)
                target = str(entry.get("target", "")).lower()
                if any(p in target for p in ("prod", "production", "zaraprptkegxqpvnsubu", "akamai-iad-prod")):
                    is_auth = False
                    violations.append(CoverageViolation(
                        kind="UNAUTHORIZED_ACTIVE_WORKER",
                        message=f"Active worker '{w_id}' is targeting forbidden production environment '{target}'.",
                        details={"worker_id": w_id, "target": target},
                    ))

                active_workers.append(NativeWorker(
                    id=w_id,
                    status="running",
                    is_active=True,
                    assigned_topic=topic,
                    assigned_task_id=task_id,
                    is_authorized=is_auth,
                    role=role,
                    raw_status=raw_status,
                ))
            else:
                # Any unclassified status is rejected as fake/unverified
                violations.append(CoverageViolation(
                    kind="FAKE_WORKER_REJECTED",
                    message=f"Worker '{w_id}' has unverified status '{raw_status}'. Refused as active native worker.",
                    details={"worker_id": w_id, "status": raw_status},
                ))

        return active_workers, idle_workers, parked_workers, violations

    def evaluate_topic_coverage(
        self,
        inventory: Sequence[InventoryItem],
        roster: Sequence[Dict[str, Any]],
        ram_used_pct: Optional[float] = None,
        authorized_task_ids: Optional[Set[str]] = None,
        baseline_inventory: Optional[Sequence[InventoryItem]] = None,
    ) -> GuardReport:
        """
        Evaluate all coverage and inventory invariants.
        Produces actionable next-ready assignments for Main.
        Consumes actual live native status, not static assigned owner text.
        Requires actively RUNNING useful workers for runnable topic coverage.
        """
        violations: List[CoverageViolation] = []

        # 1. Reconcile against baseline inventory if provided
        if baseline_inventory is not None:
            baseline_keys = {canonical_item_key(it.content, it.phase): it for it in baseline_inventory}
            inv_keys = {canonical_item_key(it.content, it.phase): it for it in inventory}
            for k, b_item in baseline_keys.items():
                if k not in inv_keys:
                    violations.append(CoverageViolation(
                        kind="DROPPED_TASK",
                        message=f"Task missing/dropped from inventory: [{b_item.id}] '{b_item.content}' in phase '{b_item.phase}'.",
                        details={"item_id": b_item.id, "content": b_item.content, "phase": b_item.phase},
                    ))

        # 2. Classify live native roster: ONLY "running" useful subagents count
        active_workers, idle_workers, parked_workers, roster_violations = self.classify_roster(
            roster, authorized_task_ids=authorized_task_ids
        )
        violations.extend(roster_violations)

        # Useful running workers exclude orchestrator (role="main")
        useful_running_workers = [w for w in active_workers if w.is_useful_worker]

        # 3. Group inventory tasks by topic/phase
        topic_tasks: Dict[str, List[InventoryItem]] = {}
        for item in inventory:
            topic_tasks.setdefault(item.phase, []).append(item)

        # Map running useful workers to topics
        running_topic_coverage: Dict[str, List[NativeWorker]] = {}
        for w in useful_running_workers:
            if w.assigned_topic:
                running_topic_coverage.setdefault(w.assigned_topic, []).append(w)

        total_tasks = len(inventory)
        completed_tasks = sum(1 for it in inventory if it.is_completed)
        cancelled_tasks = sum(1 for it in inventory if it.is_cancelled)
        blocked_tasks = sum(1 for it in inventory if it.is_blocked and not it.is_completed and not it.is_cancelled)
        runnable_tasks = sum(1 for it in inventory if it.is_runnable)

        covered_topics: List[str] = []
        uncovered_topics: List[str] = []
        blocked_topics: List[Dict[str, Any]] = []
        next_assignments: List[NextAssignment] = []

        # 4. Check each topic's status and running active coverage
        for topic_name, items in topic_tasks.items():
            topic_runnable_items = [it for it in items if it.is_runnable]
            topic_blocked_items = [it for it in items if it.is_blocked and not it.is_completed and not it.is_cancelled]
            topic_completed_items = [it for it in items if it.is_completed]
            topic_cancelled_items = [it for it in items if it.is_cancelled]

            # Check for unlinked issues or absent owners on runnable items
            for it in topic_runnable_items:
                if not it.issue_url:
                    if not self.topic_issues.get(topic_name):
                        violations.append(CoverageViolation(
                            kind="UNLINKED_TOPIC_ISSUE",
                            message=f"Runnable task [{it.id}] '{it.content}' in topic '{topic_name}' has no linked GitHub issue.",
                            details={"task_id": it.id, "topic": topic_name},
                        ))
                if not it.owner:
                    violations.append(CoverageViolation(
                        kind="ABSENT_OWNER",
                        message=f"Runnable task [{it.id}] '{it.content}' in topic '{topic_name}' has no assigned owner.",
                        details={"task_id": it.id, "topic": topic_name},
                    ))

            # If all unfinished items are explicitly blocked
            if not topic_runnable_items and topic_blocked_items:
                reasons = list(set(it.blocker_reason for it in topic_blocked_items if it.blocker_reason))
                blocked_topics.append({
                    "topic": topic_name,
                    "blocked_count": len(topic_blocked_items),
                    "reasons": reasons,
                })
                continue

            # If all items are completed or cancelled
            if not topic_runnable_items and not topic_blocked_items:
                continue

            # Topic is runnable: requires an actively RUNNING native worker
            if topic_runnable_items:
                running_w = running_topic_coverage.get(topic_name, [])
                if running_w:
                    covered_topics.append(topic_name)
                else:
                    uncovered_topics.append(topic_name)
                    violations.append(CoverageViolation(
                        kind="UNCOVERED_RUNNABLE_TOPIC",
                        message=f"Runnable topic '{topic_name}' has 0 actively running native workers ({len(topic_runnable_items)} unfinished tasks).",
                        details={
                            "topic": topic_name,
                            "runnable_task_count": len(topic_runnable_items),
                            "first_task": topic_runnable_items[0].content,
                        },
                    ))
                    # Produce primary next-ready assignment (priority 1)
                    target_task = topic_runnable_items[0]
                    next_assignments.append(NextAssignment(
                        topic=topic_name,
                        task_id=target_task.id,
                        content=target_task.content,
                        recommended_role="fast",
                        issue_url=target_task.issue_url or self.topic_issues.get(topic_name),
                        criteria=[f"Complete and verify: {target_task.content}"],
                        priority=1,
                    ))

        # 5. Worker floor evaluation (based ONLY on running useful workers)
        runnable_topic_count = len(covered_topics) + len(uncovered_topics)
        effective_active_count = len(useful_running_workers)
        floor_satisfied = True
        capacity_exc = None

        if runnable_topic_count >= self.min_worker_floor:
            required_floor = self.min_worker_floor
        else:
            required_floor = max(1, runnable_topic_count)

        if effective_active_count < required_floor:
            # Check RAM ceiling exception
            if ram_used_pct is not None and ram_used_pct >= self.ram_ceiling_pct:
                capacity_exc = (
                    f"Measured RAM usage at {ram_used_pct:.1f}% meets/exceeds {self.ram_ceiling_pct:.1f}% "
                    f"safety ceiling; worker floor deficit excused without deleting work."
                )
                floor_satisfied = True
            else:
                floor_satisfied = False
                violations.append(CoverageViolation(
                    kind="WORKER_FLOOR_DEFICIT",
                    message=(
                        f"Actively running useful worker count {effective_active_count} is below required floor {required_floor} "
                        f"while {runnable_topic_count} ready independent topics exist."
                    ),
                    details={
                        "running_active_count": effective_active_count,
                        "idle_workers_count": len(idle_workers),
                        "parked_workers_count": len(parked_workers),
                        "required_floor": required_floor,
                        "runnable_topic_count": runnable_topic_count,
                    },
                ))

        # 6. If extra slots available, generate secondary assignments (priority 2)
        if len(next_assignments) < self.min_worker_floor:
            for topic_name in covered_topics:
                items = [it for it in topic_tasks.get(topic_name, []) if it.is_runnable]
                if len(items) > 1:
                    extra_task = items[1]
                    next_assignments.append(NextAssignment(
                        topic=topic_name,
                        task_id=extra_task.id,
                        content=extra_task.content,
                        recommended_role="fast",
                        issue_url=extra_task.issue_url or self.topic_issues.get(topic_name),
                        criteria=[f"Complete and verify: {extra_task.content}"],
                        priority=2,
                    ))
                if len(next_assignments) >= self.min_worker_floor:
                    break

        ok = len(violations) == 0

        summary = (
            f"Inventory: {total_tasks} total ({completed_tasks} completed, {cancelled_tasks} cancelled, "
            f"{runnable_tasks} runnable, {blocked_tasks} blocked). "
            f"Actively running workers: {effective_active_count}/{required_floor} "
            f"(idle: {len(idle_workers)}, parked: {len(parked_workers)}; floor {'satisfied' if floor_satisfied else 'DEFICIT'}). "
            f"Topics: {len(covered_topics)} covered, {len(uncovered_topics)} uncovered, {len(blocked_topics)} blocked. "
            f"Violations: {len(violations)}."
        )

        return GuardReport(
            ok=ok,
            timestamp=_now(),
            total_tasks=total_tasks,
            completed_tasks=completed_tasks,
            cancelled_tasks=cancelled_tasks,
            runnable_tasks=runnable_tasks,
            blocked_tasks=blocked_tasks,
            active_workers_count=effective_active_count,
            idle_workers_count=len(idle_workers),
            parked_workers_count=len(parked_workers),
            fake_workers_rejected_count=sum(1 for v in violations if v.kind == "FAKE_WORKER_REJECTED"),
            required_floor=required_floor,
            floor_satisfied=floor_satisfied,
            capacity_exception=capacity_exc,
            violations=violations,
            next_assignments=next_assignments,
            uncovered_topics=uncovered_topics,
            covered_topics=covered_topics,
            blocked_topics=blocked_topics,
            reconciled_inventory_count=total_tasks,
            summary=summary,
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Deterministic topic inventory coverage guard."
    )
    p.add_argument("--check", action="store_true", help="Run full coverage and inventory check")
    p.add_argument("--sources", nargs="*", help="Baseline source JSON file paths")
    p.add_argument("--roster-json", help="Path to native roster JSON file")
    p.add_argument("--ram-pct", type=float, help="Current measured RAM used percentage")
    p.add_argument("--json", action="store_true", help="Emit structured JSON output")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    guard = TopicInventoryGuard(baseline_sources=args.sources)

    roster = []
    if args.roster_json and os.path.exists(args.roster_json):
        with open(args.roster_json, "r", encoding="utf-8") as f:
            roster = json.load(f)

    # Reconcile baseline sources
    reconciled, _ = guard.reconcile_sources_additively()
    report = guard.evaluate_topic_coverage(
        inventory=reconciled,
        roster=roster,
        ram_used_pct=args.ram_pct,
    )

    if args.json:
        print(report.to_json())
    else:
        print(f"=== Topic Inventory Coverage Report ===")
        print(f"Status: {'PASS' if report.ok else 'FAIL'}")
        print(report.summary)
        if report.violations:
            print("\nViolations:")
            for v in report.violations:
                print(f"  - [{v.kind}] {v.message}")
        if report.next_assignments:
            print(f"\nActionable Next-Ready Assignments ({len(report.next_assignments)}):")
            for a in report.next_assignments:
                print(f"  - [P{a.priority}] [{a.topic}] {a.content} (Role: {a.recommended_role})")

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
