// superboard-merge-guard: refuse a PolySimulator staging merge that skipped the lane-brief
// bookends (Control Glass QA receipt, Feature Map note). Wladefant/super-board#227.
//
// Installed from Wladefant/super-board workflows/portable/extensions/ by
// install_github_native.py; edit it there, not here.
//
// The decision lives in ~/.veyyon/workflows/merge_guard.py. This hook only spots a merge
// cheaply and asks it: a bash command that mentions `gh` and `merge`, or the GitHub MCP
// merge tool. Everything else passes without spawning anything.
// Mode: env SUPERBOARD_MERGE_GUARD_MODE, else ~/.veyyon/run/merge-guard/mode, else enforce.
// @ts-nocheck

import os from "node:os";
import path from "node:path";

const GUARD = path.join(os.homedir(), ".veyyon", "workflows", "merge_guard.py");
const MERGE_HINT = /\bgh\b[\s\S]*\bmerge\b/;
const MCP_MERGE_TOOL = /^mcp__github\w*_merge_pull_request$/;
const TIMEOUT_MS = 120_000;
const OFF_SWITCH = "write 'off' to ~/.veyyon/run/merge-guard/mode (operator only)";

function guardArgs(event) {
  if (event.toolName === "bash") {
    const command = String(event.input?.command ?? "");
    return MERGE_HINT.test(command) ? ["check-command", "--command", command] : null;
  }
  if (MCP_MERGE_TOOL.test(event.toolName)) {
    const input = event.input ?? {};
    const pr = input.pullNumber ?? input.pull_number;
    // The MCP tool rejects a call without all three itself, so there is nothing to merge.
    if (!input.owner || !input.repo || !pr) return null;
    return ["check-pr", "--repo", `${input.owner}/${input.repo}`, "--pr", String(pr)];
  }
  return null;
}

export default function (pi) {
  pi.on("tool_call", async (event, ctx) => {
    if ((process.env.SUPERBOARD_MERGE_GUARD_MODE ?? "").trim().toLowerCase() === "off") return undefined;
    const args = guardArgs(event);
    if (!args) return undefined;
    const cwd = ctx?.cwd ?? process.cwd();
    let result;
    try {
      result = await pi.exec("python", ["-B", GUARD, ...args, "--cwd", cwd], { cwd, timeout: TIMEOUT_MS });
    } catch (error) {
      result = { code: 1, stdout: "", stderr: String(error) };
    }
    let decision;
    try {
      decision = JSON.parse(String(result.stdout).trim().split(/\r?\n/).pop() ?? "");
    } catch {
      return {
        block: true,
        reason:
          `Merge refused: the super-board merge guard could not run (exit ${result.code}: ` +
          `${String(result.stderr).trim().slice(0, 300)}). It fails closed. Emergency off-switch: ${OFF_SWITCH}.`,
      };
    }
    return decision.block ? { block: true, reason: decision.reason } : undefined;
  });
}
