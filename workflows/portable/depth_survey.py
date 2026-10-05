#!/usr/bin/env python3
"""
depth_survey.py - report-only module-depth survey for the Gardener (`gardener.py --survey-depth`).

Source idea: the `improve-codebase-architecture` skill from https://github.com/mattpocock/skills
(MIT, Copyright (c) 2026 Matt Pocock; vendored under skills/improve-codebase-architecture/).
Tracking: https://github.com/Wladefant/super-board/issues/514

Interface (everything else is private to this module):
    survey(repo_root, since_days=90, limit=40) -> Survey      pure read of git + source, never edits code
    render_report(survey, template_path) -> str                one offline HTML file, no CDN
    render_markdown(survey) -> str                             issue comment body
    file_candidates(survey, repo, ...) -> list[dict]           one native sub-issue per Strong candidate

Vocabulary follows the `codebase-design` skill: module, interface, depth, seam, leverage, locality.
A module is "shallow" when its interface is nearly as wide as its implementation. Every candidate carries a
deletion-test result: `pass-through` (complexity vanishes), `concentrates` (complexity reappears across N
callers), or `inconclusive`.
"""

from __future__ import annotations

import ast
import datetime
import hashlib
import html
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".mjs", ".cjs")
SKIP_PARTS = {"node_modules", "dist", "build", ".next", "__pycache__", "vendor", "migrations", "alembic"}
FINGERPRINT_PREFIX = "depth-survey"
PARENT_TITLE = "Depth survey: standing parent"


@dataclass
class Candidate:
    path: str
    touches: int
    interface_size: int      # public names + their parameters: what a caller must learn
    impl_lines: int          # non-blank, non-comment lines
    callers: int             # other files that reference this module
    passthrough: int         # public functions that only forward their arguments
    public: int              # public functions/classes/exports
    deletion_test: str       # pass-through | concentrates | inconclusive
    evidence: str
    strength: str            # Strong | Worth exploring | Speculative
    fingerprint: str
    title: str = ""
    problem: str = ""
    solution: str = ""

    @property
    def depth_ratio(self) -> float:
        """Implementation lines behind each unit of interface. Low = shallow."""
        return self.impl_lines / max(1, self.interface_size)


@dataclass
class Survey:
    repo_root: str
    timestamp: str
    sha: str
    since_days: int
    hot_files_scanned: int
    candidates: List[Candidate] = field(default_factory=list)
    skipped_adr: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "candidates": [asdict(c) for c in self.candidates]}


# --------------------------------------------------------------------------- subprocess helpers

def _git(root: Path, *args: str, timeout: int = 60) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout,
        creationflags=_NO_WINDOW, encoding="utf-8", errors="replace",
    )
    return proc.stdout if proc.returncode == 0 else ""


def _gh(*args: str, timeout: int = 60) -> Tuple[int, str]:
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=timeout,
        creationflags=_NO_WINDOW, encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout if proc.returncode == 0 else proc.stderr)


# --------------------------------------------------------------------------- hot spots

def hot_spots(root: Path, since_days: int = 90, limit: int = 40) -> List[Tuple[str, int]]:
    """Source files ranked by how often `git log` touched them: where deepening pays off."""
    out = _git(root, "log", f"--since={since_days}.days", "--name-only", "--pretty=format:", timeout=600)
    counts: Dict[str, int] = {}
    for line in out.splitlines():
        p = line.strip().replace("\\", "/")
        if not p or not p.endswith(SOURCE_SUFFIXES):
            continue
        if SKIP_PARTS & set(p.split("/")):
            continue
        if _is_test(p):
            continue
        if (root / p).is_file():
            counts[p] = counts.get(p, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def _is_test(rel: str) -> bool:
    low = rel.lower()
    name = low.rsplit("/", 1)[-1]
    return (
        "/tests/" in low or low.startswith("tests/") or "__tests__" in low
        or name.startswith("test_") or ".test." in name or ".spec." in name or name.endswith("_test.py")
    )


# --------------------------------------------------------------------------- module measurement

def _measure_python(source: str) -> Tuple[int, int, int, int]:
    """(interface_size, impl_lines, passthrough, public) for a Python module."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0, 0, 0, 0
    interface = passthrough = public = 0

    def forwards(fn: ast.AST) -> bool:
        body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant))]
        if len(body) != 1:
            return False
        stmt = body[0]
        call = stmt.value if isinstance(stmt, (ast.Return, ast.Expr)) else None
        if not isinstance(call, ast.Call):
            return False
        params = {a.arg for a in fn.args.args + fn.args.kwonlyargs} - {"self", "cls"}
        used = {n.id for n in ast.walk(call) if isinstance(n, ast.Name)}
        # forwards when it adds no logic: every parameter is passed straight through
        return bool(params) and params <= used and not any(isinstance(n, (ast.BinOp, ast.IfExp, ast.Compare, ast.BoolOp)) for n in ast.walk(call))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            public += 1
            interface += 1 + len(node.args.args) + len(node.args.kwonlyargs)
            passthrough += 1 if forwards(node) else 0
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            public += 1
            interface += 1
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and (not sub.name.startswith("_") or sub.name == "__init__"):
                    interface += 1 + max(0, len(sub.args.args) - 1) + len(sub.args.kwonlyargs)
                    passthrough += 1 if forwards(sub) else 0
    return interface, _code_lines(source, "#"), passthrough, public


_EXPORT_RE = re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|class|const|let|var|type|interface|enum)\s+(\w+)?(\([^)]*\))?", re.M)
_FWD_RE = re.compile(r"=>\s*\(?\s*\w+\(([\w,\s]*)\)\s*\)?\s*;?\s*$|\{\s*return\s+\w+\(([\w,\s]*)\)\s*;?\s*\}")


def _measure_js(source: str) -> Tuple[int, int, int, int]:
    interface = public = passthrough = 0
    for m in _EXPORT_RE.finditer(source):
        public += 1
        params = (m.group(2) or "").strip("()")
        interface += 1 + (len([p for p in params.split(",") if p.strip()]) if params else 0)
    for line in source.splitlines():
        if line.lstrip().startswith("export") and _FWD_RE.search(line):
            passthrough += 1
    return interface, _code_lines(source, "//"), passthrough, public


def _code_lines(source: str, comment: str) -> int:
    return sum(1 for ln in source.splitlines() if ln.strip() and not ln.strip().startswith(comment))


def _callers(root: Path, rel: str) -> int:
    stem = Path(rel).stem
    if stem in ("index", "__init__", "main", "app"):
        stem = Path(rel).parent.name or stem
    out = _git(root, "grep", "-l", "-w", "-F", stem, "--", *[f"*{s}" for s in SOURCE_SUFFIXES])
    return len({p for p in out.splitlines() if p.strip() and p.strip() != rel})


# --------------------------------------------------------------------------- judgment (the deletion test)

def _judge(c: Candidate) -> None:
    shallow = c.public >= 3 and c.depth_ratio < 8
    forwarding = c.public >= 2 and c.passthrough / max(1, c.public) >= 0.6
    if forwarding and c.callers >= 1:
        c.deletion_test = "pass-through"
        c.evidence = (f"{c.passthrough} of {c.public} public names only forward their arguments; deleting the module "
                      f"moves no logic, the {c.callers} calling file(s) would call the target directly.")
    elif shallow and c.callers >= 2:
        c.deletion_test = "concentrates"
        c.evidence = (f"Deleting it would spread {c.impl_lines} lines of behaviour across {c.callers} calling files "
                      f"(interface size {c.interface_size}, depth {c.depth_ratio:.1f} lines per interface unit). "
                      "The module earns its keep but its interface is as wide as its implementation.")
    else:
        c.deletion_test = "inconclusive"
        c.evidence = (f"interface size {c.interface_size}, {c.impl_lines} implementation lines, {c.callers} calling "
                      "file(s); the heuristics cannot decide. A judgment lane must read the callers.")

    hot = c.touches >= 5
    if c.deletion_test == "pass-through" and (hot or c.callers >= 3):
        c.strength = "Strong"
    elif c.deletion_test == "concentrates" and hot and c.callers >= 3:
        c.strength = "Strong"
    elif c.deletion_test in ("pass-through", "concentrates") or (shallow and hot):
        c.strength = "Worth exploring"
    else:
        c.strength = "Speculative"

    name = Path(c.path).stem
    if c.deletion_test == "pass-through":
        c.title = f"Delete the {name} pass-through module"
        c.problem = f"The {name} module is shallow: its interface is as wide as its implementation and it adds no behaviour."
        c.solution = "Delete it and let callers cross the seam to the real module directly."
    else:
        c.title = f"Deepen the {name} module"
        c.problem = f"The {name} module has {c.public} public names over {c.impl_lines} lines and {c.callers} callers, so its interface is nearly as complex as its implementation."
        c.solution = "Shrink the interface and move the logic that leaks into callers behind it, so tests cross one interface."


def _fingerprint(path: str) -> str:
    return f"{FINGERPRINT_PREFIX}:{hashlib.sha1(path.encode()).hexdigest()[:12]}"


# --------------------------------------------------------------------------- survey

def survey(repo_root: Path, since_days: int = 90, limit: int = 40, min_public: int = 2) -> Survey:
    """Read-only. Never edits a file in `repo_root`."""
    root = Path(repo_root).resolve()
    sha = _git(root, "rev-parse", "HEAD").strip() or "unknown"
    spots = hot_spots(root, since_days, limit)
    rejected = _rejected_adr_text(root)
    result = Survey(str(root), datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                    sha, since_days, len(spots))
    for rel, touches in spots:
        try:
            source = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        measure = _measure_python if rel.endswith(".py") else _measure_js
        interface, impl, passthrough, public = measure(source)
        if public < min_public or impl == 0:
            continue
        cand = Candidate(rel, touches, interface, impl, _callers(root, rel), passthrough, public,
                         "inconclusive", "", "Speculative", _fingerprint(rel))
        _judge(cand)
        if rel in rejected or cand.fingerprint in rejected:
            result.skipped_adr.append(rel)
            continue
        if cand.deletion_test == "inconclusive" and not (cand.public >= 3 and cand.depth_ratio < 8):
            continue  # not shallow enough to report
        result.candidates.append(cand)
    order = {"Strong": 0, "Worth exploring": 1, "Speculative": 2}
    result.candidates.sort(key=lambda c: (order[c.strength], -c.touches, c.path))
    return result


def _rejected_adr_text(root: Path) -> Set[str]:
    """Paths and fingerprints named by an ADR that records a rejected candidate."""
    named: Set[str] = set()
    adr_dir = root / "docs" / "adr"
    if not adr_dir.is_dir():
        return named
    for adr in adr_dir.glob("*.md"):
        text = adr.read_text(encoding="utf-8", errors="replace")
        if not re.search(r"\b(rejected|declined|won't|will not)\b", text, re.I):
            continue
        named.update(re.findall(FINGERPRINT_PREFIX + r":[0-9a-f]{12}", text))
        named.update(re.findall(r"[\w./-]+\.(?:py|tsx?|m?js|cjs)", text))
    return named


# --------------------------------------------------------------------------- rendering

def render_report(sv: Survey, template_path: Path) -> str:
    """One offline HTML file: the template's inline CSS plus one card per candidate."""
    template = Path(template_path).read_text(encoding="utf-8")
    style = re.search(r"<style>.*?</style>", template, re.S).group(0)
    esc = html.escape
    repo = esc(Path(sv.repo_root).name)
    cards = []
    for i, c in enumerate(sv.candidates):
        css = {"Strong": "Strong", "Worth exploring": "Worth", "Speculative": "Speculative"}[c.strength]
        callers = f"{c.callers} caller{'' if c.callers == 1 else 's'}"
        cards.append(f"""    <article class="candidate" id="c{i}">
      <h2>{esc(c.title)}</h2>
      <div class="badges"><span class="badge {css}">{esc(c.strength)}</span><span class="badge">{c.touches} commits in {sv.since_days} days</span></div>
      <p class="files">{esc(c.path)}</p>
      <p class="test"><strong>Deletion test: {esc(c.deletion_test)}.</strong> {esc(c.evidence)}</p>
      <div class="pair">
        <figure><figcaption>Before</figcaption>{_svg_before(c, i)}</figure>
        <figure><figcaption>After</figcaption>{_svg_after(c, i)}</figure>
      </div>
      <p><strong>Problem.</strong> {esc(c.problem)}</p>
      <p><strong>Solution.</strong> {esc(c.solution)}</p>
      <ul class="wins"><li>locality: change and bugs concentrate in one module</li><li>leverage: {callers} share one interface</li></ul>
    </article>""")
    top = sv.candidates[0] if sv.candidates else None
    top_html = (f'<p><a href="#c0">{esc(top.title)}</a>: {esc(top.strength)}, deletion test {esc(top.deletion_test)}, '
                f'{top.touches} commits in {sv.since_days} days.</p>' if top else "<p>No candidate cleared the bar.</p>")
    when = esc(sv.timestamp[:16].replace("T", " ")) + " UTC"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Architecture review for {repo}</title>
{style}
</head>
<body>
<main>
  <header>
    <h1>Architecture review for {repo}</h1>
    <p class="legend">{when} · commit {esc(sv.sha[:12])} · {sv.hot_files_scanned} hot files from the last {sv.since_days} days</p>
    <p class="legend">solid box = module · bar on a box = its interface · dashed line = seam · red arrow = leakage · thick dark box = deep module</p>
  </header>
  <section id="candidates">
{chr(10).join(cards)}
  </section>
  <section id="top-recommendation">
    <h2>Top recommendation</h2>
    {top_html}
  </section>
</main>
</body>
</html>
"""


_CALLER_W, _CALLER_GAP, _SEAM_Y = 52, 8, 80


def _callers_row(c: Candidate) -> Tuple[str, List[float]]:
    """Up to five caller boxes, centred; the fifth reads `+N` when more files call the module."""
    n = max(1, min(5, c.callers))
    x0 = (320 - (n * _CALLER_W + (n - 1) * _CALLER_GAP)) / 2
    boxes, centres = [], []
    for i in range(n):
        x = x0 + i * (_CALLER_W + _CALLER_GAP)
        label = f"+{c.callers - 4}" if i == 4 and c.callers > 5 else "caller"
        boxes.append(f'<rect class="mod" x="{x:g}" y="16" width="{_CALLER_W}" height="28"/>'
                     f'<text x="{x + _CALLER_W / 2:g}" y="34" text-anchor="middle">{label}</text>')
        centres.append(x + _CALLER_W / 2)
    return "".join(boxes), centres


def _markers(key: str) -> str:
    """Arrow heads for one SVG. Ids are global to the page, so `key` must be unique per SVG."""
    head = '<path d="M0,0 L8,4 L0,8 z" class="{cls}"/>'
    return ("<defs>"
            f'<marker id="call-{key}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">{head.format(cls="head-call")}</marker>'
            f'<marker id="leak-{key}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">{head.format(cls="head-leak")}</marker>'
            "</defs>")


def _label(text: str, limit: int = 24) -> str:
    return html.escape(text if len(text) <= limit else text[: limit - 1] + "…")


def _svg_before(c: Candidate, i: int) -> str:
    """The shallow module: its interface bar is as wide as the module, and calls cross the seam to all of it."""
    row, centres = _callers_row(c)
    kind = "call" if c.deletion_test == "pass-through" else "leak"
    span = 200 / max(1, len(centres) - 1)
    arrows = "".join(f'<path class="{kind}" marker-end="url(#{kind}-{i}-before)" d="M{x:g},44 L{60 + j * span if len(centres) > 1 else 160:g},118"/>'
                     for j, x in enumerate(centres))
    return (f'<svg viewBox="0 0 320 200" role="img" aria-label="before: {len(centres)} callers reach across the seam into the whole of {_label(Path(c.path).stem)}">'
            f'{_markers(f"{i}-before")}{row}<line class="seam" x1="10" y1="{_SEAM_Y}" x2="310" y2="{_SEAM_Y}"/>{arrows}'
            '<rect class="iface" x="40" y="120" width="240" height="8"/>'
            f'<rect class="mod" x="40" y="128" width="240" height="44"/><text x="160" y="155" text-anchor="middle">{_label(Path(c.path).stem)}</text>'
            '</svg>')


def _svg_after(c: Candidate, i: int) -> str:
    """The deep module: callers meet at one narrow interface; a pass-through module is gone."""
    row, centres = _callers_row(c)
    gone = c.deletion_test == "pass-through"
    name = "the real module" if gone else _label(Path(c.path).stem)
    note = f"{_label(Path(c.path).stem, 18)} deleted" if gone else "one small interface"
    joins = "".join(f'<path class="call" d="M{x:g},44 L160,66"/>' for x in centres)
    return (f'<svg viewBox="0 0 320 200" role="img" aria-label="after: callers meet at one small interface of {name}">'
            f'{_markers(f"{i}-after")}{row}{joins}<path class="call" marker-end="url(#call-{i}-after)" d="M160,66 L160,102"/>'
            f'<line class="seam" x1="10" y1="{_SEAM_Y}" x2="310" y2="{_SEAM_Y}"/>'
            '<rect class="iface" x="136" y="104" width="48" height="8"/>'
            f'<rect class="deepmod" x="60" y="112" width="200" height="72"/>'
            f'<text x="160" y="143" text-anchor="middle">{name}<tspan class="note" x="160" dy="18">{note}</tspan></text>'
            '</svg>')


def find_template() -> Path:
    """report-template.html from the repo checkout, else from the installed Veyyon profile skills."""
    rel = Path("skills") / "improve-codebase-architecture" / "report-template.html"
    for base in (Path(__file__).resolve().parents[2], Path.home() / ".veyyon" / "profiles" / "default" / "agent"):
        if (base / rel).is_file():
            return base / rel
    raise FileNotFoundError("report-template.html not found; install the improve-codebase-architecture skill")


def render_markdown(sv: Survey, report_path: Optional[str] = None) -> str:
    rows = "\n".join(
        f"| {c.strength} | `{c.path}` | {c.deletion_test} | {c.touches} | {c.callers} | {c.evidence} |" for c in sv.candidates)
    head = (f"## Depth survey {sv.timestamp}\n\nCommit `{sv.sha[:12]}`, {sv.hot_files_scanned} hot files from the last "
            f"{sv.since_days} days, {len(sv.candidates)} candidate(s), {len(sv.skipped_adr)} skipped by ADR. "
            "Report only: no code was changed.\n\n")
    table = ("| Strength | Module | Deletion test | Commits | Callers | Evidence |\n|---|---|---|---|---|---|\n" + rows) if rows else "No candidate cleared the bar."
    tail = f"\n\nOffline HTML report: `{report_path}`" if report_path else ""
    return head + table + tail


# --------------------------------------------------------------------------- filing (live only)

def _existing_fingerprints(repo: str) -> Set[str]:
    rc, out = _gh("issue", "list", "-R", repo, "--state", "all", "--search", f"{FINGERPRINT_PREFIX} in:body",
                  "--limit", "200", "--json", "body")
    if rc != 0:
        raise RuntimeError(f"cannot read existing survey issues from {repo}: {out.strip()}")
    seen: Set[str] = set()
    for issue in json.loads(out or "[]"):
        seen.update(re.findall(FINGERPRINT_PREFIX + r":[0-9a-f]{12}", issue.get("body") or ""))
    return seen


def _standing_parent(repo: str, dry_run: bool) -> Optional[int]:
    rc, out = _gh("issue", "list", "-R", repo, "--state", "open", "--search", f'"{PARENT_TITLE}" in:title',
                  "--json", "number,title")
    if rc == 0:
        for issue in json.loads(out or "[]"):
            if issue["title"] == PARENT_TITLE:
                return issue["number"]
    if dry_run:
        return None
    body = ("Weightless anchor for the periodic module-depth survey (Gardener `--survey-depth`). "
            "Each Strong candidate is a native sub-issue of this issue. No code, branch or PR lives here.\n\n"
            "Source: https://github.com/Wladefant/super-board/issues/510")
    rc, out = _gh("issue", "create", "-R", repo, "--title", PARENT_TITLE, "--body", body)
    if rc != 0:
        raise RuntimeError(f"cannot create the standing parent in {repo}: {out.strip()}")
    return int(out.strip().rsplit("/", 1)[-1])


def _sub_issue_body(c: Candidate, sv: Survey, parent: Optional[int]) -> str:
    parent_line = f"#{parent}" if parent else "the standing depth-survey parent"
    return f"""<!-- fingerprint: {c.fingerprint} -->
Parent: {parent_line}

## Scope
{c.problem} {c.solution}
Files: `{c.path}`. Report only: this issue proposes no interface yet.

## Acceptance Criteria
- [ ] A person picked this candidate and a `--grill` session recorded a decision (deepen, delete, or reject with an ADR).
- [ ] If accepted: the change lands with the module's tests crossing one interface.

## Dependencies & Parent
Parent: {parent_line}

## Owner
Wladefant

## State & Blockers
Open. Waiting for a person to choose.

## Branch/PR/Exact Head
Survey commit `{sv.sha}`.

## Evidence
Deletion test: **{c.deletion_test}**. {c.evidence}
Strength: {c.strength}. Commits touching it in {sv.since_days} days: {c.touches}. Fingerprint: `{c.fingerprint}`

## Next Action
Ask which candidate to explore; run `improve-codebase-architecture --grill` on the pick.

## Authorization
Report-only survey (https://github.com/Wladefant/super-board/issues/510). No code change authorised by this issue.
"""


def file_candidates(sv: Survey, repo: str, dry_run: bool = True, max_new: int = 5,
                    seen: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    """One native sub-issue per Strong candidate, deduped by fingerprint. Never edits code."""
    try:
        seen = _existing_fingerprints(repo) if seen is None else seen
    except RuntimeError as exc:
        if not dry_run:
            raise  # live filing without a dedupe set would duplicate issues
        print(f"[WARN] dedupe unavailable in dry-run: {exc}", file=sys.stderr)
        seen = set()
    eligible = [c for c in sv.candidates if c.strength == "Strong" and c.fingerprint not in seen][:max(0, min(max_new, 5))]
    results: List[Dict[str, Any]] = []
    parent = _standing_parent(repo, dry_run) if eligible else None
    for c in eligible:
        if dry_run:
            results.append({"fingerprint": c.fingerprint, "title": c.title, "state": "planned", "url": "(dry-run)"})
            continue
        rc, out = _gh("issue", "create", "-R", repo, "--title", c.title, "--body", _sub_issue_body(c, sv, parent),
                      "--label", "kind:gardener,risk:low")
        if rc != 0:  # target repo may not have these labels
            rc, out = _gh("issue", "create", "-R", repo, "--title", c.title, "--body", _sub_issue_body(c, sv, parent))
        if rc != 0:
            results.append({"fingerprint": c.fingerprint, "title": c.title, "state": "error", "url": out.strip()})
            continue
        url = out.strip()
        number = url.rsplit("/", 1)[-1]
        if parent:
            rc2, nid = _gh("api", f"repos/{repo}/issues/{number}", "-q", ".id")
            if rc2 == 0:
                _gh("api", "-X", "POST", f"repos/{repo}/issues/{parent}/sub_issues", "-F", f"sub_issue_id={nid.strip()}")
        seen.add(c.fingerprint)
        results.append({"fingerprint": c.fingerprint, "title": c.title, "state": "created", "url": url})
    return results


def post_summary(sv: Survey, repo: str, issue: int, report_path: Optional[str]) -> bool:
    rc, _ = _gh("issue", "comment", str(issue), "-R", repo, "--body", render_markdown(sv, report_path))
    return rc == 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Report-only module-depth survey.")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--since-days", type=int, default=90)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--out", default=None, help="directory for the offline HTML report")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    sv = survey(Path(a.repo_root), a.since_days, a.limit)
    tpl = find_template()
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"architecture-review-{Path(sv.repo_root).name}.html").write_text(render_report(sv, tpl), encoding="utf-8")
    print(json.dumps(sv.to_dict(), indent=2) if a.json else render_markdown(sv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
