import * as path from "node:path";
import type { TelegramPoller } from "../extension/poller";

export interface OperatorQuestionInput {
  question: string;
  problem?: string;
  impact?: string;
  options: Array<{ id: string; label: string; description?: string }>;
  recommendation: string;
  details_url?: string;
  request_id?: string;
}
export interface QuestionAnswer {
  question_id: string;
  choice_id: string | null;
  text: string;
  origin: "telegram_account";
  actor_id: string;
  authorization: false;
}
interface Question {
  decision_id: string;
  question: string;
  status: string;
  answer: QuestionAnswer | null;
  transport: QuestionRoute & { kind: "operator_question"; message_id?: number; selection: string | null };
}
interface Result {
  question?: Question;
  questions?: Question[];
  card?: { id: string; text: string; reply_markup?: Record<string, unknown> };
  error?: string;
}
export interface QuestionRoute { session_id: string; chat_id: string; user_id: string }

/** No independent ledger, Telegram poller, approval grant, or new operator turn. */
export class OperatorQuestionService {
  private timer: Timer | undefined;
  private running: Promise<void> | undefined;
  constructor(private readonly poller: TelegramPoller, private readonly route: () => QuestionRoute,
    private readonly decisionsPath: string, private readonly poolPath: string,
    private readonly report: (message: string) => void,
    private readonly invoke: (operation: string, payload: object, card: boolean) => Promise<Result> =
      (operation, payload, card) => this.python(operation, payload, card)) {}

  private async python(operation: string, payload: object, card: boolean): Promise<Result> {
    const boundRoute = this.route();
    const proc = Bun.spawn(["python", path.join(import.meta.dir, "operator_questions.py")],
      { stdin: "pipe", stdout: "pipe", stderr: "pipe" });
    proc.stdin.write(JSON.stringify({ operation, payload, card, route: boundRoute,
      decisions_path: this.decisionsPath, pool_path: this.poolPath }));
    proc.stdin.end();
    const timeout = setTimeout(() => proc.kill(), 15_000);
    try {
      const [output, , exit] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited]);
      const result = JSON.parse(output) as Result;
      if (exit !== 0 || result.error) throw new Error(result.error || "Question store unavailable; no answer recorded");
      const current = this.route();
      if (current.session_id !== boundRoute.session_id || current.chat_id !== boundRoute.chat_id || current.user_id !== boundRoute.user_id) {
        throw new Error("Session route changed; the original question remains bound to its original session");
      }
      return result;
    } finally { clearTimeout(timeout); }
  }

  async ask(input: OperatorQuestionInput): Promise<Question> {
    const result = await this.invoke("register", input, true);
    try { await this.publish(result); }
    catch (error) { throw new Error(`Question ${result.question?.decision_id} persists. ${String(error)}`); }
    return result.question!;
  }

  async get(id: string): Promise<Question> {
    // DecisionManager replaces this file atomically. Waiting never spawns a Python process per tick.
    const data = await Bun.file(this.decisionsPath).json() as { decisions: Record<string, Question> };
    const question = data.decisions[id];
    const route = this.route();
    if (!question || question.transport?.kind !== "operator_question" ||
        question.transport.session_id !== route.session_id ||
        question.transport.chat_id !== route.chat_id || question.transport.user_id !== route.user_id) {
      throw new Error("Question is unavailable on this session and operator route");
    }
    return question;
  }

  async wait(
    id: string,
    signal?: AbortSignal,
    pollIntervalMs = 200,
    timeoutMs = 60_000,
    onProgress?: (elapsedMs: number) => void,
  ): Promise<QuestionAnswer | Question> {
    const startTime = Date.now();
    for (;;) {
      signal?.throwIfAborted();
      const question = await this.get(id);
      if (question.answer) return question.answer;

      const elapsed = Date.now() - startTime;
      if (elapsed >= timeoutMs) {
        return question;
      }
      onProgress?.(elapsed);

      const remaining = timeoutMs - elapsed;
      const waitTime = Math.min(pollIntervalMs, remaining);
      if (waitTime <= 0) return question;

      await new Promise<void>((resolve, reject) => {
        const abort = () => { clearTimeout(timer); reject(signal?.reason); };
        const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, waitTime);
        signal?.addEventListener("abort", abort, { once: true });
        if (signal?.aborted) abort();
      });
    }
  }

  async answer(id: string, eventId: string, input: { choice?: string; text?: string }): Promise<void> {
    const result = await this.invoke("answer", { id, event_id: eventId, ...input }, true);
    if (result.question?.answer) {
      const messageId = result.question.transport.message_id;
      if (messageId) await this.poller.clearCallbackButtons(this.route().chat_id, messageId);
      const sent = await this.poller.sendTelegramMessage(this.route().chat_id,
        "<b>Answer saved for this question.</b> Returned to its waiting task, not sent as a new instruction.",
        undefined, undefined, { decisionId: id });
      if (!sent?.ok) this.report("Answer saved, but Telegram receipt delivery failed");
    } else {
      await this.publish(result, true);
    }
  }

  private async publish(result: Result, edit = false): Promise<void> {
    if (!result.card || !result.question) return;
    const { chat_id } = this.route();
    let sent = edit && result.question.transport.message_id
      ? await this.poller.editTelegramMessage(chat_id, result.question.transport.message_id,
          result.card.text, "HTML", undefined, result.card.reply_markup)
      : null;
    // A lost message may be recreated; a transient edit failure must not duplicate it.
    if (edit && result.question.transport.message_id && !sent?.ok) {
      throw new Error("Selection saved. Reply to the original question to add context and submit your answer.");
    }
    if (!sent) sent = await this.poller.sendTelegramMessage(chat_id, result.card.text,
      result.card.reply_markup, undefined, { decisionId: result.card.id });
    if (!sent?.ok || !sent.result?.message_id) throw new Error("Question persists, but Telegram delivery failed; it remains pending");
    await this.invoke("sent", { id: result.card.id, message_id: sent.result.message_id }, false);
  }

  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => {
      this.running ??= this.remind().catch(error => this.report(String(error))).finally(() => { this.running = undefined; });
    }, 30_000);
    this.timer.unref?.();
  }

  stop(): void { clearInterval(this.timer); this.timer = undefined; }

  private async remind(): Promise<void> {
    const result = await this.invoke("due", {}, false);
    // One reminder per cadence tick, with cadence persisted by the existing decision workflow.
    const question = result.questions?.[0];
    if (question) await this.publish(await this.invoke("get", { id: question.decision_id }, true));
  }
}
