/**
 * Bare #N in agent prose links to the session's own repository, read from its origin
 * remote. A wrong parse links a super-board "#224" to some other repository's #224.
 */
import { expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { parseGithubRepo, resolveGithubRepo } from "../extension/github-repo";
import { markdownToTelegramHtml } from "../extension/sanitizer";

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

test("a session with cwd bendhltool (remote Wladefant/shipnovo) links #46 to shipnovo", () => {
  const bendhltoolPath = "C:/Users/wkiri/development/bendhltool";
  if (fs.existsSync(bendhltoolPath)) {
    const repo = resolveGithubRepo(bendhltoolPath);
    expect(repo).toBe("Wladefant/shipnovo");
    const html = markdownToTelegramHtml("Working on #46 today", repo);
    expect(html).toBe('Working on <a href="https://github.com/Wladefant/shipnovo/issues/46">#46</a> today');
  }

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-shipnovo-"));
  try {
    const run = (...args: string[]) => Bun.spawnSync(["git", "-C", dir, ...args], { stdout: "ignore", stderr: "ignore" });
    run("init", "-q");
    run("remote", "add", "origin", "https://github.com/Wladefant/shipnovo.git");
    const repo = resolveGithubRepo(dir);
    expect(repo).toBe("Wladefant/shipnovo");
    const html = markdownToTelegramHtml("Fixed in #46 and PR #47", repo);
    expect(html).toContain('<a href="https://github.com/Wladefant/shipnovo/issues/46">#46</a>');
    expect(html).toContain('<a href="https://github.com/Wladefant/shipnovo/pull/47">#47</a>');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("an unknown cwd leaves bare #N unlinked rather than falling back to polysimulator", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-unknown-cwd-"));
  try {
    const repo = resolveGithubRepo(dir);
    expect(repo).toBeUndefined();
    const html = markdownToTelegramHtml("Working on #46 today", repo);
    expect(html).toBe("Working on #46 today");
    expect(html).not.toContain("https://github.com/Bavariance/polysimulator");
    expect(html).not.toContain("<a href");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("a polysimulator cwd links bare #N to Bavariance/polysimulator as before", () => {
  const polysimPath = "C:/Users/wkiri/development/polysimulator";
  if (fs.existsSync(polysimPath)) {
    const repo = resolveGithubRepo(polysimPath);
    expect(repo).toBe("Bavariance/polysimulator");
    const html = markdownToTelegramHtml("Working on #46 today", repo);
    expect(html).toBe('Working on <a href="https://github.com/Bavariance/polysimulator/issues/46">#46</a> today');
  }

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-polysim-"));
  try {
    const run = (...args: string[]) => Bun.spawnSync(["git", "-C", dir, ...args], { stdout: "ignore", stderr: "ignore" });
    run("init", "-q");
    run("remote", "add", "origin", "https://github.com/Bavariance/polysimulator.git");
    const repo = resolveGithubRepo(dir);
    expect(repo).toBe("Bavariance/polysimulator");
    const html = markdownToTelegramHtml("Working on #46 today", repo);
    expect(html).toBe('Working on <a href="https://github.com/Bavariance/polysimulator/issues/46">#46</a> today');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("explicit qualifiers and full URLs remain untouched regardless of session cwd", () => {
  const repo = "Wladefant/shipnovo";
  const text = "See Bavariance/polysimulator#5157, Wladefant/super-board#121, and https://github.com/Bavariance/polysimulator/pull/4478";
  const html = markdownToTelegramHtml(text, repo);
  expect(html).toContain('<a href="https://github.com/Bavariance/polysimulator/issues/5157">Bavariance/polysimulator#5157</a>');
  expect(html).toContain('<a href="https://github.com/Wladefant/super-board/issues/121">Wladefant/super-board#121</a>');
  expect(html).toContain('https://github.com/Bavariance/polysimulator/pull/4478');
});

test("existing aliases polysimulator and polysim map to Bavariance/polysimulator", () => {
  expect(markdownToTelegramHtml("See #46", "polysimulator")).toBe('See <a href="https://github.com/Bavariance/polysimulator/issues/46">#46</a>');
  expect(markdownToTelegramHtml("See #46", "polysim")).toBe('See <a href="https://github.com/Bavariance/polysimulator/issues/46">#46</a>');
  expect(markdownToTelegramHtml("See #46", "shipnovo")).toBe('See <a href="https://github.com/Wladefant/shipnovo/issues/46">#46</a>');
  expect(markdownToTelegramHtml("See #46", "bendhltool")).toBe('See <a href="https://github.com/Wladefant/shipnovo/issues/46">#46</a>');
});
