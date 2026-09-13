export type HarnessBackend = "herdr" | "veyyon";

export type AgentState = "working" | "blocked" | "idle" | "unknown" | "stale" | "error";

export interface AgentSession {
  backend: HarnessBackend;
  /** Backend-native stable identifier used by prompt/abort. */
  id: string;
  name: string;
  state: AgentState;
  project?: string;
  updatedAt?: number;
  observedAt: number;
  detail?: string;
  canPrompt: boolean;
}

export type PromptMode = "auto" | "queue" | "steer";
export type PromptDisposition = "started" | "queued" | "steered" | "rejected";

export interface PromptResult {
  ok: boolean;
  disposition: PromptDisposition;
  detail: string;
}

export type DecisionAnswer =
  | { kind: "approve"; scope: "once" | "session" }
  | { kind: "reject" }
  | { kind: "option"; option: number }
  | { kind: "text"; text: string };

export interface ActionResult {
  ok: boolean;
  detail: string;
}

export interface ArtifactRef {
  kind: "image" | "document" | "text";
  path: string;
  label: string;
  createdAt: number;
  originalPath?: string;
  originalCallbackData?: string;
  width?: number;
  height?: number;
}

export interface ArtifactResult {
  available: boolean;
  artifacts: ArtifactRef[];
  detail?: string;
}

export interface UsageLimit {
  provider: string;
  label: string;
  remainingPercent: number;
  resetsAt?: number;
}

export interface UsageResult {
  available: boolean;
  observedAt: number;
  limits: UsageLimit[];
  detail?: string;
}

/**
 * Small, transport-free boundary between Telegram and an agent runtime.
 * Unsupported capabilities return an explicit unavailable result; adapters never
 * simulate structured approval through terminal keystrokes.
 */
export interface AgentHarnessAdapter {
  readonly backend: HarnessBackend;
  listSessions(): Promise<AgentSession[]>;
  getState(sessionId: string): Promise<AgentSession>;
  prompt(sessionId: string, text: string, mode?: PromptMode): Promise<PromptResult>;
  answer(sessionId: string, interactionId: string, answer: DecisionAnswer): Promise<ActionResult>;
  abort(sessionId: string): Promise<ActionResult>;
  artifacts(sessionId: string): Promise<ArtifactResult>;
  usage(): Promise<UsageResult>;
}

export interface CommandResult {
  exitCode: number;
  stdout: string;
  stderr: string;
}

export interface CommandRunner {
  run(argv: readonly string[]): Promise<CommandResult>;
}

export class HarnessUnavailableError extends Error {
  constructor(
    public readonly backend: HarnessBackend,
    message: string,
  ) {
    super(message);
    this.name = "HarnessUnavailableError";
  }
}
