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
from datetime import timedelta
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


def _verified_single_alias_result(command: str, agent_key: str) -> dict[str, object]:
    spec = probe_routing.ALIAS_PROBE_SPECS[command]
    return {
        "verified": True,
        "probe": command,
        "gateway_ingress_model_routing_verified": True,
        "upstream_provider_verified": False,
        "evidence": [
            {"model": spec["model"], "turn": turn, "scope": "subagent", "agent_key": agent_key}
            for turn in ("initial", "resume")
        ],
    }


def _verified_alias_probe_result() -> dict[str, object]:
    keys = {
        "live-alias-luna": "a1b2c3d4e5f6",
        "live-alias-terra": "b1c2d3e4f5a6",
        "live-alias-sol": "c1d2e3f4a5b6",
    }
    alias_results = {
        command: _verified_single_alias_result(command, keys[command])
        for command in probe_routing.LIVE_ALIAS_COMMANDS
    }
    return {
        "verified": True,
        "probe": "live-aliases",
        "gateway_ingress_model_routing_verified": True,
        "upstream_provider_verified": False,
        "alias_results": alias_results,
        "evidence": [row for command in probe_routing.LIVE_ALIAS_COMMANDS for row in alias_results[command]["evidence"]],
    }


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


def test_routing_probe_state_write_is_atomic_compact_and_sanitized() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "runtime" / "last-routing-probe.json"
        replacements: list[tuple[Path, Path]] = []
        real_replace = probe_routing.os.replace

        def capture_replace(source: str | Path, destination: str | Path) -> None:
            source_path = Path(source)
            destination_path = Path(destination)
            assert source_path.exists()
            assert source_path.parent == destination_path.parent == state_path.parent
            replacements.append((source_path, destination_path))
            real_replace(source, destination)

        with mock.patch.object(probe_routing, "_probe_versions", return_value={"claude_code_version": "2.1.217", "cli_proxy_api_version": None}):
            with mock.patch.object(probe_routing.os, "replace", side_effect=capture_replace):
                written = probe_routing.write_routing_probe_state(state_path, "live-aliases", _verified_alias_probe_result())

        assert replacements and replacements[0][1] == state_path
        assert state_path.exists()
        assert list(state_path.parent.glob(f".{state_path.name}.*.tmp")) == []
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted == written
        assert set(persisted) == {"state_schema_version", "last_attempt", "last_successful_alias_results", "last_successful_aliases"}
        assert set(persisted["last_successful_alias_results"]) == set(probe_routing.LIVE_ALIAS_COMMANDS)
        summary = persisted["last_attempt"]
        assert set(summary) == {
            "timestamp_utc", "skill_version", "probe_schema_version", "claude_code_version",
            "cli_proxy_api_version", "probe", "verified", "gateway_ingress_model_routing_verified",
            "upstream_provider_verified", "routes",
        }
        assert summary["upstream_provider_verified"] is False
        assert len(summary["routes"]) == 6
        rendered = json.dumps(persisted)
        for forbidden in ("prompt", "content", "request_id", "agent_RAW", "credential", str(state_path.parent)):
            assert forbidden not in rendered


def test_failed_probe_does_not_clobber_last_successful_alias_matrix() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        with mock.patch.object(probe_routing, "_probe_versions", return_value={"claude_code_version": None, "cli_proxy_api_version": None}):
            first = probe_routing.write_routing_probe_state(state_path, "live-aliases", _verified_alias_probe_result())
            preserved = first["last_successful_aliases"]
            failure = {
                "verified": False,
                "probe": "live-aliases",
                "gateway_ingress_model_routing_verified": False,
                "upstream_provider_verified": False,
                "evidence": [],
                "errors": [{"type": "missing", "raw_id": "must-not-persist"}],
            }
            second = probe_routing.write_routing_probe_state(state_path, "live-aliases", failure)
        assert second["last_attempt"]["verified"] is False
        assert second["last_attempt"]["routes"] == []
        assert second["last_successful_aliases"] == preserved
        assert "must-not-persist" not in state_path.read_text(encoding="utf-8")


def test_bundle_syntax_fixture_declares_isolated_cwd_and_verified_cleanup() -> None:
    source = (SCRIPTS / "probe-routing.py").read_text(encoding="utf-8")
    assert 'bundle_names = ("live-main-sol", "live-stable-control", *LIVE_ALIAS_COMMANDS)' in source
    assert '_run_fixture_process(bundle["command"], cwd=temp' in source
    assert '"captured_by_probe": captured_by_probe' in source
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
    rendered = json.dumps([probe_routing.executable_bundle(command) for command in probe_routing.LIVE_ALIAS_COMMANDS])
    for flag in (
        "--agents", "--disable-slash-commands", "--max-budget-usd", "--mcp-config", "--no-chrome",
        "--no-session-persistence", "--output-format", "--permission-mode", "--prompt-suggestions",
        "--setting-sources", "--settings", "--strict-mcp-config", "--tools", "--verbose",
    ):
        assert flag in rendered, flag


def test_live_prompts_use_current_agent_continuation_semantics() -> None:
    stable_bundle = probe_routing.executable_bundle("live-stable-control")
    bundles = {command: probe_routing.executable_bundle(command) for command in probe_routing.LIVE_ALIAS_COMMANDS}
    assert stable_bundle["claude_args"][stable_bundle["claude_args"].index("--tools") + 1] == "Agent,SendMessage"
    stable_prompt = stable_bundle["claude_args"][-1]
    assert "Run one control sequence" in stable_prompt
    for command, bundle in bundles.items():
        spec = probe_routing.ALIAS_PROBE_SPECS[command]
        prompt = bundle["claude_args"][-1]
        lowered = prompt.lower()
        assert "Agent exactly once" in prompt
        assert "run_in_background true" in prompt
        assert "run_in_background false" not in prompt
        assert "active agent ID returned immediately" in prompt
        assert "while" in lowered and "active" in lowered
        assert "immediately call SendMessage exactly once" in prompt and "exact ID" in prompt
        assert "wait for and collect" in lowered and "completion" in lowered
        assert "Do not call Agent" in prompt and "do not start any other agent" in prompt
        agents_json = bundle["claude_args"][bundle["claude_args"].index("--agents") + 1]
        agents = json.loads(agents_json)
        assert list(agents) == [spec["subagent_type"]]
        assert agents[spec["subagent_type"]]["model"] == spec["inline_model"]
        assert len(bundle["expected_evidence"]) == 2
    try:
        probe_routing.executable_bundle("live-aliases")
    except ValueError as exc:
        assert "aggregate command" in str(exc)
    else:
        raise AssertionError("live-aliases must not have a combined controller bundle")


def test_agent_continuation_capture_is_structural_and_redacted() -> None:
    payload = [
        {
            "type": "tool_use", "name": "Agent", "id": "toolu_AGENT_RAW",
            "input": {"description": "probe", "subagent_type": "control", "prompt": "private prompt", "model": "opus", "run_in_background": True},
        },
        {
            "type": "tool_use", "name": "SendMessage", "id": "toolu_SEND_RAW",
            "input": {"to": "agent_RAW_SECRET", "summary": "private summary", "message": "private follow-up"},
        },
    ]
    shapes = probe_routing._agent_tool_call_shapes(payload)
    rendered = json.dumps(shapes)
    by_tool = {shape["tool"]: shape for shape in shapes}
    assert set(by_tool) == {"agent", "send_message"}
    assert by_tool["agent"]["subagent_type"] == "control" and by_tool["agent"]["model"] == "opus"
    assert by_tool["agent"]["run_in_background"] is True
    assert "prompt" in by_tool["agent"]["input_keys"]
    assert by_tool["send_message"]["target_key"] is not None
    for forbidden in ("private prompt", "private summary", "private follow-up", "agent_RAW_SECRET", "toolu_AGENT_RAW", "toolu_SEND_RAW"):
        assert forbidden not in rendered


def test_live_subcommands_require_approval_and_execute_gated_runner() -> None:
    script = SCRIPTS / "probe-routing.py"
    for command in (*probe_routing.LIVE_ALIAS_COMMANDS, "live-aliases"):
        denied = subprocess.run([sys.executable, "-B", str(script), command], text=True, capture_output=True, check=False)
        assert denied.returncode == 64 and "approve-live-model-calls" in denied.stdout

    source = (SCRIPTS / "probe-routing.py").read_text(encoding="utf-8")
    assert '"-RequireExistingGateway"' in source
    for command in probe_routing.LIVE_ALIAS_COMMANDS:
        bundle = probe_routing.executable_bundle(command, "http://127.0.0.1:9999")
        assert len(bundle["expected_evidence"]) == 2 and bundle["timeout_seconds"] == 120
        assert "-ProbeGateway" in bundle["command"]
        assert "no-session-persistence" in json.dumps(bundle)
        assert bundle["claude_args"][-2] == "--"
    stable_bundle = probe_routing.executable_bundle("live-stable-control", "http://127.0.0.1:9999")
    assert "-StableSubagentModel" in stable_bundle["command"] and len(stable_bundle["expected_evidence"]) == 2

    approved_result = _verified_alias_probe_result()
    output = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "last-routing-probe.json"
        with mock.patch.object(probe_routing, "run_live_probe", return_value=approved_result) as runner:
            with mock.patch.object(probe_routing, "_probe_versions", return_value={"claude_code_version": None, "cli_proxy_api_version": None}):
                with contextlib.redirect_stdout(output):
                    code = probe_routing.main([
                        "live-aliases", "--approve-live-model-calls", "--state-path", str(state_path),
                    ])
        emitted = json.loads(output.getvalue())
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert code == 0 and emitted["verified"] is True and emitted["state_persisted"] is True
        assert persisted["last_successful_aliases"]["probe"] == "live-aliases"
        assert set(persisted["last_successful_alias_results"]) == set(probe_routing.LIVE_ALIAS_COMMANDS)
    runner.assert_called_once_with("live-aliases")


def test_live_alias_orchestrator_runs_independent_process_probes_serially() -> None:
    keys = {
        "live-alias-luna": "a1b2c3d4e5f6",
        "live-alias-terra": "b1c2d3e4f5a6",
        "live-alias-sol": "c1d2e3f4a5b6",
    }
    observed: list[str] = []

    def run_one(command: str) -> dict[str, object]:
        observed.append(command)
        return _verified_single_alias_result(command, keys[command])

    with mock.patch.object(probe_routing, "_run_single_live_probe", side_effect=run_one):
        result = probe_routing.run_live_aliases_probe(cooldown_seconds=0)
    assert observed == list(probe_routing.LIVE_ALIAS_COMMANDS)
    assert result["verified"] is True
    assert result["gateway_ingress_model_routing_verified"] is True
    assert result["process_isolation"] == "one Claude CLI process per alias"
    assert len(result["evidence"]) == 6


def test_live_alias_orchestrator_fails_if_any_independent_probe_fails() -> None:
    keys = {
        "live-alias-luna": "a1b2c3d4e5f6",
        "live-alias-terra": "b1c2d3e4f5a6",
        "live-alias-sol": "c1d2e3f4a5b6",
    }

    def run_one(command: str) -> dict[str, object]:
        if command == "live-alias-terra":
            return {
                "verified": False, "probe": command,
                "gateway_ingress_model_routing_verified": False,
                "upstream_provider_verified": False, "evidence": [],
                "errors": [{"type": "missing", "scope": "send-message-continuation"}],
            }
        return _verified_single_alias_result(command, keys[command])

    with mock.patch.object(probe_routing, "_run_single_live_probe", side_effect=run_one):
        result = probe_routing.run_live_aliases_probe(cooldown_seconds=0)
    assert result["verified"] is False
    assert result["gateway_ingress_model_routing_verified"] is False
    assert [error["probe"] for error in result["errors"]] == ["live-alias-terra"]
    assert set(result["alias_results"]) == set(probe_routing.LIVE_ALIAS_COMMANDS)


def test_independent_alias_attempts_assemble_recent_matching_matrix() -> None:
    keys = {
        "live-alias-luna": "a1b2c3d4e5f6",
        "live-alias-terra": "b1c2d3e4f5a6",
        "live-alias-sol": "c1d2e3f4a5b6",
    }
    versions = {"claude_code_version": "2.1.217", "cli_proxy_api_version": None}
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        with mock.patch.object(probe_routing, "_probe_versions", return_value=versions):
            for command in probe_routing.LIVE_ALIAS_COMMANDS:
                state = probe_routing.write_routing_probe_state(
                    state_path, command, _verified_single_alias_result(command, keys[command]),
                )
        assert state["last_attempt"]["probe"] == "live-alias-sol"
        assert state["last_successful_aliases"]["verified"] is True
        assert len(state["last_successful_aliases"]["routes"]) == 6
        assert set(state["last_successful_alias_results"]) == set(probe_routing.LIVE_ALIAS_COMMANDS)

        preserved = state["last_successful_aliases"]
        failure = {
            "verified": False, "probe": "live-alias-luna",
            "gateway_ingress_model_routing_verified": False,
            "upstream_provider_verified": False, "evidence": [],
        }
        with mock.patch.object(probe_routing, "_probe_versions", return_value=versions):
            later = probe_routing.write_routing_probe_state(state_path, "live-alias-luna", failure)
        assert later["last_attempt"]["verified"] is False
        assert later["last_successful_aliases"] == preserved
        assert later["last_successful_alias_results"]["live-alias-luna"]["verified"] is True


def test_failed_aggregate_keeps_successful_components_for_later_completion() -> None:
    keys = {
        "live-alias-luna": "a1b2c3d4e5f6",
        "live-alias-terra": "b1c2d3e4f5a6",
        "live-alias-sol": "c1d2e3f4a5b6",
    }
    aggregate = _verified_alias_probe_result()
    aggregate["verified"] = False
    aggregate["gateway_ingress_model_routing_verified"] = False
    aggregate["alias_results"]["live-alias-terra"] = {
        "verified": False, "probe": "live-alias-terra",
        "gateway_ingress_model_routing_verified": False,
        "upstream_provider_verified": False, "evidence": [],
    }
    aggregate["evidence"] = [
        row for command in probe_routing.LIVE_ALIAS_COMMANDS
        for row in aggregate["alias_results"][command].get("evidence", [])
    ]
    versions = {"claude_code_version": "2.1.217", "cli_proxy_api_version": None}
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        with mock.patch.object(probe_routing, "_probe_versions", return_value=versions):
            partial = probe_routing.write_routing_probe_state(state_path, "live-aliases", aggregate)
            completed = probe_routing.write_routing_probe_state(
                state_path, "live-alias-terra",
                _verified_single_alias_result("live-alias-terra", keys["live-alias-terra"]),
            )
    assert set(partial["last_successful_alias_results"]) == {"live-alias-luna", "live-alias-sol"}
    assert partial["last_successful_aliases"] is None
    assert completed["last_successful_aliases"]["verified"] is True
    assert len(completed["last_successful_aliases"]["routes"]) == 6


def test_alias_matrix_rejects_stale_or_version_mismatched_components() -> None:
    keys = {
        "live-alias-luna": "a1b2c3d4e5f6",
        "live-alias-terra": "b1c2d3e4f5a6",
        "live-alias-sol": "c1d2e3f4a5b6",
    }
    now = probe_routing.datetime.now(probe_routing.timezone.utc)
    summaries = {}
    with mock.patch.object(probe_routing, "_probe_versions", return_value={"claude_code_version": "2.1.217", "cli_proxy_api_version": None}):
        for command in probe_routing.LIVE_ALIAS_COMMANDS:
            summaries[command] = probe_routing._routing_probe_summary(command, _verified_single_alias_result(command, keys[command]))
    stale = json.loads(json.dumps(summaries))
    stale["live-alias-luna"]["timestamp_utc"] = (now - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
    assert probe_routing._matching_alias_summaries(stale, now=now) is None
    mismatched = json.loads(json.dumps(summaries))
    mismatched["live-alias-sol"]["claude_code_version"] = "2.1.218"
    assert probe_routing._matching_alias_summaries(mismatched, now=now) is None
    wrong_schema = json.loads(json.dumps(summaries))
    wrong_schema["live-alias-terra"]["probe_schema_version"] = 1
    assert probe_routing._matching_alias_summaries(wrong_schema, now=now) is None


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


def test_live_main_allows_bounded_auxiliary_luna_and_one_sol_controller() -> None:
    auxiliary = {
        "sequence": 0, "model": "gpt-5.6-luna", "message_count": 1, "subagent_scope": False,
        "agent_key": None, "agent_tool_calls": [], "http_status": 200, "terminal": True,
        "success": True, "classification": "success", "response_model": "gpt-5.6-luna",
        "provider": None, "fallback": False,
    }
    second_auxiliary = {**auxiliary, "sequence": 1}
    controller = {**auxiliary, "sequence": 2, "model": "gpt-5.6-sol", "response_model": "gpt-5.6-sol"}
    result = probe_routing._validate_live_captures("live-main-sol", [auxiliary, second_auxiliary, controller], 0, False)
    assert result["verified"] is True, result
    assert [row["model"] for row in result["auxiliary_main_luna"]] == ["gpt-5.6-luna", "gpt-5.6-luna"]
    assert [row["model"] for row in result["evidence"]] == ["gpt-5.6-sol"]
    assert [row["classification"] for row in result["capture_summary"]["rows"]] == ["main-luna-auxiliary", "main-luna-auxiliary", "main-sol-controller"]
    assert result["capture_summary"]["max_rows"] == 24
    assert not any(error["type"] == "duplicate" for error in result["errors"])

    third_auxiliary = {**auxiliary, "sequence": 3}
    bounded = probe_routing._validate_live_captures("live-main-sol", [auxiliary, second_auxiliary, controller, third_auxiliary], 0, False)
    assert bounded["verified"] is False
    assert any(error["scope"] == "main-auxiliary" and error["max_allowed"] == 2 for error in bounded["errors"])


def test_live_main_rejects_real_duplicate_sol_controllers() -> None:
    controller = {
        "sequence": 0, "model": "gpt-5.6-sol", "message_count": 1, "subagent_scope": False,
        "agent_key": None, "agent_tool_calls": [], "http_status": 200, "terminal": True,
        "success": True, "classification": "success", "response_model": "gpt-5.6-sol",
        "provider": None, "fallback": False,
    }
    result = probe_routing._validate_live_captures("live-main-sol", [controller, {**controller, "sequence": 1}], 0, False)
    assert result["verified"] is False
    assert {error["scope"] for error in result["errors"] if error["type"] == "duplicate"} == {"main-controller"}
    assert len(result["evidence"]) == 2


def test_each_alias_pair_requires_one_agent_one_exact_send_and_stable_identity() -> None:
    for command in probe_routing.LIVE_ALIAS_COMMANDS:
        spec = probe_routing.ALIAS_PROBE_SPECS[command]
        agent_key = f"{spec['subagent_type']}-agent-hash"
        controller = {
            "sequence": 0, "model": "gpt-5.6-sol", "message_count": 4, "subagent_scope": False,
            "agent_key": None,
            "agent_tool_calls": [
                {"tool": "agent", "call_key": f"{command}-initial", "input_keys": ["description", "prompt", "run_in_background", "subagent_type"], "subagent_type": spec["subagent_type"], "model": None, "run_in_background": True},
                {"tool": "send_message", "call_key": f"{command}-send", "input_keys": ["message", "summary", "to"], "target_key": agent_key},
            ],
            "http_status": 200, "terminal": True, "success": True, "classification": "success",
            "response_model": "gpt-5.6-sol", "provider": None, "fallback": False,
        }
        initial = {
            **controller, "sequence": 1, "model": spec["model"], "subagent_scope": True,
            "agent_key": agent_key, "agent_tool_calls": [], "response_model": spec["model"],
        }
        resumed = {**initial, "sequence": 2}
        passed = probe_routing._validate_live_captures(command, [controller, initial, resumed], 0, False)
        assert passed["verified"] is True, passed
        assert [row["turn"] for row in passed["evidence"]] == ["initial", "resume"]
        assert {row["agent_key"] for row in passed["evidence"]} == {agent_key}

        missing_send_controller = {**controller, "agent_tool_calls": controller["agent_tool_calls"][:1]}
        missing_send = probe_routing._validate_live_captures(command, [missing_send_controller, initial], 0, False)
        assert missing_send["verified"] is False
        assert any(error["scope"] == "send-message-continuation" for error in missing_send["errors"])

        wrong_identity = probe_routing._validate_live_captures(command, [controller, initial, {**resumed, "agent_key": "different-agent"}], 0, False)
        assert wrong_identity["verified"] is False
        assert any(error["scope"] == "subagent-resume" for error in wrong_identity["errors"])

        foreground_controller = {
            **controller,
            "agent_tool_calls": [{**controller["agent_tool_calls"][0], "run_in_background": False}, controller["agent_tool_calls"][1]],
        }
        foreground = probe_routing._validate_live_captures(command, [foreground_controller, initial, resumed], 0, False)
        assert foreground["verified"] is False
        assert any(error["scope"] == "agent-background-mode" for error in foreground["errors"])


def test_stable_same_agent_id_passes_and_missing_route_fails() -> None:
    controller = {
        "sequence": 0, "model": "gpt-5.6-sol", "message_count": 4, "subagent_scope": False,
        "agent_key": None,
        "agent_tool_calls": [
            {"tool": "agent", "call_key": "call-initial", "input_keys": ["description", "prompt", "run_in_background", "subagent_type"], "subagent_type": "control", "model": None, "run_in_background": True},
            {"tool": "send_message", "call_key": "call-follow-up", "input_keys": ["message", "summary", "to"], "target_key": "agent-hash-a"},
        ],
        "http_status": 200, "terminal": True, "success": True, "classification": "success",
        "response_model": "gpt-5.6-sol", "provider": None, "fallback": False,
    }
    initial = {**controller, "sequence": 1, "model": "gpt-5.6-luna", "subagent_scope": True, "agent_key": "agent-hash-a", "agent_tool_calls": [], "response_model": "gpt-5.6-luna"}
    resumed = {**initial, "sequence": 2}
    passed = probe_routing._validate_live_captures("live-stable-control", [controller, initial, resumed], 0, False)
    assert passed["verified"] is True, passed
    assert [row["turn"] for row in passed["evidence"]] == ["initial", "resume"]
    assert {row["agent_key"] for row in passed["evidence"]} == {"agent-hash-a"}

    missing = probe_routing._validate_live_captures("live-stable-control", [controller, initial], 0, False)
    assert missing["verified"] is False
    assert any(error["type"] == "missing" and error["scope"] == "subagent" for error in missing["errors"])
    assert [row["agent_key"] for row in missing["observed_ingress_routes"] if row["scope"] == "subagent"] == ["agent-hash-a"]


def test_distinct_agent_ids_fail_resume_but_preserve_partial_evidence() -> None:
    controller = {
        "sequence": 0, "model": "gpt-5.6-sol", "message_count": 4, "subagent_scope": False,
        "agent_key": None,
        "agent_tool_calls": [
            {"tool": "agent", "call_key": "call-initial", "input_keys": ["description", "prompt", "run_in_background", "subagent_type"], "subagent_type": "control", "model": None, "run_in_background": True},
            {"tool": "send_message", "call_key": "call-follow-up", "input_keys": ["message", "summary", "to"], "target_key": "agent-hash-a"},
        ],
        "http_status": 200, "terminal": True, "success": True, "classification": "success",
        "response_model": "gpt-5.6-sol", "provider": None, "fallback": False,
    }
    initial = {**controller, "sequence": 1, "model": "gpt-5.6-luna", "subagent_scope": True, "agent_key": "agent-hash-a", "agent_tool_calls": [], "response_model": "gpt-5.6-luna"}
    resumed_wrong = {**initial, "sequence": 2, "agent_key": "agent-hash-b"}
    result = probe_routing._validate_live_captures("live-stable-control", [controller, initial, resumed_wrong], 0, False)
    assert result["verified"] is False
    assert any(error["scope"] == "subagent-resume" for error in result["errors"] if error["type"] == "unexpected")
    assert len(result["evidence"]) == 2
    assert result["gateway_ingress_model_routing_observed"] is True
    assert result["gateway_ingress_model_routing_verified"] is False
    assert result["capture_summary"]["total"] == 3
    assert {row["agent_key"] for row in result["capture_summary"]["rows"] if row["scope"] == "subagent"} == {"agent-hash-a", "agent-hash-b"}


def test_pre_request_cli_exit_returns_bounded_redacted_diagnostic() -> None:
    stderr = (
        "Error processing --setting-sources: Invalid setting source: --strict-mcp-config\n"
        r"C:\Users\person\secret\config.json api_key=super-secret-value MAIN_SOL_OK"
    )
    result = probe_routing._validate_live_captures("live-main-sol", [], 1, False, "private stdout prompt", stderr)
    diagnostic = result["cli_diagnostic"]
    rendered = json.dumps(diagnostic)
    assert diagnostic["stdout_chars"] == len("private stdout prompt")
    assert diagnostic["stderr_chars"] == len(stderr)
    assert "private stdout prompt" not in rendered
    assert "super-secret-value" not in rendered
    assert "C:\\Users\\person" not in rendered
    assert "MAIN_SOL_OK" not in rendered
    assert "Invalid setting source: --strict-mcp-config" in (diagnostic.get("stderr_excerpt") or "")
    assert len(diagnostic.get("stderr_excerpt") or "") <= 1200


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
