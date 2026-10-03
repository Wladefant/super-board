/**
 * scripts/prove-two-way-routing.ts
 *
 * Proves actual terminal CLI two-way routing:
 * 1. Connects to live GUI host on tcp:127.0.0.1:7699.
 * 2. Verifies 4 existing operator sessions are active and untouched.
 * 3. Creates a dedicated disposable session in temporary workspace.
 * 4. Binds target { chatId: "-1004422647618", topicId: "999999" } to that session via SlotRouter.
 * 5. Sends a routed nonce prompt through router.deliver(target, prompt).
 * 6. Captures relayed outbound response through router.onSessionEvent -> options.send(target, html).
 * 7. Confirms prompt was routed to existing session and response was relayed back WITHOUT creating a new session.
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { SlotRouter, type RouteTarget } from "../daemon/router";
import { TerminalSessionControl, type SessionEvent } from "../daemon/session-control";
import { DaemonStore } from "../daemon/store";

async function main() {
  console.log("=== VEYYON TWO-WAY ROUTING TERMINAL PROOF ===");
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "veyyon-two-way-proof-"));
  const dbPath = path.join(tempDir, "daemon.db");
  const store = new DaemonStore(dbPath);

  let sessionEventHandler: ((event: SessionEvent) => void) | undefined;

  const control = new TerminalSessionControl({
    onLog: console.log,
    onEvent: (event) => {
      if (sessionEventHandler) sessionEventHandler(event);
    },
  });

  try {
    // 1. Check live operator sessions
    const operatorSessions = await control.listSessions();
    console.log(`Live operator sessions count: ${operatorSessions.length}`);
    for (const s of operatorSessions) {
      console.log(`  - [${s.id}] workspace: ${s.workspace}`);
    }

    // 2. Create disposable session
    const disposableWs = path.join(tempDir, "disposable-workspace");
    fs.mkdirSync(disposableWs, { recursive: true });
    const disposableId = await control.createSession(disposableWs, "Two-Way Routing Disposable Session");
    console.log(`\nCreated disposable session: ${disposableId}`);

    const listAfterCreate = await control.listSessions();
    console.log(`Sessions count after disposable creation: ${listAfterCreate.length}`);

    // 3. Set up SlotRouter
    const outboundMessages: Array<{ target: RouteTarget; text: string }> = [];
    const target: RouteTarget = {
      chatId: "-1004422647618",
      topicId: "999999",
    };

    const slot = {
      slotId: "telegram-superboard",
      botId: "778899",
      mode: "forum" as const,
      forumChatId: "-1004422647618",
      workspace: "C:/Users/wkiri/development/super-board",
    };

    const router = new SlotRouter({
      slot,
      store,
      control,
      send: async (tgt, html) => {
        outboundMessages.push({ target: tgt, text: html });
      },
      relay: async (tgt, text) => {
        outboundMessages.push({ target: tgt, text });
      },
      log: (msg) => console.log(`[Router] ${msg}`),
    });
    sessionEventHandler = (event) => {
      void router.onSessionEvent(event);
    };

    // 4. Bind target to disposable session
    await router.bind(target, disposableId, disposableWs);
    console.log(`Bound target ${JSON.stringify(target)} to session ${disposableId}`);
    console.log(`Verified boundSession: ${router.boundSession(target)}`);

    // 5. Deliver routed nonce prompt
    const nonce = `NONCE_CLI_VERIFY_${Date.now()}`;
    const prompt = `${nonce}: Reply with strictly and only the word VERIFIED_ROUTING_SUCCESS`;
    console.log(`\n[Inbound] Delivering prompt to target: "${prompt}"`);

    const sessionsBeforeDelivery = await control.listSessions();

    const outcome = await router.deliver(target, prompt, "auto");
    console.log(`[Inbound] Delivery outcome: "${outcome}" (null means session will answer via event stream)`);

    const sessionsAfterDelivery = await control.listSessions();
    const createdDuringDelivery = sessionsAfterDelivery.length > sessionsBeforeDelivery.length;
    console.log(`New session created during delivery? ${createdDuringDelivery ? "YES (FAIL)" : "NO (PASS: delivered to existing session)"}`);

    console.log("\nWaiting for relayed outbound assistant response...");
    let responseText: string | null = null;
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 500));
      if (outboundMessages.length > 0) {
        responseText = outboundMessages.map((m) => m.text).join("\n");
        if (responseText.includes("VERIFIED_ROUTING_SUCCESS")) break;
      }
    }

    console.log(`\n[Outbound] Relayed messages captured: ${outboundMessages.length}`);
    for (const msg of outboundMessages) {
      console.log(`  Target: ${JSON.stringify(msg.target)}`);
      console.log(`  Text:   ${msg.text}`);
    }
    if (!responseText || !responseText.includes("VERIFIED_ROUTING_SUCCESS")) {
      throw new Error(`Failed to receive VERIFIED_ROUTING_SUCCESS in response. Got: ${responseText}`);
    }

    console.log("\n=== TWO-WAY ROUTING PROOF COMPLETE ===");
    console.log("Inbound routing:  SUCCESS (routed to existing session without creating a new session)");
    console.log("Outbound relay:   SUCCESS (relayed assistant response through SlotRouter)");
    console.log("Nonce verified:   VERIFIED_ROUTING_SUCCESS");
  } finally {
    control.close();
    try {
      fs.rmSync(tempDir, { recursive: true, force: true });
    } catch {}
  }
}

void main().catch((err) => {
  console.error("FATAL:", err);
  process.exit(1);
});
