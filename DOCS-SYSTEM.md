# Docs system

Convention used across Wlad’s projects so documentation stays findable and does not rot into session-only notes.

## Index is mandatory

Every repo keeps **`docs/README.md`** as the only authoritative index:

- One line per doc
- One job per doc
- If a doc has no one-line job description in the index, it does not belong in `docs/`

### Runtime documents are indexed too

A document that describes runtime behaviour is reachable from `docs/README.md`
even when it does not live under `docs/` — the release notes, the version
reconciliation, and every `skills/super-board/references/*` file. The index is
the only place somebody has to look, so a contract that is only discoverable by
knowing the path already failed.

`tests/test_version_identity.py` asserts that every runtime reference document
is linked from the index. A new reference file that nobody links fails the
suite.

### Precedence: the executable contract, then its reference, then the doctrine

Three layers describe the same system, and they rot at different speeds:

1. **The code** — `scripts/super_board_runtime/`. It is what actually happens.
2. **The runtime references** — `skills/super-board/references/*` and
   `config-schema.json`. These document the code, and where the two disagree the
   code wins and the reference is the bug.
3. **The doctrine** — `MY-SYSTEM.md` and `skills/superboard-setup/SKILL.md`.
   These tell an operator how to stand a project up.

Doctrine drifts fastest, because nothing executes it and no test reads it as
input. A setup document that teaches a workflow target the runtime plans against
is not a stale sentence — it provisions every future project wrong, and the
damage shows up months later on a board nobody connects back to the document.

So: **when doctrine disagrees with a runtime reference, the reference wins and
the doctrine is the defect.** Fix the doctrine in place, in the same change that
lands the contract, and state the corrected value plainly rather than softening
it — the next reader is provisioning a board, not adjudicating a difference of
opinion.

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
- **Every reference is a full clickable URL.** A repository document cited in any
  document, card, issue, comment or report is a complete
  `https://github.com/<owner>/<repo>/blob/<ref>/<path>` URL — never a bare
  filename, never a relative path, never a bare issue number or commit SHA. Use a
  local absolute path only when the reader must open or run the file on their own
  machine. A link that does not resolve has not been written yet: commit and push
  the document first, then cite it.

When in doubt: promote the session note via docs-gardener first, then link the promoted path.
