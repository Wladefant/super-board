# Changelog

## [Unreleased]

### Added
- Config-driven Telegram bot slot eligibility matching cwd globs (defaulting to any project) and prioritizing specific project affinity over shared pool slots.
- Detailed slot lease busy diagnostics exposing session ID, project path, and owner PID (`ClaimResult.busyHolders` and detailed formatted reason).
- HTTP 409 conflict bounded exponential backoff, diagnostic warning logs, and host session error notifications on Telegram poller exhaustion.

### Fixed
- Preserve empty preference list for discovered Telegram bot slots lacking explicit configuration, restoring any-project eligibility default and removing hardcoded comment examples.
