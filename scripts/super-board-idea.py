#!/usr/bin/env python3
"""Single-sentence idea intake. Dry-run unless --file is explicit."""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from super_board_runtime.idea import decompose, file_drafts


def command(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True,
                          encoding="utf-8", timeout=240, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sentence")
    parser.add_argument("--repo", default="Wladefant/super-board")
    parser.add_argument("--context", required=True, help="Target scope, branch, milestone and test surface")
    parser.add_argument("--draft-model", required=True, help="An allowed model from the host profile")
    parser.add_argument("--judge-model", required=True, help="An independently configured judge model")
    parser.add_argument("--output", required=True)
    parser.add_argument("--file", action="store_true")
    parser.add_argument("--label", action="append", default=[])
    parser.add_argument("--project", help="Target GitHub Project title (required for filing)")
    args = parser.parse_args()
    if args.file and not args.project:
        parser.error("--file requires --project")

    def model(stage, prompt):
        selected = args.draft_model if stage == "draft" else args.judge_model
        return command(["veyyon", "-p", "--no-tools", "--no-extensions", "--no-skills",
                        "--no-rules", "--no-session", "--no-title", "--max-time=210",
                        "--model", selected, "--system-prompt", "Return only requested JSON.", prompt])

    result = decompose(args.sentence, model, context=args.context)
    output = Path(args.output)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if args.file:
        def create(issue):
            with tempfile.TemporaryDirectory() as directory:
                body = Path(directory) / "body.txt"
                body.write_text(issue["body"], encoding="utf-8")
                argv = ["gh", "issue", "create", "--repo", args.repo, "--title", issue["title"],
                        "--body-file", str(body), "--project", args.project]
                for label in args.label:
                    argv.extend(["--label", label])
                url = command(argv)
                return url
        try:
            file_drafts(result, create)
        finally:
            # Preserve receipts even if a later GitHub write fails.
            output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
