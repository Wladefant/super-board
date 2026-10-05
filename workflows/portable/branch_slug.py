"""Turn an issue title and number into a safe, unique git branch name."""
from __future__ import annotations

import re
import unicodedata

_MAX = 60


def branch_name(number: int, title: str, taken: frozenset[str] = frozenset()) -> str:
    """Return `issue-<number>-<slug>`.

    Interface: one call. Behaviour behind it: unicode folding to ASCII, lowercasing,
    dropping punctuation, collapsing separators, truncating at a word boundary so the
    whole name stays within 60 characters, and appending `-2`, `-3`... when the name
    is already in `taken`.
    """
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-") or "work"
    prefix = f"issue-{number}-"
    room = _MAX - len(prefix)
    if len(slug) > room:
        cut = slug[:room]
        slug = cut.rsplit("-", 1)[0] if "-" in cut else cut
    name = prefix + slug
    candidate, n = name, 2
    while candidate in taken:
        suffix = f"-{n}"
        candidate = name[: _MAX - len(suffix)] + suffix
        n += 1
    return candidate
