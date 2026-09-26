"""Join saved price-history bars onto a period capture's server dates.

Aligns ``fintr.data.price_history.PriceHistoryCapture`` bars against a
``fintr.data.period_capture.PeriodCapture``'s server dates and the
``daily_market_proxy`` derived from it (``fintr.features.period``, itself an
UNVERIFIED proxy). Everything here is a deterministic, offline consistency
observation between two independently unverified data sources; nothing here
infers price direction or causality. See ``FORMULAS`` and the constraints
carried into the report by ``fintr.inference.period_report``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fintr.data.period_capture import PeriodCapture
from fintr.data.price_history import DailyBar, PriceHistoryCapture
from fintr.features.period import daily_market_proxy


FORMULA_VERSION = "price_join_v2"

# Ratio tolerance band for implied_traded_lots (an AKD-derived proxy that is
# itself unverified, and only meaningful on a day where its own
# net_within_total/total_size_even checks pass) against the price source's
# reported volume. "Lot" units in the custody/AKD flow are not guaranteed to
# be the same unit as the price source's reported share volume, so a wide
# +/-50% band is used by default to flag only gross mismatches (e.g. an
# order-of-magnitude difference), not to assert the two series should
# reconcile exactly. Exposed as a CLI parameter so it is never a hidden
# default; callers may narrow or widen it.
DEFAULT_VOLUME_RATIO_TOLERANCE = Decimal("0.5")

# Relative-change threshold on adjusted_close/close used to flag a
# corporate-action adjustment boundary. Prices in this source are quoted to
# a small, fixed number of decimal places, so a small band (0.05%) is used
# to absorb rounding noise while still catching real scale changes, which
# are typically at least a few percent (a split or a sizeable dividend).
# Also a CLI parameter, not a hidden default.
DEFAULT_ADJUSTMENT_RATIO_EPSILON = Decimal("0.0005")

# Yahoo back-adjusts BOTH close and adjusted_close for splits/bonus issues, so
# adjustment_ratio (adjusted_close/close) never moves and adjustment_boundary
# detection above stays silent on such an event. The symptom instead shows up
# as implied_traded_lots/volume settling onto a constant 1/k ratio (k a small
# integer) over a contiguous run of days, because "lots" on the AKD/custody
# side are not retroactively rescaled while the price source's volume is.
# Relative tolerance on |ratio - 1/k| / (1/k); a CLI parameter, not a hidden
# default. 1% comfortably separates a real 1/k regime (near-exact in this
# provider's Decimal data) from an unrelated ratio that happens to be close
# to some 1/k by chance.
DEFAULT_RATIO_REGIME_TOLERANCE = Decimal("0.01")

# Upper bound on the integer k tested for a constant-ratio regime. Stock
# splits/bonus issues are small integers in practice; this bound is generous
# and documented here rather than exposed as a CLI knob because it changes
# the search space, not a tolerance judgment.
RATIO_REGIME_MAX_K = 20

FORMULAS: dict[str, str] = {
    "price_only_classification": (
        "classify_price_bar applies to any bar, not only a price-only date: "
        "'placeholder' when volume is 0 or None and open == high == low == "
        "close (all four present and equal) -- the documented shape of an "
        "exchange-holiday marker bar; 'no_data_bar' when open, high, low "
        "and close are all None; otherwise 'unexpected_trading_bar'. For "
        "price-only days (no matching period-capture server date) this "
        "classification is also exposed as PriceOnlyDay.classification"
    ),
    "vwap_within_bar_range": (
        "vwap_within_bar_range[date] = bar.low <= vwap_proxy[date] <= "
        "bar.high (evaluated only when bar.low, bar.high and vwap_proxy are "
        "all present; else None, not a failure)"
    ),
    "volume_ratio": (
        "volume_ratio[date] = implied_traded_lots[date] / bar.volume[date] "
        "(Decimal; None when bar.volume is 0 or None, or "
        "implied_traded_lots[date] is None); "
        "volume_ratio_within_tolerance[date] = "
        "|volume_ratio[date] - 1| <= volume_ratio_tolerance (parameter, "
        "default documented on the CLI); this is an informational "
        "comparison between two independently unverified series, not a "
        "reconciliation of either one"
    ),
    "adjustment_boundary": (
        "adjustment_ratio[date] = bar.adjusted_close / bar.close (both "
        "present, close != 0); a bar's date is an adjustment_boundary when "
        "|adjustment_ratio[date] - adjustment_ratio[previous_priced_bar]| / "
        "|adjustment_ratio[previous_priced_bar]| > adjustment_ratio_epsilon "
        "(parameter, default documented on the CLI); computed over the "
        "full saved price series in ascending date order, not only joined "
        "dates; only the boundary dates are reported, not the ratio values"
    ),
    "ratio_regime": (
        "For a joined day with volume_ratio, vwap_proxy, bar.low and "
        "bar.high all present: a candidate k (integer, 2 <= k <= "
        "RATIO_REGIME_MAX_K) matches when "
        "|volume_ratio - 1/k| / (1/k) <= ratio_regime_tolerance (parameter, "
        "default documented on the CLI) AND bar.low <= vwap_proxy / k <= "
        "bar.high; the smallest matching k is kept. A ratio_regime is a "
        "maximal run of consecutive joined server dates sharing the same "
        "matched k; it is reported as {start, end, k, day_count, label: "
        "'probable_retroactive_price_adjustment'} -- a hypothesis about a "
        "retroactive price-source adjustment that Yahoo's back-adjustment "
        "of both close and adjusted_close would hide from adjustment_ratio, "
        "not a verified corporate action. Days inside a ratio_regime are "
        "excluded from the volume_ratio_within_tolerance check_summary "
        "(reported separately, not as a genuine check failure)."
    ),
}


@dataclass(frozen=True, slots=True)
class JoinedDay:
    """One period-capture server date joined against a price bar, if any."""

    date: date
    bar_present: bool
    close: Decimal | None
    volume: int | None
    adjusted_close: Decimal | None
    vwap_proxy: Decimal | None
    vwap_within_bar_range: bool | None
    volume_ratio: Decimal | None
    volume_ratio_within_tolerance: bool | None
    ratio_regime_k: int | None  # integer k when volume_ratio ~= 1/k (see FORMULAS["ratio_regime"])


@dataclass(frozen=True, slots=True)
class RatioRegime:
    """A contiguous run of joined server dates whose volume_ratio settles on
    a constant 1/k; see FORMULAS["ratio_regime"]. A hypothesis, not a
    verified corporate action."""

    start: date
    end: date
    k: int
    day_count: int
    label: str = "probable_retroactive_price_adjustment"


@dataclass(frozen=True, slots=True)
class PriceOnlyDay:
    """One price-history bar whose date is not a period-capture server date."""

    date: date
    classification: str  # placeholder | unexpected_trading_bar | no_data_bar
    close: Decimal | None
    volume: int | None


@dataclass(frozen=True, slots=True)
class PriceJoinResult:
    formula_version: str
    volume_ratio_tolerance: Decimal
    adjustment_ratio_epsilon: Decimal
    ratio_regime_tolerance: Decimal
    joined: tuple[JoinedDay, ...]
    price_only_days: tuple[PriceOnlyDay, ...]
    adjustment_boundary_dates: tuple[date, ...]
    ratio_regimes: tuple[RatioRegime, ...]

    @property
    def missing_dates(self) -> tuple[date, ...]:
        """Server dates for which the price capture has no bar."""
        return tuple(day.date for day in self.joined if not day.bar_present)

    @property
    def ratio_regime_dates(self) -> tuple[date, ...]:
        """Every joined date that falls inside some ratio_regime, ascending."""
        return tuple(
            day.date for day in self.joined if day.ratio_regime_k is not None
        )

    @property
    def placeholder_dates(self) -> tuple[date, ...]:
        return tuple(
            day.date for day in self.price_only_days if day.classification == "placeholder"
        )

    @property
    def unexpected_trading_dates(self) -> tuple[date, ...]:
        return tuple(
            day.date for day in self.price_only_days
            if day.classification == "unexpected_trading_bar"
        )

    @property
    def no_data_dates(self) -> tuple[date, ...]:
        return tuple(
            day.date for day in self.price_only_days if day.classification == "no_data_bar"
        )


def classify_price_bar(bar: DailyBar) -> str:
    ohlc = (bar.open, bar.high, bar.low, bar.close)
    volume_zero_or_none = bar.volume is None or bar.volume == 0
    if all(value is None for value in ohlc) and volume_zero_or_none:
        return "no_data_bar"
    if (
        volume_zero_or_none
        and all(value is not None for value in ohlc)
        and bar.open == bar.high == bar.low == bar.close
    ):
        return "placeholder"
    return "unexpected_trading_bar"


def _volume_ratio(implied_traded_lots: Decimal | None, volume: int | None) -> Decimal | None:
    if implied_traded_lots is None or volume is None or volume == 0:
        return None
    return implied_traded_lots / Decimal(volume)


def _adjustment_ratio(bar: DailyBar) -> Decimal | None:
    if bar.adjusted_close is None or bar.close is None or bar.close == 0:
        return None
    return bar.adjusted_close / bar.close


def _adjustment_boundary_dates(bars: tuple[DailyBar, ...], epsilon: Decimal) -> tuple[date, ...]:
    """Dates whose adjustment_ratio differs from the previous priced bar's by
    more than ``epsilon`` (relative). Bars are assumed ascending by date,
    which ``PriceHistoryCapture`` guarantees."""
    boundaries: list[date] = []
    previous_ratio: Decimal | None = None
    for bar in bars:
        ratio = _adjustment_ratio(bar)
        if ratio is None:
            continue
        if previous_ratio is not None:
            changed = (
                ratio != previous_ratio if previous_ratio == 0
                else abs(ratio - previous_ratio) / abs(previous_ratio) > epsilon
            )
            if changed:
                boundaries.append(bar.trade_date)
        previous_ratio = ratio
    return tuple(boundaries)


def _ratio_regime_k(
    volume_ratio: Decimal | None, vwap_proxy: Decimal | None,
    low: Decimal | None, high: Decimal | None, tolerance: Decimal,
) -> int | None:
    """Smallest integer k (2..RATIO_REGIME_MAX_K) such that volume_ratio is
    within ``tolerance`` (relative) of 1/k AND vwap_proxy/k falls in
    [low, high]; None when no k matches or an input is missing."""
    if volume_ratio is None or vwap_proxy is None or low is None or high is None:
        return None
    for k in range(2, RATIO_REGIME_MAX_K + 1):
        expected = Decimal(1) / Decimal(k)
        if abs(volume_ratio - expected) / expected > tolerance:
            continue
        if low <= vwap_proxy / Decimal(k) <= high:
            return k
    return None


def _ratio_regimes(joined: tuple[JoinedDay, ...]) -> tuple[RatioRegime, ...]:
    """Maximal runs of consecutive joined days sharing the same ratio_regime_k."""
    regimes: list[RatioRegime] = []
    run_start: date | None = None
    run_end: date | None = None
    run_k: int | None = None
    run_days = 0
    for day in joined:
        if day.ratio_regime_k is not None and day.ratio_regime_k == run_k:
            run_end = day.date
            run_days += 1
        else:
            if run_k is not None:
                regimes.append(RatioRegime(run_start, run_end, run_k, run_days))
            if day.ratio_regime_k is not None:
                run_start = run_end = day.date
                run_k = day.ratio_regime_k
                run_days = 1
            else:
                run_start = run_end = None
                run_k = None
                run_days = 0
    if run_k is not None:
        regimes.append(RatioRegime(run_start, run_end, run_k, run_days))
    return tuple(regimes)


def join_price_history(
    capture: PeriodCapture, price: PriceHistoryCapture, *,
    volume_ratio_tolerance: Decimal = DEFAULT_VOLUME_RATIO_TOLERANCE,
    adjustment_ratio_epsilon: Decimal = DEFAULT_ADJUSTMENT_RATIO_EPSILON,
    ratio_regime_tolerance: Decimal = DEFAULT_RATIO_REGIME_TOLERANCE,
) -> PriceJoinResult:
    """Align ``price.bars`` to ``capture.server_dates``; see ``FORMULAS``."""
    if not isinstance(volume_ratio_tolerance, Decimal) or volume_ratio_tolerance < 0:
        raise ValueError("volume_ratio_tolerance must be a non-negative Decimal")
    if not isinstance(adjustment_ratio_epsilon, Decimal) or adjustment_ratio_epsilon < 0:
        raise ValueError("adjustment_ratio_epsilon must be a non-negative Decimal")
    if not isinstance(ratio_regime_tolerance, Decimal) or ratio_regime_tolerance < 0:
        raise ValueError("ratio_regime_tolerance must be a non-negative Decimal")

    bars_by_date = {bar.trade_date: bar for bar in price.bars}
    server_dates = set(capture.server_dates)
    proxy_by_date = {day.date: day for day in daily_market_proxy(capture).days}

    joined: list[JoinedDay] = []
    for server_date in capture.server_dates:
        bar = bars_by_date.get(server_date)
        proxy_day = proxy_by_date.get(server_date)
        vwap_proxy = proxy_day.vwap_proxy if proxy_day is not None else None
        implied_traded_lots = proxy_day.implied_traded_lots if proxy_day is not None else None
        if bar is None:
            joined.append(JoinedDay(
                date=server_date, bar_present=False, close=None, volume=None,
                adjusted_close=None, vwap_proxy=vwap_proxy, vwap_within_bar_range=None,
                volume_ratio=None, volume_ratio_within_tolerance=None, ratio_regime_k=None,
            ))
            continue
        vwap_within_bar_range = (
            None if vwap_proxy is None or bar.low is None or bar.high is None
            else bar.low <= vwap_proxy <= bar.high
        )
        volume_ratio = _volume_ratio(implied_traded_lots, bar.volume)
        volume_ratio_within_tolerance = (
            None if volume_ratio is None
            else abs(volume_ratio - 1) <= volume_ratio_tolerance
        )
        ratio_regime_k = _ratio_regime_k(
            volume_ratio, vwap_proxy, bar.low, bar.high, ratio_regime_tolerance,
        )
        joined.append(JoinedDay(
            date=server_date, bar_present=True, close=bar.close, volume=bar.volume,
            adjusted_close=bar.adjusted_close, vwap_proxy=vwap_proxy,
            vwap_within_bar_range=vwap_within_bar_range, volume_ratio=volume_ratio,
            volume_ratio_within_tolerance=volume_ratio_within_tolerance,
            ratio_regime_k=ratio_regime_k,
        ))

    price_only_days = tuple(
        PriceOnlyDay(
            date=bar.trade_date, classification=classify_price_bar(bar),
            close=bar.close, volume=bar.volume,
        )
        for bar in price.bars
        if bar.trade_date not in server_dates
    )

    joined_tuple = tuple(joined)
    return PriceJoinResult(
        formula_version=FORMULA_VERSION,
        volume_ratio_tolerance=volume_ratio_tolerance,
        adjustment_ratio_epsilon=adjustment_ratio_epsilon,
        ratio_regime_tolerance=ratio_regime_tolerance,
        joined=joined_tuple,
        price_only_days=price_only_days,
        adjustment_boundary_dates=_adjustment_boundary_dates(price.bars, adjustment_ratio_epsilon),
        ratio_regimes=_ratio_regimes(joined_tuple),
    )
