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
  test("parses live agent states and stable targets", () => {
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
      ["w1:p3", "idle", true],
    ]);
    expect(sessions[0].observedAt).toBe(123);
  });

  test("reports a disconnected socket as stale, never idle", async () => {
    const adapter = new HerdrAdapter(new FakeRunner([{ exitCode: 1, stdout: "", stderr: "socket unavailable" }]));
    const sessions = await adapter.listSessions();
    expect(sessions).toHaveLength(1);
    expect(sessions[0]).toMatchObject({ backend: "herdr", state: "stale", canPrompt: false });
  });

  test("rejects a busy target without injecting the prompt", async () => {
    const runner = new FakeRunner([ok({
      type: "agent_get",
      agent: { pane_id: "w1:p1", name: "builder", agent: "codex", agent_status: "working" },
    })]);
    const adapter = new HerdrAdapter(runner);
    const result = await adapter.prompt("builder", "do not inject");
    expect(result).toMatchObject({ ok: false, disposition: "rejected" });
    expect(runner.calls).toEqual([["herdr", "agent", "get", "builder"]]);
  });

  test("prompts the exact idle target through the CLI", async () => {
    const runner = new FakeRunner([
      ok({ type: "agent_get", agent: { pane_id: "w1:p2", name: "reviewer", agent: "claude", agent_status: "idle" } }),
      ok({ type: "agent_prompted" }),
    ]);
    const adapter = new HerdrAdapter(runner);
    const result = await adapter.prompt("reviewer", "Review only this diff");
    expect(result).toMatchObject({ ok: true, disposition: "started" });
    expect(runner.calls[1]).toEqual(["herdr", "agent", "prompt", "reviewer", "Review only this diff"]);
  });
});
