"""CLI: weekly replay reconciliation of saved scanner observations (E3).

Re-runs the frozen scanner on stored data for the last N saved as_of dates
(or an explicit list) and compares the result with the saved
``data/interim/scanner_daily/<as_of>/observations.json``. Differences are code
drift, a rule change or a data revision; they are detected, classified per
symbol and reported, never silently ignored. No network, no Telegram, no model
call, and it reads no study data. See ``docs/scanner.md`` ("Haftalık replay").

Not part of the daily pipeline; run it weekly (e.g. Saturday).

Modes:
  point-in-time  only data that existed at the original ``generated_at_utc``
                 (scanner ``--data-cutoff-utc``); any difference is drift.
  current        all data on disk now; differences = data revisions + drift.
  both           (default) run both.

Exit code: 0 = every unit identical, 2 = differences, 1 = error (an error
takes precedence over differences). Last stdout line:
``REPLAY: as_of=K identical=I diff=D errors=E`` where identical/diff/errors
count (as_of, mode) units.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path
import sys

from fintr.scanner import replay

PROJECT_ROOT = Path(__file__).resolve().parents[1]


DEFAULT_SCANNER_DIR = Path("data/interim/scanner_daily")
DEFAULT_PERIOD_DIR = Path("data/interim/period_analysis")
DEFAULT_DAILY_ROOT = Path("data/interim/daily_capture")
DEFAULT_PRICE_DIR = Path("data/interim/price_history")
DEFAULT_OUT_ROOT = Path("data/interim/scanner_replay")
REPORT_NAME = "replay_report.json"


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def _as_of_list(value: str) -> list[str]:
    labels = [item.strip() for item in value.split(",") if item.strip()]
    if not labels:
        raise argparse.ArgumentTypeError("expected a comma-separated list of YYYY-MM-DD")
    for label in labels:
        try:
            date.fromisoformat(label)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {label!r}") from exc
    return labels


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Weekly replay of the frozen scanner against saved observations.json "
                     "files; detects and classifies drift. See docs/scanner.md.",
    )
    parser.add_argument("--last", type=_positive_int, default=5,
                         help="replay the last N saved as_of dates (default: 5)")
    parser.add_argument("--as-of", type=_as_of_list, default=None,
                         help="comma-separated YYYY-MM-DD list instead of --last")
    parser.add_argument("--mode", choices=("point-in-time", "current", "both"), default="both")
    parser.add_argument("--scanner-dir", type=Path, default=DEFAULT_SCANNER_DIR,
                         help=f"default: {DEFAULT_SCANNER_DIR}")
    parser.add_argument("--period-dir", type=Path, default=DEFAULT_PERIOD_DIR,
                         help=f"default: {DEFAULT_PERIOD_DIR}")
    parser.add_argument("--daily-root", type=Path, default=DEFAULT_DAILY_ROOT,
                         help=f"default: {DEFAULT_DAILY_ROOT}")
    parser.add_argument("--price-dir", type=Path, default=DEFAULT_PRICE_DIR,
                         help=f"default: {DEFAULT_PRICE_DIR}")
    parser.add_argument(
        "--out", type=Path, default=None,
        help=f"JSON report path; refuses to overwrite. Default: "
             f"{DEFAULT_OUT_ROOT}/<UTC stamp>/{REPORT_NAME}",
    )
    parser.add_argument("--max-diffs", type=_positive_int, default=replay.DEFAULT_MAX_DIFFS_PER_UNIT,
                         help="max listed field differences per unit in the report (counts are "
                              f"always complete; default: {replay.DEFAULT_MAX_DIFFS_PER_UNIT})")
    return parser


def main(argv: list[str] | None = None, *, runner: replay.ScanRunner | None = None) -> int:
    args = _parser().parse_args(argv)

    out = args.out
    if out is None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        out = DEFAULT_OUT_ROOT / stamp / REPORT_NAME
    try:
        replay.check_report_target(out)
    except FileExistsError:
        print(f"Replay failed: report file already exists (no overwrite): {out}", file=sys.stderr)
        return replay.EXIT_ERROR

    modes = replay.MODES if args.mode == "both" else (args.mode,)
    config = replay.ReplayConfig(
        scanner_dir=args.scanner_dir, period_dir=args.period_dir, price_dir=args.price_dir,
        daily_root=args.daily_root, as_of=tuple(args.as_of or ()), last_n=args.last,
        modes=tuple(modes), project_root=PROJECT_ROOT, max_diffs_per_unit=args.max_diffs,
    )
    try:
        report = replay.run_replay(config, runner or replay.run_scan_script)
    except replay.ReplayError as exc:
        print(f"Replay failed: {exc}", file=sys.stderr)
        return replay.EXIT_ERROR

    try:
        replay.write_report(out, report)
    except (FileExistsError, OSError) as exc:
        print(f"Replay failed: could not write report ({exc})", file=sys.stderr)
        return replay.EXIT_ERROR

    for unit in report["units"]:
        print(replay.unit_line(unit))
    print(f"Report: {out}")
    print(replay.summary_line(report))
    return replay.exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
