"""Install this reviewable GitHub-native bundle; --check detects source/runtime drift.

Only enumerated code and policy files are replaced, never state, credentials,
configuration or processes. Run from a committed source checkout after opening
its PR. Re-run --check against that same pinned checkout before further edits.

The profile AGENTS.md is only overwritten while it still holds the policy the last
install wrote. A live profile edited since then is refused with a diff summary and
exit 1 unless --force-policy is passed: operator rulings land in the live profile
first, so the repository copy can lag behind it, and installing that copy over the
live file would silently revert them.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import tempfile

RUNTIME_FILES = (
    "coordinator.py", "ledger.py", "project_adapter.py", "superboard_adapter.py",
    "github_work_item.py", "github_pr_gate.py", "review_content.py",
    "coordinator_smoke_test.py", "test_superboard_adapter.py", "test_github_work_item.py",
    "test_github_pr_gate.py", "test_project_adapter.py", "test_continuation_driver.py",
    "test_review_content.py", "test_review_content_gate.py",
    "github_plan_renderer.py", "github_plan_templates.py", "test_github_publication.py",
    "install_github_native.py", "test_install_github_native.py", "PORTABLE.md",
    # Model router: coordinator.py and superboard_adapter.py import it at runtime.
    "model_routing.py", "balance_loader.py", "routing_smoke_test.py",
    "quota_snapshot.py", "test_quota_snapshot.py",
    # Lane quality telemetry: session mining and model comparison.
    "lane_quality.py", "test_lane_quality.py",
    # Feature map navigation and build slot arbiters.
    "feature_map.py", "feature_map.schema.json", "test_feature_map.py",
    "build_slot.py", "test_build_slot.py",
    # Safe worktree removal; polysim_frontend_deps.py imports it (Wladefant/super-board#321).
    "wt_remove.py", "test_wt_remove.py",
    # PolySimulator per-worktree Next cache (Wladefant/super-board#321).
    "polysim_next_cache.py", "test_polysim_next_cache.py",
    # Verification CLI and smoke test gate.
    "verify.py", "test_verify.py",
    # Lane-brief merge guard (Wladefant/super-board#227).
    "merge_guard.py", "test_merge_guard.py",
    # Session crash monitor and test suite.
    "session_crash_monitor.py", "test_session_crash_monitor.py",
)
# Veyyon extensions, installed into the profile's own `extensions/` dir beside AGENTS.md,
# where Veyyon loads them for every session of that profile.
PROFILE_EXTENSIONS = ("superboard-merge-guard.ts",)
POLICY = Path("policies/default/AGENTS.md")
# Hash of the policy the last install wrote, recorded beside the profile it wrote to.
POLICY_STATE_SUFFIX = ".installed.sha256"
MAX_DRIFT_DIFF_LINES = 40


def policy_state_path(profile: Path) -> Path:
    return profile.with_name(profile.name + POLICY_STATE_SUFFIX)


def recorded_policy_hash(profile: Path) -> str | None:
    """Hash of the last installed policy, or None when no install has been recorded."""
    state = policy_state_path(profile)
    if not state.exists():
        return None
    value = state.read_text(encoding="utf-8").strip()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        return None
    return value


def policy_drift_summary(profile: Path, source_policy: bytes) -> str:
    """Live-versus-source diff, bounded, for a refusal message a human has to act on."""
    live = profile.read_text(encoding="utf-8", errors="replace").splitlines()
    wanted = source_policy.decode("utf-8", "replace").splitlines()
    diff = list(difflib.unified_diff(live, wanted, f"live:{profile.name}", f"repo:{POLICY}", lineterm=""))
    hidden = len(diff) - MAX_DRIFT_DIFF_LINES
    summary = "\n".join(diff[:MAX_DRIFT_DIFF_LINES])
    return summary + (f"\n... {hidden} more diff line(s)" if hidden > 0 else "")


def synchronize(
    source_root: Path, profile: Path, runtime: Path, check: bool = False, force_policy: bool = False
) -> bool:
    pairs = [(source_root / POLICY, profile)] + [
            (source_root / "workflows/portable" / name, runtime / name) for name in RUNTIME_FILES
        ] + [
            (source_root / "workflows/portable/extensions" / name, profile.parent / "extensions" / name)
            for name in PROFILE_EXTENSIONS
        ]
    # Read every source before any mutation; an incomplete checkout changes nothing.
    payloads = [(target, source.read_bytes()) for source, target in pairs]
    source_policy = payloads[0][1]
    if not check and not force_policy and profile.exists():
        recorded = recorded_policy_hash(profile)
        live = profile.read_bytes()
        if (
            recorded is not None
            and hashlib.sha256(live).hexdigest() != recorded
            and live != source_policy
        ):
            print(
                f"REFUSED: {profile} was edited after the last install, so installing "
                f"{POLICY} over it would discard those edits."
            )
            print(policy_drift_summary(profile, source_policy))
            print(
                "Forward-port the live profile into the repository and re-run, or pass "
                "--force-policy to overwrite the live file."
            )
            return False
    # The runtime may contain newer unrelated integrations. Patch only owned
    # manifest fields, rather than replacing its independent module inventory.
    source_manifest = json.loads((source_root / "workflows/portable/manifest.json").read_text(encoding="utf-8"))
    manifest_path = runtime / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    manifest.setdefault("authority", {}).update(source_manifest["authority"])
    for name in ("github_work_item.py", "review_content.py", "install_github_native.py", "ledger.py", "verify.py"):
        if name in source_manifest.get("modules", {}):
            manifest.setdefault("modules", {})[name] = source_manifest["modules"][name]
    required = manifest.setdefault("export", {}).setdefault("required_files", [])
    for name in ("github_work_item.py", "review_content.py"):
        if name not in required:
            required.append(name)
    payloads.append((manifest_path, (json.dumps(manifest, indent=2) + "\n").encode()))
    for target, data in payloads:
        if not check:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        if not target.exists() or target.read_bytes() != data:
            print(f"DRIFT: {target.name}")
            return False
    if not check:
        policy_state_path(profile).write_text(
            hashlib.sha256(payloads[0][1]).hexdigest() + "\n", encoding="utf-8"
        )
    digest = hashlib.sha256(payloads[0][1]).hexdigest()
    print(f"MATCH: {len(payloads)} source/installed files; policy sha256={digest}")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--profile-path", type=Path, default=Path.home() / ".veyyon/profiles/default/agent/AGENTS.md")
    parser.add_argument("--runtime-dir", type=Path, default=Path.home() / ".veyyon/workflows")
    parser.add_argument("--check", action="store_true", help="Read-only byte parity check; exit 1 on drift")
    parser.add_argument(
        "--force-policy",
        action="store_true",
        help="Overwrite a profile AGENTS.md that changed after the last install (default: refuse)",
    )
    args = parser.parse_args(argv)
    return (
        0
        if synchronize(args.source_root, args.profile_path, args.runtime_dir, args.check, args.force_policy)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
