import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
  BotPoolCoordinator,
  isSlotEligibleForProject,
  matchProjectGlob,
  slotHasSpecificAffinity,
  slotMatchesProject,
} from "../extension/coordinator";

const OPERATOR_CHAT = "1247617658";

interface SlotSpec {
  slotId: string;
  projects?: string[];
  preferredProjects?: string[];
}

interface TestPool {
  channelsDir: string;
  manifestPath: string;
  dbPath: string;
  tokens: Record<string, string>;
}

function buildPool(root: string, specs: SlotSpec[]): TestPool {
  const channelsDir = path.join(root, "channels");
  const manifestPath = path.join(root, "manifest.json");
  const dbPath = path.join(root, "bot_pool.db");
  const tokens: Record<string, string> = {};

  fs.mkdirSync(channelsDir, { recursive: true });

  const slots = specs.map((spec, idx) => {
    const stateDir = path.join(channelsDir, spec.slotId);
    fs.mkdirSync(stateDir, { recursive: true });
    const token = `100000000${idx}:AA${crypto.randomUUID().replace(/-/g, "")}`;
    tokens[spec.slotId] = token;
    fs.writeFileSync(path.join(stateDir, ".env"), `TELEGRAM_BOT_TOKEN=${token}\n`, "utf8");
    fs.writeFileSync(
      path.join(stateDir, "access.json"),
      JSON.stringify({ dmPolicy: "allowlist", allowFrom: [OPERATOR_CHAT] }),
      "utf8",
    );
    return {
      slotId: spec.slotId,
      stateDir,
      projects: spec.projects,
      preferredProjects: spec.preferredProjects,
      enabled: true,
    };
  });

  fs.writeFileSync(manifestPath, JSON.stringify({ version: 1, slots }, null, 2), "utf8");
  return { channelsDir, manifestPath, dbPath, tokens };
}

describe("configuration-driven slot eligibility", () => {
  test("default / empty / omitted config matches any project", () => {
    expect(isSlotEligibleForProject(undefined, "C:/dev/any-project")).toBe(true);
    expect(isSlotEligibleForProject([], "C:/dev/any-project")).toBe(true);
    expect(isSlotEligibleForProject(["*"], "C:/dev/any-project")).toBe(true);
    expect(isSlotEligibleForProject(["**"], "C:/dev/any-project")).toBe(true);
    expect(isSlotEligibleForProject([""], "C:/dev/any-project")).toBe(true);
  });

  test("glob patterns match directory segments, wildcards and exact paths", () => {
    // Substring / segment glob
    expect(matchProjectGlob("*polysimulator*", "C:/dev/wt-polysimulator-slice")).toBe(true);
    expect(matchProjectGlob("*polysimulator*", "C:/dev/super-board")).toBe(false);

    // Path globs with slashes
    expect(matchProjectGlob("**/super-board/**", "C:/dev/super-board/packages/app")).toBe(true);
    expect(matchProjectGlob("C:/dev/polysimulator", "c:\\dev\\polysimulator")).toBe(true);

    // Backward-compatible segment tokens
    expect(slotMatchesProject(["polysimulator"], "C:/Users/user/dev/polysimulator")).toBe(true);
    expect(slotMatchesProject(["soundcore"], "C:/Users/user/dev/polysimulator")).toBe(false);
  });

  test("specific affinity is distinguished from wildcard/shared for priority claiming", () => {
    expect(slotHasSpecificAffinity(["*"], "C:/dev/my-project")).toBe(false);
    expect(slotHasSpecificAffinity(["**"], "C:/dev/my-project")).toBe(false);
    expect(slotHasSpecificAffinity([], "C:/dev/my-project")).toBe(false);
    expect(slotHasSpecificAffinity(["*my-project*"], "C:/dev/my-project")).toBe(true);
  });
});

describe("lease coordinator priority and busy holder diagnostics", () => {
  let tempDir: string;
  let coordinator: BotPoolCoordinator;
  let pool: TestPool;

  beforeAll(() => {
    tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "veyyon-eligibility-diag-"));
    pool = buildPool(tempDir, [
      { slotId: "telegram-shared", projects: ["*"] },
      { slotId: "telegram-polysim", projects: ["*polysimulator*"] },
      { slotId: "telegram-board", projects: ["*super-board*"] },
    ]);
    coordinator = new BotPoolCoordinator(pool.dbPath, pool.manifestPath, pool.channelsDir);
  });

  afterAll(() => {
    coordinator.close();
    try {
      fs.rmSync(tempDir, { recursive: true, force: true });
    } catch {}
  });

  test("dedicated slot is preferred over shared slot for matching project", async () => {
    // polysimulator should claim telegram-polysim first, not telegram-shared
    const claim = await coordinator.acquireLease("sess-poly-1", "C:/dev/polysimulator", 10001);
    expect(claim.ok).toBe(true);
    expect(claim.slot?.slotId).toBe("telegram-polysim");

    // super-board should claim telegram-board first
    const claimBoard = await coordinator.acquireLease("sess-board-1", "C:/dev/super-board", 10002);
    expect(claimBoard.ok).toBe(true);
    expect(claimBoard.slot?.slotId).toBe("telegram-board");

    // third project (unrelated) claims telegram-shared
    const claimOther = await coordinator.acquireLease("sess-other-1", "C:/dev/other-project", 10003);
    expect(claimOther.ok).toBe(true);
    expect(claimOther.slot?.slotId).toBe("telegram-shared");
  });

  test("busy refusal provides detailed diagnostics (sessionId, cwd, PID)", async () => {
    // All 3 slots are now occupied. A new polysimulator request must fail with detailed diagnostics.
    const busyClaim = await coordinator.acquireLease("sess-poly-new", "C:/dev/polysimulator", 10004);
    expect(busyClaim.ok).toBe(false);
    expect(busyClaim.error).toBe("POOL_EXHAUSTED");
    expect(busyClaim.reason).toContain("sess-poly-1");
    expect(busyClaim.reason).toContain("C:/dev/polysimulator");
    expect(busyClaim.reason).toContain("pid 10001");

    // Assert structured busyHolders
    expect(busyClaim.busyHolders).toBeDefined();
    expect(busyClaim.busyHolders!.length).toBeGreaterThanOrEqual(1);

    const polyHolder = busyClaim.busyHolders!.find(h => h.slotId === "telegram-polysim");
    expect(polyHolder).toBeDefined();
    expect(polyHolder?.sessionId).toBe("sess-poly-1");
    expect(polyHolder?.projectPath).toBe("C:/dev/polysimulator");
    expect(polyHolder?.ownerPid).toBe(10001);
    expect(polyHolder?.reason).toContain("Veyyon session active");
  });
});
