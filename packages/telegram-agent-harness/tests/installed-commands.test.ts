import { test, expect } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { Database } from "bun:sqlite";
import {
  handleInstalledCommand,
  readPendingDecisions,
  readRecentOutboundCards,
  renderApprovalRequest,
  type InstalledCommandPort,
} from "../src/installed-commands";
import type { CommandRunner } from "../src/contract";
import { DangerousToolGuard, approveOperation } from "../extension/guard";

function fixture(idle = true) {
  const sent: string[] = [], photos: string[][] = [], inbound: unknown[][] = [], calls: readonly string[][] = [];
  const state = { id: "root", cwd: "C:/project", idle };
  const port: InstalledCommandPort = {
    session: () => state,
    send: async text => { await Promise.resolve(); sent.push(text); },
    photo: async (file, caption) => { await Promise.resolve(); photos.push([file, caption]); },
    latestPng: async () => { await Promise.resolve(); return "C:/session/local/latest.png"; },
    inbound: async (...args) => { await Promise.resolve(); inbound.push(args); },
  };
  const runner: CommandRunner = { run: async argv => {
    await Promise.resolve(); (calls as string[][]).push([...argv]);
    if (argv[0] === "veyyon") return { exitCode: 0, stderr: "", stdout: JSON.stringify({ reports: [{ provider: "Codex Spark", limits: [{ label: "5-hour", status: "ok", amount: { remaining: 85, unit: "percent" }, window: { resetsAt: Date.now() + 3600000 } }] }] }) };
    return { exitCode: 0, stderr: "", stdout: JSON.stringify({ result: argv[2] === "list" ? { agents: [{ pane_id: "h1", name: "Herdr <worker>", agent_status: "idle" }] } : { agent: { pane_id: "h1", name: "Worker", agent_status: "idle" } } }) };
  } };
  return { port, runner, sent, photos, inbound, calls, state };
}

test("agents lists actual adapter and bound root, escapes labels, admits missing worker registry", async () => {
  const f = fixture();
  expect(await handleInstalledCommand("/agents", f.port, f.runner)).toBe(true);
  expect(f.calls).toEqual([["herdr", "agent", "list"]]);
  expect(f.sent[0]).toContain("herdr:Herdr &lt;worker&gt;"); expect(f.sent[0]).toContain("veyyon:root");
  expect(f.sent[0]).toContain("&lt;worker&gt;"); expect(f.sent[0]).toContain("registry is not exposed");
});
test("unavailable Herdr still renders the current session", async () => {
  const f = fixture(); f.runner.run = async () => ({ exitCode: 127, stdout: "", stderr: "unavailable" });
  await handleInstalledCommand("/agents", f.port, f.runner);
  expect(f.sent[0]).toContain("veyyon:root"); expect(f.sent[0]).toContain("herdr:unavailable");
});
test("idle and busy prompt enter existing inbound path after acknowledgement", async () => {
  for (const idle of [true, false]) {
    const f = fixture(idle);
    f.port.inbound = async (text, actualIdle) => { expect(f.sent).toHaveLength(1); f.inbound.push([text, actualIdle]); };
    await handleInstalledCommand("/prompt veyyon:root hello\nworld", f.port, f.runner);
    expect(f.inbound).toEqual([["hello\nworld", idle]]);
  }
});
test("prompt never redirects a worker or switched session to Main", async () => {
  const f = fixture();
  await handleInstalledCommand("/prompt veyyon:foreign hello", f.port, f.runner);
  expect(f.inbound).toHaveLength(0);
  f.port.send = async () => { f.state.id = "switched"; };
  f.state.id = "root";
  await handleInstalledCommand("/prompt veyyon:root hello", f.port, f.runner);
  expect(f.inbound).toHaveLength(0);
});
test("Herdr prompt is explicitly unavailable and never invokes mutation CLI", async () => {
  const f = fixture(); await handleInstalledCommand("/prompt herdr:h1 hello world", f.port, f.runner);
  expect(f.calls).toEqual([]);
  expect(f.sent.join("\n")).toContain("read-only");
  expect(f.inbound).toHaveLength(0);
});
test("shot sends latest PNG as native photo with session caption", async () => {
  const f = fixture(); await handleInstalledCommand("/shot veyyon:root", f.port, f.runner);
  expect(f.photos[0][0]).toBe("C:/session/local/latest.png"); expect(f.photos[0][1]).toContain("veyyon:root");
  f.port.latestPng = async () => null;
  await handleInstalledCommand("/shot veyyon:root", f.port, f.runner);
  expect(f.photos).toHaveLength(1); expect(f.sent[0]).toContain("No screenshot");
});
test("usage reports Spark remaining and reset, never synthetic cost", async () => {
  const f = fixture(); await handleInstalledCommand("/usage", f.port, f.runner);
  expect(f.calls).toEqual([["veyyon", "usage", "--json"]]);
  expect(f.sent[0]).toContain("Codex Spark"); expect(f.sent[0]).toContain("85% remaining"); expect(f.sent[0]).toContain("resets in");
  expect(f.sent[0]).not.toContain("$");
});
test("unknown commands and free-text decisions remain in canonical poller routing", async () => {
  const f = fixture();
  for (const text of ["/promptly hi", "Proceed with option A", "/cancel"]) expect(await handleInstalledCommand(text, f.port, f.runner)).toBe(false);
  expect(f.sent).toHaveLength(0); expect(f.inbound).toHaveLength(0);
});
test("invalid prompt and backend exceptions are explicit and not retried", async () => {
  const f = fixture(); await handleInstalledCommand("/prompt", f.port, f.runner);
  expect(f.sent[0]).toContain("Usage:");
  f.runner.run = async () => { throw new Error("private backend detail"); };
  await handleInstalledCommand("/usage", f.port, f.runner);
  expect(f.sent[1]).toContain("Command unavailable"); expect(f.sent[1]).not.toContain("private");
});

test("status command renders full HTML status with model, agents, decisions, cards, and usage", async () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-status-test-"));
  try {
    const decisionsFile = path.join(tmpDir, "decisions.json");
    fs.writeFileSync(
      decisionsFile,
      JSON.stringify({
        decisions: {
          "DEC-1": {
            decision_id: "DEC-1",
            request_id: "req-101",
            issue_number: 4500,
            issue_url: "https://github.com/Bavariance/polysimulator/issues/4500",
            question: "Approve migration plan?",
            status: "pending",
          },
          "DEC-2": {
            decision_id: "DEC-2",
            question: "Old decision already resolved",
            status: "pending",
          },
        },
      }),
    );

    const dbFile = path.join(tmpDir, "bot_pool.db");
    const db = new Database(dbFile);
    db.run(`CREATE TABLE message_correlations (
      bot_id TEXT, chat_id TEXT, message_id INTEGER, slot_id TEXT,
      session_id TEXT, request_id TEXT, decision_id TEXT, project_path TEXT, created_at REAL
    )`);
    const nowSec = Date.now() / 1000;
    db.run(
      "INSERT INTO message_correlations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
      ["bot1", "chat1", 1476, "slot1", "session-test-01a0", "phases-resend", null, "poly", nowSec - 60],
    );
    db.run(
      "INSERT INTO message_correlations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
      ["bot1", "chat1", 1479, "slot1", "session-test-01a0", "status-summary", "DEC-1", "poly", nowSec - 10],
    );
    db.run("INSERT INTO message_correlations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
      ["other-bot", "other-chat", 9999, "other-slot", "other-session", "foreign-request", "DEC-2", "other-project", nowSec]);
    db.close();

    const f = fixture(false);
    f.state.id = "session-test-01a0";
    (f.state as Record<string, unknown>).model = "gemini-3.8-flash:high";
    (f.state as Record<string, unknown>).decisionsPath = decisionsFile;
    (f.state as Record<string, unknown>).poolDbPath = dbFile;

    const handled = await handleInstalledCommand("/status", f.port, f.runner);
    expect(handled).toBe(true);
    expect(f.sent).toHaveLength(1);
    const output = f.sent[0];

    expect(output).toContain("📊 <b>Veyyon Session Status</b>");
    expect(output).toContain("session-test-01a0");
    expect(output).toContain("gemini-3.8-flash:high");
    expect(output).toContain("<b>Running / Streaming</b>");

    expect(output).toContain("👥 <b>Active Agents");
    expect(output).toContain("veyyon:session-test-01a0");
    expect(output).toContain("herdr:Herdr &lt;worker&gt;");

    expect(output).toContain("<b>Pending decisions in recent session cards (1):</b>");
    expect(output).toContain("<b>DEC-1</b>");
    expect(output).toContain("https://github.com/Bavariance/polysimulator/issues/4500");
    expect(output).toContain("Approve migration plan?");
    expect(output).not.toContain("DEC-2");

    expect(output).toContain("📤 <b>Recent Outbound Cards:</b>");
    expect(output).toContain("Message <code>1479</code> · <code>decision: DEC-1</code>");
    expect(output).toContain("Message <code>1476</code> · <code>phases-resend</code>");
    expect(output).not.toContain("9999");
    expect(output).not.toContain("foreign-request");

    expect(output).toContain("⚡ <b>Resource &amp; Quota Usage:</b>");
    expect(output).toContain("Codex Spark");
    expect(output).toContain("85% remaining");
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test("readPendingDecisions extracts pending and open decisions safely", () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-decisions-test-"));
  try {
    const missing = readPendingDecisions(path.join(tmpDir, "missing.json"));
    expect(missing).toEqual([]);

    const decisionsFile = path.join(tmpDir, "decisions.json");
    fs.writeFileSync(
      decisionsFile,
      JSON.stringify({
        decisions: {
          "D-1": { decision_id: "D-1", question: "Pending Q", status: "pending" },
          "D-2": { decision_id: "D-2", question: "Open Q", status: "open", issue_number: 123 },
          "D-3": { decision_id: "D-3", question: "Resolved Q", status: "resolved" },
        },
      }),
    );

    const result = readPendingDecisions(decisionsFile);
    expect(result).toHaveLength(2);
    expect(result[0].decisionId).toBe("D-1");
    expect(result[1].decisionId).toBe("D-2");
    expect(result[1].issueNumber).toBe(123);
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test("readRecentOutboundCards queries sqlite database with descending limit", () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-cards-test-"));
  try {
    const missing = readRecentOutboundCards("sess", path.join(tmpDir, "missing.db"));
    expect(missing).toEqual([]);

    const dbFile = path.join(tmpDir, "test_pool.db");
    const db = new Database(dbFile);
    db.run(`CREATE TABLE message_correlations (
      bot_id TEXT, chat_id TEXT, message_id INTEGER, slot_id TEXT,
      session_id TEXT, request_id TEXT, decision_id TEXT, project_path TEXT, created_at REAL
    )`);
    for (let i = 1; i <= 7; i++) {
      db.run(
        "INSERT INTO message_correlations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ["b", "c", 1000 + i, "s", "sess", `req-${i}`, null, "p", 100 + i],
      );
    }
    db.run("INSERT INTO message_correlations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
      ["foreign-bot", "foreign-chat", 9999, "foreign-slot", "foreign-session", "foreign-request", null, "foreign-project", 999]);
    db.close();

    const cards = readRecentOutboundCards("sess", dbFile, 5);
    expect(cards).toHaveLength(5);
    expect(cards[0].messageId).toBe(1007);
    expect(cards[4].messageId).toBe(1003);
    expect(readRecentOutboundCards("unknown-session", dbFile)).toEqual([]);
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test("approve command grants exactly the pending guard operation once", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "approval-command-"));
  try {
    const f = fixture(), guard = new DangerousToolGuard(dir);
    f.port.session = () => ({ ...f.state, stateDir: dir });
    f.port.approve = token => approveOperation(dir, token);
    const input = { command: "git push --force origin main" };
    const token = guard.evaluateToolCall("bash", input, true).approvalHash!;
    expect(await handleInstalledCommand(`/approve@sessionbot ${token}`, f.port, f.runner)).toBe(true);
    expect(f.sent[0]).toContain("Approved for one identical call");
    expect(f.calls).toHaveLength(0); expect(f.inbound).toHaveLength(0);
    expect(guard.evaluateToolCall("bash", input, false)).toEqual({ allowed: true });
    expect(guard.evaluateToolCall("bash", input, true).allowed).toBe(false);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
test("approve reports missing binding, invalid tokens and expired requests without dispatch", async () => {
  const f = fixture();
  expect(await handleInstalledCommand("/approve short", f.port, f.runner)).toBe(true);
  expect(f.sent[0]).toContain("Usage:");
  await handleInstalledCommand(`/approve ${"a".repeat(64)}`, f.port, f.runner);
  expect(f.sent[1]).toContain("No channel guard");
  f.port.session = () => ({ ...f.state, stateDir: "C:/isolated-test-only" });
  f.port.approve = () => { throw new Error("Approval request expired or invalid."); };
  await handleInstalledCommand(`/approve ${"a".repeat(64)}`, f.port, f.runner);
  expect(f.sent[2]).toContain("expired");
  expect(f.calls).toHaveLength(0); expect(f.inbound).toHaveLength(0);
});

test("approval buttons preserve the whole grant within Telegram's callback limit", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "approval-card-"));
  try {
    const record = new DangerousToolGuard(dir).evaluateToolCall("bash", { command: "git push --force origin main" }, false, { sessionId: "test", requester: "Agent <one>", task: "Inspect host", cwd: "/tmp" }).approval!;
    const card = renderApprovalRequest(record);
    expect(card.text).toContain("Agent &lt;one&gt;");
    expect(card.text).toContain("<code>git push --force origin main</code>");
    expect(card.text).toContain("<blockquote expandable>");
    expect(card.text).toContain("UTC");
    expect(card.text).toContain(`/approve ${record.token}`);
    const keyboard = card.replyMarkup.inline_keyboard as { text: string; callback_data: string }[][];
    expect(keyboard[0].map(button => button.text)).toEqual(["✅ Yes, run it", "❌ No"]);
    for (const button of keyboard[0]) {
      expect(Buffer.byteLength(button.callback_data)).toBeLessThanOrEqual(64);
      expect(Buffer.from(button.callback_data.slice(5), "base64url").toString("hex")).toBe(record.token);
    }
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
