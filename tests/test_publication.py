"""Task 9 — one fail-closed sanitizer in front of every GitHub write.

Pure stdlib `unittest`. **No credential material exists in this file.** Every
sentinel below is built by string concatenation so no contiguous secret-shaped
literal is ever present in the source, and the tests assert that no failure
report, log line, or exception message echoes a matched value.

Run directly:
  python -B tests/test_publication.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_publication.py' -v
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import importlib.util  # noqa: E402

from super_board_runtime import EXIT_UNSAFE  # noqa: E402
from super_board_runtime.publication import (  # noqa: E402
    ARTIFACT_CLASSIFICATIONS,
    PUBLICATION_SURFACES,
    PublicationError,
    UnsafePublication,
    publish,
    render_payload,
    sanitize_and_validate_publication,
)

_PUBLISH_CLI = _SCRIPTS / "super-board-publish.py"
_spec = importlib.util.spec_from_file_location("super_board_publish_cli", _PUBLISH_CLI)
assert _spec is not None and _spec.loader is not None
publish_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish_cli)

# ── Obvious non-secrets. Concatenated so the source holds no contiguous
#    secret-shaped literal, and long enough to look real to the detectors.
GITHUB_TOKEN = "gh" + "p_" + ("N" * 36)
FINE_GRAINED = "github" + "_pat_" + ("N" * 30)
DOKPLOY_KEY = "polysim" + "_mcp" + ("N" * 32)
AWS_KEY = "AKIA" + ("N" * 16)
GOOGLE_KEY = "AIza" + ("N" * 35)
BEARER = "Bearer " + ("N" * 40)
COOKIE_VALUE = "session=" + ("N" * 40)
PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY" + "-----\n" + ("N" * 64) + "\n-----END RSA PRIVATE KEY" + "-----"
)
DB_PASSWORD = "N" * 24
DB_URL = "postgresql://svc:" + DB_PASSWORD + "@db.internal.example:5432/app"
ENV_TOKEN_VALUE = "N" * 40
SHORT_SECRET = "N" * 6  # too short to substring-redact safely — must fail closed

# Every surface gets a case here. The completeness assertion below fails if the
# runtime grows a surface that this file does not exercise.
SURFACE_CASES: dict[str, str] = {
    "issue-create": "Steps to reproduce: open the board.",
    "issue-edit": "Updated acceptance criteria.",
    "pull-request-body": "Implements the routing contract.",
    "pull-request-comment": "Rebased onto staging.",
    "review-summary": "Two nits, both addressed.",
    "qa-comment": "Suite green on the tested SHA.",
    "check-output": "18 tests, 0 failures.",
    "commit-status": "exact-SHA QA passed.",
    "closure-comment": "Merged by a human, rebase.",
    "bug-report": "Route declaration missing on the card.",
    "release-text": "v1.4.0 — fail-closed publication.",
    "project-text-field": "staging-frankfurt",
    "dispatch-manifest": '{"cards": []}',
    "reconciliation-manifest": '{"quarantined": []}',
}


def _sanitize(text: str, environment=None, surface="qa-comment", **kwargs):
    return sanitize_and_validate_publication(
        text, environment or {}, surface=surface, **kwargs
    )


class RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, surface, text):
        self.calls.append((surface, text))
        return {"url": "https://github.com/Bavariance/polysimulator/issues/1#issuecomment-1"}


# ───────────────────────────── surface completeness ─────────────────────────────


class SurfaceCoverageTests(unittest.TestCase):
    def test_every_runtime_surface_has_a_test_case(self) -> None:
        self.assertEqual(
            tuple(sorted(SURFACE_CASES)),
            tuple(sorted(PUBLICATION_SURFACES)),
            "a publication surface was added to the runtime without a test case here",
        )

    def test_every_surface_routes_through_the_single_publisher(self) -> None:
        for surface, text in SURFACE_CASES.items():
            with self.subTest(surface=surface):
                writer = RecordingWriter()
                result = publish(surface, text, {}, writer=writer)
                self.assertTrue(result["published"])
                self.assertEqual(len(writer.calls), 1)
                self.assertEqual(writer.calls[0][0], surface)

    def test_an_unknown_surface_is_refused(self) -> None:
        writer = RecordingWriter()
        with self.assertRaises(PublicationError) as ctx:
            publish("slack-dm", "hello", {}, writer=writer)
        self.assertEqual(ctx.exception.reason, "publication-surface-unknown")
        self.assertEqual(writer.calls, [])


# ───────────────────────────── render-then-scan ordering ─────────────────────────────


class RenderOrderTests(unittest.TestCase):
    def test_a_secret_split_across_two_fragments_is_caught_after_rendering(self) -> None:
        # Neither fragment is secret-shaped on its own; the rendered whole is.
        fragments = ("QA excerpt: gh", "p_" + ("N" * 36) + " — end of excerpt")
        for fragment in fragments:
            self.assertEqual(_sanitize(fragment).redactions, (), f"{fragment!r} scanned alone")

        rendered = render_payload(fragments)
        sanitized = _sanitize(rendered)
        self.assertTrue(sanitized.safe)
        self.assertNotIn(GITHUB_TOKEN, sanitized.text)
        self.assertIn("github-token", [r.category for r in sanitized.redactions])

    def test_the_publisher_scans_the_complete_payload_not_the_fragments(self) -> None:
        writer = RecordingWriter()
        rendered = render_payload(("prefix gh", "p_" + ("N" * 36)))
        publish("qa-comment", rendered, {}, writer=writer)
        self.assertEqual(len(writer.calls), 1)
        self.assertNotIn(GITHUB_TOKEN, writer.calls[0][1])


# ───────────────────────────── redaction coverage ─────────────────────────────


class RedactionTests(unittest.TestCase):
    def _assert_redacted(self, text, category, secret, environment=None):
        sanitized = _sanitize(text, environment)
        self.assertNotIn(secret, sanitized.text, f"{category} survived redaction")
        self.assertIn(category, [r.category for r in sanitized.redactions])
        self.assertTrue(sanitized.safe)

    def test_configured_credential_environment_values(self) -> None:
        self._assert_redacted(
            f"the runner exported {ENV_TOKEN_VALUE} into the child process",
            "env-value",
            ENV_TOKEN_VALUE,
            {"SUPERBOARD_GITHUB_TOKEN": ENV_TOKEN_VALUE},
        )

    def test_github_token_patterns(self) -> None:
        self._assert_redacted(f"token {GITHUB_TOKEN} used", "github-token", GITHUB_TOKEN)
        self._assert_redacted(f"token {FINE_GRAINED} used", "github-token", FINE_GRAINED)

    def test_dokploy_keys(self) -> None:
        self._assert_redacted(f"x-api-key {DOKPLOY_KEY}", "dokploy-key", DOKPLOY_KEY)

    def test_cloud_keys(self) -> None:
        self._assert_redacted(f"aws {AWS_KEY} here", "cloud-key", AWS_KEY)
        self._assert_redacted(f"gcp {GOOGLE_KEY} here", "cloud-key", GOOGLE_KEY)

    def test_database_urls_with_embedded_credentials(self) -> None:
        self._assert_redacted(f"DSN: {DB_URL}", "credentialed-url", DB_PASSWORD)

    def test_authorization_headers(self) -> None:
        self._assert_redacted(
            f"request headers:\nAuthorization: {BEARER}\nAccept: application/json",
            "authorization-header",
            BEARER,
        )

    def test_cookies(self) -> None:
        self._assert_redacted(
            f"response headers:\nSet-Cookie: {COOKIE_VALUE}; Path=/", "cookie", COOKIE_VALUE
        )

    def test_private_key_markers(self) -> None:
        self._assert_redacted(f"deploy key:\n{PRIVATE_KEY}\n", "private-key", "N" * 64)

    def test_credential_bearing_command_arguments(self) -> None:
        self._assert_redacted(
            "ran: curl --token " + ("N" * 40) + " https://example.invalid",
            "credential-argument",
            "N" * 40,
        )
        self._assert_redacted(
            "ran: psql --password=" + ("N" * 20), "credential-argument", "N" * 20
        )

    def test_raw_command_logs(self) -> None:
        block = "```log\n" + ("N" * 40) + "\n```"
        self._assert_redacted(f"here is the tail:\n{block}\n", "raw-log", "N" * 40)

    def test_structured_tool_output(self) -> None:
        block = '```tool-output\n{"env": "' + ("N" * 40) + '"}\n```'
        self._assert_redacted(f"probe said:\n{block}\n", "tool-output", "N" * 40)

    def test_a_clean_payload_is_untouched(self) -> None:
        sanitized = _sanitize("Suite green on the tested SHA. 18 tests, 0 failures.")
        self.assertEqual(sanitized.redactions, ())
        self.assertTrue(sanitized.safe)
        self.assertEqual(sanitized.text, "Suite green on the tested SHA. 18 tests, 0 failures.")


# ───────────────────────────── fail closed ─────────────────────────────


class FailClosedTests(unittest.TestCase):
    def test_a_secret_that_survives_redaction_raises_with_no_write(self) -> None:
        # A credential-named environment value too short to substring-redact
        # safely: it is detected on the second scan and refused rather than
        # published or silently mangled.
        writer = RecordingWriter()
        with self.assertRaises(UnsafePublication) as ctx:
            publish(
                "qa-comment",
                f"the deploy step printed {SHORT_SECRET} to stdout",
                {"DEPLOY_SECRET": SHORT_SECRET},
                writer=writer,
            )
        self.assertEqual(ctx.exception.reason, "secret-survived-redaction")
        self.assertEqual(writer.calls, [], "a rejected payload must produce no partial write")
        self.assertEqual(ctx.exception.exit_code, EXIT_UNSAFE)

    def test_the_failure_report_names_the_category_and_location_only(self) -> None:
        with self.assertRaises(UnsafePublication) as ctx:
            _sanitize(
                f"the deploy step printed {SHORT_SECRET} to stdout",
                {"DEPLOY_SECRET": SHORT_SECRET},
            )
        report = str(ctx.exception) + repr(ctx.exception.findings) + json.dumps(
            [f.to_dict() for f in ctx.exception.findings], sort_keys=True
        )
        self.assertNotIn(SHORT_SECRET, report, "the failure report echoed the matched value")
        self.assertIn("env-value", report)
        self.assertIn("qa-comment", report)
        self.assertTrue(any(f.start >= 0 for f in ctx.exception.findings))

    def test_an_unclassified_binary_artifact_is_rejected(self) -> None:
        writer = RecordingWriter()
        with self.assertRaises(UnsafePublication) as ctx:
            publish(
                "bug-report",
                "screenshot attached",
                {},
                writer=writer,
                artifacts=({"name": "shot.png"},),
            )
        self.assertEqual(ctx.exception.reason, "binary-artifact-unclassified")
        self.assertEqual(writer.calls, [])

    def test_embedded_binary_content_is_rejected(self) -> None:
        writer = RecordingWriter()
        with self.assertRaises(UnsafePublication) as ctx:
            publish("bug-report", "log tail:\x00\x01\x02", {}, writer=writer)
        self.assertEqual(ctx.exception.reason, "binary-artifact-unclassified")
        self.assertEqual(writer.calls, [])

    def test_a_classified_artifact_is_accepted(self) -> None:
        writer = RecordingWriter()
        for classification in ARTIFACT_CLASSIFICATIONS:
            with self.subTest(classification=classification):
                result = publish(
                    "bug-report",
                    "screenshot attached",
                    {},
                    writer=writer,
                    artifacts=({"name": "shot", "classification": classification},),
                )
                self.assertTrue(result["published"])

    def test_dry_run_issues_zero_writes(self) -> None:
        writer = RecordingWriter()
        result = publish("qa-comment", "all green", {}, writer=writer, dry_run=True)
        self.assertFalse(result["published"])
        self.assertEqual(writer.calls, [])


# ───────────────────────────── CLI ─────────────────────────────


class PublishCliTests(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = publish_cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_the_safe_fixture_publishes(self) -> None:
        fixture = _REPO_ROOT / "tests" / "fixtures" / "publication-safe.json"
        code, out, _ = self._run(["publish", "--input", str(fixture), "--json"])
        self.assertEqual(code, 0)
        body = json.loads(out)
        self.assertTrue(body["safe"])
        self.assertEqual(body["github_writes"], 0)

    def test_the_unsafe_fixture_exits_78_with_zero_writes(self) -> None:
        fixture = _REPO_ROOT / "tests" / "fixtures" / "publication-unsafe.json"
        code, out, err = self._run(["publish", "--input", str(fixture), "--json"])
        self.assertEqual(code, EXIT_UNSAFE)
        self.assertEqual(out, "")
        self.assertIn("secret-survived-redaction", err)
        self.assertNotIn(SHORT_SECRET, err)

    def test_a_rendered_fixture_redacts_the_split_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload.json"
            path.write_text(
                json.dumps(
                    {
                        "surface": "qa-comment",
                        "template_fragments": ["excerpt: gh", "p_" + ("N" * 36)],
                    }
                ),
                encoding="utf-8",
            )
            code, out, _ = self._run(["publish", "--input", str(path), "--json"])
        self.assertEqual(code, 0)
        body = json.loads(out)
        self.assertNotIn(GITHUB_TOKEN, body["text"])
        self.assertIn("github-token", [r["category"] for r in body["redactions"]])

    def test_an_unknown_surface_exits_65(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload.json"
            path.write_text(json.dumps({"surface": "slack-dm", "text": "hi"}), encoding="utf-8")
            code, _, err = self._run(["publish", "--input", str(path), "--json"])
        self.assertEqual(code, 65)
        self.assertIn("publication-surface-unknown", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
