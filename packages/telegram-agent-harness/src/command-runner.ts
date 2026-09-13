import type { CommandRunner, CommandResult } from "./contract";

export class BunCommandRunner implements CommandRunner {
  async run(argv: readonly string[]): Promise<CommandResult> {
    if (argv.length === 0) throw new Error("Command argv must not be empty");
    let process;
    try {
      process = Bun.spawn([...argv], {
        stdin: "ignore",
        stdout: "pipe",
        stderr: "pipe",
        windowsHide: true,
      });
    } catch {
      return { exitCode: 127, stdout: "", stderr: "Command unavailable" };
    }
    const [exitCode, stdout, stderr] = await Promise.all([
      process.exited,
      new Response(process.stdout).text(),
      new Response(process.stderr).text(),
    ]);
    return { exitCode, stdout, stderr };
  }
}
