import { expect, test } from "bun:test";
import type { ExtensionAPI } from "@veyyon/coding-agent";
import { TelegramRuntime } from "../extension/runtime";

type Reply = { ok: boolean; result?: { message_id: number } } | null;

/** Fake Telegram transport: `messages` is what the operator sees; edit outcomes are scripted. */
function fakeTransport(editScript: Array<"ok" | "null" | "refused">) {
  const messages = new Map<number, string>();
  let nextId = 100;
  const transport = {
    getPrimaryChatId: () => "chat-1",
    sendTelegramMessage: async (_chat: string, text: string): Promise<Reply> => {
      const id = nextId++;
      messages.set(id, text);
      return { ok: true, result: { message_id: id } };
    },
    editTelegramMessage: async (_chat: string, id: number, text: string): Promise<Reply> => {
      const outcome = editScript.shift() ?? "ok";
      if (outcome === "null") return null;
      if (outcome === "refused") return { ok: false };
      messages.set(id, text);
      return { ok: true };
    },
  };
  return { transport, messages };
}

function runtimeWith(transport: object) {
  const runtime = new TelegramRuntime(Object.create(null) as ExtensionAPI);
  Object.assign(runtime, { poller: transport });
  const sync: (text: string, final: boolean) => Promise<void> = Reflect.get(runtime, "syncAssistantOutput").bind(runtime);
  return sync;
}

test("a dropped mid-stream edit is retried on the next flush and the full text arrives once", async () => {
  const { transport, messages } = fakeTransport(["ok", "null"]);
  const sync = runtimeWith(transport);
  await sync("Hello", false);
  await sync("Hello wor", false); // edit succeeds
  await sync("Hello world", false); // edit dropped by the governor
  expect([...messages.values()]).toEqual(["Hello wor"]);
  await sync("Hello world", true); // final pass retries the pending text
  expect([...messages.values()]).toEqual(["Hello world"]);
});

test("a dropped final edit falls back to a new message carrying the full text, delivered once", async () => {
  const { transport, messages } = fakeTransport(["null", "refused"]);
  const sync = runtimeWith(transport);
  await sync("Partial", false);
  await sync("Partial and complete", true);
  expect([...messages.values()]).toEqual(["Partial", "Partial and complete"]);
});

test("a final edit that succeeds on its retry does not send a duplicate", async () => {
  const { transport, messages } = fakeTransport(["refused", "ok"]);
  const sync = runtimeWith(transport);
  await sync("Partial", false);
  await sync("Partial and complete", true);
  expect([...messages.values()]).toEqual(["Partial and complete"]);
});
