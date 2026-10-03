# Workaround comment lint

A band-aid comment is how one lane's shortcut becomes the next lane's
precedent: the next agent reads `# HACK: retry until the sweep warms` as
precedent and copies it. [Issue #245](https://github.com/Wladefant/super-board/issues/245),
a follow-up to the [@poteto research in #227](https://github.com/Wladefant/super-board/issues/227),
turns that reflex — *when an agent makes a mistake, write a lint rule against
it* — into a gate.

The gate is `workflows/portable/workaround_comments.py`, its tests are
`workflows/portable/test_workaround_comments.py`, and
[`.github/workflows/workaround-comment-lint.yml`](../../.github/workflows/workaround-comment-lint.yml)
runs it on every pull request.

## The rule

A line a pull request **adds** fails when its comment contains `HACK`,
`WORKAROUND`, `FIXME`, or `TODO` (case-insensitive, whole word) and the comment
names no issue. Any one of these on that same line clears it:

| Form | Example |
|---|---|
| Issue URL | `https://github.com/Wladefant/super-board/issues/245` |
| `owner/repo#N` | `Wladefant/super-board#245` |
| Bare `#N` | `#245` |

The remedy is one of: fix the root cause, open the issue that will remove the
comment and link it on the marker line, or delete the comment and make the code
say it.

## What it does not do

These are deliberate, and they are the reason the gate can be trusted rather
than worked around:

- **Comment text only.** Code, string literals, and prose are never inspected.
  `todo_queue.append(item)` and `description = "# TODO: fix later"` pass.
- **Known file types only.** Only extensions with a comment syntax in
  `COMMENT_SYNTAX` are scanned, so a Markdown bullet (`* HACK: keep the old
  heading`) or a JSON note is not a false positive. Teaching the gate a language
  means adding its extension to the right family in `COMMENT_SYNTAX`.
- **Added lines only.** Removing an old workaround comment never fails the gate;
  a deletion is the fix, not the offence.
- **Same line only.** A multi-line comment block that links its issue two lines
  below the marker is still a finding. Put the link on the marker line.
- **Line-oriented.** A comment introducer inside a multi-line string reads as a
  comment, so the gate can report a false positive there. It errs toward
  reporting because the fix costs one line.

## Run it locally

From the repository root, against the branch a pull request targets:

```bash
python -B workflows/portable/test_workaround_comments.py
python -B workflows/portable/workaround_comments.py --base origin/main
```

`--base` defaults to `origin/main`; `--repo-root` defaults to the current
directory. Exit codes are **0** clean, **1** findings, **2** the diff could not
be produced (bad ref, missing history). Under GitHub Actions each finding is
also emitted as an `::error file=...,line=...::` annotation, which is what puts
it on the pull request's diff view.

To see it fail on purpose, add one unlinked marker to any scanned file and run
the command again.
