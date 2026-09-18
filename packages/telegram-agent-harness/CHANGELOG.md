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

### Fixed
- Preserve empty preference list for discovered Telegram bot slots lacking explicit configuration, restoring any-project eligibility default and removing hardcoded comment examples.
- Operator tools resolve their Telegram channel on every call and refuse a session they do not own, so a tool retained across a hot reload can no longer post into another session's chat.
- Telegram question, dashboard and lane-provenance services stop when the lease is relinquished or the session shuts down, instead of leaving timers writing to a released channel.
