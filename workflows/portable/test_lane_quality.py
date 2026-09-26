"""Tests for lane_quality.py session parsers on real sample session lines."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from lane_quality import (
    parse_session_file,
    collect_all_lanes,
    LaneRecord,
    PRRecord,
    ModelMetrics,
    Baseline,
    aggregate_by_model,
    compute_daily_snapshots,
    compute_baseline,
    detect_degradation,
    render_report,
    save_baseline,
    load_baseline,
    save_snapshot,
    _normalize_model_id,
    _is_premium,
    _is_orchestrator,
    _model_family,
    _classify_outcome,
    _parse_review_verdict,
    _pr_node_to_record,
    _load_pr_cache,
    _save_pr_cache,
    _detect_follow_ups_and_reverts,
    CACHE_DIR,
)


# ---------------------------------------------------------------------------
# Fixtures: real session JSONL lines from actual veyyon sessions
# ---------------------------------------------------------------------------

FIXTURE_SESSION_LINE = json.dumps({
    "type": "session", "version": 3,
    "id": "01a0d952-715a-7046-b9ba-9535ec11970f",
    "timestamp": "2026-09-25T16:07:33.978Z",
    "cwd": "C:\\Users\\wkiri\\development\\super-board",
})

FIXTURE_MODEL_CHANGE = json.dumps({
    "id": "f972dec1", "parentId": None,
    "timestamp": "2026-09-25T16:07:34.348Z",
    "type": "model_change",
    "model": "google-antigravity/claude-opus-4-6",
})

FIXTURE_SESSION_INIT = json.dumps({
    "id": "6317876e", "parentId": "65d942ac",
    "timestamp": "2026-09-25T16:07:34.551Z",
    "type": "session_init",
    "task": "Fix the routing configuration for OpenCode Go models",
    "tools": ["read", "bash", "edit", "search", "eval"],
    "spawns": "task",
})

FIXTURE_ASSISTANT_MSG_OPUS = json.dumps({
    "id": "a1b2c3d4", "parentId": "e5f6a7b8",
    "timestamp": "2026-09-25T16:08:10.000Z",
    "type": "message",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "Let me check https://github.com/Wladefant/super-board/pull/212 for context."}],
        "api": "anthropic-messages",
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "usage": {
            "input": 45000, "output": 1200,
            "cacheRead": 120000, "cacheWrite": 50000,
            "totalTokens": 216200,
            "cost": {"input": 0.675, "output": 0.06, "cacheRead": 0.45,
                     "cacheWrite": 0.625, "total": 1.81},
        },
        "stopReason": "toolUse",
        "timestamp": 1790352490000,
        "duration": 3500.5,
        "ttft": 800.2,
    },
})

FIXTURE_ASSISTANT_MSG_FLASH = json.dumps({
    "id": "f1e2d3c4", "parentId": "a1b2c3d4",
    "timestamp": "2026-09-25T16:09:00.000Z",
    "type": "message",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "Applied fix and opened https://github.com/Bavariance/polysimulator/pull/5500"}],
        "api": "google-gemini-cli",
        "provider": "google-antigravity",
        "model": "gemini-3.8-flash",
        "usage": {
            "input": 30000, "output": 2000,
            "cacheRead": 0, "cacheWrite": 0,
            "totalTokens": 32000,
            "cost": {"input": 0, "output": 0, "cacheRead": 0,
                     "cacheWrite": 0, "total": 0},
        },
        "stopReason": "endTurn",
        "timestamp": 1790352540000,
        "duration": 2100.0,
        "ttft": 500.0,
    },
})

FIXTURE_ASSISTANT_MSG_DEEPSEEK = json.dumps({
    "id": "d5e6f7a8", "parentId": "f1e2d3c4",
    "timestamp": "2026-09-25T16:10:00.000Z",
    "type": "message",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "Running tests now."}],
        "api": "deepseek-api",
        "provider": "deepseek",
        "model": "deepseek-flash",
        "usage": {
            "input": 10000, "output": 500,
            "cacheRead": 0, "cacheWrite": 0,
            "totalTokens": 10500,
            "cost": {"input": 0.003, "output": 0.0006, "cacheRead": 0,
                     "cacheWrite": 0, "total": 0.0036},
        },
        "stopReason": "endTurn",
        "timestamp": 1790352600000,
        "duration": 1200.0,
        "ttft": 300.0,
    },
})

FIXTURE_ASSISTANT_MSG_SPACE_BUNNY = json.dumps({
    "id": "sb001122", "parentId": "d5e6f7a8",
    "timestamp": "2026-09-25T16:11:00.000Z",
    "type": "message",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "Verifying build."}],
        "api": "opencode-go",
        "provider": "opencode-go",
        "model": "space-bunny-free",
        "usage": {
            "input": 15000, "output": 800,
            "cacheRead": 0, "cacheWrite": 0,
            "totalTokens": 15800,
            "cost": {"input": 0, "output": 0, "cacheRead": 0,
                     "cacheWrite": 0, "total": 0},
        },
        "stopReason": "endTurn",
        "timestamp": 1790352660000,
        "duration": 1800.0,
        "ttft": 400.0,
    },
})

FIXTURE_USER_MSG = json.dumps({
    "id": "u001", "parentId": None,
    "timestamp": "2026-09-25T16:07:40.000Z",
    "type": "message",
    "message": {"role": "user", "content": [{"type": "text", "text": "Fix this bug."}]},
})

FIXTURE_TOOL_START = json.dumps({
    "id": "t001", "parentId": "a1b2c3d4",
    "timestamp": "2026-09-25T16:08:15.000Z",
    "type": "custom",
    "customType": "tool_execution_start",
    "data": {
        "toolCallId": "call_001", "toolName": "bash",
        "startedAt": "2026-09-25T16:08:15.000Z",
        "args": {"command": "npm test"},
    },
})

FIXTURE_YIELD_TOOL = json.dumps({
    "id": "y001", "parentId": "f1e2d3c4",
    "timestamp": "2026-09-25T16:10:30.000Z",
    "type": "custom",
    "customType": "tool_execution_start",
    "data": {
        "toolCallId": "call_yield", "toolName": "yield",
        "startedAt": "2026-09-25T16:10:30.000Z",
    },
})

FIXTURE_SESSION_EXIT_DISPOSE = json.dumps({
    "id": "x001", "parentId": None,
    "timestamp": "2026-09-25T16:15:00.000Z",
    "type": "custom",
    "customType": "session_exit",
    "data": {"reason": "dispose", "kind": "normal",
             "recordedAt": "2026-09-25T16:15:00.000Z"},
})

FIXTURE_SESSION_EXIT_ERROR = json.dumps({
    "id": "x002", "parentId": None,
    "timestamp": "2026-09-25T16:15:00.000Z",
    "type": "custom",
    "customType": "session_exit",
    "data": {"reason": "error", "kind": "abnormal"},
})

FIXTURE_GITHUB_TOOL_START = json.dumps({
    "id": "g001", "parentId": "a1b2c3d4",
    "timestamp": "2026-09-25T16:08:20.000Z",
    "type": "custom",
    "customType": "tool_execution_start",
    "data": {
        "toolCallId": "call_gh001", "toolName": "github",
        "startedAt": "2026-09-25T16:08:20.000Z",
        "args": {"pr": "https://github.com/Bavariance/polysimulator/pull/5501"},
    },
})


def _write_session(temp_dir: str, lines: list, name: str = "TestAgent") -> Path:
    """Write a list of JSON-string lines to a JSONL file in temp_dir."""
    path = Path(temp_dir) / f"{name}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNormalizeModelId(unittest.TestCase):
    def test_standard(self):
        self.assertEqual(_normalize_model_id("anthropic", "claude-opus-5-5"),
                         "anthropic/claude-opus-5-5")

    def test_empty(self):
        self.assertEqual(_normalize_model_id("", ""), "unknown/unknown")

    def test_opencode_go(self):
        self.assertEqual(_normalize_model_id("opencode-go", "space-bunny-free"),
                         "opencode-go/space-bunny-free")


class TestModelFamilyClassification(unittest.TestCase):
    def test_premium_opus(self):
        self.assertTrue(_is_premium("anthropic/claude-opus-5-5"))

    def test_premium_fable(self):
        self.assertTrue(_is_premium("anthropic/claude-fable-5-1"))

    def test_premium_codex(self):
        self.assertTrue(_is_premium("openai-codex/gpt-6-astra:high"))

    def test_not_premium_flash(self):
        self.assertFalse(_is_premium("google-antigravity/gemini-3.8-flash"))

    def test_not_premium_deepseek(self):
        self.assertFalse(_is_premium("deepseek/deepseek-flash"))

    def test_not_premium_space_bunny(self):
        self.assertFalse(_is_premium("opencode-go/space-bunny-free"))

    def test_orchestrator_fable(self):
        self.assertTrue(_is_orchestrator("anthropic/claude-fable-5-1"))

    def test_not_orchestrator_flash(self):
        self.assertFalse(_is_orchestrator("google-antigravity/gemini-3.8-flash"))


class TestClassifyOutcome(unittest.TestCase):
    def test_dispose_with_requests(self):
        self.assertEqual(_classify_outcome("dispose", {"toolUse": 5}, 5), "completed")

    def test_dispose_without_requests(self):
        self.assertEqual(_classify_outcome("dispose", {}, 0), "disposed")

    def test_error(self):
        self.assertEqual(_classify_outcome("error", {}, 0), "failed")

    def test_completed(self):
        self.assertEqual(_classify_outcome("completed", {}, 3), "completed")

    def test_unknown_with_requests(self):
        self.assertEqual(_classify_outcome("", {}, 3), "completed")


class TestParseSessionFile(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_basic_parse(self):
        """Parse a session with multiple model responses and extract all fields."""
        path = _write_session(self.temp.name, [
            FIXTURE_SESSION_LINE,
            FIXTURE_MODEL_CHANGE,
            FIXTURE_SESSION_INIT,
            FIXTURE_USER_MSG,
            FIXTURE_ASSISTANT_MSG_OPUS,
            FIXTURE_TOOL_START,
            FIXTURE_ASSISTANT_MSG_FLASH,
            FIXTURE_YIELD_TOOL,
            FIXTURE_SESSION_EXIT_DISPOSE,
        ])
        rec = parse_session_file(path)

        self.assertEqual(rec.session_id, "01a0d952-715a-7046-b9ba-9535ec11970f")
        self.assertEqual(rec.project, "super-board")
        self.assertIn("Fix the routing", rec.task_description)
        self.assertEqual(rec.primary_model, "google-antigravity/claude-opus-4-6")
        self.assertEqual(rec.request_count, 2)
        self.assertEqual(rec.models_used, {
            "anthropic/claude-opus-5-5": 1,
            "google-antigravity/gemini-3.8-flash": 1,
        })
        self.assertEqual(rec.input_tokens, 75000)
        self.assertEqual(rec.output_tokens, 3200)
        self.assertAlmostEqual(rec.total_cost, 1.81, places=2)
        self.assertEqual(rec.outcome, "completed")
        self.assertIn("https://github.com/Wladefant/super-board/pull/212", rec.pr_urls)
        self.assertIn("https://github.com/Bavariance/polysimulator/pull/5500", rec.pr_urls)

    def test_error_exit(self):
        """A session that exits with error is classified as failed."""
        path = _write_session(self.temp.name, [
            FIXTURE_SESSION_LINE,
            FIXTURE_SESSION_EXIT_ERROR,
        ])
        rec = parse_session_file(path)
        self.assertEqual(rec.outcome, "failed")

    def test_deepseek_model(self):
        """DeepSeek model is properly tracked as its own row."""
        path = _write_session(self.temp.name, [
            FIXTURE_SESSION_LINE,
            FIXTURE_ASSISTANT_MSG_DEEPSEEK,
            FIXTURE_SESSION_EXIT_DISPOSE,
        ])
        rec = parse_session_file(path)
        self.assertEqual(rec.primary_model, "deepseek/deepseek-flash")
        self.assertIn("deepseek/deepseek-flash", rec.models_used)
        self.assertAlmostEqual(rec.total_cost, 0.0036, places=4)
        self.assertAlmostEqual(rec.cost_by_provider["deepseek"], 0.0036, places=4)

    def test_space_bunny_free_tracked(self):
        """space-bunny-free (free model) is tracked as its own model row."""
        path = _write_session(self.temp.name, [
            FIXTURE_SESSION_LINE,
            FIXTURE_ASSISTANT_MSG_SPACE_BUNNY,
            FIXTURE_SESSION_EXIT_DISPOSE,
        ])
        rec = parse_session_file(path)
        self.assertEqual(rec.primary_model, "opencode-go/space-bunny-free")
        self.assertIn("opencode-go/space-bunny-free", rec.models_used)
        self.assertAlmostEqual(rec.total_cost, 0.0, places=4)
        self.assertFalse(_is_premium("opencode-go/space-bunny-free"))

    def test_github_tool_pr_extraction(self):
        """PR URLs from github tool args are captured."""
        path = _write_session(self.temp.name, [
            FIXTURE_SESSION_LINE,
            FIXTURE_GITHUB_TOOL_START,
            FIXTURE_ASSISTANT_MSG_FLASH,
            FIXTURE_SESSION_EXIT_DISPOSE,
        ])
        rec = parse_session_file(path)
        self.assertIn("https://github.com/Bavariance/polysimulator/pull/5501", rec.pr_urls)

    def test_multi_model_fallback(self):
        """Session using multiple models tracks each separately."""
        path = _write_session(self.temp.name, [
            FIXTURE_SESSION_LINE,
            FIXTURE_ASSISTANT_MSG_OPUS,
            FIXTURE_ASSISTANT_MSG_FLASH,
            FIXTURE_ASSISTANT_MSG_DEEPSEEK,
            FIXTURE_ASSISTANT_MSG_SPACE_BUNNY,
            FIXTURE_SESSION_EXIT_DISPOSE,
        ])
        rec = parse_session_file(path)
        self.assertEqual(len(rec.models_used), 4)
        self.assertIn("anthropic/claude-opus-5-5", rec.models_used)
        self.assertIn("google-antigravity/gemini-3.8-flash", rec.models_used)
        self.assertIn("deepseek/deepseek-flash", rec.models_used)
        self.assertIn("opencode-go/space-bunny-free", rec.models_used)
        self.assertEqual(rec.request_count, 4)
        total_tokens = 45000 + 1200 + 30000 + 2000 + 10000 + 500 + 15000 + 800
        self.assertEqual(rec.input_tokens + rec.output_tokens, total_tokens)

    def test_malformed_line_skipped(self):
        """Malformed JSON lines are skipped without crashing."""
        path = _write_session(self.temp.name, [
            FIXTURE_SESSION_LINE,
            "this is not json {{{",
            FIXTURE_ASSISTANT_MSG_FLASH,
            "",
            FIXTURE_SESSION_EXIT_DISPOSE,
        ])
        rec = parse_session_file(path)
        self.assertEqual(rec.request_count, 1)

    def test_empty_file(self):
        """An empty session file produces a valid (empty) record."""
        path = _write_session(self.temp.name, [""])
        rec = parse_session_file(path)
        self.assertEqual(rec.request_count, 0)
        self.assertEqual(rec.outcome, "unknown")

    def test_nesting_depth(self):
        """Nesting depth is set from the caller."""
        path = _write_session(self.temp.name, [FIXTURE_SESSION_LINE])
        rec0 = parse_session_file(path, nesting_depth=0)
        rec1 = parse_session_file(path, nesting_depth=1)
        self.assertEqual(rec0.nesting_depth, 0)
        self.assertEqual(rec1.nesting_depth, 1)


class TestAggregateByModel(unittest.TestCase):
    def _make_lane(self, model: str, pr_urls: list = None,
                   outcome: str = "completed", tokens: int = 10000,
                   cost: float = 0.5, nesting: int = 1) -> LaneRecord:
        return LaneRecord(
            name=f"lane-{model}",
            primary_model=model,
            models_used={model: 1},
            input_tokens=tokens,
            output_tokens=tokens // 10,
            total_cost=cost,
            outcome=outcome,
            pr_urls=pr_urls or [],
            nesting_depth=nesting,
            request_count=1,
        )

    def test_basic_aggregation(self):
        lanes = [
            self._make_lane("google-antigravity/gemini-3.8-flash", cost=0.0),
            self._make_lane("google-antigravity/gemini-3.8-flash", cost=0.0),
            self._make_lane("anthropic/claude-opus-5-5", cost=5.0),
        ]
        metrics = aggregate_by_model(lanes, {})
        self.assertEqual(metrics["google-antigravity/gemini-3.8-flash"].lane_count, 2)
        self.assertEqual(metrics["anthropic/claude-opus-5-5"].lane_count, 1)
        self.assertAlmostEqual(metrics["anthropic/claude-opus-5-5"].total_cost, 5.0)

    def test_pr_attribution(self):
        pr_url = "https://github.com/Bavariance/polysimulator/pull/100"
        lanes = [self._make_lane("deepseek/deepseek-flash", pr_urls=[pr_url])]
        pr_records = {
            pr_url: PRRecord(
                url=pr_url, number=100, repo="Bavariance/polysimulator",
                merged=True, review_count=2, request_changes_count=1,
                approve_count=1, review_rounds=2,
            ),
        }
        metrics = aggregate_by_model(lanes, pr_records)
        m = metrics["deepseek/deepseek-flash"]
        self.assertEqual(m.prs_merged, 1)
        self.assertEqual(m.request_changes_received, 1)
        self.assertEqual(m.review_rounds_total, 2)

    def test_escalation_rate(self):
        lanes = [
            self._make_lane("google-antigravity/gemini-3.8-flash", outcome="completed"),
            self._make_lane("google-antigravity/gemini-3.8-flash", outcome="completed"),
            self._make_lane("google-antigravity/gemini-3.8-flash", outcome="escalated"),
        ]
        metrics = aggregate_by_model(lanes, {})
        m = metrics["google-antigravity/gemini-3.8-flash"]
        self.assertAlmostEqual(m.escalation_rate, 33.33, places=1)

    def test_space_bunny_separate_row(self):
        """space-bunny-free gets its own row in aggregation, not merged with others."""
        lanes = [
            self._make_lane("opencode-go/space-bunny-free", cost=0.0),
            self._make_lane("opencode-go/kimi-k2.7-code", cost=0.01),
        ]
        metrics = aggregate_by_model(lanes, {})
        self.assertIn("opencode-go/space-bunny-free", metrics)
        self.assertIn("opencode-go/kimi-k2.7-code", metrics)
        self.assertEqual(metrics["opencode-go/space-bunny-free"].lane_count, 1)


class TestDailySnapshots(unittest.TestCase):
    def test_groups_by_date(self):
        lanes = [
            LaneRecord(name="a", primary_model="anthropic/claude-opus-5-5",
                       start_time="2026-09-24T10:00:00Z",
                       input_tokens=50000, output_tokens=5000,
                       total_cost=3.0, request_count=1, nesting_depth=0),
            LaneRecord(name="b", primary_model="google-antigravity/gemini-3.8-flash",
                       start_time="2026-09-24T14:00:00Z",
                       input_tokens=20000, output_tokens=2000,
                       total_cost=0.0, request_count=1, nesting_depth=1),
            LaneRecord(name="c", primary_model="google-antigravity/gemini-3.8-flash",
                       start_time="2026-09-25T10:00:00Z",
                       input_tokens=25000, output_tokens=2500,
                       total_cost=0.0, request_count=1, nesting_depth=1),
        ]
        snapshots = compute_daily_snapshots(lanes, {})
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0].date, "2026-09-24")
        self.assertEqual(snapshots[0].total_lanes, 2)
        self.assertEqual(snapshots[1].date, "2026-09-25")
        self.assertEqual(snapshots[1].total_lanes, 1)
        # Day 1: 55000 total, 55000 premium (opus is premium) = opus share
        # Actually: opus: 55000 tokens (premium), flash: 22000 tokens (not premium)
        # total = 77000, premium share = 55000/77000 = 71.4%
        self.assertGreater(snapshots[0].opus_codex_token_share, 70)


class TestBaseline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self._orig_cache = CACHE_DIR

    def test_compute_and_save_load(self):
        import lane_quality
        lane_quality.CACHE_DIR = Path(self.temp.name)

        lanes = [
            LaneRecord(name="a", primary_model="anthropic/claude-opus-5-5",
                       input_tokens=50000, output_tokens=5000,
                       total_cost=3.0, request_count=5),
            LaneRecord(name="b", primary_model="google-antigravity/gemini-3.8-flash",
                       input_tokens=20000, output_tokens=2000,
                       total_cost=0.0, request_count=10),
        ]
        baseline = compute_baseline(lanes, {}, days=14)
        self.assertEqual(baseline.days, 14)
        self.assertGreater(baseline.opus_codex_token_share, 0)

        save_baseline(baseline)
        loaded = load_baseline()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.days, 14)
        self.assertAlmostEqual(loaded.opus_codex_token_share,
                               baseline.opus_codex_token_share, places=2)

        lane_quality.CACHE_DIR = self._orig_cache


class TestDegradationDetection(unittest.TestCase):
    def test_approval_drop_flagged(self):
        baseline = Baseline(
            overall_first_pass_approval=80.0,
            opus_codex_token_share=30.0,
        )
        current = {
            "flash": ModelMetrics(
                model_id="flash", prs_touched=10,
                first_pass_approvals=5, first_pass_approval_rate=50.0,
            ),
        }
        flags = detect_degradation(current, baseline, current_opus_share=30.0)
        self.assertTrue(any("First-pass approval" in f for f in flags))

    def test_opus_share_rise_flagged(self):
        baseline = Baseline(
            overall_first_pass_approval=80.0,
            opus_codex_token_share=20.0,
        )
        current = {
            "opus": ModelMetrics(
                model_id="opus", prs_touched=5,
                first_pass_approvals=4, first_pass_approval_rate=80.0,
            ),
        }
        flags = detect_degradation(current, baseline, current_opus_share=35.0)
        self.assertTrue(any("Opus/Codex token share" in f for f in flags))

    def test_no_flag_when_stable(self):
        baseline = Baseline(
            overall_first_pass_approval=80.0,
            opus_codex_token_share=30.0,
        )
        current = {
            "flash": ModelMetrics(
                model_id="flash", prs_touched=10,
                first_pass_approvals=8, first_pass_approval_rate=80.0,
            ),
        }
        flags = detect_degradation(current, baseline, current_opus_share=30.0)
        self.assertEqual(flags, [])


class TestReportRendering(unittest.TestCase):
    def test_renders_markdown(self):
        metrics = {
            "flash": ModelMetrics(
                model_id="google-antigravity/gemini-3.8-flash",
                lane_count=10, completed_count=9, failed_count=1,
                total_cost=0.0, prs_merged=5,
                first_pass_approval_rate=80.0,
            ),
        }
        report = render_report([], metrics, None, [], days=7)
        self.assertIn("Lane Quality Report", report)
        self.assertIn("gemini-3.8-flash", report)
        self.assertIn("80%", report)

    def test_baseline_report(self):
        report = render_report([], {}, None, [], days=14, is_baseline=True)
        self.assertIn("Baseline Report", report)

    def test_flags_in_report(self):
        flags = ["⚠️ Test degradation flag"]
        report = render_report([], {}, None, flags, days=7)
        self.assertIn("Degradation Flags", report)
        self.assertIn("Test degradation flag", report)


class TestLaneRecordSerialization(unittest.TestCase):
    def test_to_dict_roundtrip(self):
        rec = LaneRecord(name="test", primary_model="flash",
                         total_cost=1.5, pr_urls=["url1"])
        d = rec.to_dict()
        self.assertEqual(d["name"], "test")
        self.assertEqual(d["total_cost"], 1.5)
        self.assertIsInstance(d, dict)


class TestCollectAllLanes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_collects_from_directory_structure(self):
        import lane_quality
        orig = lane_quality.SESSIONS_DIR
        lane_quality.SESSIONS_DIR = Path(self.temp.name)

        # Create structure: project_dir/session_timestamp_dir/agent.jsonl
        project = Path(self.temp.name) / "test-project"
        session_dir = project / "2026-09-25T00-00-00_test-session-id"
        session_dir.mkdir(parents=True)
        _write_session(str(session_dir), [
            FIXTURE_SESSION_LINE,
            FIXTURE_ASSISTANT_MSG_FLASH,
            FIXTURE_SESSION_EXIT_DISPOSE,
        ], name="TestAgent")

        # Also create a main session file
        main_session = project / "2026-09-25T00-00-00_main-session.jsonl"
        main_session.write_text(
            FIXTURE_SESSION_LINE + "\n" + FIXTURE_ASSISTANT_MSG_OPUS + "\n",
            encoding="utf-8",
        )

        lanes = collect_all_lanes(days=1)
        self.assertGreaterEqual(len(lanes), 1)

        lane_quality.SESSIONS_DIR = orig


class TestReviewVerdictParsing(unittest.TestCase):
    def test_state_changes_requested(self):
        self.assertEqual(_parse_review_verdict({"state": "CHANGES_REQUESTED"}), "REQUEST-CHANGES")

    def test_state_approved(self):
        self.assertEqual(_parse_review_verdict({"state": "APPROVED"}), "APPROVE")

    def test_commented_with_request_changes_body(self):
        review = {"state": "COMMENTED", "body": "verdict: REQUEST-CHANGES\nPlease fix tests"}
        self.assertEqual(_parse_review_verdict(review), "REQUEST-CHANGES")

    def test_commented_with_approve_body(self):
        review = {"state": "COMMENTED", "body": "APPROVE\nLGTM!"}
        self.assertEqual(_parse_review_verdict(review), "APPROVE")

    def test_commented_with_safe_as_is(self):
        review = {"state": "COMMENTED", "body": "SAFE_AS_IS\nLooks good"}
        self.assertEqual(_parse_review_verdict(review), "APPROVE")

    def test_commented_with_plain_discussion(self):
        review = {"state": "COMMENTED", "body": "Could you clarify line 42?"}
        self.assertEqual(_parse_review_verdict(review), "COMMENTED")


class TestPRNodeConversion(unittest.TestCase):
    def test_converts_node_with_reviews_and_ci(self):
        node = {
            "url": "https://github.com/Bavariance/polysimulator/pull/5000",
            "number": 5000,
            "state": "MERGED",
            "merged": True,
            "mergedAt": "2026-09-20T12:00:00Z",
            "createdAt": "2026-09-20T10:00:00Z",
            "additions": 100,
            "deletions": 20,
            "reviews": {
                "nodes": [
                    {"state": "COMMENTED", "submittedAt": "2026-09-20T10:30:00Z", "body": "verdict: REQUEST-CHANGES"},
                    {"state": "APPROVED", "submittedAt": "2026-09-20T11:30:00Z", "body": "LGTM"},
                ]
            },
            "commits": {
                "nodes": [
                    {"commit": {"statusCheckRollup": {"state": "FAILURE"}}}
                ]
            },
        }
        rec = _pr_node_to_record(node, "Bavariance/polysimulator")
        self.assertEqual(rec.number, 5000)
        self.assertTrue(rec.merged)
        self.assertEqual(rec.review_count, 2)
        self.assertEqual(rec.request_changes_count, 1)
        self.assertEqual(rec.approve_count, 1)
        self.assertEqual(rec.ci_failures_first_push, 1)


class TestPRCache(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_save_and_load_cache(self):
        import lane_quality
        orig_cache_file = lane_quality.PR_CACHE_FILE
        orig_dir = lane_quality.CACHE_DIR
        test_dir = Path(self.temp.name)
        lane_quality.CACHE_DIR = test_dir
        lane_quality.PR_CACHE_FILE = test_dir / "pr_cache.json"

        cache_data = {
            "https://github.com/Wladefant/super-board/pull/1": {
                "url": "https://github.com/Wladefant/super-board/pull/1",
                "number": 1,
                "repo": "Wladefant/super-board",
                "merged": True,
            }
        }
        _save_pr_cache(cache_data)
        loaded = _load_pr_cache()
        self.assertEqual(len(loaded), 1)
        self.assertTrue(loaded["https://github.com/Wladefant/super-board/pull/1"]["merged"])

        lane_quality.PR_CACHE_FILE = orig_cache_file
        lane_quality.CACHE_DIR = orig_dir


class TestFollowUpsAndReverts(unittest.TestCase):
    def test_detects_follow_up_and_revert(self):
        rec = PRRecord(
            url="https://github.com/Wladefant/super-board/pull/100",
            number=100,
            repo="Wladefant/super-board",
            merged=True,
            merged_at="2026-09-20T12:00:00Z",
        )
        records = {rec.url: rec}
        all_nodes = {
            "Wladefant/super-board": [
                {
                    "number": 101,
                    "url": "https://github.com/Wladefant/super-board/pull/101",
                    "title": "fix: follow up for #100",
                    "createdAt": "2026-09-21T10:00:00Z",
                },
                {
                    "number": 102,
                    "url": "https://github.com/Wladefant/super-board/pull/102",
                    "title": "Revert #100 due to bug",
                    "createdAt": "2026-09-22T10:00:00Z",
                },
            ]
        }
        _detect_follow_ups_and_reverts(records, all_nodes)
        self.assertIn("https://github.com/Wladefant/super-board/pull/101", rec.follow_up_prs)
        self.assertIn("https://github.com/Wladefant/super-board/pull/102", rec.follow_up_prs)
        self.assertTrue(rec.reverted)


class TestModelMetricsReviewsPerMergedPR(unittest.TestCase):
    def test_reviews_per_merged_pr_calculation(self):
        lanes = [
            LaneRecord(name="lane1", primary_model="flash", pr_urls=["https://github.com/Wladefant/super-board/pull/1"]),
            LaneRecord(name="lane2", primary_model="flash", pr_urls=["https://github.com/Wladefant/super-board/pull/2"]),
        ]
        pr_records = {
            "https://github.com/Wladefant/super-board/pull/1": PRRecord(
                url="https://github.com/Wladefant/super-board/pull/1",
                merged=True, review_count=3,
            ),
            "https://github.com/Wladefant/super-board/pull/2": PRRecord(
                url="https://github.com/Wladefant/super-board/pull/2",
                merged=True, review_count=1,
            ),
        }
        metrics = aggregate_by_model(lanes, pr_records)
        m = metrics["flash"]
        self.assertEqual(m.prs_merged, 2)
        self.assertEqual(m.reviews_received, 4)
        self.assertEqual(m.reviews_per_merged_pr, 2.0)

    def test_space_bunny_model_normalization(self):
        self.assertEqual(_normalize_model_id("opencode-go", "space-bunny"), "opencode-go/space-bunny-free")
        self.assertEqual(_normalize_model_id("", "space-bunny-free"), "opencode-go/space-bunny-free")


if __name__ == "__main__":
    unittest.main(verbosity=2)
