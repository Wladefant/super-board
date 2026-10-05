#!/usr/bin/env node
// Measure what a repository's GitHub Actions runs cost and how long they take,
// per workflow and per job.
//
// Usage: node measure.mjs OWNER/REPO [--days 14] [--out jobs.json] [--budget 1500] [--time-limit 420] [--every K] [--cache DIR] [--concurrency 3]
// Needs: Node 18+ and the GitHub CLI (`gh auth login`) with read access to Actions.
// Source: github.com/enesgules/dotfiles (skills/optimise-github-actions) at d1e9b65f4dd4760e0f8b835eaeac510ca0918935,
// vetted: only `gh api` GET calls via execFile (no shell), writes only --out and the cache dir.
//
// Billing follows GitHub's rules for standard hosted runners: each job rounds up
// to a whole minute, Windows counts twice, macOS ten times, and skipped jobs are
// free. Jobs on any other runner group (self-hosted or larger runners) are
// listed apart, because they are either free or billed at their own rate.
import { execFile } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

const exec = promisify(execFile);
const args = process.argv.slice(2);
const option = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const repo = args.find((a, i) => a.includes("/") && !args[i - 1]?.startsWith("--"));
if (!repo) {
  console.error("Usage: node measure.mjs OWNER/REPO [--days 14] [--out jobs.json]");
  process.exit(2);
}
const days = Number(option("days", 14));
const outFile = option("out", "");

// Rate-limit budget (our adaptation). The gh token allows 5,000 core calls an hour and is
// shared with every lane, so the script spends at most --budget calls (default 1,500),
// refuses to start when fewer than budget + 500 remain, and caches finished runs on disk.
// A finished run never changes, so a rerun of the script only pays for new runs.
const budget = Number(option("budget", 1500));
const cacheDir = option("cache", join(homedir(), ".veyyon", "run", "oga-cache"));
const concurrency = Number(option("concurrency", 3));
let calls = 0;
let overBudget = false;
// gh calls can take ~5 s each here, so a command with a 600 s ceiling reads only a few hundred runs.
// --time-limit (seconds, default 420) stops reading jobs in time to print what was read; rerun the
// same command to continue from the cache until no "Partial data" note remains.
const deadline = Date.now() + Number(option("time-limit", 420)) * 1000;
mkdirSync(cacheDir, { recursive: true });

// `gh api --paginate --slurp` is missing from older gh builds, so page by hand: one call per
// page of 100, which also keeps the call count exact for the budget.
async function get(path) {
  // Retry transient network errors (TLS timeouts, 502/503) with a short backoff. Every attempt
  // counts against the budget.
  for (let attempt = 1; ; attempt++) {
    calls++;
    try {
      const { stdout } = await exec("gh", ["api", path], { maxBuffer: 1 << 28, timeout: 120000, windowsHide: true });
      return JSON.parse(stdout);
    } catch (err) {
      const transient = /timeout|TLS|EOF|reset|502|503|504/i.test(String(err.stderr ?? err.message));
      if (!transient || attempt >= 3) throw err;
      await new Promise((r) => setTimeout(r, 2000 * attempt));
    }
  }
}

async function list(path, key) {
  const items = [];
  for (let page = 1; ; page++) {
    const res = await get(`${path}${path.includes("?") ? "&" : "?"}per_page=100&page=${page}`);
    const got = res[key];
    items.push(...got);
    if (got.length < 100) return items;
  }
}

{
  const { stdout } = await exec("gh", ["api", "rate_limit", "--jq", ".resources.core.remaining"], { windowsHide: true });
  const remaining = Number(stdout.trim());
  if (remaining < budget + 500) {
    console.error(`Only ${remaining} GitHub API calls left this hour; need ${budget + 500}. Wait for the reset or pass a smaller --budget.`);
    process.exit(3);
  }
}

async function jobsOf(run, attempt) {
  const file = join(cacheDir, `${repo.replace("/", "__")}.${run.id}.${attempt}.json`);
  if (existsSync(file)) return JSON.parse(readFileSync(file, "utf8"));
  if (calls >= budget || Date.now() > deadline) {
    overBudget = true;
    return [];
  }
  const jobs = await list(`repos/${repo}/actions/runs/${run.id}/attempts/${attempt}/jobs`, "jobs");
  // Only a completed run is final; an in-progress run is re-read next time.
  if (run.status === "completed") writeFileSync(file, JSON.stringify(jobs));
  return jobs;
}

async function pool(items, size, fn) {
  const results = [];
  let next = 0;
  await Promise.all(
    Array.from({ length: size }, async () => {
      while (next < items.length) {
        const i = next++;
        results[i] = await fn(items[i]);
      }
    }),
  );
  return results;
}

const repoInfo = await get(`repos/${repo}`);

// One query per UTC day: a `created` filter returns at most 1,000 runs.
const dates = Array.from({ length: days }, (_, i) =>
  new Date(Date.now() - i * 864e5).toISOString().slice(0, 10),
);
const runs = (
  await pool(dates, concurrency, (date) => list(`repos/${repo}/actions/runs?created=${date}`, "workflow_runs"))
).flat();
console.error(`${runs.length} runs in ${days} days, ${calls} API calls so far.`);

// A busy repo has more runs than the call budget can read jobs for (polysimulator had 10,921 in
// 14 days). Read jobs for every Kth run (run id divisible by K, so the sample stays the same on a
// rerun and the cache keeps working) and say so in the output; run counts, wall-clock times and
// churn still come from the full run list. Pass --every K to pin K on a rerun.
const every = Number(
  option("every", Math.max(1, Math.ceil(runs.length / Math.max(1, Math.floor((budget - calls) * 0.9))))),
);
const sampled = every === 1 ? runs : runs.filter((r) => r.id % every === 0);
if (sampled.length < runs.length) {
  console.log(`> **Sampled:** jobs were read for ${sampled.length} of ${runs.length} runs (run id divisible by ${every}; pass \`--every ${every}\` to repeat it). Minute totals cover the sample only; multiply by about ${(runs.length / sampled.length).toFixed(1)} for the whole window.\n`);
}

console.log(`## Runs by workflow and event (all ${runs.length} runs, no job reads)\n`);
console.log("| Runs | Failed | Cancelled | Workflow / event |\n|---:|---:|---:|---|");
{
  const byKey = new Map();
  for (const r of runs) {
    const k = `${r.name} / ${r.event}`;
    const row = byKey.get(k) ?? { n: 0, failed: 0, cancelled: 0 };
    row.n++;
    if (r.conclusion === "failure") row.failed++;
    if (r.conclusion === "cancelled") row.cancelled++;
    byKey.set(k, row);
  }
  for (const [k, v] of [...byKey].sort((a, b) => b[1].n - a[1].n).slice(0, 25)) {
    console.log(`| ${v.n} | ${v.failed} | ${v.cancelled} | ${k} |`);
  }
  console.log();
}

let done = 0;
const jobs = (
  await pool(sampled, concurrency, async (run) => {
    // Earlier attempts are billed too, so read every attempt of a rerun.
    const attempts = [];
    for (let a = 1; a <= (run.run_attempt ?? 1); a++) attempts.push(await jobsOf(run, a));
    if (++done % 50 === 0) console.error(`  ${done}/${sampled.length} runs, ${calls} API calls`);
    return attempts
      .flat()
      .map((job) => ({
        run: run.id,
        workflow: run.name,
        event: run.event,
        branch: run.head_branch,
        job: job.name,
        conclusion: job.conclusion,
        labels: job.labels,
        runnerGroup: job.runner_group_name,
        startedAt: job.started_at,
        completedAt: job.completed_at,
      }));
  })
).flat();
console.error(`${calls} API calls used (budget ${budget}).`);
if (overBudget) {
  console.log(`> **Partial data:** the ${budget}-call budget or the time limit ran out before every run's jobs were read. Totals below undercount. Rerun the same command to continue (finished runs are cached in ${cacheDir}).\n`);
}
if (outFile) writeFileSync(outFile, JSON.stringify(jobs));

const minutes = (j) => (new Date(j.completedAt) - new Date(j.startedAt)) / 6e4;
const ran = jobs.filter(
  (j) => j.conclusion !== "skipped" && j.startedAt && j.completedAt && minutes(j) > 0,
);
const standard = (j) => !j.runnerGroup || j.runnerGroup === "GitHub Actions";
const multiplier = (j) =>
  j.labels.some((l) => /^macos/i.test(l)) ? 10 : j.labels.some((l) => /^windows/i.test(l)) ? 2 : 1;
const billed = (j) => (standard(j) ? Math.ceil(minutes(j)) * multiplier(j) : 0);
const sum = (list, f) => list.reduce((total, j) => total + f(j), 0);
const round = (n) => Math.round(n).toLocaleString("en-US");
const pct = (n, of) => `${((100 * n) / (of || 1)).toFixed(1)}%`;
const quantile = (sorted, q) => sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];

const hosted = ran.filter(standard);
const total = sum(hosted, billed);
const raw = sum(hosted, (j) => minutes(j) * multiplier(j));
const other = ran.filter((j) => !standard(j));

console.log(`# GitHub Actions usage: ${repo}, last ${days} days\n`);
if (!repoInfo.private) {
  console.log("> Public repository: standard hosted runners are free here. Read the minutes below as runner time, not cost.\n");
}
console.log(`- Runs: ${runs.length}; jobs that ran: ${ran.length}`);
console.log(`- Billed minutes (standard hosted runners, Linux-minute equivalents): **${round(total)}** (about ${round((total * 30) / days)} a month)`);
console.log(`- Rounding each job up to a whole minute adds ${round(total - raw)} (${pct(total - raw, total)})`);
console.log(`- Cancelled jobs: ${round(sum(hosted.filter((j) => j.conclusion === "cancelled"), billed))} billed minutes; failed jobs: ${round(sum(hosted.filter((j) => j.conclusion === "failure"), billed))}`);
if (other.length) {
  const groups = [...new Set(other.map((j) => j.runnerGroup))].join(", ");
  console.log(`- Other runner groups (${groups}): ${round(sum(other, minutes))} minutes, not in the total. Self-hosted minutes are free; larger runners bill at their own rate.`);
}

const runsOf = new Map();
for (const r of sampled) {
  const k = `${r.name} / ${r.event}`;
  runsOf.set(k, (runsOf.get(k) ?? 0) + 1);
}

// "Ran in" is the share of its workflow's runs (same event) that ran the job:
// near 100% on pull requests means its path filter matches almost everything.
function table(title, key, limit, ranIn) {
  const rows = new Map();
  for (const j of hosted) {
    const k = key(j);
    const row = rows.get(k) ?? { billed: 0, raw: 0, count: 0, runs: new Set(), of: `${j.workflow} / ${j.event}` };
    row.billed += billed(j);
    row.raw += minutes(j);
    row.count++;
    row.runs.add(j.run);
    rows.set(k, row);
  }
  console.log(`\n## ${title}\n`);
  console.log(ranIn
    ? "| Billed | Share | Jobs | Avg min | Ran in | Name |\n|---:|---:|---:|---:|---:|---|"
    : "| Billed | Share | Jobs | Avg min | Name |\n|---:|---:|---:|---:|---|");
  for (const [k, r] of [...rows].sort((a, b) => b[1].billed - a[1].billed).slice(0, limit)) {
    const share = ranIn ? ` ${pct(r.runs.size, runsOf.get(r.of))} |` : "";
    console.log(`| ${round(r.billed)} | ${pct(r.billed, total)} | ${r.count} | ${(r.raw / r.count).toFixed(1)} |${share} ${k} |`);
  }
}

table("By workflow and event", (j) => `${j.workflow} / ${j.event}`, 20, false);
table("Top jobs", (j) => `${j.workflow} / ${j.event} :: ${j.job.replace(/\s*\(.*\)$/, " (matrix)")}`, 30, true);

// Wall-clock time of successful runs, first attempt start to last update.
const durations = new Map();
for (const r of runs.filter((r) => r.conclusion === "success" && r.run_started_at)) {
  const k = `${r.name} / ${r.event}`;
  const list = durations.get(k) ?? [];
  list.push((new Date(r.updated_at) - new Date(r.run_started_at)) / 6e4);
  durations.set(k, list);
}
console.log("\n## Wall-clock time of successful runs\n");
console.log("| Runs | Median min | p90 min | Workflow / event |\n|---:|---:|---:|---|");
for (const [k, list] of [...durations].sort((a, b) => b[1].length - a[1].length).slice(0, 15)) {
  const sorted = list.sort((a, b) => a - b);
  console.log(`| ${sorted.length} | ${quantile(sorted, 0.5).toFixed(1)} | ${quantile(sorted, 0.9).toFixed(1)} | ${k} |`);
}

const prRuns = new Map();
for (const r of runs.filter((r) => r.event === "pull_request")) {
  const k = `${r.name} :: ${r.head_branch}`;
  prRuns.set(k, (prRuns.get(k) ?? 0) + 1);
}
if (prRuns.size) {
  const counts = [...prRuns.values()].sort((a, b) => a - b);
  console.log(`\n## Pull request churn\n\n- Runs per branch and workflow: median ${quantile(counts, 0.5)}, max ${counts.at(-1)} (${prRuns.size} branch/workflow pairs)`);
}
