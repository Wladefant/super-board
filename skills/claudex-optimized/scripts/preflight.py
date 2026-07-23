#!/usr/bin/env python3
"""Zero-quota policy validation, budget checks, and redacted recovery helpers."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

SENSITIVE_NORMALIZED_KEYS = {
    "prompt", "system", "content", "messages", "tools", "toolschema", "toolschemas",
    "apikey", "authtoken", "authorization", "credential", "credentials", "secret",
    "email", "sessionid", "agentid", "requestid", "rawid", "accesstoken",
    "refreshtoken", "password", "clientsecret", "privatekey", "bearertoken",
}
SENSITIVE_KEY_FRAGMENTS = {
    "apikey", "authtoken", "bearertoken", "accesstoken", "refreshtoken", "password",
    "passwd", "passphrase", "clientsecret", "privatekey", "credential", "secret",
}
ID_RE = re.compile(r"\b(?:req|msg|agent|session|sesn|toolu|call|run)_[A-Za-z0-9_-]+\b", re.I)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
SECRET_PATTERNS = [
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", re.I),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", re.I),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\b(?:sk|key|token)-[A-Za-z0-9._-]{6,}\b", re.I),
    re.compile(r"(?i)\b(?:access[_-]?token|refresh[_-]?token|auth[_-]?token|api[_-]?key|client[_-]?secret|password|passwd|passphrase)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"),
]
HTTP_RE = re.compile(r"\bHTTP(?:/\S+)?\s+(\d{3})\b", re.I)
NON_RETRYABLE = {"context", "auth", "unknown_model"}


def default_policy_path() -> Path:
    return Path(__file__).resolve().parent.parent / "references" / "policy.json"


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    with (Path(path) if path else default_policy_path()).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_policy(policy: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    route = policy.get("route", {})
    for key, expected in {
        "envelope_tokens": 272000, "warning_tokens": 180000, "admission_tokens": 190000,
        "rotation_tokens": 208000, "hard_block_above_tokens": 208000, "reserve_tokens": 64000,
    }.items():
        if route.get(key) != expected:
            errors.append(f"route.{key} must be {expected}")
    for role, (model, alias) in {
        "luna": ("gpt-5.6-luna", "haiku"), "terra": ("gpt-5.6-terra", "sonnet"),
        "sol": ("gpt-5.6-sol", "opus"),
    }.items():
        value = policy.get("roles", {}).get(role, {})
        if value.get("model") != model or value.get("alias") != alias:
            errors.append(f"roles.{role} must map {alias} to {model}")
    for name, allowed in {
        "research-readonly": ["Read", "Glob", "Grep", "WebSearch", "WebFetch"],
        "implementation-local": ["Read", "Glob", "Grep", "Edit", "Write", "Bash"],
        "review-readonly": ["Read", "Glob", "Grep", "read-only Bash commands"],
        "routing-probe": ["agent or model invocation surface", "metadata inspection", "log inspection"],
    }.items():
        if policy.get("tool_profiles", {}).get(name, {}).get("allowed") != allowed:
            errors.append(f"tool_profiles.{name}.allowed does not match the contract")
    forbidden = "\n".join(policy.get("forbidden_targets", [])).lower()
    for required in ("settings.json", "config.json", "auth", "credential", "global claude"):
        if required not in forbidden:
            errors.append(f"forbidden_targets must cover {required}")
    antigravity = policy.get("antigravity", {})
    if not all(antigravity.get(key) is True for key in (
        "official_handoff_only", "third_party_oauth_prohibited", "never_invoke_authenticate_or_proxy"
    )):
        errors.append("Antigravity must remain official-handoff-only")
    return errors


def budget_verdict(
    estimated_tokens: int | None, *, eager_tools: int = 0, unknown: bool = False,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    route = (policy or load_policy())["route"]
    unknown = unknown or estimated_tokens is None
    reported_tokens = None if unknown else estimated_tokens
    if unknown:
        if eager_tools >= route["unknown_block_eager_tools"]:
            verdict = "block"
        elif eager_tools > route["unknown_large_eager_tools"]:
            verdict = "rotate"
        else:
            verdict = "unknown"
    elif estimated_tokens < route["warning_tokens"]:
        verdict = "admit"
    elif estimated_tokens <= route["admission_tokens"]:
        verdict = "warn"
    elif estimated_tokens <= route["rotation_tokens"]:
        verdict = "rotate"
    else:
        verdict = "block"
    return {
        "verdict": verdict,
        "estimated_tokens": reported_tokens,
        "estimated": not unknown,
        "source": "unknown" if unknown else "structural_estimate",
        "eager_tools": eager_tools,
        "route_envelope": route["envelope_tokens"],
        "reserved_headroom": route["reserve_tokens"],
    }


def parse_sse_records(sse: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    event: str | None = None
    data: list[str] = []

    def flush() -> None:
        nonlocal event, data
        if event is not None or data:
            joined = "\n".join(data)
            parsed: Any = None
            if joined.strip() != "[DONE]":
                try:
                    parsed = json.loads(joined)
                except (json.JSONDecodeError, TypeError):
                    parsed = None
            records.append({"event": event, "data": joined, "json": parsed})
        event = None
        data = []

    for raw_line in sse.splitlines():
        line = raw_line.rstrip("\r")
        if line == "":
            flush()
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    flush()
    return records


def terminal_sse_seen(sse: str) -> bool:
    for record in parse_sse_records(sse):
        if record["event"] == "message_stop":
            return True
        if record["data"].strip() == "[DONE]":
            return True
        payload = record["json"]
        if isinstance(payload, dict) and payload.get("type") == "message_stop":
            return True
    return False


def sse_error_seen(sse: str) -> bool:
    for record in parse_sse_records(sse):
        if record["event"] == "error":
            return True
        payload = record["json"]
        if isinstance(payload, dict) and payload.get("type") == "error":
            return True
    return False


def sse_error_text(sse: str) -> str:
    values: list[str] = []
    for record in parse_sse_records(sse):
        payload = record["json"]
        if record["event"] == "error" or (isinstance(payload, dict) and payload.get("type") == "error"):
            if payload is not None:
                values.append(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            else:
                values.append(record["data"])
    return "\n".join(values)


def non_sse_text(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if not line.startswith(("event:", "data:", ":"))
    )


def classify_error(
    http_status: int | None, *, body: str = "", sse: str = "", interrupted: bool = False,
) -> dict[str, Any]:
    body_text = non_sse_text(body) if sse and body == sse else body
    text = f"{body_text}\n{sse_error_text(sse)}".lower()
    sse_error = sse_error_seen(sse)
    if "context_length_exceeded" in text or "exceeds the context window" in text:
        category, retryable = "context", False
    elif "auth_unavailable" in text or "authentication_error" in text or http_status in (401, 403):
        category, retryable = "auth", False
    elif http_status == 429 or "rate_limit" in text or "quota" in text:
        category, retryable = "quota", True
    elif "unknown provider" in text or "unknown model" in text or "model_not_found" in text:
        category, retryable = "unknown_model", False
    elif interrupted or (http_status == 200 and sse and not terminal_sse_seen(sse)):
        category, retryable = "interrupted", True
    elif (http_status is None or 200 <= http_status < 300) and not sse_error:
        category, retryable = "success", False
    else:
        category, retryable = "unknown", http_status is None or http_status >= 500
    return {"category": category, "retryable": retryable, "http_status": http_status, "sse_error": sse_error, "success": category == "success"}


def recovery_for(classification: Mapping[str, Any]) -> dict[str, Any]:
    category = classification.get("category", "unknown")
    actions = {
        "context": "Reduce eager tools and messages, rotate context, then preflight below 190000 tokens.",
        "auth": "Stop unchanged retries; use an already approved eligible route or wait. Do not edit credentials or restart the daemon.",
        "quota": "Honor cooldown or select the next approved quota lane without changing authentication.",
        "unknown_model": "Keep the model disabled and run a bounded zero-tool capability probe using an exact discovered ID.",
        "interrupted": "Treat partial output as untrusted; retry only if the operation is idempotent.",
        "multi_request_log": "Split the log into one correlated request/response segment before recovery classification.",
        "success": "No recovery action required.",
        "unknown": "Stop and inspect redacted structural metadata before retrying.",
    }
    return {"category": category, "retryable": bool(classification.get("retryable", False)), "action": actions.get(category, actions["unknown"])}


def normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def sensitive_key(key: str) -> bool:
    normalized = normalize_key(key)
    return normalized in SENSITIVE_NORMALIZED_KEYS or any(
        fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS
    )


def redact(value: Any, key: str = "") -> Any:
    if sensitive_key(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = EMAIL_RE.sub("<redacted-email>", value)
        for pattern in SECRET_PATTERNS:
            text = pattern.sub("<redacted-secret>", text)
        return ID_RE.sub("<redacted-id>", text)
    return value


def structural_signature(structure: Mapping[str, Any]) -> str:
    allowed = {key: structure.get(key) for key in (
        "scope", "model", "provider", "request_bytes", "estimated_tokens", "message_count",
        "eager_tools", "deferred_tools", "error_class",
    )}
    return hashlib.sha256(json.dumps(allowed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def duplicate_non_retryable(previous: set[str], signature: str, error_class: str) -> bool:
    duplicate = error_class in NON_RETRYABLE and signature in previous
    previous.add(signature)
    return duplicate


def structural_http_matches(raw: str) -> list[re.Match[str]]:
    matches: list[re.Match[str]] = []
    for match in HTTP_RE.finditer(raw):
        line_start = raw.rfind("\n", 0, match.start()) + 1
        line_prefix = raw[line_start:match.start()].lstrip()
        if line_prefix.startswith(("data:", "event:", ":")):
            continue
        matches.append(match)
    return matches


def latest_response_segment(raw: str) -> tuple[int | None, str]:
    matches = structural_http_matches(raw)
    if not matches:
        return None, raw
    latest = matches[-1]
    return int(latest.group(1)), raw[latest.start():]


def _read_text(path: str | None) -> str:
    return "" if not path else Path(path).read_text(encoding="utf-8", errors="replace")


def _print_json(value: Any) -> None:
    print(json.dumps(redact(value), sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-policy"); validate.add_argument("--policy")
    budget = sub.add_parser("budget"); budget.add_argument("--tokens", type=int); budget.add_argument("--eager-tools", type=int, default=0); budget.add_argument("--unknown", action="store_true"); budget.add_argument("--policy")
    classify = sub.add_parser("classify"); classify.add_argument("--http-status", type=int); classify.add_argument("--body-file"); classify.add_argument("--sse-file"); classify.add_argument("--interrupted", action="store_true")
    recover = sub.add_parser("recover"); recover.add_argument("log_path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-policy":
        errors = validate_policy(load_policy(args.policy)); _print_json({"valid": not errors, "errors": errors}); return 0 if not errors else 1
    if args.command == "budget":
        _print_json(budget_verdict(args.tokens, eager_tools=args.eager_tools, unknown=args.unknown, policy=load_policy(args.policy))); return 0
    if args.command == "classify":
        result = classify_error(args.http_status, body=_read_text(args.body_file), sse=_read_text(args.sse_file), interrupted=args.interrupted)
        _print_json(result); return 0 if result["success"] else 2
    if args.command == "recover":
        status, segment = latest_response_segment(_read_text(args.log_path))
        result = classify_error(status, body=segment, sse=segment)
        _print_json(recovery_for(result)); return 0
    return 64


if __name__ == "__main__":
    sys.exit(main())
