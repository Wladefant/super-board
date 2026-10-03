# Herdr 0.9.0 / Veyyon integration research

## Sources inspected

- Herdr repository: `https://github.com/herdrdev/herdr`, tag `v0.9.0` and current `master`.
- Published documentation: `https://herdr.dev/docs/integrations/`, `https://herdr.dev/docs/agents/`, `https://herdr.dev/docs/agent-automation/`, `https://herdr.dev/plugins/`, plus the socket API and plugin authoring pages in the repository.
- Veyyon fork: `C:/Users/wkiri/development/veyyon`.
- Installed Herdr 0.9.0: `C:/Users/wkiri/.herdr/packages/standalone/releases/0.9.0-x86_64-pc-windows-msvc/herdr.exe`.
- Live pane `w1:p1` and the running `veyyon.exe` process tree on this machine.

## How Herdr detects Pi and OMP

### Process identity

Herdr owns a closed `detect::Agent` enum and maps foreground-process evidence to it in `src/detect/mod.rs`.

- Pi is recognized from the canonical process/binary name `pi`.
- OMP is recognized from `omp`.
- Names are case-normalized and executable/script suffixes `.exe`, `.cmd`, `.bat`, `.ps1`, and `.js` are stripped. Herdr also inspects `argv[0]`, command lines, common runtime wrappers (Node/Bun/Python/shell/PowerShell/cmd), and known package paths. Pi has a package-path special case for `node_modules/@earendil-works/pi-coding-agent/.../cli.js`.
- Process recognition itself does not depend on an environment marker. Environment variables are used by lifecycle integrations after the process is running in a Herdr pane.

### Screen state detection

Screen detection uses declarative TOML manifests under `src/detect/manifests/` and the remotely published mirror under `distribution/agent-detection/`. Each manifest has `id`, `version`, `min_engine_version`, `updated_at`, optional aliases, and ordered `[[rules]]`. Rules select a region such as `whole_recent` or `bottom_non_empty_lines(N)`, declare `idle` / `working` / `blocked` / `unknown`, priority, evidence gates (`contains`, `regex`, `line_regex`, nested `all` / `any` / `not`), and visibility authority flags.

Pi's v0.9.0 fallback manifest is intentionally tiny: agent id `pi`, alias `herdr:pi`, and a single working rule matching the literal `Working...`. OMP has no bundled screen manifest because its installed lifecycle integration is the state authority.

The detector reads the live bottom buffer, not the user's scrolled viewport. Local manifest overrides can be hot-reloaded without a server restart, but a new executable kind still needs a core `Agent` variant so the process can be associated with a manifest.

### Pi lifecycle extension

`herdr integration install pi` writes `herdr-agent-state.ts` to `~/.pi/agent/extensions/` or `$PI_CODING_AGENT_DIR/extensions/`. The extension activates only when all of these are present:

- `HERDR_ENV=1`
- `HERDR_SOCKET_PATH`
- `HERDR_PANE_ID`

It opens the local Herdr socket directly and sends newline-delimited JSON requests. It reports session identity with `pane.report_agent_session` and semantic state with `pane.report_agent`, source `herdr:pi`, agent `pi`, and monotonically increasing `seq` values. Event mapping:

- `session_start` in TUI mode: report session, then current idle/working state.
- `agent_start`: `working`.
- `agent_settled` when `ctx.isIdle()` is true: `idle`.
- custom `herdr:blocked` active/inactive events: counted `blocked` authority with an optional label, then return to the underlying working/idle state.

Pi session identity comes from the session manager's session file or id. Herdr can restore it with `pi --session <value>`.

### OMP lifecycle extension

`herdr integration install omp` writes `herdr-omp-agent-state.ts` under OMP's agent extensions directory. It uses the same socket protocol, source `herdr:omp`, and agent label `omp`, but covers more inherited UI behavior:

- `session_start` / `session_switch` and session-path/id reporting.
- `agent_start` -> `working`; `agent_end` -> debounced `idle`.
- `agent_end.willContinue=true` remains `working` so scheduled continuations do not terminate `agent wait` early.
- retryable provider/network errors hold `working` briefly, then become `blocked` if no retry starts.
- `tool_approval_requested` / `tool_approval_resolved` map to blocked/unblocked.
- `tool_execution_start` / `tool_execution_end` for tool `ask` map the question to `blocked` and back.
- custom counted `herdr:blocked` events have highest state precedence.

Herdr restores OMP sessions with `omp --resume=<value>`.

## Agent automation and socket API

The automation surface is generic once Herdr recognizes the pane as an agent:

- CLI: `herdr agent start <name> --kind <kind> --pane <id>`, `herdr agent prompt <target> <text> [--wait --until ... --timeout ...]`, and `herdr agent wait <target> [--until ... --timeout ...]`.
- Raw methods: `agent.start`, `agent.prompt`, and `agent.wait`.
- `agent.start` parameters: `{name, kind, pane_id, args, timeout_ms}`.
- `agent.prompt` parameters: `{target, text, wait?: {until, timeout_ms}}`.
- `agent.wait` parameters: `{target, until, timeout_ms}`.

`agent.wait` is server-owned and event-driven and pins the resolved pane occupant. `agent.prompt` can atomically submit and wait; it refuses an already blocked agent without sending input, and requires a newly submitted prompt from a non-working state to show working/blocked activity before accepting a settled result.

Integrations can report state/session directly with `pane.report_agent`, `pane.report_agent_session`, and `pane.release_agent`. Pane processes inherit `HERDR_ENV`, `HERDR_PANE_ID`, `HERDR_TAB_ID`, `HERDR_WORKSPACE_ID`, `HERDR_BIN_PATH`, and `HERDR_SOCKET_PATH`.

## Plugin format and why this is not a marketplace plugin

Herdr community plugins are executable workflow packages, not agent detector definitions. A public marketplace repository needs the GitHub topic `herdr-plugin` and one or more `herdr-plugin.toml` files. Required top-level fields are `id`, `name`, `version`, and `min_herdr_version`; optional metadata includes `description` and `platforms`. Manifests may declare `[[build]]`, `[[startup]]`, `[[actions]]`, `[[events]]`, `[[panes]]`, and `[[link_handlers]]` argv commands.

Plugins can call the complete Herdr CLI/socket API, but plugin v1 cannot add a core process kind or bind a new executable name to a screen detection manifest. A plugin therefore cannot make the installed 0.9.0 detector recognize `veyyon.exe` by itself. The documented complete path is a Herdr core agent integration, matching where Pi/OMP live.

## What Veyyon already exposes

Veyyon is an OMP/Pi descendant and already exposes the lifecycle surface the OMP integration expects:

- extension loading from the active profile's `<agentDir>/extensions` directory;
- `session_start`, `session_switch`, `session_shutdown`, `agent_start`, and `agent_end` events;
- `tool_execution_start` / `tool_execution_end` with tool name and ask arguments;
- `tool_approval_requested` / `tool_approval_resolved`;
- custom extension events, including `herdr:blocked` emitted by inherited/cooperating extensions;
- session manager id/path access and `--resume <session>` CLI support.

Its directory model is profile-aware: `VEYYON_CODING_AGENT_DIR` overrides the complete agent directory; otherwise `VEYYON_CONFIG_DIR` selects the config root and `VEYYON_PROFILE` selects `<config>/profiles/<profile>/agent` (default profile `default`). No Veyyon-side hook is necessary; Herdr can install a normal Veyyon extension.

The live Veyyon TUI provides stable bottom-buffer evidence not present in Pi's minimal fallback:

- active turn: `escape interrupt` / `esc interrupt` / `ctrl+c interrupt` footer;
- job polling: a line shaped like `▏ i waiting on 8 jobs`;
- ask dialog: title `Ask` plus footer controls such as `enter select`, `enter toggle`, or `enter submit` and `esc cancel`;
- other input/approval dialogs: `enter confirm`/`enter select`/`enter submit` plus `esc cancel`;
- idle composer: a bottom prompt marker beginning with `›`, with no active interrupt footer or cancel dialog.

## Gaps before the change

1. No `Veyyon` agent variant, `veyyon`/`veyyon.exe` process mapping, canonical executable, or `agent start --kind veyyon` support.
2. No Veyyon screen manifest, so the live `escape interrupt`, job-poll, ask/input modal, and idle-composer states were not classified.
3. No `herdr integration install veyyon`, profile-aware extension target, `herdr:veyyon` lifecycle authority, or session-restore plan.
4. Consequently the installed Herdr 0.9.0 live snapshot showed pane `w1:p1` with `agent_status: unknown` and `agents: []` even though its foreground descendant was `C:/Users/wkiri/AppData/Local/veyyon/veyyon.exe --extension ... --resume ...` and it inherited all Herdr pane markers.

## Implemented shape

The Herdr PR adds a first-class `Veyyon` agent kind, Windows `.exe` recognition through the existing normalization path, a bundled/published screen manifest, a Veyyon lifecycle extension derived from the OMP integration, profile-aware install/uninstall, `veyyon --resume <session>` restore, generic `agent start/prompt/wait` participation, focused Rust/Bun tests, and matching English/Japanese/Chinese draft documentation. No Veyyon repository change is required.
