"""Zero-quota tests for the claudex-optimized policy and structural helpers."""

from __future__ import annotations

import contextlib
import errno
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "claudex-optimized" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import preflight  # noqa: E402

_probe_spec = importlib.util.spec_from_file_location("probe_routing", SCRIPTS / "probe-routing.py")
assert _probe_spec is not None and _probe_spec.loader is not None
probe_routing = importlib.util.module_from_spec(_probe_spec)
_probe_spec.loader.exec_module(probe_routing)


def test_policy_validates() -> None:
    assert preflight.validate_policy(preflight.load_policy()) == []


def test_budget_threshold_boundaries_and_unknown_source() -> None:
    for tokens, verdict in {
        179999: "admit", 180000: "warn", 190000: "warn", 190001: "rotate",
        208000: "rotate", 208001: "block",
    }.items():
        result = preflight.budget_verdict(tokens)
        assert result["verdict"] == verdict
        assert result["estimated"] is True and result["source"] == "structural_estimate"
    unknown = preflight.budget_verdict(None, eager_tools=13)
    assert unknown["verdict"] == "rotate"
    assert unknown["estimated"] is False and unknown["source"] == "unknown"
    explicitly_unknown = preflight.budget_verdict(123456, eager_tools=13, unknown=True)
    assert explicitly_unknown["estimated_tokens"] is None
    assert explicitly_unknown["estimated"] is False and explicitly_unknown["source"] == "unknown"
    assert preflight.budget_verdict(None, eager_tools=50)["verdict"] == "block"


def test_sse_terminal_records_are_structural() -> None:
    literal = 'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"literal [DONE], message_stop, context_length_exceeded, and authentication_error"}}\n\n'
    assert preflight.terminal_sse_seen(literal) is False
    assert preflight.classify_error(200, sse=literal)["category"] == "interrupted"
    assert preflight.terminal_sse_seen("data: [DONE]\n\n") is True
    assert preflight.terminal_sse_seen('data: {"type":"message_stop"}\n\n') is True
    assert preflight.terminal_sse_seen("event: message_stop\ndata: {}\n\n") is True


def test_http_200_terminal_sse_error_is_failure() -> None:
    sse = "event: message_start\ndata: {}\n\nevent: error\ndata: {\"error\":{\"code\":\"context_length_exceeded\"}}\n\n"
    result = preflight.classify_error(200, sse=sse)
    assert result["category"] == "context" and result["success"] is False and result["sse_error"] is True


def test_error_classification_matrix() -> None:
    for status, body, category, retryable in [
        (400, "context_length_exceeded", "context", False),
        (503, "auth_unavailable: no auth available", "auth", False),
        (429, "rate_limit_error", "quota", True),
        (400, "Unknown Provider/Model", "unknown_model", False),
    ]:
        result = preflight.classify_error(status, body=body)
        assert result["category"] == category and result["retryable"] is retryable


def test_duplicate_non_retryable_signature_guard() -> None:
    structure = {"scope":"subagent","model":"gpt-5.6-sol","request_bytes":1501200,"estimated_tokens":208001,"message_count":57,"eager_tools":176,"deferred_tools":0,"error_class":"context","prompt":"ignored"}
    signature = preflight.structural_signature(structure)
    seen: set[str] = set()
    assert preflight.duplicate_non_retryable(seen, signature, "context") is False
    assert preflight.duplicate_non_retryable(seen, signature, "context") is True
    assert preflight.duplicate_non_retryable(seen, signature, "quota") is False


def test_redaction_covers_normalized_keys_and_secret_patterns() -> None:
    raw = {
        "Prompt": "private prompt", "tool-schemas": [{"name": "danger"}],
        "access-token": "access value", "oauth_refresh_token_value": "refresh value", "service.client-secret-value": "secret value",
        "database-password-hash": "password value", "email": "person@example.com",
        "note": "req_RAW Bearer abc.def.ghi eyJabc.def.ghi ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 AIzaSyABCDEFGHIJKLMNOPQRSTUVWX access_token=rawvalue api_key='quoted secret value'",
    }
    rendered = json.dumps(preflight.redact(raw), sort_keys=True)
    for forbidden in ("private prompt","danger","access value","refresh value","secret value","password value","person@example.com","req_RAW","abc.def.ghi","ghp_","github_pat_","AIza","rawvalue","quoted secret value"):
        assert forbidden not in rendered


def test_recovery_output_contains_no_raw_log_content() -> None:
    output = json.dumps(preflight.recovery_for(preflight.classify_error(503, body="auth_unavailable user@example.com req_RAW sk-secret-abcdef")))
    assert "user@example.com" not in output and "req_RAW" not in output and "sk-secret" not in output
    assert "do not edit credentials" in output.lower()


def test_zero_quota_integrations_report_missing_cli_explicitly() -> None:
    with mock.patch.object(probe_routing.shutil, "which", return_value=None):
        tool_search = probe_routing.run_local_tool_search_integration(176)
        bundle = probe_routing.run_local_bundle_syntax_integration()
    assert tool_search == {"verified": False, "reason": "Claude CLI is not installed", "quota_used": 0}
    assert bundle == {"verified": False, "reason": "Claude CLI is not installed", "quota_used": 0}


def test_bundle_syntax_fixture_declares_isolated_cwd_and_verified_cleanup() -> None:
    source = (SCRIPTS / "probe-routing.py").read_text(encoding="utf-8")
    assert "_run_fixture_process([claude, *args], cwd=temp" in source
    assert '"fixture_cwd": "isolated-temp"' in source
    assert "if not _remove_temp_tree(temp):" in source
    assert 'ParentProcessId = {0}' in source
    assert 'IndexOf($fixture' in source


def test_temp_cleanup_retries_transient_windows_handle_release() -> None:
    path = Path(tempfile.mkdtemp(prefix="claudex-cleanup-unit-"))
    (path / "locked.txt").write_text("fixture", encoding="utf-8")
    real_rmtree = probe_routing.shutil.rmtree
    attempts = 0

    def transient(target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            raise PermissionError(errno.EACCES, "fixture handle still closing", str(target))
        real_rmtree(target)

    with mock.patch.object(probe_routing.shutil, "rmtree", side_effect=transient):
        assert probe_routing._remove_temp_tree(path, timeout_seconds=1.0) is True
    assert attempts == 4 and not path.exists()


def test_structural_log_parser_redacts_ids() -> None:
    text = "requested_model=gpt-5.6-sol resolved_model=gpt-5.6-sol provider=codex HTTP/1.1 200 X-Claude-Code-Agent-Id: agent_RAW\nevent: error\ndata: {\"error\":{\"code\":\"context_length_exceeded\"},\"request_id\":\"req_RAW\"}\n\n"
    result = probe_routing.parse_structural_log(text)
    rendered = json.dumps(result)
    assert result["subagent_scope"] is True and result["classification"]["category"] == "context"
    assert "agent_RAW" not in rendered and "req_RAW" not in rendered


def test_structural_log_uses_only_latest_response_segment() -> None:
    text = (
        "requested_model=gpt-5.6-sol resolved_model=gpt-5.6-sol provider=stale-provider\n"
        "HTTP/1.1 400\n"
        "event: error\ndata: {\"type\":\"error\",\"error\":{\"code\":\"context_length_exceeded\"}}\n\n"
        "requested_model=gpt-5.6-terra resolved_model=gpt-5.6-terra provider=codex X-Claude-Code-Agent-Id: agent_CURRENT\n"
        "HTTP/1.1 429\n"
        "event: error\ndata: {\"type\":\"error\",\"error\":{\"type\":\"rate_limit_error\"}}\n\n"
    )
    result = probe_routing.parse_structural_log(text)
    assert result["http_status"] == 429
    assert result["requested_model"] == "gpt-5.6-terra"
    assert result["resolved_model"] == "gpt-5.6-terra"
    assert result["resolved_provider"] == "codex"
    assert result["classification"]["category"] == "quota"
    assert result["subagent_scope"] is True


def test_live_bundle_contains_supported_cli_flags() -> None:
    rendered = json.dumps(probe_routing.executable_bundle("live-aliases"))
    for flag in (
        "--agents", "--disable-slash-commands", "--max-budget-usd", "--mcp-config", "--no-chrome",
        "--no-session-persistence", "--output-format", "--permission-mode", "--prompt-suggestions",
        "--setting-sources", "--settings", "--strict-mcp-config", "--tools", "--verbose",
    ):
        assert flag in rendered, flag


def test_live_subcommands_require_approval_and_execute_gated_runner() -> None:
    script = SCRIPTS / "probe-routing.py"
    denied = subprocess.run([sys.executable, "-B", str(script), "live-aliases"], text=True, capture_output=True, check=False)
    assert denied.returncode == 64 and "approve-live-model-calls" in denied.stdout

    source = (SCRIPTS / "probe-routing.py").read_text(encoding="utf-8")
    assert '"-RequireExistingGateway"' in source
    aliases_bundle = probe_routing.executable_bundle("live-aliases", "http://127.0.0.1:9999")
    assert len(aliases_bundle["expected_evidence"]) == 6 and aliases_bundle["timeout_seconds"] == 120
    assert "-ProbeGateway" in aliases_bundle["command"]
    assert "no-session-persistence" in json.dumps(aliases_bundle)
    assert aliases_bundle["claude_args"][-2] == "--"
    stable_bundle = probe_routing.executable_bundle("live-stable-control", "http://127.0.0.1:9999")
    assert "-StableSubagentModel" in stable_bundle["command"] and len(stable_bundle["expected_evidence"]) == 2

    approved_result = {"verified": True, "probe": "live-aliases", "evidence": []}
    output = io.StringIO()
    with mock.patch.object(probe_routing, "run_live_probe", return_value=approved_result) as runner:
        with contextlib.redirect_stdout(output):
            code = probe_routing.main(["live-aliases", "--approve-live-model-calls"])
    assert code == 0 and json.loads(output.getvalue()) == approved_result
    runner.assert_called_once_with("live-aliases")


def test_live_capture_validation_fails_closed_for_all_review_conditions() -> None:
    good = {
        "sequence": 0, "model": "gpt-5.6-sol", "message_count": 1, "subagent_scope": False,
        "agent_key": None, "http_status": 200, "terminal": True, "success": True,
        "classification": "success", "response_model": "gpt-5.6-sol", "provider": None, "fallback": False,
    }
    assert probe_routing._validate_live_captures("live-main-sol", [good], 0, False)["verified"] is True
    cases = [
        ([], "missing"),
        ([good, {**good, "sequence": 1}], "duplicate"),
        ([{**good, "model": "gpt-5.6-terra"}], "unexpected"),
        ([{**good, "fallback": True}], "fallback"),
        ([{**good, "terminal": False}], "nonterminal"),
        ([{**good, "provider": "wrong-provider"}], "provider_mismatch"),
    ]
    for captures, expected_error in cases:
        result = probe_routing._validate_live_captures("live-main-sol", captures, 0, False)
        assert result["verified"] is False
        assert expected_error in {item["type"] for item in result["errors"]}
    unobserved = probe_routing._validate_live_captures("live-main-sol", [good], 0, False)
    assert unobserved["upstream_provider_verified"] is False
    assert "gateway ingress" in unobserved["limitation"]


def test_recover_cli_redacts_and_uses_latest_correlated_response() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp) / "error.log"
        fixture.write_text("HTTP/1.1 200\nevent: error\ndata: {\"error\":{\"code\":\"context_length_exceeded\"},\"email\":\"person@example.com\",\"request_id\":\"req_RAW\"}\n\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(SCRIPTS / "preflight.py"), "recover", str(fixture)], text=True, capture_output=True, check=False)
        assert result.returncode == 0 and "context" in result.stdout
        assert "person@example.com" not in result.stdout and "req_RAW" not in result.stdout
        fixture.write_text(
            "HTTP/1.1 429\nevent: error\ndata: {\"type\":\"error\",\"error\":{\"type\":\"rate_limit_error\"}}\n\n"
            "HTTP/1.1 400\nevent: error\ndata: {\"type\":\"error\",\"error\":{\"code\":\"context_length_exceeded\"}}\n\n",
            encoding="utf-8",
        )
        latest = subprocess.run([sys.executable, str(SCRIPTS / "preflight.py"), "recover", str(fixture)], text=True, capture_output=True, check=False)
        assert latest.returncode == 0
        recovered = json.loads(latest.stdout)
        assert recovered["category"] == "context" and recovered["retryable"] is False
        fixture.write_text(
            "HTTP/1.1 200\nevent: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"delta\":{\"text\":\"literal HTTP/1.1 503 authentication_error\"}}\n\n"
            "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
            encoding="utf-8",
        )
        literal = subprocess.run([sys.executable, str(SCRIPTS / "preflight.py"), "recover", str(fixture)], text=True, capture_output=True, check=False)
        assert json.loads(literal.stdout)["category"] == "success"


def _run() -> int:
    tests = sorted((name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn))
    passed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            print(f"FAIL {name}: {exc}", file=sys.stderr); traceback.print_exc(); return 1
        print(f"  ok  {name}"); passed += 1
    print(f"\n{passed}/{len(tests)} passed."); return 0


if __name__ == "__main__":
    sys.exit(_run())
