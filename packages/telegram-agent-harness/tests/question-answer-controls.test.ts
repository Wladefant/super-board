import { test, expect, afterEach } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { BotPoolCoordinator } from "../extension/coordinator";
import { DaemonStore } from "../daemon/store";
import { getDaemonSecret, computeTokenHmac } from "../daemon/lane-panel";
import { QuestionsTopic, type QuestionsTransport } from "../daemon/questions-topic";
import type { Question } from "../src/operator-questions";

const cleanup: Array<() => void> = [];
afterEach(() => {
  for (const close of cleanup.splice(0)) close();
});

function setupTestEnvironment() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-answer-controls-test-"));
  const poolDbPath = path.join(dir, "bot_pool.db");
  const daemonDbPath = path.join(dir, "daemon.db");
  const manifestPath = path.join(dir, "manifest.json");
  const channelsDir = path.join(dir, "channels");
  fs.mkdirSync(channelsDir, { recursive: true });

  const coordinator = new BotPoolCoordinator(poolDbPath, manifestPath, channelsDir);
  const store = new DaemonStore(daemonDbPath);
  coordinator.setAuditStore(store);

  const slotId = "slot-test-1";
  const secret = getDaemonSecret(store, slotId);
  coordinator.setDaemonSecret(secret);

  cleanup.push(() => {
    coordinator.close();
    store.close();
    try {
      fs.rmSync(dir, { recursive: true, force: true });
    } catch {}
  });

  return { dir, poolDbPath, daemonDbPath, coordinator, store, slotId, secret };
}

test("HMAC-signed Answer token issuance binds session, chat, operator, expiry and choice", () => {
  const { coordinator, store, slotId, secret } = setupTestEnvironment();

  const decisionId = "tq:dec-123";
  const choiceId = "opt_approve";
  const sessionId = "sess-alpha";
  const chatId = "-1001999888";
  const userId = "4242";
  const now = 1700000000;
  const ttlSeconds = 3600;
  const expiresAt = now + ttlSeconds;

  const record = coordinator.issueDecisionCallback({
    decisionId,
    choiceId,
    sessionId,
    chatId,
    userId,
    secret,
    expiresAt,
    now,
  });

  expect(record.callbackToken).toMatch(/^ans:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+$/);
  expect(record.decisionId).toBe(decisionId);
  expect(record.choiceId).toBe(choiceId);
  expect(record.sessionId).toBe(sessionId);
  expect(record.chatId).toBe(chatId);
  expect(record.userId).toBe(userId);
  expect(record.expiresAt).toBe(expiresAt);
  expect(record.consumedAt).toBeNull();

  // Verify signature directly using computeTokenHmac and same daemon secret
  const colonIdx = record.callbackToken.lastIndexOf(":");
  const sig = record.callbackToken.slice(colonIdx + 1);
  const nonce = record.callbackToken.slice(4, colonIdx);
  const expectedSig = computeTokenHmac(
    secret,
    sessionId,
    chatId,
    "",
    userId,
    Math.floor(expiresAt),
    choiceId,
    nonce,
  );
  expect(sig).toBe(expectedSig);
});

test("authentic Answer validates, consumes once, and audits 'ok' in control_audits", () => {
  const { coordinator, store, secret } = setupTestEnvironment();

  const decisionId = "tq:dec-auth-1";
  const choiceId = "opt_proceed";
  const sessionId = "sess-42";
  const chatId = "-100123456789";
  const userId = "100";
  const now = 1700000000;

  const record = coordinator.issueDecisionCallback({
    decisionId,
    choiceId,
    sessionId,
    chatId,
    userId,
    secret,
    ttlSeconds: 3600,
    now,
  });

  // Validate authentic token
  const resolution = coordinator.validateDecisionCallback(
    record.callbackToken,
    userId,
    chatId,
    sessionId,
    undefined,
    { now: now + 10, eventId: "evt-val-1" },
  );

  expect(resolution.decision).toBe("deliver");
  expect(resolution.record?.choiceId).toBe(choiceId);
  expect(resolution.record?.decisionId).toBe(decisionId);

  // Consume callback atomically
  const consumed = coordinator.consumeDecisionCallback(record.callbackToken, now + 12, "evt-consume-1");
  expect(consumed).toBe(true);

  // Verify single-use: second consumption attempt returns false
  const consumedAgain = coordinator.consumeDecisionCallback(record.callbackToken, now + 15, "evt-consume-2");
  expect(consumedAgain).toBe(false);

  // Verify audit row exists in control_audits
  const auditRow = store.getControlAudit("evt-consume-1");
  expect(auditRow).not.toBeNull();
  expect(auditRow?.action).toBe("answer");
  expect(auditRow?.result).toBe("ok");
  expect(auditRow?.userId).toBe(userId);
  expect(auditRow?.sessionId).toBe(sessionId);
});

test("forged Answer token signature is rejected and audited as 'forged'", () => {
  const { coordinator, store, secret } = setupTestEnvironment();

  const decisionId = "tq:dec-forged-1";
  const choiceId = "opt_bad";
  const sessionId = "sess-forged";
  const chatId = "-100123456789";
  const userId = "100";
  const now = 1700000000;

  const record = coordinator.issueDecisionCallback({
    decisionId,
    choiceId,
    sessionId,
    chatId,
    userId,
    secret,
    ttlSeconds: 3600,
    now,
  });

  // Tamper with the signature portion
  const forgedToken = record.callbackToken.slice(0, -6) + "bad000";

  const resolution = coordinator.validateDecisionCallback(
    forgedToken,
    userId,
    chatId,
    sessionId,
    undefined,
    { now: now + 5, eventId: "evt-forged-1" },
  );

  expect(resolution.decision).toBe("reject_unknown");
  expect(resolution.detail).toMatch(/forged/i);

  // Audit entry must record forged
  const auditRow = store.getControlAudit("evt-forged-1");
  expect(auditRow).not.toBeNull();
  expect(auditRow?.action).toBe("answer");
  expect(auditRow?.result).toBe("forged");
  expect(auditRow?.userId).toBe(userId);
});

test("expired Answer token is rejected and audited as 'expired'", () => {
  const { coordinator, store, secret } = setupTestEnvironment();

  const decisionId = "tq:dec-expired-1";
  const choiceId = "opt_late";
  const sessionId = "sess-exp";
  const chatId = "-100123456789";
  const userId = "100";
  const now = 1700000000;
  const ttlSeconds = 60; // 1 minute TTL

  const record = coordinator.issueDecisionCallback({
    decisionId,
    choiceId,
    sessionId,
    chatId,
    userId,
    secret,
    ttlSeconds,
    now,
  });

  // Attempt validation after expiration (now + 120s > now + 60s)
  const resolution = coordinator.validateDecisionCallback(
    record.callbackToken,
    userId,
    chatId,
    sessionId,
    undefined,
    { now: now + 120, eventId: "evt-expired-1" },
  );

  expect(resolution.decision).toBe("reject_expired");
  expect(resolution.detail).toMatch(/expired/i);

  const auditRow = store.getControlAudit("evt-expired-1");
  expect(auditRow).not.toBeNull();
  expect(auditRow?.action).toBe("answer");
  expect(auditRow?.result).toBe("expired");
  expect(auditRow?.userId).toBe(userId);
  expect(auditRow?.sessionId).toBe(sessionId);
});

test("replayed Answer token is rejected and audited as 'replay'", () => {
  const { coordinator, store, secret } = setupTestEnvironment();

  const decisionId = "tq:dec-replay-1";
  const choiceId = "opt_once";
  const sessionId = "sess-replay";
  const chatId = "-100123456789";
  const userId = "100";
  const now = 1700000000;

  const record = coordinator.issueDecisionCallback({
    decisionId,
    choiceId,
    sessionId,
    chatId,
    userId,
    secret,
    ttlSeconds: 3600,
    now,
  });

  // First consumption succeeds
  const firstConsumed = coordinator.consumeDecisionCallback(record.callbackToken, now + 10, "evt-first-consume");
  expect(firstConsumed).toBe(true);

  // Subsequent validation (replay attempt) must be rejected
  const replayResolution = coordinator.validateDecisionCallback(
    record.callbackToken,
    userId,
    chatId,
    sessionId,
    undefined,
    { now: now + 20, eventId: "evt-replay-1" },
  );

  expect(replayResolution.decision).toBe("reject_already_consumed");
  expect(replayResolution.detail).toMatch(/already submitted and consumed/i);

  const auditRow = store.getControlAudit("evt-replay-1");
  expect(auditRow).not.toBeNull();
  expect(auditRow?.action).toBe("answer");
  expect(auditRow?.result).toBe("replay");
  expect(auditRow?.userId).toBe(userId);
  expect(auditRow?.sessionId).toBe(sessionId);
});

test("unauthorized non-operator user Answer is rejected and audited as 'unauthorized'", () => {
  const { coordinator, store, secret } = setupTestEnvironment();

  const decisionId = "tq:dec-unauth-1";
  const choiceId = "opt_priv";
  const sessionId = "sess-unauth";
  const chatId = "-100123456789";
  const authorizedOperatorId = "100";
  const attackerUserId = "999"; // different user
  const now = 1700000000;

  const record = coordinator.issueDecisionCallback({
    decisionId,
    choiceId,
    sessionId,
    chatId,
    userId: authorizedOperatorId,
    secret,
    ttlSeconds: 3600,
    now,
  });

  // Attempt validation from non-operator attacker ID
  const resolution = coordinator.validateDecisionCallback(
    record.callbackToken,
    attackerUserId,
    chatId,
    sessionId,
    undefined,
    { now: now + 5, eventId: "evt-unauth-1" },
  );

  expect(resolution.decision).toBe("reject_unauthorized");
  expect(resolution.detail).toMatch(/does not match authorized recipient/i);

  const auditRow = store.getControlAudit("evt-unauth-1");
  expect(auditRow).not.toBeNull();
  expect(auditRow?.action).toBe("answer");
  expect(auditRow?.result).toBe("unauthorized");
  expect(auditRow?.userId).toBe(attackerUserId);
  expect(auditRow?.sessionId).toBe(sessionId);
});

test("QuestionsTopic signs question card reply markup with HMAC-signed Answer tokens", async () => {
  const { coordinator, store, slotId } = setupTestEnvironment();

  const question: Question = {
    decision_id: "tq:dec-topic-1",
    question: "Do you want to deploy to staging?",
    status: "pending",
    options: [
      { id: "opt_yes", label: "Yes, deploy" },
      { id: "opt_no", label: "No, abort" },
    ],
    answer: null,
    transport: {
      session_id: "sess-deploy",
      chat_id: "-100555666",
      user_id: "4242",
      kind: "operator_question",
      message_id: 101,
      topic_message_id: null,
      topic_card_at: null,
    },
  };

  const card = {
    id: question.decision_id,
    text: "<b>Do you want to deploy to staging?</b>",
    reply_markup: {
      inline_keyboard: [
        [
          { text: "Yes, deploy", callback_data: "opt_yes" },
          { text: "No, abort", callback_data: "opt_no" },
        ],
      ],
    },
  };

  const sentCards: Array<{ text: string; markup: Record<string, unknown> | undefined }> = [];
  const transport: QuestionsTransport = {
    createTopic: async () => 777,
    send: async (_threadId, text, markup) => {
      sentCards.push({ text, markup });
      return { messageId: 9001 };
    },
    edit: async () => "ok",
    remove: async () => "ok",
    pin: async () => {},
  };

  const questionsTopic = new QuestionsTopic({
    chatId: question.transport.chat_id,
    slotId,
    store,
    coordinator,
    ledger: {
      list: async () => [question],
      cardFor: async () => ({ question, card }),
      cache: async () => {},
    },
    transport,
    finalize: async () => {},
    session: () => ({ name: "test-workspace", ended: false }),
  });

  await questionsTopic.reconcile(false);
  questionsTopic.stop();

  const questionCard = sentCards.find(c => c.markup && "inline_keyboard" in c.markup);
  expect(questionCard).toBeDefined();

  const markup = questionCard!.markup as { inline_keyboard: Array<Array<{ text: string; callback_data: string }>> };
  expect(markup.inline_keyboard[0].length).toBe(2);

  const btnYes = markup.inline_keyboard[0][0];
  const btnNo = markup.inline_keyboard[0][1];

  expect(btnYes.text).toBe("Yes, deploy");
  expect(btnYes.callback_data).toMatch(/^ans:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+$/);

  expect(btnNo.text).toBe("No, abort");
  expect(btnNo.callback_data).toMatch(/^ans:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+$/);

  // Validate the issued callback token from the button
  const resolution = coordinator.validateDecisionCallback(
    btnYes.callback_data,
    question.transport.user_id,
    question.transport.chat_id,
    question.transport.session_id,
  );
  expect(resolution.decision).toBe("deliver");
  expect(resolution.record?.choiceId).toBe("opt_yes");
});

test("multi-slot start validates slot A tokens after B starts", () => {
  const { dir, poolDbPath } = setupTestEnvironment();
  const manifestPath = path.join(dir, "manifest.json");
  const channelsDir = path.join(dir, "channels");

  const store = new DaemonStore(path.join(dir, "multi-slot-daemon.db"));
  const coordinator = new BotPoolCoordinator(poolDbPath, manifestPath, channelsDir);
  coordinator.setAuditStore(store);

  // Slot A starts: acquires secret and sets per-slot secret in coordinator (no shared global secret)
  const slotAId = "slot-alpha";
  const slotASecret = getDaemonSecret(store, slotAId);
  coordinator.setSlotSecret(slotAId, slotASecret);

  // Issue token for Slot A using per-slot secret
  const recA = coordinator.issueDecisionCallback({
    decisionId: "tq:dec-slot-a",
    choiceId: "opt_slot_a",
    sessionId: "sess-slot-a",
    chatId: "-100111",
    userId: "user-1",
    slotId: slotAId,
    ttlSeconds: 3600,
    now: 1700000000,
  });

  // Slot B starts: acquires distinct secret and sets per-slot secret in coordinator
  const slotBId = "slot-beta";
  const slotBSecret = getDaemonSecret(store, slotBId);
  coordinator.setSlotSecret(slotBId, slotBSecret);

  // Issue token for Slot B using per-slot secret
  const recB = coordinator.issueDecisionCallback({
    decisionId: "tq:dec-slot-b",
    choiceId: "opt_slot_b",
    sessionId: "sess-slot-b",
    chatId: "-100222",
    userId: "user-2",
    slotId: slotBId,
    ttlSeconds: 3600,
    now: 1700000000,
  });

  // Verify Slot A token validates successfully after Slot B has started
  const resA = coordinator.validateDecisionCallback(
    recA.callbackToken,
    "user-1",
    "-100111",
    "sess-slot-a",
    undefined,
    { eventId: "evt-slot-a-val", now: 1700000010 },
  );
  expect(resA.decision).toBe("deliver");
  expect(resA.record?.choiceId).toBe("opt_slot_a");

  // Verify Slot B token also validates successfully
  const resB = coordinator.validateDecisionCallback(
    recB.callbackToken,
    "user-2",
    "-100222",
    "sess-slot-b",
    undefined,
    { eventId: "evt-slot-b-val", now: 1700000010 },
  );
  expect(resB.decision).toBe("deliver");
  expect(resB.record?.choiceId).toBe("opt_slot_b");

  // Consume Slot A token
  const consumed = coordinator.consumeDecisionCallback(recA.callbackToken, 1700000015, "evt-slot-a-val");
  expect(consumed).toBe(true);

  // Re-consume with duplicate eventId is rejected
  const consumedDup = coordinator.consumeDecisionCallback(recA.callbackToken, 1700000016, "evt-slot-a-val");
  expect(consumedDup).toBe(false);

  // Duplicate validate after consumption is rejected as already consumed
  const resADup = coordinator.validateDecisionCallback(
    recA.callbackToken,
    "user-1",
    "-100111",
    "sess-slot-a",
    undefined,
    { eventId: "evt-slot-a-val", now: 1700000020 },
  );
  expect(resADup.decision).toBe("reject_already_consumed");

  coordinator.close();
  store.close();
});

test("when signing is enabled, unsigned question tokens are rejected as forged while attach legacy callbacks are preserved", () => {
  const { coordinator, store, secret } = setupTestEnvironment();

  // Register legacy unsigned question token
  coordinator.recordDecisionCallback({
    callbackToken: "legacy_unsigned_question_choice",
    decisionId: "tq:legacy-unsigned-question",
    choiceId: "opt_legacy_choice",
    sessionId: "sess-legacy-q",
    chatId: "-100123456789",
    userId: "100",
    questionHash: "",
    expiresAt: 1700000000 + 3600,
    createdAt: 1700000000,
  });

  // Register legacy attach callback (unrelated to questions)
  coordinator.recordDecisionCallback({
    callbackToken: "att:legacy_session_attach",
    decisionId: "attach:topic-42",
    choiceId: "sess-target-42",
    sessionId: "sess-target-42",
    chatId: "-100123456789",
    userId: "100",
    questionHash: "",
    expiresAt: 1700000000 + 3600,
    createdAt: 1700000000,
  });

  // Unsigned question token MUST be rejected when signing is enabled
  const questionRes = coordinator.validateDecisionCallback(
    "legacy_unsigned_question_choice",
    "100",
    "-100123456789",
    "sess-legacy-q",
    undefined,
    { eventId: "evt-legacy-q-reject", now: 1700000010 },
  );
  expect(questionRes.decision).toBe("reject_unknown");
  expect(store.getControlAudit("evt-legacy-q-reject")?.result).toBe("forged");

  // Unrelated attach callback MUST NOT be broken and should validate successfully
  const attachRes = coordinator.validateDecisionCallback(
    "att:legacy_session_attach",
    "100",
    "-100123456789",
    "sess-target-42",
    undefined,
    { eventId: "evt-attach-ok", now: 1700000010 },
  );
  expect(attachRes.decision).toBe("deliver");
  expect(attachRes.record?.choiceId).toBe("sess-target-42");
});
