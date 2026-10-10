#!/usr/bin/env python3
"""test_practices_registry.py - Standard-library unittests for practices.json registry.

Covers seed uniqueness, required fields, evidence URLs, legal status shape,
and protection from unsupported adopted claims.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
PRACTICES_JSON = REPO_ROOT / "practices.json"
HOSTING_JSON = REPO_ROOT / "hosting.json"

URL_PATTERN = re.compile(r"^https?://[^\s/$.?#].[^\s]*$")
LEGAL_STATUSES = {"adopted", "proposed", "rejected", "not-applicable"}
REQUIRED_PRACTICE_FIELDS = {
    "id",
    "title",
    "what",
    "why",
    "applies_when",
    "how",
    "cost",
    "projects",
}
EXPECTED_SEED_IDS = {
    "pagespeed-check",
    "bitwarden-us-project-collections",
    "consent-mode-v2",
    "search-consoles-bing-indexnow",
    "cookieless-web-vitals",
    "dokploy-env-safety",
}


def validate_practice_status(repo: str, record: Any) -> None:
    """Validate a single project status record against legal status contracts.

    Contract:
      - adopted: requires valid evidence URL; must not be an unsupported/checklist claim
      - proposed: requires valid issue URL
      - rejected: requires reason string
      - not-applicable: requires reason string
    """
    if not isinstance(record, dict):
        raise ValueError(f"Project '{repo}' status record must be a dict, got {type(record).__name__}")

    status = record.get("status")
    if not status or status not in LEGAL_STATUSES:
        raise ValueError(
            f"Project '{repo}' has illegal status '{status}'. Must be one of {sorted(LEGAL_STATUSES)}"
        )

    if status == "adopted":
        evidence = record.get("evidence")
        if not evidence or not isinstance(evidence, str) or not URL_PATTERN.match(evidence):
            raise ValueError(
                f"Project '{repo}' has status 'adopted' but missing or invalid 'evidence' URL: {evidence!r}"
            )
        # Protect against claiming adopted from an open checklist alone
        if record.get("checklist_only") is True:
            raise ValueError(
                f"Project '{repo}' claims adopted from an open checklist alone, which is forbidden."
            )
    elif status == "proposed":
        issue = record.get("issue")
        if not issue or not isinstance(issue, str) or not URL_PATTERN.match(issue):
            raise ValueError(
                f"Project '{repo}' has status 'proposed' but missing or invalid 'issue' URL: {issue!r}"
            )
    elif status in {"rejected", "not-applicable"}:
        reason = record.get("reason")
        if not reason or not isinstance(reason, str) or not reason.strip():
            raise ValueError(
                f"Project '{repo}' has status '{status}' but missing non-empty 'reason' string: {reason!r}"
            )


def validate_practices_registry(data: Any) -> Dict[str, Any]:
    """Validate complete practices registry data structure.

    Raises ValueError on any contract or schema violation.
    """
    if not isinstance(data, dict):
        raise ValueError(f"Registry root must be a JSON object, got {type(data).__name__}")

    schema = data.get("schema")
    if schema != 1:
        raise ValueError(f"Registry schema must be 1, got {schema!r}")

    practices = data.get("practices")
    if not isinstance(practices, list):
        raise ValueError(f"'practices' must be a list, got {type(practices).__name__}")

    seen_ids = set()
    for idx, practice in enumerate(practices):
        if not isinstance(practice, dict):
            raise ValueError(f"Practice entry at index {idx} must be a dict")

        missing_fields = REQUIRED_PRACTICE_FIELDS - set(practice.keys())
        if missing_fields:
            raise ValueError(
                f"Practice entry {practice.get('id', f'at index {idx}')} missing required fields: {sorted(missing_fields)}"
            )

        practice_id = practice["id"]
        if not isinstance(practice_id, str) or not practice_id.strip():
            raise ValueError(f"Practice entry at index {idx} has empty or non-string id")
        if practice_id in seen_ids:
            raise ValueError(f"Duplicate practice id '{practice_id}' found at index {idx}")
        seen_ids.add(practice_id)

        title = practice["title"]
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"Practice '{practice_id}' has empty title")

        what = practice["what"]
        if not isinstance(what, str) or not what.strip():
            raise ValueError(f"Practice '{practice_id}' has empty 'what' description")

        why = practice["why"]
        if not isinstance(why, str) or not URL_PATTERN.match(why):
            raise ValueError(f"Practice '{practice_id}' has invalid 'why' evidence URL: {why!r}")

        applies_when = practice["applies_when"]
        if not isinstance(applies_when, list) or not applies_when:
            raise ValueError(f"Practice '{practice_id}' applies_when must be a non-empty list")
        for cond in applies_when:
            if not isinstance(cond, str) or not cond.strip():
                raise ValueError(f"Practice '{practice_id}' has empty condition in applies_when")

        how = practice["how"]
        if not isinstance(how, str) or not how.strip():
            raise ValueError(f"Practice '{practice_id}' has empty 'how' link/reference")

        cost = practice["cost"]
        if not isinstance(cost, str) or not cost.strip():
            raise ValueError(f"Practice '{practice_id}' has empty 'cost' description")

        projects = practice["projects"]
        if not isinstance(projects, dict):
            raise ValueError(f"Practice '{practice_id}' projects must be a mapping, got {type(projects).__name__}")
        if not projects:
            raise ValueError(f"Practice '{practice_id}' projects mapping cannot be empty")

        for repo, record in projects.items():
            if not isinstance(repo, str) or not repo.strip():
                raise ValueError(f"Practice '{practice_id}' contains invalid empty repo key")
            validate_practice_status(repo, record)

    return data


class PracticesRegistryTest(unittest.TestCase):
    """Test suite verifying practices.json existence, schema, seeds, and safety guards."""

    def test_registry_file_exists_beside_hosting_json(self) -> None:
        """Verify practices.json exists beside hosting.json at the repository root."""
        self.assertTrue(HOSTING_JSON.is_file(), f"hosting.json must exist at {HOSTING_JSON}")
        self.assertTrue(PRACTICES_JSON.is_file(), f"practices.json must exist at {PRACTICES_JSON}")

    def test_registry_loads_valid_json(self) -> None:
        """Verify practices.json parses as valid JSON and satisfies root schema."""
        self.assertTrue(PRACTICES_JSON.is_file(), f"Missing {PRACTICES_JSON}")
        with open(PRACTICES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data.get("schema"), 1)
        self.assertIsInstance(data.get("practices"), list)

    def test_seed_uniqueness_and_required_seed_presence(self) -> None:
        """Verify seed IDs are strictly unique and contain all six requested practices."""
        with open(PRACTICES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        validate_practices_registry(data)
        practice_ids = [p["id"] for p in data["practices"]]
        self.assertEqual(len(practice_ids), len(set(practice_ids)), "All practice IDs must be unique")
        self.assertTrue(
            EXPECTED_SEED_IDS.issubset(set(practice_ids)),
            f"Missing expected seeds: {EXPECTED_SEED_IDS - set(practice_ids)}",
        )

    def test_required_fields_and_types_on_every_entry(self) -> None:
        """Verify every practice entry adheres to all required fields and string types."""
        with open(PRACTICES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        for p in data["practices"]:
            for field in REQUIRED_PRACTICE_FIELDS:
                self.assertIn(field, p, f"Practice {p.get('id')} missing {field}")
            self.assertIsInstance(p["applies_when"], list)
            self.assertTrue(len(p["applies_when"]) > 0)
            self.assertIsInstance(p["projects"], dict)
            self.assertTrue(len(p["projects"]) > 0)

    def test_evidence_urls_are_well_formed_http_or_https(self) -> None:
        """Verify 'why' fields and 'adopted' evidence fields are valid URLs."""
        with open(PRACTICES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        for p in data["practices"]:
            self.assertTrue(
                URL_PATTERN.match(p["why"]),
                f"Practice {p['id']} 'why' is not a valid URL: {p['why']}",
            )
            for repo, rec in p["projects"].items():
                if rec["status"] == "adopted":
                    self.assertTrue(
                        URL_PATTERN.match(rec["evidence"]),
                        f"Practice {p['id']} project {repo} adopted evidence not a valid URL: {rec['evidence']}",
                    )
                elif rec["status"] == "proposed":
                    self.assertTrue(
                        URL_PATTERN.match(rec["issue"]),
                        f"Practice {p['id']} project {repo} proposed issue not a valid URL: {rec['issue']}",
                    )

    def test_legal_status_shape_and_no_guessed_statuses(self) -> None:
        """Verify every project record uses only valid statuses with required contracts."""
        with open(PRACTICES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        for p in data["practices"]:
            for repo, rec in p["projects"].items():
                status = rec.get("status")
                self.assertIn(status, LEGAL_STATUSES, f"Illegal status '{status}' in {p['id']}:{repo}")
                if status == "adopted":
                    self.assertIn("evidence", rec)
                    self.assertNotIn("issue", rec)
                elif status == "proposed":
                    self.assertIn("issue", rec)
                    self.assertNotIn("evidence", rec)
                elif status in {"rejected", "not-applicable"}:
                    self.assertIn("reason", rec)

    def test_protection_from_unsupported_adopted_claims(self) -> None:
        """Ensure validator rejects unsupported adopted claims, open checklists, and malformed records."""
        # 1. Adopted without evidence
        bad_adopted_no_evidence = {
            "schema": 1,
            "practices": [
                {
                    "id": "sample-practice",
                    "title": "Sample",
                    "what": "Sample",
                    "why": "https://github.com/Wladefant/pinthread/issues/379",
                    "applies_when": ["web"],
                    "how": "skill://sample",
                    "cost": "free",
                    "projects": {"Wladefant/pinthread": {"status": "adopted"}},
                }
            ],
        }
        with self.assertRaises(ValueError) as ctx:
            validate_practices_registry(bad_adopted_no_evidence)
        self.assertIn("missing or invalid 'evidence' URL", str(ctx.exception))

        # 2. Adopted claiming open checklist alone
        bad_checklist_claim = {
            "schema": 1,
            "practices": [
                {
                    "id": "sample-practice",
                    "title": "Sample",
                    "what": "Sample",
                    "why": "https://github.com/Wladefant/pinthread/issues/379",
                    "applies_when": ["web"],
                    "how": "skill://sample",
                    "cost": "free",
                    "projects": {
                        "Wladefant/pinthread": {
                            "status": "adopted",
                            "evidence": "https://github.com/Wladefant/pinthread/issues/374",
                            "checklist_only": True,
                        }
                    },
                }
            ],
        }
        with self.assertRaises(ValueError) as ctx:
            validate_practices_registry(bad_checklist_claim)
        self.assertIn("open checklist alone", str(ctx.exception))

        # 3. Invalid status string
        bad_status_string = {
            "schema": 1,
            "practices": [
                {
                    "id": "sample-practice",
                    "title": "Sample",
                    "what": "Sample",
                    "why": "https://github.com/Wladefant/pinthread/issues/379",
                    "applies_when": ["web"],
                    "how": "skill://sample",
                    "cost": "free",
                    "projects": {"Wladefant/pinthread": {"status": "completed"}},
                }
            ],
        }
        with self.assertRaises(ValueError) as ctx:
            validate_practices_registry(bad_status_string)
        self.assertIn("illegal status 'completed'", str(ctx.exception))

        # 4. Proposed without issue URL
        bad_proposed_no_issue = {
            "schema": 1,
            "practices": [
                {
                    "id": "sample-practice",
                    "title": "Sample",
                    "what": "Sample",
                    "why": "https://github.com/Wladefant/pinthread/issues/379",
                    "applies_when": ["web"],
                    "how": "skill://sample",
                    "cost": "free",
                    "projects": {"Wladefant/pinthread": {"status": "proposed"}},
                }
            ],
        }
        with self.assertRaises(ValueError) as ctx:
            validate_practices_registry(bad_proposed_no_issue)
        self.assertIn("missing or invalid 'issue' URL", str(ctx.exception))

        # 5. Duplicate IDs rejected
        bad_duplicate_id = {
            "schema": 1,
            "practices": [
                {
                    "id": "dup",
                    "title": "One",
                    "what": "One",
                    "why": "https://github.com/Wladefant/pinthread/issues/379",
                    "applies_when": ["web"],
                    "how": "skill://sample",
                    "cost": "free",
                    "projects": {"Wladefant/pinthread": {"status": "proposed", "issue": "https://github.com/Wladefant/pinthread/issues/379"}},
                },
                {
                    "id": "dup",
                    "title": "Two",
                    "what": "Two",
                    "why": "https://github.com/Wladefant/pinthread/issues/379",
                    "applies_when": ["web"],
                    "how": "skill://sample",
                    "cost": "free",
                    "projects": {"Wladefant/pinthread": {"status": "proposed", "issue": "https://github.com/Wladefant/pinthread/issues/379"}},
                },
            ],
        }
        with self.assertRaises(ValueError) as ctx:
            validate_practices_registry(bad_duplicate_id)
        self.assertIn("Duplicate practice id 'dup'", str(ctx.exception))

    def test_conservative_status_decisions_for_open_issues(self) -> None:
        """Verify Bing IndexNow (Pinthread #374, open issue/checklist) is not marked adopted."""
        with open(PRACTICES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        practices_by_id = {p["id"]: p for p in data["practices"]}
        bing_practice = practices_by_id["search-consoles-bing-indexnow"]
        pinthread_record = bing_practice["projects"]["Wladefant/pinthread"]
        self.assertEqual(
            pinthread_record["status"],
            "proposed",
            "Pinthread #374 is open with open checklist and Schema.org validator open; status must be 'proposed', not 'adopted'.",
        )
        self.assertEqual(
            pinthread_record["issue"],
            "https://github.com/Wladefant/pinthread/issues/374",
        )


if __name__ == "__main__":
    unittest.main()
