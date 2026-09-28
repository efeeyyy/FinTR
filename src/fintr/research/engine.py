"""Offline, lookahead-safe event-study engine.

Builds forward-return events from a pre-registered ``StudySpec`` condition
over saved period captures and price histories, compares them to an
unconditional baseline (all days, same symbols), and reports statistics with
a bootstrap confidence interval on the difference. This module never emits a
buy/sell recommendation; it only reports historical-outcome statistics for
research purposes. See ``docs/study_engine.md`` and the ADR in
``docs/architecture.md``.

Information timing: a signal computed at server date ``t`` may only use data
available after close of ``t`` (daily AKD for ``t``, custody balance for
``t`` -- itself only settled up to ``t-2``, provider metadata for ``t``).
Entry is at the next tradeable day's open (or close, per
``spec.entry_timing``); exit is ``horizon`` tradeable days after entry.
Exchange-holiday placeholder bars (``fintr.features.price_join.
classify_price_bar`` == ``"placeholder"``) are never counted as a tradeable
day and never used for a price.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import math
import random
import statistics

from fintr.data.period_capture import PeriodCapture
from fintr.data.price_history import DailyBar, PriceHistoryCapture
from fintr.features.price_join import (
    DEFAULT_RATIO_REGIME_TOLERANCE,
    RatioRegime,
    classify_price_bar,
    join_price_history,
)
from fintr.research.conditions import evaluate_condition
from fintr.research.features import truncate_period_capture
from fintr.research.study_spec import BlockBootstrapConfig, StudySpec


FORMULA_VERSION = "study_engine_v3"  # v3: ratio-regime signal-date drop (docs/prereg/DECISIONS.md)

FORMULAS: dict[str, str] = {
    "entry_price": (
        "next_open: entry_bar.open (raw), the first tradeable bar strictly "
        "after the signal date t; next_close: entry_bar.close (raw), same "
        "bar. A day with no tradeable bar after t within the price history "
        "yields no event (insufficient_bars)."
    ),
    "exit_price": "exit_bar.close (raw), the tradeable bar `horizon` positions after entry_bar.",
    "adjustment_boundary_handling": (
        "if any date d with entry_date < d <= exit_date is an adjustment "
        "boundary (fintr.features.price_join join_price_history."
        "adjustment_boundary_dates, computed over the full saved price "
        "series), raw open/close are not comparable across the boundary; "
        "the event instead uses adjusted_close at both entry and exit "
        "(policy 'use_adjusted') or is dropped (policy 'drop'), per "
        "spec.adjustment_boundary_policy. Both outcomes are counted."
    ),
    "gross_return": "exit_price / entry_price - 1",
    "net_return": (
        "gross_return - 2 * (transaction_cost_bps / 10000); transaction_cost_bps "
        "is per side, charged once at entry and once at exit (round-trip = 2x)"
    ),
    "baseline": (
        "the same entry/exit/cost computation run for every server date in "
        "scope, regardless of the condition; the conditioned population is "
        "a subset of, not disjoint from, the baseline population"
    ),
    "difference": "mean(conditioned net_return) - mean(baseline net_return)",
    "bootstrap_ci": (
        "seeded two-sample percentile bootstrap on the difference of means "
        "(random.Random(seed); resamples independently drawn with "
        "replacement from the conditioned and baseline net_return lists); "
        "an informational statistic about the observed sample, not a "
        "source financial value -- computed in float, not Decimal, because "
        "it is a resampling estimate. Secondary diagnostic only: it treats "
        "events as independent, which same-day cross-symbol events and "
        "overlapping holding windows are not"
    ),
    "block_bootstrap": (
        "E1 moving block bootstrap by signal date (Kunsch 1989): the sorted "
        "distinct signal dates of the kept (non-dropped) conditioned and "
        "baseline events, N of them, are the resampling unit; ceil(N/L) "
        "start positions are drawn with replacement from 0..N-L "
        "(random.Random(block_bootstrap.seed)), each contributing the L "
        "consecutive dates from that start, and the concatenation is cut to "
        "N dates. Every event (all symbols) on a drawn date enters the "
        "replicate, once per draw of that date. Each replicate's "
        "conditioned mean and baseline mean are event-weighted means over "
        "the same drawn dates; delta*_b = conditioned_mean - baseline_mean. "
        "A replicate whose drawn dates contain no conditioned (or no "
        "baseline) event has no delta*_b (undefined) and is counted. "
        "Computed in float (resampling estimate, not a source value)."
    ),
    "one_sided_p_value": (
        "predicted_direction negative: p = (1 + #{delta*_b >= 0}) / (B + 1); "
        "positive: p = (1 + #{delta*_b <= 0}) / (B + 1); B = "
        "block_bootstrap.resamples; an undefined replicate is counted as "
        "not supporting the predicted direction (conservative)"
    ),
    "bootstrap_percentile": (
        "nearest-rank percentile over the defined delta*_b values sorted "
        "ascending: q-th percentile = sorted[ceil(q * n) - 1], n = number of "
        "defined replicates"
    ),
    "custody_scale_jump": (
        "a server date d (in daily_custody_lots) where the total lot count "
        "over codes listed on both d and the previous listed day changes "
        "by a factor >= custody_scale_jump_ratio or <= 1/"
        "custody_scale_jump_ratio; a conditioned event whose condition read "
        "custody data over a range spanning such a d (min_date < d <= "
        "max_date) is dropped and counted, never silently kept"
    ),
    "ratio_regime_signal_date_drop": (
        "docs/prereg/DECISIONS.md 'Dondurma öncesi veri kalitesi kuralı': a "
        "symbol's signal dates falling inside one of its "
        "fintr.features.price_join.join_price_history(...).ratio_regimes "
        "(probable_retroactive_price_adjustment, integer k >= 2, "
        "ratio_regime_tolerance) are removed from both the conditioned and "
        "the baseline signal-date set before any event is built for that "
        "symbol/split -- a lot/volume unit-scale mismatch, not a return "
        "computation, so returns themselves (Yahoo's own internally "
        "consistent series) are never adjusted; only which signal days are "
        "evaluated. The dropped count and the regimes found are reported "
        "per symbol (see report.run_study's ratio_regime_quality block)."
    ),
}


class EngineError(ValueError):
    """A structural precondition of the study engine was violated."""


@dataclass(frozen=True, slots=True)
class SymbolData:
    symbol: str
    capture: PeriodCapture
    price: PriceHistoryCapture
    tradeable_bars: tuple[DailyBar, ...]  # ascending, placeholder bars excluded
    tradeable_dates: tuple[date, ...]
    close_by_date: dict[date, Decimal]
    adjustment_boundary_dates: frozenset[date]
    custody_scale_jump_dates: tuple[date, ...]
    ratio_regimes: tuple[RatioRegime, ...]
    ratio_regime_dates: frozenset[date]


def detect_custody_scale_jumps(capture: PeriodCapture, ratio_threshold: Decimal) -> tuple[date, ...]:
    """Dates where the whole-panel custody lot total jumps by a scale factor.

    Flags a lot-size/unit redefinition across the whole panel, not one
    broker's ordinary flow: only codes listed on both the day and the
    previous listed day are summed, and the ratio must clear
    ``ratio_threshold`` (or its reciprocal) in either direction.
    """
    if not isinstance(ratio_threshold, Decimal) or ratio_threshold <= 1:
        raise EngineError("ratio_threshold must be a Decimal greater than 1")
    flagged: list[date] = []
    previous: dict[str, Decimal] | None = None
    for day in capture.daily_custody_lots:
        by_code = day.by_code()
        if previous is not None:
            common = previous.keys() & by_code.keys()
            prev_total = sum((previous[code] for code in common), Decimal(0))
            curr_total = sum((by_code[code] for code in common), Decimal(0))
            if prev_total > 0 and curr_total > 0:
                ratio = curr_total / prev_total
                if ratio >= ratio_threshold or ratio <= (Decimal(1) / ratio_threshold):
                    flagged.append(day.date)
        previous = by_code
    return tuple(flagged)


def tradeable_price_bars(price: PriceHistoryCapture) -> tuple[DailyBar, ...]:
    """Ascending bars usable as a trading day: not a holiday placeholder and
    carrying both open and close."""
    return tuple(
        bar for bar in price.bars
        if classify_price_bar(bar) != "placeholder" and bar.open is not None and bar.close is not None
    )


def load_symbol_data(
    symbol: str, capture: PeriodCapture, price: PriceHistoryCapture, *,
    adjustment_ratio_epsilon: Decimal, custody_scale_jump_ratio: Decimal,
    ratio_regime_tolerance: Decimal = DEFAULT_RATIO_REGIME_TOLERANCE,
) -> SymbolData:
    if capture.symbol != symbol:
        raise EngineError("capture_symbol_mismatch")
    # price.symbol is intentionally not compared to `symbol`: the price
    # source echoes its own ticker convention (e.g. an exchange suffix such
    # as "AKFIS.IS" for a period capture symbol "AKFIS"), documented in
    # docs/price_history.md. fintr.features.price_join.join_price_history
    # already treats this as a legitimate, unenforced difference; the CLI
    # caller is responsible for pairing the right files under one --data
    # SYMBOL= entry.
    tradeable_bars = tradeable_price_bars(price)
    close_by_date = {bar.trade_date: bar.close for bar in price.bars if bar.close is not None}
    joined = join_price_history(
        capture, price, adjustment_ratio_epsilon=adjustment_ratio_epsilon,
        ratio_regime_tolerance=ratio_regime_tolerance,
    )
    return SymbolData(
        symbol=symbol,
        capture=capture,
        price=price,
        tradeable_bars=tradeable_bars,
        tradeable_dates=tuple(bar.trade_date for bar in tradeable_bars),
        close_by_date=close_by_date,
        adjustment_boundary_dates=frozenset(joined.adjustment_boundary_dates),
        custody_scale_jump_dates=detect_custody_scale_jumps(capture, custody_scale_jump_ratio),
        ratio_regimes=joined.ratio_regimes,
        ratio_regime_dates=frozenset(joined.ratio_regime_dates),
    )


def drop_ratio_regime_signal_dates(
    signal_dates: tuple[date, ...], ratio_regime_dates: frozenset[date],
) -> tuple[tuple[date, ...], int]:
    """Signal dates minus those inside a price_join ratio regime, plus how
    many were dropped (see ``FORMULAS["ratio_regime_signal_date_drop"]``).

    Applied to one symbol's signal dates before either the conditioned or
    the baseline event set is built for it, so a regime day is excluded
    from both -- ``build_events`` never sees it.
    """
    kept = tuple(d for d in signal_dates if d not in ratio_regime_dates)
    return kept, len(signal_dates) - len(kept)


@dataclass(frozen=True, slots=True)
class EventOutcome:
    symbol: str
    signal_date: date
    entry_date: date | None
    exit_date: date | None
    horizon: int
    price_basis: str | None  # "raw" | "adjusted" | None (dropped)
    gross_return: Decimal | None
    net_return: Decimal | None
    dropped: bool
    drop_reason: str | None
    condition_value: bool | None  # None for baseline events


def _entry_index(tradeable_dates: tuple[date, ...], signal_date: date) -> int | None:
    index = bisect.bisect_right(tradeable_dates, signal_date)
    return index if index < len(tradeable_dates) else None


def _price_return(
    symbol_data: SymbolData, entry_index: int, horizon: int, *,
    entry_timing: str, adjustment_boundary_policy: str, cost_bps: Decimal,
    condition_value: bool | None, symbol: str, signal_date: date,
) -> EventOutcome:
    exit_index = entry_index + horizon
    if exit_index >= len(symbol_data.tradeable_bars):
        return EventOutcome(
            symbol, signal_date, None, None, horizon, None, None, None,
            True, "insufficient_bars", condition_value,
        )
    entry_bar = symbol_data.tradeable_bars[entry_index]
    exit_bar = symbol_data.tradeable_bars[exit_index]

    boundary_in_window = any(
        entry_bar.trade_date < d <= exit_bar.trade_date for d in symbol_data.adjustment_boundary_dates
    )
    if boundary_in_window:
        if adjustment_boundary_policy == "drop":
            return EventOutcome(
                symbol, signal_date, entry_bar.trade_date, exit_bar.trade_date, horizon,
                None, None, None, True, "adjustment_boundary_drop", condition_value,
            )
        entry_price = entry_bar.adjusted_close
        exit_price = exit_bar.adjusted_close
        basis = "adjusted"
    else:
        entry_price = entry_bar.open if entry_timing == "next_open" else entry_bar.close
        exit_price = exit_bar.close
        basis = "raw"

    if entry_price is None or exit_price is None or entry_price == 0:
        return EventOutcome(
            symbol, signal_date, entry_bar.trade_date, exit_bar.trade_date, horizon,
            None, None, None, True, "insufficient_bars", condition_value,
        )

    gross_return = exit_price / entry_price - 1
    net_return = gross_return - 2 * (cost_bps / Decimal(10000))
    return EventOutcome(
        symbol, signal_date, entry_bar.trade_date, exit_bar.trade_date, horizon,
        basis, gross_return, net_return, False, None, condition_value,
    )


def build_events(
    symbol_data: SymbolData, spec: StudySpec, *, horizon: int, cost_bps: Decimal,
    signal_dates: tuple[date, ...],
) -> tuple[list[EventOutcome], list[EventOutcome]]:
    """Returns ``(conditioned_events, baseline_events)`` for one symbol/horizon.

    ``signal_dates`` are the server dates in scope for the requested split
    (already time-filtered by the caller). A baseline event is built for
    every one of them; a conditioned event is built only where the
    condition evaluates ``True`` and its inputs do not span a custody scale
    jump.
    """
    conditioned: list[EventOutcome] = []
    baseline: list[EventOutcome] = []
    for t in signal_dates:
        entry_index = _entry_index(symbol_data.tradeable_dates, t)
        if entry_index is None:
            baseline.append(EventOutcome(
                symbol_data.symbol, t, None, None, horizon, None, None, None,
                True, "insufficient_bars", None,
            ))
        else:
            baseline.append(_price_return(
                symbol_data, entry_index, horizon, entry_timing=spec.entry_timing,
                adjustment_boundary_policy=spec.adjustment_boundary_policy, cost_bps=cost_bps,
                condition_value=None, symbol=symbol_data.symbol, signal_date=t,
            ))

        truncated = truncate_period_capture(symbol_data.capture, t)
        result = evaluate_condition(spec.condition, truncated, t, close_by_date=symbol_data.close_by_date)
        if result.value is None:
            conditioned.append(EventOutcome(
                symbol_data.symbol, t, None, None, horizon, None, None, None,
                True, "condition_not_evaluable", None,
            ))
            continue
        if result.value is False:
            continue  # condition false that day: not an event, not a drop
        spans_jump = "custody" in result.data_families and any(
            result.min_date is not None and result.max_date is not None
            and result.min_date < jump_date <= result.max_date
            for jump_date in symbol_data.custody_scale_jump_dates
        )
        if spans_jump:
            conditioned.append(EventOutcome(
                symbol_data.symbol, t, None, None, horizon, None, None, None,
                True, "custody_scale_jump", True,
            ))
            continue
        if entry_index is None:
            conditioned.append(EventOutcome(
                symbol_data.symbol, t, None, None, horizon, None, None, None,
                True, "insufficient_bars", True,
            ))
            continue
        conditioned.append(_price_return(
            symbol_data, entry_index, horizon, entry_timing=spec.entry_timing,
            adjustment_boundary_policy=spec.adjustment_boundary_policy, cost_bps=cost_bps,
            condition_value=True, symbol=symbol_data.symbol, signal_date=t,
        ))
    return conditioned, baseline


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    seed: int
    resamples: int
    confidence_level: Decimal
    lower: float | None
    upper: float | None
    mean_difference: float | None


def bootstrap_diff_ci(
    conditioned_returns: list[Decimal], baseline_returns: list[Decimal], *,
    seed: int, resamples: int, confidence_level: Decimal,
) -> BootstrapResult:
    """Seeded two-sample percentile bootstrap on the difference of means.

    Deterministic given identical inputs and ``seed``: this function creates
    its own ``random.Random(seed)`` and never reads global random state, so
    two calls with the same arguments always produce identical bounds
    (exercised by ``tests/test_study_engine.py``).
    """
    if not conditioned_returns or not baseline_returns:
        return BootstrapResult(seed, resamples, confidence_level, None, None, None)
    rng = random.Random(seed)
    conditioned = [float(value) for value in conditioned_returns]
    baseline = [float(value) for value in baseline_returns]
    n_c, n_b = len(conditioned), len(baseline)
    diffs: list[float] = []
    for _ in range(resamples):
        sample_c = [conditioned[rng.randrange(n_c)] for _ in range(n_c)]
        sample_b = [baseline[rng.randrange(n_b)] for _ in range(n_b)]
        diffs.append(statistics.fmean(sample_c) - statistics.fmean(sample_b))
    diffs.sort()
    alpha = (Decimal(1) - confidence_level) / 2
    lower_index = max(0, min(resamples - 1, int(alpha * resamples)))
    upper_index = max(0, min(resamples - 1, int((Decimal(1) - alpha) * resamples) - 1))
    return BootstrapResult(
        seed, resamples, confidence_level, diffs[lower_index], diffs[upper_index],
        statistics.fmean(diffs),
    )


@dataclass(frozen=True, slots=True)
class DatePanel:
    """Kept events grouped by signal date, ascending: the E1 resampling unit."""

    dates: tuple[date, ...]
    conditioned_sum: tuple[float, ...]
    conditioned_count: tuple[int, ...]
    baseline_sum: tuple[float, ...]
    baseline_count: tuple[int, ...]


def build_date_panel(
    conditioned_events: list[EventOutcome], baseline_events: list[EventOutcome],
) -> DatePanel:
    """Groups kept (non-dropped) events by ``signal_date`` across all symbols.

    The date axis is every signal date carrying at least one kept
    conditioned or baseline event; a date whose events were all dropped
    carries no return and is not a resampling unit.
    """
    cond: dict[date, list[float]] = {}
    base: dict[date, list[float]] = {}
    for events, target in ((conditioned_events, cond), (baseline_events, base)):
        for event in events:
            if event.dropped:
                continue
            target.setdefault(event.signal_date, []).append(float(event.net_return))
    dates = tuple(sorted(cond.keys() | base.keys()))
    return DatePanel(
        dates=dates,
        conditioned_sum=tuple(math.fsum(cond.get(d, ())) for d in dates),
        conditioned_count=tuple(len(cond.get(d, ())) for d in dates),
        baseline_sum=tuple(math.fsum(base.get(d, ())) for d in dates),
        baseline_count=tuple(len(base.get(d, ())) for d in dates),
    )


def draw_moving_block_indices(rng: random.Random, n_dates: int, block_length: int) -> list[int]:
    """One moving-block resample of date positions ``0..n_dates-1``.

    ``ceil(n_dates / block_length)`` block starts are drawn with replacement
    from ``0..n_dates - block_length``; each contributes ``block_length``
    consecutive positions; the concatenation is cut to ``n_dates``.
    """
    if block_length < 1 or n_dates < block_length:
        raise EngineError("block_length_exceeds_distinct_signal_dates")
    n_starts = n_dates - block_length + 1
    indices: list[int] = []
    while len(indices) < n_dates:
        start = rng.randrange(n_starts)
        indices.extend(range(start, start + block_length))
    return indices[:n_dates]


def block_bootstrap_replicates(
    panel: DatePanel, *, block_length: int, resamples: int, seed: int,
) -> list[tuple[float | None, float | None]]:
    """``(conditioned_mean, baseline_mean)`` per replicate; ``None`` when the
    drawn dates hold no event of that population. Deterministic given the
    panel, ``block_length``, ``resamples`` and ``seed`` (own
    ``random.Random(seed)``, no global random state)."""
    rng = random.Random(seed)
    n = len(panel.dates)
    replicates: list[tuple[float | None, float | None]] = []
    for _ in range(resamples):
        indices = draw_moving_block_indices(rng, n, block_length)
        c_count = sum(panel.conditioned_count[i] for i in indices)
        b_count = sum(panel.baseline_count[i] for i in indices)
        c_mean = math.fsum(panel.conditioned_sum[i] for i in indices) / c_count if c_count else None
        b_mean = math.fsum(panel.baseline_sum[i] for i in indices) / b_count if b_count else None
        replicates.append((c_mean, b_mean))
    return replicates


def one_sided_p_value(deltas: list[float | None], predicted_direction: str) -> float:
    """README formula; ``None`` (undefined replicate) never supports the
    predicted direction."""
    if predicted_direction == "negative":
        against = sum(1 for d in deltas if d is None or d >= 0)
    elif predicted_direction == "positive":
        against = sum(1 for d in deltas if d is None or d <= 0)
    else:
        raise EngineError("invalid_predicted_direction")
    return (1 + against) / (len(deltas) + 1)


def nearest_rank_percentile(sorted_values: list[float], q: Decimal) -> float | None:
    if not sorted_values:
        return None
    rank = math.ceil(q * len(sorted_values))
    return sorted_values[max(0, min(len(sorted_values) - 1, rank - 1))]


@dataclass(frozen=True, slots=True)
class BlockBootstrapResult:
    method: str
    block_length_signal_dates: int
    resamples: int
    seed: int
    distinct_signal_dates: int  # N, the resampling units (kept events of either population)
    distinct_conditioned_signal_dates: int
    observed_difference: float | None
    predicted_direction: str | None
    p_value_one_sided: float | None
    percentile_05: float | None
    percentile_95: float | None
    undefined_resamples: int
    status: str  # "computed" | "no_conditioned_events" | "no_baseline_events" | "too_few_signal_dates"


BLOCK_BOOTSTRAP_METHOD = "moving_block_by_signal_date"


def moving_block_bootstrap(
    conditioned_events: list[EventOutcome], baseline_events: list[EventOutcome], *,
    config: BlockBootstrapConfig, predicted_direction: str | None,
) -> BlockBootstrapResult:
    """E1: see ``FORMULAS["block_bootstrap"]``/``["one_sided_p_value"]``.

    ``predicted_direction`` comes only from the spec's preregistration
    block; without it no p value is reported (never both tails, so a tail
    cannot be chosen after seeing the result).
    """
    if predicted_direction not in (None, "negative", "positive"):
        raise EngineError("invalid_predicted_direction")
    panel = build_date_panel(conditioned_events, baseline_events)
    n_cond_dates = sum(1 for count in panel.conditioned_count if count)
    n_cond = sum(panel.conditioned_count)
    n_base = sum(panel.baseline_count)
    observed = None
    if n_cond and n_base:
        observed = math.fsum(panel.conditioned_sum) / n_cond - math.fsum(panel.baseline_sum) / n_base

    def empty(status: str) -> BlockBootstrapResult:
        return BlockBootstrapResult(
            BLOCK_BOOTSTRAP_METHOD, config.block_length_signal_dates, config.resamples, config.seed,
            len(panel.dates), n_cond_dates, observed, predicted_direction, None, None, None, 0, status,
        )

    if not n_cond:
        return empty("no_conditioned_events")
    if not n_base:
        return empty("no_baseline_events")
    if len(panel.dates) < config.block_length_signal_dates:
        return empty("too_few_signal_dates")
    replicates = block_bootstrap_replicates(
        panel, block_length=config.block_length_signal_dates, resamples=config.resamples,
        seed=config.seed,
    )
    deltas = [None if c is None or b is None else c - b for c, b in replicates]
    defined = sorted(d for d in deltas if d is not None)
    return BlockBootstrapResult(
        method=BLOCK_BOOTSTRAP_METHOD,
        block_length_signal_dates=config.block_length_signal_dates,
        resamples=config.resamples,
        seed=config.seed,
        distinct_signal_dates=len(panel.dates),
        distinct_conditioned_signal_dates=n_cond_dates,
        observed_difference=observed,
        predicted_direction=predicted_direction,
        p_value_one_sided=(
            None if predicted_direction is None else one_sided_p_value(deltas, predicted_direction)
        ),
        percentile_05=nearest_rank_percentile(defined, Decimal("0.05")),
        percentile_95=nearest_rank_percentile(defined, Decimal("0.95")),
        undefined_resamples=len(deltas) - len(defined),
        status="computed",
    )


@dataclass(frozen=True, slots=True)
class HorizonStats:
    horizon: int
    event_count: int
    mean_return: Decimal | None
    median_return: Decimal | None
    hit_rate: Decimal | None
    baseline_event_count: int
    baseline_mean_return: Decimal | None
    baseline_median_return: Decimal | None
    difference_mean: Decimal | None
    bootstrap: BootstrapResult
    below_min_events_warning: bool
    dropped_insufficient_bars: int
    dropped_adjustment_boundary: int
    dropped_custody_scale_jump: int
    condition_not_evaluable: int
    adjusted_close_used_count: int
    distinct_conditioned_signal_dates: int
    block_bootstrap: BlockBootstrapResult | None


def _stats_from_returns(returns: list[Decimal]) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    if not returns:
        return None, None, None
    mean = sum(returns, Decimal(0)) / len(returns)
    median = Decimal(str(statistics.median(returns)))
    hit_rate = Decimal(sum(1 for value in returns if value > 0)) / len(returns)
    return mean, median, hit_rate


def summarize_horizon(
    conditioned: list[EventOutcome], baseline: list[EventOutcome], *,
    horizon: int, min_event_warning_threshold: int, bootstrap_seed: int, bootstrap_resamples: int,
    bootstrap_confidence_level: Decimal,
    block_bootstrap: BlockBootstrapConfig | None = None, predicted_direction: str | None = None,
) -> HorizonStats:
    """``block_bootstrap`` (E1) is computed only when a config is given;
    the i.i.d. ``bootstrap`` is always computed as a secondary diagnostic."""
    kept = [event for event in conditioned if not event.dropped]
    kept_returns = [event.net_return for event in kept]
    baseline_kept = [event.net_return for event in baseline if not event.dropped]
    mean, median, hit_rate = _stats_from_returns(kept_returns)
    baseline_mean, baseline_median, _ = _stats_from_returns(baseline_kept)
    difference = None if mean is None or baseline_mean is None else mean - baseline_mean
    bootstrap = bootstrap_diff_ci(
        kept_returns, baseline_kept, seed=bootstrap_seed, resamples=bootstrap_resamples,
        confidence_level=bootstrap_confidence_level,
    )
    return HorizonStats(
        horizon=horizon,
        event_count=len(kept),
        mean_return=mean,
        median_return=median,
        hit_rate=hit_rate,
        baseline_event_count=len(baseline_kept),
        baseline_mean_return=baseline_mean,
        baseline_median_return=baseline_median,
        difference_mean=difference,
        bootstrap=bootstrap,
        below_min_events_warning=len(kept) < min_event_warning_threshold,
        dropped_insufficient_bars=sum(
            1 for e in conditioned if e.drop_reason == "insufficient_bars"
        ),
        dropped_adjustment_boundary=sum(
            1 for e in conditioned if e.drop_reason == "adjustment_boundary_drop"
        ),
        dropped_custody_scale_jump=sum(
            1 for e in conditioned if e.drop_reason == "custody_scale_jump"
        ),
        condition_not_evaluable=sum(
            1 for e in conditioned if e.drop_reason == "condition_not_evaluable"
        ),
        adjusted_close_used_count=sum(1 for e in kept if e.price_basis == "adjusted"),
        distinct_conditioned_signal_dates=len({event.signal_date for event in kept}),
        block_bootstrap=(
            None if block_bootstrap is None else moving_block_bootstrap(
                conditioned, baseline, config=block_bootstrap, predicted_direction=predicted_direction,
            )
        ),
    )


def symbols_for_split(spec: StudySpec, split: str) -> tuple[str, ...]:
    """Train excludes hold-out symbols entirely; test (frozen-gated by the
    caller) uses every symbol the spec ever declared."""
    if split == "train":
        return spec.symbols
    if split == "test":
        return spec.all_declared_symbols()
    raise EngineError("invalid_split")


def signal_dates_for_split(
    capture: PeriodCapture, cutoff: date, split: str, *, window_end: date | None = None,
) -> tuple[date, ...]:
    """``window_end`` (test split only): drop signal dates after it (E3, the
    pre-registered test-window end; ``docs/prereg/DECISIONS.md`` 2026-09-28).
    ``None`` (default, and always for ``train``) applies no upper bound,
    unchanged from the pre-E3 behaviour."""
    if split == "train":
        return tuple(d for d in capture.server_dates if d < cutoff)
    if split == "test":
        dates = tuple(d for d in capture.server_dates if d >= cutoff)
        if window_end is not None:
            dates = tuple(d for d in dates if d <= window_end)
        return dates
    raise EngineError("invalid_split")
