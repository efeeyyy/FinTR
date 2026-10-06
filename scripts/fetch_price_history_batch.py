"""Batch-fetch daily OHLCV history for every symbol in a universe file.

Reuses ``scripts/fetch_price_history.py``'s single-symbol fetch/save logic
(loaded as a module, the same way ``tests/test_fetch_price_history.py``
loads it) for each symbol in a universe JSON file (``fintr_universe_v1``
shape: an object with a ``symbols`` list). Adds: a bounded, CLI-configurable
pacing delay between network requests; skipping a symbol whose target
capture file already exists; continuing past a single symbol's failure
instead of aborting the whole run; and a stop (not a hammer) on the first
sign of upstream rate-limiting (an HTTP 429 from the chart endpoint).

Prints only a per-symbol status table -- symbol, status, bar count, date
source, and a zero-volume flat placeholder-bar count. Never a price or
volume value. Without ``--save`` nothing is fetched or written; the table
shows the plan only (``planned``/``skipped``), exactly like
``fetch_price_history.py --dry-run``.

A symbol listed in ``configs/exclusions/symbols.json`` is never requested:
it gets status ``skipped_excluded`` (no override flag). Holdout symbols of
the frozen BIST 30 split are *not* blocked -- collecting held-out data is
allowed; only analysing it is guarded (see docs/evaluation_protocol.md).
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import importlib.util
import json
from pathlib import Path
import time
from typing import Any

from fintr.evaluation.symbol_guard import SymbolGuardError, exclusions_report, load_active_exclusions


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_PACING_SECONDS = 1.5
# A floor, not just a default: keeps --pacing-seconds from being set low
# enough to hammer the upstream endpoint.
MIN_PACING_SECONDS = 1.5


def _load_fetch_module():
    spec = importlib.util.spec_from_file_location(
        "fetch_price_history", PROJECT_ROOT / "scripts" / "fetch_price_history.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = _load_fetch_module()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Batch-fetch daily OHLCV history for every symbol in a universe "
            "file, reusing fetch_price_history.py's fetch/save logic with "
            "bounded pacing between requests (unofficial secondary source; "
            "see docs/price_history.md)."
        ),
    )
    parser.add_argument(
        "--universe", required=True, type=Path,
        help="Path to a universe JSON file with a 'symbols' list (e.g. configs/universe/bist30.json).",
    )
    parser.add_argument("--start-date", required=True, type=fetch._date)
    parser.add_argument("--end-date", required=True, type=fetch._date)
    parser.add_argument(
        "--save", action="store_true",
        help="Fetch and save each symbol. Without this flag, no network call is made "
        "and the table only shows the plan (planned/skipped).",
    )
    parser.add_argument(
        "--pacing-seconds", type=float, default=DEFAULT_PACING_SECONDS,
        help=(
            f"Minimum delay between network requests, in seconds "
            f"(default: {DEFAULT_PACING_SECONDS:g}; floor: {MIN_PACING_SECONDS:g})."
        ),
    )
    parser.add_argument(
        "--timeout", type=float, default=fetch.DEFAULT_TIMEOUT_SECONDS,
        help="Per-symbol network timeout in seconds, passed through to the underlying fetch.",
    )
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser, today: date) -> None:
    if args.start_date > args.end_date:
        parser.error("--start-date must not be after --end-date")
    if args.end_date > today:
        parser.error(f"--end-date {args.end_date.isoformat()} is after today ({today.isoformat()})")
    if args.pacing_seconds < MIN_PACING_SECONDS:
        parser.error(f"--pacing-seconds must be at least {MIN_PACING_SECONDS:g}")
    if not 0 < args.timeout <= 60:
        parser.error("--timeout must be greater than 0 and at most 60 seconds")


def _load_universe(path: Path, parser: argparse.ArgumentParser) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        parser.error(f"--universe: unreadable file ({exc})")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        parser.error(f"--universe: invalid JSON ({exc})")
    if not isinstance(document, dict) or not isinstance(document.get("symbols"), list):
        parser.error("--universe: expected a JSON object with a 'symbols' list")
    symbols: list[str] = []
    seen: set[str] = set()
    for item in document["symbols"]:
        if not isinstance(item, str):
            parser.error(f"--universe: 'symbols' entries must be strings, got {item!r}")
        try:
            symbol = fetch._symbol(item)
        except argparse.ArgumentTypeError as exc:
            parser.error(f"--universe: invalid symbol {item!r}: {exc}")
        if symbol in seen:
            parser.error(f"--universe: duplicate symbol {symbol}")
        seen.add(symbol)
        symbols.append(symbol)
    if not symbols:
        parser.error("--universe: 'symbols' list is empty")
    return sorted(symbols)


def _is_zero_volume_flat_bar(bar: dict[str, Any]) -> bool:
    """Same 'placeholder' shape as fintr.features.price_join.classify_price_bar:
    volume 0/None with all four OHLC fields present and equal."""
    volume = bar.get("volume")
    if not (volume is None or volume == 0):
        return False
    ohlc = (bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close"))
    if any(value is None for value in ohlc):
        return False
    return ohlc[0] == ohlc[1] == ohlc[2] == ohlc[3]


def _process_symbol(
    symbol: str, start: date, end: date, *, timeout: float, save: bool,
) -> dict[str, Any]:
    path = fetch.target_path(symbol, start, end)
    if path.exists():
        return {"symbol": symbol, "status": "skipped", "reason": "target_exists"}
    if not save:
        return {"symbol": symbol, "status": "planned", "reason": None}

    url = fetch.build_request_url(symbol, start, end)
    try:
        raw_bytes = fetch._fetch_raw(url, timeout=timeout)
        capture = fetch.build_capture(
            symbol=symbol, start=start, end=end, request_url=url,
            raw_bytes=raw_bytes, accessed_at=datetime.now(timezone.utc),
        )
    except (fetch.FetchError, fetch.PriceHistoryParseError, ValueError) as exc:
        return {"symbol": symbol, "status": "failed", "reason": str(exc)}

    # The end day's close is not published yet: saving would freeze a null
    # last bar (no-overwrite), so leave the file absent and let a later run fetch it.
    if fetch.last_bar_incomplete(capture["normalized"]):
        return {"symbol": symbol, "status": fetch.INCOMPLETE_LAST_BAR_STATUS,
                "reason": "last_bar_close_missing"}

    try:
        fetch._save_capture(capture, symbol, start, end)
    except fetch.FetchError as exc:
        return {"symbol": symbol, "status": "failed", "reason": str(exc)}

    normalized = capture["normalized"]
    zero_volume_flat_count = sum(
        1 for bar in normalized["bars"] if _is_zero_volume_flat_bar(bar)
    )
    return {
        "symbol": symbol,
        "status": "saved",
        "reason": None,
        "bar_count": normalized["bar_count"],
        "date_source": normalized["date_source"],
        "zero_volume_flat_count": zero_volume_flat_count,
    }


def _is_rate_limited(reason: str | None) -> bool:
    return reason is not None and "http_429" in reason


def run_symbols(
    symbols: list[str], start: date, end: date, *, exclusions: Any, save: bool,
    pacing_seconds: float, timeout: float,
) -> list[dict[str, Any]]:
    """Per-symbol loop shared with ``scripts/collect_daily.py``'s price step.

    Excluded symbols are never requested; a symbol whose target file exists
    is skipped (no overwrite); a symbol failure does not abort the rest; the
    first HTTP 429 stops every remaining request (those symbols get status
    ``skipped`` / reason ``stopped_after_rate_limit``); consecutive network
    requests are separated by ``pacing_seconds`` (callers enforce the floor).
    """
    results: list[dict[str, Any]] = []
    rate_limited = False
    previous_made_request = False
    for symbol in symbols:
        if exclusions.is_excluded(symbol):
            results.append({"symbol": symbol, "status": "skipped_excluded", "reason": "excluded_symbol"})
            continue
        if rate_limited:
            results.append({"symbol": symbol, "status": "skipped", "reason": "stopped_after_rate_limit"})
            continue

        path = fetch.target_path(symbol, start, end)
        will_request = save and not path.exists()
        if will_request and previous_made_request:
            time.sleep(pacing_seconds)

        result = _process_symbol(symbol, start, end, timeout=timeout, save=save)
        previous_made_request = will_request
        results.append(result)

        if result["status"] == "failed" and _is_rate_limited(result.get("reason")):
            rate_limited = True
            print(f"Rate limit detected on {symbol}: stopping remaining requests (no hammering).")
    return results


def _print_table(results: list[dict[str, Any]]) -> None:
    header = f"{'SYMBOL':<10}{'STATUS':<18}{'BARS':>6}  {'DATE_SOURCE':<22}{'ZERO_VOL_FLAT':>14}  REASON"
    print(header)
    print("-" * len(header))
    for result in results:
        bars = result.get("bar_count")
        date_source = result.get("date_source") or "-"
        zero_vol = result.get("zero_volume_flat_count")
        reason = result.get("reason") or "-"
        print(
            f"{result['symbol']:<10}{result['status']:<18}"
            f"{bars if bars is not None else '-':>6}  "
            f"{date_source:<22}"
            f"{zero_vol if zero_vol is not None else '-':>14}  {reason}"
        )
    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    summary = ", ".join(f"{status}: {count}" for status, count in sorted(counts.items()))
    print(f"Total: {len(results)} ({summary})")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    today = fetch._today()
    _validate_args(args, parser, today)

    symbols = _load_universe(args.universe, parser)

    try:
        exclusions_path, exclusions = load_active_exclusions()
    except SymbolGuardError as exc:
        parser.error(str(exc))
        return 2
    excl = exclusions_report(exclusions_path, exclusions)
    print(
        f"Exclusions: {excl['exclusions_file']} (sha256={excl['exclusions_file_sha256']}; "
        f"excluded: {', '.join(excl['excluded_symbols']) or 'none'})"
    )

    results = run_symbols(
        symbols, args.start_date, args.end_date, exclusions=exclusions,
        save=args.save, pacing_seconds=args.pacing_seconds, timeout=args.timeout,
    )

    _print_table(results)
    any_failed = any(result["status"] == "failed" for result in results)
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
