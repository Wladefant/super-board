#!/usr/bin/env python3
"""Run zero-quota routing checks and explicit approval-gated live probes."""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from preflight import (
    classify_error,
    parse_sse_records,
    redact,
    structural_http_matches,
    terminal_sse_seen,
)

REQUESTED_MODEL_RE = re.compile(r"requested[ _-]?model[=: ]+([A-Za-z0-9._/-]+)", re.I)
RESOLVED_MODEL_RE = re.compile(r"resolved[ _-]?model[=: ]+([A-Za-z0-9._/-]+)", re.I)
PROVIDER_RE = re.compile(r"(?:resolved[ _-]?)?provider[=: ]+([A-Za-z0-9._/-]+)", re.I)
LIVE_ALIAS_COMMANDS = ("live-alias-luna", "live-alias-terra", "live-alias-sol")
LIVE_COMMANDS = {"live-main-sol", "live-stable-control", "live-aliases", *LIVE_ALIAS_COMMANDS}
ALIAS_PROBE_SPECS = {
    "live-alias-luna": {
        "subagent_type": "luna", "inline_model": "haiku", "model": "gpt-5.6-luna",
        "description": "Run Luna initial probe", "initial": "LUNA_INITIAL",
        "summary": "Resume Luna routing probe", "resume": "LUNA_RESUME",
    },
    "live-alias-terra": {
        "subagent_type": "terra", "inline_model": "sonnet", "model": "gpt-5.6-terra",
        "description": "Run Terra initial probe", "initial": "TERRA_INITIAL",
        "summary": "Resume Terra routing probe", "resume": "TERRA_RESUME",
    },
    "live-alias-sol": {
        "subagent_type": "sol", "inline_model": "opus", "model": "gpt-5.6-sol",
        "description": "Run Sol initial probe", "initial": "SOL_INITIAL",
        "summary": "Resume Sol routing probe", "resume": "SOL_RESUME",
    },
}
APPROVED_UPSTREAM = "http://127.0.0.1:8317"
EXPECTED_PROVIDER = "codex"
# The current CLI can emit one unscoped Luna helper request before and after the Sol controller.
MAX_AUXILIARY_MAIN_LUNA = 2
ROUTING_STATE_SCHEMA_VERSION = 2
PROBE_SUMMARY_SCHEMA_VERSION = 2
ALIAS_RESULT_MAX_AGE_HOURS = 24
LIVE_ALIAS_COOLDOWN_SECONDS = 1.0
SKILL_VERSION = "1"
ROUTING_STATE_FILENAME = "last-routing-probe.json"
ROUTE_MODELS = {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"}
ROUTE_TURNS = {"initial", "resume"}
HASHED_AGENT_KEY_RE = re.compile(r"^[0-9a-f]{12}$")
SAFE_VERSION_RE = re.compile(r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.-]+)?)(?![A-Za-z0-9])")


def default_routing_state_path() -> Path:
    return Path.home() / ".claude" / "claudex-optimized" / ROUTING_STATE_FILENAME


def launch_script_path() -> Path:
    return Path(__file__).resolve().with_name("launch.ps1")


def _safe_version(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    match = SAFE_VERSION_RE.search((completed.stdout + "\n" + completed.stderr)[:500])
    return match.group(1) if match else None


def _probe_versions() -> dict[str, str | None]:
    claude = shutil.which("claude")
    return {
        "claude_code_version": _safe_version([claude, "--version"]) if claude else None,
        "cli_proxy_api_version": None,
    }


def _normalized_utc_timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sanitize_route(route: Any) -> dict[str, str] | None:
    if not isinstance(route, dict):
        return None
    model = route.get("model")
    turn = route.get("turn")
    scope = route.get("scope")
    agent_key = route.get("agent_key")
    if model not in ROUTE_MODELS or turn not in ROUTE_TURNS or scope not in {"main", "subagent"}:
        return None
    if scope == "subagent":
        if not isinstance(agent_key, str) or not HASHED_AGENT_KEY_RE.fullmatch(agent_key):
            return None
    elif agent_key is not None:
        return None
    sanitized = {"model": model, "turn": turn, "scope": scope}
    if agent_key is not None:
        sanitized["agent_key"] = agent_key
    return sanitized


def _sanitize_summary(summary: Any) -> dict[str, Any] | None:
    if not isinstance(summary, dict):
        return None
    probe = summary.get("probe")
    timestamp = _normalized_utc_timestamp(summary.get("timestamp_utc"))
    if (
        probe not in LIVE_COMMANDS
        or timestamp is None
        or summary.get("skill_version") != SKILL_VERSION
        or summary.get("probe_schema_version") != PROBE_SUMMARY_SCHEMA_VERSION
    ):
        return None
    routes = []
    for route in summary.get("routes", []):
        sanitized = _sanitize_route(route)
        if sanitized is not None:
            routes.append(sanitized)
    versions: dict[str, str | None] = {}
    for key in ("claude_code_version", "cli_proxy_api_version"):
        value = summary.get(key)
        if value is None:
            versions[key] = None
        elif isinstance(value, str) and SAFE_VERSION_RE.fullmatch(value):
            versions[key] = value
        else:
            return None
    return {
        "timestamp_utc": timestamp,
        "skill_version": SKILL_VERSION,
        "probe_schema_version": PROBE_SUMMARY_SCHEMA_VERSION,
        **versions,
        "probe": probe,
        "verified": bool(summary.get("verified")),
        "gateway_ingress_model_routing_verified": bool(summary.get("gateway_ingress_model_routing_verified")),
        "upstream_provider_verified": False,
        "routes": routes,
    }


def _routing_probe_summary(
    name: str, result: dict[str, Any], *, timestamp_utc: str | None = None,
    versions: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    routes = []
    for route in result.get("evidence", []):
        sanitized = _sanitize_route(route)
        if sanitized is not None:
            routes.append(sanitized)
    order = {"gpt-5.6-luna": 0, "gpt-5.6-terra": 1, "gpt-5.6-sol": 2}
    routes.sort(key=lambda route: (order[route["model"]], 0 if route["turn"] == "initial" else 1))
    return {
        "timestamp_utc": timestamp_utc or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "skill_version": SKILL_VERSION,
        "probe_schema_version": PROBE_SUMMARY_SCHEMA_VERSION,
        **(versions if versions is not None else _probe_versions()),
        "probe": name,
        "verified": bool(result.get("verified")),
        "gateway_ingress_model_routing_verified": bool(result.get("gateway_ingress_model_routing_verified")),
        "upstream_provider_verified": False,
        "routes": routes,
    }


def _read_routing_state(state_path: Path) -> dict[str, Any]:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(state, dict) or state.get("state_schema_version") != ROUTING_STATE_SCHEMA_VERSION:
        return {}
    return state


def _summary_timestamp(summary: dict[str, Any]) -> datetime | None:
    value = _normalized_utc_timestamp(summary.get("timestamp_utc"))
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _summary_is_recent(summary: dict[str, Any], now: datetime) -> bool:
    timestamp = _summary_timestamp(summary)
    if timestamp is None:
        return False
    age_seconds = (now - timestamp).total_seconds()
    return -300 <= age_seconds <= ALIAS_RESULT_MAX_AGE_HOURS * 3600


def _matching_alias_summaries(
    summaries: dict[str, dict[str, Any]], *, now: datetime | None = None,
) -> dict[str, dict[str, Any]] | None:
    checked: dict[str, dict[str, Any]] = {}
    current = now or datetime.now(timezone.utc)
    version_key: tuple[Any, ...] | None = None
    for command in LIVE_ALIAS_COMMANDS:
        summary = _sanitize_summary(summaries.get(command))
        spec = ALIAS_PROBE_SPECS[command]
        if (
            summary is None
            or summary.get("probe") != command
            or not summary.get("verified")
            or not summary.get("gateway_ingress_model_routing_verified")
            or not _summary_is_recent(summary, current)
        ):
            return None
        routes = summary.get("routes", [])
        expected_routes = {
            (spec["model"], "initial", "subagent"),
            (spec["model"], "resume", "subagent"),
        }
        if len(routes) != 2 or {(row["model"], row["turn"], row["scope"]) for row in routes} != expected_routes:
            return None
        if len({row.get("agent_key") for row in routes}) != 1:
            return None
        candidate_key = (
            summary.get("skill_version"), summary.get("probe_schema_version"),
            summary.get("claude_code_version"), summary.get("cli_proxy_api_version"),
        )
        if version_key is None:
            version_key = candidate_key
        elif candidate_key != version_key:
            return None
        checked[command] = summary
    return checked


def _assemble_alias_summary(summaries: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    matched = _matching_alias_summaries(summaries)
    if matched is None:
        return None
    first = matched[LIVE_ALIAS_COMMANDS[0]]
    timestamps = [_summary_timestamp(matched[command]) for command in LIVE_ALIAS_COMMANDS]
    if any(value is None for value in timestamps):
        return None
    routes = [route for command in LIVE_ALIAS_COMMANDS for route in matched[command]["routes"]]
    order = {"gpt-5.6-luna": 0, "gpt-5.6-terra": 1, "gpt-5.6-sol": 2}
    routes.sort(key=lambda route: (order[route["model"]], 0 if route["turn"] == "initial" else 1))
    if len({route.get("agent_key") for route in routes}) != 3:
        return None
    oldest = min(value for value in timestamps if value is not None)
    return {
        "timestamp_utc": oldest.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "skill_version": first["skill_version"],
        "probe_schema_version": first["probe_schema_version"],
        "claude_code_version": first["claude_code_version"],
        "cli_proxy_api_version": first["cli_proxy_api_version"],
        "probe": "live-aliases",
        "verified": True,
        "gateway_ingress_model_routing_verified": True,
        "upstream_provider_verified": False,
        "routes": routes,
    }


def _alias_result_from_aggregate(result: dict[str, Any], command: str) -> dict[str, Any] | None:
    alias_results = result.get("alias_results")
    if isinstance(alias_results, dict) and isinstance(alias_results.get(command), dict):
        return alias_results[command]
    spec = ALIAS_PROBE_SPECS[command]
    evidence = [row for row in result.get("evidence", []) if row.get("model") == spec["model"]]
    if len(evidence) != 2:
        return None
    return {
        "verified": bool(result.get("verified")),
        "gateway_ingress_model_routing_verified": bool(result.get("gateway_ingress_model_routing_verified")),
        "evidence": evidence,
    }


def write_routing_probe_state(state_path: Path, name: str, result: dict[str, Any]) -> dict[str, Any]:
    existing = _read_routing_state(state_path)
    timestamp_utc = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    versions = _probe_versions()
    summary = _routing_probe_summary(name, result, timestamp_utc=timestamp_utc, versions=versions)
    preserved = _sanitize_summary(existing.get("last_successful_aliases"))
    if preserved is not None and (preserved.get("probe") != "live-aliases" or not preserved.get("verified")):
        preserved = None

    alias_summaries: dict[str, dict[str, Any]] = {}
    stored_aliases = existing.get("last_successful_alias_results")
    if isinstance(stored_aliases, dict):
        for command in LIVE_ALIAS_COMMANDS:
            sanitized = _sanitize_summary(stored_aliases.get(command))
            if sanitized is not None and sanitized.get("probe") == command and sanitized.get("verified"):
                alias_summaries[command] = sanitized

    commands_to_update = LIVE_ALIAS_COMMANDS if name == "live-aliases" else (name,) if name in LIVE_ALIAS_COMMANDS else ()
    for command in commands_to_update:
        alias_result = _alias_result_from_aggregate(result, command) if name == "live-aliases" else result
        if alias_result is None:
            continue
        alias_summary = _routing_probe_summary(
            command, alias_result, timestamp_utc=timestamp_utc, versions=versions,
        )
        if alias_summary["verified"] and alias_summary["gateway_ingress_model_routing_verified"]:
            alias_summaries[command] = alias_summary

    assembled = _assemble_alias_summary(alias_summaries)
    last_successful_aliases = assembled or preserved
    state = {
        "state_schema_version": ROUTING_STATE_SCHEMA_VERSION,
        "last_attempt": summary,
        "last_successful_alias_results": alias_summaries,
        "last_successful_aliases": last_successful_aliases,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{state_path.name}.", suffix=".tmp", dir=state_path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, state_path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    return state


def _structural_matches(pattern: re.Pattern[str], text: str) -> list[re.Match[str]]:
    matches: list[re.Match[str]] = []
    for match in pattern.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        prefix = text[line_start:match.start()].lstrip()
        if prefix.startswith(("data:", "event:", ":")):
            continue
        matches.append(match)
    return matches


def _latest_structural_segment(text: str) -> tuple[int | None, str]:
    statuses = structural_http_matches(text)
    if not statuses:
        return None, text
    latest = statuses[-1]
    prior_end = statuses[-2].end() if len(statuses) > 1 else 0
    prefix = text[prior_end:latest.start()]
    candidates: list[int] = []
    for pattern in (REQUESTED_MODEL_RE, RESOLVED_MODEL_RE, PROVIDER_RE):
        matches = _structural_matches(pattern, prefix)
        if matches:
            absolute = prior_end + matches[-1].start()
            candidates.append(text.rfind("\n", 0, absolute) + 1)
    start = min(candidates) if candidates else latest.start()
    return int(latest.group(1)), text[start:]


def parse_structural_log(text: str) -> dict[str, Any]:
    status, segment = _latest_structural_segment(text)
    requested = _structural_matches(REQUESTED_MODEL_RE, segment)
    resolved = _structural_matches(RESOLVED_MODEL_RE, segment)
    providers = _structural_matches(PROVIDER_RE, segment)
    return redact({
        "requested_model": requested[-1].group(1) if requested else None,
        "resolved_model": resolved[-1].group(1) if resolved else None,
        "resolved_provider": providers[-1].group(1) if providers else None,
        "http_status": status,
        "subagent_scope": "x-claude-code-agent-id" in segment.lower(),
        "classification": classify_error(status, body=segment, sse=segment),
    })


MCP_SERVER_SOURCE = r'''import json, sys
CAPTURE_PATH = sys.argv[1]
TOOLS = [
    {"name": f"tool_{i:03d}", "description": f"Synthetic zero-quota tool {i}",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}
    for i in range(176)
]
for raw in sys.stdin:
    try:
        request = json.loads(raw)
    except Exception:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "claudex-zero-quota", "version": "1"}}
    elif method == "tools/list":
        with open(CAPTURE_PATH, "a", encoding="utf-8") as capture:
            capture.write(json.dumps({"listed_tools": len(TOOLS)}) + "\n")
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "not used"}]}
    else:
        if "id" not in request:
            continue
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}), flush=True)
'''


def _agent_tool_call_shapes(payload: Any) -> list[dict[str, Any]]:
    shapes: list[dict[str, Any]] = []
    stack: list[Any] = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            tool_name = str(value.get("name", "")).lower()
            if value.get("type") == "tool_use" and tool_name in {"agent", "sendmessage"}:
                tool_input = value.get("input") if isinstance(value.get("input"), dict) else {}
                call_id = value.get("id")
                shape: dict[str, Any] = {
                    "tool": "agent" if tool_name == "agent" else "send_message",
                    "call_key": hashlib.sha256(call_id.encode()).hexdigest()[:12] if isinstance(call_id, str) else None,
                    "input_keys": sorted(str(key) for key in tool_input),
                }
                if tool_name == "agent":
                    subagent_type = tool_input.get("subagent_type")
                    model = tool_input.get("model")
                    run_in_background = tool_input.get("run_in_background")
                    shape.update({
                        "subagent_type": subagent_type if subagent_type in {"control", "luna", "terra", "sol"} else None,
                        "model": model if model in {"haiku", "sonnet", "opus", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"} else None,
                        "run_in_background": run_in_background if isinstance(run_in_background, bool) else None,
                    })
                else:
                    target = tool_input.get("to")
                    shape["target_key"] = hashlib.sha256(target.encode()).hexdigest()[:12] if isinstance(target, str) and target else None
                shapes.append(shape)
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return shapes


class _GatewayHandler(BaseHTTPRequestHandler):
    server_version = "ClaudexZeroQuota/1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return (
            self.headers.get("x-api-key") == "sk-zero-quota-local"
            or self.headers.get("authorization") == "Bearer sk-dummy"
        )

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(401, {"error": {"type": "authentication_error"}})
        elif self.path.startswith("/v1/models"):
            self._json(200, {"data": [{"id": model} for model in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol")]})
        else:
            self._json(404, {"error": {"type": "not_found_error"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(401, {"error": {"type": "authentication_error"}})
            return
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            self._json(400, {"error": {"type": "invalid_request_error"}})
            return
        if self.path.startswith("/v1/messages/count_tokens"):
            self._json(200, {"input_tokens": 1000})
            return
        if self.path.startswith("/v1/messages"):
            tools = request.get("tools") or []
            structural = []
            for tool in tools:
                name = tool.get("name") or tool.get("type") or "unknown"
                structural.append({"name": name, "deferred": bool(tool.get("defer_loading", False))})
            tool_references: list[str] = []
            stack: list[Any] = [request.get("system"), request.get("messages")]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    if value.get("type") == "tool_reference" and isinstance(value.get("tool_name"), str):
                        tool_references.append(value["tool_name"])
                    stack.extend(value.values())
                elif isinstance(value, list):
                    stack.extend(value)
            beta_header = self.headers.get("anthropic-beta", "")
            self.server.captures.append({  # type: ignore[attr-defined]
                "model": request.get("model"),
                "message_count": len(request.get("messages") or []),
                "subagent_scope": bool(self.headers.get("x-claude-code-agent-id")),
                "agent_tool_calls": _agent_tool_call_shapes(request.get("messages") or []),
                "tools": structural,
                "tool_references": tool_references,
                "tool_search_beta": "tool-search" in beta_header or "advanced-tool-use" in beta_header,
            })
            self._json(200, {
                "id": "msg_zero_quota", "type": "message", "role": "assistant",
                "model": request.get("model", "gpt-5.6-sol"),
                "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            })
            return
        self._json(404, {"error": {"type": "not_found_error"}})


def _terminate_fixture_descendants(root_pid: int, fixture_root: Path) -> None:
    """Stop only surviving descendants whose command line names this fixture."""
    if os.name != "nt":
        return
    env = os.environ.copy()
    env["CLAUDEX_FIXTURE_ROOT_PID"] = str(root_pid)
    env["CLAUDEX_FIXTURE_ROOT"] = str(fixture_root.resolve())
    script = r'''$rootPid = [int]$env:CLAUDEX_FIXTURE_ROOT_PID
$fixture = [IO.Path]::GetFullPath($env:CLAUDEX_FIXTURE_ROOT)
$queue = New-Object 'System.Collections.Generic.Queue[int]'
$queue.Enqueue($rootPid)
$seen = New-Object 'System.Collections.Generic.HashSet[int]'
$owned = New-Object 'System.Collections.Generic.List[int]'
while ($queue.Count -gt 0) {
    $parent = $queue.Dequeue()
    if (-not $seen.Add($parent)) { continue }
    foreach ($child in @(Get-CimInstance Win32_Process -Filter ("ParentProcessId = {0}" -f $parent) -ErrorAction SilentlyContinue)) {
        $pidValue = [int]$child.ProcessId
        $queue.Enqueue($pidValue)
        if (-not [string]::IsNullOrWhiteSpace([string]$child.CommandLine) -and
            $child.CommandLine.IndexOf($fixture, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $owned.Add($pidValue)
        }
    }
}
for ($i = $owned.Count - 1; $i -ge 0; $i--) {
    Stop-Process -Id $owned[$i] -Force -ErrorAction SilentlyContinue
}'''
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env=env, text=True, capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def _terminate_exact_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], text=True, capture_output=True, check=False)
    else:
        try:
            os.killpg(process.pid, 9)
        except ProcessLookupError:
            pass


def _run_fixture_process(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout: int,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "cwd": cwd, "env": env, "text": True,
        "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_exact_process_tree(process)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from exc
    finally:
        _terminate_fixture_descendants(process.pid, cwd)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _run_claude_tool_case(claude: str, base_url: str, temp: Path, enabled: bool) -> tuple[dict[str, Any] | None, str | None]:
    case_name = "enabled" if enabled else "disabled"
    mcp_script = temp / "mcp_server.py"
    mcp_script.write_text(MCP_SERVER_SOURCE, encoding="utf-8")
    mcp_capture = temp / f"mcp-{case_name}.jsonl"
    mcp_config = temp / f"mcp-{case_name}.json"
    mcp_config.write_text(json.dumps({"mcpServers": {"bulk": {"command": sys.executable, "args": [str(mcp_script), str(mcp_capture)]}}}), encoding="utf-8")
    settings = temp / f"settings-{case_name}.json"
    settings.write_text("{}", encoding="utf-8")
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("ANTHROPIC_") or key.startswith("CLAUDE_CODE_") or key == "ENABLE_TOOL_SEARCH":
            env.pop(key, None)
    isolated_home = temp / ("home-true" if enabled else "home-false")
    isolated_home.mkdir()
    env.update({
        "HOME": str(isolated_home), "USERPROFILE": str(isolated_home),
        "APPDATA": str(isolated_home / "AppData"), "LOCALAPPDATA": str(isolated_home / "LocalAppData"),
        "CLAUDE_CONFIG_DIR": str(isolated_home / ".claude"),
        "ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_API_KEY": "sk-zero-quota-local",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "gpt-5.6-luna",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "gpt-5.6-terra",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "gpt-5.6-sol",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        "ENABLE_TOOL_SEARCH": "true" if enabled else "false",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    command = [
        claude, "-p", "--output-format", "json", "--no-session-persistence",
        "--disable-slash-commands", "--no-chrome", "--setting-sources", "",
        "--model", "opus", "--settings", str(settings), "--strict-mcp-config",
        "--mcp-config", str(mcp_config), "--", "Return exactly OK.",
    ]
    try:
        completed = _run_fixture_process(command, cwd=temp, env=env, timeout=45)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"Claude CLI invocation failed structurally: {type(exc).__name__}"
    if completed.returncode != 0:
        return None, f"Claude CLI exited {completed.returncode} before completing the fake-gateway exchange"
    listed_counts = []
    if mcp_capture.exists():
        for line in mcp_capture.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                listed_counts.append(int(json.loads(line)["listed_tools"]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    if not listed_counts:
        return None, "Claude CLI did not request the disposable MCP tool catalog"
    return {"advertised": listed_counts[-1], "list_calls": len(listed_counts)}, None


def _retryable_cleanup_error(exc: OSError) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if exc.errno in {errno.EACCES, errno.EBUSY, errno.ENOTEMPTY, errno.EPERM}:
        return True
    return os.name == "nt" and getattr(exc, "winerror", None) in {5, 32, 33, 145, 183}


def _remove_temp_tree(path: Path, timeout_seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    delay = 0.05
    while True:
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            if not path.exists():
                return True
        except OSError as exc:
            if not _retryable_cleanup_error(exc):
                raise
        if not path.exists():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        delay = min(delay * 1.7, 1.0)


def run_local_tool_search_integration(total_tools: int = 176) -> dict[str, Any]:
    if total_tools != 176:
        return {"verified": False, "reason": "integration fixture is fixed at 176 advertised MCP tools", "quota_used": 0}
    claude = shutil.which("claude")
    if not claude:
        return {"verified": False, "reason": "Claude CLI is not installed", "quota_used": 0}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
    server.captures = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    temp = Path(tempfile.mkdtemp(prefix="claudex-tool-search-"))
    try:
        observations: dict[str, Any] = {}
        for enabled in (False, True):
            before = len(server.captures)  # type: ignore[attr-defined]
            mcp_observation, error = _run_claude_tool_case(claude, base_url, temp, enabled)
            captures = server.captures[before:]  # type: ignore[attr-defined]
            if error or not captures or mcp_observation is None:
                return {"verified": False, "reason": error or "Claude CLI produced no capturable message request", "quota_used": 0}
            request = captures[-1]
            catalog = [tool for tool in request["tools"] if str(tool["name"]).startswith("mcp__bulk__tool_")]
            request_tool_names = [str(tool["name"]) for tool in request["tools"]]
            search_tools = [tool for tool in request["tools"] if "toolsearch" in re.sub(r"[^a-z0-9]", "", str(tool["name"]).lower())]
            referenced_catalog = [name for name in request["tool_references"] if str(name).startswith("mcp__bulk__tool_")]
            advertised = int(mcp_observation["advertised"])
            eager = sum(not tool["deferred"] for tool in catalog)
            explicit_deferred = sum(tool["deferred"] for tool in catalog)
            deferred = max(explicit_deferred, len(set(referenced_catalog)))
            if search_tools and request["tool_search_beta"] and eager + deferred < advertised:
                deferred = advertised - eager
            observations[str(enabled).lower()] = {
                "eager": eager,
                "deferred": deferred,
                "catalog": advertised,
                "request_catalog": len(catalog),
                "tool_search_tools": len(search_tools),
                "tool_references": len(set(referenced_catalog)),
                "tool_search_beta": bool(request["tool_search_beta"]),
                "mcp_list_calls": int(mcp_observation["list_calls"]),
                "request_tool_count": len(request_tool_names),
            }
        disabled = observations["false"]
        enabled = observations["true"]
        conserved = (
            disabled["catalog"] == total_tools
            and enabled["catalog"] == total_tools
            and disabled["eager"] + disabled["deferred"] == disabled["catalog"]
            and enabled["eager"] + enabled["deferred"] == enabled["catalog"]
        )
        material = (
            conserved
            and disabled["eager"] >= int(total_tools * 0.9)
            and enabled["eager"] <= int(total_tools * 0.1)
            and enabled["tool_search_beta"]
            and (enabled["tool_search_tools"] >= 1 or enabled["tool_references"] > 0)
        )
        return {
            "verified": bool(material), "quota_used": 0, "advertised_tools": max(disabled["catalog"], enabled["catalog"]),
            "tool_search_disabled": disabled, "tool_search_enabled": enabled,
            "catalog_conserved": conserved, "material_reduction": material,
            "reason": None if material else "Captured Claude requests did not prove catalog conservation and material eager-schema reduction",
        }
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
        if not _remove_temp_tree(temp):
            raise RuntimeError(f"Disposable Claude CLI tool-search directory remained after the 15-second cleanup deadline: {temp}")


def run_local_bundle_syntax_integration() -> dict[str, Any]:
    claude = shutil.which("claude")
    if not claude:
        return {"verified": False, "reason": "Claude CLI is not installed", "quota_used": 0}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
    server.captures = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    temp = Path(tempfile.mkdtemp(prefix="claudex-bundle-syntax-"))
    try:
        isolated_home = temp / "home"
        isolated_home.mkdir()
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("ANTHROPIC_") or key.startswith("CLAUDE_CODE_") or key == "ENABLE_TOOL_SEARCH":
                env.pop(key, None)
        env.update({
            "HOME": str(isolated_home), "USERPROFILE": str(isolated_home),
            "APPDATA": str(isolated_home / "AppData"), "LOCALAPPDATA": str(isolated_home / "LocalAppData"),
            "CLAUDE_CONFIG_DIR": str(isolated_home / ".claude"),
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}",
            "ANTHROPIC_API_KEY": "sk-zero-quota-local",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "gpt-5.6-luna",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "gpt-5.6-terra",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "gpt-5.6-sol",
            "ENABLE_TOOL_SEARCH": "true",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        bundle_names = ("live-main-sol", "live-stable-control", *LIVE_ALIAS_COMMANDS)
        captured_by_probe: dict[str, int] = {}
        request_shapes: list[dict[str, Any]] = []
        for bundle_name in bundle_names:
            before = len(server.captures)  # type: ignore[attr-defined]
            bundle = executable_bundle(bundle_name, f"http://127.0.0.1:{server.server_address[1]}")
            try:
                completed = _run_fixture_process(bundle["command"], cwd=temp, env=env, timeout=45)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {"verified": False, "reason": f"Bundle syntax probe failed structurally for {bundle_name}: {type(exc).__name__}", "quota_used": 0}
            captures = server.captures[before:]  # type: ignore[attr-defined]
            captured_by_probe[bundle_name] = len(captures)
            request_shapes.extend(
                {
                    "probe": bundle_name,
                    "model": capture["model"],
                    "message_count": capture["message_count"],
                    "tool_count": len(capture["tools"]),
                    "subagent_scope": capture["subagent_scope"],
                }
                for capture in captures
            )
            if completed.returncode != 0 or not captures:
                return {
                    "verified": False,
                    "reason": f"Claude CLI exited {completed.returncode} without completing the {bundle_name} fake-gateway request",
                    "quota_used": 0,
                    "bundle_probes": list(bundle_names),
                    "captured_by_probe": captured_by_probe,
                }
        return {
            "verified": True,
            "quota_used": 0,
            "captured_requests": len(server.captures),  # type: ignore[attr-defined]
            "bundle_probes": list(bundle_names),
            "captured_by_probe": captured_by_probe,
            "request_shapes": request_shapes,
            "fixture_cwd": "isolated-temp",
            "cleanup_contract": "fixture directory removal is verified or the integration raises",
            "reason": None,
        }
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
        if not _remove_temp_tree(temp):
            raise RuntimeError(f"Disposable Claude CLI bundle-syntax directory remained after the 15-second cleanup deadline: {temp}")


def _walk_values(value: Any, key_names: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in key_names and isinstance(child, str):
                found.append(child)
            found.extend(_walk_values(child, key_names))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_values(child, key_names))
    return found


def _response_structure(status: int, content_type: str, raw: bytes, requested_model: str | None) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace")
    is_sse = "text/event-stream" in content_type.lower() or "event:" in text
    response_models: list[str] = []
    providers: list[str] = []
    fallback = False
    terminal = status < 300
    if is_sse:
        terminal = terminal_sse_seen(text)
        for record in parse_sse_records(text):
            payload = record.get("json")
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "message_start" and isinstance(payload.get("message"), dict):
                model = payload["message"].get("model")
                if isinstance(model, str):
                    response_models.append(model)
            response_models.extend(_walk_values(payload, {"resolved_model"}))
            providers.extend(_walk_values(payload, {"provider", "resolved_provider", "upstream_provider"}))
            if _walk_values(payload, {"type"}).count("fallback") > 0:
                fallback = True
    else:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            model = payload.get("model")
            if isinstance(model, str):
                response_models.append(model)
            response_models.extend(_walk_values(payload, {"resolved_model"}))
            providers.extend(_walk_values(payload, {"provider", "resolved_provider", "upstream_provider"}))
            fallback = "fallback" in _walk_values(payload, {"type"})
            terminal = status >= 300 or payload.get("stop_reason") is not None
    response_model = response_models[-1] if response_models else None
    if requested_model and response_model and response_model != requested_model:
        fallback = True
    classification = classify_error(status, body=text, sse=text if is_sse else "")
    if not terminal and classification.get("success"):
        classification = classify_error(status, body=text, sse=text, interrupted=True)
    return {
        "http_status": status,
        "terminal": bool(terminal),
        "success": bool(classification.get("success")),
        "classification": classification.get("category"),
        "response_model": response_model,
        "provider": providers[-1] if providers else None,
        "fallback": bool(fallback),
    }


class _ForwardingGatewayHandler(BaseHTTPRequestHandler):
    server_version = "ClaudexCaptureGateway/1"
    protocol_version = "HTTP/1.0"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def _forward(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length) if length else b""
        request_model: str | None = None
        message_count = 0
        agent_tool_calls: list[dict[str, Any]] = []
        if self.path.startswith("/v1/messages") and not self.path.startswith("/v1/messages/count_tokens"):
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                request_model = payload.get("model") if isinstance(payload.get("model"), str) else None
                message_count = len(payload.get("messages") or [])
                agent_tool_calls = _agent_tool_call_shapes(payload.get("messages") or [])
        agent_id = self.headers.get("x-claude-code-agent-id")
        agent_key = hashlib.sha256(agent_id.encode()).hexdigest()[:12] if agent_id else None
        headers = {
            key: value for key, value in self.headers.items()
            if key.lower() not in {"connection", "content-length", "host", "transfer-encoding", "accept-encoding"}
        }
        headers["Host"] = "127.0.0.1:8317"
        headers["Accept-Encoding"] = "identity"
        if body:
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection("127.0.0.1", 8317, timeout=125)
        raw_response = bytearray()
        try:
            connection.request(self.command, self.path, body=body or None, headers=headers)
            response = connection.getresponse()
            content_type = response.getheader("content-type", "")
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in {"connection", "content-length", "transfer-encoding", "content-encoding"}:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                raw_response.extend(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
            status = response.status
        except Exception:
            status = 502
            content_type = "application/json"
            failure = json.dumps({"error": {"type": "capture_gateway_error"}}).encode()
            raw_response.extend(failure)
            try:
                self.send_response(502)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(failure)))
                self.end_headers()
                self.wfile.write(failure)
            except Exception:
                pass
        finally:
            connection.close()
            self.close_connection = True
        if request_model is not None:
            response_shape = _response_structure(status, content_type, bytes(raw_response), request_model)
            with self.server.capture_lock:  # type: ignore[attr-defined]
                sequence = len(self.server.captures)  # type: ignore[attr-defined]
                self.server.captures.append({  # type: ignore[attr-defined]
                    "sequence": sequence,
                    "model": request_model,
                    "message_count": message_count,
                    "subagent_scope": agent_key is not None,
                    "agent_key": agent_key,
                    "agent_tool_calls": agent_tool_calls,
                    **response_shape,
                })


def executable_bundle(name: str, gateway_base_url: str | None = None) -> dict[str, Any]:
    launch = str(launch_script_path())
    empty_mcp = '{"mcpServers":{}}'
    common_args = [
        "-p", "--verbose", "--output-format", "stream-json", "--no-session-persistence",
        "--prompt-suggestions", "false", "--disable-slash-commands", "--no-chrome",
        "--setting-sources", "", "--strict-mcp-config",
        "--mcp-config", empty_mcp, "--settings", "{}", "--permission-mode", "dontAsk",
        "--max-budget-usd", "1", "--tools", "Agent,SendMessage",
    ]
    if name == "live-main-sol":
        prompt = "Return exactly MAIN_SOL_OK. Do not call tools."
        agents = None
        expected = [{"scope": "main", "model": "gpt-5.6-sol", "provider": EXPECTED_PROVIDER, "turn": "initial"}]
        launch_parameters: list[str] = []
    elif name == "live-stable-control":
        agents = {"control": {"description": "Routing control", "prompt": "Return the requested marker only.", "model": "opus"}}
        prompt = "Run one control sequence. Call Agent once with description 'Run control initial probe', subagent_type 'control', prompt 'CONTROL_INITIAL', and run_in_background true. Copy the active agent ID returned immediately. While that agent is active, immediately call SendMessage once with to set to that exact ID, summary 'Resume control routing probe', and message 'CONTROL_RESUME'. Then wait for the background agent's completion using the current tool-supported completion behavior. Do not call Agent again. Return CONTROL_INITIAL and CONTROL_RESUME."
        expected = [
            {"scope": "subagent", "model": "gpt-5.6-luna", "provider": EXPECTED_PROVIDER, "turn": "initial"},
            {"scope": "subagent", "model": "gpt-5.6-luna", "provider": EXPECTED_PROVIDER, "turn": "resume"},
        ]
        launch_parameters = ["-StableSubagentModel", "gpt-5.6-luna"]
    elif name in ALIAS_PROBE_SPECS:
        spec = ALIAS_PROBE_SPECS[name]
        alias = spec["subagent_type"]
        agents = {
            alias: {
                "description": f"{alias.title()} alias probe",
                "prompt": "Return the requested marker only.",
                "model": spec["inline_model"],
            },
        }
        prompt = (
            f"Run exactly one {alias} alias sequence. Call Agent exactly once with description "
            f"'{spec['description']}', subagent_type '{alias}', prompt '{spec['initial']}', and "
            "run_in_background true. Copy the active agent ID returned immediately. While that agent "
            f"is active, immediately call SendMessage exactly once with to set to that exact ID, summary "
            f"'{spec['summary']}', and message '{spec['resume']}'. Then wait for and collect the background "
            "agent's completion using the current tool-supported completion behavior. Do not call Agent "
            f"again and do not start any other agent. Return {spec['initial']} and {spec['resume']}."
        )
        expected = [
            {"scope": "subagent", "model": spec["model"], "provider": EXPECTED_PROVIDER, "turn": turn}
            for turn in ("initial", "resume")
        ]
        launch_parameters = []
    else:
        raise ValueError(f"{name} is an aggregate command and has no combined Claude CLI bundle")
    args = common_args + (["--agents", json.dumps(agents, separators=(",", ":"))] if agents else []) + ["--", prompt]
    encoded = base64.b64encode(json.dumps(args, ensure_ascii=False, separators=(",", ":")).encode()).decode()
    probe_parameters = ["-ProbeGateway", "-GatewayBaseUrl", gateway_base_url] if gateway_base_url else []
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", launch] + launch_parameters + probe_parameters + ["-EncodedArgv", encoded]
    return {
        "verified": False,
        "command": command,
        "claude_args": args,
        "timeout_seconds": 120,
        "expected_evidence": expected,
        "credentials_or_config_changes": False,
    }


def _bounded_redacted_cli_diagnostic(stdout: str, stderr: str, max_chars: int = 1200) -> dict[str, Any]:
    safe_lines: list[str] = []
    for line in stderr.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(marker in stripped for marker in ("MAIN_SOL_OK", "CONTROL_INITIAL", "CONTROL_RESUME", "_INITIAL", "_RESUME", "Return exactly")):
            continue
        sanitized = str(redact(stripped))
        sanitized = re.sub(r"(?i)\b[A-Z]:\\[^\s:]+", "<redacted-path>", sanitized)
        sanitized = re.sub(r"(?i)/(?:home|Users|tmp)/[^\s:]+", "<redacted-path>", sanitized)
        sanitized = re.sub(r"(?:\{.*\}|\[.*\])", "<redacted-structured-value>", sanitized)
        safe_lines.append(sanitized)
        if sum(len(value) + 1 for value in safe_lines) >= max_chars:
            break
    excerpt = "\n".join(safe_lines)[:max_chars]
    stdout_error_types: list[str] = []
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("is_error") is True:
            for key in ("type", "subtype"):
                value = payload.get(key)
                if isinstance(value, str):
                    stdout_error_types.append(value)
    return {
        "stdout_chars": len(stdout),
        "stderr_chars": len(stderr),
        "stderr_excerpt": excerpt or None,
        "stdout_error_types": sorted(set(stdout_error_types)),
        "truncated": len(excerpt) >= max_chars,
    }


def _run_bounded(command: list[str], cwd: Path, timeout_seconds: int) -> tuple[int, bool, str, str]:
    kwargs: dict[str, Any] = {"cwd": cwd, "text": True, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return process.returncode, False, stdout, stderr
    except subprocess.TimeoutExpired:
        _terminate_exact_process_tree(process)
        stdout, stderr = process.communicate()
        return process.returncode if process.returncode is not None else -9, True, stdout, stderr


def _capture_classification(capture: dict[str, Any]) -> str:
    if capture["subagent_scope"]:
        return "subagent"
    if capture["model"] == "gpt-5.6-sol":
        return "main-sol-controller"
    if capture["model"] == "gpt-5.6-luna":
        return "main-luna-auxiliary"
    return "main-unexpected"


def _sanitized_capture_summary(captures: list[dict[str, Any]], max_rows: int = 24) -> dict[str, Any]:
    rows = []
    for capture in sorted(captures, key=lambda item: item["sequence"])[:max_rows]:
        rows.append({
            "sequence": capture["sequence"],
            "model": capture["model"],
            "scope": "subagent" if capture["subagent_scope"] else "main",
            "agent_key": capture.get("agent_key"),
            "http_status": capture["http_status"],
            "terminal": capture["terminal"],
            "classification": _capture_classification(capture),
            "response_classification": capture["classification"],
            "response_model": capture["response_model"],
            "provider": capture["provider"],
        })
    return {"rows": rows, "truncated": len(captures) > max_rows, "total": len(captures), "max_rows": max_rows}


def _observed_route(capture: dict[str, Any], turn: str | None = None) -> dict[str, Any]:
    return {
        "sequence": capture["sequence"],
        "scope": "subagent" if capture["subagent_scope"] else "main",
        "model": capture["model"],
        "turn": turn,
        "agent_key": capture.get("agent_key"),
        "http_status": capture["http_status"],
        "terminal": capture["terminal"],
        "response_model": capture["response_model"],
        "provider": capture["provider"],
    }


def _validate_live_captures(
    name: str, captures: list[dict[str, Any]], returncode: int, timed_out: bool,
    cli_stdout: str = "", cli_stderr: str = "",
) -> dict[str, Any]:
    bundle = executable_bundle(name)
    errors: list[dict[str, Any]] = []
    if timed_out:
        errors.append({"type": "timeout", "timeout_seconds": 120})
    if returncode != 0:
        errors.append({"type": "cli_exit", "exit_code": returncode})
    for capture in captures:
        if not capture["terminal"]:
            errors.append({"type": "nonterminal", "sequence": capture["sequence"], "model": capture["model"]})
        if not capture["success"]:
            errors.append({"type": "terminal_failure", "sequence": capture["sequence"], "model": capture["model"], "classification": capture["classification"], "http_status": capture["http_status"]})
        if capture["fallback"]:
            errors.append({"type": "fallback", "sequence": capture["sequence"], "model": capture["model"], "response_model": capture["response_model"]})
        if capture["provider"] is not None and str(capture["provider"]).lower() != EXPECTED_PROVIDER:
            errors.append({"type": "provider_mismatch", "sequence": capture["sequence"], "model": capture["model"], "provider": capture["provider"]})

    main = [capture for capture in captures if not capture["subagent_scope"]]
    main_sol = sorted((capture for capture in main if capture["model"] == "gpt-5.6-sol"), key=lambda item: item["sequence"])
    successful_main_sol = [row for row in main_sol if row["terminal"] and row["success"] and not row["fallback"]]
    auxiliary_main_luna = sorted((capture for capture in main if capture["model"] == "gpt-5.6-luna"), key=lambda item: item["sequence"])
    unexpected_main = sorted({capture["model"] for capture in main if capture["model"] not in {"gpt-5.6-sol", "gpt-5.6-luna"}})
    if unexpected_main:
        errors.append({"type": "unexpected", "scope": "main", "models": unexpected_main})
    if len(auxiliary_main_luna) > MAX_AUXILIARY_MAIN_LUNA:
        errors.append({
            "type": "unexpected", "scope": "main-auxiliary", "model": "gpt-5.6-luna",
            "count": len(auxiliary_main_luna), "max_allowed": MAX_AUXILIARY_MAIN_LUNA,
        })
    subagents = [capture for capture in captures if capture["subagent_scope"]]
    evidence: list[dict[str, Any]] = []

    if name == "live-main-sol":
        if len(successful_main_sol) < 1:
            errors.append({"type": "missing", "scope": "main-controller", "model": "gpt-5.6-sol"})
        elif len(successful_main_sol) > 1:
            errors.append({"type": "duplicate", "scope": "main-controller", "model": "gpt-5.6-sol", "count": len(successful_main_sol)})
        if subagents:
            errors.append({"type": "unexpected", "scope": "subagent", "count": len(subagents)})
        for index, row in enumerate(successful_main_sol):
            evidence.append(_observed_route(row, "initial" if index == 0 else f"duplicate-{index + 1}"))
    else:
        if not successful_main_sol:
            errors.append({"type": "missing", "scope": "main-controller", "model": "gpt-5.6-sol"})
        expected_models = sorted({row["model"] for row in bundle["expected_evidence"]})
        expected_agent_types = ["control"] if name == "live-stable-control" else [ALIAS_PROBE_SPECS[name]["subagent_type"]]
        unexpected_models = sorted({capture["model"] for capture in subagents if capture["model"] not in expected_models})
        if unexpected_models:
            errors.append({"type": "unexpected", "scope": "subagent", "models": unexpected_models})

        unique_tool_calls: dict[str, dict[str, Any]] = {}
        for controller in main_sol:
            for call in controller.get("agent_tool_calls", []):
                key = call.get("call_key") or json.dumps(call, sort_keys=True)
                unique_tool_calls[key] = call
        agent_calls = [call for call in unique_tool_calls.values() if call.get("tool") == "agent"]
        send_calls = [call for call in unique_tool_calls.values() if call.get("tool") == "send_message"]
        for agent_type in expected_agent_types:
            matching_calls = [call for call in agent_calls if call.get("subagent_type") == agent_type]
            count = len(matching_calls)
            if count < 1:
                errors.append({"type": "missing", "scope": "agent-initial-call", "subagent_type": agent_type})
            elif count > 1:
                errors.append({"type": "duplicate", "scope": "agent-initial-call", "subagent_type": agent_type, "count": count})
            elif matching_calls[0].get("run_in_background") is not True:
                errors.append({"type": "unexpected", "scope": "agent-background-mode", "subagent_type": agent_type})
        unexpected_agent_types = sorted({str(call.get("subagent_type")) for call in agent_calls if call.get("subagent_type") not in expected_agent_types})
        if unexpected_agent_types:
            errors.append({"type": "unexpected", "scope": "agent-initial-call", "subagent_types": unexpected_agent_types})
        if len(send_calls) < len(expected_agent_types):
            errors.append({"type": "missing", "scope": "send-message-continuation", "expected": len(expected_agent_types), "observed": len(send_calls)})
        elif len(send_calls) > len(expected_agent_types):
            errors.append({"type": "duplicate", "scope": "send-message-continuation", "expected": len(expected_agent_types), "observed": len(send_calls)})

        model_agent_keys: dict[str, str] = {}
        for model in expected_models:
            rows = sorted((capture for capture in subagents if capture["model"] == model), key=lambda item: item["sequence"])
            for index, row in enumerate(rows):
                turn = "initial" if index == 0 else "resume" if index == 1 else f"unexpected-{index + 1}"
                if row["terminal"] and row["success"] and not row["fallback"]:
                    evidence.append(_observed_route(row, turn))
            if len(rows) < 2:
                errors.append({"type": "missing", "scope": "subagent", "model": model, "count": len(rows)})
                continue
            if len(rows) > 2:
                errors.append({"type": "duplicate", "scope": "subagent", "model": model, "count": len(rows)})
                continue
            agent_keys = {row.get("agent_key") for row in rows}
            if len(agent_keys) != 1 or None in agent_keys:
                errors.append({"type": "unexpected", "scope": "subagent-resume", "model": model, "agent_count": len(agent_keys)})
                continue
            model_agent_keys[model] = rows[0]["agent_key"]
        if len(model_agent_keys) == len(expected_models):
            target_counts = {key: sum(call.get("target_key") == key for call in send_calls) for key in model_agent_keys.values()}
            for key, count in target_counts.items():
                if count < 1:
                    errors.append({"type": "missing", "scope": "send-message-target", "agent_key": key})
                elif count > 1:
                    errors.append({"type": "duplicate", "scope": "send-message-target", "agent_key": key, "count": count})
            expected_target_keys = set(model_agent_keys.values())
            unexpected_target_keys = sorted({call.get("target_key") for call in send_calls if call.get("target_key") not in expected_target_keys}, key=str)
            if unexpected_target_keys:
                errors.append({"type": "unexpected", "scope": "send-message-target", "agent_keys": unexpected_target_keys})

    observed_ingress_routes = [
        _observed_route(capture)
        for capture in sorted(captures, key=lambda item: item["sequence"])
        if capture["terminal"] and capture["success"] and not capture["fallback"]
    ]
    provider_rows = [row for row in evidence if row.get("provider") is not None]
    upstream_provider_verified = len(provider_rows) == len(evidence) and bool(evidence)
    result = {
        "verified": not errors,
        "probe": name,
        "timeout_seconds": 120,
        "captured_message_requests": len(captures),
        "capture_summary": _sanitized_capture_summary(captures),
        "auxiliary_main_luna": [_observed_route(row) for row in auxiliary_main_luna],
        "evidence": evidence,
        "observed_ingress_routes": observed_ingress_routes,
        "errors": errors,
        "gateway_ingress_model_routing_observed": bool(observed_ingress_routes),
        "gateway_ingress_model_routing_verified": not errors,
        "upstream_provider_verified": upstream_provider_verified and not errors,
        "provider_observation": "captured from response metadata" if upstream_provider_verified else "not observable at gateway ingress without proxy debug",
        "limitation": None if upstream_provider_verified else "Successful rows prove only the routed model at gateway ingress and terminal response behavior, not the upstream provider selected inside CLIProxyAPI.",
        "credentials_or_config_changes": False,
    }
    if returncode != 0 and not captures:
        result["cli_diagnostic"] = _bounded_redacted_cli_diagnostic(cli_stdout, cli_stderr)
    return result


def _run_single_live_probe(name: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with socket.create_connection(("127.0.0.1", 8317), timeout=0.5):
            pass
    except OSError:
        return {"verified": False, "probe": name, "errors": [{"type": "gateway_closed", "message": "approved 8317 proxy must already be running"}], "credentials_or_config_changes": False}
    try:
        validation = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(launch_script_path()), "-ValidateGatewayOnly", "-RequireExistingGateway", "-GatewayBaseUrl", APPROVED_UPSTREAM],
            text=True, capture_output=True, timeout=20, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"verified": False, "probe": name, "errors": [{"type": "timeout", "timeout_seconds": 120, "stage": "gateway_validation"}], "credentials_or_config_changes": False}
    if validation.returncode != 0:
        return {"verified": False, "probe": name, "errors": [{"type": "gateway_validation", "message": "approved owner identity or authenticated inventory validation failed"}], "credentials_or_config_changes": False}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ForwardingGatewayHandler)
    server.captures = []  # type: ignore[attr-defined]
    server.capture_lock = threading.Lock()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    temp = Path(tempfile.mkdtemp(prefix=f"claudex-{name}-"))
    try:
        gateway = f"http://127.0.0.1:{server.server_address[1]}"
        bundle = executable_bundle(name, gateway)
        remaining = max(1, int(bundle["timeout_seconds"] - (time.monotonic() - started)))
        returncode, timed_out, cli_stdout, cli_stderr = _run_bounded(bundle["command"], temp, remaining)
        with server.capture_lock:  # type: ignore[attr-defined]
            captures = list(server.captures)  # type: ignore[attr-defined]
        return _validate_live_captures(name, captures, returncode, timed_out, cli_stdout, cli_stderr)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
        if not _remove_temp_tree(temp):
            raise RuntimeError(f"Disposable live-probe directory remained after the 15-second cleanup deadline: {temp}")


def run_live_aliases_probe(cooldown_seconds: float = LIVE_ALIAS_COOLDOWN_SECONDS) -> dict[str, Any]:
    alias_results: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    observed: list[dict[str, Any]] = []
    for index, command in enumerate(LIVE_ALIAS_COMMANDS):
        result = _run_single_live_probe(command)
        alias_results[command] = result
        evidence.extend(result.get("evidence", []))
        observed.extend(result.get("observed_ingress_routes", []))
        if not result.get("verified"):
            errors.append({"type": "alias_probe_failed", "probe": command, "errors": result.get("errors", [])})
        if index + 1 < len(LIVE_ALIAS_COMMANDS) and cooldown_seconds > 0:
            time.sleep(min(cooldown_seconds, 5.0))
    all_verified = len(alias_results) == len(LIVE_ALIAS_COMMANDS) and all(
        alias_results[command].get("verified") and alias_results[command].get("probe") == command
        for command in LIVE_ALIAS_COMMANDS
    )
    ingress_verified = all_verified and all(
        alias_results[command].get("gateway_ingress_model_routing_verified") for command in LIVE_ALIAS_COMMANDS
    )
    provider_verified = all_verified and all(
        alias_results[command].get("upstream_provider_verified") for command in LIVE_ALIAS_COMMANDS
    )
    return {
        "verified": bool(all_verified and ingress_verified),
        "probe": "live-aliases",
        "alias_results": alias_results,
        "evidence": evidence,
        "observed_ingress_routes": observed,
        "errors": errors,
        "gateway_ingress_model_routing_observed": bool(observed),
        "gateway_ingress_model_routing_verified": bool(ingress_verified),
        "upstream_provider_verified": bool(provider_verified),
        "provider_observation": "captured from every alias response metadata" if provider_verified else "not observable for every alias at gateway ingress without proxy debug",
        "limitation": None if provider_verified else "Successful rows prove only each routed model at gateway ingress and terminal response behavior, not the upstream provider selected inside CLIProxyAPI.",
        "credentials_or_config_changes": False,
        "process_isolation": "one Claude CLI process per alias",
        "cooldown_seconds": min(max(cooldown_seconds, 0.0), 5.0),
    }


def run_live_probe(name: str) -> dict[str, Any]:
    if name == "live-aliases":
        return run_live_aliases_probe()
    return _run_single_live_probe(name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    local = sub.add_parser("local-tool-search"); local.add_argument("--tools", type=int, default=176)
    sub.add_parser("local-bundle-syntax")
    structural = sub.add_parser("structural-log"); structural.add_argument("log_path")
    for name in ("live-main-sol", "live-stable-control", *LIVE_ALIAS_COMMANDS, "live-aliases"):
        command = sub.add_parser(name)
        command.add_argument("--approve-live-model-calls", action="store_true")
        command.add_argument("--state-path", type=Path, default=default_routing_state_path())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "local-tool-search":
        result = run_local_tool_search_integration(args.tools); print(json.dumps(result, sort_keys=True)); return 0 if result.get("verified") else 3
    if args.command == "local-bundle-syntax":
        result = run_local_bundle_syntax_integration(); print(json.dumps(result, sort_keys=True)); return 0 if result.get("verified") else 3
    if args.command == "structural-log":
        print(json.dumps(parse_structural_log(Path(args.log_path).read_text(encoding="utf-8", errors="replace")), sort_keys=True)); return 0
    if args.command in LIVE_COMMANDS:
        if not args.approve_live_model_calls:
            print(json.dumps({"error": "live model calls require --approve-live-model-calls"}, sort_keys=True)); return 64
        result = run_live_probe(args.command)
        state_persisted = False
        try:
            write_routing_probe_state(args.state_path, args.command, result)
            state_persisted = True
        except OSError as exc:
            result["state_write_error"] = type(exc).__name__
        result["state_persisted"] = state_persisted
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("verified") and state_persisted else 3
    return 64


if __name__ == "__main__":
    sys.exit(main())
