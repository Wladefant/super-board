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

  test("read-only Herdr refuses mutation without invoking the terminal CLI", async () => {
    const runner = new FakeRunner([]);
    const adapter = new HerdrAdapter(runner);
    expect(await adapter.prompt("builder", "hello")).toMatchObject({ ok: false, disposition: "rejected" });
    expect(await adapter.abort("builder")).toMatchObject({ ok: false });
    expect(runner.calls).toEqual([]);
  });
});
