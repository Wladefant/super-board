import { test, expect } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { latestSessionPng } from "../src/session-artifacts";

test("PNG lookup selects latest session artifact, excludes other sessions and non-PNG files", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "tg-artifacts-"));
  const session = path.join(root, "session");
  try {
    await fs.mkdir(path.join(session, "local", "evidence"), { recursive: true });
    await fs.mkdir(path.join(root, "other"));
    const old = path.join(session, "old.png"), latest = path.join(session, "local", "evidence", "latest.png");
    await fs.writeFile(old, "old"); await fs.utimes(old, 1, 1);
    await fs.writeFile(latest, "latest");
    await fs.writeFile(path.join(session, "local", "not-image.txt"), "text");
    await fs.writeFile(path.join(root, "other", "foreign.png"), "foreign");
    expect(await latestSessionPng(path.join(session, "root.jsonl"))).toBe(latest);
    expect(await latestSessionPng(path.join(root, "missing", "root.jsonl"))).toBeNull();
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});
