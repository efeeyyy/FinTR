"""Pure computation of the walk-forward accuracy tracker (no file or network IO).

What is measured (see ``docs/accuracy.md``): for each development symbol, each
signal day ``t`` and each frozen rule whose condition holds at ``t``, whether
the realized direction of the forward return matched the rule's frozen
``predicted_direction``, at the primary horizon (5 trading days) and the
secondary horizon (1 trading day), with the unconditional direction fractions
of the same symbols and days as the base rate. Nothing here recomputes an
engine formula: the entry/exit/return definition is
``fintr.research.engine._price_return`` and the prediction is
``fintr.scanner.observe.evaluate_rule_for_symbol``.

Point in time: a reconstructed prediction at ``t`` only sees
``point_in_time_view`` (capture rows dated ``<= t`` with custody additionally
limited to dates settled by ``t``, and price bars dated ``<= t``). A change to
data dated after ``t`` cannot change it. Residual look-ahead (revised values of
days ``<= t``) is documented in ``docs/accuracy.md`` and reported in the output.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
import math

from fintr.data.period_capture import PeriodCapture
from fintr.data.price_history import PriceHistoryCapture
from fintr.evaluation.symbol_guard import ActiveGuards
from fintr.features.price_join import DEFAULT_RATIO_REGIME_TOLERANCE
from fintr.research.engine import (
    _entry_index,  # reused, not reimplemented: the engine's own entry-day rule
    _price_return,  # reused, not reimplemented: the engine's own return definition
    load_symbol_data,
    tradeable_price_bars,
)
from fintr.research.features import truncate_period_capture
from fintr.scanner.observe import RuleObservation, evaluate_rule_for_symbol
from fintr.scanner.rules import ScannerRule


class AccuracyError(ValueError):
    """A precondition of the accuracy tracker was violated (reported, never swallowed)."""


PERIOD_TRAIN = "train_window"
PERIOD_POST_TRAIN = "post_train"
PERIODS = (PERIOD_TRAIN, PERIOD_POST_TRAIN)
TRAIN_WINDOW_END = date(2026, 9, 25)  # signal date <= this: the rules were built on this window
TEST_WINDOW_START = date(2026, 10, 1)  # primary holdout test window start (spec test_window.signal_start)

COST_BPS_PER_SIDE = Decimal("10")  # run_parameters.cost_bps_per_side of the frozen specs
SECONDARY_HORIZON = 1
DEFAULT_WINDOW_DAYS = 42
Z_95 = 1.959963984540054  # two-sided 95% normal quantile

OUTCOME_HIT = "hit"
OUTCOME_MISS = "miss"
OUTCOME_ZERO = "zero_return"
OUTCOME_PENDING = "pending"
OUTCOME_DROPPED = "dropped"
RESOLVED_OUTCOMES = (OUTCOME_HIT, OUTCOME_MISS, OUTCOME_ZERO)

SOURCE_FORWARD_LOG = "forward_log"
SOURCE_SAVED_SCAN = "saved_scan"
SOURCE_RECONSTRUCTED = "reconstructed"

FORMULAS = {
    "forward_return": (
        "engine definition (fintr.research.engine._price_return): entry = open of the first "
        "tradeable bar strictly after t (spec.entry_timing next_open); exit = close of the "
        "tradeable bar `horizon` positions after entry; adjustment boundary inside the window "
        "-> dropped (spec policy 'drop'); gross_return = exit/entry - 1; net_return = "
        "gross_return - 2*cost_bps/10000"
    ),
    "hit": "sign(gross_return) == sign of the frozen predicted_direction (positive: > 0, negative: < 0)",
    "zero_return": "gross_return == 0: not a hit, counted separately",
    "cost_aware_hit": (
        "positive: gross_return > 2*cost_bps/10000; negative: gross_return < -2*cost_bps/10000 "
        "(|return| must exceed the round-trip cost in the predicted direction)"
    ),
    "pending": "the horizon has not elapsed in the saved price history (never guessed)",
    "base_rate": (
        "unconditional fraction of resolved (symbol, day) returns in the predicted direction "
        "(strictly > 0 for positive, strictly < 0 for negative; cost-aware: beyond the round-trip "
        "cost) over the same symbols, days, period and horizon; ratio-regime days dropped as in "
        "the engine; for a pooled group the mean of each row's own direction base"
    ),
    "difference": "hit_rate - base_rate (proportion points); cost-aware likewise",
    "wilson_95": "Wilson score interval, z=1.959964, on hits/resolved (treats outcomes as independent)",
}


# --------------------------------------------------------------------------
# small statistics
# --------------------------------------------------------------------------

def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """Wilson score interval for ``successes`` out of ``n``; ``None`` when n == 0."""
    if n <= 0:
        return None
    if not 0 <= successes <= n:
        raise AccuracyError("successes_out_of_range")
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def period_of(t: date) -> str:
    return PERIOD_TRAIN if t <= TRAIN_WINDOW_END else PERIOD_POST_TRAIN


# --------------------------------------------------------------------------
# universe guard
# --------------------------------------------------------------------------

def validate_symbols(requested: list[str], guards: ActiveGuards) -> list[str]:
    """Sorted development symbols; an excluded or holdout symbol is refused
    (the holdout stays unmonitored: Efe's decision of 2026-10-16)."""
    symbols = sorted({symbol.strip().upper() for symbol in requested})
    excluded = [symbol for symbol in symbols if guards.is_excluded(symbol)]
    if excluded:
        raise AccuracyError(f"excluded symbol(s) requested ({','.join(excluded)})")
    holdout = [symbol for symbol in symbols if guards.is_holdout(symbol)]
    if holdout:
        raise AccuracyError(
            f"holdout symbol(s) requested ({','.join(holdout)}); holdout symbols are never monitored"
        )
    development = set(guards.split.development_symbols)
    outside = [symbol for symbol in symbols if symbol not in development]
    if outside:
        raise AccuracyError(f"symbol(s) not in the active split's development set ({','.join(outside)})")
    return symbols


# --------------------------------------------------------------------------
# inputs, window, point-in-time view
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SymbolInput:
    symbol: str
    capture: PeriodCapture  # base capture, or base + rolling captures composed (latest view)
    price: PriceHistoryCapture


@dataclass(frozen=True, slots=True)
class EvalParams:
    adjustment_ratio_epsilon: Decimal
    custody_scale_jump_ratio: Decimal
    ratio_regime_tolerance: Decimal = DEFAULT_RATIO_REGIME_TOLERANCE


@dataclass(frozen=True, slots=True)
class SavedPrediction:
    """One rule's saved scanner observation (``scanner_daily/<as_of>/observations.json``)."""

    source: str  # SOURCE_FORWARD_LOG | SOURCE_SAVED_SCAN
    condition_met: bool | None
    suppressed: str | None


def symbol_signal_days(item: SymbolInput) -> tuple[date, ...]:
    """Server dates on which the symbol also has a tradeable price bar."""
    tradeable = {bar.trade_date for bar in tradeable_price_bars(item.price)}
    return tuple(day for day in item.capture.server_dates if day in tradeable)


def select_window(
    inputs: list[SymbolInput], *, start: date | None, end: date | None, last_n: int,
) -> tuple[date, ...]:
    """Signal days: with an explicit ``start``/``end`` every such day in range;
    otherwise the last ``last_n`` days with data (up to ``end`` when given)."""
    days = sorted(set().union(*(symbol_signal_days(item) for item in inputs))) if inputs else []
    if end is not None:
        days = [day for day in days if day <= end]
    if start is not None:
        return tuple(day for day in days if day >= start)
    return tuple(days[-last_n:])


def custody_visible_cutoff(server_dates_up_to_t: tuple[date, ...], lag: int) -> date | None:
    """Latest custody date settled at ``t``: ``lag`` server dates before the last one."""
    if lag < 0:
        raise AccuracyError("negative_settlement_lag")
    if len(server_dates_up_to_t) <= lag:
        return None
    return server_dates_up_to_t[-1 - lag]


def point_in_time_view(
    capture: PeriodCapture, price: PriceHistoryCapture, t: date, *, custody_lag: int,
) -> tuple[PeriodCapture, PriceHistoryCapture]:
    """Data as it could have been seen at the end of server day ``t``: rows
    dated ``<= t`` (``truncate_period_capture``), custody only up to the date
    settled by ``t`` (T+``custody_lag`` in server dates), bars dated ``<= t``."""
    truncated = truncate_period_capture(capture, t)
    cutoff = custody_visible_cutoff(truncated.server_dates, custody_lag)
    lots = tuple(
        day for day in truncated.daily_custody_lots if cutoff is not None and day.date <= cutoff
    )
    price_view = replace(price, bars=tuple(bar for bar in price.bars if bar.trade_date <= t))
    return replace(truncated, daily_custody_lots=lots), price_view


# --------------------------------------------------------------------------
# outcome
# --------------------------------------------------------------------------

def predicted_direction_of(rule: ScannerRule) -> str:
    direction = rule.spec.preregistration.predicted_direction
    if direction not in ("positive", "negative"):
        raise AccuracyError(f"{rule.rule_id}: predicted_direction must be positive or negative")
    return direction


def classify_outcome(direction: str, gross_return: Decimal) -> str:
    if gross_return == 0:
        return OUTCOME_ZERO
    realized_positive = gross_return > 0
    return OUTCOME_HIT if realized_positive == (direction == "positive") else OUTCOME_MISS


def cost_aware_hit(direction: str, gross_return: Decimal, cost_bps_per_side: Decimal) -> bool:
    threshold = 2 * cost_bps_per_side / Decimal(10000)
    return gross_return > threshold if direction == "positive" else gross_return < -threshold


def forward_outcome(
    symbol_data, t: date, horizon: int, *, entry_timing: str, adjustment_boundary_policy: str,
    cost_bps_per_side: Decimal,
) -> dict:
    """Engine-defined outcome of a signal on ``t``: ``status`` is ``pending``
    (price history too short), ``dropped`` (engine drop, e.g. adjustment
    boundary) or ``resolved`` with gross/net return."""
    entry_index = _entry_index(symbol_data.tradeable_dates, t)
    if entry_index is None or entry_index + horizon >= len(symbol_data.tradeable_bars):
        return {"status": OUTCOME_PENDING}
    event = _price_return(
        symbol_data, entry_index, horizon, entry_timing=entry_timing,
        adjustment_boundary_policy=adjustment_boundary_policy, cost_bps=cost_bps_per_side,
        condition_value=None, symbol=symbol_data.symbol, signal_date=t,
    )
    if event.dropped:
        return {
            "status": OUTCOME_DROPPED, "drop_reason": event.drop_reason,
            "entry_date": event.entry_date, "exit_date": event.exit_date,
        }
    return {
        "status": "resolved", "entry_date": event.entry_date, "exit_date": event.exit_date,
        "price_basis": event.price_basis, "gross_return": event.gross_return,
        "net_return": event.net_return,
    }


# --------------------------------------------------------------------------
# tracking
# --------------------------------------------------------------------------

@dataclass(slots=True)
class BaselineTally:
    resolved: int = 0
    up: int = 0
    down: int = 0
    zero: int = 0
    up_cost: int = 0
    down_cost: int = 0
    pending: int = 0
    dropped: int = 0

    def add(self, outcome: dict, cost_bps_per_side: Decimal) -> None:
        status = outcome["status"]
        if status == OUTCOME_PENDING:
            self.pending += 1
            return
        if status == OUTCOME_DROPPED:
            self.dropped += 1
            return
        gross = outcome["gross_return"]
        self.resolved += 1
        if gross > 0:
            self.up += 1
        elif gross < 0:
            self.down += 1
        else:
            self.zero += 1
        if cost_aware_hit("positive", gross, cost_bps_per_side):
            self.up_cost += 1
        if cost_aware_hit("negative", gross, cost_bps_per_side):
            self.down_cost += 1

    def base(self, direction: str, *, cost_aware: bool) -> float | None:
        if self.resolved == 0:
            return None
        if direction == "positive":
            count = self.up_cost if cost_aware else self.up
        else:
            count = self.down_cost if cost_aware else self.down
        return count / self.resolved

    def payload(self) -> dict:
        def fraction(count: int) -> float | None:
            return None if self.resolved == 0 else _r(count / self.resolved)
        return {
            "resolved": self.resolved, "pending": self.pending, "dropped": self.dropped,
            "up": self.up, "down": self.down, "zero": self.zero,
            "up_fraction": fraction(self.up), "down_fraction": fraction(self.down),
            "zero_fraction": fraction(self.zero),
            "beyond_cost_up": self.up_cost, "beyond_cost_down": self.down_cost,
            "beyond_cost_up_fraction": fraction(self.up_cost),
            "beyond_cost_down_fraction": fraction(self.down_cost),
        }


def _observation_state(suppressed: str | None, condition_met: bool | None) -> str:
    if suppressed:
        return f"suppressed:{suppressed}"
    if condition_met is None:
        return "not_evaluable"
    return "condition_met" if condition_met else "condition_not_met"


@dataclass(slots=True)
class TrackResult:
    rows: list[dict]
    evaluation_counts: dict  # period -> source -> state -> count
    baseline: dict  # (period, horizon) -> BaselineTally
    reconstruction_check: dict
    skipped_days: dict  # symbol -> list of ISO dates in the window without server/price data
    horizons: tuple[int, ...]
    primary_horizons: tuple[int, ...]


def tracking_horizons(rules: tuple[ScannerRule, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """``(all horizons, primary horizons)``: each rule's frozen primary horizon
    plus the 1-day secondary one (which must be one of the specs' horizons)."""
    primary = sorted({rule.spec.preregistration.primary_horizon_trading_days for rule in rules})
    for rule in rules:
        if SECONDARY_HORIZON not in rule.spec.horizons_trading_days:
            raise AccuracyError(f"{rule.rule_id}: secondary horizon {SECONDARY_HORIZON} not in spec horizons")
    if len({(rule.spec.entry_timing, rule.spec.adjustment_boundary_policy) for rule in rules}) != 1:
        # the shared unconditional baseline assumes one entry/exit convention
        raise AccuracyError("rules differ in entry_timing or adjustment_boundary_policy")
    return tuple(sorted(set(primary) | {SECONDARY_HORIZON})), tuple(primary)


def track_accuracy(
    inputs: list[SymbolInput], rules: tuple[ScannerRule, ...], days: tuple[date, ...], *,
    params: EvalParams, custody_lag_on, saved: dict | None = None,
    cost_bps_per_side: Decimal = COST_BPS_PER_SIDE,
) -> TrackResult:
    """Walk forward over ``days`` for every symbol and rule.

    ``custody_lag_on(t)`` -> settlement lag in server dates effective on ``t``.
    ``saved`` maps ``(symbol, rule_id, t)`` to ``SavedPrediction``; a saved
    prediction is preferred over the reconstruction (it was written by the live
    scanner before the outcome), and every saved one is also cross-checked
    against the reconstruction.
    """
    saved = saved or {}
    horizons, primary_horizons = tracking_horizons(rules)
    rows: list[dict] = []
    evaluation_counts: dict = {}
    baseline: dict = {(period, h): BaselineTally() for period in PERIODS for h in horizons}
    skipped_days: dict = {}
    check = {"compared": 0, "agree": 0, "disagree": 0, "disagreements": []}
    day_set = set(days)

    for item in inputs:
        symbol_days = set(symbol_signal_days(item)) & day_set
        missing = sorted(day_set - symbol_days)
        if missing:
            skipped_days[item.symbol] = [day.isoformat() for day in missing]
        if not symbol_days:
            continue
        full = load_symbol_data(
            item.symbol, item.capture, item.price,
            adjustment_ratio_epsilon=params.adjustment_ratio_epsilon,
            custody_scale_jump_ratio=params.custody_scale_jump_ratio,
            ratio_regime_tolerance=params.ratio_regime_tolerance,
        )
        outcome_cache: dict = {}

        def outcome_for(t: date, horizon: int, rule: ScannerRule) -> dict:
            key = (t, horizon, rule.spec.entry_timing, rule.spec.adjustment_boundary_policy)
            if key not in outcome_cache:
                outcome_cache[key] = forward_outcome(
                    full, t, horizon, entry_timing=rule.spec.entry_timing,
                    adjustment_boundary_policy=rule.spec.adjustment_boundary_policy,
                    cost_bps_per_side=cost_bps_per_side,
                )
            return outcome_cache[key]

        for t in sorted(symbol_days):
            period = period_of(t)
            view_capture, view_price = point_in_time_view(
                item.capture, item.price, t, custody_lag=custody_lag_on(t),
            )
            # unconditional baseline (engine rule: ratio-regime days are dropped)
            if t not in full.ratio_regime_dates:
                for horizon in horizons:
                    baseline[(period, horizon)].add(outcome_for(t, horizon, rules[0]), cost_bps_per_side)

            for rule in rules:
                reconstructed: RuleObservation = evaluate_rule_for_symbol(
                    rule, view_capture, view_price, as_of=t,
                    adjustment_ratio_epsilon=params.adjustment_ratio_epsilon,
                    custody_scale_jump_ratio=params.custody_scale_jump_ratio,
                    ratio_regime_tolerance=params.ratio_regime_tolerance,
                )
                prior = saved.get((item.symbol, rule.rule_id, t))
                if prior is None:
                    source = SOURCE_RECONSTRUCTED
                    suppressed, met = reconstructed.suppressed, reconstructed.condition_met
                else:
                    source = prior.source
                    suppressed, met = prior.suppressed, prior.condition_met
                    check["compared"] += 1
                    same = (prior.suppressed, prior.condition_met) == (
                        reconstructed.suppressed, reconstructed.condition_met)
                    if same:
                        check["agree"] += 1
                    else:
                        check["disagree"] += 1
                        check["disagreements"].append({
                            "symbol": item.symbol, "rule_id": rule.rule_id, "t": t.isoformat(),
                            "saved": _observation_state(prior.suppressed, prior.condition_met),
                            "reconstructed": _observation_state(
                                reconstructed.suppressed, reconstructed.condition_met),
                        })
                state = _observation_state(suppressed, met)
                by_state = evaluation_counts.setdefault(period, {}).setdefault(source, {})
                by_state[state] = by_state.get(state, 0) + 1
                if state != "condition_met":
                    continue

                direction = predicted_direction_of(rule)
                for horizon in horizons:
                    outcome = outcome_for(t, horizon, rule)
                    row = {
                        "symbol": item.symbol, "rule_id": rule.rule_id, "t": t.isoformat(),
                        "predicted_direction": direction, "prediction_source": source,
                        "horizon": horizon,
                        "horizon_role": "primary" if horizon in primary_horizons else "secondary",
                        "period": period, "in_test_window": t >= TEST_WINDOW_START,
                        "cost_aware_hit": None, "gross_return": None, "net_return": None,
                        "entry_date": None, "exit_date": None, "price_basis": None,
                        "drop_reason": None,
                    }
                    if outcome["status"] == "resolved":
                        gross = outcome["gross_return"]
                        row.update(
                            outcome=classify_outcome(direction, gross),
                            cost_aware_hit=cost_aware_hit(direction, gross, cost_bps_per_side),
                            gross_return=str(gross), net_return=str(outcome["net_return"]),
                            entry_date=outcome["entry_date"].isoformat(),
                            exit_date=outcome["exit_date"].isoformat(),
                            price_basis=outcome["price_basis"],
                        )
                    elif outcome["status"] == OUTCOME_DROPPED:
                        row.update(
                            outcome=OUTCOME_DROPPED, drop_reason=outcome["drop_reason"],
                            entry_date=outcome["entry_date"].isoformat(),
                            exit_date=outcome["exit_date"].isoformat(),
                        )
                    else:
                        row.update(outcome=OUTCOME_PENDING)
                    rows.append(row)

    rows.sort(key=lambda r: (r["t"], r["symbol"], r["rule_id"], r["horizon"]))
    return TrackResult(
        rows=rows, evaluation_counts=evaluation_counts, baseline=baseline,
        reconstruction_check=check, skipped_days=skipped_days, horizons=horizons,
        primary_horizons=primary_horizons,
    )


# --------------------------------------------------------------------------
# summaries (never pool the two periods)
# --------------------------------------------------------------------------

def _stats(rows: list[dict], base: BaselineTally) -> dict:
    resolved = [row for row in rows if row["outcome"] in RESOLVED_OUTCOMES]
    hits = sum(1 for row in resolved if row["outcome"] == OUTCOME_HIT)
    cost_hits = sum(1 for row in resolved if row["cost_aware_hit"])
    n = len(resolved)

    def mean_base(cost_aware: bool) -> float | None:
        if not resolved:
            return None
        bases = [base.base(row["predicted_direction"], cost_aware=cost_aware) for row in resolved]
        if any(value is None for value in bases):
            return None
        return sum(bases) / len(bases)

    def block(count: int, cost_aware: bool) -> dict:
        interval = wilson_interval(count, n)
        rate = None if n == 0 else count / n
        base_rate = mean_base(cost_aware)
        return {
            "hits": count, "hit_rate": _r(rate),
            "wilson95_low": None if interval is None else _r(interval[0]),
            "wilson95_high": None if interval is None else _r(interval[1]),
            "base_rate": _r(base_rate),
            "difference": None if rate is None or base_rate is None else _r(rate - base_rate),
        }

    return {
        "signals": len(rows), "resolved": n,
        "pending": sum(1 for row in rows if row["outcome"] == OUTCOME_PENDING),
        "dropped": sum(1 for row in rows if row["outcome"] == OUTCOME_DROPPED),
        "misses": sum(1 for row in resolved if row["outcome"] == OUTCOME_MISS),
        "zero_return": sum(1 for row in resolved if row["outcome"] == OUTCOME_ZERO),
        "distinct_signal_dates": len({row["t"] for row in rows}),
        "distinct_symbols": len({row["symbol"] for row in rows}),
        "directional": block(hits, False),
        "cost_aware": block(cost_hits, True),
    }


def summarize(result: TrackResult, rule_ids: list[str]) -> dict:
    """``period -> horizon -> {baseline, by_rule, by_direction, overall}``;
    the two periods are separate branches of the result, never one number."""
    summary: dict = {}
    for period in PERIODS:
        summary[period] = {}
        for horizon in result.horizons:
            base = result.baseline[(period, horizon)]
            rows = [
                row for row in result.rows
                if row["period"] == period and row["horizon"] == horizon
            ]
            summary[period][str(horizon)] = {
                "horizon_role": "primary" if horizon in result.primary_horizons else "secondary",
                "baseline": base.payload(),
                "by_rule": {
                    rule_id: _stats([r for r in rows if r["rule_id"] == rule_id], base)
                    for rule_id in rule_ids
                },
                "by_direction": {
                    direction: _stats([r for r in rows if r["predicted_direction"] == direction], base)
                    for direction in ("positive", "negative")
                },
                "overall": _stats(rows, base),
            }
    return summary


def period_counts(result: TrackResult) -> dict:
    """Counts only (no rate): signals, resolved, pending, dropped per period
    and horizon, for logs and reports that must not carry metric values."""
    counts: dict = {}
    for period in PERIODS:
        counts[period] = {}
        for horizon in result.horizons:
            rows = [r for r in result.rows if r["period"] == period and r["horizon"] == horizon]
            counts[period][str(horizon)] = {
                "signals": len(rows),
                "resolved": sum(1 for r in rows if r["outcome"] in RESOLVED_OUTCOMES),
                "pending": sum(1 for r in rows if r["outcome"] == OUTCOME_PENDING),
                "dropped": sum(1 for r in rows if r["outcome"] == OUTCOME_DROPPED),
            }
    return counts
