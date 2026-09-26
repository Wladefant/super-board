#!/usr/bin/env python3
"""
workflows/portable/staging_outer_loop.py — Staging Outer-Loop Poller for Superboard.

Scheduled local read-only poller, strictly scoped to the staging Dokploy compose.
Detects staging deployment failures, runtime error spikes, and down containers,
deduplicates against open GitHub issues in Bavariance/polysimulator, and dispatches
Telegram alerts for new incidents.

Governed by:
  - https://github.com/Wladefant/super-board/issues/246#issuecomment-5850342612
  - https://github.com/Wladefant/super-board/issues/227 (High-Trust Architecture)
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Hard-coded allowlist strictly scoped to staging. Any other id exits 2.
STAGING_COMPOSE_ID = "TU7b_dY9l9_nCas6YBNwj"
STAGING_SERVER_ID = "-7a9lRfDEHKAcUnlwY-cF"
ALLOWED_COMPOSE_IDS = frozenset({STAGING_COMPOSE_ID})
ALLOWED_SERVER_IDS = frozenset({STAGING_SERVER_ID})

DEFAULT_REPO = "Bavariance/polysimulator"
DEFAULT_APP_NAME = "polysimulator-staging-iad-v09j4g"
DEFAULT_BASE_URL = "https://hosting.wladefant.de/api"
DEFAULT_STATE_FILE = Path.home() / ".veyyon" / "workflows" / "staging_outer_loop_state.json"


def check_allowlist(compose_id: str, server_id: str) -> None:
    """Verify that target composeId and serverId strictly match the staging allowlist."""
    if compose_id not in ALLOWED_COMPOSE_IDS or server_id not in ALLOWED_SERVER_IDS:
        sys.stderr.write(
            f"ERROR: Allowlist violation: composeId='{compose_id}', serverId='{server_id}' not allowed. "
            f"Only staging compose '{STAGING_COMPOSE_ID}' and server '{STAGING_SERVER_ID}' are permitted.\n"
        )
        sys.exit(2)


def load_dokploy_api_key() -> Optional[str]:
    """
    Read DOKPLOY_API_KEY from environment or default agent profile .env.
    Never prints, logs, or persists the key.
    """
    key = os.environ.get("DOKPLOY_API_KEY")
    if key:
        return key.strip().strip('"').strip("'")

    candidates = [
        Path.home() / ".veyyon" / "profiles" / "default" / "agent" / ".env",
        Path.home() / ".veyyon" / ".env",
    ]
    for env_path in candidates:
        if env_path.exists():
            try:
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() == "DOKPLOY_API_KEY":
                        val = v.strip().strip('"').strip("'")
                        if val:
                            return val
            except Exception:
                continue
    return None


def redact_log_content(text: str) -> str:
    """Apply existing redaction helpers to sanitize logs and diagnostics."""
    if not text:
        return ""
    out = str(text)
    try:
        from recurrence_guard import redact_diagnostic
        out = redact_diagnostic(out)
    except Exception:
        pass
    try:
        from telegram_notifier import SecretSanitizer
        out = SecretSanitizer.sanitize(out)
    except Exception:
        pass
    return out


class DokployReadOnlyClient:
    """Read-only client for Dokploy REST API. Only calls GET procedures."""

    def __init__(self, base_url: str, api_key: str, compose_id: str, server_id: str):
        check_allowlist(compose_id, server_id)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.compose_id = compose_id
        self.server_id = server_id

    def _get(self, proc: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}/{proc}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "x-api-key": self.api_key,
            "User-Agent": "SuperBoard-StagingOuterLoop/1.0",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)

    def get_deployments(self) -> List[Dict[str, Any]]:
        return self._get("deployment.allByCompose", {"composeId": self.compose_id})

    def get_containers(self, app_name: str) -> List[Dict[str, Any]]:
        return self._get("docker.getContainersByAppNameMatch", {
            "appName": app_name,
            "appType": "docker-compose",
            "serverId": self.server_id,
        })

    def read_compose_logs(self, container_id: str, tail: int = 500) -> str:
        try:
            res = self._get("compose.readLogs", {
                "composeId": self.compose_id,
                "containerId": container_id,
                "tail": tail,
            })
            if isinstance(res, str):
                return res
            return str(res)
        except Exception as e:
            return f"[compose logs unavailable: {e}]"

    def read_deployment_logs(self, deployment_id: str, tail: int = 60) -> str:
        try:
            res = self._get("deployment.readLogs", {
                "deploymentId": deployment_id,
                "tail": tail,
            })
            if isinstance(res, str):
                return res
            return str(res)
        except Exception as e:
            return f"[deployment logs unavailable: {e}]"


class FixtureClient:
    """Mock client used when --inject-fixture is supplied."""

    def __init__(self, fixture_data: Dict[str, Any]):
        self.fixture_data = fixture_data

    def get_deployments(self) -> List[Dict[str, Any]]:
        return self.fixture_data.get("deployments", [])

    def get_containers(self, app_name: str) -> List[Dict[str, Any]]:
        return self.fixture_data.get("containers", [])

    def read_compose_logs(self, container_id: str, tail: int = 500) -> str:
        logs = self.fixture_data.get("logs", {})
        if isinstance(logs, dict):
            return logs.get(container_id, "")
        return str(logs)

    def read_deployment_logs(self, deployment_id: str, tail: int = 60) -> str:
        for dep in self.fixture_data.get("deployments", []):
            if dep.get("deploymentId") == deployment_id and "logs" in dep:
                return dep["logs"]
        logs = self.fixture_data.get("deployment_logs", {})
        if isinstance(logs, dict):
            return logs.get(deployment_id, "")
        return str(logs)


@dataclass
class Incident:
    key: str
    signal: str
    service: str
    title: str
    body: str
    summary: str
    labels: List[str]


def extract_service_name(container_name: str) -> str:
    """Extract canonical service name from container name."""
    name = container_name.strip().lstrip("/")
    prefix = "polysimulator-staging-iad-v09j4g-"
    if name.startswith(prefix):
        name = name[len(prefix):]
    name = re.sub(r"-\d+$", "", name)
    return name or "service"


def parse_datetime(iso_str: Optional[str]) -> Optional[datetime]:
    if not iso_str:
        return None
    try:
        clean = iso_str.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(clean)
    except Exception:
        return None
def parse_log_timestamp(line: str) -> Optional[datetime]:
    # 1. Leading Docker timestamp
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)(?:Z|[+-]\d{2}:?\d{2})?", line)
    if m:
        try:
            return datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    # 2. JSON timestamp field
    if "{" in line:
        try:
            data = json.loads(line[line.find("{"):])
            if "timestamp" in data:
                return parse_datetime(str(data["timestamp"]))
        except Exception:
            pass
    return None


def is_error_log_line(line: str) -> bool:
    if "{" in line:
        try:
            data = json.loads(line[line.find("{"):])
            if str(data.get("level", "")).upper() == "ERROR":
                return True
        except Exception:
            pass
    if re.search(r"\bERROR\b", line, re.IGNORECASE):
        if not re.search(r"error_count[\"':\s]+0\b", line, re.IGNORECASE):
            return True
    return False


def parse_api_stats(line: str) -> Optional[Dict[str, int]]:
    if "{" not in line:
        return None
    try:
        data = json.loads(line[line.find("{"):])
        if data.get("logger") == "api.stats":
            stats_data = data.get("data") if isinstance(data.get("data"), dict) else data
            req_count = int(stats_data.get("request_count", 0))
            err_count = int(stats_data.get("error_count", 0))
            return {"requests": req_count, "errors": err_count}
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Signal 1: Deploy Failure
# ---------------------------------------------------------------------------

def check_deploy_failures(
    deployments: List[Dict[str, Any]],
    client_or_fixture: Any,
    tail_lines: int = 60,
) -> List[Incident]:
    """Detect deployments with status == 'error', sorted by parsed createdAt descending."""
    def sort_key(d: Dict[str, Any]) -> float:
        dt = parse_datetime(d.get("createdAt"))
        return dt.timestamp() if dt else 0.0

    sorted_deps = sorted(deployments, key=sort_key, reverse=True)
    incidents = []

    for dep in sorted_deps:
        if dep.get("status") == "error":
            dep_id = dep.get("deploymentId") or "unknown"
            key = f"deploy:{dep_id}"
            title_text = redact_log_content(dep.get("title") or "Deployment Error")
            desc = dep.get("description") or ""
            commit_match = re.search(r"Commit:\s*([0-9a-fA-F]+)", desc)
            commit_str = commit_match.group(1) if commit_match else desc
            err_msg = redact_log_content(dep.get("errorMessage") or "No error message recorded.")
            created_at = dep.get("createdAt") or "unknown"
            finished_at = dep.get("finishedAt") or "unknown"

            raw_logs = ""
            if "logs" in dep:
                raw_logs = dep["logs"]
            elif hasattr(client_or_fixture, "read_deployment_logs"):
                raw_logs = client_or_fixture.read_deployment_logs(dep_id, tail=tail_lines)

            log_tail = raw_logs.splitlines()[-tail_lines:] if raw_logs else []
            redacted_logs = redact_log_content("\n".join(log_tail))

            body = (
                f"outer-loop-key: {key}\n\n"
                f"## Incident: Staging Deployment Failure\n\n"
                f"- **Deployment ID:** `{dep_id}`\n"
                f"- **Title:** {title_text}\n"
                f"- **Commit:** `{commit_str}`\n"
                f"- **Error Message:** {err_msg}\n"
                f"- **Created At (UTC):** {created_at}\n"
                f"- **Finished At (UTC):** {finished_at}\n\n"
                f"### Deployment Logs (tail {tail_lines})\n"
                f"```text\n{redacted_logs}\n```\n"
            )
            summary = f"Staging deploy failed: {title_text[:60]} (ID: {dep_id})"
            incidents.append(Incident(
                key=key,
                signal="deploy_failed",
                service="polysimulator-staging",
                title=f"incident(staging): deploy_failed polysimulator-staging",
                body=body,
                summary=summary,
                labels=["kind:incident", "area:deploy", "risk:high"],
            ))
    return incidents


# ---------------------------------------------------------------------------
# Signal 2: Runtime Error Spike
# ---------------------------------------------------------------------------

def check_runtime_error_spikes(
    containers: List[Dict[str, Any]],
    client_or_fixture: Any,
    now_utc: Optional[datetime] = None,
    window_minutes: int = 10,
    min_buckets: int = 3,
    min_errors: int = 5,
    min_rate: float = 20.0,
    min_error_lines: int = 50,
) -> List[Incident]:
    """
    Detect runtime error spikes in backend or backend-daemon containers.
    A spike is:
      - >= 3 of the last 5 one-minute buckets with error_count >= 5 and error_rate_percent >= 20%
      OR
      - >= 50 'level': 'ERROR' lines in 10 minutes in the backend or backend-daemon container.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    utc_hour = now_utc.strftime("%Y-%m-%dT%H")
    window_start = now_utc - timedelta(minutes=window_minutes)

    incidents = []

    for c in containers:
        name = c.get("name") or c.get("Names") or ""
        service = extract_service_name(name)
        if "backend" not in service:
            continue

        container_id = c.get("containerId") or c.get("Id") or ""
        raw_logs = ""
        if "logs" in c:
            raw_logs = c["logs"]
        elif hasattr(client_or_fixture, "read_compose_logs") and container_id:
            raw_logs = client_or_fixture.read_compose_logs(container_id, tail=500)

        lines = raw_logs.splitlines() if raw_logs else []
        error_line_count = 0
        bucket_stats: Dict[int, Dict[str, int]] = {i: {"requests": 0, "errors": 0} for i in range(window_minutes)}

        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue

            ts = parse_log_timestamp(line_str)
            if not ts or ts < window_start or ts > now_utc:
                continue

            if is_error_log_line(line_str):
                error_line_count += 1

            stats = parse_api_stats(line_str)
            if stats:
                delta_sec = (now_utc - ts).total_seconds()
                minute_idx = int(delta_sec // 60)
                if 0 <= minute_idx < window_minutes:
                    bucket_stats[minute_idx]["requests"] += stats["requests"]
                    bucket_stats[minute_idx]["errors"] += stats["errors"]

        # Evaluate last 5 one-minute buckets (minutes 0 to 4)
        spiking_buckets = 0
        for m in range(5):
            reqs = bucket_stats[m]["requests"]
            errs = bucket_stats[m]["errors"]
            rate = (errs / reqs * 100.0) if reqs > 0 else 0.0
            if errs >= min_errors and rate >= min_rate:
                spiking_buckets += 1

        criterion_a_triggered = spiking_buckets >= min_buckets
        criterion_b_triggered = error_line_count >= min_error_lines

        if criterion_a_triggered or criterion_b_triggered:
            key = f"spike:{service}:{utc_hour}"
            reasons = []
            if criterion_a_triggered:
                reasons.append(
                    f"{spiking_buckets} of the last 5 one-minute buckets had >= {min_errors} errors and >= {min_rate}% error rate"
                )
            if criterion_b_triggered:
                reasons.append(
                    f"{error_line_count} 'ERROR' lines in the last {window_minutes} minutes (threshold: {min_error_lines})"
                )

            reason_str = "; ".join(reasons)
            body = (
                f"outer-loop-key: {key}\n\n"
                f"## Incident: Staging Runtime Error Spike on `{service}`\n\n"
                f"- **Service:** `{service}`\n"
                f"- **Container:** `{name}`\n"
                f"- **UTC Hour:** `{utc_hour}`\n"
                f"- **Trigger:** {reason_str}\n\n"
                f"### Metrics Summary\n"
                f"- Total ERROR lines in {window_minutes}m: {error_line_count}\n"
                f"- Spiking 1-minute buckets (last 5 min): {spiking_buckets}/5\n"
            )
            summary = f"Staging runtime error spike on {service}: {reason_str}"
            incidents.append(Incident(
                key=key,
                signal="error_spike",
                service=service,
                title=f"incident(staging): error_spike {service}",
                body=body,
                summary=summary,
                labels=["kind:incident", "area:backend", "risk:high"],
            ))
    return incidents


# ---------------------------------------------------------------------------
# Signal 3: Container Down
# ---------------------------------------------------------------------------

def check_container_down(
    containers: List[Dict[str, Any]],
    now_utc: Optional[datetime] = None,
) -> List[Incident]:
    """Detect staging containers whose state is not 'running', or status has unhealthy/restarting."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    utc_hour = now_utc.strftime("%Y-%m-%dT%H")

    incidents = []
    for c in containers:
        name = c.get("name") or c.get("Names") or ""
        state = (c.get("state") or c.get("State") or "").lower()
        status = (c.get("status") or c.get("Status") or "").lower()

        is_down = False
        reason = ""
        if state != "running":
            is_down = True
            reason = f"State is '{state}' (expected 'running')"
        elif "unhealthy" in status:
            is_down = True
            reason = f"Status reports unhealthy: '{status}'"
        elif "restarting" in status:
            is_down = True
            reason = f"Status reports restarting: '{status}'"

        if is_down:
            service = extract_service_name(name)
            key = f"down:{service}:{utc_hour}"
            body = (
                f"outer-loop-key: {key}\n\n"
                f"## Incident: Staging Container Down\n\n"
                f"- **Service:** `{service}`\n"
                f"- **Container Name:** `{name}`\n"
                f"- **State:** `{state}`\n"
                f"- **Status:** `{status}`\n"
                f"- **Trigger:** {reason}\n"
                f"- **Detected At (UTC):** {now_utc.isoformat()}\n"
            )
            summary = f"Staging container down: {service} ({reason})"
            incidents.append(Incident(
                key=key,
                signal="container_down",
                service=service,
                title=f"incident(staging): container_down {service}",
                body=body,
                summary=summary,
                labels=["kind:incident", "area:deploy", "risk:high"],
            ))
    return incidents


# ---------------------------------------------------------------------------
# GitHub & Telegram Interaction
# ---------------------------------------------------------------------------

def find_open_issue_by_key(repo: str, key: str) -> Optional[Dict[str, Any]]:
    """Search open issues in repository for the literal 'outer-loop-key: <key>' in body."""
    cmd = [
        "gh", "issue", "list",
        "--repo", repo,
        "--state", "open",
        "--search", f"\"outer-loop-key: {key}\"",
        "--json", "number,title,body,url",
        "--limit", "10",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        issues = json.loads(res.stdout or "[]")
        exact_target = f"outer-loop-key: {key}"
        for issue in issues:
            if exact_target in issue.get("body", ""):
                return issue
    except Exception as e:
        sys.stderr.write(f"Warning: error searching issues in {repo}: {e}\n")
    return None


def create_github_issue(repo: str, incident: Incident) -> Optional[str]:
    """Create a new GitHub issue and return its URL."""
    cmd = [
        "gh", "issue", "create",
        "--repo", repo,
        "--title", incident.title,
        "--body", incident.body,
        "--label", ",".join(incident.labels),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"Error creating GitHub issue: {e.stderr}\n")
        return None


def add_github_comment(repo: str, issue_number: int, comment: str) -> bool:
    """Add a comment to an existing GitHub issue."""
    cmd = [
        "gh", "issue", "comment",
        str(issue_number),
        "--repo", repo,
        "--body", comment,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return res.returncode == 0
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"Error commenting on GitHub issue #{issue_number}: {e.stderr}\n")
        return False


def send_telegram_alert(summary: str, link: str, dry_run: bool = False) -> bool:
    """Send a Telegram alert for a newly created incident issue."""
    if dry_run:
        print(f"[DRY-RUN] Would send Telegram alert: summary='{summary}', link='{link}'")
        return True

    # Locate telegram_notifier.py in current directory or ~/.veyyon/workflows/
    notifier_script = SCRIPT_DIR / "telegram_notifier.py"
    if not notifier_script.exists():
        alt = Path.home() / ".veyyon" / "workflows" / "telegram_notifier.py"
        if alt.exists():
            notifier_script = alt

    cmd = [
        sys.executable,
        str(notifier_script),
        "--project", "polysimulator",
        "--event-type", "blocker",
        "--summary", summary,
        "--link", link,
        "--send",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            sys.stderr.write(f"Warning: telegram_notifier.py exited {res.returncode}: {res.stderr}\n")
            return False
        return True
    except Exception as e:
        sys.stderr.write(f"Warning: failed to invoke telegram_notifier.py: {e}\n")
        return False


# ---------------------------------------------------------------------------
# State Management
# ---------------------------------------------------------------------------

def load_state(state_path: Path) -> Dict[str, Any]:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_seen_deployment_ids": [], "commented_keys": {}}


def save_state(state_path: Path, state: Dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, state_path)


# ---------------------------------------------------------------------------
# Main Orchestration
# ---------------------------------------------------------------------------

def run_outer_loop(
    compose_id: str = STAGING_COMPOSE_ID,
    server_id: str = STAGING_SERVER_ID,
    app_name: str = DEFAULT_APP_NAME,
    repo: str = DEFAULT_REPO,
    state_path: Path = DEFAULT_STATE_FILE,
    dry_run: bool = False,
    inject_fixture: Optional[str] = None,
    base_url: str = DEFAULT_BASE_URL,
    now_utc: Optional[datetime] = None,
) -> int:
    """Execute one outer-loop polling iteration."""
    # Strict allowlist check: fails closed with exit code 2
    check_allowlist(compose_id, server_id)

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # Initialize client (either fixture or live Dokploy)
    if inject_fixture:
        fixture_data: Dict[str, Any] = {}
        if os.path.exists(inject_fixture):
            fixture_data = json.loads(Path(inject_fixture).read_text(encoding="utf-8"))
        else:
            fixture_data = json.loads(inject_fixture)
        client = FixtureClient(fixture_data)
    else:
        api_key = load_dokploy_api_key()
        if not api_key:
            sys.stderr.write("ERROR: DOKPLOY_API_KEY could not be resolved from environment or profile .env\n")
            return 1
        client = DokployReadOnlyClient(
            base_url=base_url,
            api_key=api_key,
            compose_id=compose_id,
            server_id=server_id,
        )

    # Load state
    state = load_state(state_path)
    last_seen_deps = set(state.get("last_seen_deployment_ids", []))
    commented_keys = state.get("commented_keys", {})

    # Collect signals
    deployments = client.get_deployments()
    containers = client.get_containers(app_name)

    incidents: List[Incident] = []
    incidents.extend(check_deploy_failures(deployments, client))
    incidents.extend(check_runtime_error_spikes(containers, client, now_utc=now_utc))
    incidents.extend(check_container_down(containers, now_utc=now_utc))

    print(f"Staging outer loop evaluated {len(deployments)} deployments and {len(containers)} containers.")
    print(f"Total active incident signals: {len(incidents)}")

    # Process each incident with deduplication
    created_count = 0
    commented_count = 0
    skipped_count = 0

    for inc in incidents:
        existing = find_open_issue_by_key(repo, inc.key)
        if existing:
            issue_num = existing.get("number")
            issue_url = existing.get("url") or f"https://github.com/{repo}/issues/{issue_num}"

            # If it's a spike, at most one comment per hour
            if inc.signal == "error_spike":
                last_commented_iso = commented_keys.get(inc.key)
                if last_commented_iso:
                    last_dt = parse_datetime(last_commented_iso)
                    if last_dt and (now_utc - last_dt) < timedelta(hours=1):
                        print(f"Dedupe: Skipping comment for {inc.key} on issue #{issue_num} (commented within 1 hour).")
                        skipped_count += 1
                        continue

            comment_body = (
                f"### Incident Update: {inc.title}\n\n"
                f"- **Signal:** `{inc.signal}`\n"
                f"- **Key:** `{inc.key}`\n"
                f"- **Updated At (UTC):** {now_utc.isoformat()}\n\n"
                f"{inc.summary}\n"
            )
            if dry_run:
                print(f"[DRY-RUN] Would comment on issue #{issue_num} ({issue_url}):\n{comment_body[:200]}...")
            else:
                success = add_github_comment(repo, issue_num, comment_body)
                if success:
                    print(f"Dedupe: Added comment to existing issue #{issue_num} for key {inc.key}")
                    commented_keys[inc.key] = now_utc.isoformat()
                    commented_count += 1
        else:
            # Create new issue and notify Telegram
            if dry_run:
                print(f"[DRY-RUN] Would create issue in {repo}: title='{inc.title}', key='{inc.key}'")
                send_telegram_alert(inc.summary, f"https://github.com/{repo}/issues/<new>", dry_run=True)
                created_count += 1
            else:
                issue_url = create_github_issue(repo, inc)
                if issue_url:
                    print(f"Created new incident issue: {issue_url} for key {inc.key}")
                    send_telegram_alert(inc.summary, issue_url, dry_run=False)
                    created_count += 1
                else:
                    sys.stderr.write(f"Failed to create incident issue for key {inc.key}\n")

    # Update state
    if not dry_run:
        all_dep_ids = [d.get("deploymentId") for d in deployments if d.get("deploymentId")]
        state["last_seen_deployment_ids"] = list(set(last_seen_deps).union(all_dep_ids))
        state["commented_keys"] = commented_keys
        state["last_run_utc"] = now_utc.isoformat()
        save_state(state_path, state)

    print(f"Outer loop finished: {created_count} created, {commented_count} commented, {skipped_count} skipped.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Staging Outer-Loop Poller for Superboard")
    parser.add_argument("--compose-id", default=STAGING_COMPOSE_ID, help="Target Dokploy compose ID (allowlist enforced)")
    parser.add_argument("--server-id", default=STAGING_SERVER_ID, help="Target Dokploy server ID (allowlist enforced)")
    parser.add_argument("--app-name", default=DEFAULT_APP_NAME, help="Target application name for container lookup")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="Target GitHub repository for incident tracking")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE, help="Path to JSON state file")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without mutating GitHub or sending Telegram")
    parser.add_argument("--inject-fixture", type=str, default=None, help="JSON string or file path containing synthetic fixture data")
    parser.add_argument("--dokploy-base-url", default=DEFAULT_BASE_URL, help="Dokploy API base URL")

    args = parser.parse_args()
    rc = run_outer_loop(
        compose_id=args.compose_id,
        server_id=args.server_id,
        app_name=args.app_name,
        repo=args.repo,
        state_path=args.state_file,
        dry_run=args.dry_run,
        inject_fixture=args.inject_fixture,
        base_url=args.dokploy_base_url,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
