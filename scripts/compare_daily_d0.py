"""CLI: D0 comparison of daily rolling captures against a one-year base capture.

ADR ``docs/research/daily_miniapp_collection_adr.md`` "Doğrulama": the days of
the daily rolling-window captures (``<daily-root>/<SYM>/<S>_<E>.json``) that
fall on or before ``D0_CUTOFF_DATE`` are compared, per (date, family), with
the base capture ``<base-root>/<SYM>/2025-09-26_2026-09-25.json``: is the
canonical row-set hash equal, and if not, how many institution codes differ.

Reuse, not reimplementation: captures are loaded with the validated
``load_period_capture`` (packet-hash/integrity check; a mismatch fails only
that symbol with ``failed:<category>``); the daily side of a (date, family)
is the ``compose_period_capture`` winner among the daily captures (latest
``captured_at_utc``), so this script and the composer agree on which daily
row "the" row is; row hashes and the differing-code count come from the
composer's public ``family_row_hash`` / ``differing_code_count``.

Output is counts only: no lot, net, price or ratio value and no institution
code list is ever printed or written. The JSON report goes to
``<daily-root>/_d0/<UTC timestamp>.json`` (created ``O_EXCL``, never
overwritten); ``--dry-run`` lists the files that would be compared and
writes nothing. A (date, family) present on one side only is counted as
``only_base`` / ``only_daily`` and is not part of the match rate.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from fintr.data.period_capture import PeriodCapture, PeriodCaptureError, load_period_capture
from fintr.evaluation.live_capture import LivePilotCaptureError, validate_context_contains_no_secrets
from fintr.evaluation.symbol_guard import SymbolGuardError, load_active_guards
from fintr.research.compose_period_capture import (
    CaptureInput,
    CompositionError,
    compose_period_capture,
    differing_code_count,
    family_days,
    family_row_hash,
)
from fintr.scanner.report import resolve_code_commit


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ADR §7: before the one-shot holdout test the D0 comparison may only look at
# dates on or before 2026-09-25 (the last day of the base capture, i.e. before
# the 2026-10-01..2027-03-31 test window opens). Deliberately a single
# constant with no CLI flag: moving it would let a later comparison touch
# test-window dates.
D0_CUTOFF_DATE = date(2026, 9, 25)

BASE_CAPTURE_NAME = "2025-09-26_2026-09-25.json"
# Same value as ``capture_root`` in configs/collection/daily_miniapp_v1.json
# (a test pins the two together).
DEFAULT_DAILY_ROOT = Path("data/interim/daily_capture")
DEFAULT_BASE_ROOT = Path("data/interim/period_analysis")
REPORT_SCHEMA = "fintr_d0_comparison_v1"
REPORT_SUBDIR = "_d0"
FAMILIES = ("akd", "akd_market", "custody", "metadata")

_CAPTURE_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json$")
_CATEGORY_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def _symbol_list(value: str) -> list[str]:
    symbols = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not symbols:
        raise argparse.ArgumentTypeError("expected a comma-separated list of symbols")
    return symbols


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "D0: compare daily rolling captures (<= the fixed 2026-09-25 cutoff) with a "
            "one-year base capture per (date, family); counts only, no values."
        ),
    )
    parser.add_argument(
        "--daily-root", type=Path, default=DEFAULT_DAILY_ROOT,
        help=f"daily capture root (default: {DEFAULT_DAILY_ROOT}, as collect_daily.py)",
    )
    parser.add_argument(
        "--base-root", type=Path, default=DEFAULT_BASE_ROOT,
        help=f"root of <SYM>/{BASE_CAPTURE_NAME} base captures (default: {DEFAULT_BASE_ROOT})",
    )
    parser.add_argument(
        "--symbols", type=_symbol_list, default=None,
        help="comma-separated; default: development symbols found under --daily-root",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List the files that would be compared; load and write nothing.",
    )
    return parser


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def _daily_files(root: Path, symbol: str) -> tuple[list[Path], int]:
    """Rolling captures under ``root/<symbol>/`` that can hold a date <= the cutoff.

    A file whose window starts after the cutoff can carry only later dates;
    it is not even opened (returned only as a count).
    """
    symbol_dir = root / symbol
    if not symbol_dir.is_dir():
        return [], 0
    included: list[tuple[str, Path]] = []
    skipped_after_cutoff = 0
    for entry in sorted(symbol_dir.iterdir()):
        match = _CAPTURE_NAME_RE.match(entry.name) if entry.is_file() else None
        if match is None:
            continue
        try:
            start = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if start > D0_CUTOFF_DATE:
            skipped_after_cutoff += 1
        else:
            included.append((entry.name, entry))
    return [path for _name, path in sorted(included)], skipped_after_cutoff


def _development_symbols_in_root(root: Path, development: tuple[str, ...]) -> tuple[list[str], int]:
    """(development symbols with a directory under ``root``, other directory count)."""
    if not root.is_dir():
        return [], 0
    directories = {
        entry.name for entry in root.iterdir() if entry.is_dir() and not entry.name.startswith("_")
    }
    found = sorted(directories & set(development))
    return found, len(directories) - len(found)


def _failure_category(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return "unreadable_file"
    if isinstance(exc, CompositionError):
        head = str(exc).split(":", 1)[0].strip()
        return f"composition_{head}" if _CATEGORY_RE.fullmatch(head) else "composition_error"
    head = str(exc).split(":", 1)[0].strip()
    return head if _CATEGORY_RE.fullmatch(head) else "invalid_capture"


class _InputFailure(Exception):
    def __init__(self, category: str, path: Path) -> None:
        super().__init__(category)
        self.category = category
        self.path = path


def _load(path: Path, symbol: str) -> tuple[PeriodCapture, dict[str, str]]:
    try:
        payload = path.read_bytes()
        capture = load_period_capture(path)
    except (OSError, PeriodCaptureError) as exc:
        raise _InputFailure(_failure_category(exc), path) from None
    if capture.symbol != symbol:
        raise _InputFailure("symbol_mismatch", path)
    return capture, {"path": _display_path(path), "sha256": hashlib.sha256(payload).hexdigest()}


def _rows_up_to_cutoff(capture: PeriodCapture, family: str) -> dict[date, object]:
    return {
        day: row for day, row in family_days(capture, family).items() if day <= D0_CUTOFF_DATE
    }


def compare_family(base: PeriodCapture, daily: PeriodCapture, family: str) -> dict[str, Any]:
    """Counts for one family; ``daily`` is the composed daily-side capture."""
    base_rows = _rows_up_to_cutoff(base, family)
    daily_rows = _rows_up_to_cutoff(daily, family)
    common = sorted(set(base_rows) & set(daily_rows))
    matched = 0
    differing_codes = 0
    for day in common:
        if family_row_hash(base_rows[day], family) == family_row_hash(daily_rows[day], family):
            matched += 1
        else:
            differing_codes += differing_code_count(daily_rows[day], base_rows[day], family)
    compared = len(common)
    return {
        "compared_days": compared,
        "matched_days": matched,
        "match_rate": None if compared == 0 else round(matched / compared, 6),
        "differing_days": compared - matched,
        "differing_code_count_total": differing_codes,
        "only_base": len(set(base_rows) - set(daily_rows)),
        "only_daily": len(set(daily_rows) - set(base_rows)),
    }


def compare_symbol(symbol: str, base_root: Path, daily_root: Path) -> dict[str, Any]:
    daily_paths, skipped_after_cutoff = _daily_files(daily_root, symbol)
    if not daily_paths:
        return {"status": "skipped_no_daily_captures",
                "daily_files_skipped_after_cutoff": skipped_after_cutoff}
    base_path = base_root / symbol / BASE_CAPTURE_NAME
    if not base_path.is_file():
        return {"status": "failed:base_missing", "failed_path": _display_path(base_path)}
    try:
        base, base_input = _load(base_path, symbol)
        daily_captures: list[PeriodCapture] = []
        daily_inputs: list[dict[str, str]] = []
        for path in daily_paths:
            capture, record = _load(path, symbol)
            daily_captures.append(capture)
            daily_inputs.append(record)
        try:
            composed = compose_period_capture([
                CaptureInput(capture, kind="rolling", label=record["path"])
                for capture, record in zip(daily_captures, daily_inputs)
            ])
        except CompositionError as exc:
            raise _InputFailure(_failure_category(exc), daily_root / symbol) from None
    except _InputFailure as failure:
        return {"status": f"failed:{failure.category}", "failed_path": _display_path(failure.path)}

    families: dict[str, Any] = {}
    for family in FAMILIES:
        if family == "metadata" and not (
            _rows_up_to_cutoff(base, family) or _rows_up_to_cutoff(composed, family)
        ):
            continue  # metadata only when either side has it
        families[family] = compare_family(base, composed, family)
    return {
        "status": "compared",
        "inputs": {
            "base": base_input, "daily": daily_inputs,
            "daily_files_skipped_after_cutoff": skipped_after_cutoff,
        },
        "families": families,
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _write_report(report: dict[str, Any], directory: Path, stamp: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{stamp}.json"
    payload = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def _print_table(symbols: dict[str, dict[str, Any]]) -> None:
    header = (
        f"{'SYMBOL':<8}{'FAMILY':<12}{'COMPARED':>9}{'MATCHED':>9}{'RATE':>8}"
        f"{'DIFF_DAYS':>10}{'DIFF_CODES':>11}{'ONLY_BASE':>10}{'ONLY_DAILY':>11}"
    )
    print(header)
    print("-" * len(header))
    for symbol, entry in symbols.items():
        if entry["status"] != "compared":
            print(f"{symbol:<8}{entry['status']}")
            continue
        for family, counts in entry["families"].items():
            rate = "-" if counts["match_rate"] is None else f"{counts['match_rate']:.4f}"
            print(
                f"{symbol:<8}{family:<12}{counts['compared_days']:>9}{counts['matched_days']:>9}"
                f"{rate:>8}{counts['differing_days']:>10}{counts['differing_code_count_total']:>11}"
                f"{counts['only_base']:>10}{counts['only_daily']:>11}"
            )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    daily_root = _resolve(args.daily_root)
    base_root = _resolve(args.base_root)

    try:
        guards = load_active_guards()
    except SymbolGuardError as exc:
        print(f"D0 failed: {exc}", file=sys.stderr)
        return 1

    ignored_directories = 0
    if args.symbols is not None:
        symbols = sorted(set(args.symbols))
        excluded = [symbol for symbol in symbols if guards.is_excluded(symbol)]
        if excluded:
            print(f"D0 failed: excluded symbol(s) requested ({','.join(excluded)})", file=sys.stderr)
            return 1
        holdout = [symbol for symbol in symbols if guards.is_holdout(symbol)]
        if holdout:
            print(
                f"D0 failed: holdout symbol(s) requested ({','.join(holdout)}); holdout "
                "analysis is forbidden until the one-shot holdout test",
                file=sys.stderr,
            )
            return 1
        not_development = [
            symbol for symbol in symbols if symbol not in guards.split.development_symbols
        ]
        if not_development:
            print(
                f"D0 failed: not a development symbol of the active split "
                f"({','.join(not_development)})",
                file=sys.stderr,
            )
            return 1
    else:
        symbols, ignored_directories = _development_symbols_in_root(
            daily_root, guards.split.development_symbols,
        )
    if not symbols:
        print(f"D0 failed: no development symbols to compare under {_display_path(daily_root)}",
              file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"Dry run: D0 cutoff {D0_CUTOFF_DATE.isoformat()}; nothing loaded, nothing written.")
        for symbol in symbols:
            paths, skipped = _daily_files(daily_root, symbol)
            base_path = base_root / symbol / BASE_CAPTURE_NAME
            print(f"{symbol}: base={_display_path(base_path)} "
                  f"({'present' if base_path.is_file() else 'MISSING'}); "
                  f"daily files={len(paths)} (skipped after cutoff: {skipped})")
            for path in paths:
                print(f"    {_display_path(path)}")
        return 0

    compared = {symbol: compare_symbol(symbol, base_root, daily_root) for symbol in symbols}
    started = _utc_now()
    report = {
        "schema": REPORT_SCHEMA,
        "generated_at_utc": started.isoformat().replace("+00:00", "Z"),
        "cutoff_date": D0_CUTOFF_DATE.isoformat(),
        "code_commit": resolve_code_commit(PROJECT_ROOT),
        "daily_root": _display_path(daily_root),
        "base_root": _display_path(base_root),
        "split_config_sha256": guards.split.split_sha256,
        "exclusions_sha256": guards.exclusions.file_sha256,
        "ignored_non_development_directory_count": ignored_directories,
        "symbols": compared,
        "notes": "counts only; no financial value and no institution code is recorded",
    }
    try:
        validate_context_contains_no_secrets(report)
    except LivePilotCaptureError as exc:
        print(f"D0 report failed the secret/value check ({exc}); not written.", file=sys.stderr)
        return 1

    stamp = started.strftime("%Y-%m-%dT%H-%M-%SZ")
    try:
        target = _write_report(report, daily_root / REPORT_SUBDIR, stamp)
    except FileExistsError:
        print(f"D0 failed: report {stamp}.json already exists (no overwrite)", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"D0 failed: could not write report ({exc.__class__.__name__})", file=sys.stderr)
        return 1

    _print_table(compared)
    print(f"Report: {_display_path(target)}")
    failed = [symbol for symbol, entry in compared.items() if entry["status"].startswith("failed")]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
