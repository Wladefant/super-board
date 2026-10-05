/**
 * week-github.ts — guarded GitHub read for the Week view (issue #477).
 *
 * One batched GraphQL search per owner (PRs the operator authored in the window), cached 5 minutes.
 * Every refresh takes ONE quota reading through `scripts/super-board-gh-guard.sh`
 * (`sb_gh_guard_begin_cycle`, then `sb_gh_guard_check <cost>`). Exit 75 (reserve reached or quota
 * unreadable) means: no `gh` call, serve the last result with `stale = true`. The guard never sleeps
 * and never retries here either.
 */
import { execFile } from "node:child_process";
import * as path from "node:path";
import type { WeekPullRequest } from "./week-summary";

export const GITHUB_CACHE_MS = 5 * 60_000;
/** Estimated GraphQL points of one search query (cost 1 per owner) plus margin. */
export const GITHUB_QUERY_COST = 10;
/** After a refused or failed read, serve the last result for this long before asking the guard again. */
export const GITHUB_RETRY_MS = 60_000;

export interface GithubReaderDeps {
  now(): number;
  /** Run the guard; resolves the shell exit code (0 = affordable, 75 = stop). */
  guard(cost: number): Promise<number>;
  /** Run one GraphQL query and resolve the parsed JSON. */
  graphql(query: string): Promise<unknown>;
}

export interface GithubResult { pullRequests: WeekPullRequest[]; fetchedAt: number; stale: boolean; staleReason: string | null }

export function buildQuery(owners: string[], sinceIso: string): string {
  const aliases = owners.map((owner, i) =>
    `o${i}: search(type: ISSUE, first: 50, query: ${JSON.stringify(`user:${owner} author:Wladefant is:pr updated:>=${sinceIso.slice(0, 10)}`)}) {
      nodes { ... on PullRequest { number title url state mergedAt headRefName repository { nameWithOwner } } } }`);
  return `query { ${aliases.join("\n")} }`;
}

export function parsePullRequests(raw: unknown): WeekPullRequest[] {
  const out: WeekPullRequest[] = [];
  const data = raw && typeof raw === "object" && "data" in raw ? raw.data : null;
  if (!data || typeof data !== "object") return out;
  for (const group of Object.values(data)) {
    const nodes = group && typeof group === "object" && "nodes" in group && Array.isArray(group.nodes) ? group.nodes : [];
    for (const node of nodes) {
      if (!node || typeof node !== "object") continue;
      const n = Object.fromEntries(Object.entries(node));
      const repo = n.repository && typeof n.repository === "object" && "nameWithOwner" in n.repository ? String(n.repository.nameWithOwner) : "";
      if (typeof n.number !== "number" || !repo) continue;
      out.push({ repo, number: n.number, title: String(n.title ?? ""), url: String(n.url ?? ""), state: String(n.state ?? ""), mergedAt: typeof n.mergedAt === "string" ? n.mergedAt : null, branch: String(n.headRefName ?? "") });
    }
  }
  return out;
}

export class GithubReader {
  private last: GithubResult | null = null;
  private inflight: Promise<GithubResult> | null = null;
  private checkedAt = 0;

  constructor(private readonly deps: GithubReaderDeps, private readonly owners: string[], private readonly cacheMs = GITHUB_CACHE_MS) {}

  read(sinceMs: number): Promise<GithubResult> {
    // One refresh at a time: concurrent requests share it instead of each taking a quota reading.
    this.inflight ??= this.refresh(sinceMs).finally(() => { this.inflight = null; });
    return this.inflight;
  }

  private async refresh(sinceMs: number): Promise<GithubResult> {
    const now = this.deps.now();
    if (this.last && !this.last.stale && now - this.last.fetchedAt < this.cacheMs) return this.last;
    if (this.last?.stale && now - this.checkedAt < GITHUB_RETRY_MS) return this.last;
    this.checkedAt = now;
    const code = await this.deps.guard(GITHUB_QUERY_COST * this.owners.length);
    if (code !== 0) return this.stale(now, code === 75 ? "GitHub quota reserve reached; showing the last snapshot." : "GitHub guard unavailable; showing the last snapshot.");
    try {
      const raw = await this.deps.graphql(buildQuery(this.owners, new Date(sinceMs).toISOString()));
      this.last = { pullRequests: parsePullRequests(raw), fetchedAt: now, stale: false, staleReason: null };
      return this.last;
    } catch {
      return this.stale(now, "GitHub read failed; showing the last snapshot.");
    }
  }

  private stale(now: number, reason: string): GithubResult {
    const previous = this.last ?? { pullRequests: [], fetchedAt: 0, stale: true, staleReason: null };
    this.last = { ...previous, stale: true, staleReason: reason };
    return this.last;
  }
}

function run(file: string, args: string[], timeoutMs: number): Promise<{ code: number; stdout: string }> {
  const { promise, resolve } = Promise.withResolvers<{ code: number; stdout: string }>();
  execFile(file, args, { timeout: timeoutMs, windowsHide: true, maxBuffer: 8 * 1024 * 1024 }, (error, stdout) => {
    const code = error ? (typeof error.code === "number" ? error.code : 1) : 0;
    resolve({ code, stdout: String(stdout) });
  });
  return promise;
}

/** Production deps: the guard is the real bash script, graphql is `gh api graphql`. Both have timeouts. */
export function defaultGithubDeps(repoRoot: string, bash = process.env.WEEK_BASH ?? "bash"): GithubReaderDeps {
  const guardScript = path.join(repoRoot, "scripts", "super-board-gh-guard.sh").replace(/\\/g, "/");
  return {
    now: () => Date.now(),
    guard: async cost => (await run(bash, ["-c", `. "${guardScript}" && sb_gh_guard_begin_cycle; sb_gh_guard_check ${Math.max(1, Math.trunc(cost))}`], 60_000)).code,
    graphql: async query => {
      const { code, stdout } = await run("gh", ["api", "graphql", "-f", `query=${query}`], 60_000);
      if (code !== 0) throw new Error("gh api graphql failed");
      return JSON.parse(stdout);
    },
  };
}
