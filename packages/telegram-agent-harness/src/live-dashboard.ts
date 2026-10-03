import * as os from "node:os";
import { escapeHtml, renderUsage } from "./telegram-router";
import { parseVeyyonUsage } from "./veyyon-adapter";
import type { CommandRunner } from "./contract";
import type { TelegramPoller } from "../extension/poller";

export interface DashboardSnapshot {
  observedAt: number;
  lanes: Array<{ name: string; task: string; state: "active" | "blocked" | "exited" }>;
  blockers: Array<{ question: string; url?: string }>;
  mergeQueue: Array<{ title: string; url: string; state: string }>;
}

export function renderDashboard(snapshot: DashboardSnapshot | null, usage: string, now = Date.now()): string {
  const stale = !snapshot || now - snapshot.observedAt > 300_000;
  const lines = ["<b>Fleet dashboard</b>",
    `<i>Refreshed ${new Date(now).toISOString()} · lane state ${stale ? "stale/unavailable" : "reported by Main"}</i>`,
    "", "<b>Active lanes</b>"];
  // A snapshot handed over as deserialized JSON may omit a key entirely, so every list is
  // defended once here and its length read from the defended value, never from the snapshot.
  const active = (snapshot?.lanes ?? []).filter(lane => lane.state !== "exited");
  const blockers = snapshot?.blockers ?? [];
  const mergeQueue = snapshot?.mergeQueue ?? [];
  lines.push(...active.slice(0, 6).map(lane => `• <b>${escapeHtml(lane.name.slice(0, 40))}</b> · ${stale ? "last reported" : lane.state}: ${escapeHtml(lane.task.slice(0, 100))}`));
  if (!active.length) lines.push("Worker registry unavailable; no active lanes asserted.");
  if (active.length > 6) lines.push(`Plus ${active.length - 6} lanes in the full report.`);
  lines.push("", "<b>Awaiting your answer</b>");
  lines.push(...blockers.slice(0, 3).map(blocker => `• ${escapeHtml(blocker.question.slice(0, 140))}`));
  if (!blockers.length) lines.push(snapshot ? "No blockers reported." : "Blocker state unavailable.");
  lines.push("", "<b>Merge queue</b>");
  for (const entry of mergeQueue.slice(0, 3)) {
    const title = escapeHtml(entry.title.slice(0, 80));
    const label = /^https:\/\/github\.com\/[^/]+\/[^/]+\/pull\/\d+$/.test(entry.url)
      ? `<a href="${escapeHtml(entry.url)}">${title}</a>` : title;
    lines.push(`• ${label} · ${escapeHtml(entry.state.slice(0, 80))}`);
  }
  if (!mergeQueue.length) lines.push(snapshot ? "No pull requests queued by Main." : "Merge queue unavailable.");
  lines.push("", `<b>Host memory:</b> ${(100 * (1 - os.freemem() / os.totalmem())).toFixed(1)}% used`, "", usage);
  return lines.join("\n");
}

/** One durable message id; updates are coalesced and sent by the existing leased poller. */
export class LiveDashboard {
  private timer: Timer | undefined;
  private active: Promise<void> | undefined;
  private usage = "<b>Allowance windows:</b> unavailable\n• Codex Spark: unavailable (not assumed unused).";
  private usageAt = 0;
  constructor(private readonly poller: TelegramPoller, private readonly runner: CommandRunner,
    private readonly sessionId: () => string, private readonly report: (message: string) => void) {}
  set(snapshot: DashboardSnapshot): void {
    this.poller.setMeta(`dashboard-snapshot:${this.sessionId()}`, JSON.stringify(snapshot));
  }
  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => {
      this.active ??= this.refresh().catch(error => this.report(String(error))).finally(() => { this.active = undefined; });
    }, 30_000);
    this.timer.unref?.();
  }
  stop(): void { clearInterval(this.timer); this.timer = undefined; }
  async refresh(): Promise<void> {
    const session = this.sessionId();
    const chat = this.poller.getPrimaryChatId();
    if (!chat) return;
    if (Date.now() - this.usageAt >= 60_000) {
      try {
        const result = await this.runner.run(["veyyon", "usage", "--json"]);
        const parsed = parseVeyyonUsage(result.exitCode === 0 ? result.stdout : "{}");
        this.usage = renderUsage(parsed.limits, parsed.observedAt);
        if (!parsed.limits.some(limit => /spark/i.test(`${limit.provider} ${limit.label}`))) {
          this.usage += "\n• <b>Codex Spark:</b> unavailable (not assumed unused).";
        }
      } catch { this.usage = "<b>Allowance windows:</b> unavailable\n• Codex Spark: unavailable."; }
      this.usageAt = Date.now();
    }
    if (session !== this.sessionId()) return;
    const raw = this.poller.getMeta(`dashboard-snapshot:${session}`);
    const snapshot = raw ? JSON.parse(raw) as DashboardSnapshot : null;
    await this.poller.updateDashboard(chat, renderDashboard(snapshot, this.usage));
  }
}
