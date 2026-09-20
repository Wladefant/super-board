import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { Database } from "bun:sqlite";
import { registerTelegramCommands, renderTelegramHelp, TELEGRAM_COMMANDS } from "../extension/command-registry";
import { TelegramPoller } from "../extension/poller";
import { getDaemonCommands } from "../daemon/router";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

interface RegisteredCommand { command: string; description: string }
interface Registration { scope: { type: string; chat_id: string }; commands: RegisteredCommand[] }

test("private registration exposes exactly the supported help surface, without group scopes", async () => {
  const calls: Registration[] = [];
  globalThis.fetch = (async (_url, init) => {
    calls.push(JSON.parse(String(init?.body)));
    return Response.json({ ok: true, result: true });
  }) as typeof fetch;
  await registerTelegramCommands("1:test", ["101", "102", "101", "-100123"]);
  expect(calls.map(call => call.scope)).toEqual([{ type: "chat", chat_id: "101" }, { type: "chat", chat_id: "102" }]);
  expect(calls[0].commands.map(command => command.command)).toEqual(["status", "agents", "usage", "shot", "prompt", "steer", "cancel", "release", "reload", "help"]);
  const help = renderTelegramHelp();
  for (const command of TELEGRAM_COMMANDS) {
    expect(help).toContain(`<code>${command.syntax}</code>`);
    expect(command.description.length).toBeGreaterThan(0);
    expect(command.description.length).toBeLessThanOrEqual(256);
  }
  expect(help).toContain("<blockquote expandable>");
  expect(help).toContain("it does not capture a live screen");
  expect(help).toContain("Worker targets are not exposed");
});

test("a transport-only host does not advertise unbound harness commands", async () => {
  let commands: RegisteredCommand[] = [];
  globalThis.fetch = (async (_url, init) => {
    commands = JSON.parse(String(init?.body)).commands;
    return Response.json({ ok: true });
  }) as typeof fetch;
  await registerTelegramCommands("1:test", ["101"], false);
  expect(commands.map(command => command.command)).toEqual(["status", "steer", "cancel", "release", "reload", "help"]);
  expect(renderTelegramHelp(false)).not.toContain("/agents");
});

test("registration failures never disclose token-bearing transport errors", async () => {
  globalThis.fetch = Object.assign(async () => { throw new Error("https://api.telegram.org/botSECRET/setMyCommands"); },
    { preconnect: originalFetch.preconnect });
  await expect(registerTelegramCommands("SECRET", ["101"])).rejects.toThrow("Telegram command registration failed;");
});

test("real poller startup registers before polling and still polls after registration fails", async () => {
  for (const registrationOk of [true, false]) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-start-registry-"));
    const calls: string[] = [], failures: string[] = [];
    const poller = new TelegramPoller("1:test", dir, { dmPolicy: "allowlist", allowFrom: ["101"] }, {
      isIdle: () => true, onUserMessage: () => {}, onFollowUp: () => {}, onSteer: () => {}, onAbort: () => {}, onRelease: async () => {}, getStatusText: () => "status", onTelegramTurnStart: () => {}, onLedgerFailure: message => failures.push(message),
    });
    globalThis.fetch = (async (url) => {
      if (String(url).endsWith("setMyCommands")) { calls.push("register"); return Response.json({ ok: registrationOk }); }
      calls.push("poll"); poller.stop(); return Response.json({ ok: false });
    }) as typeof fetch;
    try {
      await poller.start();
      expect(calls).toEqual(["register", "poll"]);
      expect(failures.length).toBe(registrationOk ? 0 : 1);
    } finally { poller.stop(); fs.rmSync(dir, { recursive: true, force: true }); }
  }
});

test("daemon registration includes /sessions, /new, /attach from daemon router", async () => {
  const daemonCommands = getDaemonCommands();
  const commandNames = daemonCommands.map(c => c.command);
  expect(commandNames).toContain("sessions");
  expect(commandNames).toContain("new");
  expect(commandNames).toContain("attach");
  expect(commandNames).toContain("detach");
  expect(commandNames).toContain("where");

  const calls: Registration[] = [];
  globalThis.fetch = (async (_url, init) => {
    if (init && typeof init === "object" && "body" in init && typeof init.body === "string") {
      const parsed: unknown = JSON.parse(init.body);
      if (parsed && typeof parsed === "object" && "commands" in parsed && "scope" in parsed) {
        calls.push(parsed as Registration);
      }
    }
    return Response.json({ ok: true });
  }) as typeof fetch;

  await registerTelegramCommands("1:test", ["101"], true, undefined, daemonCommands);
  expect(calls).toHaveLength(1);
  const registeredNames = calls[0].commands.map(c => c.command);
  expect(registeredNames).toContain("sessions");
  expect(registeredNames).toContain("new");
  expect(registeredNames).toContain("attach");
  expect(registeredNames).toContain("detach");
  expect(registeredNames).toContain("where");
  expect(registeredNames).toContain("status");
});

test("command boundaries, native idle/busy delivery and release preserve the durable ledger", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-command-lifecycle-"));
  const user: string[] = [], steer: string[] = [], replies: string[] = [];
  let idle = true, aborts = 0, released = false, updateId = 0;
  const poller = new TelegramPoller("1:test", dir, { dmPolicy: "allowlist", allowFrom: ["101"] }, {
    isIdle: () => idle, onUserMessage: text => user.push(text), onFollowUp: () => {}, onSteer: text => steer.push(text), onAbort: () => { aborts++; },
    onRelease: async () => { released = true; poller.stop(); }, getStatusText: () => "status", onTelegramTurnStart: () => {}, onLedgerFailure: message => { throw new Error(message); },
    onHarnessCommand: async () => false,
  });
  globalThis.fetch = (async (_url, init) => { replies.push(JSON.parse(String(init?.body)).text); return Response.json({ ok: true, result: { message_id: replies.length, chat: { id: 101 } } }); }) as typeof fetch;
  async function deliver(text: string) {
    poller.ingestUpdates([{ update_id: ++updateId, message: { message_id: updateId, date: 0, chat: { id: 101, type: "private" }, from: { id: 101, is_bot: false }, text } }]);
    await poller.redrivePendingUpdates();
  }
  try {
    await deliver("idle plain");
    await deliver("/steer@SampleBot idle explicit");
    await deliver("/cancel");
    expect(aborts).toBe(0);
    idle = false;
    await deliver("busy plain");
    await deliver("/steer busy explicit");
    await deliver("/steerwrong must not run");
    await deliver("/cancel");
    // Plain and explicit /steer text both arrive stamped with the sending Telegram
    // account, so neither route can be read as an attested operator instruction.
    const stamp = "[Telegram sender: 101; origin: telegram_account; human presence not attested]";
    expect(user).toEqual([`${stamp}\nidle plain`, `${stamp}\nidle explicit`]);
    expect(steer).toEqual([`${stamp}\nbusy plain`, `${stamp}\nbusy explicit`]);
    expect(aborts).toBe(1);
    expect(replies.some(reply => reply.includes("Unknown command"))).toBe(true);
    await deliver("/release");
    expect(released).toBe(true);
    const db = new Database(path.join(dir, "veyyon_bridge_state.db"), { readonly: true });
    try { expect(db.query("SELECT status FROM update_ledger WHERE update_id = ?").get(updateId)).toEqual({ status: "COMPLETED" }); }
    finally { db.close(); }
  } finally { poller.stop(); fs.rmSync(dir, { recursive: true, force: true }); }
});
