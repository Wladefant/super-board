#!/usr/bin/env python3
"""Fail a pull request that adds a workaround comment citing no issue.

A band-aid comment is how one lane's shortcut becomes the next lane's
precedent, so a workaround is only allowed when it names the issue that will
remove it. This gate reads nothing but the lines a pull request adds, and fails
on a marker word (HACK, WORKAROUND, FIXME, TODO) in a comment that carries no
issue reference: no ``https://github.com/<owner>/<repo>/issues/<N>``, no
``owner/repo#N``, no bare ``#N``.

Rules, kept narrow so the gate stays predictable:

* Only comment text is inspected. Code, string literals, and file types with no
  known comment syntax (Markdown, JSON, plain text, ...) are never scanned.
* The marker and its issue reference must be on the same added line. A comment
  block that links the issue on the next line is still a finding.
* Scanning is line-oriented, so a comment introducer inside a multi-line string
  reads as a comment. This gate errs toward reporting.

Usage:
    python workaround_comments.py [--base origin/main] [--repo-root .]

Exit codes: 0 clean, 1 findings, 2 git failure.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

MARKER_WORDS = ('HACK', 'WORKAROUND', 'FIXME', 'TODO')

# Comment introducers per language family. An extension absent from this table
# is not scanned at all, because guessing a comment syntax for an unknown file
# type is how a documentation bullet gets reported as a code comment. Teaching
# the gate a language means adding its extension to a family below.
COMMENT_SYNTAX = {
    extension: introducers
    for extensions, introducers in (
        (
            '.py .pyi .ps1 .psm1 .sh .bash .zsh .ksh .rb .pl .pm .yaml .yml '
            '.toml .cfg .ini .conf .mk .tf .hcl .ex .exs .r .jl .nim .cr',
            ('#',),
        ),
        (
            '.js .mjs .cjs .jsx .ts .tsx .mts .cts .java .kt .kts .scala .groovy '
            '.c .h .cc .cpp .cxx .hpp .hh .cs .go .rs .swift .php .dart .css '
            '.scss .less .sass .glsl .proto',
            ('//', '/*', '*'),
        ),
        ('.sql .lua .hs .elm .ada', ('--',)),
        ('.lisp .clj .cljs .edn .el .asm .s', (';',)),
        ('.html .htm .xhtml .xml .svg .vue .svelte', ('<!--',)),
    )
    for extension in extensions.split()
}

MARKER = re.compile(r'\b(?:' + '|'.join(MARKER_WORDS) + r')\b', re.IGNORECASE)
ISSUE_REFERENCE = re.compile(
    r'https://github\.com/[\w.-]+/[\w.-]+/issues/\d+'  # canonical issue URL
    r'|[\w.-]+/[\w.-]+#\d+'  # owner/repo#N
    r'|#\d+'  # bare #N
)
HUNK_HEADER = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
QUOTE_CHARS = ('"', "'", '`')


def comment_text(extension: str, line: str) -> str | None:
    """The comment portion of ``line``, or ``None`` when it carries none."""
    introducers = COMMENT_SYNTAX.get(extension)
    if not introducers:
        return None
    body = line.rstrip('\r\n')
    stripped = body.lstrip()
    for introducer in introducers:
        if stripped.startswith(introducer):
            return stripped
    start = _comment_start(body, introducers)
    return body[start:] if start is not None else None


def _comment_start(body: str, introducers: tuple) -> int | None:
    """Index where a trailing comment opens outside any quoted string."""
    quote = None
    escaped = False
    for index, char in enumerate(body):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in QUOTE_CHARS:
            quote = char
            continue
        for introducer in introducers:
            if body.startswith(introducer, index) and (index == 0 or body[index - 1] in ' \t'):
                return index
    return None


def line_finding(path: str, number: int, line: str) -> dict | None:
    """A finding when ``line`` is an added workaround comment linking no issue."""
    comment = comment_text(os.path.splitext(path)[1].lower(), line)
    if comment is None:
        return None
    marker = MARKER.search(comment)
    if marker is None or ISSUE_REFERENCE.search(comment):
        return None
    return {
        'path': path,
        'line': number,
        'marker': marker.group(0).upper(),
        'comment': comment.strip(),
    }


def _path_from_header(raw: str) -> str | None:
    """Destination path of a ``+++`` header, or ``None`` for a deleted file."""
    token = raw[4:].split('\t', 1)[0].strip()
    if token == '/dev/null':
        return None
    if len(token) > 1 and token[0] == '"' and token[-1] == '"':
        token = token[1:-1]
    for prefix in ('a/', 'b/'):
        if token.startswith(prefix):
            return token[2:]
    return token


def added_lines(diff: str):
    """Yield ``(path, line_number, text)`` for every line ``diff`` adds.

    Hunk bodies are consumed against the counts in their ``@@`` header, so a
    removed block, a context line, or the ``\\ No newline`` marker never
    advances a reported line number.
    """
    path = None
    number = 0
    remaining = None
    for raw in diff.splitlines():
        if remaining is not None:
            old_left, new_left = remaining
            if raw.startswith('+'):
                if path is not None:
                    yield path, number, raw[1:]
                number += 1
                new_left -= 1
            elif raw.startswith('-'):
                old_left -= 1
            elif raw.startswith(' '):
                number += 1
                old_left -= 1
                new_left -= 1
            remaining = None if old_left <= 0 and new_left <= 0 else (old_left, new_left)
            continue
        if raw.startswith('@@'):
            header = HUNK_HEADER.match(raw)
            if header:
                number = int(header.group(3))
                remaining = (int(header.group(2) or 1), int(header.group(4) or 1))
        elif raw.startswith('+++ '):
            path = _path_from_header(raw)


def lint(diff: str) -> list:
    """Every added workaround comment in ``diff`` that cites no issue."""
    return [finding for finding in
            (line_finding(path, number, text) for path, number, text in added_lines(diff))
            if finding]


def _git_diff(base: str, root: Path) -> str | None:
    result = subprocess.run(
        ['git', '-C', str(root), 'diff', '--no-color', '--unified=0', f'{base}...HEAD'],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
    )
    if result.returncode != 0:
        print(f'[workaround-comments] git diff failed: {result.stderr.strip()}', file=sys.stderr)
        return None
    return result.stdout


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base', default='origin/main', help='branch this change targets (default: origin/main)')
    parser.add_argument('--repo-root', default='.', help='checkout to diff (default: current directory)')
    args = parser.parse_args(argv)

    diff = _git_diff(args.base, Path(args.repo_root))
    if diff is None:
        return 2

    added = list(added_lines(diff))
    items = lint(diff)
    if not items:
        print(f'[workaround-comments] clean: {len(added)} added line(s) scanned against {args.base}')
        return 0

    print(f'[workaround-comments] {len(items)} added comment(s) cite no issue')
    for item in items:
        print(f'  {item["path"]}:{item["line"]}: {item["marker"]}: {item["comment"]}')
        if os.environ.get('GITHUB_ACTIONS') == 'true':
            print(f'::error file={item["path"]},line={item["line"]}::'
                  f'{item["marker"]} comment cites no issue; name the issue that removes it')
    print('Name the issue that removes the workaround, on the same line:')
    print('  https://github.com/<owner>/<repo>/issues/<N>  |  owner/repo#N  |  #N')
    print('If no issue exists yet, open one and link that.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
