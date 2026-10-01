---
name: existing-chrome-signed-in-browser-control
description: "Use gh/API for GitHub text, headless for routine browser work, and persistent signed-in Chrome only for authenticated UI operations or visual QA."
---

# Browser routing and existing signed-in Chrome

## Operator decision (2026-09-21, corrected)
Do NOT use the operator's browser for everything. Routine/easy tasks use the ordinary isolated headless browser. Static content uses read/API tools. Use the existing signed-in Chrome for difficult authenticated operations, dashboards that cannot safely be operated through available APIs, and QA when reusing the existing login reduces repeated setup, tool calls and token use. Complexity alone does not require personal-browser access; choose the simplest adequate surface. Do not start a new login flow when an appropriate authenticated session is already available.

This is a standing orchestration choice, not a mandate to build browser-routing tooling or change installed harness configuration. Apply it in lane assignments. Browser access does not replace reasoning, independent review or authorization.

## Safety and ownership
- Do not restart/kill the user's Chrome, copy profiles/cookies, extract session credentials or recover secrets.
- Restrict work to authorized targets. Existing login never grants production access or extra mutation permission.
- Use a dedicated tab where possible; one worker owns each operational tab. Preserve user tabs and never close-all/kill the user's browser.
- Share named tab/connection metadata with workers, not cookies or tokens.
- For trading QA, confirm an authorized staging test account; do not transact on a real or unrelated account just because it is signed in. Use separate isolated QA login when appropriate.
- Keep environment blobs, password fields, tokens and secret-bearing terminal output out of observations/screenshots.

## Attach existing Chrome on Windows
1. Inspect existing Chrome process metadata (`Get-CimInstance Win32_Process -Filter "Name='chrome.exe'"`) and main-process flags. Do not guess endpoint or restart Chrome.
2. Read the known profile's DevToolsActivePort. Default workstation path: `C:/Users/wkiri/AppData/Local/Google/Chrome/User Data/DevToolsActivePort`. First line is actual port, second is browser websocket path. Rediscover after lifecycle changes; never reuse recorded browser IDs blindly.
3. If disabled, ask the operator to enable `chrome://inspect/#remote-debugging` in that same Chrome (Chrome144+ supports live activation), then click Allow on the connection prompt. No new application login needed.
4. Deliver hands-on instructions via the verified session Telegram route when available. If unavailable, state the limitation and use the active conversation; never rebind another session's route.
5. Use native `browser.open` with app.cdp_url HTTP endpoint and app.target matching the authorized application hostname.

## Chrome153 websocket-only discovery compatibility
Observed: live debugging wrote port9222 and a browser websocket path, but HTTP `/json/version` returned404. Native Veyyon browser rejected direct ws URLs because it expects an HTTP discovery endpoint.

Provide a temporary loopback-only HTTP discovery adapter: `/json/version` returns `{ "Browser": "Chrome/153", "webSocketDebuggerUrl": "ws://127.0.0.1:<actual-port><actual-browser-path>" }`; other paths return404. Point native browser.open at that HTTP endpoint. Browser then connects directly to Chrome's real websocket. No credentials or profile data are read/copied.

Use a supervised launch process for reusable adapter lifetime, verify a free port and readiness, and stop the adapter after use without terminating Chrome. An initial prototype Bun server in persistent eval succeeded; do not leave such a prototype running permanently. A worker-owned Edge CDP endpoint disappeared across worker lifecycle; process/window existence alone is not readiness.

## Verify and operate
- Open must succeed, then `tab.observe()` must confirm expected authenticated URL and normal dashboard controls, not just title/cached response.
- Transfer exact native tab name plus active discovery endpoint to one worker. No concurrent navigation of the same tab.
- Prefer structured observations for actions; screenshot when appearance is the claim. Re-observe after navigation.
- Use real browser UI for frontend QA; preserve desktop/mobile proof and valid staging test authentication. Reusing operator Chrome is optional when an isolated QA browser is simpler.
- Confirm real runtime/version/behavior effects after mutation; a queued toast is not deployment proof.

## Dokploy lessons
- MCP env/compose blobs can be wholly redacted while setters replace the entire blob. Never write a redacted blob back. Find safe named-key mutation capability instead.
- The operator's authenticated Dokploy staging terminal was successfully exercised read-only after live Chrome attachment: backend/daemon target database host, effective migration flags and immutable image IDs were inspectable. Check current target and protect secrets every time; this past success does not authorize future changes.
- Installed polysim-deploy help contained stale autoDeployOFF wording; implementation unconditionally builds. It does not satisfy immutable-image-only constraints without a supported alternative or changed authorization.
- Authorization and capability are different. Do not repeatedly request already-granted permission; investigate available safe capability and report the exact missing capability if blocked.

## Reference
https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session

## Avoid repeated Allow prompts
Chrome live remote debugging explicitly requests consent per new debugging session, not per browser action (official Chrome documentation above). No permanent allow-all setting was established for this mode. Keep approved native browser connections alive and use browser.run on the existing named tab. Do not let multiple workers independently open/reconnect to the same operator browser. Prefer one operational owner; coordinate separate authorized tabs without repeated browser.open calls. Batch related safe observations. After genuine disconnect, ask for renewed consent once, not an attachment retry loop. Do not bypass Chrome consent/security checks or claim prompt-free persistence after a connection loss. Operator explicitly requested fewer prompts on2026-09-21.

## GitHub reading: API first, never browser by default
Operator clarification2026-09-21: use gh CLI, GitHub MCP/API or issue/pr readers for issue bodies, comments, reviews, diffs, checks and metadata. Select only needed fields; batch related retrievals and paginate instead of dumping entire threads. This is ordinarily more token-efficient than browser DOM/accessibility snapshots containing navigation and surrounding UI. Do not open Chrome just to read issue prose, and do not treat private404 from an unauthenticated web reader as a blocker when authenticated gh/API works. Use the browser only for an actually visual/interactive claim such as verifying embedded screenshots render for the signed-in user, or a UI-only operation not supported safely by API. For image-render proof inspect only image alt/load/dimensions and the relevant screenshot; avoid dumping whole comment text or resolved signed asset URLs (which may contain temporary tokens). Reuse the approved connection rather than reconnecting.
