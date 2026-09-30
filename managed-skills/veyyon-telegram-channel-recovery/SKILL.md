---
name: veyyon-telegram-channel-recovery
description: Recover Telegram delivery without confusing working account-forwarded chat replies with unavailable explicit-send or question bot leases
---

# Telegram channel and question-receiver recovery

## Distinguish all three surfaces first

Treat these as separate observed capabilities:
1. Normal assistant commentary/final replies forwarded through the Telegram account route.
2. Explicit `telegram_message` delivery.
3. `telegram_question` registration and its reply/callback receiver.

A failure of one does not prove the others are down. On 2026-09-21, explicit messages worked while the question receiver reported an unbound `telegram-superboard` bot lease. During the same overnight session, explicit messages also failed even with `rebind: true`, but the operator confirmed normal assistant descriptions still arrived in Telegram, and an inbound message carried `origin: telegram_account`.

- Use actual tool results and operator confirmation, not a remembered routing assumption.
- An inbound Telegram message proves inbound delivery, not outbound delivery by itself.
- If normal replies are reported to work, send a short normal reply as a delivery probe and continue using that route. Do not call the entire channel offline or insist on a terminal reload solely because the explicit send tool fails.
- Never claim an explicit message or registered question succeeded when its tool returned an error.
- Try `telegram_message` with `rebind: true` once when requested by the tool. Repeated identical failures are not useful recovery.
- Retry the question once after a successful explicit-message rebind. If it still fails, record the narrower condition: messages work, specialized question receiver does not.
- For operator-requested plain Ja/Nein decisions, use the working reply surface. Include problem, concrete action, risk, recommendation and clear Ja/Nein choices. Preserve the pending decision on its canonical issue. Wait for an explicit answer tied to that wording; delivery, silence and tool errors are never authorization.
- Do not claim question callbacks or reminders are active without observed support.
- Continue independent product work; do not restart active sessions or build another bridge to repair a single surface.

## Diagnose a genuinely silent route

Only after distinguishing normal forwarding from explicit-send failure:
- Inspect lease metadata only in `~/.veyyon/telegram/bot_pool.db`, table `bot_leases`: slot, lease status, owner PID, heartbeat and TTL. Never dump credentials.
- SQLite `SELECT name FROM pragma_table_info('bot_leases')` safely discovers metadata columns without dumping rows containing unknown fields.
- Verify the recorded owner process. It may be `bun`, not only `veyyon.exe`; an ACTIVE lease owned by a live process must not be stolen. A RELEASED lease with a live owner was a historical failure mode.
- `powershell -File ~/.veyyon/telegram/veyyon-telegram.ps1 json` may expose lease/PID state; do not print secret fields.
- Legacy inbound state may be in `~/.claude/channels/<slot>/veyyon_bridge_state.db`, `update_ledger`; inspect timestamps/routing metadata only. A latest timestamp proves only the last observed inbound item.
- The historical daemon `~/.veyyon/telegram/daemon/main.ts` polled opted-in slots only. Verify configuration rather than assuming it provides fallback.

## Terminal reload fallback

Only if the needed delivery surface is actually unavailable and supported rebinding cannot restore it, ask the operator to type `/tg-reload` or `/telegram reload` in the owning Veyyon terminal. Name the precise failed surface. Do not request repeated reloads while normal replies are reaching the operator. Afterwards prove recovery on the affected surface; message delivery alone does not prove question callbacks work.

## Durable source repair

Historical source: `super-board/packages/telegram-agent-harness/extension/`, installed under `~/.veyyon/telegram/` with `install-manifest.json`. Verify current ownership before editing. Make authorized changes in source and install reviewed bytes; never silently patch only the runtime.

Historical PR153 introduced `ensureChannelBound` auto-rebinding on user turn/turn_end with a 60-second limiter and an explicit-release exception. This is version-specific background, not proof of current behavior.

## Never

- Force-release an active owner's lease or clear lock/PID files to manufacture recovery.
- Start a legacy bridge or another daemon that could route replies to a different session.
- Resume a session twice or restart active workers for a failed send tool.
- Treat a failed question ID, unacknowledged message, or elapsed time as approval.
- Run `git stash` in a shared tooling checkout.
