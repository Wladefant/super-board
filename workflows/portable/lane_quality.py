#!/usr/bin/env python3
"""Lane quality telemetry: mine session files and GitHub PR data to compare
model quality over time.

Usage:
    python lane_quality.py report --days 7
    python lane_quality.py report --days 7 --post https://github.com/Wladefant/super-board/issues/NNN
    python lane_quality.py baseline --days 14
    python lane_quality.py baseline --days 14 --post https://github.com/Wladefant/super-board/issues/NNN
    python lane_quality.py snapshot              # save daily cache
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SESSIONS_DIR = Path.home() / ".veyyon/profiles/default/agent/sessions"
CACHE_DIR = Path.home() / ".veyyon/run/lane-quality"

TRACKED_REPOS = (
    "Bavariance/polysimulator",
    "Wladefant/super-board",
)

# Degradation thresholds
THRESHOLD_APPROVAL_DROP_PP = 15       # percentage-point drop in first-pass approval
THRESHOLD_OPUS_SHARE_RISE_PP = 10     # percentage-point rise in Opus/Codex share
THRESHOLD_REVIEW_ROUNDS_RISE = 0.5    # additional review rounds per PR
THRESHOLD_DEFECT_RATE_RISE_PP = 10    # percentage-point rise in post-merge defect rate

# Orchestrator model family patterns (for Opus/Codex share tracking)
ORCHESTRATOR_FAMILIES = {
    "anthropic/claude-fable",
    "anthropic/claude-opus",
}
PREMIUM_MODEL_FAMILIES = {
    "anthropic/claude-opus",
    "anthropic/claude-fable",
    "openai-codex/gpt-5.6-sol",
    "openai-codex/gpt-6-astra",
}


# ---------------------------------------------------------------------------
# Session JSONL parser
# ---------------------------------------------------------------------------

@dataclass
class LaneRecord:
    """Parsed metrics for one agent/lane session."""
    name: str
    session_id: str = ""
    parent_session_dir: str = ""
    parent_session_id: str = ""
    project: str = ""  # e.g. "polysimulator" from session dir name
    start_time: str = ""
    end_time: str = ""
    task_description: str = ""

    # Model tracking: model_id -> request count
    models_used: Dict[str, int] = field(default_factory=dict)
    primary_model: str = ""  # most-used model

    # Token accounting
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    # Cost by provider
    cost_by_provider: Dict[str, float] = field(default_factory=dict)
    total_cost: float = 0.0

    request_count: int = 0
    duration_ms: float = 0.0

    # Outcome
    outcome: str = "unknown"  # completed, failed, escalated, dispose, unknown
    stop_reasons: Dict[str, int] = field(default_factory=dict)

    # PR URLs touched
    pr_urls: List[str] = field(default_factory=list)

    # Nesting depth (0 = main session, 1 = direct child, etc.)
    nesting_depth: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _classify_outcome(exit_reason: str, stop_reasons: Dict[str, int],
                       request_count: int) -> str:
    """Map session exit reason to a normalized outcome."""
    if exit_reason == "dispose":
        # Disposed means the parent cleaned up; could be normal completion
        if stop_reasons.get("endTurn", 0) > 0 or request_count > 0:
            return "completed"
        return "disposed"
    if exit_reason in ("completed", "normal"):
        return "completed"
    if exit_reason in ("error", "crash"):
        return "failed"
    if exit_reason in ("escalated",):
        return "escalated"
    if request_count > 0:
        return "completed"
    return "unknown"


def _normalize_model_id(provider: str, model: str) -> str:
    """Build a canonical model identifier from provider and model name."""
    if not provider and not model:
        return "unknown/unknown"
    model_str = str(model).lower()
    prov_str = str(provider).lower()
    if "space-bunny" in model_str or "space-bunny" in prov_str:
        return "opencode-go/space-bunny-free"
    if not provider or not model:
        return "unknown/unknown"
    return f"{provider}/{model}"


def parse_session_file(filepath: Path, nesting_depth: int = 0) -> LaneRecord:
    """Parse a single session JSONL file into a LaneRecord."""
    rec = LaneRecord(
        name=filepath.stem,
        nesting_depth=nesting_depth,
    )
    models_counter: Counter = Counter()
    cost_counter: Counter = Counter()
    stop_counter: Counter = Counter()
    pr_urls: Set[str] = set()
    exit_reason = ""

    with open(filepath, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            line_type = obj.get("type", "")
            ts = obj.get("timestamp", "")

            if line_type == "session":
                rec.session_id = obj.get("id", "")
                rec.start_time = ts
                cwd = obj.get("cwd", "")
                # Derive project from cwd
                if cwd:
                    rec.project = Path(cwd).name

            elif line_type == "session_init":
                task = obj.get("task", "") or ""
                rec.task_description = task[:500]

            elif line_type == "model_change":
                model = obj.get("model", "")
                if model and not rec.primary_model:
                    rec.primary_model = model

            elif line_type == "message":
                msg = obj.get("message", {})
                if msg.get("role") != "assistant":
                    continue

                provider = msg.get("provider", "unknown")
                model = msg.get("model", "unknown")
                full_model = _normalize_model_id(provider, model)
                usage = msg.get("usage", {})
                cost = usage.get("cost", {})

                models_counter[full_model] += 1
                rec.request_count += 1
                rec.input_tokens += usage.get("input", 0) or 0
                rec.output_tokens += usage.get("output", 0) or 0
                rec.cache_read_tokens += usage.get("cacheRead", 0) or 0
                rec.cache_write_tokens += usage.get("cacheWrite", 0) or 0
                total_cost = cost.get("total", 0) or 0
                rec.total_cost += total_cost
                cost_counter[provider] += total_cost
                stop_counter[msg.get("stopReason", "unknown")] += 1
                rec.duration_ms += msg.get("duration", 0) or 0
                rec.end_time = ts or rec.end_time

                # Extract PR URLs from content
                content = str(msg.get("content", ""))
                urls = re.findall(
                    r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+',
                    content)
                pr_urls.update(urls)

            elif line_type == "custom":
                ct = obj.get("customType", "")
                if ct == "session_exit":
                    data = obj.get("data", {})
                    exit_reason = data.get("reason", "")
                elif ct == "tool_execution_start":
                    data = obj.get("data", {})
                    tool = data.get("toolName", "")
                    # Check github tool for PR URLs
                    args = data.get("args", {})
                    if tool == "github" and isinstance(args, dict):
                        pr_val = args.get("pr", "")
                        if pr_val and "github.com" in str(pr_val):
                            pr_urls.add(str(pr_val))

    rec.models_used = dict(models_counter)
    rec.cost_by_provider = dict(cost_counter)
    rec.stop_reasons = dict(stop_counter)
    rec.pr_urls = sorted(pr_urls)
    rec.outcome = _classify_outcome(exit_reason, rec.stop_reasons, rec.request_count)

    # Set primary model as most-used if not set from model_change
    if not rec.primary_model and models_counter:
        rec.primary_model = models_counter.most_common(1)[0][0]

    return rec


def collect_all_lanes(days: int = 14) -> List[LaneRecord]:
    """Scan all session directories and return LaneRecords for the last N days."""
    cutoff = datetime.now() - timedelta(days=days)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    records: List[LaneRecord] = []

    if not SESSIONS_DIR.is_dir():
        return records

    for project_dir in SESSIONS_DIR.iterdir():
        if not project_dir.is_dir():
            continue

        for item in project_dir.iterdir():
            if item.is_dir():
                # This is a session-timestamp directory with spawned agent files
                parent_name = item.name
                for agent_file in item.glob("*.jsonl"):
                    try:
                        mtime = datetime.fromtimestamp(agent_file.stat().st_mtime)
                    except OSError:
                        continue
                    if mtime < cutoff:
                        continue
                    rec = parse_session_file(agent_file, nesting_depth=1)
                    # Filter by actual start_time, not just file mtime
                    if rec.start_time and rec.start_time < cutoff_iso:
                        continue
                    rec.parent_session_dir = parent_name
                    # Extract parent session ID from dir name
                    parts = parent_name.split("_")
                    if len(parts) >= 2:
                        rec.parent_session_id = parts[-1]
                    records.append(rec)

            elif item.suffix == ".jsonl":
                # Main session file
                try:
                    mtime = datetime.fromtimestamp(item.stat().st_mtime)
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                rec = parse_session_file(item, nesting_depth=0)
                # Filter by actual start_time, not just file mtime
                if rec.start_time and rec.start_time < cutoff_iso:
                    continue
                records.append(rec)

    return records


# ---------------------------------------------------------------------------
# GitHub PR data joiner
# ---------------------------------------------------------------------------

@dataclass
class PRRecord:
    """GitHub PR metadata for quality analysis."""
    url: str = ""
    number: int = 0
    repo: str = ""
    state: str = ""
    merged: bool = False
    merged_at: str = ""
    created_at: str = ""
    additions: int = 0
    deletions: int = 0
    review_count: int = 0
    request_changes_count: int = 0
    approve_count: int = 0
    review_rounds: int = 0
    reviewer_models: List[str] = field(default_factory=list)
    ci_failures_first_push: int = 0
    follow_up_prs: List[str] = field(default_factory=list)
    reverted: bool = False
    reopened_issue_urls: List[str] = field(default_factory=list)
    implementing_model: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


PR_CACHE_FILE = CACHE_DIR / "pr_cache.json"


def _parse_review_verdict(review: Dict[str, Any]) -> str:
    """Parse review verdict from GitHub review object, checking state and body."""
    state = (review.get("state") or "").upper()
    if state == "CHANGES_REQUESTED":
        return "REQUEST-CHANGES"
    if state == "APPROVED":
        return "APPROVE"
    body = (review.get("body") or "").strip()
    first_line = ""
    for line in body.splitlines():
        line = line.strip()
        if line:
            first_line = line
            break
    if first_line.startswith("verdict:"):
        first_line = first_line[len("verdict:"):].strip()
    upper = first_line.upper()
    if "REQUEST-CHANGES" in upper or "REQUEST_CHANGES" in upper or upper == "REJECT":
        return "REQUEST-CHANGES"
    if any(k in upper for k in ("APPROVE", "APPROVED", "SAFE_AS_IS", "LGTM")):
        return "APPROVE"
    return state


def _load_pr_cache() -> Dict[str, Dict[str, Any]]:
    """Load cached PR records from disk."""
    if not PR_CACHE_FILE.is_file():
        return {}
    try:
        data = json.loads(PR_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_pr_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    """Save PR records cache to disk."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        PR_CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception:
        pass


def _pr_node_to_record(node: Dict[str, Any], repo: str) -> PRRecord:
    """Convert a GitHub GraphQL PR node to a PRRecord."""
    rec = PRRecord(
        url=node.get("url", ""),
        number=node.get("number", 0),
        repo=repo,
        state=node.get("state", ""),
        merged=bool(node.get("merged") or node.get("mergedAt")),
        merged_at=node.get("mergedAt", "") or "",
        created_at=node.get("createdAt", "") or "",
        additions=node.get("additions", 0) or 0,
        deletions=node.get("deletions", 0) or 0,
    )

    # Reviews
    rev_nodes = node.get("reviews", {}).get("nodes", []) if isinstance(node.get("reviews"), dict) else []
    submission_times = set()
    for review in rev_nodes:
        rec.review_count += 1
        verdict = _parse_review_verdict(review)
        if verdict == "REQUEST-CHANGES":
            rec.request_changes_count += 1
        elif verdict == "APPROVE":
            rec.approve_count += 1
        submitted = review.get("submittedAt", "")
        if submitted:
            submission_times.add(submitted[:16])

    rec.review_rounds = len(submission_times)

    # CI status on first commit
    commits_nodes = node.get("commits", {}).get("nodes", []) if isinstance(node.get("commits"), dict) else []
    if commits_nodes:
        first_commit = commits_nodes[0].get("commit", {})
        rollup = first_commit.get("statusCheckRollup")
        if rollup and isinstance(rollup, dict):
            if rollup.get("state") in ("FAILURE", "ERROR"):
                rec.ci_failures_first_push = 1

    return rec


def _fetch_repo_prs_graphql(
    owner: str,
    name: str,
    days: int = 14,
    timeout: int = 30,
) -> List[Dict[str, Any]]:
    """Fetch recent PRs for a repository in batched GraphQL calls."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days + 7)
    cutoff_iso = cutoff.strftime("%Y-%m-%d")
    all_nodes: List[Dict[str, Any]] = []
    cursor = None
    max_pages = 8

    gql = """
    query($owner: String!, $name: String!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        pullRequests(first: 50, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
          pageInfo { hasNextPage endCursor }
          nodes {
            number title url state merged mergedAt createdAt additions deletions
            commits(first: 1) { nodes { commit { oid statusCheckRollup { state } } } }
            reviews(first: 25) { nodes { state submittedAt body } }
          }
        }
      }
    }
    """

    for _ in range(max_pages):
        cmd = ["gh", "api", "graphql", "-F", f"owner={owner}", "-F", f"name={name}"]
        if cursor:
            cmd.extend(["-F", f"cursor={cursor}"])
        cmd.extend(["-F", f"query={gql}"])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if res.returncode != 0 or not res.stdout:
                break
            data = json.loads(res.stdout)
            pr_data = data.get("data", {}).get("repository", {}).get("pullRequests", {})
            nodes = pr_data.get("nodes", [])
            if not nodes:
                break
            all_nodes.extend(nodes)
            oldest = nodes[-1].get("createdAt", "")
            if oldest and oldest[:10] < cutoff_iso:
                break
            if not pr_data.get("pageInfo", {}).get("hasNextPage"):
                break
            cursor = pr_data.get("pageInfo", {}).get("endCursor")
            if not cursor:
                break
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, OSError):
            break

    return all_nodes


def _fetch_repo_prs_fallback(
    repo: str,
    days: int = 14,
    timeout: int = 30,
) -> List[Dict[str, Any]]:
    """Fallback using gh pr list --json if GraphQL is unavailable."""
    cmd = [
        "gh", "pr", "list", "--repo", repo, "--state", "all",
        "--limit", "150",
        "--json", "number,title,url,state,mergedAt,createdAt,additions,deletions,reviews"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            for p in data:
                p["merged"] = (p.get("state") == "MERGED" or bool(p.get("mergedAt")))
                revs = p.get("reviews", [])
                p["reviews"] = {"nodes": revs} if isinstance(revs, list) else {"nodes": []}
            return data
    except Exception:
        pass
    return []


def _detect_follow_ups_and_reverts(
    records: Dict[str, PRRecord],
    all_nodes_by_repo: Dict[str, List[Dict[str, Any]]],
) -> None:
    """Detect follow-up fix PRs and reverts within 7 days of merge in-memory."""
    for url, rec in records.items():
        if not rec.merged or not rec.merged_at:
            continue
        try:
            merged_dt = datetime.fromisoformat(rec.merged_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        window_end = merged_dt + timedelta(days=7)

        repo_nodes = all_nodes_by_repo.get(rec.repo, [])
        for other in repo_nodes:
            if other.get("number") == rec.number:
                continue
            other_created_str = other.get("createdAt", "")
            if not other_created_str:
                continue
            try:
                other_created = datetime.fromisoformat(other_created_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if merged_dt <= other_created <= window_end:
                other_title = (other.get("title") or "").lower()
                other_url = other.get("url", "")
                if f"#{rec.number}" in other_title or f"pull/{rec.number}" in other_title:
                    if other_url not in rec.follow_up_prs:
                        rec.follow_up_prs.append(other_url)
                    if "revert" in other_title:
                        rec.reverted = True
                elif "revert" in other_title and str(rec.number) in other_title:
                    if other_url not in rec.follow_up_prs:
                        rec.follow_up_prs.append(other_url)
                    rec.reverted = True


def fetch_pr_data(
    pr_urls: Set[str],
    days: int = 14,
    use_cache: bool = True,
    timeout_per_repo: int = 35,
) -> Dict[str, PRRecord]:
    """Fetch PR metadata from GitHub for the given PR URLs in batched queries."""
    tracked_urls = {
        u for u in pr_urls
        if any(f"github.com/{r}/pull/" in u for r in TRACKED_REPOS)
    }
    if not tracked_urls:
        return {}

    cache = _load_pr_cache() if use_cache else {}
    records: Dict[str, PRRecord] = {}

    repos_to_fetch: Set[str] = set()
    for url in tracked_urls:
        if url in cache:
            cached_item = cache[url]
            rec = PRRecord(**{k: v for k, v in cached_item.items() if k in PRRecord.__dataclass_fields__})
            records[url] = rec
            if not rec.merged and rec.state not in ("MERGED", "CLOSED"):
                repos_to_fetch.add(rec.repo)
        else:
            for r in TRACKED_REPOS:
                if f"github.com/{r}/pull/" in url:
                    repos_to_fetch.add(r)

    if repos_to_fetch:
        all_nodes_by_repo: Dict[str, List[Dict[str, Any]]] = {}
        for repo in sorted(repos_to_fetch):
            owner, name = repo.split("/")
            nodes = _fetch_repo_prs_graphql(owner, name, days=days, timeout=timeout_per_repo)
            if not nodes:
                nodes = _fetch_repo_prs_fallback(repo, days=days, timeout=timeout_per_repo)
            all_nodes_by_repo[repo] = nodes

            for node in nodes:
                url = node.get("url", "")
                if url:
                    pr_rec = _pr_node_to_record(node, repo)
                    records[url] = pr_rec
                    cache[url] = pr_rec.to_dict()

        _detect_follow_ups_and_reverts(records, all_nodes_by_repo)
        _save_pr_cache(cache)

    return {u: records[u] for u in tracked_urls if u in records}


# ---------------------------------------------------------------------------
# Metrics aggregation
# ---------------------------------------------------------------------------

@dataclass
class ModelMetrics:
    """Aggregated metrics for one model."""
    model_id: str = ""
    lane_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    escalated_count: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    total_duration_ms: float = 0.0
    prs_touched: int = 0
    prs_merged: int = 0
    reviews_received: int = 0
    request_changes_received: int = 0
    first_pass_approvals: int = 0
    review_rounds_total: int = 0
    post_merge_defects: int = 0  # follow-up fix PRs or reverts
    ci_failures: int = 0
    cost_per_merged_pr: float = 0.0
    first_pass_approval_rate: float = 0.0
    escalation_rate: float = 0.0
    reviews_per_merged_pr: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["cost_per_merged_pr"] = round(self.cost_per_merged_pr, 4)
        d["first_pass_approval_rate"] = round(self.first_pass_approval_rate, 2)
        d["escalation_rate"] = round(self.escalation_rate, 2)
        d["reviews_per_merged_pr"] = round(self.reviews_per_merged_pr, 2)
        return d


@dataclass
class DailySnapshot:
    """One day's aggregated metrics."""
    date: str = ""
    total_lanes: int = 0
    total_cost: float = 0.0
    total_tokens: int = 0
    model_metrics: Dict[str, Dict] = field(default_factory=dict)
    opus_codex_token_share: float = 0.0
    opus_codex_orchestrator_share: float = 0.0
    opus_codex_worker_share: float = 0.0
    flags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _model_family(model_id: str) -> str:
    """Extract family prefix from model ID (provider/model-name without version)."""
    return model_id.rsplit(":", 1)[0] if ":" in model_id else model_id


def _is_premium(model_id: str) -> bool:
    """Check if model is in the premium (Opus/Codex) family."""
    family = _model_family(model_id)
    for prefix in PREMIUM_MODEL_FAMILIES:
        if family.startswith(prefix):
            return True
    return False


def _is_orchestrator(model_id: str) -> bool:
    """Check if model is an orchestrator model."""
    family = _model_family(model_id)
    for prefix in ORCHESTRATOR_FAMILIES:
        if family.startswith(prefix):
            return True
    return False


def aggregate_by_model(
    lanes: List[LaneRecord],
    pr_records: Dict[str, PRRecord],
) -> Dict[str, ModelMetrics]:
    """Aggregate lane records by primary model."""
    metrics: Dict[str, ModelMetrics] = defaultdict(lambda: ModelMetrics())

    for lane in lanes:
        model = lane.primary_model or "unknown/unknown"
        m = metrics[model]
        m.model_id = model
        m.lane_count += 1
        m.total_tokens += lane.input_tokens + lane.output_tokens
        m.total_cost += lane.total_cost
        m.total_duration_ms += lane.duration_ms

        if lane.outcome == "completed":
            m.completed_count += 1
        elif lane.outcome == "failed":
            m.failed_count += 1
        elif lane.outcome == "escalated":
            m.escalated_count += 1

    # Map PRs to models and avoid double counting
    model_prs: Dict[str, Set[str]] = defaultdict(set)
    for lane in lanes:
        model = lane.primary_model or "unknown/unknown"
        for url in lane.pr_urls:
            model_prs[model].add(url)

    for model, pr_urls_set in model_prs.items():
        m = metrics[model]
        for url in sorted(pr_urls_set):
            if url in pr_records:
                pr = pr_records[url]
                m.prs_touched += 1
                if pr.merged:
                    m.prs_merged += 1
                m.reviews_received += pr.review_count
                m.request_changes_received += pr.request_changes_count
                m.review_rounds_total += pr.review_rounds
                m.ci_failures += pr.ci_failures_first_push
                if pr.review_count > 0 and pr.request_changes_count == 0:
                    m.first_pass_approvals += 1
                m.post_merge_defects += len(pr.follow_up_prs)
                if pr.reverted:
                    m.post_merge_defects += 1

    # Compute derived metrics
    for model, m in metrics.items():
        if m.prs_merged > 0:
            m.cost_per_merged_pr = m.total_cost / m.prs_merged
            m.reviews_per_merged_pr = m.reviews_received / m.prs_merged
        elif m.prs_touched > 0:
            m.reviews_per_merged_pr = m.reviews_received / m.prs_touched
        if m.prs_touched > 0:
            m.first_pass_approval_rate = (m.first_pass_approvals / m.prs_touched) * 100
        if m.lane_count > 0:
            m.escalation_rate = (m.escalated_count / m.lane_count) * 100

    return dict(metrics)


def compute_daily_snapshots(
    lanes: List[LaneRecord],
    pr_records: Dict[str, PRRecord],
) -> List[DailySnapshot]:
    """Group lanes by date and compute daily snapshots."""
    by_date: Dict[str, List[LaneRecord]] = defaultdict(list)

    for lane in lanes:
        if lane.start_time:
            date_str = lane.start_time[:10]
        elif lane.end_time:
            date_str = lane.end_time[:10]
        else:
            continue
        by_date[date_str].append(lane)

    snapshots = []
    for date_str in sorted(by_date.keys()):
        day_lanes = by_date[date_str]
        day_pr_urls = set()
        for lane in day_lanes:
            day_pr_urls.update(lane.pr_urls)
        day_prs = {url: pr_records[url] for url in day_pr_urls if url in pr_records}

        model_metrics = aggregate_by_model(day_lanes, day_prs)
        total_tokens = sum(
            lane.input_tokens + lane.output_tokens for lane in day_lanes
        )
        premium_tokens = sum(
            lane.input_tokens + lane.output_tokens
            for lane in day_lanes if _is_premium(lane.primary_model)
        )
        orchestrator_tokens = sum(
            lane.input_tokens + lane.output_tokens
            for lane in day_lanes
            if lane.nesting_depth == 0 and _is_orchestrator(lane.primary_model)
        )
        worker_premium_tokens = sum(
            lane.input_tokens + lane.output_tokens
            for lane in day_lanes
            if lane.nesting_depth > 0 and _is_premium(lane.primary_model)
        )

        snap = DailySnapshot(
            date=date_str,
            total_lanes=len(day_lanes),
            total_cost=sum(lane.total_cost for lane in day_lanes),
            total_tokens=total_tokens,
            model_metrics={k: v.to_dict() for k, v in model_metrics.items()},
            opus_codex_token_share=(premium_tokens / total_tokens * 100) if total_tokens else 0,
            opus_codex_orchestrator_share=(orchestrator_tokens / total_tokens * 100) if total_tokens else 0,
            opus_codex_worker_share=(worker_premium_tokens / total_tokens * 100) if total_tokens else 0,
        )
        snapshots.append(snap)

    return snapshots


# ---------------------------------------------------------------------------
# Baseline & comparison
# ---------------------------------------------------------------------------

@dataclass
class Baseline:
    """Reference metrics for degradation detection."""
    computed_at: str = ""
    days: int = 14
    total_lanes: int = 0
    total_cost: float = 0.0
    opus_codex_token_share: float = 0.0
    model_first_pass_approval: Dict[str, float] = field(default_factory=dict)
    model_review_rounds_avg: Dict[str, float] = field(default_factory=dict)
    model_defect_rate: Dict[str, float] = field(default_factory=dict)
    model_escalation_rate: Dict[str, float] = field(default_factory=dict)
    model_cost_per_merged_pr: Dict[str, float] = field(default_factory=dict)
    model_reviews_per_merged_pr: Dict[str, float] = field(default_factory=dict)
    overall_first_pass_approval: float = 0.0
    overall_review_rounds_avg: float = 0.0
    overall_defect_rate: float = 0.0
    overall_reviews_per_merged_pr: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "Baseline":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def compute_baseline(
    lanes: List[LaneRecord],
    pr_records: Dict[str, PRRecord],
    days: int = 14,
) -> Baseline:
    """Compute baseline reference metrics from the given lanes."""
    model_metrics = aggregate_by_model(lanes, pr_records)

    total_tokens = sum(l.input_tokens + l.output_tokens for l in lanes)
    premium_tokens = sum(
        l.input_tokens + l.output_tokens for l in lanes if _is_premium(l.primary_model)
    )

    baseline = Baseline(
        computed_at=datetime.now(timezone.utc).isoformat(),
        days=days,
        total_lanes=len(lanes),
        total_cost=sum(l.total_cost for l in lanes),
        opus_codex_token_share=(premium_tokens / total_tokens * 100) if total_tokens else 0,
    )

    total_prs_touched = 0
    total_first_pass = 0
    total_review_rounds = 0
    total_reviews_received = 0
    total_prs_merged = 0
    total_defects = 0

    for model, m in model_metrics.items():
        baseline.model_first_pass_approval[model] = m.first_pass_approval_rate
        if m.prs_touched > 0:
            baseline.model_review_rounds_avg[model] = m.review_rounds_total / m.prs_touched
        baseline.model_defect_rate[model] = (
            (m.post_merge_defects / m.prs_merged * 100) if m.prs_merged else 0
        )
        baseline.model_escalation_rate[model] = m.escalation_rate
        baseline.model_cost_per_merged_pr[model] = m.cost_per_merged_pr
        baseline.model_reviews_per_merged_pr[model] = m.reviews_per_merged_pr

        total_prs_touched += m.prs_touched
        total_first_pass += m.first_pass_approvals
        total_review_rounds += m.review_rounds_total
        total_reviews_received += m.reviews_received
        total_prs_merged += m.prs_merged
        total_defects += m.post_merge_defects

    if total_prs_touched > 0:
        baseline.overall_first_pass_approval = (total_first_pass / total_prs_touched) * 100
        baseline.overall_review_rounds_avg = total_review_rounds / total_prs_touched
    if total_prs_merged > 0:
        baseline.overall_defect_rate = (total_defects / total_prs_merged) * 100
        baseline.overall_reviews_per_merged_pr = total_reviews_received / total_prs_merged

    return baseline


def detect_degradation(
    current: Dict[str, ModelMetrics],
    baseline: Baseline,
    current_opus_share: float,
) -> List[str]:
    """Compare current metrics against baseline and return flags."""
    flags = []

    # Overall first-pass approval drop
    total_touched = sum(m.prs_touched for m in current.values())
    total_first = sum(m.first_pass_approvals for m in current.values())
    if total_touched > 0:
        current_approval = (total_first / total_touched) * 100
        drop = baseline.overall_first_pass_approval - current_approval
        if drop > THRESHOLD_APPROVAL_DROP_PP:
            flags.append(
                f"⚠️ First-pass approval rate dropped by {drop:.1f}pp "
                f"({baseline.overall_first_pass_approval:.1f}% → {current_approval:.1f}%)"
            )

    # Opus/Codex share rise
    share_rise = current_opus_share - baseline.opus_codex_token_share
    if share_rise > THRESHOLD_OPUS_SHARE_RISE_PP:
        flags.append(
            f"⚠️ Opus/Codex token share rose by {share_rise:.1f}pp "
            f"({baseline.opus_codex_token_share:.1f}% → {current_opus_share:.1f}%)"
        )

    # Per-model checks
    for model, m in current.items():
        baseline_approval = baseline.model_first_pass_approval.get(model)
        if baseline_approval is not None and m.prs_touched >= 3:
            drop = baseline_approval - m.first_pass_approval_rate
            if drop > THRESHOLD_APPROVAL_DROP_PP:
                flags.append(
                    f"⚠️ {model}: first-pass approval dropped by {drop:.1f}pp"
                )

        baseline_defect = baseline.model_defect_rate.get(model)
        if baseline_defect is not None and m.prs_merged >= 3:
            current_defect = (m.post_merge_defects / m.prs_merged * 100) if m.prs_merged else 0
            rise = current_defect - baseline_defect
            if rise > THRESHOLD_DEFECT_RATE_RISE_PP:
                flags.append(
                    f"⚠️ {model}: post-merge defect rate rose by {rise:.1f}pp"
                )

        baseline_reviews = baseline.model_reviews_per_merged_pr.get(model)
        if baseline_reviews is not None and m.prs_merged >= 3:
            rise = m.reviews_per_merged_pr - baseline_reviews
            if rise > THRESHOLD_REVIEW_ROUNDS_RISE:
                flags.append(
                    f"⚠️ {model}: reviews per merged PR rose by {rise:.1f} ({baseline_reviews:.1f} → {m.reviews_per_merged_pr:.1f})"
                )

    return flags


# ---------------------------------------------------------------------------
# Report rendering (Markdown)
# ---------------------------------------------------------------------------

def render_report(
    snapshots: List[DailySnapshot],
    model_metrics: Dict[str, ModelMetrics],
    baseline: Optional[Baseline],
    flags: List[str],
    days: int,
    is_baseline: bool = False,
) -> str:
    """Render a Markdown report."""
    title = "Baseline Report" if is_baseline else f"Lane Quality Report ({days}-day window)"
    lines = [f"## {title}", ""]

    if flags:
        lines.append("### ⚠️ Degradation Flags")
        for flag in flags:
            lines.append(f"- {flag}")
        lines.append("")

    # Summary table
    total_lanes = sum(m.lane_count for m in model_metrics.values())
    total_cost = sum(m.total_cost for m in model_metrics.values())
    total_merged = sum(m.prs_merged for m in model_metrics.values())
    lines.append(f"**Period:** {days} days | **Lanes:** {total_lanes} "
                 f"| **Cost:** ${total_cost:.2f} | **PRs merged:** {total_merged}")
    lines.append("")

    # Opus/Codex share over time
    if snapshots:
        lines.append("### Opus/Codex Token Share Over Time")
        lines.append("| Date | Lanes | Total Tokens | Opus/Codex % | Orchestrator % | Reviewer/Escalation % | Cost |")
        lines.append("|------|-------|-------------|-------------|---------------|-----------------------|------|")
        for snap in snapshots:
            lines.append(
                f"| {snap.date} | {snap.total_lanes} | {snap.total_tokens:,} "
                f"| {snap.opus_codex_token_share:.1f}% | {snap.opus_codex_orchestrator_share:.1f}% "
                f"| {snap.opus_codex_worker_share:.1f}% | ${snap.total_cost:.2f} |"
            )
        lines.append("")

    # Model comparison table
    lines.append("### Model Comparison")
    lines.append(
        "| Model | Lanes | Completed | Failed | Escalated | PRs Merged "
        "| 1st-Pass Approval | Reviews / Merged PR | Req-Changes Findings | Post-Merge Defects "
        "| Cost / Merged PR | Total Cost |"
    )
    lines.append("|" + "|".join(["---"] * 12) + "|")

    for model_id in sorted(model_metrics.keys()):
        m = model_metrics[model_id]
        lines.append(
            f"| `{m.model_id}` | {m.lane_count} | {m.completed_count} | {m.failed_count} "
            f"| {m.escalated_count} | {m.prs_merged} "
            f"| {m.first_pass_approval_rate:.0f}% | {m.reviews_per_merged_pr:.1f} "
            f"| {m.request_changes_received} | {m.post_merge_defects} | ${m.cost_per_merged_pr:.2f} "
            f"| ${m.total_cost:.2f} |"
        )
    lines.append("")

    # Baseline reference
    if baseline and not is_baseline:
        lines.append("### Baseline Reference")
        lines.append(f"- Computed: {baseline.computed_at[:10]}")
        lines.append(f"- Period: {baseline.days} days")
        lines.append(f"- Opus/Codex share: {baseline.opus_codex_token_share:.1f}%")
        lines.append(f"- Overall first-pass approval: {baseline.overall_first_pass_approval:.1f}%")
        lines.append(f"- Overall reviews per merged PR: {baseline.overall_reviews_per_merged_pr:.1f}")
        lines.append(f"- Overall avg review rounds: {baseline.overall_review_rounds_avg:.1f}")
        lines.append(f"- Overall defect rate: {baseline.overall_defect_rate:.1f}%")
        lines.append("")
    # Escalation detail
    escalated_models = [
        (model, m) for model, m in model_metrics.items()
        if m.escalated_count > 0 or m.failed_count > 0
    ]
    if escalated_models:
        lines.append("### Escalation & Failure Detail")
        for model, m in sorted(escalated_models, key=lambda x: -(x[1].escalated_count + x[1].failed_count)):
            esc_rate = (m.escalated_count / m.lane_count * 100) if m.lane_count else 0
            fail_rate = (m.failed_count / m.lane_count * 100) if m.lane_count else 0
            lines.append(
                f"- `{model}`: {m.escalated_count} escalated ({esc_rate:.0f}%), "
                f"{m.failed_count} failed ({fail_rate:.0f}%) out of {m.lane_count} lanes"
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Snapshot persistence
# ---------------------------------------------------------------------------

def save_snapshot(snapshot: DailySnapshot) -> Path:
    """Save a daily snapshot to the cache directory."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"snapshot-{snapshot.date}.json"
    path.write_text(json.dumps(snapshot.to_dict(), indent=2), encoding="utf-8")
    return path


def save_baseline(baseline: Baseline) -> Path:
    """Save baseline to the cache directory."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / "baseline.json"
    path.write_text(json.dumps(baseline.to_dict(), indent=2), encoding="utf-8")
    return path


def load_baseline() -> Optional[Baseline]:
    """Load baseline from cache."""
    path = CACHE_DIR / "baseline.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Baseline.from_dict(data)
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# GitHub issue posting
# ---------------------------------------------------------------------------

def post_to_issue(issue_url: str, body: str) -> bool:
    """Post a comment to a GitHub issue."""
    match = re.match(r'https://github\.com/([^/]+/[^/]+)/issues/(\d+)', issue_url)
    if not match:
        print(f"ERROR: Invalid issue URL: {issue_url}", file=sys.stderr)
        return False
    repo, number = match.group(1), match.group(2)
    try:
        result = subprocess.run(
            ["gh", "issue", "comment", number, "--repo", repo, "--body", body],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_report(args: argparse.Namespace) -> int:
    """Generate a rolling-window report."""
    print(f"Collecting lanes from last {args.days} days...")
    lanes = collect_all_lanes(days=args.days)
    print(f"Found {len(lanes)} lane records")

    if not lanes:
        print("No lanes found. Nothing to report.")
        return 0

    # Collect all PR URLs
    all_pr_urls: Set[str] = set()
    for lane in lanes:
        all_pr_urls.update(lane.pr_urls)
    print(f"Found {len(all_pr_urls)} unique PR URLs")

    # Fetch PR data (skip if --no-github)
    pr_records: Dict[str, PRRecord] = {}
    if not args.no_github:
        print("Fetching GitHub PR data...")
        pr_records = fetch_pr_data(all_pr_urls, days=args.days, use_cache=not getattr(args, "refresh_cache", False))
        print(f"Fetched data for {len(pr_records)} PRs")

    # Aggregate
    model_metrics = aggregate_by_model(lanes, pr_records)
    snapshots = compute_daily_snapshots(lanes, pr_records)

    # Save snapshots
    for snap in snapshots:
        save_snapshot(snap)

    # Load baseline
    baseline = load_baseline()

    # Detect degradation
    total_tokens = sum(l.input_tokens + l.output_tokens for l in lanes)
    premium_tokens = sum(
        l.input_tokens + l.output_tokens for l in lanes if _is_premium(l.primary_model)
    )
    current_opus_share = (premium_tokens / total_tokens * 100) if total_tokens else 0
    flags = []
    if baseline:
        flags = detect_degradation(model_metrics, baseline, current_opus_share)

    # Render
    report = render_report(snapshots, model_metrics, baseline, flags, args.days)
    print(report)

    if args.post:
        print(f"\nPosting to {args.post}...")
        if post_to_issue(args.post, report):
            print("Posted successfully.")
        else:
            print("Failed to post.", file=sys.stderr)
            return 1

    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    """Compute and save a baseline from the last N days."""
    print(f"Computing baseline from last {args.days} days...")
    lanes = collect_all_lanes(days=args.days)
    print(f"Found {len(lanes)} lane records")

    if not lanes:
        print("No lanes found. Cannot compute baseline.")
        return 1

    all_pr_urls: Set[str] = set()
    for lane in lanes:
        all_pr_urls.update(lane.pr_urls)

    pr_records: Dict[str, PRRecord] = {}
    if not args.no_github:
        print("Fetching GitHub PR data...")
        pr_records = fetch_pr_data(all_pr_urls, days=args.days, use_cache=not getattr(args, "refresh_cache", False))
        print(f"Fetched data for {len(pr_records)} PRs")

    baseline = compute_baseline(lanes, pr_records, days=args.days)
    path = save_baseline(baseline)
    print(f"Baseline saved to {path}")

    model_metrics = aggregate_by_model(lanes, pr_records)
    snapshots = compute_daily_snapshots(lanes, pr_records)
    report = render_report(snapshots, model_metrics, None, [], args.days, is_baseline=True)
    print(report)

    if args.post:
        print(f"\nPosting baseline to {args.post}...")
        if post_to_issue(args.post, report):
            print("Posted successfully.")
        else:
            print("Failed to post.", file=sys.stderr)
            return 1

    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Save today's snapshot."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lanes = collect_all_lanes(days=1)
    print(f"Found {len(lanes)} lanes for {today}")

    all_pr_urls: Set[str] = set()
    for lane in lanes:
        all_pr_urls.update(lane.pr_urls)

    pr_records: Dict[str, PRRecord] = {}
    if not args.no_github:
        pr_records = fetch_pr_data(all_pr_urls, days=1, use_cache=not getattr(args, "refresh_cache", False))

    snapshots = compute_daily_snapshots(lanes, pr_records)
    for snap in snapshots:
        path = save_snapshot(snap)
        print(f"Saved {path}")

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lane quality telemetry: model comparison over time"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_report = sub.add_parser("report", help="Generate rolling-window report")
    p_report.add_argument("--days", type=int, default=7)
    p_report.add_argument("--post", type=str, default="",
                          help="GitHub issue URL to post report to")
    p_report.add_argument("--no-github", action="store_true",
                          help="Skip GitHub API calls (session data only)")
    p_report.add_argument("--refresh-cache", action="store_true",
                          help="Bypass PR cache and re-fetch from GitHub")

    p_baseline = sub.add_parser("baseline", help="Compute and save baseline")
    p_baseline.add_argument("--days", type=int, default=14)
    p_baseline.add_argument("--post", type=str, default="",
                            help="GitHub issue URL to post baseline to")
    p_baseline.add_argument("--no-github", action="store_true",
                            help="Skip GitHub API calls (session data only)")
    p_baseline.add_argument("--refresh-cache", action="store_true",
                            help="Bypass PR cache and re-fetch from GitHub")

    p_snapshot = sub.add_parser("snapshot", help="Save today's snapshot")
    p_snapshot.add_argument("--no-github", action="store_true")
    p_snapshot.add_argument("--refresh-cache", action="store_true",
                            help="Bypass PR cache and re-fetch from GitHub")

    args = parser.parse_args(argv)
    if args.command == "report":
        return cmd_report(args)
    elif args.command == "baseline":
        return cmd_baseline(args)
    elif args.command == "snapshot":
        return cmd_snapshot(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
