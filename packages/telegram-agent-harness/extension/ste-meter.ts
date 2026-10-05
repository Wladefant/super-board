import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

/**
 * Style meter for outbound Telegram text. It runs the deterministic STE checker
 * (`ste_check.py`, rules of the `ste-writing` skill) and appends one counts line
 * to a metrics file. It is a metric and a warning only: it never blocks, delays or
 * alters delivery. Every failure (no Python, no script, timeout) is swallowed.
 * Blocking on a low score is an open operator decision (see the STE parent issue).
 */

export const STE_METER_TIMEOUT_MS = 5000;

/** Argument vector for the checker. Exported so a test can pin the contract. */
export function steArgs(script: string, format: "html" | "md", source: string, metrics: string): string[] {
  return [script, "check", "--format", format, "--summary-json", "--metrics", metrics, "--source", source, "-"];
}

/**
 * Fire and forget. The promise resolves (never rejects) with the summary line, or
 * null when the meter did not run. Callers MUST NOT await it on the send path.
 */
export function meterOutbound(text: string, format: "html" | "md", source: string): Promise<string | null> {
  const { promise, resolve } = Promise.withResolvers<string | null>();
  try {
    const script = process.env.SUPER_BOARD_STE_CHECK || path.join(os.homedir(), ".veyyon", "workflows", "ste_check.py");
    if (process.env.SUPER_BOARD_STE_METER === "0" || !text.trim() || !fs.existsSync(script)) {
      resolve(null);
      return promise;
    }
    const metrics = process.env.SUPER_BOARD_STE_METRICS || path.join(os.homedir(), ".veyyon", "run", "ste-metrics.jsonl");
    const python = process.env.SUPER_BOARD_PYTHON || (process.platform === "win32" ? "python" : "python3");
    const child = spawn(python, steArgs(script, format, source, metrics), {
      stdio: ["pipe", "pipe", "ignore"],
      windowsHide: true,
    });
    let out = "";
    const timer = setTimeout(() => {
      child.kill();
      resolve(null);
    }, STE_METER_TIMEOUT_MS);
    child.stdout.on("data", chunk => {
      out += String(chunk);
    });
    child.on("error", () => {
      clearTimeout(timer);
      resolve(null);
    });
    child.on("close", code => {
      clearTimeout(timer);
      resolve(code === 0 ? out.trim() : null);
    });
    child.stdin.on("error", () => {});
    child.stdin.end(text);
  } catch {
    resolve(null);
  }
  return promise;
}
