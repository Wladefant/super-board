/**
 * question-factory.ts — The one construction of OperatorQuestionService for a
 * live Veyyon session.
 *
 * The constructor takes many positional arguments (poller, route, decisions
 * path, pool path, report, invoke, coordinator, secret, wake). A wrong order
 * is easy to write and hard to notice, so every runtime construction goes
 * through this factory and tests/runtime-question-service.test.ts pins the
 * wiring end to end (publish + daemon-side token validation).
 */

import * as os from "node:os";
import * as path from "node:path";
import { getDaemonDbPath } from "../daemon/config";
import { getDaemonSecret } from "../daemon/daemon-secret";
import type { QuestionRoute } from "./harness/operator-questions";
import { OperatorQuestionService } from "./harness/operator-questions";
import type { TelegramPoller } from "./poller";

export interface CreateOperatorQuestionServiceOptions {
  poller: TelegramPoller;
  route: () => QuestionRoute;
  report: (message: string) => void;
  coordinator: BotPoolCoordinator;
  slotId: string;
  /** Test-only paths override; production uses the machine defaults. */
  decisionsPath?: string;
  poolPath?: string;
  /** Test-only signing secret; production reads the slot secret from the daemon store. */
  secret?: Buffer;
}

/** The canonical machine-wide question and pool paths (daemon/runtime.ts uses the same). */
export function operatorQuestionPaths(): { decisionsPath: string; poolPath: string } {
  return {
    decisionsPath: path.join(os.homedir(), ".veyyon", "workflows", "decisions.json"),
    poolPath: process.env.VEYYON_POOL_DB ?? path.join(os.homedir(), ".veyyon", "telegram", "bot_pool.db"),
  };
}

export function createOperatorQuestionService(
  options: CreateOperatorQuestionServiceOptions,
): OperatorQuestionService {
  const defaults = operatorQuestionPaths();
  // Order mirrors OperatorQuestionService: (poller, route, decisionsPath, poolPath,
  // report, invoke, coordinator, secret, wake). `undefined` for invoke falls back to
  // the inherited python question-store bridge.
  return new OperatorQuestionService(
    options.poller,
    options.route,
    options.decisionsPath ?? defaults.decisionsPath,
    options.poolPath ?? defaults.poolPath,
    options.report,
    undefined,
    options.coordinator,
    options.secret ?? getDaemonSecret({ stateDir: path.dirname(getDaemonDbPath()) }, options.slotId),
  );
}
