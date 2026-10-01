---
name: ing-never-stop-wave
description: "Use when orchestrating ING/TestING lanes: continuous replenishment, quota-fallback to Flash with Opus retry after reset, and edge-case hunting for testers"
---

# Never-stop wave (ING/TestING, user-locked 2026-09-11)

1. Keep the wave running: on every lane completion, replenish in the same turn (crash recovery: `read history://<name>` and resume from the transcript, never redo).
2. Quota: if Opus 5 (`reviewer`) returns 429/quota, dispatch the slice on Flash (`task`/`qa-verifier`) now and re-run/verify on Opus ~4 h later when the window resets. Never idle waiting.
3. Every lane also hunts small tester edge cases in its area (offline SharePoint, expired ADO session, empty sheets, umlauts, duplicate steps, Windows paths) and files them as issues.
4. RAM: measure before spawning; do not kill other veyyon sessions without the operator's word.
5. PC28GR reachability: `sh tools/remote/rcmd.sh run "hostname"`; announce to all lanes via irc when it comes back.
