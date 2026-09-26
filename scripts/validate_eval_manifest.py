"""Validate an eval case manifest (and optional annotation records) offline.

No network, no model call. Checks structural validity of the manifest and
annotation formats (``fintr.evaluation.manifest``) and runs the leakage guard
(``fintr.evaluation.leakage_guard``) against the bundled development-material
registry. Prints a short report; exits nonzero on any violation.

This script does NOT certify human-reviewed gold quality, independent
permission/license verification, or genuinely held-out status beyond the
structural and leakage checks it runs. See docs/evaluation_protocol.md for
what remains human work.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from fintr.evaluation.leakage_guard import (
    DEFAULT_REGISTRY_PATH,
    find_leakage_violations,
    load_dev_registry,
)
from fintr.evaluation.manifest import ManifestError, parse_annotation, parse_manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate an eval manifest against the case/annotation formats and the "
            "development-material leakage guard."
        ),
    )
    parser.add_argument(
        "manifest", type=Path, help="path to a manifest JSON file (fintr_eval_manifest_v1)",
    )
    parser.add_argument(
        "--annotations", type=Path, default=None,
        help="optional directory of annotation JSON files (*.json, fintr_eval_annotation_v1)",
    )
    parser.add_argument(
        "--registry", type=Path, default=DEFAULT_REGISTRY_PATH,
        help="development-material registry JSON (default: bundled registry)",
    )
    return parser


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _family_horizon_breakdown(cases) -> dict[str, dict[str, int]]:
    families: dict[str, int] = {}
    horizons: dict[str, int] = {}
    for case in cases:
        for family in case.data_families:
            families[family] = families.get(family, 0) + 1
        for horizon in case.horizons:
            horizons[horizon] = horizons.get(horizon, 0) + 1
    return {"data_families": families, "horizons": horizons}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        manifest_document = _load_json(args.manifest)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"Validation failed: unreadable manifest ({exc})", file=sys.stderr)
        return 1
    try:
        cases = parse_manifest(manifest_document)
    except ManifestError as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1

    try:
        registry = load_dev_registry(args.registry)
    except ManifestError as exc:
        print(f"Validation failed: unreadable registry ({exc})", file=sys.stderr)
        return 1

    problems: list[str] = [
        f"leakage: {violation.case_id}: {violation.reason}"
        for violation in find_leakage_violations(cases, registry)
    ]

    annotations_by_case: dict[str, int] = {}
    if args.annotations is not None:
        if not args.annotations.is_dir():
            print(f"Validation failed: annotations dir not found: {args.annotations}", file=sys.stderr)
            return 1
        case_ids = {case.case_id for case in cases}
        for annotation_path in sorted(args.annotations.glob("*.json")):
            try:
                annotation = parse_annotation(_load_json(annotation_path))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ManifestError) as exc:
                problems.append(f"annotation {annotation_path.name}: {exc}")
                continue
            if annotation.case_id not in case_ids:
                problems.append(f"annotation {annotation_path.name}: case_id_not_in_manifest")
            annotations_by_case[annotation.case_id] = annotations_by_case.get(
                annotation.case_id, 0
            ) + 1
        duplicated = sorted(
            case_id for case_id, count in annotations_by_case.items() if count > 1
        )
        if duplicated:
            problems.append(f"multiple annotation files for case_id(s): {duplicated}")

    breakdown = _family_horizon_breakdown(cases)
    print(f"Manifest: {args.manifest}")
    print(f"Cases: {len(cases)}")
    print(f"Data family breakdown: {breakdown['data_families']}")
    print(f"Horizon breakdown: {breakdown['horizons']}")
    if args.annotations is not None:
        annotated = sum(1 for case in cases if annotations_by_case.get(case.case_id, 0) > 0)
        print(f"Cases with an annotation file: {annotated}/{len(cases)}")

    if problems:
        print("Violations:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(
        "No structural or leakage violations found. This does not certify "
        "human-reviewed gold quality, independent permission verification, or "
        "held-out status beyond the checks above; real gold annotation, second "
        "review, baseline answers, and a versioned metric artifact remain human work."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
