# Changelog

## [Unreleased]

### Added
- Config-driven Telegram bot slot eligibility matching cwd globs (defaulting to any project) and prioritizing specific project affinity over shared pool slots.
- Detailed slot lease busy diagnostics exposing session ID, project path, and owner PID (`ClaimResult.busyHolders` and detailed formatted reason).
- HTTP 409 conflict bounded exponential backoff, diagnostic warning logs, and host session error notifications on Telegram poller exhaustion.
- Standalone Telegram bot daemon (`daemon/`): polls opted-in bot tokens machine-wide and drives Veyyon sessions through the GUI host action protocol, so a bot keeps answering with no session open in the project. Slots opt in with `"daemon": true` in `manifest.json` (or `VEYYON_TELEGRAM_DAEMON_SLOTS`); the daemon holds an ordinary pool lease per slot, so in-session pollers see the slot as busy and the single-poller-per-token invariant is unchanged. Durable per-chat session routing and per-entry delivery claims in `~/.veyyon/telegram/daemon.db` keep a restart from replaying a transcript into the operator's chat. Chat commands: `/sessions`, `/attach <id>`, `/new [path]`, `/detach`, `/where`. CLI: `bun daemon/main.ts run|status|stop|check`.

### Fixed
- Preserve empty preference list for discovered Telegram bot slots lacking explicit configuration, restoring any-project eligibility default and removing hardcoded comment examples.
