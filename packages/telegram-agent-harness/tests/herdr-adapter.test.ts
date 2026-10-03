import { describe, expect, test } from "bun:test";
import type { CommandResult, CommandRunner } from "../src/contract";
import { HerdrAdapter, parseHerdrAgentList } from "../src/herdr-adapter";

class FakeRunner implements CommandRunner {
  readonly calls: string[][] = [];
  constructor(private readonly results: CommandResult[]) {}
  async run(argv: readonly string[]): Promise<CommandResult> {
    this.calls.push([...argv]);
    const result = this.results.shift();
    if (!result) throw new Error("Unexpected command");
    return result;
  }
}

const ok = (value: unknown): CommandResult => ({
  exitCode: 0,
  stdout: JSON.stringify({ id: "test", result: value }),
  stderr: "",
});

describe("HerdrAdapter", () => {
  test("parses live agent states and initializes canPrompt to false", () => {
    const sessions = parseHerdrAgentList(JSON.stringify({
      id: "cli:agent:list",
      result: {
        type: "agent_list",
        agents: [
          { pane_id: "w1:p1", name: "builder", agent: "codex", agent_status: "working", cwd: "C:/repo" },
          { pane_id: "w1:p2", agent: "claude", agent_status: "blocked", title: "Review" },
          { pane_id: "w1:p3", agent: "pi", agent_status: "done" },
        ],
      },
    }), 123);

    expect(sessions.map(session => [session.id, session.state, session.canPrompt])).toEqual([
      ["builder", "working", false],
      ["w1:p2", "blocked", false],
      ["w1:p3", "idle", false],
    ]);
    expect(sessions[0].observedAt).toBe(123);
  });

  test("reports a disconnected socket as stale, never idle", async () => {
    const adapter = new HerdrAdapter(new FakeRunner([{ exitCode: 1, stdout: "", stderr: "socket unavailable" }]));
    const sessions = await adapter.listSessions();
    expect(sessions).toHaveLength(1);
    expect(sessions[0]).toMatchObject({ backend: "herdr", state: "stale", canPrompt: false });
  });

  test("listSessions only advertises canPrompt after valid native snapshot", async () => {
    const runner = new FakeRunner([
      ok({
        type: "agent_list",
        agents: [
          { pane_id: "w1:p1", name: "builder", agent_status: "working" },
          { pane_id: "w1:p2", name: "reviewer", agent_status: "done" },
        ],
      }),
      ok({ type: "agent_lifecycle", status: "working", generation: "gen-1", delivered: false }),
      ok({ type: "agent_lifecycle", status: "idle", generation: "gen-2", delivered: false }),
    ]);
    const adapter = new HerdrAdapter(runner);
    const sessions = await adapter.listSessions();
    expect(sessions).toHaveLength(2);
    expect(sessions[0]).toMatchObject({ id: "builder", state: "working", canPrompt: false });
    expect(sessions[1]).toMatchObject({ id: "reviewer", state: "idle", canPrompt: true });
    expect(runner.calls).toEqual([
      ["herdr", "agent", "list"],
      ["herdr", "agent", "lifecycle", "builder", "snapshot"],
      ["herdr", "agent", "lifecycle", "reviewer", "snapshot"],
    ]);
  });

  test("getState only advertises canPrompt after valid native snapshot", async () => {
    const runner = new FakeRunner([
      ok({ type: "agent_get", agent: { pane_id: "w1:p2", name: "reviewer", agent_status: "idle" } }),
      ok({ type: "agent_lifecycle", status: "idle", generation: "gen-2", delivered: false }),
    ]);
    const adapter = new HerdrAdapter(runner);
    const session = await adapter.getState("reviewer");
    expect(session).toMatchObject({ id: "reviewer", state: "idle", canPrompt: true });
    expect(runner.calls).toEqual([
      ["herdr", "agent", "get", "reviewer"],
      ["herdr", "agent", "lifecycle", "reviewer", "snapshot"],
    ]);
  });

  test("empty prompt executes nothing", async () => {
    const runner = new FakeRunner([]);
    const adapter = new HerdrAdapter(runner);
    const emptyResult = await adapter.prompt("builder", "");
    expect(emptyResult).toEqual({ ok: false, disposition: "rejected", detail: "Prompt text is empty." });
    const whitespaceResult = await adapter.prompt("builder", "   \t\n  ");
    expect(whitespaceResult).toEqual({ ok: false, disposition: "rejected", detail: "Prompt text is empty." });
    expect(runner.calls).toHaveLength(0);
  });

  test("verifies exactly one native lifecycle submit for a valid prompt and no legacy prompt command", async () => {
    const runner = new FakeRunner([
      ok({ type: "agent_lifecycle", status: "working", generation: "gen-1", delivered: true }),
    ]);
    const adapter = new HerdrAdapter(runner);
    const result = await adapter.prompt("reviewer", "Review only this diff");
    expect(result).toEqual({
      ok: true,
      disposition: "started",
      detail: "Herdr confirmed delivery to the reserved idle generation.",
    });
    expect(runner.calls).toEqual([
      ["herdr", "agent", "lifecycle", "reviewer", "submit", "Review only this diff"],
    ]);
  });

  test("native busy rejection without any fallback or retry", async () => {
    const runner = new FakeRunner([
      ok({ type: "agent_lifecycle", status: "working", generation: "gen-1", delivered: false }),
    ]);
    const adapter = new HerdrAdapter(runner);
    const result = await adapter.prompt("builder", "do not inject");
    expect(result).toEqual({
      ok: false,
      disposition: "rejected",
      detail: "Native idle-only delivery was not confirmed. No fallback or retry was attempted.",
    });
    expect(runner.calls).toEqual([
      ["herdr", "agent", "lifecycle", "builder", "submit", "do not inject"],
    ]);
  });

  test("malformed and empty success envelopes rejected", async () => {
    for (const malformed of [
      {},
      { type: "wrong_type" },
      { type: "agent_lifecycle", status: "invalid_status", generation: "g1", delivered: true },
      { type: "agent_lifecycle", status: "working", generation: "", delivered: true },
      { type: "agent_lifecycle", status: "working", generation: "g1", delivered: "yes" },
    ]) {
      const runner = new FakeRunner([ok(malformed)]);
      const adapter = new HerdrAdapter(runner);
      const result = await adapter.prompt("builder", "hello");
      expect(result).toEqual({
        ok: false,
        disposition: "rejected",
        detail: "Native idle-only delivery was not confirmed. No fallback or retry was attempted.",
      });
      expect(runner.calls).toEqual([
        ["herdr", "agent", "lifecycle", "builder", "submit", "hello"],
      ]);
    }
  });

  test("old-native unknown-command failure rejected without fallback", async () => {
    const runner = new FakeRunner([{ exitCode: 1, stdout: "", stderr: "unknown command: lifecycle" }]);
    const adapter = new HerdrAdapter(runner);
    const result = await adapter.prompt("builder", "hello");
    expect(result).toEqual({
      ok: false,
      disposition: "rejected",
      detail: "Native idle-only delivery was not confirmed. No fallback or retry was attempted.",
    });
    expect(runner.calls).toEqual([
      ["herdr", "agent", "lifecycle", "builder", "submit", "hello"],
    ]);
  });

  test("abort snapshots native status+generation then passes exact token to native abort", async () => {
    const runner = new FakeRunner([
      ok({ type: "agent_lifecycle", status: "working", generation: "gen-token-1", delivered: false }),
      ok({ type: "agent_lifecycle", status: "idle", generation: "gen-token-1", delivered: true }),
    ]);
    const adapter = new HerdrAdapter(runner);
    const result = await adapter.abort("builder");
    expect(result).toEqual({
      ok: true,
      detail: "Herdr confirmed the interrupt reached the observed generation.",
    });
    expect(runner.calls).toEqual([
      ["herdr", "agent", "lifecycle", "builder", "snapshot"],
      ["herdr", "agent", "lifecycle", "builder", "abort", "gen-token-1"],
    ]);
  });

  test("abort succeeds when native snapshot observes blocked status", async () => {
    const runner = new FakeRunner([
      ok({ type: "agent_lifecycle", status: "blocked", generation: "gen-token-blocked", delivered: false }),
      ok({ type: "agent_lifecycle", status: "idle", generation: "gen-token-blocked", delivered: true }),
    ]);
    const adapter = new HerdrAdapter(runner);
    const result = await adapter.abort("builder");
    expect(result).toEqual({
      ok: true,
      detail: "Herdr confirmed the interrupt reached the observed generation.",
    });
  });

  test("abort fails immediately when snapshot status is not working or blocked", async () => {
    const runner = new FakeRunner([
      ok({ type: "agent_lifecycle", status: "idle", generation: "gen-token-idle", delivered: false }),
    ]);
    const adapter = new HerdrAdapter(runner);
    const result = await adapter.abort("builder");
    expect(result).toEqual({
      ok: false,
      detail: "Herdr did not expose an active native generation to abort.",
    });
    expect(runner.calls).toEqual([
      ["herdr", "agent", "lifecycle", "builder", "snapshot"],
    ]);
  });

  test("stale-generation rejection after settlement or replacement yields false and never raw send-keys", async () => {
    const runner = new FakeRunner([
      ok({ type: "agent_lifecycle", status: "working", generation: "gen-token-stale", delivered: false }),
      ok({ type: "agent_lifecycle", status: "idle", generation: "gen-token-stale", delivered: false }),
    ]);
    const adapter = new HerdrAdapter(runner);
    const result = await adapter.abort("builder");
    expect(result).toEqual({
      ok: false,
      detail: "Herdr did not confirm that abort. No raw interrupt or retry was sent.",
    });
    expect(runner.calls).toEqual([
      ["herdr", "agent", "lifecycle", "builder", "snapshot"],
      ["herdr", "agent", "lifecycle", "builder", "abort", "gen-token-stale"],
    ]);
  });

  test("abort fails when abort returns mismatched generation token", async () => {
    const runner = new FakeRunner([
      ok({ type: "agent_lifecycle", status: "working", generation: "gen-token-1", delivered: false }),
      ok({ type: "agent_lifecycle", status: "idle", generation: "gen-token-2", delivered: true }),
    ]);
    const adapter = new HerdrAdapter(runner);
    const result = await adapter.abort("builder");
    expect(result).toEqual({
      ok: false,
      detail: "Herdr did not confirm that abort. No raw interrupt or retry was sent.",
    });
    expect(runner.calls).toEqual([
      ["herdr", "agent", "lifecycle", "builder", "snapshot"],
      ["herdr", "agent", "lifecycle", "builder", "abort", "gen-token-1"],
    ]);
  });
});
