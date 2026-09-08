import type {
  ActionResult,
  AgentHarnessAdapter,
  AgentSession,
  ArtifactResult,
  CommandRunner,
  DecisionAnswer,
  PromptMode,
  PromptResult,
  UsageLimit,
  UsageResult,
} from "./contract";
import type { GuiHostPort, GuiHostResponse } from "./gui-host-client";

export interface VeyyonArtifactSource {
  latest(sessionId: string): Promise<ArtifactResult>;
}

interface VeyyonAdapterOptions {
  currentSessionId?: string;
  artifactSource?: VeyyonArtifactSource;
  usageBinary?: string;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function number(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function snapshots(response: GuiHostResponse): Record<string, unknown>[] {
  const values: Record<string, unknown>[] = [];
  for (const event of response.events) {
    const eventRecord = record(event);
    const snapshot = record(eventRecord?.Snapshot);
    if (snapshot) values.push(snapshot);
  }
  return values;
}

export function mapVeyyonState(value: unknown): AgentSession["state"] {
  switch (String(value ?? "").toLowerCase()) {
    case "pending":
    case "running":
    case "working":
      return "working";
    case "blocked":
    case "waiting":
      return "blocked";
    case "complete":
    case "completed":
    case "done":
    case "idle":
    case "interrupted":
    case "aborted":
      return "idle";
    case "error":
    case "failed":
      return "error";
    default:
      return "unknown";
  }
}

function parseVersionedValue(value: unknown): unknown {
  return record(value)?.value;
}

export function parseVeyyonUsage(raw: string): UsageResult {
  const observedAt = Date.now();
  try {
    const root = record(JSON.parse(raw) as unknown);
    const reports = Array.isArray(root?.reports) ? root.reports : [];
    const limits: UsageLimit[] = [];
    for (const reportValue of reports) {
      const report = record(reportValue);
      const provider = text(report?.provider);
      if (!provider || !Array.isArray(report?.limits)) continue;
      for (const limitValue of report.limits) {
        const limit = record(limitValue);
        const amount = record(limit?.amount);
        const window = record(limit?.window);
        const remaining = number(amount?.remaining);
        if (remaining === undefined || amount?.unit !== "percent" || limit?.status !== "ok") continue;
        limits.push({
          provider,
          label: text(limit?.label) ?? text(window?.label) ?? "Usage",
          remainingPercent: Math.max(0, Math.min(100, remaining)),
          resetsAt: number(window?.resetsAt),
        });
      }
    }
    return limits.length > 0
      ? { available: true, observedAt: number(root?.generatedAt) ?? observedAt, limits }
      : { available: false, observedAt, limits: [], detail: "Veyyon reported no current allowance windows." };
  } catch {
    return { available: false, observedAt, limits: [], detail: "Veyyon returned unreadable usage data." };
  }
}

export class VeyyonAdapter implements AgentHarnessAdapter {
  readonly backend = "veyyon" as const;
  private readonly targets = new Map<string, string>();

  constructor(
    private readonly host: GuiHostPort,
    private readonly runner: CommandRunner,
    private readonly options: VeyyonAdapterOptions = {},
  ) {}

  async listSessions(): Promise<AgentSession[]> {
    const observedAt = Date.now();
    let response: GuiHostResponse;
    try {
      response = await this.host.request({ Attach: { endpoint: null } });
    } catch {
      return [{
        backend: this.backend,
        id: this.options.currentSessionId ?? "unavailable",
        name: "Veyyon",
        state: "stale",
        observedAt,
        detail: "Veyyon GUI host is unavailable.",
        canPrompt: false,
      }];
    }

    let sessionValues: unknown[] = [];
    let activeId: string | undefined;
    let agentValues: unknown[] = [];
    let blockedSession: string | undefined;
    for (const snapshot of snapshots(response)) {
      if (Array.isArray(snapshot.Sessions)) {
        const versioned = snapshot.Sessions[0];
        const value = parseVersionedValue(versioned);
        if (Array.isArray(value)) sessionValues = value;
      }
      const active = record(parseVersionedValue(snapshot.ActiveSession));
      if (active) activeId = text(active.id);
      if (Array.isArray(snapshot.Agents)) agentValues = snapshot.Agents;
      const interactions = record(snapshot.Interactions);
      const pending = record(interactions?.pending);
      if (pending) {
        const count = [pending.approvals, pending.questions, pending.plans]
          .reduce((sum, items) => sum + (Array.isArray(items) ? items.length : 0), 0);
        if (count > 0) blockedSession = text(interactions?.session);
      }
    }

    const requestedId = this.options.currentSessionId ?? activeId;
    const rootValue = sessionValues.find(value => text(record(value)?.id) === requestedId)
      ?? (!requestedId ? sessionValues[0] : undefined);
    const root = record(rootValue);
    const rootId = text(root?.id) ?? requestedId;
    const sessions: AgentSession[] = [];
    this.targets.clear();

    if (rootId) {
      const rootState = blockedSession === rootId ? "blocked" : mapVeyyonState(root?.status);
      this.targets.set(rootId, rootId);
      sessions.push({
        backend: this.backend,
        id: rootId,
        name: text(root?.title) ?? `Veyyon ${rootId.slice(0, 8)}`,
        state: root ? rootState : "stale",
        project: text(root?.cwd) ?? text(root?.workspace),
        updatedAt: number(root?.modified_at_ms),
        observedAt,
        detail: root ? "Current Veyyon session" : "Current session is not exposed by this GUI host.",
        canPrompt: Boolean(root && rootState === "idle"),
      });
    }

    for (const agentValue of agentValues) {
      const agent = record(agentValue);
      const id = text(agent?.id);
      if (!id) continue;
      const target = text(agent?.session) ?? rootId;
      if (target) this.targets.set(id, target);
      const state = mapVeyyonState(agent?.status);
      sessions.push({
        backend: this.backend,
        id,
        name: text(agent?.display_name) ?? id,
        state,
        project: text(agent?.scope),
        observedAt,
        detail: text(agent?.kind),
        canPrompt: Boolean(target && state === "idle"),
      });
    }

    return sessions.length > 0 ? sessions : [{
      backend: this.backend,
      id: "unavailable",
      name: "Veyyon",
      state: "stale",
      observedAt,
      detail: "Veyyon GUI host exposed no current session.",
      canPrompt: false,
    }];
  }

  async getState(sessionId: string): Promise<AgentSession> {
    const sessions = await this.listSessions();
    return sessions.find(session => session.id === sessionId) ?? {
      backend: this.backend,
      id: sessionId,
      name: sessionId,
      state: "stale",
      observedAt: Date.now(),
      detail: "Veyyon session is no longer exposed.",
      canPrompt: false,
    };
  }

  async prompt(sessionId: string, promptText: string, mode: PromptMode = "auto"): Promise<PromptResult> {
    const prompt = promptText.trim();
    if (!prompt) return { ok: false, disposition: "rejected", detail: "Prompt text is empty." };
    const state = await this.getState(sessionId);
    const target = this.targets.get(sessionId);
    if (!target || state.state === "stale" || state.state === "error" || state.state === "unknown") {
      return { ok: false, disposition: "rejected", detail: "Veyyon target is not safely promptable; nothing was sent." };
    }
    if (state.state === "blocked") {
      return { ok: false, disposition: "rejected", detail: "Veyyon is waiting on a decision; answer it before prompting." };
    }

    let action: unknown;
    let disposition: PromptResult["disposition"];
    if (state.state === "working") {
      if (mode === "steer") {
        action = { Steer: { session: target, text: prompt, attachments: [] } };
        disposition = "steered";
      } else {
        action = { FollowUp: { session: target, text: prompt, attachments: [] } };
        disposition = "queued";
      }
    } else {
      action = { SubmitPrompt: { session: target, text: prompt, attachments: [] } };
      disposition = "started";
    }

    try {
      await this.host.request(action);
      return {
        ok: true,
        disposition,
        detail: disposition === "queued"
          ? "Queued for the selected Veyyon session's next turn."
          : disposition === "steered"
            ? "Steered into the selected Veyyon session's active turn."
            : "Prompt delivered to the idle Veyyon session.",
      };
    } catch {
      return { ok: false, disposition: "rejected", detail: "Veyyon rejected the prompt; nothing was retried." };
    }
  }

  async answer(sessionId: string, interactionId: string, answer: DecisionAnswer): Promise<ActionResult> {
    const target = this.targets.get(sessionId) ?? sessionId;
    const response = answer.kind === "approve"
      ? { approved: true, scope: answer.scope }
      : answer.kind === "reject"
        ? { approved: false, scope: "once" }
        : answer.kind === "option"
          ? { option: answer.option }
          : { text: answer.text };
    try {
      await this.host.request({ RespondToInteraction: { session: target, interaction_id: interactionId, response } });
      return { ok: true, detail: answer.kind === "text" ? "Guidance sent to the waiting decision." : "Decision recorded once." };
    } catch {
      return { ok: false, detail: "That decision is expired, already answered, or unavailable." };
    }
  }

  async abort(sessionId: string): Promise<ActionResult> {
    const target = this.targets.get(sessionId) ?? sessionId;
    try {
      await this.host.request({ AbortTurn: { session: target } });
      return { ok: true, detail: "Selected Veyyon turn aborted." };
    } catch {
      return { ok: false, detail: "No active Veyyon turn was aborted." };
    }
  }

  async artifacts(sessionId: string): Promise<ArtifactResult> {
    return this.options.artifactSource
      ? this.options.artifactSource.latest(this.targets.get(sessionId) ?? sessionId)
      : { available: false, artifacts: [], detail: "No trusted screenshot source is connected." };
  }

  async usage(): Promise<UsageResult> {
    const result = await this.runner.run([this.options.usageBinary ?? "veyyon", "usage", "--json"]);
    return result.exitCode === 0
      ? parseVeyyonUsage(result.stdout)
      : { available: false, observedAt: Date.now(), limits: [], detail: "Veyyon usage is unavailable." };
  }
}
