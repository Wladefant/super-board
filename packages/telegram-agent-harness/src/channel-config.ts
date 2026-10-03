import * as fs from "node:fs";
import * as path from "node:path";

/** The leased channel's configuration, never a legacy project/default route. */
export function readMessageThreadId(stateDir: string): number | undefined {
  const file = path.join(stateDir, "access.json");
  if (!fs.existsSync(file)) return undefined;
  const config = JSON.parse(fs.readFileSync(file, "utf8")) as { message_thread_id?: unknown };
  const value = config.message_thread_id;
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) {
    throw new Error("The leased channel's message_thread_id must be a positive integer");
  }
  return value;
}
