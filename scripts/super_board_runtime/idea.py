"""Draft, independently judge, and normalize ideas before any GitHub write."""
from __future__ import annotations

import json
from typing import Callable

from .normalize import REQUIRED_INTAKE_SECTIONS, IssueSnapshot, normalize_intake
from .project import ProjectSnapshot


class IdeaError(ValueError):
    pass


def candidates(raw: str) -> list[dict[str, str]]:
    value = json.loads(raw)
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        raise IdeaError("Expected 1 to 10 issue drafts")
    for issue in value:
        if not isinstance(issue, dict) or set(issue) != {"title", "body"}:
            raise IdeaError("Each draft must contain only title and body")
        if any(not isinstance(issue[k], str) or not issue[k].strip() for k in issue):
            raise IdeaError("Draft title and body must be nonempty strings")
    return value


def lint(issue: dict[str, str]) -> str | None:
    snapshot = IssueSnapshot(kind="issue", event="opened", number=None, url=None,
                             node_id=None, state="open", **issue)
    project = ProjectSnapshot(project_owner="draft", project_number=0,
                              items=(), fields={}, hit_cap=False)
    return normalize_intake(snapshot, project).blocked_reason


def decompose(sentence: str, model: Callable[[str, str], str], *, context: str,
              max_rewrites: int = 3) -> dict:
    sentence = sentence.strip()
    if not sentence or "\n" in sentence:
        raise IdeaError("Provide one nonempty idea sentence on one line")
    contract = (
        "Return only a JSON array of 1 to 10 objects with title and body strings. "
        "Each body must use these Markdown headings: "
        + ", ".join(REQUIRED_INTAKE_SECTIONS)
        + ". Use concrete Given/When/Then acceptance criteria and a test surface. "
        "Priority must be P0/P1/P2/P3; Work type build/docs/research/proof/decision/risk. "
        "Environment constraint may be none. Branch route must be staging or staging-frankfurt. "
        "Use only the supplied target milestone. Do not invent requirements or file anything. "
    )
    raw = model("draft", contract + "\nTarget context: " + context + "\nIdea: " + sentence)
    history = []
    for attempt in range(max_rewrites + 1):
        # Judge always runs in a fresh completion, including after every rewrite.
        raw = model("judge", contract + "\nIndependently judge scope, measurable outcomes, "
                    "and title/body agreement. Correct the candidates before returning them. "
                    "\nTarget context: " + context + "\nIdea: " + sentence + "\nCandidates: " + raw)
        try:
            issues = candidates(raw)
            failures = [lint(issue) for issue in issues]
        except (ValueError, TypeError) as exc:
            issues, failures = [], [str(exc)]
        history.append({"attempt": attempt, "candidates": raw, "failures": failures})
        if issues and not any(failures):
            return {"idea": sentence, "drafts": issues, "history": history, "filed": []}
        if attempt < max_rewrites:
            raw = model("draft", contract + "\nRewrite these rejected drafts. "
                        "\nTarget context: " + context + "\nIdea: " + sentence
                        + "\nCandidates: " + raw + "\nLint failures: " + json.dumps(failures))
    raise IdeaError("Rewrite limit reached; no issues filed: " + json.dumps(failures))


def file_drafts(result: dict, create: Callable[[dict[str, str]], str]) -> None:
    # Validate the whole batch again before the first irreversible write.
    issues = candidates(json.dumps(result["drafts"]))
    failures = [lint(issue) for issue in issues]
    if any(failures):
        raise IdeaError("No issues filed: " + json.dumps(failures))
    for issue in issues:
        result["filed"].append(create(issue))
