"""Apply the pre-registered Holm family decision to saved study results.

Reads ``fintr_study_result_v1`` JSON files written by ``scripts/run_study.py
--save`` (one per hypothesis, same split and run parameters) and applies
``fintr.research.family.evaluate_primary_family``: Holm over the specs'
``primary_family_members`` only; ``secondary_descriptive`` hypotheses are
listed without a Holm decision. No network, no model call, no new return
statistic. Not investment advice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from fintr.research.family import FamilyError, evaluate_primary_family


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Holm decision over one pre-registered study family (offline).",
    )
    parser.add_argument("--result", type=Path, action="append", required=True, dest="results",
                        help="repeatable; one saved study result JSON per hypothesis")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--out", type=Path, help="write the JSON decision here; never overwrites")
    return parser


def _render_text(decision: dict) -> str:
    lines = [
        f"Aile: {decision['family_id']} split={decision['split']} "
        f"(doğrulayıcı: {'evet' if decision['confirmatory'] else 'hayır'}), "
        f"Holm m={decision['m']}, α={decision['familywise_alpha']}",
    ]
    for row in decision["primary"]:
        holm = row["holm"]
        lines.append(
            f"  {row['hypothesis_id']}: p={row['p_value_for_holm']} sıra={holm['rank']} "
            f"eşik={holm['threshold']} ret={'evet' if holm['rejected'] else 'hayır'} "
            f"sonuç={row['outcome']}"
        )
    for row in decision["secondary_descriptive"]:
        lines.append(
            f"  {row['hypothesis_id']} (ikincil/tanımlayıcı, Holm dışı): "
            f"p_düzeltilmemiş={row['p_value_one_sided_unadjusted_descriptive']}"
        )
    lines += ["", "Sınırlar:"] + [f"  - {text}" for text in decision["constraints"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    results = []
    hashes = {}
    for path in args.results:
        try:
            raw = path.read_bytes()
            results.append(json.loads(raw.decode("utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"Family evaluation failed: unreadable result {path} ({exc})", file=sys.stderr)
            return 1
        hashes[str(path)] = hashlib.sha256(raw).hexdigest()
    try:
        decision = evaluate_primary_family(results)
    except FamilyError as exc:
        print(f"Family evaluation failed: {exc}", file=sys.stderr)
        return 1
    decision["input_result_sha256"] = hashes
    payload = (json.dumps(decision, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if args.out is not None:
        try:
            with args.out.open("xb") as handle:
                handle.write(payload)
        except FileExistsError:
            print(f"Family evaluation failed: {args.out} exists (never overwritten)", file=sys.stderr)
            return 1
    if args.format == "json":
        sys.stdout.buffer.write(payload)
    else:
        sys.stdout.buffer.write(_render_text(decision).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
