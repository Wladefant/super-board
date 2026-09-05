#!/usr/bin/env python3
"""
worker_backend.py - Real, configured agent-CLI worker execution for the portable workflow core.

WHY THIS EXISTS
---------------
The portable coordinator could previously only *simulate* worker execution. Its
dispatch path fell through to a labelled fixture whenever a real command was not
wired up, so a step that never ran anything still reported "simulated successful
build" and the gate advanced the request. Exit code 0 and the phrase
"proven absent" were, between them, sufficient to advance a request through
build -> QA -> review. That is the hole this module closes.

WHAT IT DOES
------------
Takes a structured request/dispatch packet, builds a *configured* argv vector
(never a shell string, never a shell=True invocation), runs exactly one real
agent CLI, and returns a structured, head-bound evidence record.

It is a backend, not a scheduler. It runs one stage for one request and returns.
The existing SuperboardExecutionAdapter.run_step remains the only step engine;
this module is what that engine dispatches *into*.

FAIL-CLOSED CONTRACT
--------------------
Every one of the following produces ok=False with a populated blocked_reason,
and never a synthetic success:

  1.  Malformed request (no stage, no request_id, no usable repo_root).
  2.  Unknown backend name, or a backend with no configured argv.
  3.  Backend executable absent from PATH.
  4.  Non-zero exit status from the agent CLI.
  5.  Timeout.
  6.  No structured result emitted, or a result that is not a JSON object.
  7.  Structured result missing any required field.
  8.  A verdict outside the allowed vocabulary.
  9.  verdict == "pass" with no executed check carrying a command and an
      exit code. Exit 0 from the agent alone is NEVER evidence.
 10.  A declared artifact that does not exist on disk.
 11.  Observed git HEAD disagreeing with the requested head, or with the head
      the agent claims. The reported head_sha is always the *observed* one.
 12.  A build stage that produced neither a commit nor an artifact.
 13.  A bug-QA reproduction claim that is a bare boolean or a keyword rather
      than a re-executed scenario with a command, exit code and observation.

BACKENDS
--------
Three real backends are configured out of the box, each using flags taken from
the installed CLI's own help output:

  claude   claude -p <prompt> --output-format json --json-schema <inline schema>
           Structured result arrives on stdout under "structured_output".
  codex    codex exec -m <model> -C <repo> --output-schema <file> -o <file>
           Structured result is written to the last-message file.
  veyyon   veyyon -p --mode=json --model <model> <prompt>
           Optional. Veyyon is one backend among several, never a dependency of
           the core: this module imports nothing from Veyyon and works with it
           absent.

Any other harness is reachable without touching this file, by declaring a
custom backend in user config. See BACKEND CONFIGURATION below.

BACKEND CONFIGURATION
---------------------
Resolution order, first hit wins:

  1. Explicit dict or JSON path passed to WorkerBackend(config=...).
  2. Environment variable PORTABLE_WORKER_CONFIG (path to JSON).
  3. <state_dir>/worker_backends.json.
  4. project_config.metadata["portable_worker"], when a ProjectConfig-like
     object is supplied.
  5. The built-in defaults in DEFAULT_BACKENDS.

A user config is a JSON object shaped like:

  {
    "default_backend": "my-harness",
    "stage_backends": {"review": "codex"},
    "backends": {
      "my-harness": {
        "argv": ["my-agent", "run", "--model", "{model}", "--schema",
                 "{schema_path}", "--out", "{result_path}", "{prompt}"],
        "result_source": "file",
        "result_path_template": "{work_dir}/result.json",
        "timeout_seconds": 900,
        "env": {"MY_AGENT_QUIET": "1"}
      }
    }
  }

Every argv element is substituted as a whole token against the placeholder set
{prompt} {model} {agent_role} {stage} {request_id} {head_sha} {repo_root}
{schema_path} {schema_inline} {result_path} {work_dir} {permission_mode}
{allowed_tools} {sandbox_mode} {issue_url} {pr_url}. Substitution never
splits or re-parses a token, so a prompt containing spaces, quotes or newlines
travels as exactly one argv entry.

BOUNDARIES
----------
This module never merges, never pushes, never deploys, and never resolves a
human authorization gate. It runs one command and reports what happened.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

VALID_STAGES = ("build", "qa", "review")
VALID_VERDICTS = ("pass", "fail", "blocked")

#: Stages where the tested tree must not move underneath the worker. QA and
#: review evidence is bound to one commit; a head that advances mid-run
#: invalidates the result rather than producing a passing one.
IMMUTABLE_HEAD_STAGES = ("qa", "review")

MAX_CAPTURED_OUTPUT = 20000
#: Upper bound on "{" positions tried when recovering a result from noisy
#: output. A real envelope appears within a handful; the cap stops adversarial
#: or runaway output from turning recovery into a quadratic scan.
MAX_JSON_SCAN_ATTEMPTS = 4096
DEFAULT_TIMEOUT_SECONDS = 1800


# ---------------------------------------------------------------------------
# Structured agent result schema
# ---------------------------------------------------------------------------

def agent_result_schema() -> Dict[str, Any]:
    """
    JSON Schema handed to the agent CLI so its final answer is machine-readable.

    This is passed to `claude --json-schema` and written out for
    `codex exec --output-schema`, both of which are documented flags of the
    installed CLIs. The same shape is required of a custom harness.
    """
    check = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "command": {"type": "array", "items": {"type": "string"}},
            "exit_code": {"type": "integer"},
            "observed": {"type": "string"},
        },
        "required": ["name", "command", "exit_code", "observed"],
    }
    return {
        "type": "object",
        "properties": {
            "stage": {"type": "string", "enum": list(VALID_STAGES)},
            "request_id": {"type": "string"},
            "head_sha": {
                "type": "string",
                "description": "Full 40-character git HEAD of the tree you worked in, read with `git rev-parse HEAD`. Never invent it.",
            },
            "verdict": {"type": "string", "enum": list(VALID_VERDICTS)},
            "summary": {"type": "string"},
            "checks": {
                "type": "array",
                "description": "Commands you actually executed, with their real exit codes and observed output. A pass verdict with no checks is rejected.",
                "items": check,
            },
            "artifacts": {
                "type": "array",
                "description": "Files you created or changed, as paths relative to the repository root. They must exist on disk.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "role": {"type": "string"},
                    },
                    "required": ["path", "role"],
                },
            },
            "reproduction": {
                "type": "object",
                "description": "Required when QA-ing a bug: the original failing scenario, re-executed.",
                "properties": {
                    "scenario": {"type": "string"},
                    "command": {"type": "array", "items": {"type": "string"}},
                    "exit_code": {"type": "integer"},
                    "observed": {"type": "string"},
                    "still_reproduces": {"type": "boolean"},
                },
                "required": ["scenario", "command", "exit_code", "observed", "still_reproduces"],
            },
        },
        "required": ["stage", "request_id", "head_sha", "verdict", "summary", "checks", "artifacts"],
    }


# ---------------------------------------------------------------------------
# Request / outcome data models
# ---------------------------------------------------------------------------

@dataclass
class WorkerRequest:
    """
    Structured request/dispatch packet handed to a backend.

    Callers are not obliged to use this class. WorkerBackend.execute accepts any
    object or mapping exposing these names, so the adapter can build its own
    packet without importing this module.
    """
    request_id: str
    stage: str
    repo_root: str
    head_sha: Optional[str] = None
    model: Optional[str] = None
    agent_role: Optional[str] = None
    issue_url: Optional[str] = None
    pr_url: Optional[str] = None
    prompt: Optional[str] = None
    criteria: List[str] = field(default_factory=list)
    task_type: Optional[str] = None
    backend: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WorkerOutcome:
    """
    Structured, head-bound result of one real worker execution.

    Field names are a published contract consumed by SuperboardExecutionAdapter
    and by ContinuationDriver. They do not change without coordination.
    """
    ok: bool
    stage: str
    exit_code: Optional[int]
    command: List[str]
    head_sha: Optional[str]
    evidence: Dict[str, Any]
    artifacts: List[str] = field(default_factory=list)
    blocked_reason: Optional[str] = None
    backend_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


@dataclass
class BackendSpec:
    """One configured agent CLI invocation."""
    name: str
    argv: List[str]
    result_source: str = "stdout_json"   # stdout_json | file
    result_path_template: Optional[str] = None
    stdout_result_keys: List[str] = field(default_factory=lambda: ["structured_output"])
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    env: Dict[str, str] = field(default_factory=dict)
    schema_mode: str = "inline"          # inline | file | none
    description: str = ""
    #: Substituted for {permission_mode} and {allowed_tools}.
    #:
    #: Measured against the installed Claude Code CLI, neither documented mode
    #: is sufficient on its own for a worker that must both write code and
    #: prove it works:
    #:   acceptEdits alone  -> file writes permitted, every Bash call denied,
    #:                         so a build worker can write code and substantiate
    #:                         nothing.
    #:   dontAsk alone      -> Bash reads permitted, every file write denied,
    #:                         so a build worker cannot produce anything.
    #: acceptEdits together with an explicit allowed-tools list permits both,
    #: with zero permission denials, and is the least privilege that actually
    #: works. It grants file and shell access inside the target repo and
    #: nothing wider; merge, push and deploy remain refused by the workflow
    #: gates regardless of what a worker is permitted to run.
    permission_mode: str = "acceptEdits"
    allowed_tools: str = "Bash Write Edit Read"
    #: Substituted for {sandbox_mode}. Codex-style backends take a sandbox
    #: policy; "workspace-write" lets a build worker produce something, and a
    #: verification stage should be pinned to "read-only" via its own backend
    #: entry so it cannot mutate the tree it is judging.
    sandbox_mode: str = "workspace-write"
    #: Translation from the routing layer's harness-qualified model ids
    #: ("provider/model:effort") to a name THIS CLI accepts.
    #:
    #: The routing layer picks a model for the whole fleet and names it in its
    #: own vocabulary; each CLI has a different one. Passing a routing id
    #: straight through is a real failure, not a cosmetic one: the claude CLI
    #: answers "[claude-code:unrecognized_model]" and exits 1.
    model_map: Dict[str, str] = field(default_factory=dict)
    #: Model used when the request names none.
    model_default: Optional[str] = None
    #: With strict_model set, an unmapped harness-qualified id BLOCKS and names
    #: the mapping to add. Unset, it falls back to model_default and records the
    #: substitution in the evidence. Strict is the default because a silent
    #: downgrade makes the model in the evidence a fiction.
    strict_model: bool = True

    @classmethod
    def from_dict(cls, name: str, data: Mapping[str, Any]) -> "BackendSpec":
        argv = data.get("argv")
        if not isinstance(argv, (list, tuple)) or not argv:
            raise ValueError(f"backend '{name}': 'argv' must be a non-empty list of strings")
        argv = [str(a) for a in argv]
        result_source = str(data.get("result_source", "stdout_json"))
        if result_source not in ("stdout_json", "file"):
            raise ValueError(
                f"backend '{name}': result_source must be 'stdout_json' or 'file', got {result_source!r}"
            )
        keys = data.get("stdout_result_keys") or ["structured_output"]
        if not isinstance(keys, (list, tuple)):
            raise ValueError(f"backend '{name}': stdout_result_keys must be a list")
        return cls(
            name=name,
            argv=argv,
            result_source=result_source,
            result_path_template=data.get("result_path_template"),
            stdout_result_keys=[str(k) for k in keys],
            timeout_seconds=int(data.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            schema_mode=str(data.get("schema_mode", "inline")),
            description=str(data.get("description", "")),
            permission_mode=str(data.get("permission_mode", "acceptEdits")),
            allowed_tools=str(data.get("allowed_tools", "Bash Write Edit Read")),
            sandbox_mode=str(data.get("sandbox_mode", "workspace-write")),
            model_map={str(k): str(v) for k, v in (data.get("model_map") or {}).items()},
            model_default=(str(data["model_default"]) if data.get("model_default") else None),
            strict_model=bool(data.get("strict_model", True)),
        )


# ---------------------------------------------------------------------------
# Built-in backends, using flags taken from each installed CLI's own help
# ---------------------------------------------------------------------------

#: The routing layer in model_routing.py names models in a harness-qualified
#: vocabulary. These maps translate the ids it actually emits into names each
#: installed CLI accepts. An id absent from a backend's map is one that backend
#: cannot serve, and is refused rather than guessed at.
_ANTHROPIC_MODEL_MAP = {
    "anthropic/claude-opus-5:high": "opus",
    "anthropic/claude-fable-5-1": "sonnet",
    # A routing choice pointing at another vendor is deliberately absent: this
    # backend cannot run a Gemini or a Codex model, and saying so is the honest
    # answer. Map it explicitly in user config to redirect it here.
}
_CODEX_MODEL_MAP = {
    "openai-codex/gpt-5.6-sol:high": "gpt-5.6-sol",
    "openai-codex/gpt-6-astra:high": "gpt-6-astra",
    "openai-codex/gpt-5.3-codex": "gpt-5.3-codex",
}
#: Veyyon resolves fuzzy model names itself ("opus", "gpt-5.2",
#: "openai/gpt-5.2" all work per its --model help), so it can take the routing
#: id unchanged and is not strict about unmapped ones.
_VEYYON_MODEL_MAP: Dict[str, str] = {}

DEFAULT_BACKENDS: Dict[str, Dict[str, Any]] = {
    # claude --help: -p/--print, --output-format json, --json-schema <schema>,
    # --model, --add-dir, --permission-mode, --allowedTools. The structured
    # answer lands on stdout under "structured_output".
    "claude": {
        "argv": [
            "claude", "-p", "{prompt}",
            "--output-format", "json",
            "--json-schema", "{schema_inline}",
            "--model", "{model}",
            "--add-dir", "{repo_root}",
            "--permission-mode", "{permission_mode}",
            "--allowedTools", "{allowed_tools}",
        ],
        "result_source": "stdout_json",
        "stdout_result_keys": ["structured_output"],
        "schema_mode": "inline",
        "model_map": _ANTHROPIC_MODEL_MAP,
        "model_default": "sonnet",
        "description": "Claude Code headless, JSON schema constrained.",
    },
    # The same CLI, configured for a verification stage: no Write and no Edit
    # tool, so it cannot casually rewrite the tree it is judging.
    #
    # Bash is granted in full rather than narrowed to patterns. A narrowed list
    # was measured and rejected: "Bash(python*)" denies the compound commands a
    # tester naturally writes, such as `python tests.py; echo EXIT:$?`, and
    # denies PowerShell outright, so the worker ends up asking a human for
    # approval and returning "blocked" instead of verifying anything. A tool
    # allowlist is in any case the wrong place to enforce immutability, because
    # anything with a shell can write a file. The real guarantee is enforced on
    # the observable outcome: a qa or review result whose observed git HEAD
    # moved is refused, which holds no matter how the tree was touched.
    "claude-verify": {
        "argv": [
            "claude", "-p", "{prompt}",
            "--output-format", "json",
            "--json-schema", "{schema_inline}",
            "--model", "{model}",
            "--add-dir", "{repo_root}",
            "--permission-mode", "{permission_mode}",
            "--allowedTools", "{allowed_tools}",
        ],
        "result_source": "stdout_json",
        "stdout_result_keys": ["structured_output"],
        "schema_mode": "inline",
        "model_map": _ANTHROPIC_MODEL_MAP,
        "model_default": "sonnet",
        "allowed_tools": "Bash Read",
        "description": "Claude Code headless for qa and review: runs checks, cannot edit.",
    },
    # codex exec --help: -m/--model, -C/--cd <DIR>, -s/--sandbox <MODE>,
    # --output-schema <FILE>, -o/--output-last-message <FILE>. The final
    # structured message is written to the last-message file.
    #
    # Codex defaults to a read-only sandbox, which is correct for qa and review
    # and useless for build, so the mode is explicit here rather than inherited.
    # Declare a read-only variant as a separate stage backend to pin verification
    # stages shut.
    "codex": {
        "argv": [
            "codex", "exec",
            "-m", "{model}",
            "-C", "{repo_root}",
            "-s", "{sandbox_mode}",
            "--output-schema", "{schema_path}",
            "-o", "{result_path}",
            "{prompt}",
        ],
        "result_source": "file",
        "result_path_template": "{work_dir}/codex_last_message.json",
        "schema_mode": "file",
        "model_map": _CODEX_MODEL_MAP,
        "model_default": "gpt-5.6-sol",
        "description": "Codex headless exec, output-schema constrained.",
    },
    # veyyon --help: -p/--print, --mode=json, --model. Optional backend.
    "veyyon": {
        "argv": [
            "veyyon", "-p",
            "--mode=json",
            "--model", "{model}",
            "{prompt}",
        ],
        "result_source": "stdout_json",
        "stdout_result_keys": ["structured_output", "result", "output"],
        "schema_mode": "none",
        "model_map": _VEYYON_MODEL_MAP,
        "model_default": "opus",
        "strict_model": False,
        "description": "Veyyon headless print mode. Resolves model names itself, so routing "
                       "ids pass through. Optional; never required by the core.",
    },
}

DEFAULT_BACKEND_NAME = "claude"

#: Out of the box, verification stages route to the edit-free variant so the
#: shipped configuration is correct for all three stages without the operator
#: having to discover that a build worker and a QA worker need different tool
#: grants. Applied ONLY when the operator has declared no stage_backends and is
#: still on the built-in default backend; choosing another backend means owning
#: the stage map too, rather than being silently redirected to claude.
DEFAULT_STAGE_BACKENDS = {"qa": "claude-verify", "review": "claude-verify"}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _read_json_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"worker backend config at {path} must be a JSON object")
    return data


def load_backend_config(
    config: Optional[Union[str, Mapping[str, Any]]] = None,
    state_dir: Optional[str] = None,
    project_config: Any = None,
) -> Dict[str, Any]:
    """
    Resolve worker backend configuration. See BACKEND CONFIGURATION in the
    module docstring for the precedence order.

    User backends are merged over the built-in defaults, so declaring one custom
    harness does not hide `claude`, `codex` or `veyyon`.
    """
    raw: Optional[Dict[str, Any]] = None
    source = "builtin-defaults"

    if isinstance(config, Mapping):
        raw = dict(config)
        source = "explicit-dict"
    elif isinstance(config, str) and config.strip():
        raw = _read_json_file(config)
        source = f"explicit-path:{config}"
    else:
        env_path = os.environ.get("PORTABLE_WORKER_CONFIG", "").strip()
        if env_path and os.path.exists(env_path):
            raw = _read_json_file(env_path)
            source = f"env:PORTABLE_WORKER_CONFIG:{env_path}"
        elif state_dir:
            candidate = os.path.join(state_dir, "worker_backends.json")
            if os.path.exists(candidate):
                raw = _read_json_file(candidate)
                source = f"state-dir:{candidate}"

    if raw is None and project_config is not None:
        meta = getattr(project_config, "metadata", None)
        if isinstance(meta, Mapping) and isinstance(meta.get("portable_worker"), Mapping):
            raw = dict(meta["portable_worker"])
            source = "project-config-metadata"

    raw = raw or {}

    backends: Dict[str, Dict[str, Any]] = copy.deepcopy(DEFAULT_BACKENDS)
    user_backends = raw.get("backends") or {}
    if not isinstance(user_backends, Mapping):
        raise ValueError("worker backend config: 'backends' must be an object")
    for name, spec in user_backends.items():
        if not isinstance(spec, Mapping):
            raise ValueError(f"worker backend config: backend '{name}' must be an object")
        backends[str(name)] = dict(spec)

    stage_backends = raw.get("stage_backends") or {}
    if not isinstance(stage_backends, Mapping):
        raise ValueError("worker backend config: 'stage_backends' must be an object")
    resolved_stage_backends = {str(k): str(v) for k, v in stage_backends.items()}

    default_backend = str(raw.get("default_backend") or DEFAULT_BACKEND_NAME)
    if not resolved_stage_backends and default_backend == DEFAULT_BACKEND_NAME:
        resolved_stage_backends = dict(DEFAULT_STAGE_BACKENDS)

    return {
        "source": source,
        "default_backend": default_backend,
        "stage_backends": resolved_stage_backends,
        "backends": backends,
        "default_model": raw.get("default_model"),
    }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _field(req: Any, name: str, default: Any = None) -> Any:
    """Read a field from a dataclass, plain object, or mapping. Duck-typed by design."""
    if isinstance(req, Mapping):
        value = req.get(name, default)
    else:
        value = getattr(req, name, default)
    return default if value is None else value


def _clip(text: str, limit: int = MAX_CAPTURED_OUTPUT) -> str:
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[clipped {len(text) - limit} chars]"


def _git_head(repo_root: str) -> Optional[str]:
    """Observed git HEAD of repo_root, or None when it is not a usable repo."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        res = subprocess.run(
            [git, "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    sha = res.stdout.strip()
    return sha if SHA_RE.match(sha) else None


def _sha256_file(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _coerce_result_object(payload: Any, keys: Sequence[str]) -> Optional[Dict[str, Any]]:
    """
    Pull the agent's structured answer out of a CLI envelope.

    Handles the shapes the real CLIs emit: a bare result object, an envelope
    carrying it under a known key, and an envelope carrying it as a JSON string
    that still needs parsing.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    if not isinstance(payload, dict):
        return None

    # Already the result object itself.
    if "verdict" in payload and "stage" in payload:
        return payload

    for key in keys:
        if key not in payload:
            continue
        candidate = payload[key]
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except (ValueError, TypeError):
                continue
        if isinstance(candidate, dict):
            return candidate
    return None


def _extract_last_json_object(text: str) -> Optional[Any]:
    """
    Recover the JSON result from output that also carries log noise.

    Real CLIs interleave hook warnings, OAuth errors and progress banners with
    their JSON, and that noise can itself contain an unbalanced brace. So this
    walks every "{" and lets the JSON decoder decide where an object ends,
    keeping the LAST top-level object that parses, because a CLI prints its
    result after its chatter.

    Two things it deliberately does not do: it does not scan backwards from the
    final brace, which would return whichever innermost nested object happens to
    parse alone and throw away the envelope; and it does not stop at the first
    unbalanced brace, which would let one malformed log line hide a perfectly
    good result printed after it.
    """
    if not text:
        return None
    try:
        return json.loads(text.strip())
    except (ValueError, TypeError):
        pass

    decoder = json.JSONDecoder()
    candidates: List[Any] = []
    index = 0
    attempts = 0
    length = len(text)
    while index < length and attempts < MAX_JSON_SCAN_ATTEMPTS:
        index = text.find("{", index)
        if index < 0:
            break
        attempts += 1
        try:
            obj, end = decoder.raw_decode(text, index)
        except ValueError:
            index += 1
            continue
        if isinstance(obj, dict):
            candidates.append(obj)
        # Skip past what was consumed, so a successfully parsed envelope is
        # never re-entered to harvest its own nested objects.
        index = max(end, index + 1)

    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

STAGE_BRIEFS = {
    "build": (
        "You are the BUILD worker. Implement the request in the repository at {repo_root}. "
        "Create or modify real files, then commit them. Report every file you touched under "
        "\"artifacts\" as a path relative to the repository root, and report the commands you ran "
        "under \"checks\" with their real exit codes."
    ),
    "qa": (
        "You are the QA worker, independent of whoever built this. Do NOT modify the tree and do "
        "NOT commit. Verify the request against the repository at {repo_root} by executing real "
        "commands, and report each one under \"checks\" with its real exit code and observed output. "
        "Return verdict \"fail\" if verification does not hold."
    ),
    "review": (
        "You are the REVIEW worker, independent of whoever built this. Do NOT modify the tree and "
        "do NOT commit. Review the change on the current commit, execute the commands you need to "
        "substantiate your reading, and report them under \"checks\". Return verdict \"fail\" if the "
        "change is not sound."
    ),
}


def build_stage_prompt(req: Any, schema: Dict[str, Any]) -> str:
    """
    Compose the stage prompt. An explicit prompt on the request is used verbatim
    as the task statement; the stage brief and result contract are always added
    so a backend cannot be talked out of returning structured evidence.
    """
    stage = str(_field(req, "stage", "build"))
    repo_root = str(_field(req, "repo_root", ""))
    request_id = str(_field(req, "request_id", ""))
    head_sha = _field(req, "head_sha") or "(unset)"
    task = str(_field(req, "prompt", "")).strip()
    criteria = _field(req, "criteria", []) or []
    task_type = _field(req, "task_type") or "unspecified"

    brief = STAGE_BRIEFS.get(stage, STAGE_BRIEFS["build"]).format(repo_root=repo_root)

    lines = [
        brief,
        "",
        f"Request id: {request_id}",
        f"Stage: {stage}",
        f"Task type: {task_type}",
        f"Repository root: {repo_root}",
        f"Expected head commit: {head_sha}",
    ]
    if task:
        lines += ["", "TASK", task]
    if criteria:
        lines += ["", "ACCEPTANCE CRITERIA"]
        for c in criteria:
            if isinstance(c, Mapping):
                c = c.get("criterion", "")
            lines.append(f"- {c}")

    lines += [
        "",
        "RESULT CONTRACT",
        "Your final answer must be a single JSON object matching this schema exactly:",
        json.dumps(schema, indent=2),
        "",
        "Rules that are enforced after you exit, so satisfy them or you will be rejected:",
        "- \"head_sha\" must be the full 40-character output of `git rev-parse HEAD` in the "
        "repository you worked in. It is cross-checked against the real HEAD.",
        "- \"checks\" must describe commands you actually executed. A \"pass\" verdict with an "
        "empty checks list is rejected.",
        "- Every \"artifacts\" path must exist on disk when you exit.",
        "- Do not claim a reproduction is gone with a phrase. If this is a bug, fill in "
        "\"reproduction\" with the scenario re-executed, its command, its real exit code and what "
        "you observed.",
        "- Never merge, never push, never deploy. Committing locally is allowed for the build "
        "stage only.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------

class WorkerBackendError(Exception):
    """Raised only for programmer error, never for a failed worker run."""


class WorkerBackend:
    """
    Executes one real agent-CLI worker stage and returns structured, head-bound
    evidence.

    Usage:
        backend = WorkerBackend(state_dir=..., default_model="sonnet")
        outcome = backend.execute(request)

    A failed run is a WorkerOutcome with ok=False, not an exception. The only
    exceptions raised are configuration errors surfaced at construction time.
    """

    def __init__(
        self,
        config: Optional[Union[str, Mapping[str, Any]]] = None,
        state_dir: Optional[str] = None,
        project_config: Any = None,
        default_model: Optional[str] = None,
        default_backend: Optional[str] = None,
        work_dir: Optional[str] = None,
        dry_run: bool = False,
        timeout_seconds: Optional[int] = None,
    ):
        self.state_dir = os.path.abspath(state_dir) if state_dir else SCRIPT_DIR
        self.resolved_config = load_backend_config(
            config=config, state_dir=self.state_dir, project_config=project_config
        )
        self.default_backend = default_backend or self.resolved_config["default_backend"]
        self.default_model = default_model or self.resolved_config.get("default_model") or "sonnet"
        self.work_dir = os.path.abspath(work_dir) if work_dir else os.path.join(self.state_dir, "worker_runs")
        self.dry_run = dry_run
        self.timeout_override = timeout_seconds

    # -- configuration ----------------------------------------------------

    def available_backends(self) -> Dict[str, bool]:
        """Map backend name -> whether its executable is on PATH right now."""
        out: Dict[str, bool] = {}
        for name, raw in self.resolved_config["backends"].items():
            argv = raw.get("argv") or []
            out[name] = bool(argv) and shutil.which(str(argv[0])) is not None
        return out

    def resolve_backend(self, stage: str, requested: Optional[str] = None) -> Tuple[Optional[BackendSpec], Optional[str]]:
        """Pick the backend for this stage. Returns (spec, error)."""
        name = requested or self.resolved_config["stage_backends"].get(stage) or self.default_backend
        raw = self.resolved_config["backends"].get(name)
        if raw is None:
            known = ", ".join(sorted(self.resolved_config["backends"])) or "(none)"
            return None, (
                f"No worker backend named '{name}' is configured. Configured backends: {known}. "
                f"Config source: {self.resolved_config['source']}."
            )
        try:
            spec = BackendSpec.from_dict(name, raw)
        except ValueError as e:
            return None, f"Worker backend '{name}' is misconfigured: {e}"
        if self.timeout_override:
            spec.timeout_seconds = int(self.timeout_override)
        return spec, None

    def resolve_model(self, spec: BackendSpec, requested: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Translate a routing model id into a name this backend's CLI accepts.

        Returns (model, note, error). Exactly one of model or error is set; note
        records a substitution worth carrying into the evidence.

        A harness-qualified id ("provider/model" or "model:effort") belongs to
        the routing layer's vocabulary, not to any one CLI, so it must be mapped.
        A bare name is already a CLI name and passes through untouched.
        """
        fallback = spec.model_default or self.default_model
        requested = (requested or "").strip()
        if not requested:
            return fallback, None, None

        if requested in spec.model_map:
            mapped = spec.model_map[requested]
            if mapped == requested:
                return mapped, None, None
            return mapped, f"routing model '{requested}' mapped to '{mapped}' for backend '{spec.name}'", None

        is_qualified = "/" in requested or ":" in requested
        if not is_qualified:
            return requested, None, None

        if spec.strict_model:
            known = ", ".join(sorted(spec.model_map)) or "(none)"
            return None, None, (
                f"Backend '{spec.name}' has no mapping for routing model '{requested}', so it "
                f"cannot run that model. Refusing rather than passing an id its CLI will reject "
                f"or silently substituting a different model. Mapped ids: {known}. Add "
                f"\"model_map\": {{\"{requested}\": \"<cli-model-name>\"}} to this backend's "
                f"config, or route this stage to a backend that serves it."
            )

        return fallback, (
            f"routing model '{requested}' is unmapped for backend '{spec.name}'; fell back to "
            f"'{fallback}' because this backend is configured with strict_model disabled"
        ), None

    # -- argv construction -------------------------------------------------

    def _substitute(self, argv: Sequence[str], values: Mapping[str, str]) -> List[str]:
        """
        Whole-token placeholder substitution. Each argv element is rendered
        independently and stays a single element, so no value can inject an
        extra argument regardless of its content.
        """
        out: List[str] = []
        for token in argv:
            rendered = token
            for key, val in values.items():
                needle = "{" + key + "}"
                if needle in rendered:
                    rendered = rendered.replace(needle, val)
            out.append(rendered)
        return out

    # -- execution ---------------------------------------------------------

    def execute(self, request: Any) -> WorkerOutcome:
        """
        Run one real worker stage. Never raises for a failed run; always returns
        a WorkerOutcome whose ok flag is the verdict.
        """
        stage = str(_field(request, "stage", "") or "").strip()
        request_id = str(_field(request, "request_id", "") or "").strip()
        repo_root = str(_field(request, "repo_root", "") or "").strip()
        requested_head = _field(request, "head_sha") or None
        backend_name = _field(request, "backend") or None

        base_evidence: Dict[str, Any] = {
            "backend": None,
            "stage": stage or "unknown",
            "request_id": request_id,
            "head_sha": None,
            "structured_result": None,
            "verdict": None,
            "artifact_digests": {},
            "started_at": _now(),
            "config_source": self.resolved_config["source"],
        }

        def blocked(reason: str, *, exit_code: Optional[int] = None,
                    command: Optional[List[str]] = None,
                    extra: Optional[Dict[str, Any]] = None,
                    head: Optional[str] = None) -> WorkerOutcome:
            ev = dict(base_evidence)
            ev["finished_at"] = _now()
            ev["blocked_reason"] = reason
            ev["head_sha"] = head
            if extra:
                ev.update(extra)
            return WorkerOutcome(
                ok=False,
                stage=stage or "unknown",
                exit_code=exit_code,
                command=command or [],
                head_sha=head,
                evidence=ev,
                artifacts=[],
                blocked_reason=reason,
                backend_name=base_evidence.get("backend"),
            )

        # 1. Request validation. A malformed packet blocks; it never crashes the
        #    caller's step loop.
        if not request_id:
            return blocked("Malformed worker request: 'request_id' is required.")
        if stage not in VALID_STAGES:
            return blocked(
                f"Malformed worker request: stage must be one of {VALID_STAGES}, got {stage!r}."
            )
        if not repo_root:
            return blocked("Malformed worker request: 'repo_root' is required.")
        repo_root = os.path.abspath(repo_root)
        if not os.path.isdir(repo_root):
            return blocked(f"Worker repo_root does not exist or is not a directory: {repo_root}")
        if requested_head is not None and not SHA_RE.match(str(requested_head)):
            return blocked(
                f"Malformed worker request: head_sha must be a full 40-character SHA, got {requested_head!r}."
            )

        # 2. Backend resolution.
        spec, err = self.resolve_backend(stage, backend_name)
        if err or spec is None:
            return blocked(err or "Worker backend could not be resolved.")
        base_evidence["backend"] = spec.name

        # 2b. Model translation. The routing layer names models in its own
        #     vocabulary; this backend's CLI has a different one.
        resolved_model, model_note, model_err = self.resolve_model(
            spec, _field(request, "model")
        )
        if model_err:
            return blocked(model_err)
        base_evidence["model"] = resolved_model
        base_evidence["model_requested"] = _field(request, "model") or None
        if model_note:
            base_evidence["model_note"] = model_note

        # 3. Executable presence. A missing command blocks, it does not fall
        #    back to a fixture.
        executable = shutil.which(spec.argv[0])
        if not executable:
            return blocked(
                f"Worker backend '{spec.name}' command '{spec.argv[0]}' was not found on PATH. "
                f"Install it or configure a different backend; there is no fixture fallback."
            )

        # 4. Prepare the run directory, schema and prompt.
        schema = agent_result_schema()
        run_dir = os.path.join(self.work_dir, f"{request_id}__{stage}__{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}")
        try:
            os.makedirs(run_dir, exist_ok=True)
        except OSError as e:
            return blocked(f"Could not create worker run directory {run_dir}: {e}")

        schema_path = os.path.join(run_dir, "result_schema.json")
        if spec.schema_mode == "file":
            try:
                with open(schema_path, "w", encoding="utf-8") as fh:
                    json.dump(schema, fh, indent=2)
            except OSError as e:
                return blocked(f"Could not write result schema to {schema_path}: {e}")

        prompt = build_stage_prompt(request, schema)
        result_path = ""
        if spec.result_source == "file":
            template = spec.result_path_template or "{work_dir}/result.json"
            result_path = template.replace("{work_dir}", run_dir).replace("{repo_root}", repo_root)

        values = {
            "prompt": prompt,
            "model": str(resolved_model),
            "agent_role": str(_field(request, "agent_role") or "worker"),
            "stage": stage,
            "request_id": request_id,
            "head_sha": str(requested_head or ""),
            "repo_root": repo_root,
            "schema_path": schema_path,
            "schema_inline": json.dumps(schema),
            "permission_mode": spec.permission_mode,
            "allowed_tools": spec.allowed_tools,
            "sandbox_mode": spec.sandbox_mode,
            "result_path": result_path,
            "work_dir": run_dir,
            "issue_url": str(_field(request, "issue_url") or ""),
            "pr_url": str(_field(request, "pr_url") or ""),
        }
        argv = self._substitute(spec.argv, values)
        argv[0] = executable

        # 5. Observed head before the run. QA and review are bound to a commit
        #    that must not move; build may advance it.
        head_before = _git_head(repo_root)
        if requested_head and head_before and head_before != str(requested_head):
            return blocked(
                f"Head binding refused before execution: request targets {requested_head} but the "
                f"tree at {repo_root} is on {head_before}. Check out the requested commit first; "
                f"a worker never runs against whatever happens to be present.",
                command=argv,
                head=head_before,
                extra={"head_before": head_before},
            )
        base_evidence["head_before"] = head_before
        base_evidence["command"] = argv
        base_evidence["run_dir"] = run_dir

        if self.dry_run:
            ev = dict(base_evidence)
            ev["finished_at"] = _now()
            ev["dry_run"] = True
            ev["blocked_reason"] = "Dry run: the configured command was built and validated but not executed."
            return WorkerOutcome(
                ok=False,
                stage=stage,
                exit_code=None,
                command=argv,
                head_sha=head_before,
                evidence=ev,
                artifacts=[],
                blocked_reason="Dry run: command built and validated, nothing executed.",
                backend_name=spec.name,
            )

        # 6. Run it. shell=False, explicit argv, no interpolation into a shell.
        env = dict(os.environ)
        env.update(spec.env)
        try:
            proc = subprocess.run(
                argv,
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=spec.timeout_seconds,
                shell=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return blocked(
                f"Worker backend '{spec.name}' exceeded its {spec.timeout_seconds}s timeout for "
                f"stage '{stage}' of request '{request_id}'.",
                command=argv,
                head=head_before,
            )
        except OSError as e:
            return blocked(
                f"Worker backend '{spec.name}' could not be executed: {e}",
                command=argv,
                head=head_before,
            )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        base_evidence["exit_code"] = proc.returncode
        base_evidence["stdout_tail"] = _clip(stdout)
        base_evidence["stderr_tail"] = _clip(stderr)

        head_after = _git_head(repo_root)
        base_evidence["head_after"] = head_after

        # 7. Non-zero exit blocks.
        if proc.returncode != 0:
            return blocked(
                f"Worker backend '{spec.name}' exited {proc.returncode} for stage '{stage}' of "
                f"request '{request_id}'.",
                exit_code=proc.returncode,
                command=argv,
                head=head_after or head_before,
            )

        # 8. Recover the structured result. Absence blocks; exit 0 is not a result.
        structured: Optional[Dict[str, Any]] = None
        if spec.result_source == "file":
            if not result_path or not os.path.exists(result_path):
                return blocked(
                    f"Worker backend '{spec.name}' exited 0 but wrote no structured result to "
                    f"{result_path or '(unset)'}. Exit status alone is not evidence.",
                    exit_code=proc.returncode,
                    command=argv,
                    head=head_after or head_before,
                )
            try:
                with open(result_path, "r", encoding="utf-8") as fh:
                    raw_text = fh.read()
            except OSError as e:
                return blocked(
                    f"Worker backend '{spec.name}' result file {result_path} could not be read: {e}",
                    exit_code=proc.returncode, command=argv, head=head_after or head_before,
                )
            payload = _extract_last_json_object(raw_text)
            structured = _coerce_result_object(payload, spec.stdout_result_keys)
        else:
            payload = _extract_last_json_object(stdout)
            if payload is None:
                return blocked(
                    f"Worker backend '{spec.name}' exited 0 but emitted no parseable JSON on stdout. "
                    f"Exit status alone is not evidence.",
                    exit_code=proc.returncode, command=argv, head=head_after or head_before,
                )
            if isinstance(payload, dict) and payload.get("is_error") is True:
                return blocked(
                    f"Worker backend '{spec.name}' reported is_error=true "
                    f"(subtype={payload.get('subtype')!r}) despite exit 0.",
                    exit_code=proc.returncode, command=argv, head=head_after or head_before,
                )
            structured = _coerce_result_object(payload, spec.stdout_result_keys)

        if structured is None:
            return blocked(
                f"Worker backend '{spec.name}' produced output that is not a structured worker "
                f"result. Expected a JSON object with the required stage/verdict fields "
                f"(looked under {spec.stdout_result_keys}).",
                exit_code=proc.returncode, command=argv, head=head_after or head_before,
            )
        base_evidence["structured_result"] = structured

        # 9. Validate the result against the contract.
        valid, reason, verdict = self._validate_result(
            structured=structured,
            stage=stage,
            request_id=request_id,
            request=request,
            repo_root=repo_root,
            head_before=head_before,
            head_after=head_after,
            requested_head=requested_head,
        )
        base_evidence["verdict"] = verdict
        observed_head = head_after or head_before
        base_evidence["head_sha"] = observed_head

        if not valid:
            return blocked(
                reason or "Worker result failed validation.",
                exit_code=proc.returncode, command=argv, head=observed_head,
                extra={"verdict": verdict},
            )

        # 10. Artifacts: every declared path must exist, and is digested.
        artifacts: List[str] = []
        digests: Dict[str, str] = {}
        for entry in structured.get("artifacts") or []:
            rel = entry.get("path") if isinstance(entry, Mapping) else entry
            if not rel:
                continue
            rel = str(rel)
            abs_path = rel if os.path.isabs(rel) else os.path.join(repo_root, rel)
            if not os.path.exists(abs_path):
                return blocked(
                    f"Worker declared artifact '{rel}' but it does not exist at {abs_path}. "
                    f"A claimed artifact that is absent is not evidence.",
                    exit_code=proc.returncode, command=argv, head=observed_head,
                    extra={"verdict": verdict},
                )
            artifacts.append(rel)
            digest = _sha256_file(abs_path) if os.path.isfile(abs_path) else None
            if digest:
                digests[rel] = digest

        base_evidence["artifact_digests"] = digests
        base_evidence["artifacts"] = artifacts
        base_evidence["checks"] = structured.get("checks") or []
        base_evidence["summary"] = structured.get("summary") or ""
        repro = structured.get("reproduction")
        if isinstance(repro, Mapping):
            # `verdict` is DERIVED here, never taken from the agent. It is only
            # ever set to "absent" once the re-executed scenario has already
            # passed _validate_result: a real command, a real integer exit code,
            # a real observation, and still_reproduces is literally False. A
            # model writing "verdict": "absent" or the words "proven absent"
            # into its answer cannot reach this value on its own.
            derived = dict(repro)
            derived["verdict"] = "absent" if repro.get("still_reproduces") is False else "present"
            derived["derived_by"] = "worker_backend._validate_result"
            base_evidence["reproduction"] = derived
        base_evidence["finished_at"] = _now()
        base_evidence["auto_merge_allowed"] = False
        base_evidence["auto_deploy_allowed"] = False

        return WorkerOutcome(
            ok=True,
            stage=stage,
            exit_code=proc.returncode,
            command=argv,
            head_sha=observed_head,
            evidence=base_evidence,
            artifacts=artifacts,
            blocked_reason=None,
            backend_name=spec.name,
        )

    # -- validation --------------------------------------------------------

    def _validate_result(
        self,
        structured: Mapping[str, Any],
        stage: str,
        request_id: str,
        request: Any,
        repo_root: str,
        head_before: Optional[str],
        head_after: Optional[str],
        requested_head: Optional[str],
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Enforce the evidence contract on a parsed agent result.

        Returns (valid, reason, verdict). This is where "exit 0" and
        "proven absent" stop being acceptable.
        """
        required = ("stage", "request_id", "head_sha", "verdict", "summary", "checks", "artifacts")
        missing = [k for k in required if k not in structured]
        if missing:
            return False, (
                f"Worker result is missing required field(s): {', '.join(missing)}. "
                f"A partial result is not evidence."
            ), structured.get("verdict")

        verdict = structured.get("verdict")
        if verdict not in VALID_VERDICTS:
            return False, (
                f"Worker result verdict {verdict!r} is not one of {VALID_VERDICTS}."
            ), verdict

        if str(structured.get("stage")) != stage:
            return False, (
                f"Worker result is for stage {structured.get('stage')!r} but stage {stage!r} was "
                f"dispatched. Refusing to credit a result to the wrong stage."
            ), verdict

        if str(structured.get("request_id")) != request_id:
            return False, (
                f"Worker result is for request {structured.get('request_id')!r} but request "
                f"{request_id!r} was dispatched."
            ), verdict

        # A non-pass verdict is a legitimate, honest outcome, but it does not
        # advance the request. Report it as blocked with the worker's reason.
        if verdict != "pass":
            return False, (
                f"Worker returned verdict '{verdict}' for stage '{stage}': "
                f"{structured.get('summary') or '(no summary)'}"
            ), verdict

        # --- head binding -------------------------------------------------
        claimed = structured.get("head_sha")
        if not isinstance(claimed, str) or not SHA_RE.match(claimed.strip()):
            return False, (
                f"Worker claimed head_sha {claimed!r}, which is not a full 40-character SHA. "
                f"Evidence must be bound to a real commit."
            ), verdict
        claimed = claimed.strip()

        observed = head_after
        if observed is None:
            return False, (
                f"Could not read git HEAD in {repo_root} after execution, so the worker's "
                f"evidence cannot be bound to a commit."
            ), verdict
        if claimed != observed:
            return False, (
                f"Head binding refused: worker claims commit {claimed} but the observed HEAD in "
                f"{repo_root} after execution is {observed}. The observed commit always wins."
            ), verdict

        if stage in IMMUTABLE_HEAD_STAGES:
            if head_before and head_before != observed:
                return False, (
                    f"{stage.upper()} evidence is invalid: HEAD moved from {head_before} to "
                    f"{observed} during the run. A verification stage must not mutate the tree it "
                    f"is verifying."
                ), verdict
            if requested_head and observed != str(requested_head):
                return False, (
                    f"{stage.upper()} evidence is invalid: request targets {requested_head} but the "
                    f"tested commit is {observed}."
                ), verdict

        # --- executed checks ---------------------------------------------
        checks = structured.get("checks")
        if not isinstance(checks, list) or not checks:
            return False, (
                f"Worker returned verdict 'pass' for stage '{stage}' with no executed checks. "
                f"Exit status alone is never evidence."
            ), verdict

        good_checks = 0
        for idx, chk in enumerate(checks):
            if not isinstance(chk, Mapping):
                return False, f"Worker check #{idx} is not an object.", verdict
            cmd = chk.get("command")
            if not isinstance(cmd, list) or not cmd or not all(str(c).strip() for c in cmd):
                return False, (
                    f"Worker check #{idx} ({chk.get('name')!r}) carries no executed command. "
                    f"A pass verdict needs commands that actually ran."
                ), verdict
            if not isinstance(chk.get("exit_code"), int):
                return False, (
                    f"Worker check #{idx} ({chk.get('name')!r}) has no integer exit_code."
                ), verdict
            if not str(chk.get("observed") or "").strip():
                return False, (
                    f"Worker check #{idx} ({chk.get('name')!r}) reports nothing observed."
                ), verdict
            good_checks += 1

        if good_checks == 0:
            return False, "Worker returned no substantiated check.", verdict

        # --- build must have produced something --------------------------
        artifacts = structured.get("artifacts")
        if not isinstance(artifacts, list):
            return False, "Worker result 'artifacts' must be a list.", verdict

        if stage == "build":
            advanced = bool(head_before and head_after and head_before != head_after)
            if not advanced and not artifacts:
                return False, (
                    "Build stage returned 'pass' but produced neither a new commit nor any "
                    "artifact. A build that changed nothing has proven nothing."
                ), verdict

        # --- bug reproduction must be re-executed, not asserted ----------
        # The adapter supplies task_type, so it is authoritative when present.
        # The id heuristic survives only for a caller that omits it entirely
        # (the standalone CLI), and would otherwise demand a reproduction record
        # from any request whose id merely contains the word.
        task_type = str(_field(request, "task_type", "") or "").strip().lower()
        if task_type:
            is_bug = task_type == "bug"
        else:
            is_bug = "bug" in request_id.lower()
        if is_bug and stage == "qa":
            repro = structured.get("reproduction")
            if not isinstance(repro, Mapping):
                return False, (
                    f"Bug QA for '{request_id}' returned 'pass' without a 'reproduction' record. "
                    f"A bug is closed by re-running the original failing scenario, not by asserting "
                    f"it is gone."
                ), verdict
            for key in ("scenario", "command", "exit_code", "observed", "still_reproduces"):
                if key not in repro:
                    return False, (
                        f"Bug QA reproduction record for '{request_id}' is missing '{key}'."
                    ), verdict
            if not isinstance(repro.get("command"), list) or not repro["command"]:
                return False, (
                    f"Bug QA reproduction for '{request_id}' names no command, so the scenario was "
                    f"not re-executed."
                ), verdict
            if not isinstance(repro.get("exit_code"), int):
                return False, (
                    f"Bug QA reproduction for '{request_id}' has no integer exit_code."
                ), verdict
            if not str(repro.get("observed") or "").strip():
                return False, (
                    f"Bug QA reproduction for '{request_id}' records nothing observed."
                ), verdict
            if repro.get("still_reproduces") is not False:
                return False, (
                    f"Bug QA for '{request_id}': the original scenario still reproduces on "
                    f"{observed}. Reopening rather than advancing."
                ), verdict
            if not str(repro.get("scenario") or "").strip():
                return False, (
                    f"Bug QA reproduction for '{request_id}' does not state the scenario."
                ), verdict

        return True, None, verdict


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    import argparse

    p = argparse.ArgumentParser(
        description="Real configured agent-CLI worker backend for the portable workflow core."
    )
    p.add_argument("--list-backends", action="store_true",
                   help="Show configured backends and whether each command is on PATH.")
    p.add_argument("--print-schema", action="store_true",
                   help="Print the JSON Schema required of an agent worker result.")
    p.add_argument("--request-id", help="Request id to execute a stage for.")
    p.add_argument("--stage", choices=list(VALID_STAGES), help="Worker stage to run.")
    p.add_argument("--repo-root", help="Repository the worker runs in.")
    p.add_argument("--head-sha", help="Full 40-character commit the stage is bound to.")
    p.add_argument("--model", help="Model to pass to the backend.")
    p.add_argument("--agent-role", default="worker", help="Role label carried into the prompt.")
    p.add_argument("--task-type", help="Ledger task type, e.g. 'bug'.")
    p.add_argument("--prompt", help="Task statement for the worker.")
    p.add_argument("--criterion", action="append", default=[], dest="criteria",
                   help="Acceptance criterion (repeatable).")
    p.add_argument("--backend", help="Force a specific configured backend.")
    p.add_argument("--config", help="Path to a worker backend config JSON.")
    p.add_argument("--state-dir", help="State directory used for config and run artifacts.")
    p.add_argument("--timeout", type=int, help="Override the backend timeout, in seconds.")
    p.add_argument("--dry-run", action="store_true",
                   help="Build and validate the argv without executing it.")
    p.add_argument("--json", action="store_true", help="Emit the outcome as JSON.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.print_schema:
        print(json.dumps(agent_result_schema(), indent=2))
        return 0

    backend = WorkerBackend(
        config=args.config,
        state_dir=args.state_dir,
        default_model=args.model,
        default_backend=args.backend,
        dry_run=args.dry_run,
        timeout_seconds=args.timeout,
    )

    if args.list_backends:
        avail = backend.available_backends()
        print(f"config source: {backend.resolved_config['source']}")
        print(f"default backend: {backend.default_backend}")
        stage_map = backend.resolved_config["stage_backends"]
        if stage_map:
            print(f"stage overrides: {stage_map}")
        for name in sorted(avail):
            spec = backend.resolved_config["backends"][name]
            state = "on PATH" if avail[name] else "NOT FOUND"
            print(f"  {name:12s} {state:10s} {(spec.get('argv') or [''])[0]}")
        return 0

    if not (args.request_id and args.stage and args.repo_root):
        print("--request-id, --stage and --repo-root are required to execute a stage.",
              file=sys.stderr)
        return 64

    req = WorkerRequest(
        request_id=args.request_id,
        stage=args.stage,
        repo_root=args.repo_root,
        head_sha=args.head_sha,
        model=args.model,
        agent_role=args.agent_role,
        prompt=args.prompt,
        criteria=list(args.criteria or []),
        task_type=args.task_type,
        backend=args.backend,
    )
    outcome = backend.execute(req)

    if args.json:
        print(outcome.to_json())
    else:
        print(f"ok            : {outcome.ok}")
        print(f"backend       : {outcome.backend_name}")
        print(f"stage         : {outcome.stage}")
        print(f"exit_code     : {outcome.exit_code}")
        print(f"head_sha      : {outcome.head_sha}")
        print(f"artifacts     : {outcome.artifacts}")
        if outcome.blocked_reason:
            print(f"blocked_reason: {outcome.blocked_reason}")
        if outcome.evidence.get("summary"):
            print(f"summary       : {outcome.evidence['summary']}")

    return 0 if outcome.ok else 1


if __name__ == "__main__":
    sys.exit(main())
