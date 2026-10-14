"""Per-symbol data freshness: last AKD/custody/price dates as of a given
``as_of`` server date.

Missing data is reported as ``None`` ("yok"), never coerced to a date or to
a zero-length gap. Custody normally settles behind AKD by server date
(T+2 today); ``custody_flagged_stale`` marks a gap of at least the
settlement lag effective on ``as_of`` (``configs/market/settlement_cycle.json``)
in listed server dates so a stale custody panel is never silently treated as current.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from fintr.data.period_capture import PeriodCapture
from fintr.data.price_history import PriceHistoryCapture
from fintr.data.settlement_cycle import SettlementCycle
from fintr.research.engine import tradeable_price_bars


@dataclass(frozen=True, slots=True)
class FreshnessReport:
    last_akd_server_date: date | None
    last_custody_date: date | None
    last_price_date: date | None
    custody_behind_akd_server_dates: int | None
    custody_flagged_stale: bool
    custody_settlement_lag: int


def build_freshness_report(
    capture: PeriodCapture, price: PriceHistoryCapture, *, as_of: date,
    settlement_cycle: SettlementCycle,
) -> FreshnessReport:
    server_dates = tuple(d for d in capture.server_dates if d <= as_of)
    last_akd = server_dates[-1] if server_dates else None
    custody_dates = tuple(sorted(day.date for day in capture.daily_custody_lots if day.date <= as_of))
    last_custody = custody_dates[-1] if custody_dates else None
    price_dates = tuple(bar.trade_date for bar in tradeable_price_bars(price) if bar.trade_date <= as_of)
    last_price = price_dates[-1] if price_dates else None

    gap: int | None = None
    if last_akd is not None and last_custody is not None:
        if last_custody in server_dates:
            gap = server_dates.index(last_akd) - server_dates.index(last_custody)
        else:
            gap = sum(1 for d in server_dates if last_custody < d <= last_akd)
    lag = settlement_cycle.lag_on(as_of)
    flagged = gap is not None and gap >= lag
    return FreshnessReport(
        last_akd_server_date=last_akd,
        last_custody_date=last_custody,
        last_price_date=last_price,
        custody_behind_akd_server_dates=gap,
        custody_flagged_stale=flagged,
        custody_settlement_lag=lag,
    )
