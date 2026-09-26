#!/usr/bin/env python3
"""
feature_map.py - Machine-readable feature map generator, validator, and navigator.

Part of portable workflow core in Wladefant/super-board.
References:
  - Superboard Issue #227 (High-Trust Agent Architecture & Verification Skills)
  - Superboard Issue #244 (Feature map for agent navigation)
  - Adopt / Adapt / Build Master Matrix: Idea 2 (Feature Map as Materialized Memory)

Responsibilities:
  1. Defines data models for feature map entries (entry files, tests, owning issue, risk).
  2. Loads and parses JSON (and optional YAML) feature map files.
  3. Validates feature map structure, required fields, and filesystem path existence.
     FAILS with exit code 1 if any mapped entry_file, test, or doc file does not exist.
  4. Provides CLI commands for:
     - `validate`: Strict filesystem and schema verification (used in CI).
     - `query`: Fast keyword / symbol / path lookup for agent navigation.
     - `show`: Detailed inspection of a single feature.
     - `list`: Formatted overview of all registered features.
     - `generate`: Scaffolding new feature map entries.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Supported risk levels per Issue #195 & Issue #244 conventions
ALLOWED_RISK_LEVELS = ("low", "medium", "high")


@dataclass
class FeatureEntry:
    """Represents a single registered feature in the feature map."""
    name: str
    description: str
    entry_files: List[str]
    tests: List[str]
    owning_issue: str
    risk: str
    components: List[str] = field(default_factory=list)
    cli_command: str = ""
    docs: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert entry to dictionary with clean formatting."""
        d: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "entry_files": list(self.entry_files),
            "tests": list(self.tests),
            "owning_issue": self.owning_issue,
            "risk": self.risk,
        }
        if self.components:
            d["components"] = list(self.components)
        if self.cli_command:
            d["cli_command"] = self.cli_command
        if self.docs:
            d["docs"] = list(self.docs)
        if self.dependencies:
            d["dependencies"] = list(self.dependencies)
        return d


@dataclass
class ValidationResult:
    """Detailed validation outcome."""
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    feature_count: int = 0
    file_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "feature_count": self.feature_count,
            "file_count": self.file_count,
        }


class FeatureMap:
    """Container and query interface for a machine-readable feature map."""

    def __init__(
        self,
        version: str = "1.0.0",
        repository: str = "",
        description: str = "",
        features: Optional[Dict[str, FeatureEntry]] = None,
        schema: str = "workflows/portable/feature_map.schema.json",
    ):
        self.version = version
        self.repository = repository
        self.description = description
        self.features: Dict[str, FeatureEntry] = features or {}
        self.schema = schema

    def get(self, feature_id: str) -> Optional[FeatureEntry]:
        """Get feature by exact key."""
        return self.features.get(feature_id)

    def find_by_file(self, file_path: str) -> List[Tuple[str, FeatureEntry]]:
        """Find features referencing the given file path in entry_files, tests, or docs."""
        normalized = file_path.replace("\\", "/").strip().lstrip("./")
        results: List[Tuple[str, FeatureEntry]] = []
        for fid, entry in self.features.items():
            all_files = entry.entry_files + entry.tests + entry.docs
            for f in all_files:
                norm_f = f.replace("\\", "/").strip().lstrip("./")
                if norm_f == normalized or norm_f.endswith("/" + normalized) or normalized.endswith("/" + norm_f):
                    results.append((fid, entry))
                    break
        return results

    def query(self, search_term: str) -> List[Tuple[str, FeatureEntry]]:
        """Search features by ID, name, description, file path, or component."""
        term = search_term.strip().lower()
        if not term:
            return list(self.features.items())

        matches: List[Tuple[str, FeatureEntry]] = []
        for fid, entry in self.features.items():
            # Check ID
            if term in fid.lower():
                matches.append((fid, entry))
                continue
            # Check name
            if term in entry.name.lower():
                matches.append((fid, entry))
                continue
            # Check description
            if term in entry.description.lower():
                matches.append((fid, entry))
                continue
            # Check entry files and tests
            found_file = False
            for f in entry.entry_files + entry.tests + entry.docs:
                if term in f.lower():
                    matches.append((fid, entry))
                    found_file = True
                    break
            if found_file:
                continue
            # Check components
            for c in entry.components:
                if term in c.lower():
                    matches.append((fid, entry))
                    break

        return matches

    def validate(self, repo_root: Path) -> ValidationResult:
        """Validate structure and filesystem existence of all mapped files."""
        return validate_feature_map(self, repo_root)

    def to_dict(self) -> Dict[str, Any]:
        """Export map to dictionary matching schema."""
        d: Dict[str, Any] = {
            "$schema": self.schema,
            "version": self.version,
        }
        if self.repository:
            d["repository"] = self.repository
        if self.description:
            d["description"] = self.description
        d["features"] = {fid: entry.to_dict() for fid, entry in self.features.items()}
        return d

    def to_json(self, indent: int = 2) -> str:
        """Serialize map to JSON string."""
        return json.dumps(self.to_dict(), indent=indent) + "\n"


def auto_detect_repo_root() -> Path:
    """Detect repository root from working directory or ancestors."""
    cwd = Path.cwd().resolve()
    # Check current directory for git or FEATURE_MAP.json
    for p in [cwd] + list(cwd.parents):
        if (p / "FEATURE_MAP.json").is_file() or (p / ".git").is_dir():
            return p
    return cwd


def load_feature_map(
    path: Optional[Path | str] = None,
    repo_root: Optional[Path | str] = None,
) -> FeatureMap:
    """
    Loads a FeatureMap from the specified path or standard candidate locations.
    """
    root = Path(repo_root).resolve() if repo_root else auto_detect_repo_root()

    map_path: Optional[Path] = None
    if path:
        p = Path(path)
        map_path = p if p.is_absolute() else (root / p)
    else:
        candidates = [
            root / "FEATURE_MAP.json",
            root / "feature_map.json",
            root / "workflows" / "portable" / "FEATURE_MAP.json",
            Path.cwd() / "FEATURE_MAP.json",
        ]
        for c in candidates:
            if c.is_file():
                map_path = c
                break

    if not map_path or not map_path.is_file():
        raise FileNotFoundError(
            f"Feature map not found at {path or 'default candidates (e.g. FEATURE_MAP.json)'} under root {root}"
        )

    raw_text = map_path.read_text(encoding="utf-8")

    # Try JSON
    data: Dict[str, Any]
    if map_path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(raw_text)
        except ImportError:
            raise RuntimeError(
                f"YAML feature map '{map_path}' requires PyYAML. Install PyYAML or use JSON format."
            )
    else:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Feature map at {map_path} is invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Feature map root must be a JSON/YAML object, got {type(data).__name__}")

    version = str(data.get("version", "1.0.0"))
    repository = str(data.get("repository", ""))
    description = str(data.get("description", ""))
    schema = str(data.get("$schema", "workflows/portable/feature_map.schema.json"))

    raw_features = data.get("features", {})
    if not isinstance(raw_features, dict):
        raise ValueError(f"Feature map 'features' must be a dictionary, got {type(raw_features).__name__}")

    features: Dict[str, FeatureEntry] = {}
    for fid, fdef in raw_features.items():
        if not isinstance(fdef, dict):
            continue
        features[fid] = FeatureEntry(
            name=str(fdef.get("name", "")),
            description=str(fdef.get("description", "")),
            entry_files=[str(x) for x in fdef.get("entry_files", [])],
            tests=[str(x) for x in fdef.get("tests", [])],
            owning_issue=str(fdef.get("owning_issue", "")),
            risk=str(fdef.get("risk", "medium")).lower(),
            components=[str(x) for x in fdef.get("components", [])],
            cli_command=str(fdef.get("cli_command", "")),
            docs=[str(x) for x in fdef.get("docs", [])],
            dependencies=[str(x) for x in fdef.get("dependencies", [])],
        )

    return FeatureMap(
        version=version,
        repository=repository,
        description=description,
        features=features,
        schema=schema,
    )


def validate_feature_map(feature_map: FeatureMap, repo_root: Path) -> ValidationResult:
    """
    Strict validation of feature map:
      1. Structural constraints (non-empty fields, valid risk enum).
      2. Filesystem existence: Every mapped entry_file, test, and doc path MUST exist on disk.
    """
    errors: List[str] = []
    warnings: List[str] = []
    total_files_checked = 0

    if not feature_map.version:
        errors.append("Feature map 'version' must not be empty.")

    if not feature_map.features:
        errors.append("Feature map 'features' must contain at least one feature.")
        return ValidationResult(valid=False, errors=errors, warnings=warnings, feature_count=0, file_count=0)

    repo_root = repo_root.resolve()

    for fid, entry in feature_map.features.items():
        prefix = f"Feature '{fid}'"

        # Check ID naming convention (alphanumeric, kebab, snake)
        if not re.match(r"^[a-zA-Z0-9_\-]+$", fid):
            errors.append(f"{prefix}: identifier must contain only alphanumeric, dash, or underscore characters.")

        # Check required string fields
        if not entry.name.strip():
            errors.append(f"{prefix}: 'name' is required and cannot be empty.")
        if not entry.description.strip():
            errors.append(f"{prefix}: 'description' is required and cannot be empty.")
        if not entry.owning_issue.strip():
            errors.append(f"{prefix}: 'owning_issue' is required and cannot be empty.")

        # Check risk level
        if entry.risk not in ALLOWED_RISK_LEVELS:
            errors.append(
                f"{prefix}: invalid risk level '{entry.risk}'. Must be one of: {', '.join(ALLOWED_RISK_LEVELS)}."
            )

        # Check entry files
        if not entry.entry_files:
            errors.append(f"{prefix}: 'entry_files' must contain at least one file.")
        else:
            for ef in entry.entry_files:
                total_files_checked += 1
                full_path = (repo_root / ef).resolve()
                if not full_path.exists():
                    errors.append(f"{prefix}: entry_file '{ef}' does not exist on disk at '{full_path}'.")

        # Check tests
        if not entry.tests:
            errors.append(f"{prefix}: 'tests' must contain at least one test file.")
        else:
            for tf in entry.tests:
                total_files_checked += 1
                full_path = (repo_root / tf).resolve()
                if not full_path.exists():
                    errors.append(f"{prefix}: test file '{tf}' does not exist on disk at '{full_path}'.")

        # Check docs (optional, but if listed must exist)
        for df in entry.docs:
            total_files_checked += 1
            full_path = (repo_root / df).resolve()
            if not full_path.exists():
                errors.append(f"{prefix}: doc file '{df}' does not exist on disk at '{full_path}'.")

    valid = len(errors) == 0
    return ValidationResult(
        valid=valid,
        errors=errors,
        warnings=warnings,
        feature_count=len(feature_map.features),
        file_count=total_files_checked,
    )


def generate_feature_entry(
    feature_id: str,
    name: str,
    description: str,
    entry_files: Sequence[str],
    tests: Sequence[str],
    owning_issue: str,
    risk: str = "medium",
    components: Optional[Sequence[str]] = None,
    cli_command: str = "",
    docs: Optional[Sequence[str]] = None,
) -> FeatureEntry:
    """Helper to construct a validated FeatureEntry instance."""
    clean_risk = risk.lower().strip()
    if clean_risk not in ALLOWED_RISK_LEVELS:
        raise ValueError(f"Invalid risk '{risk}'. Allowed: {ALLOWED_RISK_LEVELS}")

    return FeatureEntry(
        name=name.strip(),
        description=description.strip(),
        entry_files=[f.replace("\\", "/").strip() for f in entry_files if f.strip()],
        tests=[f.replace("\\", "/").strip() for f in tests if f.strip()],
        owning_issue=owning_issue.strip(),
        risk=clean_risk,
        components=list(components or []),
        cli_command=cli_command.strip(),
        docs=[f.replace("\\", "/").strip() for f in (docs or []) if f.strip()],
    )


# ==============================================================================
# CLI Entrypoint
# ==============================================================================

def format_feature_table(features: Sequence[Tuple[str, FeatureEntry]]) -> str:
    """Formats a list of feature tuples for terminal display."""
    if not features:
        return "No matching features found."

    lines = [
        f"{'ID':<15} {'RISK':<8} {'TESTS':<6} {'NAME':<35} {'OWNING ISSUE'}",
        "-" * 80,
    ]
    for fid, feat in features:
        test_cnt = len(feat.tests)
        issue = feat.owning_issue.split("/")[-1] if "/" in feat.owning_issue else feat.owning_issue
        lines.append(f"{fid:<15} {feat.risk:<8} {test_cnt:<6} {feat.name[:33]:<35} {issue}")
    return "\n".join(lines)


def format_feature_detail(fid: str, feat: FeatureEntry) -> str:
    """Formats a single feature with full details for terminal display."""
    lines = [
        f"Feature ID    : {fid}",
        f"Name          : {feat.name}",
        f"Risk Level    : {feat.risk.upper()}",
        f"Owning Issue  : {feat.owning_issue}",
        f"Description   : {feat.description}",
        f"CLI Command   : {feat.cli_command or '(none)'}",
        "",
        "Entry Files:",
    ]
    for ef in feat.entry_files:
        lines.append(f"  - {ef}")

    lines.append("")
    lines.append("Tests:")
    for tf in feat.tests:
        lines.append(f"  - {tf}")

    if feat.components:
        lines.append("")
        lines.append("Components:")
        for c in feat.components:
            lines.append(f"  - {c}")

    if feat.docs:
        lines.append("")
        lines.append("Docs:")
        for d in feat.docs:
            lines.append(f"  - {d}")

    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "--map",
        type=str,
        default=None,
        help="Path to feature map file (default: auto-detected FEATURE_MAP.json)",
    )
    common_parser.add_argument(
        "--repo-root",
        type=str,
        default=None,
        help="Repository root directory (default: auto-detected from git or working directory)",
    )
    common_parser.add_argument(
        "--json",
        action="store_true",
        help="Output results in JSON format",
    )

    parser = argparse.ArgumentParser(
        description="Feature Map Navigator: Machine-readable feature map query and validator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common_parser],
    )

    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # Command: validate
    subparsers.add_parser(
        "validate",
        help="Validate feature map schema and verify that all mapped files exist on disk.",
        parents=[common_parser],
    )

    # Command: query
    query_parser = subparsers.add_parser(
        "query",
        help="Search features by keyword, feature ID, or referenced file path.",
        parents=[common_parser],
    )
    query_parser.add_argument("term", type=str, help="Search term (feature ID, path, or keyword)")

    # Command: show
    show_parser = subparsers.add_parser(
        "show",
        help="Display detailed entry for a specific feature ID.",
        parents=[common_parser],
    )
    show_parser.add_argument("feature_id", type=str, help="Target feature ID")

    # Command: list
    subparsers.add_parser(
        "list",
        help="List all registered features.",
        parents=[common_parser],
    )

    # Command: generate
    gen_parser = subparsers.add_parser(
        "generate",
        help="Scaffold or append a new feature entry.",
        parents=[common_parser],
    )
    gen_parser.add_argument("--id", required=True, help="Unique feature ID (e.g. 'routing')")
    gen_parser.add_argument("--name", required=True, help="Human-readable name")
    gen_parser.add_argument("--description", required=True, help="Feature description")
    gen_parser.add_argument("--entry", required=True, nargs="+", help="Entrypoint file path(s)")
    gen_parser.add_argument("--tests", required=True, nargs="+", help="Test file path(s)")
    gen_parser.add_argument("--issue", required=True, help="Owning issue URL or number")
    gen_parser.add_argument("--risk", choices=ALLOWED_RISK_LEVELS, default="medium", help="Risk level")
    gen_parser.add_argument("--components", nargs="*", default=[], help="Key components or classes")
    gen_parser.add_argument("--cli", default="", help="Canonical CLI command")
    gen_parser.add_argument("--docs", nargs="*", default=[], help="Related documentation paths")
    gen_parser.add_argument("--append", action="store_true", help="Append directly to map file")

    return parser

def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else auto_detect_repo_root()

    if not args.command or args.command == "validate":
        # Default action: validate
        try:
            fmap = load_feature_map(path=args.map, repo_root=repo_root)
        except Exception as exc:
            if args.json:
                print(json.dumps({"valid": False, "errors": [str(exc)], "warnings": [], "feature_count": 0, "file_count": 0}))
            else:
                print(f"[FAIL] Unable to load feature map: {exc}", file=sys.stderr)
            return 1

        result = fmap.validate(repo_root=repo_root)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            if result.valid:
                print(
                    f"[PASS] Feature map valid: {result.feature_count} features, "
                    f"{result.file_count} mapped files verified on disk."
                )
            else:
                print(f"[FAIL] Feature map validation failed ({len(result.errors)} errors):", file=sys.stderr)
                for err in result.errors:
                    print(f"  - {err}", file=sys.stderr)

        return 0 if result.valid else 1

    elif args.command == "query":
        try:
            fmap = load_feature_map(path=args.map, repo_root=repo_root)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        matches = fmap.query(args.term)
        if args.json:
            out = {fid: feat.to_dict() for fid, feat in matches}
            print(json.dumps(out, indent=2))
        else:
            print(format_feature_table(matches))
        return 0

    elif args.command == "show":
        try:
            fmap = load_feature_map(path=args.map, repo_root=repo_root)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        feat = fmap.get(args.feature_id)
        if not feat:
            print(f"Error: Feature '{args.feature_id}' not found in feature map.", file=sys.stderr)
            return 1

        if args.json:
            print(json.dumps(feat.to_dict(), indent=2))
        else:
            print(format_feature_detail(args.feature_id, feat))
        return 0

    elif args.command == "list":
        try:
            fmap = load_feature_map(path=args.map, repo_root=repo_root)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        all_feats = list(fmap.features.items())
        if args.json:
            out = {fid: feat.to_dict() for fid, feat in all_feats}
            print(json.dumps(out, indent=2))
        else:
            print(format_feature_table(all_feats))
        return 0

    elif args.command == "generate":
        try:
            entry = generate_feature_entry(
                feature_id=args.id,
                name=args.name,
                description=args.description,
                entry_files=args.entry,
                tests=args.tests,
                owning_issue=args.issue,
                risk=args.risk,
                components=args.components,
                cli_command=args.cli,
                docs=args.docs,
            )
        except Exception as exc:
            print(f"Error generating feature entry: {exc}", file=sys.stderr)
            return 1

        if args.append:
            try:
                fmap = load_feature_map(path=args.map, repo_root=repo_root)
                fmap.features[args.id] = entry
                out_path = Path(args.map).resolve() if args.map else (repo_root / "FEATURE_MAP.json")
                out_path.write_text(fmap.to_json(), encoding="utf-8")
                print(f"Successfully appended feature '{args.id}' to {out_path}")
            except Exception as exc:
                print(f"Error appending to feature map: {exc}", file=sys.stderr)
                return 1
        else:
            print(json.dumps({args.id: entry.to_dict()}, indent=2))

        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
