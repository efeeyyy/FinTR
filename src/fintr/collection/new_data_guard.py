"""Offline "is a new trading day expected?" check for the daily pipeline.

Runs before ``collect_daily.py`` (so before any initData prompt or
connection). Reads only local files: the saved Mini App run records
(``_runs``), the saved price-history captures (the trading-day calendar) and
the latest saved scanner output (settlement state). No network, no clock
beyond the caller-supplied run date.

Decision for window end ``E = R - end_offset_days`` (the planner's):

1. An unsettled day is pending (or the settlement state is unknown) ->
   collect; the rolling window re-reads recent days to close late custody
   settlement and provider revisions, even when no new day is expected.
2. ``E`` is a Saturday or Sunday -> no new trading day.
3. ``E`` is a weekday inside the price-history coverage (``E <=`` the latest
   requested end of a saved price file) but has no tradeable bar in any
   symbol's history -> holiday, no new trading day. The calendar is derived
   from price history only, so a holiday that no price fetch has covered yet
   is unknown and is collected as before (once; the price step of that run
   then records it).
4. Every planned symbol's last saved window end (``max`` of run record and
   base-capture floor, as in the planner) is ``>= E`` -> already collected.
5. Otherwise a new trading day is expected -> collect.

Any missing or unreadable input resolves to "collect": the guard may only
skip when it can prove nothing new is expected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Collection, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fintr.data.price_history import (
    PriceHistoryCaptureError,
    capture_last_bar_incomplete,
    load_price_history_capture,
)
from fintr.research.engine import tradeable_price_bars


REASON_UNSETTLED = "unsettled_days_pending"
REASON_WEEKEND = "weekend"
REASON_HOLIDAY = "holiday"
REASON_ALREADY_COLLECTED = "already_collected"
REASON_NEW_TRADING_DAY = "new_trading_day_expected"

_ISTANBUL_FIXED_OFFSET_HOURS = 3  # Türkiye: UTC+3 year-round since 2016, no DST.
_PRICE_FILE_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json$")
_SCANNER_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SCANNER_OBSERVATIONS_FILENAME = "observations.json"


@dataclass(frozen=True, slots=True)
class PriceCalendar:
    """Trading days seen in saved price history and how far the history reaches."""

    trading_days: frozenset[date]
    coverage_end: date | None
    # Requested ends whose saved file has a null close on that very day (saved
    # before the incomplete-last-bar guard): not a holiday, the close is missing.
    incomplete_ends: frozenset[date] = frozenset()

    def last_trading_day(self, on_or_before: date) -> date | None:
        days = [day for day in self.trading_days if day <= on_or_before]
        return max(days) if days else None


@dataclass(frozen=True, slots=True)
class NewDataDecision:
    expect_new_data: bool
    reason: str
    window_end: date
    last_trading_day: date | None


def istanbul_today(*, now: datetime | None = None) -> date:
    """Run day R: the Europe/Istanbul calendar date (same rule as ``collect_daily``)."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    try:
        tzinfo = ZoneInfo("Europe/Istanbul")
    except ZoneInfoNotFoundError:
        tzinfo = timezone(timedelta(hours=_ISTANBUL_FIXED_OFFSET_HOURS))
    return now.astimezone(tzinfo).date()


def load_price_calendar(price_dir: Path, symbols: Iterable[str]) -> PriceCalendar:
    """Calendar from each symbol's latest-ending valid saved price file.

    Unreadable or invalid files are skipped (the next older one is used):
    less history can only make the guard collect, never skip.
    """
    root = Path(price_dir)
    days: set[date] = set()
    incomplete: set[date] = set()
    coverage_end: date | None = None
    for symbol in symbols:
        symbol_dir = root / symbol
        if not symbol_dir.is_dir():
            continue
        files: list[tuple[date, Path]] = []
        for entry in symbol_dir.iterdir():
            match = _PRICE_FILE_NAME.match(entry.name) if entry.is_file() else None
            if match is None:
                continue
            try:
                files.append((date.fromisoformat(match.group(2)), entry))
            except ValueError:
                continue
        for _, path in sorted(files, reverse=True):  # newest first; first valid file wins
            try:
                capture = load_price_history_capture(path)
            except (OSError, PriceHistoryCaptureError):
                continue
            days.update(bar.trade_date for bar in tradeable_price_bars(capture))
            if capture_last_bar_incomplete(capture):
                incomplete.add(capture.requested_end)
            if coverage_end is None or capture.requested_end > coverage_end:
                coverage_end = capture.requested_end
            break
    return PriceCalendar(frozenset(days), coverage_end, frozenset(incomplete))


def missing_close_day(*, window_end: date, calendar: PriceCalendar, as_of: date) -> bool:
    """True when the scan stopped at ``as_of`` before the collected day
    ``window_end`` because that weekday has no close in the saved price history.

    Not a weekend, and not a holiday (a window end inside the price coverage
    without a tradeable bar and without a null last bar). ``False`` when
    ``window_end`` has a tradeable bar: then a stale ``as_of`` is not a price
    gap (e.g. the AKD data lags) and this check says nothing.
    """
    if as_of >= window_end or window_end.weekday() >= 5:
        return False
    if window_end in calendar.trading_days:
        return False
    if window_end in calendar.incomplete_ends:
        return True
    # No saved price history at all: nothing to compare with, so no claim.
    return calendar.coverage_end is not None and calendar.coverage_end < window_end


def scanner_unsettled_pending(scanner_dir: Path) -> bool:
    """True when the latest saved scanner output lists an unsettled in-window
    day for any symbol, or when that state cannot be read (unknown = pending)."""
    root = Path(scanner_dir)
    if not root.is_dir():
        return True
    labels = sorted(
        entry.name for entry in root.iterdir()
        if entry.is_dir() and _SCANNER_DATE_DIR.match(entry.name)
        and (entry / SCANNER_OBSERVATIONS_FILENAME).is_file()
    )
    if not labels:
        return True
    try:
        document = json.loads(
            (root / labels[-1] / SCANNER_OBSERVATIONS_FILENAME).read_text(encoding="utf-8")
        )
        symbols = document["symbols"]
        if not isinstance(symbols, dict) or not symbols:
            return True
        for block in symbols.values():
            count = block["composition"]["unsettled"]["in_window_day_count"]
            if type(count) is not int or count < 0:
                return True
            if count > 0:
                return True
    except (OSError, ValueError, KeyError, TypeError):
        return True
    return False


def last_saved_end(
    symbols: Collection[str], last_success: Mapping[str, date], base_ends: Mapping[str, date],
) -> date | None:
    """Earliest per-symbol last saved window end; ``None`` if any symbol has none."""
    ends: list[date] = []
    for symbol in symbols:
        known = [end for end in (last_success.get(symbol), base_ends.get(symbol)) if end is not None]
        if not known:
            return None
        ends.append(max(known))
    return min(ends) if ends else None


def decide_new_data(
    *, window_end: date, calendar: PriceCalendar, last_saved: date | None, unsettled_pending: bool,
) -> NewDataDecision:
    last_trading = calendar.last_trading_day(window_end)

    def decision(expect: bool, reason: str) -> NewDataDecision:
        return NewDataDecision(expect, reason, window_end, last_trading)

    if unsettled_pending:
        return decision(True, REASON_UNSETTLED)
    if window_end.weekday() >= 5:
        return decision(False, REASON_WEEKEND)
    if (
        calendar.coverage_end is not None and window_end <= calendar.coverage_end
        and window_end not in calendar.trading_days
    ):
        return decision(False, REASON_HOLIDAY)
    if last_saved is not None and last_saved >= window_end:
        return decision(False, REASON_ALREADY_COLLECTED)
    return decision(True, REASON_NEW_TRADING_DAY)
