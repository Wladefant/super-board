import * as path from "node:path";
import { escapeHtml } from "../extension/sanitizer";
import type { TelegramPoller } from "../extension/poller";
import type { AccessConfig } from "../extension/types";

/** A forum chat is not an operator account; callback ownership must name a user. */
export function questionOperator(access: AccessConfig, chatId: string): string {
  if (!chatId.startsWith("-") && access.allowFrom.includes(chatId)) return chatId;
  const group = access.groups?.[chatId];
  const operators = group
    ? access.allowFrom.filter(id => !id.startsWith("-") && (!group.allowFrom || group.allowFrom.includes(id)))
    : [];
  if (operators.length !== 1) throw new Error("Question route requires exactly one authorized operator for this forum.");
  return operators[0];
}

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
  origin: "telegram_account" | "agent_recorded";
  actor_id: string;
  authorization: false;
  answered_at?: string;
}
export interface Question {
  decision_id: string;
  question: string;
  status: "pending" | "answered" | "dropped" | string;
  created_at?: string;
  options: Array<{ id: string; label: string; description?: string }>;
  answer: QuestionAnswer | null;
  drop?: { reason: string; dropped_at?: string };
  transport: QuestionRoute & {
    kind: "operator_question";
    selection: string | null;
    created_ts?: number;
    /** The copy in the session's own topic. */
    message_id?: number | null;
    /** The copy in the Questions topic. Cache only. */
    topic_message_id?: number | null;
    topic_card_at?: number | null;
    session_finalized?: boolean;
  };
}
interface Result {
  question?: Question;
  questions?: Question[];
  card?: { id: string; text: string; reply_markup?: Record<string, unknown> };
  status?: string;
  error?: string;
}
export interface QuestionRoute { session_id: string; chat_id: string; user_id: string }

/** What the operator sees on a question once it is closed anywhere. */
export function closedQuestionText(question: Question): string {
  const labelOf = (id: string | null) => question.options.find(option => option.id === id)?.label ?? id;
  let verdict: string;
  if (question.status === "dropped") {
    verdict = `🚫 <b>Dropped:</b> ${escapeHtml(question.drop?.reason ?? "no longer needed")}`;
  } else {
    const answer = question.answer;
    const parts = [labelOf(answer?.choice_id ?? null), answer?.text?.slice(0, 300)].filter(Boolean) as string[];
    verdict = `✅ <b>Answered:</b> ${escapeHtml(parts.join(" — ") || "answer saved")}`;
  }
  return `${verdict}\n<i>${escapeHtml(question.question)}</i>`;
}

/** True when a question no longer waits for an answer. */
export const isClosed = (question: Question): boolean => question.status === "answered" || question.status === "dropped";

async function spawnQuestionStore(request: object): Promise<Result> {
  const proc = Bun.spawn(["python", path.join(import.meta.dir, "operator_questions.py")],
    { stdin: "pipe", stdout: "pipe", stderr: "pipe" });
  proc.stdin.write(JSON.stringify(request));
  proc.stdin.end();
  const timeout = setTimeout(() => proc.kill(), 15_000);
  try {
    const [output, , exit] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited]);
    const result = JSON.parse(output) as Result;
    if (exit !== 0 || result.error) throw new Error(result.error || "Question store unavailable; no answer recorded");
    return result;
  } finally { clearTimeout(timeout); }
}

/** Every question of every session, read straight from the decision store. */
export async function readQuestions(decisionsPath: string): Promise<Question[]> {
  let data: { decisions?: Record<string, Question> };
  try { data = await Bun.file(decisionsPath).json(); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  }
  return Object.values(data.decisions ?? {}).filter(question => question.transport?.kind === "operator_question");
}

/** Daemon-owned operations on any question. They cache Telegram ids and mint cards; they never answer. */
export class QuestionStore {
  constructor(readonly decisionsPath: string, private readonly poolPath: string) {}
  list(): Promise<Question[]> { return readQuestions(this.decisionsPath); }
  async cache(id: string, fields: { topic_message_id?: number | null; topic_card_at?: number | null; session_finalized?: boolean }): Promise<void> {
    await spawnQuestionStore({ operation: "cache", payload: { id, ...fields },
      decisions_path: this.decisionsPath, pool_path: this.poolPath });
  }
  async cardFor(id: string): Promise<{ question: Question; card: NonNullable<Result["card"]> }> {
    const result = await spawnQuestionStore({ operation: "card_for", payload: { id },
      decisions_path: this.decisionsPath, pool_path: this.poolPath });
    return { question: result.question!, card: result.card! };
  }
}

type QuestionCompactionRoute = Pick<QuestionRoute, "session_id"> & Partial<QuestionRoute>;
export async function questionCompactionContext(
  decisionsPath: string,
  route: QuestionCompactionRoute,
): Promise<string | undefined> {
  const data = await Bun.file(decisionsPath).json() as { decisions?: Record<string, Question> };
  const questions = Object.values(data.decisions ?? {})
    .filter(question =>
      (question.status === "pending" || isClosed(question)) &&
      question.transport?.kind === "operator_question" &&
      question.transport.session_id === route.session_id &&
      (route.chat_id === undefined || question.transport.chat_id === route.chat_id) &&
      (route.user_id === undefined || question.transport.user_id === route.user_id)
    )
    .sort((left, right) =>
      (left.created_at ?? "").localeCompare(right.created_at ?? "") ||
      left.decision_id.localeCompare(right.decision_id)
    )
    .map(question => ({
      id: question.decision_id,
      question: question.question,
      status: question.status,
      answer: question.answer
        ? {
            choice_id: question.answer.choice_id,
            text: question.answer.text,
            answered_at: question.answer.answered_at ?? null,
          }
        : null,
    }));
  if (questions.length === 0) return undefined;
  return [
    "The following Telegram question-store snapshot is authoritative at compaction time.",
    "An answered question MUST NOT remain pending or blocked in the new summary.",
    JSON.stringify({ questions }),
  ].join("\n");
}

/** No independent ledger, Telegram poller, approval grant, or new operator turn. */
export class OperatorQuestionService {
  constructor(private readonly poller: TelegramPoller, private readonly route: () => QuestionRoute,
    private readonly decisionsPath: string, private readonly poolPath: string,
    private readonly report: (message: string) => void,
    private readonly invoke: (operation: string, payload: object, card: boolean) => Promise<Result> =
      (operation, payload, card) => this.python(operation, payload, card)) {}

  private async python(operation: string, payload: object, card: boolean): Promise<Result> {
    const boundRoute = this.route();
    const result = await spawnQuestionStore({ operation, payload, card, route: boundRoute,
      decisions_path: this.decisionsPath, pool_path: this.poolPath });
    const current = this.route();
    if (current.session_id !== boundRoute.session_id || current.chat_id !== boundRoute.chat_id || current.user_id !== boundRoute.user_id) {
      throw new Error("Session route changed; the original question remains bound to its original session");
    }
    return result;
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

  /**
   * The route-owned question state that a compaction summary must reconcile.
   *
   * Answers intentionally do not enter the session as new user turns: they resolve
   * only the waiting telegram_question call. A later compaction can therefore see an
   * old "pending" summary after the durable store has moved on. This snapshot makes
   * the store authoritative without waking the agent or duplicating the answer.
   */
  async compactionContext(): Promise<string | undefined> {
    return questionCompactionContext(this.decisionsPath, this.route());
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
      if (question.answer || question.status === "dropped") return question.answer ?? question;

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

  /** Records an operator answer that arrived by button or reply, then closes the session copy. */
  async answer(id: string, eventId: string, input: { choice?: string; text?: string }): Promise<Question> {
    const result = await this.invoke("answer", { id, event_id: eventId, ...input }, true);
    if (isClosed(result.question!)) await this.finalize(result.question!);
    else await this.publish(result, true);
    return result.question!;
  }

  /** The operator answered somewhere else (terminal, prose the agent heard). */
  async resolve(id: string, input: { choice?: string; text?: string }): Promise<Question> {
    const result = await this.invoke("resolve", { id, ...input }, false);
    await this.finalize(result.question!);
    return result.question!;
  }

  /** The question no longer matters. */
  async drop(id: string, reason: string): Promise<Question> {
    const result = await this.invoke("drop", { id, reason }, false);
    await this.finalize(result.question!);
    return result.question!;
  }

  /**
   * Rewrites the copy in the session's topic to its verdict. Safe to repeat: the daemon calls it again
   * for any closed question whose copy was not finalized, and a missing or unchanged message counts as done.
   */
  async finalize(question: Question): Promise<void> {
    const messageId = question.transport.message_id;
    if (messageId && !question.transport.session_finalized) {
      const edited = await this.poller.editTelegramMessage(question.transport.chat_id, messageId,
        closedQuestionText(question), "HTML");
      const done = edited?.ok || /not modified|not found/i.test(edited?.description ?? "");
      if (!done) { this.report(`Question ${question.decision_id} is closed, but its Telegram copy was not updated`); return; }
    }
    await this.invoke("cache", { id: question.decision_id, session_finalized: true }, false);
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
    // The card text is finished HTML, exactly as the edit above treats it.
    if (!sent) sent = await this.poller.sendTelegramMessage(chat_id, result.card.text,
      "HTML", result.card.reply_markup, { decisionId: result.card.id });
    if (!sent?.ok || !sent.result?.message_id) throw new Error(`Question persists, but Telegram delivery failed${sent?.description ? ` (${sent.description})` : ""}; it remains pending`);
    await this.invoke("sent", { id: result.card.id, message_id: sent.result.message_id }, false);
  }
}
