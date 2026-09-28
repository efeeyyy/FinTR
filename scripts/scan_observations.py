"""CLI: offline observation scanner over saved period captures + price history.

No network, no Telegram, no model call. Reports which pre-registered H1-H4
conditions (``configs/scanner/rules_v1.json``) hold for development symbols
on one or more as_of server dates. This is an observation notice, not a
study and not a signal -- it emits no score, rank, return or target. See
``docs/scanner.md``. Never reads ``data/interim/studies/``.

``--daily-root`` (default off; ADR §4,
``docs/research/daily_miniapp_collection_adr.md``) composes each symbol's
base period capture with its rolling daily captures via
``fintr.research.compose_period_capture`` before scanning. Omitted, output
is byte-identical to a run without this option (aside from inherently
variable fields such as ``generated_at_utc``); given, the document schema
becomes ``fintr_scanner_observations_v2`` and each symbol carries an
additional ``composition`` block plus a per-scan ``data_status`` marker.
"""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from fintr.data.period_capture import PeriodCapture, PeriodCaptureError, load_period_capture
from fintr.data.price_history import PriceHistoryCaptureError, load_price_history_capture
from fintr.evaluation.symbol_guard import SymbolGuardError, load_active_guards
from fintr.features.price_join import DEFAULT_ADJUSTMENT_RATIO_EPSILON, DEFAULT_RATIO_REGIME_TOLERANCE
from fintr.research.compose_period_capture import CaptureInput, CompositionError, compose_period_capture
from fintr.research.engine import tradeable_price_bars
from fintr.scanner.freshness import build_freshness_report
from fintr.scanner.observe import evaluate_rule_for_symbol
from fintr.scanner.report import (
    RESULT_SCHEMA_V1,
    RESULT_SCHEMA_V2,
    build_document,
    freshness_payload,
    observation_payload,
    render_text,
)
from fintr.scanner.rules import ScannerRulesError, load_scanner_rules


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES_FILE = Path("configs/scanner/rules_v1.json")
DEFAULT_PERIOD_DIR = Path("data/interim/period_analysis")
DEFAULT_PRICE_DIR = Path("data/interim/price_history")
DEFAULT_OUT_DIR = Path("data/interim/scanner")
DEFAULT_CUSTODY_SCALE_JUMP_RATIO = Decimal("3")

# Same filename shape/matching logic as scripts/analyze_period.py --all.
_CAPTURE_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json$")
_PRICE_CAPTURE_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json$")


def find_period_capture(root: Path, symbol: str) -> Path | None:
    """Most-recent-ending ``<S>_<E>.json`` capture under ``root/<symbol>/``."""
    symbol_dir = root / symbol
    if not symbol_dir.is_dir():
        return None
    candidates: list[tuple[date, str, Path]] = []
    for entry in sorted(symbol_dir.iterdir()):
        if not entry.is_file():
            continue
        match = _CAPTURE_NAME_RE.match(entry.name)
        if not match:
            continue
        end = date.fromisoformat(match.group(2))
        candidates.append((end, entry.name, entry))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][2]


def find_daily_captures(root: Path, symbol: str) -> list[Path]:
    """Every ``<S>_<E>.json`` rolling capture under ``root/<symbol>/``, ascending
    by end date then name (ADR §2: ``data/interim/daily_capture/<SYM>/<S>_<E>.json``).
    Unlike ``find_period_capture`` this never picks a single "latest" file --
    the composer needs every overlapping window."""
    symbol_dir = root / symbol
    if not symbol_dir.is_dir():
        return []
    candidates: list[tuple[date, str, Path]] = []
    for entry in sorted(symbol_dir.iterdir()):
        if not entry.is_file():
            continue
        match = _CAPTURE_NAME_RE.match(entry.name)
        if not match:
            continue
        end = date.fromisoformat(match.group(2))
        candidates.append((end, entry.name, entry))
    candidates.sort()
    return [item[2] for item in candidates]


def discover_price_history(root: Path, symbol: str, start: date, end: date) -> Path | None:
    """Best covering saved price-history file (same logic as analyze_period.py)."""
    symbol_dir = root / symbol
    if not symbol_dir.is_dir():
        return None
    candidates: list[tuple[int, str, Path]] = []
    for entry in sorted(symbol_dir.iterdir()):
        if not entry.is_file():
            continue
        match = _PRICE_CAPTURE_NAME_RE.match(entry.name)
        if not match:
            continue
        file_start = date.fromisoformat(match.group(1))
        file_end = date.fromisoformat(match.group(2))
        if file_start > file_end or file_start > start or file_end < end:
            continue
        candidates.append(((file_end - file_start).days, entry.name, entry))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


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
        description="Offline observation scanner over saved period captures and price "
                     "history; no network, no Telegram, no model call. See docs/scanner.md.",
    )
    parser.add_argument(
        "--as-of", type=_date, default=None,
        help="default: each symbol's own common last AKD/price server day",
    )
    parser.add_argument(
        "--lookback-days", type=_positive_int, default=1,
        help="scan this many trailing server dates per symbol, ending at --as-of "
             "(or its default); default: 1",
    )
    parser.add_argument(
        "--symbols", type=_symbol_list, default=None,
        help="comma-separated; default: the active split's development symbols",
    )
    parser.add_argument("--period-dir", type=Path, default=DEFAULT_PERIOD_DIR,
                         help=f"default: {DEFAULT_PERIOD_DIR}")
    parser.add_argument("--price-dir", type=Path, default=DEFAULT_PRICE_DIR,
                         help=f"default: {DEFAULT_PRICE_DIR}")
    parser.add_argument(
        "--daily-root", type=Path, default=None,
        help="default: off (disabled). If given, each symbol's period-dir capture is "
             "composed with every rolling capture under <daily-root>/<symbol>/ via "
             "fintr.research.compose_period_capture (ADR §4); output schema becomes "
             "fintr_scanner_observations_v2 and gains a per-symbol composition block "
             "plus a per-scan data_status marker. See docs/scanner.md.",
    )
    parser.add_argument("--rules-file", type=Path, default=DEFAULT_RULES_FILE,
                         help=f"default: {DEFAULT_RULES_FILE}")
    parser.add_argument("--adjustment-ratio-epsilon", type=_decimal,
                         default=DEFAULT_ADJUSTMENT_RATIO_EPSILON,
                         help=f"default: {DEFAULT_ADJUSTMENT_RATIO_EPSILON}")
    parser.add_argument("--custody-scale-jump-ratio", type=_decimal,
                         default=DEFAULT_CUSTODY_SCALE_JUMP_RATIO,
                         help=f"default: {DEFAULT_CUSTODY_SCALE_JUMP_RATIO}")
    parser.add_argument("--ratio-regime-tolerance", type=_decimal,
                         default=DEFAULT_RATIO_REGIME_TOLERANCE,
                         help=f"default: {DEFAULT_RATIO_REGIME_TOLERANCE}")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                         help=f"default: {DEFAULT_OUT_DIR}")
    parser.add_argument(
        "--save", action="store_true",
        help=f"write {DEFAULT_OUT_DIR}/<as_of>/observations.json; never overwrites",
    )
    return parser


def _save(out_dir: Path, as_of_label: str, payload: bytes) -> Path:
    target_dir = out_dir / as_of_label
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "observations.json"
    if target.exists():
        raise FileExistsError(target)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".scanner-", suffix=".tmp", dir=target_dir, delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        rules = load_scanner_rules(args.rules_file, project_root=PROJECT_ROOT)
    except ScannerRulesError as exc:
        print(f"Scan failed: invalid rules file ({exc})", file=sys.stderr)
        return 1
    rules_file_sha256 = hashlib.sha256(args.rules_file.read_bytes()).hexdigest()

    try:
        guards = load_active_guards()
    except SymbolGuardError as exc:
        print(f"Scan failed: {exc}", file=sys.stderr)
        return 1

    requested = sorted(set(args.symbols)) if args.symbols is not None else sorted(
        guards.split.development_symbols
    )

    excluded = sorted(symbol for symbol in requested if guards.is_excluded(symbol))
    if excluded:
        print(f"Scan failed: excluded symbol(s) requested ({','.join(excluded)})", file=sys.stderr)
        return 1
    holdout = sorted(symbol for symbol in requested if guards.is_holdout(symbol))
    if holdout:
        print(
            f"Scan failed: holdout symbol(s) requested ({','.join(holdout)}); holdout "
            "analysis is forbidden until the one-shot holdout test",
            file=sys.stderr,
        )
        return 1

    used_daily_root = args.daily_root is not None
    symbols_payload: dict[str, dict] = {}
    for symbol in requested:
        base_path = find_period_capture(args.period_dir, symbol)
        if base_path is None:
            print(f"Scan failed: no period capture found for {symbol} under {args.period_dir}",
                  file=sys.stderr)
            return 1
        try:
            base_bytes = base_path.read_bytes()
            base_capture = load_period_capture(base_path)
        except (OSError, PeriodCaptureError) as exc:
            print(f"Scan failed: could not load period capture for {symbol} ({exc})", file=sys.stderr)
            return 1

        composition_payload: dict | None = None
        if not used_daily_root:
            capture = base_capture
            price_path = discover_price_history(args.price_dir, capture.symbol, capture.start, capture.end)
            if price_path is None:
                print(f"Scan failed: no price history found for {symbol} under {args.price_dir}",
                      file=sys.stderr)
                return 1
            try:
                price_bytes = price_path.read_bytes()
                price = load_price_history_capture(price_path)
            except (OSError, PriceHistoryCaptureError) as exc:
                print(f"Scan failed: could not load price history for {symbol} ({exc})", file=sys.stderr)
                return 1
        else:
            daily_paths = find_daily_captures(args.daily_root, symbol)
            daily_loaded: list[tuple[Path, bytes, PeriodCapture]] = []
            for daily_path in daily_paths:
                try:
                    daily_bytes = daily_path.read_bytes()
                    daily_capture = load_period_capture(daily_path)
                except (OSError, PeriodCaptureError) as exc:
                    print(
                        f"Scan failed: could not load daily capture for {symbol} "
                        f"({daily_path}): {exc}",
                        file=sys.stderr,
                    )
                    return 1
                daily_loaded.append((daily_path, daily_bytes, daily_capture))

            inputs = [CaptureInput(base_capture, kind="base", label=str(base_path))]
            inputs.extend(
                CaptureInput(daily_capture, kind="rolling", label=str(daily_path))
                for daily_path, _, daily_capture in daily_loaded
            )
            # Same start/end formula compose_period_capture uses internally
            # (ADR §3 rule 7); computed ahead of composition only so the
            # covering price-history file can be discovered before the
            # tradeable-dates calendar it needs is itself available.
            composed_start = min(item.capture.start for item in inputs)
            composed_end = max(item.capture.end for item in inputs)

            price_path = discover_price_history(
                args.price_dir, base_capture.symbol, composed_start, composed_end,
            )
            if price_path is None:
                print(f"Scan failed: no price history found for {symbol} under {args.price_dir}",
                      file=sys.stderr)
                return 1
            try:
                price_bytes = price_path.read_bytes()
                price = load_price_history_capture(price_path)
            except (OSError, PriceHistoryCaptureError) as exc:
                print(f"Scan failed: could not load price history for {symbol} ({exc})", file=sys.stderr)
                return 1

            gap_calendar = tuple(bar.trade_date for bar in tradeable_price_bars(price))
            try:
                capture = compose_period_capture(inputs, tradeable_dates=gap_calendar)
            except CompositionError as exc:
                print(
                    f"Scan failed: could not compose base+daily captures for {symbol} ({exc})",
                    file=sys.stderr,
                )
                return 1

            lineage = capture.composition
            gap_dates = [d.isoformat() for d in lineage.gaps]
            unsettled_entries = [
                {"date": entry.date.isoformat(), "family": entry.family} for entry in lineage.unsettled
            ]
            revision_entries = [
                {"date": entry.date.isoformat(), "family": entry.family} for entry in lineage.revisions
            ]
            composition_payload = {
                "composition_sha256": lineage.composition_sha256,
                "composer_version": lineage.composer_version,
                "inputs": [
                    {
                        "kind": "base", "path": str(base_path),
                        "sha256": hashlib.sha256(base_bytes).hexdigest(),
                    },
                    *(
                        {
                            "kind": "rolling", "path": str(daily_path),
                            "sha256": hashlib.sha256(daily_bytes).hexdigest(),
                        }
                        for daily_path, daily_bytes, _ in daily_loaded
                    ),
                ],
                "gaps": {"count": len(gap_dates), "dates": gap_dates},
                "unsettled": {
                    "day_count": len({entry["date"] for entry in unsettled_entries}),
                    "entries": unsettled_entries,
                },
                "revisions": {
                    "day_count": len({entry["date"] for entry in revision_entries}),
                    "entries": revision_entries,
                },
            }

        tradeable_dates = tuple(bar.trade_date for bar in tradeable_price_bars(price))
        if args.as_of is not None:
            reference_date = args.as_of
            as_of_source = "explicit"
        else:
            last_akd = capture.server_dates[-1] if capture.server_dates else None
            last_price = tradeable_dates[-1] if tradeable_dates else None
            if last_akd is None or last_price is None:
                print(
                    f"Scan failed: {symbol} has no AKD or no price data to default --as-of from",
                    file=sys.stderr,
                )
                return 1
            reference_date = min(last_akd, last_price)
            as_of_source = "default_common_last_day"

        eligible_dates = tuple(d for d in capture.server_dates if d <= reference_date)
        as_of_dates = eligible_dates[-args.lookback_days:]
        if not as_of_dates:
            print(f"Scan failed: {symbol} has no server date on/before {reference_date}",
                  file=sys.stderr)
            return 1

        if composition_payload is not None:
            # Window-bounded counts (docs/scanner.md): the composer's totals
            # (rule 4) count every base-only day as unsettled; these restrict
            # reporting to [earliest rolling server date, reference_date].
            rolling_dates = [
                d for _, _, daily_capture in daily_loaded for d in daily_capture.server_dates
            ]
            window_start = min(rolling_dates) if rolling_dates else None
            if window_start is not None and window_start > reference_date:
                window_start = None
            if window_start is None:
                window_payload = None
                gap_in_window = unsettled_in_window = revisions_in_window = None
            else:
                def _in_window(day: date) -> bool:
                    return window_start <= day <= reference_date

                window_payload = {
                    "start": window_start.isoformat(), "end": reference_date.isoformat(),
                }
                gap_in_window = sum(1 for d in capture.composition.gaps if _in_window(d))
                unsettled_in_window = len(
                    {e.date for e in capture.composition.unsettled if _in_window(e.date)}
                )
                revisions_in_window = len(
                    {e.date for e in capture.composition.revisions if _in_window(e.date)}
                )
            composition_payload["window"] = window_payload
            composition_payload["gaps"]["in_window_count"] = gap_in_window
            composition_payload["unsettled"]["in_window_day_count"] = unsettled_in_window
            composition_payload["revisions"]["in_window_day_count"] = revisions_in_window

        scans = []
        for as_of in as_of_dates:
            freshness = build_freshness_report(capture, price, as_of=as_of)
            observations = [
                evaluate_rule_for_symbol(
                    rule, capture, price, as_of=as_of,
                    adjustment_ratio_epsilon=args.adjustment_ratio_epsilon,
                    custody_scale_jump_ratio=args.custody_scale_jump_ratio,
                    ratio_regime_tolerance=args.ratio_regime_tolerance,
                )
                for rule in rules
            ]
            scan_entry = {
                "as_of": as_of.isoformat(),
                "freshness": freshness_payload(freshness),
                "observations": [observation_payload(obs) for obs in observations],
            }
            if composition_payload is not None:
                # ADR §4: mark, never suppress -- an as_of day that the
                # composed capture still lists as unsettled or that carries
                # a provider revision is flagged here, not dropped.
                lineage = capture.composition
                scan_entry["data_status"] = {
                    "unsettled_families": sorted(
                        {entry.family for entry in lineage.unsettled if entry.date == as_of}
                    ),
                    "revised_families": sorted(
                        {entry.family for entry in lineage.revisions if entry.date == as_of}
                    ),
                }
            scans.append(scan_entry)

        symbol_entry = {
            "as_of_source": as_of_source,
            "input_files": {
                "period_capture": {
                    "path": str(base_path),
                    "sha256": hashlib.sha256(base_bytes).hexdigest(),
                },
                "price_history": {
                    "path": str(price_path),
                    "sha256": hashlib.sha256(price_bytes).hexdigest(),
                },
            },
            "scans": scans,
        }
        if composition_payload is not None:
            symbol_entry["composition"] = composition_payload
        symbols_payload[symbol] = symbol_entry

    document = build_document(
        project_root=PROJECT_ROOT, rules_file=str(args.rules_file), rules_file_sha256=rules_file_sha256,
        split_guard=guards.report(processed_symbols=requested), symbols=symbols_payload,
        schema=RESULT_SCHEMA_V2 if used_daily_root else RESULT_SCHEMA_V1,
    )
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    if args.save:
        all_as_of = sorted({
            scan["as_of"] for block in symbols_payload.values() for scan in block["scans"]
        })
        label = all_as_of[-1] if all_as_of else "unknown"
        try:
            saved = _save(args.out_dir, label, payload)
        except FileExistsError:
            print("Scan failed: output file already exists (no overwrite)", file=sys.stderr)
            return 1
        print(f"Observations saved: {saved}", file=sys.stderr)

    if args.format == "json":
        sys.stdout.buffer.write(payload)
    else:
        sys.stdout.buffer.write(render_text(document).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
