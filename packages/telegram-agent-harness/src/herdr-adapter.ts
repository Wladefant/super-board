import type {
  ActionResult,
  AgentHarnessAdapter,
  AgentSession,
  ArtifactResult,
  CommandRunner,
  DecisionAnswer,
  PromptMode,
  PromptResult,
  UsageResult,
} from "./contract";

interface HerdrAgent {
  pane_id?: unknown;
  terminal_id?: unknown;
  name?: unknown;
  agent?: unknown;
  display_agent?: unknown;
  title?: unknown;
  agent_status?: unknown;
  cwd?: unknown;
  foreground_cwd?: unknown;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function parseEnvelope(text: string): Record<string, unknown> {
  const parsed = JSON.parse(text) as unknown;
  const root = asRecord(parsed);
  const result = asRecord(root?.result);
  if (!root || !result) throw new Error("Herdr returned an invalid JSON response");
  return result;
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

export function mapHerdrState(value: unknown): AgentSession["state"] {
  switch (String(value ?? "").toLowerCase()) {
    case "working":
      return "working";
    case "blocked":
      return "blocked";
    case "idle":
    case "done":
      return "idle";
    case "error":
    case "failed":
      return "error";
    default:
      return "unknown";
  }
}

function toSession(value: unknown, observedAt: number): AgentSession {
  const agent = asRecord(value) as HerdrAgent | null;
  const id = stringValue(agent?.name) ?? stringValue(agent?.pane_id);
  if (!agent || !id) throw new Error("Herdr agent is missing a target identifier");
  const kind = stringValue(agent.agent) ?? stringValue(agent.display_agent);
  const title = stringValue(agent.title);
  const name = stringValue(agent.name) ?? title ?? kind ?? id;
  const state = mapHerdrState(agent.agent_status);
  return {
    backend: "herdr",
    id,
    name,
    state,
    project: stringValue(agent.foreground_cwd) ?? stringValue(agent.cwd),
    observedAt,
    detail: state === "unknown" ? "Herdr cannot classify this agent yet." : kind,
    canPrompt: false,
  };
}

export function parseHerdrAgentList(text: string, observedAt = Date.now()): AgentSession[] {
  const result = parseEnvelope(text);
  if (!Array.isArray(result.agents)) throw new Error("Herdr response has no agent list");
  return result.agents.map(agent => toSession(agent, observedAt));
}

export class HerdrAdapter implements AgentHarnessAdapter {
  readonly backend = "herdr" as const;

  constructor(
    private readonly runner: CommandRunner,
    private readonly binary = "herdr",
  ) {}

  async listSessions(): Promise<AgentSession[]> {
    const observedAt = Date.now();
    const result = await this.runner.run([this.binary, "agent", "list"]);
    if (result.exitCode !== 0) {
      return [{
        backend: this.backend,
        id: "unavailable",
        name: "Herdr",
        state: "stale",
        observedAt,
        detail: "Herdr socket is unavailable.",
        canPrompt: false,
      }];
    }
    try {
      return await Promise.all(parseHerdrAgentList(result.stdout, observedAt).map(session => this.withLifecycle(session)));
    } catch {
      return [{
        backend: this.backend,
        id: "unavailable",
        name: "Herdr",
        state: "error",
        observedAt,
        detail: "Herdr returned an unreadable agent list.",
        canPrompt: false,
      }];
    }
  }

  async getState(sessionId: string): Promise<AgentSession> {
    if (!sessionId || sessionId === "unavailable") {
      return {
        backend: this.backend,
        id: sessionId || "unavailable",
        name: "Herdr",
        state: "stale",
        observedAt: Date.now(),
        detail: "Herdr socket is unavailable.",
        canPrompt: false,
      };
    }
    const result = await this.runner.run([this.binary, "agent", "get", sessionId]);
    if (result.exitCode !== 0) {
      return {
        backend: this.backend,
        id: sessionId,
        name: sessionId,
        state: "stale",
        observedAt: Date.now(),
        detail: "Herdr could not find this live agent.",
        canPrompt: false,
      };
    }
    try {
      const envelope = parseEnvelope(result.stdout);
      return await this.withLifecycle(toSession(envelope.agent, Date.now()));
    } catch {
      return {
        backend: this.backend,
        id: sessionId,
        name: sessionId,
        state: "error",
        observedAt: Date.now(),
        detail: "Herdr returned unreadable agent state.",
        canPrompt: false,
      };
    }
  }

  async prompt(sessionId: string, text: string, _mode: PromptMode = "auto"): Promise<PromptResult> {
    const prompt = text.trim();
    if (!prompt) return { ok: false, disposition: "rejected", detail: "Prompt text is empty." };
    const result = await this.lifecycle(sessionId, ["submit", prompt]);
    if (result?.partial) {
      return {
        ok: false,
        disposition: "rejected",
        detail: "Herdr typed the prompt but did not press Enter because the agent changed state. The text may still sit unsent in the agent's input box. No retry was attempted.",
      };
    }
    if (!result?.delivered || result.status !== "working") {
      return {
        ok: false,
        disposition: "rejected",
        detail: "Native idle-only delivery was not confirmed. No fallback or retry was attempted.",
      };
    }
    return { ok: true, disposition: "started", detail: "Herdr confirmed delivery to the reserved idle generation." };
  }

  async answer(_sessionId: string, _interactionId: string, _answer: DecisionAnswer): Promise<ActionResult> {
    return { ok: false, detail: "Herdr terminal prompts do not provide typed approvals." };
  }

  async abort(sessionId: string): Promise<ActionResult> {
    const observed = await this.lifecycle(sessionId, ["snapshot"]);
    if (!observed || observed.delivered || !["working", "blocked"].includes(observed.status)) {
      return { ok: false, detail: "Herdr did not expose an active native generation to abort." };
    }
    const result = await this.lifecycle(sessionId, ["abort", observed.generation]);
    return result?.delivered && result.generation === observed.generation
      ? { ok: true, detail: "Herdr confirmed the interrupt reached the observed generation." }
      : { ok: false, detail: "Herdr did not confirm that abort. No raw interrupt or retry was sent." };
  }

  private async lifecycle(sessionId: string, action: string[]): Promise<{
    status: string;
    generation: string;
    delivered: boolean;
    partial?: boolean;
  } | null> {
    if (!sessionId || sessionId === "unavailable") return null;
    try {
      const result = await this.runner.run([this.binary, "agent", "lifecycle", sessionId, ...action]);
      if (result.exitCode !== 0) {
        try {
          const failure = JSON.parse(result.stdout) as { error?: { code?: unknown } };
          if (failure.error?.code === "partial_delivery") return { status: "unknown", generation: "", delivered: false, partial: true };
        } catch { /* not a structured error */ }
        return null;
      }
      const native = parseEnvelope(result.stdout);
      if (native.type !== "agent_lifecycle"
        || !["idle", "working", "blocked", "unknown"].includes(String(native.status))
        || typeof native.generation !== "string" || !native.generation
        || typeof native.delivered !== "boolean") return null;
      return { status: native.status as string, generation: native.generation, delivered: native.delivered };
    } catch {
      return null;
    }
  }

  private async withLifecycle(session: AgentSession): Promise<AgentSession> {
    const native = await this.lifecycle(session.id, ["snapshot"]);
    if (!native || native.delivered) return session;
    return { ...session, state: mapHerdrState(native.status), canPrompt: native.status === "idle" };
  }

  async artifacts(_sessionId: string): Promise<ArtifactResult> {
    return { available: false, artifacts: [], detail: "Herdr does not expose native screenshots." };
  }

  async usage(): Promise<UsageResult> {
    return { available: false, observedAt: Date.now(), limits: [], detail: "Herdr does not expose provider usage." };
  }
}
