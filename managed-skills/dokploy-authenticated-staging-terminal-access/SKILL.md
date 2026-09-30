---
name: dokploy-authenticated-staging-terminal-access
description: "Recover authorized PolySimulator staging host access through Dokploy's web terminal in existing signed-in Chrome when direct SSH or MCP capabilities fail; verify target, use xterm safely, preserve secrets and connection ownership."
---

# Verified staging host access through authenticated Dokploy

## When to use
Use for an authorized staging host investigation or operation when ordinary MCP/API capabilities are insufficient and the operator already has a signed-in Dokploy browser session. Investigate existing capabilities before declaring an access blocker or asking for another login. Prefer safe MCP/API reads when they suffice; do not use the browser for ordinary GitHub issue/PR text.

## What actually worked
Verified 2026-09-21: host commands ran through **Dokploy's authenticated web terminal**, using the control plane's server-side SSH connection. This did **not** establish a working local SSH alias or recover a local key/agent. No SSH private key, password, cookie, or session token was extracted.

Evidence:
- [Access and recovery evidence](https://github.com/Bavariance/polysimulator/issues/5293#issuecomment-5763731652)
- [Actual runtime/container attestation](https://github.com/Bavariance/polysimulator/pull/5312#issuecomment-5764040249)
- [Selective effective runtime flag inspection](https://github.com/Bavariance/polysimulator/issues/5293#issuecomment-5764287001)

## Prerequisites and boundaries
1. Read `skill://existing-chrome-signed-in-browser-control` for attachment, Chrome consent, discovery compatibility, and tab ownership.
2. Confirm current authorization. A logged-in browser supplies capability, not permission. Read-only inspection does not authorize deploys, environment writes, restarts, DDL, or rollback.
3. Re-resolve the target using read-only Dokploy metadata. For this verified staging environment: control plane `https://hosting.wladefant.de`, compose `TU7b_dY9l9_nCas6YBNwj`, application `polysimulator-staging-iad-v09j4g`, server name `akamai-iad-staging`, database project `hgzyqmaanndcimnclxtv`. Rediscover the server ID; do not substitute another similarly named server. Stop on any mismatch.
4. Production and retired Hetzner targets remain excluded. Do not enumerate unrelated hosts or open their terminals.

## Attach once, then preserve the connection
Use the operator's existing authenticated Chrome rather than creating an Edge window or a new application login.

The successful Chrome 153 session had a real browser websocket recorded in the default profile's `DevToolsActivePort`, but `/json/version` returned HTTP 404. A loopback-only HTTP discovery adapter returned that real websocket from `/json/version`, allowing native `browser.open` to attach. Read the browser skill for the procedure. Port 60622 and native tab name `operator-dokploy-recover` were session observations, not permanent configuration; discover current endpoints rather than copying stale browser IDs or ports.

After a successful approved attachment, use `browser.run` on the existing named tab. New browser connections can trigger another Allow prompt. Do not restart Chrome, copy its profile/cookies, extract credentials, enumerate every browser page, or reconnect merely because a terminal dialog became idle. Assign one worker exclusive ownership of the operational tab.

## Open and verify the exact terminal
1. Navigate to the authenticated Dokploy server settings surface (`/dashboard/settings/servers` in the verified session).
2. Open the terminal action for **akamai-iad-staging**, not the control-plane machine or another server.
3. Wait for an interactive prompt. Confirm the selected terminal/server identity against the metadata.
4. Corroborate with the intended Docker compose/application labels and the staging database hostname from safe runtime boot logs or a narrowly selected, redacted inspection.

**`hostname` returned `localhost` in the working terminal. It is not sufficient identity proof.** The selected server plus matching container labels and staging database binding establish the target. Stop if these disagree.

## xterm interaction and bounded output
Observed working native browser interaction:

```javascript
await tab.type('textarea.xterm-helper-textarea', command);
await tab.press('Enter', { selector: 'textarea.xterm-helper-textarea' });
```

Observe the prompt/output before continuing. When xterm's visible rows had not refreshed, a separate screenshot call flushed rendering, after which the visible rows were readable:

```javascript
await tab.screenshot();
const visibleOutput = await tab.evaluate(() =>
  [...document.querySelectorAll('.xterm-rows > div')]
    .map(row => row.textContent)
    .join('\n')
);
```

This reads only the **visible terminal rows**, not complete scrollback. Bound command output in the command itself and use explicit start/end sentinels and exit status for important operations. A clipped screen is not proof that a long operation completed. Avoid screenshots whenever secret-bearing output could be present.

After an idle period, typing sometimes failed to execute. Do not blindly resend a mutation. First establish whether the command ran. If it did not, close **only the terminal dialog**, reopen the same verified server terminal, wait for its prompt, and resend. Preserve the Chrome connection and user tabs. If execution is uncertain, inspect the resulting state rather than duplicate the command.

## Secret-safe and mutation-safe operation
- Inspect only needed non-secret fields: container/image IDs, creation/start times, baked revision, selected non-secret flags, bounded logs, health and business-route responses.
- Never dump whole environment maps or connection strings. Equality checks and named-key preservation checks may run in memory/on-host and return only sanitized results.
- Base64 is transport encoding, **not redaction**. Never encode and paste secrets into a command or transcript.
- MCP environment/compose blobs may be wholly redacted while setters replace the whole blob. **Never write a redacted blob back.** A UI operation must fetch current complete state internally, preserve unknown keys, change only explicitly authorized keys, and avoid exposing values to observations or logs.
- The verified session performed explicitly authorized two-key compatibility changes, same-image backend/daemon recreation, and rollback with root-only backup artifacts. That historical authorization does not transfer to a future task.
- Before an authorized mutation, capture current state and rollback evidence, name the exact services and action, and verify resulting runtime state. A queued toast, deployment row, or health response alone is not revision proof.
- Browser terminal capability does not authorize database stamping or execution of a generated migration script. Apply the repository's separate migration and authorization gates.

## Failed approaches and reusable lessons
- Local SSH attempts did not establish a usable connection; do not document them as successful access or guess a key path.
- An initially created Edge window did not provide durable authenticated access and disappeared with its owner. Reuse the operator's existing appropriate session instead.
- Chrome's HTTP discovery 404 did not mean its websocket endpoint was absent; the verified discovery-only loopback adapter solved that compatibility issue without credentials.
- Some deployment-log MCP reads returned 404 while an already identified on-host log was readable through the authorized terminal. Use known log paths and bounded reads, not broad host probing.
- Capability failure is not missing authorization. Do not repeatedly request permission already granted; identify the exact missing capability and investigate supported alternatives.

## Cleanup and future reuse
Preserve the user's browser and unrelated tabs. Close only owned temporary terminal dialogs/tabs when finished. Stop temporary adapters only when no remaining owner needs the connection; never terminate Chrome as cleanup. Record successful and failed access paths, exact target evidence, authorization scope, and limitations on the relevant GitHub issue. Update this procedure when observed platform behavior changes; never turn an old successful session into assumed current access.
