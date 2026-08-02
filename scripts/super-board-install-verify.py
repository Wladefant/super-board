#!/usr/bin/env python3
"""super-board-install-verify.py — install the pinned payload, and prove it.

    install    copy the complete payload and write the install manifest
    verify     compare the install manifest against the installed tree
    snapshot   write a deterministic path-and-checksum snapshot of that tree
    compare    diff two snapshots — the accepted idempotency proof

`install.sh` is the operator-facing entry point; it validates the release
contract and then delegates here, because copying, checksumming, executable
bits, and stale-file pruning must have exactly one implementation.

The idempotency proof is `snapshot` → reinstall → `snapshot` → `compare`. It is
deliberately NOT a diff of the working tree against the pre-install checkout:
that comparison is dominated by the first install's own output, so it can read
clean while the second install rewrites half the payload.

Usage:
    super-board-install-verify.py install  --source-root PATH --repo-root PATH
                                           --user-home PATH --source-sha SHA
                                           --release-version VERSION
                                           --design-skill-source URL
                                           --design-skill-sha SHA
                                           --design-skill-checksum SHA256
                                           [--slug NAME] [--allow-downgrade] [--json]
    super-board-install-verify.py verify   --manifest PATH --repo-root PATH [--json]
    super-board-install-verify.py snapshot --manifest PATH --repo-root PATH --out FILE
    super-board-install-verify.py compare  --first FILE --second FILE [--json]

Exit: 0 ok · 64 invalid invocation · 65 the install contract was not satisfied,
      verification failed, or the two snapshots disagree.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime import EXIT_CONFIG, EXIT_OK, EXIT_USAGE  # noqa: E402
from super_board_runtime.install_manifest import (  # noqa: E402
    InstallError,
    InstallTreeSnapshot,
    compare_install_snapshots,
    install_payload,
    read_manifest,
    snapshot_install_tree,
    verify_install_manifest,
    verify_source_sha,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        print(f"super-board-install-verify: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="super-board-install-verify.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="copy the payload and write the manifest")
    install.add_argument("--source-root", required=True)
    install.add_argument("--repo-root", required=True)
    install.add_argument("--user-home", required=True)
    install.add_argument("--source-sha", required=True)
    install.add_argument("--release-version", required=True)
    install.add_argument("--design-skill-source", required=True)
    install.add_argument("--design-skill-sha", required=True)
    install.add_argument("--design-skill-checksum", required=True)
    install.add_argument("--slug", default=None)
    install.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="documented override: install a release older than the installed one",
    )
    install.add_argument("--skip-source-check", action="store_true",
                         help="the caller already proved the source HEAD")

    verify = sub.add_parser("verify", help="compare the manifest against the tree")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--repo-root", required=True)

    snapshot = sub.add_parser("snapshot", help="write a path-and-checksum snapshot")
    snapshot.add_argument("--manifest", required=True)
    snapshot.add_argument("--repo-root", required=True)
    snapshot.add_argument("--out", required=True)

    compare = sub.add_parser("compare", help="diff two snapshots")
    compare.add_argument("--first", required=True)
    compare.add_argument("--second", required=True)

    for command in (install, verify, snapshot, compare):
        command.add_argument(
            "--json", action="store_true", help="accepted for symmetry; output is always JSON"
        )
    return parser


def _read_snapshot(path: str) -> InstallTreeSnapshot:
    body = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = body.get("entries") if isinstance(body, dict) else None
    if not isinstance(entries, dict):
        raise InstallError("install-snapshot-invalid", f"{path} is not a tree snapshot")
    return InstallTreeSnapshot(entries=entries)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "install":
            if not args.skip_source_check:
                verify_source_sha(Path(args.source_root), args.source_sha)
            manifest = install_payload(
                Path(args.source_root),
                Path(args.repo_root),
                source_sha=args.source_sha,
                release_version=args.release_version,
                design_skill_source=args.design_skill_source,
                design_skill_sha=args.design_skill_sha,
                design_skill_checksum=args.design_skill_checksum,
                user_home=args.user_home,
                slug=args.slug,
                allow_downgrade=args.allow_downgrade,
            )
            print(
                json.dumps(
                    {
                        "installed_files": len(manifest.files),
                        "ok": True,
                        "release_version": manifest.release_version,
                        "source_sha": manifest.source_sha,
                    },
                    sort_keys=True,
                )
            )
            return EXIT_OK

        if args.command == "verify":
            report = verify_install_manifest(Path(args.manifest), Path(args.repo_root))
            body = json.dumps(report.to_dict(), sort_keys=True)
            if report.ok:
                print(body)
                return EXIT_OK
            print(body, file=sys.stderr)
            print(
                "🛑 super-board-install-verify: the installed tree does not match its "
                "manifest.",
                file=sys.stderr,
            )
            return EXIT_CONFIG

        if args.command == "snapshot":
            manifest = read_manifest(Path(args.manifest))
            snapshot = snapshot_install_tree(Path(args.repo_root), manifest)
            text = json.dumps(snapshot.to_dict(), indent=2, sort_keys=True) + "\n"
            Path(args.out).write_text(text, encoding="utf-8")
            print(json.dumps({"entries": len(snapshot.entries), "ok": True}, sort_keys=True))
            return EXIT_OK

        first = _read_snapshot(args.first)
        second = _read_snapshot(args.second)
        drift = compare_install_snapshots(first, second)
        body = json.dumps({**drift.to_dict(), "ok": drift.clean}, sort_keys=True)
        if drift.clean:
            print(body)
            return EXIT_OK
        print(body, file=sys.stderr)
        return EXIT_CONFIG

    except InstallError as exc:
        print(f"super-board-install-verify: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": exc.reason}, sort_keys=True), file=sys.stderr)
        return exc.exit_code
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"super-board-install-verify: invalid input: {exc}", file=sys.stderr)
        print(
            json.dumps({"ok": False, "reason": "install-input-invalid"}, sort_keys=True),
            file=sys.stderr,
        )
        return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
