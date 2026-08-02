# Agent Native — parked 2026-08-02

The self-hosted Agent Native stack was **deliberately stopped, not deleted**, on 2026-08-02.
The operator paused this direction and may return to it. Everything needed to restart it is
below.

## What was done

- All four Dokploy applications **stopped**: `agent-native-design`, `agent-native-analytics`,
  `agent-native-clips`, `agent-native-minio`.
- All three Postgres instances **stopped**: design, analytics, clips.
- `autoDeploy` set to **false** on all four apps, so a push to the platform repo can no longer
  wake them.
- The scheduled upstream-sync workflow **disabled** (`disabled_manually`).

Nothing was deleted. Projects, apps, databases, volumes, domains, TLS certificates and
environment variables are all intact.

Verified after stopping: `design`, `analytics`, `clips` and `minio-an` on `wladefant.de` all
return **502**. Unrelated services were checked and are unaffected — `hq.wladefant.de` 401
(its normal basic auth), `hosting.wladefant.de` 200, `elumiai.com` 200.

## How to bring it back

Dokploy project **Agent Native** — `mtRLA9hkop95jktJchjHD`.

| Service | ID |
|---|---|
| `agent-native-design` | `XKYD5oYD0w8iShOOajzlc` |
| `agent-native-analytics` | `drH4XsXW_y0elNJ4N2Sw6` |
| `agent-native-clips` | `XNh-c8F6VmcsHx17m9pUd` |
| `agent-native-minio` | `fL1pqD9ly8VN1JPMOXlPR` |
| Postgres — design | `lpCjn0C81CZsaWxgvc-pn` |
| Postgres — analytics | `fhT5DuTnqwuasLs9GYrRb` |
| Postgres — clips | `OJxTYgU3qUI67WKPoznEX` |

1. Start the three Postgres instances **first**, then MinIO, then the three apps.
2. Re-enable `autoDeploy` only if wanted — see the open decision in
   [issue #44](https://github.com/Wladefant/super-board/issues/44) first, because every push
   rebuilds all three apps.
3. Re-enable the upstream-sync workflow if wanted. **Note it had been failing daily since
   2026-07-31** (three consecutive failures; its last success was 2026-07-28), so it needs
   diagnosing before it is trusted.
4. Point [`design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md)
   back at the self-hosted URL — its endpoint block is currently marked parked.

## Read this before restarting

The stack worked, but four things were open and are still open:

- [#42](https://github.com/Wladefant/super-board/issues/42) — no AI provider configured, so
  Design cannot generate variants and Analytics' Ask/Dashboards stay blocked. This gates the
  useful half of both apps.
- [#46](https://github.com/Wladefant/super-board/issues/46) — the runtime image ships no
  `node_modules`, so `ffmpeg-static` is missing and Clips' frame extraction, remux and
  transcription are broken.
- [#47](https://github.com/Wladefant/super-board/issues/47) — self-hosted apps send analytics
  events and full session replays to Builder's servers by default. **Fix this before putting
  real product traffic through it.**
- [#44](https://github.com/Wladefant/super-board/issues/44) — every push rebuilds all three
  live apps.

Also: password reset does not work (no mail transport), and enabling it switches email
verification on for new signups — one check gates both.

## Where the knowledge lives

- [START-HERE.md](https://github.com/Wladefant/super-board/blob/main/docs/START-HERE.md) —
  what was live and how it was verified
- [Operating guide](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-OPERATING-GUIDE.md)
  — capability→surface routing
- [Architecture decision](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-SUPERBOARD-PRODUCTION.md)
- [Dokploy new-app runbook](https://github.com/Wladefant/super-board/blob/main/docs/runbooks/DOKPLOY-NEW-APP.md)
- Issues [#27–#47](https://github.com/users/Wladefant/projects/5) carry the full evidence trail

## Two traps that cost hours, worth re-reading first

- A Dokploy **"Rebuild deployment" does not re-pull** — it rebuilds the already-cloned working
  copy. After pushing a fix, use **Deploy**, not Rebuild, or you will rebuild old code and
  conclude your fix failed.
- **API 200 + SSR 500** means a template is missing a dependency that `@agent-native/core`
  pulls into its bundle. That was the `yjs` failure in Clips.
