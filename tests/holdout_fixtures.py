"""Synthetic fixture generator for the holdout-day rehearsal tests.

Nothing here is real market data: period records and price histories for
the 30 declared BIST 30 symbols are generated from a fixed seed over the
pre-registered test window (2026-10-01..2027-03-31) in the *existing* file
schemas (``fintr_period_analysis_capture_v1`` and
``fintr_price_history_capture_v1``), so the real loaders, hash checks and
the real engine run unmodified. Files are written under a caller-supplied
(temporary) directory; nothing is committed to the repository.

Deterministic: every value derives from ``random.Random(f"{seed}:{symbol}")``
and the calendar, so two runs with the same arguments write byte-identical
files (``tests/test_holdout_rehearsal.py`` checks this).

The generator never reads a frozen spec's outcome statistics; the synthetic
returns say nothing about any hypothesis. It only needs to be *shaped* like
the real data: weekday bars, exchange-holiday placeholder bars (volume 0,
open == high == low == close), AKD rows whose total size/price derive a
vwap proxy near the close, custody balances, and -- for one symbol -- a run
of days in a probable-retroactive-price-adjustment scale regime (AKD lots
and prices at k x the price series' scale).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import random

import test_period_analysis as period_fixtures


WINDOW_START = date(2026, 10, 1)
WINDOW_END = date(2027, 3, 31)
# Full saved price history is kept (docs/prereg/DECISIONS.md 2026-09-28): it
# starts before the window and runs past the window end.
PRICE_START = date(2026, 8, 3)
PRICE_END = date(2027, 5, 14)

# Exchange-holiday placeholder bars (weekdays; synthetic dates, not an
# exchange calendar): they are price-only days, never server dates.
HOLIDAY_PLACEHOLDERS = (date(2026, 10, 29), date(2027, 1, 1), date(2027, 3, 10), date(2027, 3, 11))

# One symbol gets a run of days in a scale regime (k = 2): AKD lots are half
# the price series' volume and AKD prices are 2x the price series' prices.
SCALE_REGIME_SYMBOL = "GARAN"
SCALE_REGIME_K = 2
SCALE_REGIME_DAY_COUNT = 3
SCALE_REGIME_FIRST_WINDOW_DAY_INDEX = 20

BROKER_CODES = ("AAA", "BBB", "CCC", "DDD", "EEE")
DEFAULT_SEED = 20261001
SYNTHETIC_CAPTURED_AT = "2027-04-01T06:00:00Z"


def declared_symbols() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(development, holdout)`` from the active split file (symbol names
    only; the generator reads no data)."""
    from fintr.evaluation.symbol_guard import load_active_guards

    guards = load_active_guards()
    return (
        tuple(sorted(s for s in guards.split.development_symbols if not guards.is_excluded(s))),
        tuple(sorted(s for s in guards.split.holdout_symbols if not guards.is_excluded(s))),
    )


def trading_days(start: date, end: date) -> list[date]:
    """Weekdays from ``start`` to ``end`` that are not holiday placeholders."""
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in HOLIDAY_PLACEHOLDERS:
            days.append(current)
        current += timedelta(days=1)
    return days


def weekdays(start: date, end: date) -> list[date]:
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


@dataclass(frozen=True, slots=True)
class SymbolFixture:
    symbol: str
    period_document: dict
    price_document: dict
    period_name: str
    price_name: str
    scale_regime_dates: tuple[date, ...] = ()
    server_dates: tuple[date, ...] = ()
    conditioned_h1_dates: tuple[date, ...] = field(default=())


def _timestamp(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 7, tzinfo=timezone.utc).timestamp())


def build_symbol_fixture(
    symbol: str, *, seed: int = DEFAULT_SEED, window_start: date = WINDOW_START,
    window_end: date = WINDOW_END, price_start: date = PRICE_START, price_end: date = PRICE_END,
) -> SymbolFixture:
    rng = random.Random(f"{seed}:{symbol}")
    all_weekdays = weekdays(price_start, price_end)
    server_dates = trading_days(window_start, window_end)
    server_set = set(server_dates)
    regime_dates: tuple[date, ...] = ()
    if symbol == SCALE_REGIME_SYMBOL:
        start_index = SCALE_REGIME_FIRST_WINDOW_DAY_INDEX
        regime_dates = tuple(server_dates[start_index:start_index + SCALE_REGIME_DAY_COUNT])
    regime_set = set(regime_dates)

    base_price = Decimal(rng.randint(20, 400))
    closes: dict[date, Decimal] = {}
    price = base_price
    for day in all_weekdays:
        step = Decimal(rng.randint(-150, 150)) / Decimal(10000)
        price = max(Decimal("1"), (price * (1 + step)).quantize(Decimal("0.01")))
        closes[day] = price

    bars: list[dict] = []
    akd_days: list[dict] = []
    custody_days: list[dict] = []
    metadata_days: list[dict] = []
    custody_balance = {code: Decimal(rng.randint(10_000, 90_000)) for code in BROKER_CODES}
    first_custody: dict[str, Decimal] | None = None
    last_custody: dict[str, Decimal] = {}
    period_net = {code: 0 for code in BROKER_CODES}
    conditioned: list[date] = []
    previous_close = base_price

    for day in all_weekdays:
        close = closes[day]
        if day in HOLIDAY_PLACEHOLDERS:
            bars.append(period_fixtures._price_bar(
                day.isoformat(), o=str(previous_close), h=str(previous_close),
                l=str(previous_close), c=str(previous_close), adj=str(previous_close),
                v=0, ts=_timestamp(day),
            ))
            continue
        open_ = (previous_close * (1 + Decimal(rng.randint(-60, 60)) / Decimal(10000))).quantize(
            Decimal("0.01"))
        high = max(open_, close) * Decimal("1.006")
        low = min(open_, close) * Decimal("0.994")
        high, low = high.quantize(Decimal("0.01")), low.quantize(Decimal("0.01"))
        sizes = [rng.randint(2_000, 40_000) for _ in BROKER_CODES]
        if sum(sizes) % 2:
            sizes[0] += 1
        volume = sum(sizes) // 2
        deviation = Decimal(rng.randint(-400, 400)) / Decimal(10000)  # vwap vs close, +-4%
        vwap = (close * (1 + deviation)).quantize(Decimal("0.01"))
        if day in regime_set:
            # Price series is back-adjusted (k x smaller price, k x larger
            # volume) while AKD keeps the unadjusted scale.
            k = Decimal(SCALE_REGIME_K)
            close, open_ = (close / k).quantize(Decimal("0.01")), (open_ / k).quantize(Decimal("0.01"))
            high, low = (high / k).quantize(Decimal("0.01")), (low / k).quantize(Decimal("0.01"))
            volume *= SCALE_REGIME_K
            high = max(high, (vwap / k).quantize(Decimal("0.01")) + Decimal("0.01"))
            low = min(low, (vwap / k).quantize(Decimal("0.01")) - Decimal("0.01"))
        bars.append(period_fixtures._price_bar(
            day.isoformat(), o=str(open_), h=str(high), l=str(low), c=str(close), adj=str(close),
            v=volume, ts=_timestamp(day),
        ))
        previous_close = close
        if day not in server_set:
            continue

        nets = [rng.randint(-sizes[i] // 2, sizes[i] // 2) for i in range(len(sizes) - 1)]
        nets.append(-sum(nets))  # net sums to zero across the panel
        nets[-1] = max(-sizes[-1], min(sizes[-1], nets[-1]))
        akd_rows = []
        for code, size, net in zip(BROKER_CODES, sizes, nets):
            cost = (vwap * (1 + Decimal(rng.randint(-20, 20)) / Decimal(10000))).quantize(Decimal("0.01"))
            akd_rows.append({
                "broker_code": code, "net_size": Decimal(net), "total_size": Decimal(size),
                "total_cost": cost,
            })
            period_net[code] += net
        akd_days.append({"date": day.isoformat(), "rows": akd_rows})

        for code in BROKER_CODES:
            custody_balance[code] += Decimal(rng.randint(-500, 500))
        custody_days.append({"date": day.isoformat(), "rows": [
            {"broker_code": code, "lot_count": custody_balance[code]} for code in BROKER_CODES
        ]})
        if first_custody is None:
            first_custody = dict(custody_balance)
        last_custody = dict(custody_balance)
        metadata_days.append({
            "date": day.isoformat(), "null_fields": [],
            "retail_pct": Decimal("40.5"), "institutional_pct": Decimal("59.5"),
        })
        proxy_total = sum(row["total_size"] for row in akd_rows)
        proxy_vwap = sum(row["total_size"] * row["total_cost"] for row in akd_rows) / proxy_total
        proxy_dev = (proxy_vwap - close) / close
        if Decimal("-0.10") <= proxy_dev <= Decimal("-0.01") and day not in regime_set:
            conditioned.append(day)

    descending = sorted(server_dates, reverse=True)
    packet = {
        "schema": "fintr_period_analysis_handoff_v1",
        "requested_period": {
            "symbol": symbol, "start_date": window_start.isoformat(),
            "end_date": window_end.isoformat(), "origin": "local_cli_arguments",
        },
        "get_akd_period": {"rows_in_provider_order": [
            {"brokerage": code, "net": {
                "size": Decimal(period_net[code]), "percentage": Decimal("0.1"),
                "cost": Decimal("1.5"),
            }}
            for code in BROKER_CODES
        ]},
        "custody_analysis_daily": {
            "provenance": {"server_echo": {"dates_in_provider_order": [d.isoformat() for d in descending]}},
            "daily_akd_flow_in_provider_order": list(reversed(akd_days)),
            "daily_custody_balance_in_provider_order": list(reversed(custody_days)),
            "metadata_in_provider_order": list(reversed(metadata_days)),
        },
        "get_custodian_diff_period": {
            "provenance": {"server_echo": {
                "start_date": window_start.isoformat(), "end_date": window_end.isoformat(),
            }},
            "rows_in_provider_order": [
                {"custodian": code, "first_value": (first_custody or {}).get(code, Decimal(0)),
                 "last_value": last_custody.get(code, Decimal(0)),
                 "difference": last_custody.get(code, Decimal(0)) - (first_custody or {}).get(code, Decimal(0))}
                for code in BROKER_CODES
            ],
        },
    }
    period_document = period_fixtures._capture(packet)
    period_document["capture_metadata"]["captured_at_utc"] = SYNTHETIC_CAPTURED_AT
    price_document = period_fixtures._price_capture_document(
        f"{symbol}.IS", price_start.isoformat(), price_end.isoformat(), bars,
    )
    return SymbolFixture(
        symbol=symbol, period_document=period_document, price_document=price_document,
        period_name=f"{window_start.isoformat()}_{window_end.isoformat()}.json",
        price_name=f"{price_start.isoformat()}_{price_end.isoformat()}.json",
        scale_regime_dates=regime_dates, server_dates=tuple(server_dates),
        conditioned_h1_dates=tuple(conditioned),
    )


def write_symbol_fixture(period_root: Path, price_root: Path, fixture: SymbolFixture) -> tuple[Path, Path]:
    """Writes one symbol's period record and price history; returns both paths."""
    period_dir = period_root / fixture.symbol
    price_dir = price_root / fixture.symbol
    period_dir.mkdir(parents=True, exist_ok=True)
    price_dir.mkdir(parents=True, exist_ok=True)
    period_path = period_dir / fixture.period_name
    price_path = price_dir / fixture.price_name
    period_path.write_text(period_fixtures.smoke._compact_json(fixture.period_document), encoding="utf-8")
    price_path.write_text(json.dumps(fixture.price_document), encoding="utf-8")
    return period_path, price_path


def write_world(
    root: Path, *, seed: int = DEFAULT_SEED, symbols: tuple[str, ...] | None = None,
    window_end: date = WINDOW_END, price_end: date = PRICE_END,
) -> tuple[Path, Path, dict[str, SymbolFixture]]:
    """Writes every declared symbol (or ``symbols``) under ``root``; returns
    ``(period_root, price_root, fixtures)``. ``window_end`` / ``price_end``
    shorten the period record / price history (an early, incomplete world)."""
    development, holdout = declared_symbols()
    names = tuple(symbols) if symbols is not None else development + holdout
    period_root, price_root = root / "period_analysis", root / "price_history"
    fixtures: dict[str, SymbolFixture] = {}
    for symbol in names:
        fixture = build_symbol_fixture(symbol, seed=seed, window_end=window_end, price_end=price_end)
        write_symbol_fixture(period_root, price_root, fixture)
        fixtures[symbol] = fixture
    return period_root, price_root, fixtures
