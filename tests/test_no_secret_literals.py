"""No committed file contains a contiguous secret-shaped literal.

Pure stdlib `unittest`. No network.

The runtime's own detector is the judge — `super_board_runtime.publication` is
the single authority on what a credential looks like, so a scanner with its own
opinion here would be a second one to keep in sync.

Why this matters even for a value that is obviously fake:

  * GitHub push protection scans by SHAPE, not by provenance. A repository that
    cannot be pushed is a repository whose release cannot ship, and the fix
    arrives at exactly the moment nobody wants to be editing test fixtures.
  * A grep for a leaked token across the estate returns this repository, and
    somebody has to prove the hit is harmless. Every fake that looks real costs
    that proof again.
  * The sanitizer's own tests need token-shaped inputs. They build them by
    concatenation, which is also the exact construction the boundary exists to
    defeat — `"gh" + "p_…"` is invisible to any per-fragment scanner and visible
    to the renderer. Writing the fixtures that way keeps the demonstration and
    the property in the same place.

Only the provider patterns are scanned. `authorization-header` and `cookie` also
match ordinary prose about headers, which this repository is full of on purpose;
the shapes below have no innocent reading.

Run directly:
  python -B tests/test_no_secret_literals.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.publication import _PATTERNS  # noqa: E402

#: Categories whose match is a credential shape and nothing else.
PROVIDER_CATEGORIES = frozenset({"github-token", "cloud-key", "dokploy-key", "private-key"})

#: Any auth scheme carrying a long opaque value. Kept separate from the
#: `authorization-header` mapping patterns, which match prose.
_SCHEME_CREDENTIAL_RE = re.compile(
    r"(?i)(?<![-\w])(?:Bearer|Basic|Digest|OAuth)[ \t]+[A-Za-z0-9._~+/=\-]{16,}"
)

_SKIPPED_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", ".pytest_cache"})


def _scanned_files():
    for path in sorted(_REPO_ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIPPED_DIRS for part in path.relative_to(_REPO_ROOT).parts):
            continue
        yield path


class NoSecretLiteralsTests(unittest.TestCase):
    def test_no_committed_file_carries_a_secret_shaped_literal(self) -> None:
        patterns = [(c, p) for c, p in _PATTERNS if c in PROVIDER_CATEGORIES]
        patterns.append(("scheme-credential", _SCHEME_CREDENTIAL_RE))
        hits: list[str] = []
        for path in _scanned_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            relative = path.relative_to(_REPO_ROOT).as_posix()
            if relative == Path(__file__).name or relative.endswith("/" + Path(__file__).name):
                continue  # this file names the categories, never a value
            for number, line in enumerate(text.splitlines(), start=1):
                for category, pattern in patterns:
                    if pattern.search(line):
                        # The location, never the value: a leak report that
                        # quotes the leak is a second leak.
                        hits.append(f"{relative}:{number} [{category}]")
        self.assertEqual(
            hits,
            [],
            "secret-shaped literals are committed; build them by concatenation instead:\n"
            + "\n".join(hits),
        )

    def test_the_scanner_would_actually_catch_one(self) -> None:
        # A green scan means nothing unless the scan can fail. Both halves of
        # this literal are innocuous; only the concatenation is a shape.
        planted = "gh" + "p_" + "D" * 36
        patterns = [(c, p) for c, p in _PATTERNS if c in PROVIDER_CATEGORIES]
        self.assertTrue(any(pattern.search(planted) for _c, pattern in patterns))


if __name__ == "__main__":
    unittest.main(verbosity=2)
