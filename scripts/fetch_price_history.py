"""Fetch daily OHLCV history for one BIST symbol from Yahoo Finance's public
chart endpoint and save it as a provenance-tracked capture artifact.

Source: ``https://query1.finance.yahoo.com/v8/finance/chart/<SYMBOL>.IS``, an
**unofficial secondary source**, not Borsa Istanbul official data. See
``docs/price_history.md``.

No retries beyond a small bounded count, a response size cap, and a fixed
User-Agent header. ``--dry-run`` never touches the network.

A symbol listed in ``configs/exclusions/symbols.json`` is refused before any
URL is built or connection opened, in every mode (no override flag).
Holdout symbols of the frozen BIST 30 split are not blocked: collection is
allowed, analysis is guarded (see docs/evaluation_protocol.md).
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fintr.data.period_capture import PeriodCaptureError, load_period_capture
from fintr.data.price_history import (
    PriceHistoryParseError,
    canonical_json,
    filter_to_range,
    last_bar_incomplete,
    normalized_payload,
    parse_chart_json,
)
from fintr.evaluation.symbol_guard import SymbolGuardError, load_active_exclusions


PROJECT_ROOT = Path(__file__).resolve().parents[1]

CAPTURE_SCHEMA = "fintr_price_history_capture_v1"
SOURCE_ID = "yahoo_finance_chart_v8"
CHART_ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# Recorded verbatim per the manager-verified permission basis (2026-09-26);
# this is an unofficial secondary source, not Borsa Istanbul official data.
INCOMPLETE_LAST_BAR_STATUS = "skipped_incomplete_last_bar"
PERMISSION_STATUS = "user_asserted_2026-09-26_not_independently_verified"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ATTEMPTS = 2
DEFAULT_TIMEOUT_SECONDS = 15.0
# Widens the requested Yahoo window beyond [start, end] so exchange-timezone
# offsets near the boundary can't silently drop an in-range trading day;
# filter_to_range() then drops and counts anything still outside the range.
QUERY_BUFFER_DAYS = 5

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,16}$")


class FetchError(RuntimeError):
    """A fixed, payload-free reason the chart-endpoint request failed."""


def _symbol(value: str) -> str:
    candidate = value.strip().upper()
    if not _SYMBOL_RE.fullmatch(candidate):
        raise argparse.ArgumentTypeError("expected a BIST symbol like AKFIS (A-Z0-9, 1-16 chars)")
    return candidate


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _today() -> date:
    """Local calendar date; tests override via ``patch.object(module, "_today", ...)``."""
    return date.today()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch daily OHLCV history for one BIST symbol from Yahoo Finance's "
            "public chart endpoint (unofficial secondary source)."
        ),
    )
    parser.add_argument("--symbol", required=True, type=_symbol, help="BIST symbol, e.g. AKFIS")
    parser.add_argument("--start-date", required=True, type=_date)
    parser.add_argument("--end-date", required=True, type=_date)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the request URL and target save path; opens no network connection.",
    )
    parser.add_argument(
        "--save", action="store_true",
        help="Fetch and atomically save the capture artifact; never overwrites an existing file.",
    )
    parser.add_argument(
        "--compare-capture", type=Path, default=None, metavar="PERIOD_CAPTURE_JSON",
        help=(
            "After fetching, print only a date-coverage comparison against a saved "
            "fintr_period_analysis_capture_v1 file's server dates: dates present in "
            "one side but not the other. Prints no price values."
        ),
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-attempt network timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g}).",
    )
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser, today: date) -> None:
    if args.start_date > args.end_date:
        parser.error("--start-date must not be after --end-date")
    if args.end_date > today:
        parser.error(f"--end-date {args.end_date.isoformat()} is after today ({today.isoformat()})")
    if args.dry_run and (args.save or args.compare_capture is not None):
        parser.error("--dry-run cannot be combined with --save or --compare-capture (no network)")
    if not 0 < args.timeout <= 60:
        parser.error("--timeout must be greater than 0 and at most 60 seconds")


def _unix_query_bounds(start: date, end: date) -> tuple[int, int]:
    buffer = timedelta(days=QUERY_BUFFER_DAYS)
    period1 = datetime.combine(start - buffer, time.min, tzinfo=timezone.utc)
    period2 = datetime.combine(end + buffer, time.min, tzinfo=timezone.utc)
    return int(period1.timestamp()), int(period2.timestamp())


def build_request_url(symbol: str, start: date, end: date) -> str:
    yahoo_symbol = f"{symbol}.IS"
    period1, period2 = _unix_query_bounds(start, end)
    query = urllib.parse.urlencode({"period1": period1, "period2": period2, "interval": "1d"})
    base = CHART_ENDPOINT.format(symbol=urllib.parse.quote(yahoo_symbol, safe=""))
    return f"{base}?{query}"


def target_path(symbol: str, start: date, end: date) -> Path:
    return (
        PROJECT_ROOT / "data" / "interim" / "price_history" / symbol
        / f"{start.isoformat()}_{end.isoformat()}.json"
    )


def _fetch_raw(url: str, *, timeout: float) -> bytes:
    """GET with a browser User-Agent, a bounded size cap, and at most two attempts."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    last_error: str | None = None
    for _attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https host)
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            last_error = f"http_{exc.code}"
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"network_error: {exc}"
            continue
        if len(body) > MAX_RESPONSE_BYTES:
            raise FetchError(f"response_too_large: exceeded {MAX_RESPONSE_BYTES} bytes")
        return body
    raise FetchError(f"fetch_failed_after_{MAX_ATTEMPTS}_attempts: {last_error}")


def _reject_json_constant(token: str) -> None:
    raise PriceHistoryParseError(f"non_finite_json_constant: {token}")


def _decode_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PriceHistoryParseError("response_not_utf8") from exc
    return json.loads(text, parse_constant=_reject_json_constant)


def build_capture(
    *, symbol: str, start: date, end: date, request_url: str, raw_bytes: bytes,
    accessed_at: datetime,
) -> dict[str, Any]:
    """Parse, filter, and wrap one fetched response into the capture artifact shape."""
    raw_document = _decode_json(raw_bytes)
    history = parse_chart_json(raw_document)
    filtered = filter_to_range(history, start, end)
    normalized = normalized_payload(filtered)
    normalized_bytes = canonical_json(normalized).encode("utf-8")
    if accessed_at.tzinfo is None or accessed_at.utcoffset() is None:
        raise ValueError("accessed_at must be timezone-aware")
    accessed_at_utc = accessed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "capture_metadata": {
            "schema": CAPTURE_SCHEMA,
            "source_id": SOURCE_ID,
            "request_url": request_url,
            "accessed_at_utc": accessed_at_utc,
            "permission_provenance": {
                "status": PERMISSION_STATUS,
                "source_note": (
                    "Unofficial secondary source (Yahoo Finance public chart endpoint), "
                    "not Borsa Istanbul official data."
                ),
            },
            "raw_response_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "normalized_sha256": hashlib.sha256(normalized_bytes).hexdigest(),
            "transformation": (
                "Yahoo Finance /v8/finance/chart response parsed with "
                "fintr.data.price_history.parse_chart_json (strict typed OHLCV parse; "
                "exchange-local trade date via meta.exchangeTimezoneName, falling back "
                "to meta.gmtoffset when the local IANA tz database is unavailable; raw "
                "close kept separate from adjusted close), then restricted to the "
                "requested [start_date, end_date] inclusive range with "
                "fintr.data.price_history.filter_to_range; points outside that range "
                "are dropped and counted in normalized.dropped_before_start_count / "
                "normalized.dropped_after_end_count."
            ),
        },
        "raw_response": raw_document,
        "normalized": normalized,
    }


def _save_capture(capture: dict[str, Any], symbol: str, start: date, end: date) -> Path:
    """Atomically create one capture file; refuses to overwrite an existing one."""
    payload = (json.dumps(capture, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    directory = PROJECT_ROOT / "data" / "interim" / "price_history"
    if directory.parent.parent.is_symlink() or directory.parent.is_symlink() or directory.is_symlink():
        raise FetchError("capture_directory_unavailable")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise OSError
        symbol_directory = directory / symbol
        if symbol_directory.is_symlink():
            raise OSError
        symbol_directory.mkdir(exist_ok=True)
        if (
            symbol_directory.is_symlink()
            or not symbol_directory.is_dir()
            or symbol_directory.resolve().parent != directory.resolve()
        ):
            raise OSError
    except OSError:
        raise FetchError("capture_directory_unavailable") from None

    target = symbol_directory / f"{start.isoformat()}_{end.isoformat()}.json"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".price-history-", suffix=".tmp", dir=symbol_directory, delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary_path, target)
    except FileExistsError:
        raise FetchError("capture_already_exists") from None
    except OSError:
        raise FetchError("capture_write_failed") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return target


def _print_coverage_comparison(normalized: dict[str, Any], capture_path: Path) -> int:
    """Print only date sets/counts; never a price, volume, or other numeric field."""
    try:
        period_capture = load_period_capture(capture_path)
    except PeriodCaptureError as exc:
        print(f"Comparison failed: unreadable/invalid period capture ({exc})")
        return 1
    server_dates = {day.isoformat() for day in period_capture.server_dates}
    price_dates = {bar["date"] for bar in normalized["bars"]}
    missing_in_price = sorted(server_dates - price_dates)
    not_in_server = sorted(price_dates - server_dates)
    print(f"Period capture: {capture_path}")
    print(f"Period capture server date count: {len(server_dates)}")
    print(f"Price history date count (within requested range): {len(price_dates)}")
    print(f"Server dates missing from price data: {len(missing_in_price)}")
    for day in missing_in_price:
        print(f"  - {day}")
    print(f"Price dates not in server dates: {len(not_in_server)}")
    for day in not_in_server:
        print(f"  - {day}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    today = _today()
    _validate_args(args, parser, today)
    try:
        _exclusions_path, exclusions = load_active_exclusions()
    except SymbolGuardError as exc:
        parser.error(str(exc))
    if exclusions.is_excluded(args.symbol):
        parser.error(
            f"symbol {args.symbol} is excluded by configs/exclusions/symbols.json "
            "(no override); nothing fetched"
        )

    url = build_request_url(args.symbol, args.start_date, args.end_date)
    path = target_path(args.symbol, args.start_date, args.end_date)

    if args.dry_run:
        print(f"Symbol: {args.symbol} (Yahoo: {args.symbol}.IS)")
        print(f"Range: {args.start_date.isoformat()}..{args.end_date.isoformat()} (inclusive)")
        print(f"Request URL: {url}")
        print(f"Target path: {path.relative_to(PROJECT_ROOT).as_posix()}")
        print(f"Target exists: {path.exists()}")
        print("Dry run: no network connection opened")
        return 0

    try:
        raw_bytes = _fetch_raw(url, timeout=args.timeout)
        capture = build_capture(
            symbol=args.symbol, start=args.start_date, end=args.end_date,
            request_url=url, raw_bytes=raw_bytes, accessed_at=datetime.now(timezone.utc),
        )
    except (FetchError, PriceHistoryParseError, ValueError) as exc:
        print(f"Fetch failed: {exc}")
        return 1

    normalized = capture["normalized"]
    if args.save and last_bar_incomplete(normalized):
        print(f"Save skipped: {INCOMPLETE_LAST_BAR_STATUS} (the close of "
              f"{args.end_date.isoformat()} is not published yet); nothing saved, fetch again later")
        return 1
    print(f"Symbol: {args.symbol} (Yahoo: {args.symbol}.IS)")
    print(f"Requested range: {args.start_date.isoformat()}..{args.end_date.isoformat()} (inclusive)")
    print(f"Bars in requested range: {normalized['bar_count']}")
    print(
        "Dropped (outside requested range): "
        f"{normalized['dropped_before_start_count']} before start, "
        f"{normalized['dropped_after_end_count']} after end"
    )
    print(f"Date source: {normalized['date_source']}")

    if args.save:
        try:
            saved_path = _save_capture(capture, args.symbol, args.start_date, args.end_date)
        except FetchError as exc:
            if str(exc) == "capture_already_exists":
                print("Save failed: capture already exists (no overwrite)")
            else:
                print(f"Save failed: {exc}")
            return 1
        print(f"Saved: {saved_path.relative_to(PROJECT_ROOT).as_posix()}")

    if args.compare_capture is not None:
        return _print_coverage_comparison(normalized, args.compare_capture)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
