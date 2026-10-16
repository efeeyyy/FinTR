"""CLI: walk-forward directional accuracy tracking of the frozen H1-H4 rules.

Opt-in, offline, descriptive (Efe's decision of 2026-10-16: K4 is lifted for the
20 development symbols only; ``docs/prereg/DECISIONS.md``, ``TRIAL_LOG`` T-31;
``docs/accuracy.md``). For each development symbol, signal day ``t`` in the
window and frozen rule it records the rule's prediction at ``t`` (saved scan /
forward log when one exists, else reconstructed from data dated ``<= t`` only)
and, for every ``condition_met`` observation, whether the realized direction of
the engine-defined forward return (5 and 1 trading days) matched the frozen
``predicted_direction``, next to the unconditional base rate. Periods
``train_window`` (signal day <= 2026-09-25) and ``post_train`` are reported
separately, never pooled.

Holdout and excluded symbols are refused. No network, no Telegram, no model
call; rules, thresholds and specs are not touched. Writes
``<out-dir>/<run-date>/accuracy.json`` and ``accuracy.md`` (never overwrites;
exit 3 when they exist). stdout carries only the output path and counts.
"""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from fintr.accuracy.core import (
    DEFAULT_WINDOW_DAYS,
    SECONDARY_HORIZON,
    AccuracyError,
    EvalParams,
    SymbolInput,
    select_window,
    symbol_signal_days,
    track_accuracy,
    validate_symbols,
)
from fintr.accuracy.report import ACCURACY_SCHEMA, build_document, render_markdown
from fintr.accuracy.saved import load_saved_predictions
from fintr.collection.new_data_guard import istanbul_today
from fintr.data.period_capture import PeriodCaptureError, load_period_capture
from fintr.data.price_history import PriceHistoryCaptureError, load_price_history_capture
from fintr.data.settlement_cycle import (
    DEFAULT_SETTLEMENT_CYCLE_PATH,
    SettlementCycleError,
    load_settlement_cycle,
)
from fintr.evaluation.symbol_guard import SymbolGuardError, load_active_guards
from fintr.features.price_join import DEFAULT_ADJUSTMENT_RATIO_EPSILON, DEFAULT_RATIO_REGIME_TOLERANCE
from fintr.research.compose_period_capture import CaptureInput, CompositionError, compose_period_capture
from fintr.research.engine import tradeable_price_bars
from fintr.scanner.rules import ScannerRulesError, load_scanner_rules


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCAN_SCRIPT = PROJECT_ROOT / "scripts" / "scan_observations.py"
DEFAULT_RULES_FILE = Path("configs/scanner/rules_v1.json")
DEFAULT_PERIOD_DIR = Path("data/interim/period_analysis")
DEFAULT_PRICE_DIR = Path("data/interim/price_history")
DEFAULT_DAILY_ROOT = Path("data/interim/daily_capture")
DEFAULT_SCANNER_DIR = Path("data/interim/scanner_daily")
DEFAULT_FORWARD_LOG = Path("forward_log/manifest.jsonl")
DEFAULT_OUT_DIR = Path("data/interim/accuracy")
DEFAULT_CUSTODY_SCALE_JUMP_RATIO = Decimal("3")

EXIT_FAILED = 1
EXIT_EXISTS = 3  # accuracy.json / accuracy.md of this run date already exist (no overwrite)
SAVED_PREFIX = "ACCURACY_SAVED: "

_PRICE_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json$")


def _load_scan_module():
    """The scanner script's discovery helpers (period/daily capture and price file
    lookup), reused as-is, exactly as ``fintr.scanner.replay`` does."""
    spec = importlib.util.spec_from_file_location("fintr_accuracy_scan_observations", SCAN_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decimal(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("expected a decimal number") from exc
    if not number.is_finite():
        raise argparse.ArgumentTypeError("expected a finite decimal number")
    return number


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def _symbol_list(value: str) -> list[str]:
    symbols = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not symbols:
        raise argparse.ArgumentTypeError("expected a comma-separated list of symbols")
    return symbols


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Walk-forward directional accuracy tracking of the frozen H1-H4 rules for "
                     "development symbols (offline, descriptive; docs/accuracy.md).",
    )
    parser.add_argument("--start", type=_date, default=None,
                        help="first signal day (default: the last --last-n-days days with data)")
    parser.add_argument("--end", type=_date, default=None,
                        help="last signal day (default: the latest day with AKD and price data)")
    parser.add_argument("--last-n-days", type=_positive_int, default=DEFAULT_WINDOW_DAYS,
                        help=f"default window length in trading days; default: {DEFAULT_WINDOW_DAYS}")
    parser.add_argument("--symbols", type=_symbol_list, default=None,
                        help="comma-separated; default: the active split's development symbols; "
                             "holdout and excluded symbols are refused")
    parser.add_argument("--period-dir", type=Path, default=DEFAULT_PERIOD_DIR)
    parser.add_argument("--price-dir", type=Path, default=DEFAULT_PRICE_DIR)
    parser.add_argument("--daily-root", type=Path, default=DEFAULT_DAILY_ROOT,
                        help="rolling captures composed with the base capture (as the daily scan "
                             "does); a missing directory means the base capture alone")
    parser.add_argument("--scanner-dir", type=Path, default=DEFAULT_SCANNER_DIR,
                        help="saved daily scans (observations.json): preferred predictions")
    parser.add_argument("--forward-log", type=Path, default=DEFAULT_FORWARD_LOG,
                        help="forward-log manifest verifying saved scans")
    parser.add_argument("--rules-file", type=Path, default=DEFAULT_RULES_FILE)
    parser.add_argument("--adjustment-ratio-epsilon", type=_decimal, default=DEFAULT_ADJUSTMENT_RATIO_EPSILON)
    parser.add_argument("--custody-scale-jump-ratio", type=_decimal, default=DEFAULT_CUSTODY_SCALE_JUMP_RATIO)
    parser.add_argument("--ratio-regime-tolerance", type=_decimal, default=DEFAULT_RATIO_REGIME_TOLERANCE)
    parser.add_argument("--run-date", type=_date, default=None,
                        help="output folder date (default: today, Europe/Istanbul)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help=f"default: {DEFAULT_OUT_DIR}; writes <out-dir>/<run-date>/accuracy.{{json,md}}")
    return parser


def latest_price_file(root: Path, symbol: str, *, covers_start: date) -> Path | None:
    """Saved price-history file ending latest (ties: the later name) that starts
    on or before ``covers_start``: outcomes need the most recent prices, unlike
    the scanner, which prefers the narrowest file covering its window."""
    symbol_dir = root / symbol
    if not symbol_dir.is_dir():
        return None
    best: tuple[date, str, Path] | None = None
    for entry in sorted(symbol_dir.iterdir()):
        match = _PRICE_NAME_RE.match(entry.name) if entry.is_file() else None
        if not match:
            continue
        file_start, file_end = date.fromisoformat(match.group(1)), date.fromisoformat(match.group(2))
        if file_start > file_end or file_start > covers_start:
            continue
        if best is None or (file_end, entry.name) > (best[0], best[1]):
            best = (file_end, entry.name, entry)
    return None if best is None else best[2]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_symbol(scan, symbol: str, args) -> tuple[SymbolInput, dict, dict]:
    """``(input, input_files record, look-ahead meta)`` for one symbol."""
    base_path = scan.find_period_capture(args.period_dir, symbol)
    if base_path is None:
        raise AccuracyError(f"no period capture found for {symbol} under {args.period_dir}")
    try:
        base_bytes = base_path.read_bytes()
        base = load_period_capture(base_path)
    except (OSError, PeriodCaptureError) as exc:
        raise AccuracyError(f"could not load period capture for {symbol} ({exc})") from exc

    daily_loaded = []
    if args.daily_root is not None and Path(args.daily_root).is_dir():
        for daily_path in scan.find_daily_captures(args.daily_root, symbol):
            try:
                daily_bytes = daily_path.read_bytes()
                daily = load_period_capture(daily_path)
            except (OSError, PeriodCaptureError) as exc:
                raise AccuracyError(f"could not load daily capture {daily_path} ({exc})") from exc
            daily_loaded.append((daily_path, daily_bytes, daily))

    composed_start = min([base.start, *(d.start for _, _, d in daily_loaded)])
    price_path = latest_price_file(args.price_dir, symbol, covers_start=composed_start)
    if price_path is None:
        raise AccuracyError(f"no price history found for {symbol} under {args.price_dir}")
    try:
        price_bytes = price_path.read_bytes()
        price = load_price_history_capture(price_path)
    except (OSError, PriceHistoryCaptureError) as exc:
        raise AccuracyError(f"could not load price history for {symbol} ({exc})") from exc

    record = {
        "period_capture": {"path": str(base_path), "sha256": _sha256(base_bytes),
                           "captured_at_utc": base.captured_at_utc},
        "daily_captures": [
            {"path": str(path), "sha256": _sha256(data), "captured_at_utc": cap.captured_at_utc}
            for path, data, cap in daily_loaded
        ],
        "price_history": {"path": str(price_path), "sha256": _sha256(price_bytes),
                          "accessed_at_utc": price.meta.accessed_at_utc},
    }
    if not daily_loaded:
        capture = base
        meta = {"base_end": base.end, "revised_days": set(), "unsettled_days": set()}
    else:
        inputs = [CaptureInput(base, kind="base", label=str(base_path))]
        inputs += [CaptureInput(cap, kind="rolling", label=str(path)) for path, _, cap in daily_loaded]
        calendar = tuple(bar.trade_date for bar in tradeable_price_bars(price))
        try:
            capture = compose_period_capture(inputs, tradeable_dates=calendar)
        except CompositionError as exc:
            raise AccuracyError(f"could not compose base+daily captures for {symbol} ({exc})") from exc
        lineage = capture.composition
        record["composition_sha256"] = lineage.composition_sha256
        meta = {
            "base_end": base.end,
            "revised_days": {e.date for e in lineage.revisions if e.family in ("akd", "akd_market", "metadata")},
            "unsettled_days": {e.date for e in lineage.unsettled if e.family == "akd"},
        }
    return SymbolInput(symbol=symbol, capture=capture, price=price), record, meta


def _save_pair(out_dir: Path, run_date: str, files: dict[str, bytes]) -> list[Path]:
    """Write every file without overwriting; all-or-nothing on pre-existing names."""
    target_dir = out_dir / run_date
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in files:
        if (target_dir / name).exists():
            raise FileExistsError(target_dir / name)
    written: list[Path] = []
    for name, payload in files.items():
        target = target_dir / name
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".accuracy-", suffix=".tmp", dir=target_dir, delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except AccuracyError as exc:
        print(f"Accuracy failed: {exc}", file=sys.stderr)
        return EXIT_FAILED


def _run(args: argparse.Namespace) -> int:
    try:
        rules = load_scanner_rules(args.rules_file, project_root=PROJECT_ROOT)
    except ScannerRulesError as exc:
        raise AccuracyError(f"invalid rules file ({exc})") from exc
    rules_file_sha256 = _sha256(args.rules_file.read_bytes())
    try:
        settlement_cycle = load_settlement_cycle(PROJECT_ROOT / DEFAULT_SETTLEMENT_CYCLE_PATH)
        guards = load_active_guards()
    except (SettlementCycleError, SymbolGuardError) as exc:
        raise AccuracyError(str(exc)) from exc

    requested = args.symbols if args.symbols is not None else sorted(guards.split.development_symbols)
    symbols = validate_symbols(requested, guards)  # refuses holdout / excluded / non-development

    scan = _load_scan_module()
    inputs: list[SymbolInput] = []
    input_files: dict = {}
    metas: dict = {}
    for symbol in symbols:
        item, record, meta = _load_symbol(scan, symbol, args)
        inputs.append(item)
        input_files[symbol] = record
        metas[symbol] = meta

    days = select_window(inputs, start=args.start, end=args.end, last_n=args.last_n_days)
    if not days:
        raise AccuracyError("no signal day with AKD and price data in the requested window")

    saved, provenance = load_saved_predictions(
        args.scanner_dir, days=days, rules_file_sha256=rules_file_sha256,
        forward_manifest=args.forward_log if args.forward_log and Path(args.forward_log).exists() else None,
    )
    params = EvalParams(
        adjustment_ratio_epsilon=args.adjustment_ratio_epsilon,
        custody_scale_jump_ratio=args.custody_scale_jump_ratio,
        ratio_regime_tolerance=args.ratio_regime_tolerance,
    )
    result = track_accuracy(
        inputs, rules, days, params=params, custody_lag_on=settlement_cycle.lag_on, saved=saved,
    )

    day_set = set(days)
    exposure = {"symbol_days_in_window": 0, "days_from_base_capture_only_or_earlier": 0,
                "days_with_later_revision": 0, "days_akd_unsettled": 0}
    for item in inputs:
        meta = metas[item.symbol]
        for day in set(symbol_signal_days(item)) & day_set:
            exposure["symbol_days_in_window"] += 1
            exposure["days_from_base_capture_only_or_earlier"] += day <= meta["base_end"]
            exposure["days_with_later_revision"] += day in meta["revised_days"]
            exposure["days_akd_unsettled"] += day in meta["unsettled_days"]

    run_date = (args.run_date or istanbul_today()).isoformat()
    parameters = {
        "window_start": days[0].isoformat(), "window_end": days[-1].isoformat(),
        "window_day_count": len(days),
        "window_selection": "explicit_start_end" if args.start is not None else f"last_{args.last_n_days}_days_with_data",
        "symbols": symbols, "primary_horizons": list(result.primary_horizons), "secondary_horizon": SECONDARY_HORIZON,
        "horizons": list(result.horizons), "cost_bps_per_side": "10",
        "custody_settlement_lag_server_dates": sorted({settlement_cycle.lag_on(d) for d in days}),
        "adjustment_ratio_epsilon": str(args.adjustment_ratio_epsilon),
        "custody_scale_jump_ratio": str(args.custody_scale_jump_ratio),
        "ratio_regime_tolerance": str(args.ratio_regime_tolerance),
        "price_file_policy": "latest-ending saved price file per symbol (outcomes need recent bars)",
        "saved_scan_dir": str(args.scanner_dir), "forward_log": str(args.forward_log),
    }
    document = build_document(
        project_root=PROJECT_ROOT, run_date=run_date, parameters=parameters,
        rules_file=str(args.rules_file), rules_file_sha256=rules_file_sha256, rules=rules,
        split_guard=guards.report(processed_symbols=symbols), input_files=input_files,
        prediction_provenance=provenance, look_ahead_exposure=exposure, result=result,
    )
    assert document["schema"] == ACCURACY_SCHEMA
    json_bytes = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    md_bytes = render_markdown(document).encode("utf-8")
    try:
        written = _save_pair(args.out_dir, run_date, {"accuracy.json": json_bytes, "accuracy.md": md_bytes})
    except FileExistsError as exc:
        print(f"Accuracy failed: output file already exists (no overwrite): {exc.args[0]}", file=sys.stderr)
        return EXIT_EXISTS

    print(f"{SAVED_PREFIX}{written[0]}")
    for period, by_horizon in document["counts"].items():
        for horizon, counts in by_horizon.items():
            print(
                f"counts period={period} horizon={horizon} signals={counts['signals']} "
                f"resolved={counts['resolved']} pending={counts['pending']} dropped={counts['dropped']}"
            )
    check = document["reconstruction_check"]
    print(f"reconstruction_check compared={check['compared']} agree={check['agree']} disagree={check['disagree']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
