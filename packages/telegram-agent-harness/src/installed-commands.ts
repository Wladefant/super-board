import { HerdrAdapter } from "./herdr-adapter";
import { parseVeyyonUsage } from "./veyyon-adapter";
import { escapeHtml, renderAgentList, renderUsage } from "./telegram-router";
import type { AgentSession, CommandRunner } from "./contract";

export interface InstalledSession {
  id: string;
  cwd: string;
  idle: boolean;
  /** Only a live host registry may supply workers; journal files are not heartbeats. */
  agents?: AgentSession[];
}
export interface InstalledCommandPort {
  session(): InstalledSession;
  send(text: string): Promise<void>;
  photo(file: string, caption: string): Promise<void>;
  latestPng(sessionId: string): Promise<string | null>;
  inbound(text: string, idle: boolean): Promise<void>;
}

/** Called after the installed poller's actor/reply gates, never owns a lease or offset. */
export async function handleInstalledCommand(text: string, port: InstalledCommandPort, runner: CommandRunner): Promise<boolean> {
  const match = /^\/(agents|prompt|shot|usage)(?:@\w+)?(?:\s|$)/.exec(text.trim());
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
        await port.send("<b>Checking Herdr target.</b>");
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
