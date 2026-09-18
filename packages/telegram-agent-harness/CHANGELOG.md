# Changelog

## [Unreleased]

### Added
- Config-driven Telegram bot slot eligibility matching cwd globs (defaulting to any project) and prioritizing specific project affinity over shared pool slots.
- Detailed slot lease busy diagnostics exposing session ID, project path, and owner PID (`ClaimResult.busyHolders` and detailed formatted reason).
- HTTP 409 conflict bounded exponential backoff, diagnostic warning logs, and host session error notifications on Telegram poller exhaustion.
- `telegram_question` tool asking the operator a labeled-choice question on the session's Telegram route, returning only that question's answer and never an approval.
- `telegram_message` tool sending lane-attributed updates with durable reply context, so an operator reply routes back to the originating lane.
- `telegram_dashboard` tool refreshing one pinned fleet dashboard from observed lane, blocker and merge-queue state on a 30-second coalesced edit.
- Telegram free-text replies and callback selections answer the question they reply to instead of starting a new operator turn.
- Configured message thread id is read from the leased channel so a forum-topic channel receives messages in its own topic.
- Standalone Telegram bot daemon (`daemon/`): polls opted-in bot tokens machine-wide and drives Veyyon sessions through the GUI host action protocol, so a bot keeps answering with no session open in the project. Slots opt in with `"daemon": true` in `manifest.json` (or `VEYYON_TELEGRAM_DAEMON_SLOTS`); the daemon holds an ordinary pool lease per slot, so in-session pollers see the slot as busy and the single-poller-per-token invariant is unchanged. Durable per-chat session routing and per-entry delivery claims in `~/.veyyon/telegram/daemon.db` keep a restart from replaying a transcript into the operator's chat. Chat commands: `/sessions`, `/attach <id>`, `/new [path]`, `/detach`, `/where`. CLI: `bun daemon/main.ts run|status|stop|check`.

### Fixed
- Preserve empty preference list for discovered Telegram bot slots lacking explicit configuration, restoring any-project eligibility default and removing hardcoded comment examples.
- Operator tools resolve their Telegram channel on every call and refuse a session they do not own, so a tool retained across a hot reload can no longer post into another session's chat.
- Telegram question, dashboard and lane-provenance services stop when the lease is relinquished or the session shuts down, instead of leaving timers writing to a released channel.
