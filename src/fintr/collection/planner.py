"""Pure, offline window planner for daily incremental collection.

Implements the window rule from ``docs/research/daily_miniapp_collection_adr.md``
§1 ("Toplama birimi: kayan pencere") and the scope rule from §7. No network,
no clock reads beyond the caller-supplied run date, no secret prompt, no
randomness -- the same inputs always produce the same plan.

Window rule:
    ``E = R - end_offset_days`` (default 1: the same-day data may be
    incomplete). ``S = E - lookback_calendar_days`` (default 14).
    Catch-up: when a symbol's last successful window end ``L`` is known
    (the latest saved run-record end, or the end date of its base one-year
    capture when that file exists, whichever is later),
    ``S = min(S, L - lookback_calendar_days)`` -- the window only ever
    grows backwards to close a gap, never shrinks past the default
    lookback. When the resulting span ``E - S`` exceeds ``max_span_days``
    (default 90), the symbol is blocked rather than silently sending a
    large request; a human decides (a future ``--allow-large-range``,
    mirroring ``scripts/live_collector_smoke.py``, is not implemented by
    this PR -- only the block is).

Scope rule: only the active split's development symbols are planned.
Holdout symbols are reported as ``skipped_holdout`` (never silently
dropped, so the plan stays auditable); any symbol named in
``configs/exclusions/symbols.json`` is reported as ``skipped_excluded``
even when the split does not itself contain it (e.g. DSTKF, which was
removed from the pool before the active split was drawn).

This module never reads ``configs/collection/daily_miniapp_v1.json``
directly -- ``load_daily_collection_config`` below does that, and
``scripts/collect_daily.py`` is the only thing that wires the two
together with ``fintr.evaluation.symbol_guard.load_active_guards``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from fintr.evaluation.symbol_guard import ActiveGuards


PLANNER_VERSION = "daily_collection_planner_v1"
CONFIG_SCHEMA = "fintr_daily_collection_config_v1"
RUN_RECORD_SCHEMA = "fintr_daily_collection_run_v1"

DEFAULT_END_OFFSET_DAYS = 1
DEFAULT_LOOKBACK_CALENDAR_DAYS = 14
DEFAULT_MAX_SPAN_DAYS = 90

STATUS_PLANNED = "planned"
STATUS_SKIPPED_HOLDOUT = "skipped_holdout"
STATUS_SKIPPED_EXCLUDED = "skipped_excluded"
STATUS_BLOCKED_MAX_SPAN_EXCEEDED = "blocked_max_span_exceeded"

ROLE_DEVELOPMENT = "development"
ROLE_HOLDOUT = "holdout"
ROLE_EXCLUDED = "excluded"

_BASE_CAPTURE_FILENAME = re.compile(
    r"^(?P<start>\d{4}-\d{2}-\d{2})_(?P<end>\d{4}-\d{2}-\d{2})\.json$"
)


class CollectionPlannerError(ValueError):
    """A fixed reason the daily collection plan or its config could not be built."""


@dataclass(frozen=True, slots=True)
class CollectionWindow:
    """One symbol's planned ``custody_analysis``/``get_akd`` request range."""

    start: date
    end: date
    catch_up: bool

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise CollectionPlannerError("window_start_after_end")

    @property
    def span_days(self) -> int:
        return (self.end - self.start).days


@dataclass(frozen=True, slots=True)
class SymbolCollectionPlan:
    """One symbol's planned window, or the reason it was skipped/blocked."""

    symbol: str
    role: str  # development | holdout | excluded
    status: str  # planned | skipped_holdout | skipped_excluded | blocked_max_span_exceeded
    window: CollectionWindow | None


@dataclass(frozen=True, slots=True)
class DailyCollectionPlan:
    planner_version: str
    run_date: date
    window_end: date
    end_offset_days: int
    lookback_calendar_days: int
    max_span_days: int
    symbols: tuple[SymbolCollectionPlan, ...]

    def planned(self) -> tuple[SymbolCollectionPlan, ...]:
        return tuple(entry for entry in self.symbols if entry.status == STATUS_PLANNED)

    def blocked(self) -> tuple[SymbolCollectionPlan, ...]:
        return tuple(
            entry for entry in self.symbols if entry.status == STATUS_BLOCKED_MAX_SPAN_EXCEEDED
        )


def plan_daily_collection(
    *,
    run_date: date,
    guards: ActiveGuards,
    last_success_by_symbol: Mapping[str, date] | None = None,
    base_capture_end_by_symbol: Mapping[str, date] | None = None,
    end_offset_days: int = DEFAULT_END_OFFSET_DAYS,
    lookback_calendar_days: int = DEFAULT_LOOKBACK_CALENDAR_DAYS,
    max_span_days: int = DEFAULT_MAX_SPAN_DAYS,
) -> DailyCollectionPlan:
    """Plan one run's per-symbol windows; never opens a network connection.

    ``guards`` supplies both the active split (for the holdout/development
    role) and the exclusions (for the excluded role) -- the same object
    ``fintr.evaluation.symbol_guard.load_active_guards()`` returns, unlike
    most collection tools this one *does* consult the split, because §7 of
    the ADR restricts daily collection to development symbols only.

    ``base_capture_end_by_symbol`` is a last-success *floor*: a symbol whose
    base one-year capture exists counts as collected up to that capture's end
    date, so the very first daily window reaches back to the base end instead
    of leaving the days after it uncollected (see ``scan_base_capture_ends``).
    The effective last end is ``max(run-record last success, base end)``.
    """
    if type(end_offset_days) is not int or end_offset_days < 0:
        raise CollectionPlannerError("invalid_end_offset_days")
    if type(lookback_calendar_days) is not int or lookback_calendar_days < 1:
        raise CollectionPlannerError("invalid_lookback_calendar_days")
    if type(max_span_days) is not int or max_span_days < lookback_calendar_days:
        raise CollectionPlannerError("invalid_max_span_days")

    last_success = last_success_by_symbol or {}
    base_ends = base_capture_end_by_symbol or {}
    window_end = run_date - timedelta(days=end_offset_days)
    base_start = window_end - timedelta(days=lookback_calendar_days)

    all_symbols = sorted(
        set(guards.split.all_symbols()) | {entry.symbol for entry in guards.exclusions.entries}
    )

    plans: list[SymbolCollectionPlan] = []
    for symbol in all_symbols:
        if guards.is_excluded(symbol):
            plans.append(SymbolCollectionPlan(symbol, ROLE_EXCLUDED, STATUS_SKIPPED_EXCLUDED, None))
            continue
        if guards.is_holdout(symbol):
            plans.append(SymbolCollectionPlan(symbol, ROLE_HOLDOUT, STATUS_SKIPPED_HOLDOUT, None))
            continue

        start = base_start
        catch_up = False
        known_ends = [
            end for end in (last_success.get(symbol), base_ends.get(symbol))
            if end is not None
        ]
        last_end = max(known_ends) if known_ends else None
        if last_end is not None:
            catch_up_start = last_end - timedelta(days=lookback_calendar_days)
            if catch_up_start < start:
                start = catch_up_start
                catch_up = True

        if (window_end - start).days > max_span_days:
            plans.append(
                SymbolCollectionPlan(symbol, ROLE_DEVELOPMENT, STATUS_BLOCKED_MAX_SPAN_EXCEEDED, None)
            )
            continue

        plans.append(SymbolCollectionPlan(
            symbol, ROLE_DEVELOPMENT, STATUS_PLANNED,
            CollectionWindow(start=start, end=window_end, catch_up=catch_up),
        ))

    return DailyCollectionPlan(
        planner_version=PLANNER_VERSION, run_date=run_date, window_end=window_end,
        end_offset_days=end_offset_days, lookback_calendar_days=lookback_calendar_days,
        max_span_days=max_span_days, symbols=tuple(plans),
    )


@dataclass(frozen=True, slots=True)
class DailyCollectionConfig:
    schema: str
    source_id: str
    symbol_scope: str
    split_config: str
    exclusions_config: str
    end_offset_days: int
    lookback_calendar_days: int
    max_span_days: int
    symbol_pause_seconds: float
    max_wall_seconds: int
    capture_root: str
    permission_status: str
    stop_on_http_status: tuple[int, ...]
    raw: dict[str, Any]
    # Base one-year capture floor (optional): ``<root>/<SYMBOL>/<filename>``.
    base_capture_root: str | None = None
    base_capture_filename: str | None = None


def base_capture_end_date(filename: str) -> date:
    """End date of a base capture file named ``<YYYY-MM-DD>_<YYYY-MM-DD>.json``.

    The date comes from the configured filename, never from planner logic or
    the file's contents (which the planner never opens).
    """
    match = _BASE_CAPTURE_FILENAME.fullmatch(filename) if isinstance(filename, str) else None
    if match is None:
        raise CollectionPlannerError("invalid_base_capture_filename")
    try:
        start = date.fromisoformat(match["start"])
        end = date.fromisoformat(match["end"])
    except ValueError:
        raise CollectionPlannerError("invalid_base_capture_filename") from None
    if start > end:
        raise CollectionPlannerError("invalid_base_capture_filename")
    return end


def _parse_base_capture(document: Mapping[str, Any]) -> tuple[str | None, str | None]:
    raw = document.get("base_capture")
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise CollectionPlannerError("invalid_base_capture")
    root, filename = raw.get("root"), raw.get("filename")
    if not isinstance(root, str) or not root.strip():
        raise CollectionPlannerError("invalid_base_capture_root")
    base_capture_end_date(filename)
    return root, filename


def load_daily_collection_config(path: Path) -> DailyCollectionConfig:
    """Read and structurally validate ``configs/collection/daily_miniapp_v1.json``."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        document = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionPlannerError(f"unreadable_config: {exc}") from None
    if not isinstance(document, dict) or document.get("schema") != CONFIG_SCHEMA:
        raise CollectionPlannerError("invalid_config_schema")
    try:
        window = document["window"]
        pacing = document["pacing"]
        base_capture_root, base_capture_filename = _parse_base_capture(document)
        return DailyCollectionConfig(
            schema=document["schema"],
            source_id=str(document["source_id"]),
            symbol_scope=str(document["symbol_scope"]),
            split_config=str(document["split_config"]),
            exclusions_config=str(document["exclusions_config"]),
            end_offset_days=int(window["end_offset_days"]),
            lookback_calendar_days=int(window["lookback_calendar_days"]),
            max_span_days=int(window["max_span_days"]),
            symbol_pause_seconds=float(pacing["symbol_pause_seconds"]),
            max_wall_seconds=int(pacing["max_wall_seconds"]),
            capture_root=str(document["capture_root"]),
            permission_status=str(document["permission_status"]),
            stop_on_http_status=tuple(int(code) for code in document["stop_on_http_status"]),
            raw=document,
            base_capture_root=base_capture_root,
            base_capture_filename=base_capture_filename,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CollectionPlannerError(f"invalid_config_field: {exc}") from None


def config_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


_SYMBOL_PATH_TOKEN = re.compile(r"^[A-Za-z0-9]{1,16}$")


def scan_base_capture_ends(
    base_root: Path, filename: str, symbols: Iterable[str],
) -> dict[str, date]:
    """Floor dates: ``{symbol: base capture end}`` for symbols whose file exists.

    Only the existence of ``<base_root>/<SYMBOL>/<filename>`` is checked (the
    file is never opened); the end date is ``base_capture_end_date(filename)``.
    A missing file (or an unsafe symbol token) yields no floor for that
    symbol, so it keeps today's behavior (``S = E - lookback``).
    """
    end = base_capture_end_date(filename)
    root = Path(base_root)
    floors: dict[str, date] = {}
    for symbol in symbols:
        if _SYMBOL_PATH_TOKEN.fullmatch(symbol) and (root / symbol / filename).is_file():
            floors[symbol] = end
    return floors


def scan_last_success(run_records_dir: Path) -> dict[str, date]:
    """Best-effort read of saved ``fintr_daily_collection_run_v1`` records.

    Returns each symbol's latest ``status == "saved"`` window end across
    every run record found directly under ``run_records_dir``. Missing
    directories, unreadable files, and records with the wrong schema or a
    malformed entry are skipped rather than failing the whole scan -- a
    dry-run plan should still be printable even if one old run record is
    corrupt; the run executor added in a later PR decides whether a
    corrupt record should instead block a live run.
    """
    directory = Path(run_records_dir)
    if not directory.is_dir():
        return {}
    latest: dict[str, date] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict) or document.get("schema") != RUN_RECORD_SCHEMA:
            continue
        entries = document.get("symbols")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("status") != "saved":
                continue
            symbol = entry.get("symbol")
            window = entry.get("window")
            if not isinstance(symbol, str) or not isinstance(window, dict):
                continue
            try:
                end = date.fromisoformat(window.get("end"))
            except (TypeError, ValueError):
                continue
            if symbol not in latest or end > latest[symbol]:
                latest[symbol] = end
    return latest
