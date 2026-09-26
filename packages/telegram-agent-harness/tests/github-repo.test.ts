/**
 * Bare #N in agent prose links to the session's own repository, read from its origin
 * remote. A wrong parse links a super-board "#224" to some other repository's #224.
 */
import { expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { parseGithubRepo, resolveGithubRepo } from "../extension/github-repo";

test("https, ssh and scp-like GitHub remotes resolve to owner/repo", () => {
  expect(parseGithubRepo("https://github.com/Wladefant/super-board.git\n")).toBe("Wladefant/super-board");
  expect(parseGithubRepo("https://github.com/Bavariance/polysimulator")).toBe("Bavariance/polysimulator");
  expect(parseGithubRepo("git@github.com:Wladefant/veyyon.git")).toBe("Wladefant/veyyon");
  expect(parseGithubRepo("ssh://git@github.com/Wladefant/my.repo.git")).toBe("Wladefant/my.repo");
});

test("a non-GitHub remote resolves to nothing, so the configured default applies", () => {
  expect(parseGithubRepo("https://gitlab.com/Wladefant/super-board.git")).toBeNull();
  expect(parseGithubRepo("")).toBeNull();
});

test("a directory's origin remote resolves, and a non-checkout yields undefined", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-github-repo-"));
  try {
    const run = (...args: string[]) => Bun.spawnSync(["git", "-C", dir, ...args], { stdout: "ignore", stderr: "ignore" });
    const plain = path.join(dir, "plain");
    fs.mkdirSync(plain);
    expect(resolveGithubRepo(plain)).toBeUndefined();

    run("init", "-q");
    run("remote", "add", "origin", "git@github.com:Wladefant/super-board.git");
    expect(resolveGithubRepo(dir)).toBe("Wladefant/super-board");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
