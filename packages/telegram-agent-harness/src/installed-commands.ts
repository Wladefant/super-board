import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { HerdrAdapter } from "./herdr-adapter";
import { parseVeyyonUsage } from "./veyyon-adapter";
import { escapeHtml, renderAgentList, renderUsage } from "./telegram-router";
import type { AgentSession, CommandRunner, UsageLimit } from "./contract";

export interface InstalledSession {
  id: string;
  cwd: string;
  idle: boolean;
  model?: string;
  /** Only a live host registry may supply workers; journal files are not heartbeats. */
  agents?: AgentSession[];
  poolDbPath?: string;
  decisionsPath?: string;
}
export interface InstalledCommandPort {
  session(): InstalledSession;
  send(text: string): Promise<void>;
  photo(file: string, caption: string): Promise<void>;
  mediaGroup?(files: string[], caption?: string): Promise<void>;
  latestPng(sessionId: string): Promise<string | null>;
  inbound(text: string, idle: boolean): Promise<void>;
}

export interface OutboundCardSummary {
  messageId: number;
  requestId?: string | null;
  decisionId?: string | null;
  createdAt: number;
}

export function readRecentOutboundCards(dbPath?: string, limit = 5): OutboundCardSummary[] {
  const resolved = dbPath ?? path.join(os.homedir(), ".veyyon", "telegram", "bot_pool.db");
  if (!fs.existsSync(resolved)) return [];
  try {
    const db = new Database(resolved, { readonly: true });
    try {
      const rows = db.query(
        "SELECT message_id, request_id, decision_id, created_at FROM message_correlations ORDER BY created_at DESC LIMIT ?"
      ).all(limit) as Array<{
        message_id: number;
        request_id: string | null;
        decision_id: string | null;
        created_at: number;
      }>;
      return rows.map(r => ({
        messageId: r.message_id,
        requestId: r.request_id,
        decisionId: r.decision_id,
        createdAt: r.created_at,
      }));
    } finally {
      db.close();
    }
  } catch {
    return [];
  }
}

export interface PendingDecisionSummary {
  decisionId: string;
  requestId?: string | null;
  issueNumber?: number | null;
  issueUrl?: string | null;
  question: string;
}

export function readPendingDecisions(filePath?: string): PendingDecisionSummary[] {
  const resolved = filePath ?? path.join(os.homedir(), ".veyyon", "workflows", "decisions.json");
  if (!fs.existsSync(resolved)) return [];
  try {
    const content = fs.readFileSync(resolved, "utf8");
    const parsed = JSON.parse(content);
    const rawMap = parsed?.decisions;
    if (!rawMap || typeof rawMap !== "object") return [];
    const list: PendingDecisionSummary[] = [];
    for (const val of Object.values(rawMap)) {
      if (val && typeof val === "object") {
        const item = val as Record<string, unknown>;
        if (item.status === "pending" || item.status === "open") {
          list.push({
            decisionId: String(item.decision_id ?? ""),
            requestId: typeof item.request_id === "string" ? item.request_id : null,
            issueNumber: typeof item.issue_number === "number" ? item.issue_number : null,
            issueUrl: typeof item.issue_url === "string" ? item.issue_url : null,
            question: String(item.question ?? ""),
          });
        }
      }
    }
    return list;
  } catch {
    return [];
  }
}

export function renderFullStatus(params: {
  session: InstalledSession;
  agents: AgentSession[];
  decisions: PendingDecisionSummary[];
  outboundCards: OutboundCardSummary[];
  usageLimits: UsageLimit[];
  now?: number;
}): string {
  const now = params.now ?? Date.now();
  const session = params.session;
  const lines: string[] = [
    "📊 <b>Veyyon Session Status</b>",
    "",
    `• <b>Session ID:</b> <code>${escapeHtml(session.id)}</code>`,
    `• <b>Model:</b> <code>${escapeHtml(session.model ?? "default")}</code>`,
    `• <b>State:</b> <b>${session.idle ? "Idle" : "Running / Streaming"}</b>`,
    `• <b>Directory:</b> <code>${escapeHtml(session.cwd)}</code>`,
    "",
  ];

  const workingCount = params.agents.filter(a => a.state === "working" || a.state === "streaming").length;
  lines.push(`👥 <b>Active Agents (${workingCount} working / ${params.agents.length} observed):</b>`);
  if (params.agents.length === 0) {
    lines.push("• <i>No agents observed.</i>");
  } else {
    for (const agent of params.agents) {
      const stateLabel = agent.state === "working" ? "<b>working</b>" : agent.state === "idle" ? "idle" : escapeHtml(agent.state);
      lines.push(`• <code>${escapeHtml(`${agent.backend}:${agent.id}`)}</code> — ${escapeHtml(agent.name)} (${stateLabel})`);
    }
  }
  lines.push("");

  lines.push(`❓ <b>Open Operator Decisions (${params.decisions.length} pending):</b>`);
  if (params.decisions.length === 0) {
    lines.push("• <i>None pending.</i>");
  } else {
    for (const dec of params.decisions) {
      const issueLink = dec.issueUrl
        ? ` (<a href="${escapeHtml(dec.issueUrl)}">#${dec.issueNumber ?? "issue"}</a>)`
        : dec.issueNumber ? ` (#${dec.issueNumber})` : "";
      const qText = dec.question.length > 80 ? `${dec.question.slice(0, 77)}...` : dec.question;
      lines.push(`• <b>${escapeHtml(dec.decisionId)}</b>${issueLink}: ${escapeHtml(qText)}`);
    }
  }
  lines.push("");

  lines.push("📤 <b>Recent Outbound Cards:</b>");
  if (params.outboundCards.length === 0) {
    lines.push("• <i>None recorded.</i>");
  } else {
    for (const card of params.outboundCards) {
      const ageSeconds = Math.max(0, Math.round(now / 1000 - card.createdAt));
      const ageStr = ageSeconds < 60 ? `${ageSeconds}s ago` : ageSeconds < 3600 ? `${Math.round(ageSeconds / 60)}m ago` : `${Math.round(ageSeconds / 3600)}h ago`;
      const topic = card.decisionId ? `decision: ${card.decisionId}` : (card.requestId ? card.requestId : "general");
      lines.push(`• #${card.messageId} · <code>${escapeHtml(topic)}</code> (${ageStr})`);
    }
  }
  lines.push("");

  lines.push("⚡ <b>Resource &amp; Quota Usage:</b>");
  if (params.usageLimits.length === 0) {
    lines.push("• <i>Usage is currently unavailable.</i>");
  } else {
    for (const limit of params.usageLimits) {
      lines.push(`• <b>${escapeHtml(limit.provider)}</b> · ${escapeHtml(limit.label)}: ${Math.round(limit.remainingPercent)}% remaining`);
    }
  }

  return lines.join("\n");
}

/** Called after the installed poller's actor/reply gates, never owns a lease or offset. */
export async function handleInstalledCommand(text: string, port: InstalledCommandPort, runner: CommandRunner): Promise<boolean> {
  const match = /^\/(agents|prompt|shot|usage|status)(?:@\w+)?(?:\s|$)/.exec(text.trim());
  if (!match) return false;
  const raw = text.trim().replace(/^(\/\w+)@\w+/, "$1");
  const session = { ...port.session() };
  const herdr = new HerdrAdapter(runner);
  try {
    if (match[1] === "agents") {
      const agents = await herdr.listSessions();
      const now = Date.now();
      agents.push({ backend: "veyyon", id: session.id, name: "Current session", state: session.idle ? "idle" : "working", project: session.cwd, observedAt: now, updatedAt: now, canPrompt: true });
      agents.push(...(session.agents ?? []));
      await port.send(renderAgentList(agents) + (session.agents ? "" : "\n<i>Worker registry is not exposed by this extension host.</i>") + "\n<code>/prompt backend:id instruction</code>\n<code>/shot backend:id</code>");
    } else if (match[1] === "usage") {
      const result = await runner.run(["veyyon", "usage", "--json"]);
      const usage = parseVeyyonUsage(result.exitCode === 0 ? result.stdout : "{}");
      const spark = usage.limits.some(limit => /spark/i.test(`${limit.provider} ${limit.label}`));
      await port.send(renderUsage(usage.limits, usage.observedAt) + (spark ? "" : "\n• <b>Codex Spark:</b> allowance unavailable (not assumed unused)."));
    } else if (match[1] === "status") {
      const agents = await herdr.listSessions();
      const now = Date.now();
      agents.push({
        backend: "veyyon",
        id: session.id,
        name: "Current session",
        state: session.idle ? "idle" : "working",
        project: session.cwd,
        observedAt: now,
        updatedAt: now,
        canPrompt: true,
      });
      agents.push(...(session.agents ?? []));

      const decisions = readPendingDecisions(session.decisionsPath);
      const outboundCards = readRecentOutboundCards(session.poolDbPath, 5);

      let usageLimits: UsageLimit[] = [];
      try {
        const result = await runner.run(["veyyon", "usage", "--json"]);
        if (result.exitCode === 0) {
          const usage = parseVeyyonUsage(result.stdout);
          usageLimits = usage.limits;
        }
      } catch {}

      const html = renderFullStatus({
        session,
        agents,
        decisions,
        outboundCards,
        usageLimits,
        now,
      });
      await port.send(html);
    } else if (match[1] === "prompt") {
      const prompt = /^\/prompt\s+(\S+)\s+([\s\S]+)$/.exec(raw);
      if (!prompt) { await port.send("<b>Usage:</b> <code>/prompt backend:id instruction</code>"); return true; }
      const [, target, message] = prompt;
      if (target === session.id || target === `veyyon:${session.id}`) {
        await port.send(session.idle ? "<b>Starting turn.</b>" : "<b>Steering active turn.</b>");
        // Recheck binding after network I/O; never inject into a switched session.
        if (port.session().id !== session.id) { await port.send("Session changed; nothing was sent. Refresh <code>/agents</code>."); return true; }
        await port.inbound(message, port.session().idle);
      } else if (target.startsWith("herdr:")) {
        const result = await herdr.prompt(target.slice(6), message);
        await port.send(`<b>${result.ok ? "Prompt delivered" : "Prompt not sent"}.</b> ${escapeHtml(result.detail)}`);
      } else {
        await port.send("<b>Target unavailable.</b> Copy the full backend:id from <code>/agents</code>. Worker prompts are not redirected to Main.");
      }
    } else {
      const shot = /^\/shot\s+(\S+)$/.exec(raw);
      if (!shot) { await port.send("<b>Usage:</b> <code>/shot backend:id</code>"); return true; }
      if (shot[1] !== session.id && shot[1] !== `veyyon:${session.id}`) {
        await port.send("<b>No screenshot available.</b> This target exposes no session PNG artifacts."); return true;
      }
      const file = await port.latestPng(session.id);
      if (!file) { await port.send("<b>No screenshot available.</b> No PNG artifact was recorded for this session."); return true; }
      await port.photo(file, `<b>Latest session screenshot</b> · <code>veyyon:${escapeHtml(session.id)}</code>`);
    }
  } catch {
    await port.send("<b>Command unavailable.</b> The backend or delivery failed; no action was retried.");
  }
  return true;
}
