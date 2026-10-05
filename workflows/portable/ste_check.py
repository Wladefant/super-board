#!/usr/bin/env python3
"""
ste_check.py - deterministic "80% of the way to ASD-STE100" checker.

Rules come from the managed skill `ste-writing` (rules 1-15) and the checks
of https://github.com/0xpili/simplified-technical-english (scripts/ste_check.py):
sentence length, paragraph length, passive voice, semicolons, unapproved words
with replacements. The ASD dictionary (869 approved words) is NOT used. ASD does
not allow redistribution, and Karpathy and Suwandi both say to relax the
vocabulary rules, so the vocabulary check is a short replacement list.

The check never blocks by default. It exits 0 and reports. `--fail-under N`
turns it into a gate (an operator decision, off by default).

Subcommands:
  check [FILE|-]          report findings (default subcommand)
  rewrite-prompt [FILE|-] print the rewrite prompt plus the findings for the text

Options for check: --format auto|text|md|html, --max-words N (25),
  --warn-words N (20), --strict, --json, --summary-json, --metrics PATH,
  --source LABEL, --fail-under SCORE.

Pure standard library.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

MAX_WORDS = 25
WARN_WORDS = 20
MAX_PARAGRAPH_SENTENCES = 6

# The prompt from https://x.com/richardcsuwandi/status/2106617018225147907
REWRITE_PROMPT = (
    "Use ASD-STE100 as a guide for your responses. Write short sentences, use active voice, "
    "and keep terminology consistent. Relax the vocabulary rules when they make explanations "
    "awkward. Preserve technical precision and uncertainty."
)

# phrase -> replacement. Longest phrases are matched first.
REPLACEMENTS: Dict[str, str] = {
    "in order to": "to",
    "prior to": "before",
    "due to the fact that": "because",
    "in the event that": "if",
    "at this point in time": "now",
    "going forward": "from now on",
    "a number of": "some",
    "with regard to": "about",
    "with respect to": "about",
    "make use of": "use",
    "is able to": "can",
    "are able to": "can",
    "carry out": "do",
    "perform a check of": "check",
    "perform a check on": "check",
    "perform an analysis of": "analyze",
    "utilize": "use",
    "utilise": "use",
    "utilization": "use",
    "commence": "start",
    "terminate": "stop",
    "leverage": "use",
    "facilitate": "help",
    "subsequently": "then",
    "approximately": "about",
    "additionally": "also",
    "demonstrate": "show",
    "endeavor": "try",
    "ascertain": "find",
    "necessitate": "need",
    "consequently": "so",
    "furthermore": "also",
    "numerous": "many",
    "sufficient": "enough",
    "assistance": "help",
    "requirement": "need",
    "spin up": "start",
    "spin down": "stop",
    "figure out": "find",
    "kick off": "start",
    "bring up": "start",
    "hook up": "connect",
    "roll out": "release",
}

IDIOMS = [
    "smoking gun", "low-hanging fruit", "fire off", "land on", "deep dive", "silver bullet",
    "at the end of the day", "tip of the iceberg", "move the needle", "boil the ocean",
    "in the weeds", "rabbit hole", "game changer", "out of the box", "touch base",
]

# Participles that describe a state, not an action. "The PR is merged" is fine.
STATE_PARTICIPLES = {
    "done", "closed", "open", "opened", "merged", "queued", "blocked", "ready", "stopped",
    "finished", "based", "expected", "required", "needed", "installed", "enabled", "disabled",
    "set", "red", "green", "pinned", "bound", "tied", "used", "supposed", "allowed", "known",
    "named", "called", "located", "pending", "skipped", "cancelled", "canceled", "failed",
    "passed", "completed", "complete", "verified", "fixed", "deployed", "applied", "added",
    "removed", "shipped", "published", "released", "pushed", "built", "configured", "signed",
    "aligned", "rendered", "included", "listed", "recorded", "stored", "saved", "sent",
}
IRREGULAR_PARTICIPLES = (
    "given|taken|shown|seen|written|made|run|found|kept|left|held|sent|built|done|known|"
    "chosen|driven|broken|stolen|spoken|hidden|forgotten|begun|thrown|drawn|grown"
)
PASSIVE_RE = re.compile(
    r"\b(is|are|was|were|be|been|being)\s+(?:(?:not|also|then|already|still|never|always|just|now)\s+)?"
    rf"([a-z]+ed|{IRREGULAR_PARTICIPLES})\b(\s+by\b)?",
    re.IGNORECASE,
)
CONTRACTION_RE = re.compile(r"\b\w+'(?:t|s|re|ve|ll|d|m)\b", re.IGNORECASE)

STATE_WORDS = (
    "done", "finished", "merged", "blocked", "failed", "passed", "running", "stopped", "open",
    "closed", "ready", "needs", "need", "waiting", "fixed", "shipped", "installed", "complete",
    "completed", "verified", "started", "pending", "progress", "found", "cannot", "cause",
    "yes", "no", "result", "summary", "status", "confirmed", "reproduced",
)

SYNONYM_GROUPS = [
    ("lane", "worker", "agent"),
    ("pr", "pull request"),
    ("issue", "ticket"),
]

SKIP_LINE_RE = re.compile(r"^\s*(QA-RECEIPT|FLOW-QA|gh-quota-on-exit|reviewed-sha|delta-from)\b", re.IGNORECASE)
ABBREV = ["e.g.", "i.e.", "etc.", "vs.", "approx.", "no.", "incl."]


@dataclass
class Finding:
    rule: str
    severity: str  # error | warn | info
    line: int
    sentence: str
    message: str
    fix: str = ""


def detect_format(text: str) -> str:
    if re.search(r"</?(b|i|code|pre|a|u|s|blockquote|tg-spoiler)\b[^>]*>", text):
        return "html"
    return "md"


def strip_markup(text: str, fmt: str) -> List[Tuple[int, str]]:
    """Return (line_number, plain_text) lines with code, links and markup removed."""
    if fmt == "html":
        text = re.sub(r"<pre\b[^>]*>.*?</pre>", " CODE ", text, flags=re.S | re.I)
        text = re.sub(r"<code\b[^>]*>.*?</code>", "CODE", text, flags=re.S | re.I)
        text = re.sub(r"<a\b[^>]*>(.*?)</a>", r"\1", text, flags=re.S | re.I)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
    out: List[Tuple[int, str]] = []
    in_fence = False
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            out.append((no, ""))
            continue
        if in_fence or SKIP_LINE_RE.match(line):
            out.append((no, ""))
            continue
        if re.match(r"^\s*\|", line) or re.match(r"^\s*[-*_]{3,}\s*$", line):
            out.append((no, ""))
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        line = re.sub(r"^\s*>+\s?", "", line)
        is_item = bool(re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", line))
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"`[^`]*`", "CODE", line)
        line = re.sub(r"https?://\S+", "LINK", line)
        line = re.sub(r"(?:[A-Za-z]:)?[\\/][\w.\-\\/]+\.\w+", "PATH", line)
        line = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", line)
        out.append((no, ("\u0001" if is_item else "") + line.strip()))
    return out


def split_sentences(line: str) -> List[str]:
    protected = line
    for a in ABBREV:
        protected = protected.replace(a, a.replace(".", "\u2024"))
    protected = re.sub(r"(\d)\.(\d)", "\\1\u2024\\2", protected)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", protected)
    return [p.replace("\u2024", ".").strip() for p in parts if p.strip()]


def word_count(sentence: str) -> int:
    return len([w for w in re.findall(r"[A-Za-z0-9][\w'\-./]*", sentence)])


def replacement_re() -> re.Pattern:
    keys = sorted(REPLACEMENTS, key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b", re.IGNORECASE)


REPL_RE = replacement_re()


def check_text(text: str, fmt: str = "auto", max_words: int = MAX_WORDS,
               warn_words: int = WARN_WORDS, strict: bool = False) -> Dict:
    if fmt == "auto":
        fmt = detect_format(text)
    lines = strip_markup(text, fmt)
    findings: List[Finding] = []
    sentences: List[Tuple[int, str]] = []
    paragraph_sentences = 0
    paragraph_start = 0
    first_sentence_seen = False
    all_plain = []

    def close_paragraph() -> None:
        nonlocal paragraph_sentences
        if paragraph_sentences > MAX_PARAGRAPH_SENTENCES:
            findings.append(Finding(
                "paragraph-length", "warn", paragraph_start, "",
                f"Paragraph has {paragraph_sentences} sentences (max {MAX_PARAGRAPH_SENTENCES}).",
                "Split it. Put the result first."))
        paragraph_sentences = 0

    for no, plain in lines:
        if plain.startswith("\u0001"):
            close_paragraph()  # each list item is its own paragraph
            plain = plain[1:]
        if not plain:
            close_paragraph()
            continue
        if paragraph_sentences == 0:
            paragraph_start = no
        all_plain.append(plain)
        for sent in split_sentences(plain):
            if not re.search(r"[A-Za-z]{2,}", sent.replace("CODE", "").replace("LINK", "").replace("PATH", "")):
                continue
            sentences.append((no, sent))
            paragraph_sentences += 1
            n = word_count(sent)
            if n > max_words:
                findings.append(Finding("sentence-length", "error", no, sent,
                                        f"{n} words (max {max_words}).", "Split into two sentences."))
            elif n > warn_words:
                findings.append(Finding("sentence-length", "warn", no, sent,
                                        f"{n} words (aim for {warn_words} or fewer).", "Shorten or split."))
            if ";" in sent:
                findings.append(Finding("semicolon", "error", no, sent, "Semicolon.", "Write two sentences."))
            for m in PASSIVE_RE.finditer(sent):
                part = m.group(2).lower()
                if part in STATE_PARTICIPLES and not m.group(3):
                    continue
                findings.append(Finding("passive-voice", "warn", no, sent,
                                        f"Passive voice: '{m.group(0).strip()}'.",
                                        "Name who acts: 'The gate rejects the PR'."))
                break
            for m in REPL_RE.finditer(sent):
                key = m.group(1).lower()
                findings.append(Finding("plain-word", "warn", no, sent,
                                        f"'{m.group(1)}' is not plain.", f"Use '{REPLACEMENTS[key]}'."))
            low = sent.lower()
            for idiom in IDIOMS:
                if idiom in low:
                    findings.append(Finding("idiom", "warn", no, sent, f"Idiom: '{idiom}'.",
                                            "Say it literally."))
            if strict and CONTRACTION_RE.search(sent):
                findings.append(Finding("contraction", "warn", no, sent, "Contraction (strict mode).",
                                        "Write the full form."))
            if not first_sentence_seen:
                first_sentence_seen = True
    close_paragraph()

    # Rule 15: state in line 1. Only for texts with enough sentences to need it.
    if len(sentences) >= 4:
        first = sentences[0][1].lower()
        if not any(re.search(rf"\b{w}\b", first) for w in STATE_WORDS):
            findings.append(Finding("state-first", "warn", sentences[0][0], sentences[0][1],
                                    "Line 1 does not say the state (done, blocked, needs a decision).",
                                    "Start with the state."))

    # Rule 6: one word for one thing.
    joined = " ".join(all_plain).lower()
    for group in SYNONYM_GROUPS:
        present = [w for w in group if re.search(rf"\b{re.escape(w)}s?\b", joined)]
        if len(present) >= 2:
            findings.append(Finding("terminology", "info", 0, "",
                                    f"Mixed terms for one thing? Found: {', '.join(present)}.",
                                    "Pick one word if they mean the same thing."))

    total = len(sentences)
    errors = sum(1 for f in findings if f.severity == "error")
    warns = sum(1 for f in findings if f.severity == "warn")
    bad_sentences = len({(f.line, f.sentence) for f in findings if f.severity in ("error", "warn") and f.sentence})
    penalty = errors + 0.5 * (warns)
    score = 100.0 if total == 0 else max(0.0, round(100.0 * (1.0 - min(penalty, total) / total), 1))
    return {
        "score": score,
        "sentences": total,
        "errors": errors,
        "warnings": warns,
        "info": sum(1 for f in findings if f.severity == "info"),
        "flagged_sentences": bad_sentences,
        "words": sum(word_count(s) for _, s in sentences),
        "findings": [asdict(f) for f in findings],
    }


def append_metrics(path: str, result: Dict, source: str) -> None:
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        **{k: result[k] for k in ("score", "sentences", "errors", "warnings", "info", "flagged_sentences", "words")},
        "rules": sorted({f["rule"] for f in result["findings"]}),
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def render(result: Dict) -> str:
    lines = [
        f"STE score {result['score']}/100: {result['sentences']} sentences, "
        f"{result['errors']} errors, {result['warnings']} warnings, {result['info']} info."
    ]
    for f in result["findings"]:
        where = f"L{f['line']}" if f["line"] else "text"
        snippet = f" | {f['sentence'][:90]}" if f["sentence"] else ""
        lines.append(f"  {where} [{f['severity']}:{f['rule']}] {f['message']} {f['fix']}{snippet}".rstrip())
    return "\n".join(lines)


def read_input(path: Optional[str]) -> str:
    if not path or path == "-":
        return sys.stdin.buffer.read().decode("utf-8", errors="replace")
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("check", "rewrite-prompt"):
        argv = ["check"] + argv
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "rewrite-prompt"])
    ap.add_argument("file", nargs="?", default="-")
    ap.add_argument("--format", choices=["auto", "text", "md", "html"], default="auto")
    ap.add_argument("--max-words", type=int, default=MAX_WORDS)
    ap.add_argument("--warn-words", type=int, default=WARN_WORDS)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--summary-json", action="store_true", help="print one JSON line without findings")
    ap.add_argument("--metrics", help="append one JSON line of counts to this file")
    ap.add_argument("--source", default="cli", help="label stored in the metrics line")
    ap.add_argument("--fail-under", type=float, default=None,
                    help="exit 1 when the score is below this value (blocking mode, off by default)")
    args = ap.parse_intermixed_args(argv)
    text = read_input(args.file)
    fmt = "md" if args.format == "text" else args.format
    result = check_text(text, fmt, args.max_words, args.warn_words, args.strict)
    if args.metrics:
        try:
            append_metrics(args.metrics, result, args.source)
        except OSError:
            pass
    if args.command == "rewrite-prompt":
        print(REWRITE_PROMPT)
        print("\nFindings to fix (keep every fact, link, number and hedge):")
        print(render(result))
        print("\nText:\n" + text)
    elif args.json:
        print(json.dumps(result, indent=2))
    elif args.summary_json:
        print(json.dumps({k: v for k, v in result.items() if k != "findings"}))
    else:
        print(render(result))
    if args.fail_under is not None and result["score"] < args.fail_under:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
