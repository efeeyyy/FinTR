"""Offline detection of retroactive revisions in saved price-history captures.

Yahoo's chart endpoint is an unofficial secondary source and may change
stored history after the fact (2026-10-10 manual diff: 7 marker rows gone from
the files fetched on 2026-10-09; ``adjusted_close`` of two symbols revised on
932 dates). Saved capture files are never overwritten, so a revision shows up
as a *newer file that disagrees with the previous file* over the dates both
cover. This module compares, per symbol, the newest file with the previous one
(ordered by ``capture_metadata.accessed_at_utc``, NOT by the end date in the
file name) and classifies each differing date. No network, no file is
modified, and the report holds no price or volume value, only classes,
dates, field names and relative differences.

Per-date change types (one row per date, first match wins):

* ``removed`` / ``added``: the date has a bar in only one of the two files
  (within the overlapping requested range).
* ``marker_status_changed``: the bar class changed between ``trading``,
  ``placeholder`` and ``no_data_bar`` (the class comes from
  ``fintr.features.price_join.classify_price_bar``; its
  ``unexpected_trading_bar`` is reported here as ``trading``).
* ``raw_ohlcv_changed``: open/high/low/close/volume differ by at least the
  tolerance (relative) or flip between null and non-null.
* ``adjusted_close_changed``: only ``adjusted_close`` differs (same rule).

Relative difference = ``|a - b| / max(|a|, |b|)`` of the same field in the two
files; below the tolerance (default 1e-6) it is float noise and ignored (such
dates are only counted in ``noise_ignored_dates``).

Severity (a documented convention, not a statistical threshold):

* ``high``: a trading bar was removed or added, changed class, or any raw
  OHLCV value of a trading bar changed (these can move returns and the
  holiday calendar derived from price history).
* ``medium``: ``adjusted_close``-only change (raw prices unchanged, but
  adjusted-close based studies would move); a marker bar changing to another
  marker class; raw values of a marker bar changing.
* ``info``: a marker row (``no_data_bar``/``placeholder``) removed or added.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

from fintr.data.price_history import (
    DailyBar,
    PriceHistoryCapture,
    PriceHistoryCaptureError,
    parse_price_history_capture,
)
from fintr.features.price_join import classify_price_bar

REPORT_SCHEMA = "fintr_price_revisions_report_v1"
DEFAULT_TOLERANCE = Decimal("1e-6")
INCOMPLETE_DIRNAME = "_incomplete"
PRICE_FILE_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.json$")

CHANGE_TYPES = (
    "removed",
    "added",
    "marker_status_changed",
    "raw_ohlcv_changed",
    "adjusted_close_changed",
)
SEVERITIES = ("high", "medium", "info")
_RAW_FIELDS = ("open", "high", "low", "close", "volume")


class PriceRevisionError(Exception):
    """A capture file could not be read/validated or the inputs are inconsistent."""


@dataclass(frozen=True, slots=True)
class LoadedCapture:
    path: Path
    sha256: str
    capture: PriceHistoryCapture
    accessed_at: datetime


def _reject_constant(token: str) -> None:
    raise PriceRevisionError("non_finite_number_in_capture")


def _parse_accessed_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PriceRevisionError("unparseable_accessed_at_utc") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_capture(path: Path) -> LoadedCapture:
    """Read one saved capture, validate it (incl. ``normalized_sha256``), hash the file bytes."""
    path = Path(path)
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"), parse_float=Decimal,
                              parse_constant=_reject_constant)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PriceRevisionError(f"unreadable_capture: {path.name} ({type(exc).__name__})") from None
    try:
        capture = parse_price_history_capture(document)
    except PriceHistoryCaptureError as exc:
        raise PriceRevisionError(f"invalid_capture: {path.name} ({exc})") from None
    return LoadedCapture(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        capture=capture,
        accessed_at=_parse_accessed_at(capture.meta.accessed_at_utc),
    )


def bar_status(bar: DailyBar) -> str:
    """``trading`` | ``placeholder`` | ``no_data_bar`` (repo rule: classify_price_bar)."""
    label = classify_price_bar(bar)
    return "trading" if label == "unexpected_trading_bar" else label


def _rel_diff(a: Decimal | int | None, b: Decimal | int | None) -> Decimal | None:
    """None when exactly one side is null (null flip); 0 when equal."""
    if a is None and b is None:
        return Decimal(0)
    if a is None or b is None:
        return None
    da, db = Decimal(a), Decimal(b)
    if da == db:
        return Decimal(0)
    denominator = max(abs(da), abs(db))
    return abs(da - db) / denominator if denominator else None


def _sci(value: Decimal) -> str:
    return f"{value:.3E}"


def _field_changes(old: DailyBar, new: DailyBar, tolerance: Decimal) -> tuple[
    list[str], list[str], Decimal, Decimal, bool,
]:
    """(changed fields, null-flip fields, max rel raw, max rel adjusted_close, noise seen)."""
    changed: list[str] = []
    flips: list[str] = []
    max_raw = Decimal(0)
    max_adj = Decimal(0)
    noise = False
    for name in (*_RAW_FIELDS, "adjusted_close"):
        rel = _rel_diff(getattr(old, name), getattr(new, name))
        if rel is None:
            changed.append(name)
            flips.append(name)
            continue
        if rel == 0:
            continue
        if rel < tolerance:
            noise = True
            continue
        changed.append(name)
        if name == "adjusted_close":
            max_adj = max(max_adj, rel)
        else:
            max_raw = max(max_raw, rel)
    return changed, flips, max_raw, max_adj, noise


def _row(day: date, change_type: str, severity: str, old_status: str | None,
         new_status: str | None, fields: list[str] | None = None,
         null_flips: list[str] | None = None, max_raw: Decimal | None = None,
         max_adj: Decimal | None = None) -> dict[str, Any]:
    return {
        "date": day.isoformat(),
        "change_type": change_type,
        "severity": severity,
        "old_status": old_status,
        "new_status": new_status,
        "fields_changed": fields or [],
        "null_flip_fields": null_flips or [],
        "max_rel_diff_raw_ohlcv": None if max_raw is None else _sci(max_raw),
        "max_rel_diff_adjusted_close": None if max_adj is None else _sci(max_adj),
    }


def compare_bars(old: PriceHistoryCapture, new: PriceHistoryCapture,
                 tolerance: Decimal = DEFAULT_TOLERANCE) -> tuple[list[dict[str, Any]], int]:
    """Per-date change rows over the overlapping requested range, plus the
    number of dates whose only differences were below the tolerance."""
    low = max(old.requested_start, new.requested_start)
    high = min(old.requested_end, new.requested_end)
    old_bars = {bar.trade_date: bar for bar in old.bars if low <= bar.trade_date <= high}
    new_bars = {bar.trade_date: bar for bar in new.bars if low <= bar.trade_date <= high}
    rows: list[dict[str, Any]] = []
    noise_dates = 0
    for day in sorted(set(old_bars) | set(new_bars)):
        a, b = old_bars.get(day), new_bars.get(day)
        if b is None:
            status = bar_status(a)
            rows.append(_row(day, "removed", "high" if status == "trading" else "info", status, None))
            continue
        if a is None:
            status = bar_status(b)
            rows.append(_row(day, "added", "high" if status == "trading" else "info", None, status))
            continue
        old_status, new_status = bar_status(a), bar_status(b)
        fields, flips, max_raw, max_adj, noise = _field_changes(a, b, tolerance)
        raw_changed = any(name in _RAW_FIELDS for name in fields)
        if old_status != new_status:
            severity = "high" if "trading" in (old_status, new_status) else "medium"
            rows.append(_row(day, "marker_status_changed", severity, old_status, new_status,
                             fields, flips, max_raw, max_adj))
        elif raw_changed:
            severity = "high" if new_status == "trading" else "medium"
            rows.append(_row(day, "raw_ohlcv_changed", severity, old_status, new_status,
                             fields, flips, max_raw, max_adj))
        elif fields:
            rows.append(_row(day, "adjusted_close_changed", "medium", old_status, new_status,
                             fields, flips, max_raw, max_adj))
        elif noise:
            noise_dates += 1
    return rows, noise_dates


def _file_info(item: LoadedCapture, price_dir: Path | None) -> dict[str, Any]:
    try:
        shown = item.path.relative_to(price_dir).as_posix() if price_dir else item.path.as_posix()
    except ValueError:
        shown = item.path.as_posix()
    return {
        "path": shown,
        "sha256": item.sha256,
        "accessed_at_utc": item.capture.meta.accessed_at_utc,
        "requested_start_date": item.capture.requested_start.isoformat(),
        "requested_end_date": item.capture.requested_end.isoformat(),
        "bar_count": len(item.capture.bars),
    }


def compare_pair(symbol: str, old: LoadedCapture, new: LoadedCapture,
                 tolerance: Decimal = DEFAULT_TOLERANCE,
                 price_dir: Path | None = None) -> dict[str, Any]:
    """One per-symbol report entry for an explicit (old, new) pair."""
    entry: dict[str, Any] = {
        "symbol": symbol,
        "status": "compared",
        "old_file": _file_info(old, price_dir),
        "new_file": _file_info(new, price_dir),
    }
    low = max(old.capture.requested_start, new.capture.requested_start)
    high = min(old.capture.requested_end, new.capture.requested_end)
    if low > high:
        entry.update(status="no_overlap", overlap=None, counts=_empty_counts(),
                     severity_counts=_empty_severities(), noise_ignored_dates=0, changes=[])
        return entry
    rows, noise = compare_bars(old.capture, new.capture, tolerance)
    counts = _empty_counts()
    severities = _empty_severities()
    for row in rows:
        counts[row["change_type"]] += 1
        severities[row["severity"]] += 1
    entry.update(
        overlap={"start": low.isoformat(), "end": high.isoformat()},
        counts=counts,
        severity_counts=severities,
        noise_ignored_dates=noise,
        changes=rows,
    )
    return entry


def _empty_counts() -> dict[str, int]:
    return {name: 0 for name in CHANGE_TYPES}


def _empty_severities() -> dict[str, int]:
    return {name: 0 for name in SEVERITIES}


def list_symbol_files(symbol_dir: Path) -> list[Path]:
    return sorted(p for p in Path(symbol_dir).iterdir()
                  if p.is_file() and PRICE_FILE_NAME.match(p.name))


def discover_symbols(price_dir: Path) -> list[str]:
    return sorted(p.name for p in Path(price_dir).iterdir()
                  if p.is_dir() and p.name != INCOMPLETE_DIRNAME)


def fetch_order_key(item: LoadedCapture) -> tuple[datetime, str, str]:
    """Fetch time first; end date / file name only break exact ties."""
    return (item.accessed_at, item.capture.requested_end.isoformat(), item.path.name)


def compare_symbol(price_dir: Path, symbol: str,
                   tolerance: Decimal = DEFAULT_TOLERANCE) -> dict[str, Any]:
    """Newest vs previous file of ``price_dir/<symbol>`` by fetch time."""
    symbol_dir = Path(price_dir) / symbol
    if not symbol_dir.is_dir():
        raise PriceRevisionError(f"unknown_symbol: {symbol}")
    files = list_symbol_files(symbol_dir)
    if len(files) < 2:
        return {"symbol": symbol, "status": "single_file" if files else "no_files",
                "file_count": len(files), "counts": _empty_counts(),
                "severity_counts": _empty_severities(), "noise_ignored_dates": 0, "changes": []}
    loaded = sorted((load_capture(p) for p in files), key=fetch_order_key)
    old, new = loaded[-2], loaded[-1]
    entry = compare_pair(symbol, old, new, tolerance, price_dir)
    entry["file_count"] = len(files)
    return entry


def build_report(entries: list[dict[str, Any]], errors: list[dict[str, str]], *,
                 tolerance: Decimal, mode: str, price_dir: Path | None,
                 symbols_filter: list[str] | None, now: datetime | None = None) -> dict[str, Any]:
    totals = {"symbols": len(entries), "symbols_with_changes": 0, "changes": 0,
              "by_change_type": _empty_counts(), "by_severity": _empty_severities()}
    for entry in entries:
        n = len(entry["changes"])
        totals["changes"] += n
        totals["symbols_with_changes"] += 1 if n else 0
        for key, value in entry["counts"].items():
            totals["by_change_type"][key] += value
        for key, value in entry["severity_counts"].items():
            totals["by_severity"][key] += value
    moment = datetime.now(timezone.utc) if now is None else now
    return {
        "schema_version": REPORT_SCHEMA,
        "generated_at_utc": moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": {
            "description": "newest vs previous saved price file per symbol, ordered by "
                           "capture_metadata.accessed_at_utc, over the overlapping requested range",
            "relative_difference": "abs(a - b) / max(abs(a), abs(b)), same field in the two files",
            "bar_status_rule": "fintr.features.price_join.classify_price_bar "
                               "(unexpected_trading_bar reported as trading)",
            "contains_price_values": False,
        },
        "parameters": {
            "mode": mode,
            "relative_tolerance": format(tolerance, "E"),
            "price_dir": None if price_dir is None else Path(price_dir).as_posix(),
            "symbols_filter": symbols_filter,
        },
        "totals": totals,
        "errors": errors,
        "symbols": entries,
    }


def run_price_dir(price_dir: Path, symbols: Iterable[str] | None = None,
                  tolerance: Decimal = DEFAULT_TOLERANCE, now: datetime | None = None,
                  ) -> dict[str, Any]:
    """Compare every symbol directory (or the given symbols) under ``price_dir``."""
    price_dir = Path(price_dir)
    if not price_dir.is_dir():
        raise PriceRevisionError("price_dir_not_found")
    wanted = None if symbols is None else sorted({s.strip() for s in symbols if s.strip()})
    names = discover_symbols(price_dir) if wanted is None else wanted
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for name in names:
        try:
            entries.append(compare_symbol(price_dir, name, tolerance))
        except PriceRevisionError as exc:
            errors.append({"symbol": name, "error": str(exc)})
    return build_report(entries, errors, tolerance=tolerance, mode="price_dir",
                        price_dir=price_dir, symbols_filter=wanted, now=now)


def run_pair(old_path: Path, new_path: Path, tolerance: Decimal = DEFAULT_TOLERANCE,
             now: datetime | None = None) -> dict[str, Any]:
    """Compare an explicit (old, new) file pair; both must be the same symbol."""
    old, new = load_capture(old_path), load_capture(new_path)
    if old.capture.symbol != new.capture.symbol:
        raise PriceRevisionError("symbol_mismatch")
    entry = compare_pair(old.capture.symbol, old, new, tolerance)
    return build_report([entry], [], tolerance=tolerance, mode="explicit_pair",
                        price_dir=None, symbols_filter=None, now=now)


def exit_code_for(report: dict[str, Any]) -> int:
    """0 no changes, 2 changes found (a warning), 1 errors recorded in the report."""
    if report["errors"]:
        return 1
    return 2 if report["totals"]["changes"] else 0


def text_summary(report: dict[str, Any]) -> str:
    """Short text: counts and dates only, no price values."""
    totals = report["totals"]
    lines = [
        f"price revisions ({report['parameters']['mode']}, tolerance "
        f"{report['parameters']['relative_tolerance']}): {totals['symbols']} symbols, "
        f"{totals['symbols_with_changes']} with changes, {totals['changes']} changed dates "
        f"(high {totals['by_severity']['high']}, medium {totals['by_severity']['medium']}, "
        f"info {totals['by_severity']['info']})",
    ]
    for entry in report["symbols"]:
        if entry["status"] in ("single_file", "no_files"):
            continue
        if entry["status"] == "no_overlap":
            lines.append(f"  {entry['symbol']}: files do not overlap")
            continue
        if not entry["changes"]:
            continue
        counts = ", ".join(f"{k} {v}" for k, v in entry["counts"].items() if v)
        lines.append(
            f"  {entry['symbol']}: {counts} (old {entry['old_file']['path']} -> "
            f"new {entry['new_file']['path']})"
        )
    for err in report["errors"]:
        lines.append(f"  ERROR {err['symbol']}: {err['error']}")
    return "\n".join(lines)
