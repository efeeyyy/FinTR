"""Composed-capture loading for the note packet.

Same loading path as ``scripts/scan_observations.py --daily-root``: the
base capture (most recent ending ``<S>_<E>.json`` under the period dir) and
every rolling capture under ``<daily-root>/<SYM>/`` are read with the
hash/integrity-checking ``load_period_capture`` and composed with
``compose_period_capture``; the covering price-history file is read only for
its trading-day calendar (gap detection) -- no price is ever put in the
packet. The three discovery helpers mirror the scanner's (a test compares
them on the same directory tree so they cannot drift).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import re

from fintr.data.period_capture import PeriodCapture, PeriodCaptureError, load_period_capture
from fintr.data.price_history import PriceHistoryCaptureError, load_price_history_capture
from fintr.research.compose_period_capture import CaptureInput, CompositionError, compose_period_capture
from fintr.research.engine import tradeable_price_bars


_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json$")


class LoadError(ValueError):
    """A capture could not be loaded or composed (stable, payload-free text)."""


def _captures(symbol_dir: Path) -> list[tuple[date, str, Path]]:
    if not symbol_dir.is_dir():
        return []
    found: list[tuple[date, str, Path]] = []
    for entry in sorted(symbol_dir.iterdir()):
        match = _NAME_RE.match(entry.name) if entry.is_file() else None
        if match:
            found.append((date.fromisoformat(match.group(2)), entry.name, entry))
    found.sort()
    return found


def find_period_capture(root: Path, symbol: str) -> Path | None:
    found = _captures(root / symbol)
    return found[-1][2] if found else None


def find_daily_captures(root: Path, symbol: str) -> list[Path]:
    return [item[2] for item in _captures(root / symbol)]


def discover_price_history(root: Path, symbol: str, start: date, end: date) -> Path | None:
    symbol_dir = root / symbol
    if not symbol_dir.is_dir():
        return None
    candidates: list[tuple[int, str, Path]] = []
    for entry in sorted(symbol_dir.iterdir()):
        match = _NAME_RE.match(entry.name) if entry.is_file() else None
        if not match:
            continue
        file_start, file_end = date.fromisoformat(match.group(1)), date.fromisoformat(match.group(2))
        if file_start > file_end or file_start > start or file_end < end:
            continue
        candidates.append(((file_end - file_start).days, entry.name, entry))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2] if candidates else None


def find_daily_captures_until(root: Path, symbol: str, as_of: date) -> list[Path]:
    """Rolling captures that can hold data up to ``as_of``: those whose file
    name start date is on or before it (a later capture is not even opened)."""
    return [
        path for _, name, path in _captures(root / symbol)
        if date.fromisoformat(_NAME_RE.match(name).group(1)) <= as_of
    ]


def load_composed_capture(
    symbol: str, *, base_root: Path, daily_root: Path, price_dir: Path, as_of: date | None = None,
) -> PeriodCapture:
    """Base + rolling captures composed (never truncated here; the caller
    truncates at its own ``as_of``).

    With ``as_of`` (the note step always passes it): rolling captures that
    start after ``as_of`` are not loaded, and the price-history file only has
    to cover the days up to ``as_of`` (it is read for the trading-day
    calendar only). So newer daily captures -- a live run that has already
    written the next day's capture before its price file, or a re-run of an
    older ``as_of`` -- never make an older ``as_of`` fail. A capture that
    straddles ``as_of`` is still composed whole; its rows after ``as_of`` are
    cut by the caller's truncation."""
    base_path = find_period_capture(base_root, symbol)
    if base_path is None:
        raise LoadError(f"no_period_capture:{symbol}")
    daily_paths = (find_daily_captures(daily_root, symbol) if as_of is None
                   else find_daily_captures_until(daily_root, symbol, as_of))
    try:
        inputs = [CaptureInput(load_period_capture(base_path), kind="base", label=str(base_path))]
        for path in daily_paths:
            inputs.append(CaptureInput(load_period_capture(path), kind="rolling", label=str(path)))
    except (OSError, PeriodCaptureError) as exc:
        raise LoadError(f"capture_unreadable:{symbol}:{exc}") from None
    start = min(item.capture.start for item in inputs)
    end = max(item.capture.end for item in inputs)
    if as_of is not None:
        end = min(end, as_of)
    price_path = discover_price_history(price_dir, symbol, start, end)
    if price_path is None:
        raise LoadError(f"no_price_history:{symbol}")
    try:
        price = load_price_history_capture(price_path)
    except (OSError, PriceHistoryCaptureError) as exc:
        raise LoadError(f"price_history_unreadable:{symbol}:{exc}") from None
    calendar = tuple(bar.trade_date for bar in tradeable_price_bars(price))
    try:
        return compose_period_capture(inputs, tradeable_dates=calendar)
    except CompositionError as exc:
        raise LoadError(f"composition_failed:{symbol}:{exc}") from None
