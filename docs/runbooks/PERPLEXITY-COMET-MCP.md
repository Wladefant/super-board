# Restoring the Perplexity Comet MCP

The user-scoped `perplexity-comet` MCP registration was **deliberately removed on
2026-07-27, not lost**. It loaded Comet tools into every Claude Code project, which
is the reason it is off; the local installation is untouched and this runbook is
everything needed to bring it back when a job actually wants it.

Tracking issue:
[super-board#20](https://github.com/Wladefant/super-board/issues/20), migrated from
[ing-qa-automation#87](https://github.com/Wladefant/ing-qa-automation/issues/87).
Removal evidence:
[ing-qa-automation#85 comment](https://github.com/Wladefant/ing-qa-automation/issues/85#issuecomment-5089610568).

Paths below are written against `<user-home>` (the Windows user profile directory)
and `<dev-root>` (the local development checkout directory). Substitute the real
values on the workstation; never paste them back into an issue, a card comment or a
transcript.

---

## The one that bites: never register both scopes

`-s user` puts the Comet tools into **every** Claude Code project on the machine.
That is what caused the removal in the first place, and it is the whole reason this
runbook exists rather than a live registration.

- Needed in one project only → `claude mcp add -s project ...`.
- Needed everywhere → `-s user`, knowing every project pays the context cost.
- **Never both at once.** Two registrations produce duplicate tools with no error,
  and the duplicate set is only visible once a session is already confused.

## Existing local installation

| Item | Value |
|---|---|
| Source | `<dev-root>\Perplexity-Comet-MCP` |
| Wrapper | `<user-home>\.claude\bin\perplexity-comet-mcp.cmd` |
| Entry point | `<dev-root>\Perplexity-Comet-MCP\dist\index.js` |
| Comet binary | `<user-home>\AppData\Local\Perplexity\Comet\Application\comet.exe` |
| CDP port | `9223` |
| Automation data dir / profile | `<user-home>\AppData\Local\Perplexity\Comet\ClaudeAutomation` / `ClaudeAutomation` |
| Version when removed | `2.6.2` |

The automation profile is dedicated: it exists so the MCP never drives the operator's
own signed-in Comet window.

## Restore

```powershell
Set-Location "<dev-root>\Perplexity-Comet-MCP"
npm install
npm run build
Test-Path ".\dist\index.js"
claude mcp add -s user perplexity-comet -- "<user-home>\.claude\bin\perplexity-comet-mcp.cmd"
claude mcp get perplexity-comet
```

Then, in order:

1. **Fully restart Claude Code** — a running session does not pick the server up.
2. Run `comet_connect`.
3. Run one small disposable `comet_ask`.
4. Confirm the dedicated automation profile and port `9223` are the ones in use, not
   the operator's everyday Comet profile.

For project-only testing, use `-s project` instead of `-s user`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Node missing | Repair Node.js 18+, or update `NODE_EXE` in the wrapper. |
| `dist/index.js` missing | `npm install` then `npm run build` in the source directory. |
| Connection failure | Verify the Comet path and port `9223`; kill stale processes still holding the automation profile. |
| Tools absent | Claude Code was not fully restarted. |
| Duplicate tools | Both a user and a project registration exist — remove one. |

## Disable again

```powershell
claude mcp remove perplexity-comet -s user
claude mcp get perplexity-comet
```

Restart Claude Code. `claude mcp get` returning nothing is the confirmation; a
restart without it is not.

## Acceptance criteria for a restoration

These gate a restore when one is actually performed — they do not gate keeping this
runbook.

- [ ] Source builds successfully.
- [ ] Exactly one authoritative registration exists.
- [ ] A fresh Claude Code session connects to `comet-bridge`.
- [ ] A disposable query succeeds through the dedicated profile.
- [ ] The user-wide vs project-only scope decision is recorded on
      [super-board#20](https://github.com/Wladefant/super-board/issues/20).
