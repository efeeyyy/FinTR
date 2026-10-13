"""CLI: detect retroactive revisions in saved Yahoo price-history files (offline).

For each symbol under ``<price-dir>/<SYMBOL>/`` (``_incomplete/`` excluded) the
newest file, by ``capture_metadata.accessed_at_utc`` (fetch time, not the end
date in the file name), is compared with the previous one over the dates both
cover. Per date the report says: ``removed`` / ``added`` / ``marker_status_changed``
/ ``raw_ohlcv_changed`` / ``adjusted_close_changed``, with a severity (high,
medium, info; see ``fintr.data.price_revisions``). Differences below
``--tolerance`` (relative, default 1e-6) are float noise and ignored.

``--old FILE --new FILE`` compares one explicit pair instead. Nothing is
fetched, no price file is touched, and the report (JSON, ``--out``, never
overwritten) holds no price or volume value.

Exit codes: 0 no changes, 2 changes found (a warning, not an error),
1 error (bad input, unreadable file, ``--out`` exists).
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys

from fintr.data.price_revisions import (
    DEFAULT_TOLERANCE,
    PriceRevisionError,
    exit_code_for,
    run_pair,
    run_price_dir,
    text_summary,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRICE_DIR = ROOT / "data" / "interim" / "price_history"
SUMMARY_PREFIX = "PRICE_DIFF: "


def _tolerance(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("expected a number, e.g. 1e-6") from None
    if not parsed.is_finite() or parsed < 0:
        raise argparse.ArgumentTypeError("expected a finite number >= 0")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--price-dir", type=Path, default=DEFAULT_PRICE_DIR,
                        help="price history root (default: data/interim/price_history)")
    parser.add_argument("--out", type=Path, required=True,
                        help="JSON report path; refused if it already exists")
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="only these symbols (space or comma separated)")
    parser.add_argument("--old", type=Path, default=None, help="explicit older file (needs --new)")
    parser.add_argument("--new", type=Path, default=None, help="explicit newer file (needs --old)")
    parser.add_argument("--tolerance", type=_tolerance, default=DEFAULT_TOLERANCE,
                        help="relative difference below which a change is float noise "
                             "(default 1e-6)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (args.old is None) != (args.new is None):
        print("diff_price_history: --old and --new must be given together.", file=sys.stderr)
        return 1
    if args.old is not None and args.symbols:
        print("diff_price_history: --symbols cannot be combined with --old/--new.", file=sys.stderr)
        return 1
    if args.out.exists():
        print(f"diff_price_history: refusing to overwrite existing report: {args.out}",
              file=sys.stderr)
        return 1
    symbols = None
    if args.symbols:
        symbols = [part for item in args.symbols for part in item.split(",") if part.strip()]
    try:
        if args.old is not None:
            report = run_pair(args.old, args.new, args.tolerance)
        else:
            report = run_price_dir(args.price_dir, symbols, args.tolerance)
    except PriceRevisionError as exc:
        print(f"diff_price_history: {exc}", file=sys.stderr)
        return 1
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
    except FileExistsError:
        print(f"diff_price_history: refusing to overwrite existing report: {args.out}",
              file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"diff_price_history: cannot write report ({type(exc).__name__}).", file=sys.stderr)
        return 1
    print(text_summary(report))
    totals = report["totals"]
    severity = totals["by_severity"]
    print(f"{SUMMARY_PREFIX}changes={totals['changes']} high={severity['high']} "
          f"medium={severity['medium']} info={severity['info']} "
          f"errors={len(report['errors'])} report={args.out}")
    return exit_code_for(report)


if __name__ == "__main__":
    sys.exit(main())
