import { describe, expect, test } from "bun:test";
import type {
  ActionResult,
  AgentHarnessAdapter,
  AgentSession,
  ArtifactResult,
  DecisionAnswer,
  HarnessBackend,
  PromptMode,
  PromptResult,
  UsageResult,
} from "../src/contract";
import {
  renderDecisionCard,
  TelegramHarnessRouter,
  type CallbackBridge,
  type CallbackResolution,
  type TelegramSendOptions,
  type TelegramTransport,
} from "../src/telegram-router";

class FakeAdapter implements AgentHarnessAdapter {
  readonly prompts: Array<{ id: string; text: string; mode?: PromptMode }> = [];
  readonly answers: Array<{ id: string; interaction: string; answer: DecisionAnswer }> = [];
  promptResult: PromptResult = { ok: true, disposition: "started", detail: "Delivered." };
  artifactResult: ArtifactResult = { available: false, artifacts: [] };
  usageResult: UsageResult = { available: false, observedAt: 1, limits: [] };

  constructor(
    readonly backend: HarnessBackend,
    readonly sessions: AgentSession[],
  ) {}

  async listSessions(): Promise<AgentSession[]> { return this.sessions; }
  async getState(id: string): Promise<AgentSession> {
    return this.sessions.find(session => session.id === id) ?? { backend: this.backend, id, name: id, state: "stale", observedAt: 1, canPrompt: false };
  }
  async prompt(id: string, text: string, mode?: PromptMode): Promise<PromptResult> {
    this.prompts.push({ id, text, mode });
    return this.promptResult;
  }
  async answer(id: string, interaction: string, answer: DecisionAnswer): Promise<ActionResult> {
    this.answers.push({ id, interaction, answer });
    return { ok: true, detail: answer.kind === "text" ? "Guidance sent." : "Decision recorded." };
  }
  async abort(_id: string): Promise<ActionResult> { return { ok: true, detail: "Aborted." }; }
  async artifacts(_id: string): Promise<ArtifactResult> { return this.artifactResult; }
  async usage(): Promise<UsageResult> { return this.usageResult; }
}

class FakeTransport implements TelegramTransport {
  readonly messages: Array<{ chat: string; text: string; options: TelegramSendOptions }> = [];
  readonly photos: Array<{ path: string; caption: string; options: TelegramSendOptions }> = [];
  readonly documents: Array<{ path: string; caption: string }> = [];
  readonly callbackAnswers: Array<{ id: string; text: string; alert?: boolean }> = [];
  readonly edits: Array<{ messageId: number; text: string }> = [];

  async sendMessage(chat: string, text: string, options: TelegramSendOptions): Promise<void> { this.messages.push({ chat, text, options }); }
  async sendPhoto(_chat: string, path: string, caption: string, options: TelegramSendOptions): Promise<void> { this.photos.push({ path, caption, options }); }
  async sendDocument(_chat: string, path: string, caption: string, _options: TelegramSendOptions): Promise<void> { this.documents.push({ path, caption }); }
  async answerCallbackQuery(id: string, text: string, alert?: boolean): Promise<void> { this.callbackAnswers.push({ id, text, alert }); }
  async editMessage(_chat: string, messageId: number, text: string, _options: TelegramSendOptions): Promise<void> { this.edits.push({ messageId, text }); }
}

class FakeCallbackBridge implements CallbackBridge {
  constructor(private readonly results: CallbackResolution[]) {}
  async resolveCallback(_data: string, _actor: string, _chat: string): Promise<CallbackResolution> {
    const result = this.results.shift();
    if (!result) return { decision: "reject", detail: "Already used." };
    return result;
  }
}

const session = (backend: HarnessBackend, id: string, state: AgentSession["state"], name = id): AgentSession => ({
  backend,
  id,
  name,
  state,
  observedAt: 1_000,
  canPrompt: state === "idle",
});

describe("TelegramHarnessRouter", () => {
  test("renders escaped live states and session picker for /agents", async () => {
    const adapter = new FakeAdapter("herdr", [session("herdr", "builder", "working", "Build <one>")]);
    const transport = new FakeTransport();
    await new TelegramHarnessRouter([adapter], transport).handleMessage({ chatId: "chat", actorId: "actor", text: "/agents" });
    expect(transport.messages[0].text).toContain("🟢");
    expect(transport.messages[0].text).toContain("Build &lt;one&gt;");
    expect(transport.messages[0].options.parseMode).toBe("HTML");
    expect(transport.messages[0].options.replyMarkup?.inline_keyboard[0][0].callback_data).toBe("sel:herdr:builder");
  });

  test("routes /prompt to the exact backend and reports queueing", async () => {
    const herdr = new FakeAdapter("herdr", [session("herdr", "builder", "idle")]);
    const veyyon = new FakeAdapter("veyyon", [session("veyyon", "main", "working")]);
    veyyon.promptResult = { ok: true, disposition: "queued", detail: "Queued for next turn." };
    const transport = new FakeTransport();
    await new TelegramHarnessRouter([herdr, veyyon], transport).handleMessage({
      chatId: "chat",
      actorId: "actor",
      text: "/prompt veyyon:main finish the check",
    });
    expect(herdr.prompts).toHaveLength(0);
    expect(veyyon.prompts[0]).toMatchObject({ id: "main", text: "finish the check" });
    expect(transport.messages[0].text).toContain("Queued for next turn");
  });

  test("reply provenance overrides a later selected session", async () => {
    const herdr = new FakeAdapter("herdr", [session("herdr", "old", "idle")]);
    const veyyon = new FakeAdapter("veyyon", [session("veyyon", "new", "idle")]);
    const transport = new FakeTransport();
    const router = new TelegramHarnessRouter([herdr, veyyon], transport);
    await router.handleCallback({ chatId: "chat", actorId: "actor", callbackQueryId: "select", messageId: 1, data: "sel:veyyon:new" });
    await router.handleMessage({
      chatId: "chat",
      actorId: "actor",
      text: "answer the earlier post",
      reply: { messageId: 41, backend: "herdr", sessionId: "old", quotedText: "Earlier output" },
    });
    expect(veyyon.prompts).toHaveLength(0);
    expect(herdr.prompts[0].id).toBe("old");
    expect(herdr.prompts[0].text).toContain("Replying to Telegram post #41");
    expect(herdr.prompts[0].text).toContain("not automatic approval");
  });

  test("an unbound reply asks for a target instead of guessing", async () => {
    const transport = new FakeTransport();
    await new TelegramHarnessRouter([], transport).handleMessage({
      chatId: "chat",
      actorId: "actor",
      text: "ambiguous",
      reply: { messageId: 7 },
    });
    expect(transport.messages[0].text).toContain("Reply needs a target");
  });

  test("free text reaches a bound decision as guidance, not approval", async () => {
    const veyyon = new FakeAdapter("veyyon", [session("veyyon", "main", "blocked")]);
    const transport = new FakeTransport();
    await new TelegramHarnessRouter([veyyon], transport).handleMessage({
      chatId: "chat",
      actorId: "actor",
      text: "Use the safer path",
      reply: { messageId: 8, backend: "veyyon", sessionId: "main", decisionId: "decision-1" },
    });
    expect(veyyon.answers[0].answer).toEqual({ kind: "text", text: "Use the safer path" });
  });

  test("callback bridge prevents repeated and wrong-user decisions", async () => {
    const veyyon = new FakeAdapter("veyyon", [session("veyyon", "main", "blocked")]);
    const transport = new FakeTransport();
    const bridge = new FakeCallbackBridge([
      { decision: "deliver", kind: "decision", backend: "veyyon", sessionId: "main", interactionId: "decision-1", answer: { kind: "approve", scope: "once" }, resolvedLabel: "Approved once" },
      { decision: "reject", detail: "Wrong user or already used." },
    ]);
    const router = new TelegramHarnessRouter([veyyon], transport, bridge);
    const callback = { chatId: "chat", actorId: "actor", callbackQueryId: "cb-1", messageId: 9, data: "opaque-1" };
    await router.handleCallback(callback);
    await router.handleCallback({ ...callback, callbackQueryId: "cb-2", actorId: "other" });
    expect(veyyon.answers).toHaveLength(1);
    expect(transport.edits[0].text).toContain("Approved once");
    expect(transport.callbackAnswers[1]).toMatchObject({ alert: true });
  });

  test("/shot uses native photo and original-document callback", async () => {
    const veyyon = new FakeAdapter("veyyon", [session("veyyon", "main", "idle")]);
    veyyon.artifactResult = {
      available: true,
      artifacts: [{ kind: "image", path: "shot-preview.jpg", originalPath: "shot.png", originalCallbackData: "artifact-token", label: "Main · Desktop", createdAt: 5, width: 1440, height: 900 }],
    };
    const transport = new FakeTransport();
    await new TelegramHarnessRouter([veyyon], transport).handleMessage({ chatId: "chat", actorId: "actor", text: "/shot veyyon:main" });
    expect(transport.photos[0]).toMatchObject({ path: "shot-preview.jpg", caption: "Main · Desktop · 1440x900" });
    expect(transport.photos[0].options.replyMarkup?.inline_keyboard[0][0]).toEqual({ text: "📄 Original document", callback_data: "artifact-token" });
  });

  test("decision cards escape HTML and keep A/B callbacks", () => {
    const card = renderDecisionCard({
      title: "Deploy <staging>?",
      summary: "Pick A & B safely.",
      action: "restart --target <id>",
      choices: [
        { label: "A · Approve", callbackData: "opaque-a" },
        { label: "B · Reject", callbackData: "opaque-b" },
      ],
    });
    expect(card.text).toContain("Deploy &lt;staging&gt;");
    expect(card.text).toContain("A &amp; B");
    expect(card.replyMarkup?.inline_keyboard[0].map(button => button.callback_data)).toEqual(["opaque-a", "opaque-b"]);
  });
});
