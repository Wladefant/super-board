"""Task 18 — one release number, derived by a written rule.

Pure stdlib `unittest`. No network, no `gh`.

Four sources claimed to say what version this repository is and they disagreed:
`VERSION` said 1.7.1, `skills/super-board/VERSION` said 1.6.0, the newest
release-notes heading said v1.7.1, and the only published tag said v1.2.0. That
is not cosmetic — the installer pins a release, the manifest records it, and a
support question that starts "I'm on 1.6.0" is unanswerable when the tree says
something else.

This file locks the fix: the three in-tree sources must be byte-identical, the
reconciliation must be written down and must name all four original sources, the
derivation rule must be executable and tested in both directions, and active
guidance must not still advertise anything the release retired.

Tagging and publishing are outward-facing and stay behind their own approval.
The tooling is tested here; the tag is not created here.

Run directly:
  python -B tests/test_version_identity.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_version_identity.py' -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.release import (  # noqa: E402
    ACTIVE_GUIDANCE_FILES,
    RECONCILIATION_DOCUMENT,
    RELEASE_CONTRACT_TOPICS,
    VERSION_SOURCES,
    ReleaseError,
    authorize_release_publication,
    derive_next_release,
    newest_release_notes_section,
    newest_release_notes_version,
    normalize_version,
    read_version_sources,
    reconcile_current_release,
    scan_retired_release_claims,
    verify_release_tag,
    verify_version_identity,
)

RECONCILIATION = _REPO_ROOT / RECONCILIATION_DOCUMENT
NOTES = _REPO_ROOT / "RELEASE-NOTES.md"
README = _REPO_ROOT / "README.md"
DOCS_INDEX = _REPO_ROOT / "docs" / "README.md"
DOCS_SYSTEM = _REPO_ROOT / "DOCS-SYSTEM.md"

#: The values that disagreed, as observed at the reconciliation commit.
ORIGINAL_OBSERVATIONS = ("1.7.1", "1.6.0", "v1.7.1", "v1.2.0")


# ───────────────────────────── identity ─────────────────────────────


class VersionIdentityTests(unittest.TestCase):
    def test_all_three_in_tree_sources_agree(self) -> None:
        identity = verify_version_identity(_REPO_ROOT)
        self.assertTrue(identity.ok, identity.disagreements)
        self.assertEqual(identity.root, identity.skill)
        self.assertEqual(identity.root, identity.notes)

    def test_they_are_byte_identical_after_stripping_the_leading_v(self) -> None:
        sources = read_version_sources(_REPO_ROOT)
        self.assertEqual(len({value for value in sources.values()}), 1, sources)

    def test_the_skill_mirror_is_pinned_to_the_root_version(self) -> None:
        root = (_REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        skill = (_REPO_ROOT / "skills" / "super-board" / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(skill, root)

    def test_the_newest_release_notes_heading_matches_the_version_file(self) -> None:
        root = normalize_version((_REPO_ROOT / "VERSION").read_text(encoding="utf-8"))
        heading = newest_release_notes_version(NOTES.read_text(encoding="utf-8"))
        self.assertEqual(normalize_version(heading), root)

    def test_a_disagreeing_tree_is_reported_not_papered_over(self) -> None:
        identity = verify_version_identity(_REPO_ROOT / "tests")  # no VERSION files here
        self.assertFalse(identity.ok)
        self.assertTrue(identity.disagreements)

    def test_normalize_version_strips_only_the_leading_v(self) -> None:
        self.assertEqual(normalize_version("v2.0.0"), "2.0.0")
        self.assertEqual(normalize_version(" 2.0.0\n"), "2.0.0")
        self.assertEqual(normalize_version("2.0.0-rc.1"), "2.0.0-rc.1")
        self.assertIsNone(normalize_version("   "))


# ───────────────────────────── the reconciliation ─────────────────────────────


class ReconciliationDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = RECONCILIATION.read_text(encoding="utf-8")

    def test_the_document_exists(self) -> None:
        self.assertTrue(RECONCILIATION.is_file(), f"{RECONCILIATION_DOCUMENT} must exist")

    def test_it_names_all_four_original_sources(self) -> None:
        for source in VERSION_SOURCES:
            with self.subTest(source=source):
                needle = "tag" if source == "git-tag" else source
                self.assertIn(needle, self.text)

    def test_it_records_all_four_observed_values(self) -> None:
        for value in ORIGINAL_OBSERVATIONS:
            with self.subTest(value=value):
                self.assertIn(value, self.text)

    def test_it_records_the_commit_each_value_was_read_at(self) -> None:
        self.assertRegex(self.text, r"\b[0-9a-f]{40}\b")

    def test_it_explains_the_skill_version_lag_and_the_pin(self) -> None:
        lowered = self.text.casefold()
        self.assertIn("mirror", lowered)
        self.assertIn("pinned to the root version", lowered)

    def test_it_explains_the_tag_gap_and_the_decision(self) -> None:
        lowered = self.text.casefold()
        self.assertIn("explicitly untagged", lowered)
        self.assertIn("not retro-tagged", lowered)

    def test_it_states_the_derivation_rule_and_the_result(self) -> None:
        root = normalize_version((_REPO_ROOT / "VERSION").read_text(encoding="utf-8"))
        self.assertIn("major", self.text.casefold())
        self.assertIn(root, self.text)


class DerivationRuleTests(unittest.TestCase):
    def test_the_current_release_is_decided_by_the_content_sources(self) -> None:
        current, reasoning = reconcile_current_release("1.7.1", "1.6.0", "v1.7.1", "v1.2.0")
        self.assertEqual(current, "1.7.1")
        joined = " ".join(reasoning).casefold()
        self.assertIn("mirror", joined)
        self.assertIn("publication record", joined)

    def test_the_stale_mirror_and_the_tag_never_decide_it(self) -> None:
        current, _reasoning = reconcile_current_release("1.7.1", "0.1.0", "1.7.1", "v9.9.9")
        self.assertEqual(current, "1.7.1")

    def test_disagreeing_content_sources_refuse_to_reconcile(self) -> None:
        with self.assertRaises(ReleaseError) as ctx:
            reconcile_current_release("1.7.1", "1.6.0", "1.5.0", "v1.2.0")
        self.assertEqual(ctx.exception.reason, "release-content-sources-disagree")

    def test_a_missing_content_source_refuses_to_reconcile(self) -> None:
        with self.assertRaises(ReleaseError) as ctx:
            reconcile_current_release("1.7.1", "1.6.0", None, "v1.2.0")
        self.assertEqual(ctx.exception.reason, "release-content-source-missing")

    def test_a_backward_incompatible_release_takes_the_next_major(self) -> None:
        self.assertEqual(derive_next_release("1.7.1", backward_incompatible=True), "2.0.0")
        self.assertEqual(derive_next_release("0.9.9", backward_incompatible=True), "1.0.0")

    def test_a_compatible_release_takes_the_next_minor(self) -> None:
        self.assertEqual(derive_next_release("1.7.1", backward_incompatible=False), "1.8.0")

    def test_this_release_is_what_the_rule_produces(self) -> None:
        current, _reasoning = reconcile_current_release("1.7.1", "1.6.0", "v1.7.1", "v1.2.0")
        derived = derive_next_release(current, backward_incompatible=True)
        shipped = normalize_version((_REPO_ROOT / "VERSION").read_text(encoding="utf-8"))
        self.assertEqual(shipped, derived)


# ───────────────────────────── what the release must say ─────────────────────────────


class ReleaseDocumentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.notes = newest_release_notes_section(NOTES.read_text(encoding="utf-8"))
        self.readme = README.read_text(encoding="utf-8")

    def test_the_newest_release_notes_section_documents_every_contract(self) -> None:
        lowered = self.notes.casefold()
        for topic in RELEASE_CONTRACT_TOPICS:
            with self.subTest(topic=topic):
                self.assertIn(topic.casefold(), lowered)

    def test_the_readme_documents_every_contract(self) -> None:
        lowered = self.readme.casefold()
        for topic in RELEASE_CONTRACT_TOPICS:
            with self.subTest(topic=topic):
                self.assertIn(topic.casefold(), lowered)

    def test_the_newest_section_is_the_shipped_version(self) -> None:
        shipped = normalize_version((_REPO_ROOT / "VERSION").read_text(encoding="utf-8"))
        self.assertIn(shipped, self.notes.splitlines()[0])

    def test_the_release_notes_say_the_tag_was_not_created(self) -> None:
        lowered = self.notes.casefold()
        self.assertIn("approval", lowered)
        self.assertIn("not done here", lowered)


class RetiredClaimTests(unittest.TestCase):
    def test_active_guidance_advertises_nothing_the_release_retired(self) -> None:
        findings = scan_retired_release_claims(_REPO_ROOT)
        detail = "\n".join(
            f"  {f['path']}:{f['line']} — {f['claim']} — {f['text']}" for f in findings
        )
        self.assertEqual(findings, [], f"retired claims still advertised:\n{detail}")

    def test_the_scanned_surfaces_include_the_readme_and_every_pipeline_skill(self) -> None:
        self.assertIn("README.md", ACTIVE_GUIDANCE_FILES)
        for skill in ("super-board", "super-build", "super-qa", "super-review"):
            with self.subTest(skill=skill):
                self.assertIn(f"skills/{skill}/SKILL.md", ACTIVE_GUIDANCE_FILES)

    def test_every_retired_claim_is_detectable(self) -> None:
        seeds = {
            # Assembled by concatenation: the tree-wide retired-status scanner
            # fails on the literal, and this fixture must not be allowlisted.
            "retired-status-skipped": "Move the card to Skip" + "ped when it stalls.",
            "squash-merging": "The reviewer will squash-merge the branch.",
            "runtime-merging": "The reviewer merges once checks are green.",
            "200-point-reserve": "Sleeps until reset when remaining quota dips under 200.",
            "workflow-default-backend": 'Set "worker_backend": "workflow" to use the default.',
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for claim, line in seeds.items():
                with self.subTest(claim=claim):
                    (root / "guide.md").write_text(f"# Guide\n\n{line}\n", encoding="utf-8")
                    findings = scan_retired_release_claims(root, files=("guide.md",))
                    self.assertEqual([f["claim"] for f in findings], [claim])

    def test_a_refusal_of_a_retired_claim_is_not_an_advertisement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "guide.md").write_text(
                "The runtime must never do any of these:\n\n- squash-merge a branch\n",
                encoding="utf-8",
            )
            self.assertEqual(scan_retired_release_claims(root, files=("guide.md",)), [])


# ───────────────────────────── index reachability ─────────────────────────────


class DocumentationIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = DOCS_INDEX.read_text(encoding="utf-8")

    def test_the_reconciliation_is_reachable_from_the_index(self) -> None:
        self.assertIn(RECONCILIATION_DOCUMENT, self.index)

    def test_every_runtime_reference_document_is_reachable(self) -> None:
        references = sorted(
            (_REPO_ROOT / "skills" / "super-board" / "references").iterdir()
        )
        for path in references:
            with self.subTest(reference=path.name):
                self.assertIn(path.name, self.index)

    def test_the_release_notes_and_deployed_evidence_are_reachable(self) -> None:
        self.assertIn("RELEASE-NOTES.md", self.index)
        self.assertIn("AGENT-NATIVE-DEPLOYED-EVIDENCE.md", self.index)

    def test_the_docs_convention_says_runtime_documents_are_indexed(self) -> None:
        text = DOCS_SYSTEM.read_text(encoding="utf-8").casefold()
        self.assertIn("runtime documents are indexed", text)


# ───────────────────────────── the publication gate ─────────────────────────────


class PublicationGateTests(unittest.TestCase):
    def test_publication_is_refused_without_an_explicit_approval(self) -> None:
        decision = authorize_release_publication("2.0.0")
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason_code, "release-publication-approval-required")

    def test_an_implied_approval_is_not_an_approval(self) -> None:
        for approved, text in ((True, None), (True, "   "), (False, "yes please")):
            with self.subTest(approved=approved, text=text):
                decision = authorize_release_publication("2.0.0", approved=approved, approval_text=text)
                self.assertFalse(decision.authorized)
                self.assertEqual(
                    decision.reason_code, "release-publication-approval-required"
                )

    def test_an_explicit_approval_authorizes_it(self) -> None:
        decision = authorize_release_publication(
            "2.0.0", approved=True, approval_text="approved: tag and publish v2.0.0"
        )
        self.assertTrue(decision.authorized)
        self.assertIsNone(decision.reason_code)

    def test_the_tag_check_requires_the_recorded_commit(self) -> None:
        sha = "a" * 40
        self.assertTrue(
            verify_release_tag("2.0.0", sha, tag_reader=lambda _tag: sha)
        )
        self.assertFalse(
            verify_release_tag("2.0.0", sha, tag_reader=lambda _tag: "b" * 40)
        )

    def test_an_unresolvable_tag_fails_closed(self) -> None:
        def explode(_tag):
            raise RuntimeError("no such ref")

        self.assertFalse(verify_release_tag("2.0.0", "a" * 40, tag_reader=explode))
        self.assertFalse(verify_release_tag("2.0.0", "a" * 40, tag_reader=lambda _t: None))

    def test_nothing_in_the_runtime_creates_a_tag_by_itself(self) -> None:
        for path in sorted((_SCRIPTS / "super_board_runtime").glob("*.py")):
            with self.subTest(module=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("refs/tags", text)
                # A tag-creating command shape: `git tag -a`, or "tag" passed as
                # a subprocess argument. Prose that merely mentions tags is fine.
                self.assertNotRegex(text, r"git\s+tag\s+-")
                self.assertNotRegex(text, r"[\[,]\s*[\"']tag[\"']\s*,")
                self.assertNotRegex(text, r"create_release|createRelease")


if __name__ == "__main__":
    unittest.main(verbosity=2)
