# Docs system

Convention used across Wlad’s projects so documentation stays findable and does not rot into session-only notes.

## Index is mandatory

Every repo keeps **`docs/README.md`** as the only authoritative index:

- One line per doc
- One job per doc
- If a doc has no one-line job description in the index, it does not belong in `docs/`

## Session docs are an inbox

Anything a Claude Code / agent session writes mid-task lands in:

```
docs/sessions/YYYY-MM-DD-<topic>.md
```

These are **raw notes, not canon**. Treat them as an inbox: useful for continuity, never as the source of truth.

## docs-gardener

A recurring board ticket — **docs-gardener** — periodically:

1. Folds session docs into the canonical `docs/` tree
2. Updates `docs/README.md` so the index stays complete

That ticket is a normal card on the project board (same principle as [MY-SYSTEM.md](./MY-SYSTEM.md): roadmap and maintenance work live on the board, not in a separate tracker).

## Linking rules

- **Allowed:** links to canonical `docs/` files that appear in `docs/README.md`
- **Forbidden:** treating `docs/sessions/...` as an authoritative source (no README, ADR, skill, or PR description may depend on a session path)

When in doubt: promote the session note via docs-gardener first, then link the promoted path.
