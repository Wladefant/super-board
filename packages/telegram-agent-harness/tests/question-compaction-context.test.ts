import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@veyyon/coding-agent";
import telegramSessionExtension, { setActiveRuntime } from "../extension/index";
import {
	ACTIVE_ROOT_SYMBOL,
	type ActiveRootState,
	type GlobalTelegramState,
	type TelegramRuntime,
} from "../extension/runtime";
import {
	OperatorQuestionService,
	type QuestionRoute,
} from "../src/operator-questions";

const cleanup: Array<() => Promise<void> | void> = [];

afterEach(async () => {
	for (const close of cleanup.splice(0).reverse()) await close();
	setActiveRuntime(null);
	delete (globalThis as unknown as GlobalTelegramState)[ACTIVE_ROOT_SYMBOL];
});

function inertSchemaModule(): unknown {
	const node: Record<string, unknown> = {};
	for (const key of [
		"object",
		"string",
		"enum",
		"array",
		"boolean",
		"number",
		"optional",
		"default",
	]) {
		node[key] = () => node;
	}
	return node;
}

function extensionHost(): {
	api: ExtensionAPI;
	listeners: Map<string, Array<(...args: unknown[]) => unknown>>;
	warnings: string[];
} {
	const listeners = new Map<string, Array<(...args: unknown[]) => unknown>>();
	const warnings: string[] = [];
	const api = {
		setLabel: () => {},
		on: (event: string, handler: (...args: unknown[]) => unknown) => {
			const handlers = listeners.get(event) ?? [];
			handlers.push(handler);
			listeners.set(event, handlers);
		},
		zod: inertSchemaModule(),
		registerTool: () => {},
		registerCommand: () => {},
		logger: {
			info: () => {},
			warn: (message: string) => warnings.push(message),
			error: () => {},
			debug: () => {},
		},
	} as unknown as ExtensionAPI;
	return { api, listeners, warnings };
}

function questionRecord(
	route: QuestionRoute,
	id: string,
	status: "pending" | "answered" | "resolved",
	answer: {
		choice_id: string | null;
		text: string;
		answered_at: string;
	} | null,
) {
	return {
		decision_id: id,
		question: `Question ${id}?`,
		status,
		created_at: "2026-09-29T13:38:34Z",
		answer: answer
			? {
					question_id: id,
					...answer,
					origin: "telegram_account",
					actor_id: route.user_id,
					authorization: false,
				}
			: null,
		transport: { ...route, kind: "operator_question", selection: null },
	};
}

test("compaction receives current route-owned Telegram question state", async () => {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-question-compaction-"));
	cleanup.push(() => fs.rmSync(dir, { recursive: true, force: true }));
	const decisionsPath = path.join(dir, "decisions.json");
	const route = { session_id: "main-session", chat_id: "100", user_id: "200" };
	const otherRoute = {
		session_id: "other-session",
		chat_id: "100",
		user_id: "200",
	};
	fs.writeFileSync(
		decisionsPath,
		JSON.stringify({
			decisions: {
				"tq:pending": questionRecord(route, "tq:pending", "pending", null),
				"tq:answered": questionRecord(route, "tq:answered", "answered", {
					choice_id: "split",
					text: "Yes, split it into three issues",
					answered_at: "2026-09-29T21:41:45Z",
				}),
				"tq:foreign": questionRecord(otherRoute, "tq:foreign", "pending", null),
				"tq:resolved": questionRecord(route, "tq:resolved", "resolved", null),
			},
		}),
	);

	const questions = new OperatorQuestionService(
		{} as never,
		() => route,
		decisionsPath,
		path.join(dir, "pool.db"),
		() => {},
	);
	const runtime = {
		instanceId: "question-compaction-runtime",
		setReloadTrigger: () => {},
		getQuestions: () => questions,
		onSessionStart: async () => {},
		onSessionShutdown: async () => {},
	} as unknown as TelegramRuntime;
	setActiveRuntime(runtime);
	(globalThis as unknown as GlobalTelegramState)[ACTIVE_ROOT_SYMBOL] = {
		instanceId: runtime.instanceId,
		sessionId: route.session_id,
		questions,
		poller: { getPrimaryChatId: () => route.chat_id },
		activeSlot: { slotId: "slot-test" },
	} as ActiveRootState;

	const host = extensionHost();
	telegramSessionExtension(host.api);
	const context = {
		isSubagent: false,
		taskDepth: 0,
		parentTaskPrefix: undefined,
		sessionId: route.session_id,
	} as unknown as ExtensionContext;
	await host.listeners.get("session_start")?.[0]?.({}, context);
	delete (globalThis as unknown as GlobalTelegramState)[ACTIVE_ROOT_SYMBOL];
	const result = (await host.listeners.get("session_compacting")?.[0]?.(
		{ type: "session_compacting" },
		context,
	)) as { context?: string[] } | undefined;

	expect(host.warnings).toEqual([]);
	expect(result?.context).toHaveLength(1);
	expect(result?.context?.[0]).toContain("authoritative");
	expect(result?.context?.[0]).toContain('"id":"tq:pending"');
	expect(result?.context?.[0]).toContain('"status":"pending"');
	expect(result?.context?.[0]).not.toContain("reminder");
	expect(result?.context?.[0]).toContain('"id":"tq:answered"');
	expect(result?.context?.[0]).toContain(
		'"text":"Yes, split it into three issues"',
	);
	expect(result?.context?.[0]).not.toContain("tq:foreign");
	expect(result?.context?.[0]).not.toContain("tq:resolved");

	await host.listeners.get("session_shutdown")?.[0]?.({}, context);
});
