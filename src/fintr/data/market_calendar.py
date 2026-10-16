"""BIST equity trading calendar (holidays) from ``configs/market/bist_holidays.json``.

Fail closed: any malformed, unsorted, duplicate, unknown-key or unapproved
content raises ``MarketCalendarError``, and any date outside the covered range
raises ``OutsideCoverageError`` (never a silent "no holiday" answer).

Only the exchange's own full closures and half days are listed; a weekend is
never a trading day here whatever the file lists. A half day is a trading day.
Per-day ``status`` (``official``, ``uncertain``, ``expected_not_announced``) is
kept as source provenance; every status counts as listed (Efe approved the
file on 2026-10-16, see the file's ``notes``). Single-stock halts, circuit
breakers and technical outages are out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import json
from pathlib import Path
import re

MARKET_CALENDAR_SCHEMA_VERSION = 1
DEFAULT_MARKET_CALENDAR_PATH = Path("configs/market/bist_holidays.json")

KIND_FULL_CLOSURE = "full_closure"
KIND_HALF_DAY = "half_day"
_KINDS = frozenset({KIND_FULL_CLOSURE, KIND_HALF_DAY})
_DAY_STATUSES = frozenset({"official", "uncertain", "expected_not_announced"})
_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_SESSION_END = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_TOP_KEYS = {
    "schema_version", "status", "verified_by", "verified_on", "market", "coverage",
    "generated_utc", "notes", "sources", "days",
}
_COVERAGE_KEYS = {"start", "end"}
_SOURCE_KEYS = {"id", "url", "accessed_utc", "title"}
_DAY_KEYS = {"date", "weekday", "kind", "session_end_local", "name_tr", "source_id", "status", "note"}


class MarketCalendarError(ValueError):
    """The calendar config is invalid or does not support the request."""


class OutsideCoverageError(MarketCalendarError):
    """The date (or the search for a neighbouring trading day) leaves the covered range."""


@dataclass(frozen=True, slots=True)
class MarketDay:
    date: date
    kind: str
    status: str
    name_tr: str
    session_end_local: str | None
    source_id: str


@dataclass(frozen=True, slots=True)
class MarketCalendar:
    coverage_start: date
    coverage_end: date
    days: tuple[MarketDay, ...]

    def covers(self, day: date) -> bool:
        return self.coverage_start <= day <= self.coverage_end

    def check_covered(self, day: date) -> None:
        if not self.covers(day):
            raise OutsideCoverageError(
                f"date_outside_calendar_coverage: {day.isoformat()} not in "
                f"{self.coverage_start.isoformat()}..{self.coverage_end.isoformat()}"
            )

    def listed_day(self, day: date) -> MarketDay | None:
        """The listed closure/half-day entry for a covered ``day``, else ``None``."""
        self.check_covered(day)
        for entry in self.days:
            if entry.date == day:
                return entry
        return None

    def is_full_closure(self, day: date) -> bool:
        """True when ``day`` is a listed full closure (a listed weekend day counts)."""
        entry = self.listed_day(day)
        return entry is not None and entry.kind == KIND_FULL_CLOSURE

    def is_half_day(self, day: date) -> bool:
        entry = self.listed_day(day)
        return entry is not None and entry.kind == KIND_HALF_DAY

    def is_trading_day(self, day: date) -> bool:
        """A covered weekday that is not a full closure; a half day trades."""
        self.check_covered(day)
        return day.weekday() < 5 and not self.is_full_closure(day)

    def next_trading_day(self, day: date) -> date:
        """The first trading day strictly after ``day``; raises past the coverage end."""
        return self._step(day, 1)

    def previous_trading_day(self, day: date) -> date:
        """The last trading day strictly before ``day``; raises before the coverage start."""
        return self._step(day, -1)

    def _step(self, day: date, direction: int) -> date:
        self.check_covered(day)
        current = day
        while True:
            current += timedelta(days=direction)
            self.check_covered(current)
            if self.is_trading_day(current):
                return current


def _fail(path: str, message: str) -> MarketCalendarError:
    return MarketCalendarError(f"{path}: {message}")


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(path, "expected a non-empty string")
    return value


def _parse_date(value: object, path: str) -> date:
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise _fail(path, "expected YYYY-MM-DD") from None


def _exact_keys(raw: object, expected: set[str], path: str) -> dict:
    if not isinstance(raw, dict):
        raise _fail(path, "expected an object")
    unknown = set(raw) - expected
    if unknown:
        raise _fail(path, f"unknown_keys: {sorted(unknown)}")
    missing = expected - set(raw)
    if missing:
        raise _fail(path, f"missing_keys: {sorted(missing)}")
    return raw


def parse_market_calendar(document: object) -> MarketCalendar:
    raw = _exact_keys(document, _TOP_KEYS, "calendar")
    version = raw["schema_version"]
    if isinstance(version, bool) or version != MARKET_CALENDAR_SCHEMA_VERSION:
        raise _fail("schema_version", "unsupported_market_calendar_schema")
    if raw["status"] != "approved":
        raise _fail("status", "calendar must be approved (not a draft)")
    _text(raw["verified_by"], "verified_by")
    _parse_date(raw["verified_on"], "verified_on")
    _text(raw["market"], "market")
    _text(raw["generated_utc"], "generated_utc")
    notes = raw["notes"]
    if not isinstance(notes, list) or not notes:
        raise _fail("notes", "expected a non-empty list")
    for index, note in enumerate(notes):
        _text(note, f"notes[{index}]")

    coverage = _exact_keys(raw["coverage"], _COVERAGE_KEYS, "coverage")
    start = _parse_date(coverage["start"], "coverage.start")
    end = _parse_date(coverage["end"], "coverage.end")
    if start > end:
        raise _fail("coverage", "start must not be after end")

    raw_sources = raw["sources"]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise _fail("sources", "expected a non-empty list")
    source_ids: set[str] = set()
    for index, source in enumerate(raw_sources):
        path = f"sources[{index}]"
        entry = _exact_keys(source, _SOURCE_KEYS, path)
        for key in _SOURCE_KEYS:
            _text(entry[key], f"{path}.{key}")
        if entry["id"] in source_ids:
            raise _fail(f"{path}.id", "duplicate source id")
        source_ids.add(entry["id"])

    raw_days = raw["days"]
    if not isinstance(raw_days, list) or not raw_days:
        raise _fail("days", "expected a non-empty list")
    days: list[MarketDay] = []
    for index, item in enumerate(raw_days):
        path = f"days[{index}]"
        entry = _exact_keys(item, _DAY_KEYS, path)
        day = _parse_date(entry["date"], f"{path}.date")
        if not start <= day <= end:
            raise _fail(f"{path}.date", "outside the declared coverage")
        if days and day <= days[-1].date:
            raise _fail(f"{path}.date", "must be strictly increasing (sorted, unique)")
        if entry["weekday"] != _WEEKDAY_NAMES[day.weekday()]:
            raise _fail(f"{path}.weekday", "does not match the date")
        kind = entry["kind"]
        if kind not in _KINDS:
            raise _fail(f"{path}.kind", f"expected one of {sorted(_KINDS)}")
        status = entry["status"]
        if status not in _DAY_STATUSES:
            raise _fail(f"{path}.status", f"expected one of {sorted(_DAY_STATUSES)}")
        session_end = entry["session_end_local"]
        if kind == KIND_FULL_CLOSURE and session_end is not None:
            raise _fail(f"{path}.session_end_local", "must be null for a full closure")
        if session_end is not None and (not isinstance(session_end, str) or not _SESSION_END.match(session_end)):
            raise _fail(f"{path}.session_end_local", "expected HH:MM or null")
        name = _text(entry["name_tr"], f"{path}.name_tr")
        if not isinstance(entry["note"], str):
            raise _fail(f"{path}.note", "expected a string")
        source_id = _text(entry["source_id"], f"{path}.source_id")
        if source_id not in source_ids:
            raise _fail(f"{path}.source_id", "not listed in sources")
        days.append(MarketDay(day, kind, status, name, session_end, source_id))
    return MarketCalendar(start, end, tuple(days))


def load_market_calendar(path: Path = DEFAULT_MARKET_CALENDAR_PATH) -> MarketCalendar:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise MarketCalendarError("unreadable_market_calendar_file") from None
    return parse_market_calendar(document)
