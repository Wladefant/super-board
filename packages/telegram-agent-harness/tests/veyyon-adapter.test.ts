import { describe, expect, test } from "bun:test";
import type { AgentHarnessAdapter, CommandResult, CommandRunner } from "../src/contract";
import type { GuiHostPort, GuiHostResponse } from "../src/gui-host-client";
import { VeyyonAdapter, parseVeyyonUsage } from "../src/veyyon-adapter";

class FakeHost implements GuiHostPort {
  readonly actions: unknown[] = [];
  constructor(private readonly responses: GuiHostResponse[]) {}
  async request(action: unknown): Promise<GuiHostResponse> {
    this.actions.push(action);
    const response = this.responses.shift();
    if (!response) throw new Error("Unexpected GUI action");
    return response;
  }
}

class FakeRunner implements CommandRunner {
  constructor(private readonly result: CommandResult) {}
  async run(_argv: readonly string[]): Promise<CommandResult> {
    return this.result;
  }
}

const attachResponse = (status = "Complete"): GuiHostResponse => ({
  events: [
    { Snapshot: { Sessions: [{ revision: 1, value: [{ id: "session-1", title: "Main", cwd: "C:/repo", modified_at_ms: 50, status }] }, []] } },
    { Snapshot: { ActiveSession: { revision: 2, value: { id: "session-1" } } } },
    { Snapshot: { Agents: [{ id: "agent-1", display_name: "Reviewer", kind: "task", status: "blocked", scope: "C:/repo", session: "session-1" }] } },
  ],
});

const emptyRunner = new FakeRunner({ exitCode: 1, stdout: "", stderr: "" });

describe("VeyyonAdapter", () => {
  test("implements the tiny adapter contract and reads GUI snapshots", async () => {
    const adapter: AgentHarnessAdapter = new VeyyonAdapter(new FakeHost([attachResponse()]), emptyRunner, { currentSessionId: "session-1" });
    const sessions = await adapter.listSessions();
    expect(sessions.map(session => [session.id, session.state])).toEqual([
      ["session-1", "idle"],
      ["agent-1", "blocked"],
    ]);
  });

  test("queues a busy prompt explicitly with FollowUp", async () => {
    const host = new FakeHost([attachResponse("Pending"), { events: [] }]);
    const adapter = new VeyyonAdapter(host, emptyRunner, { currentSessionId: "session-1" });
    const result = await adapter.prompt("session-1", "Continue safely");
    expect(result).toMatchObject({ ok: true, disposition: "queued" });
    expect(host.actions[1]).toEqual({ FollowUp: { session: "session-1", text: "Continue safely", attachments: [] } });
  });

  test("maps structured decisions to RespondToInteraction", async () => {
    const host = new FakeHost([{ events: [] }]);
    const adapter = new VeyyonAdapter(host, emptyRunner);
    const result = await adapter.answer("session-1", "decision-7", { kind: "approve", scope: "once" });
    expect(result.ok).toBe(true);
    expect(host.actions[0]).toEqual({
      RespondToInteraction: {
        session: "session-1",
        interaction_id: "decision-7",
        response: { approved: true, scope: "once" },
      },
    });
  });

  test("parses only percent allowance windows from veyyon usage", () => {
    const usage = parseVeyyonUsage(JSON.stringify({
      generatedAt: 500,
      reports: [{
        provider: "openai-codex",
        metadata: { email: "must-not-escape@example.invalid", syntheticCost: 999 },
        limits: [
          { label: "7 days", status: "ok", window: { resetsAt: 9_000 }, amount: { unit: "percent", remaining: 47 } },
          { label: "tokens", status: "ok", window: {}, amount: { unit: "tokens", remaining: 12 } },
        ],
      }],
    }));
    expect(usage).toEqual({
      available: true,
      observedAt: 500,
      limits: [{ provider: "openai-codex", label: "7 days", remainingPercent: 47, resetsAt: 9_000 }],
    });
    expect(JSON.stringify(usage)).not.toContain("email");
    expect(JSON.stringify(usage)).not.toContain("Cost");
  });
});
