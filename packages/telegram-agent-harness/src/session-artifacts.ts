import * as fs from "node:fs/promises";
import * as path from "node:path";

/** Search only this session's artifact directories, never the project or another session. */
export async function latestSessionPng(sessionFile: string): Promise<string | null> {
  const root = path.dirname(sessionFile);
  let latest: { file: string; time: number } | undefined;
  async function visit(directory: string, recursive: boolean): Promise<void> {
    let entries;
    try { entries = await fs.readdir(directory, { withFileTypes: true }); } catch { return; }
    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue;
      const file = path.join(directory, entry.name);
      if (entry.isDirectory() && recursive) await visit(file, true);
      else if (entry.isFile() && /\.png$/i.test(entry.name)) {
        try {
          const stat = await fs.stat(file);
          if (!latest || stat.mtimeMs > latest.time) latest = { file, time: stat.mtimeMs };
        } catch { /* Artifact may disappear while a worker replaces it. */ }
      }
    }
  }
  await visit(root, false);
  await visit(path.join(root, "local"), true);
  return latest?.file ?? null;
}
