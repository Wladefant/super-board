"""Install this reviewable GitHub-native bundle; --check detects source/runtime drift.

Only enumerated code and policy files are replaced, never state, credentials,
configuration or processes. Run from a committed source checkout after opening
its PR. Re-run --check against that same pinned checkout before further edits.
"""
from __future__ import annotations

import argparse
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
    # Verification CLI and smoke test gate.
    "verify.py", "test_verify.py",
)
POLICY = Path("policies/default/AGENTS.md")


def synchronize(source_root: Path, profile: Path, runtime: Path, check: bool = False) -> bool:
    pairs = [(source_root / POLICY, profile)] + [
        (source_root / "workflows/portable" / name, runtime / name) for name in RUNTIME_FILES
    ]
    # Read every source before any mutation; an incomplete checkout changes nothing.
    payloads = [(target, source.read_bytes()) for source, target in pairs]
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
    digest = hashlib.sha256(payloads[0][1]).hexdigest()
    print(f"MATCH: {len(payloads)} source/installed files; policy sha256={digest}")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--profile-path", type=Path, default=Path.home() / ".veyyon/profiles/default/agent/AGENTS.md")
    parser.add_argument("--runtime-dir", type=Path, default=Path.home() / ".veyyon/workflows")
    parser.add_argument("--check", action="store_true", help="Read-only byte parity check; exit 1 on drift")
    args = parser.parse_args(argv)
    return 0 if synchronize(args.source_root, args.profile_path, args.runtime_dir, args.check) else 1


if __name__ == "__main__":
    raise SystemExit(main())
