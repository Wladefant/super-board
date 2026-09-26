/**
 * github-repo.ts — Which GitHub repository a session works in.
 *
 * Bare `#N` references in agent prose link to the session's own repository, so a
 * super-board session's "#224" does not point at PolySimulator. The repository is read
 * from the working directory's `origin` remote.
 */

/**
 * Resolves `owner/repo` from a GitHub remote URL (https, ssh or scp-like form).
 * Returns null for any other host, so a session outside GitHub links no bare `#N` at all.
 */
export function parseGithubRepo(remoteUrl: string): string | null {
  const match = remoteUrl.trim().match(/github\.com[:/]+([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?\/?$/i);
  return match ? `${match[1]}/${match[2]}` : null;
}

const repoByDir = new Map<string, string | undefined>();

/** `owner/repo` of `dir`'s origin remote, cached per directory; undefined outside a GitHub checkout. */
export function resolveGithubRepo(dir: string): string | undefined {
  if (repoByDir.has(dir)) return repoByDir.get(dir);
  let repo: string | undefined;
  try {
    const result = Bun.spawnSync(["git", "-C", dir, "remote", "get-url", "origin"], { stdout: "pipe", stderr: "ignore" });
    if (result.exitCode === 0) repo = parseGithubRepo(result.stdout.toString()) ?? undefined;
  } catch {
    // No git binary or an unreadable directory: the session has no repository, so a bare
    // `#N` in its prose stays unlinked rather than pointing at an unrelated project.
  }
  repoByDir.set(dir, repo);
  return repo;
}
