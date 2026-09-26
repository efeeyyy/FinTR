"""Deterministic condition-input features for the study engine.

Every feature reads only a *truncated* ``PeriodCapture`` (see
``truncate_period_capture``) that structurally contains no data dated after
the signal day ``t``: the truncation happens once, before any feature runs,
so a feature cannot see day ``t+1`` even if it wanted to. This is the
engine's lookahead guard for AKD/custody inputs; ``tests/test_study_engine.py``
exercises it directly.

Feature names here are the only vocabulary a study spec's ``condition`` may
reference (see ``fintr.research.conditions``); there is no free-text formula
and no model call. Values are ``Decimal`` or ``None`` (not evaluable that
day); ``None`` is never coerced to ``0``. Each feature also reports which
data families it read and the inclusive date range it read from, used by the
engine to detect a custody scale jump spanning a conditioned event's inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from fintr.data.period_capture import PeriodCapture
from fintr.features.period import daily_market_proxy


FORMULA_VERSION = "study_features_v1"

FEATURE_FORMULAS: dict[str, str] = {
    "top1_akd_net_share_of_volume": (
        "max_code(|daily_akd.net_size[code, t]|) / daily_market_proxy."
        "sum_total_size[t]; None if the day has no daily_akd_market rows or "
        "sum_total_size[t] is 0"
    ),
    "flow_run_length": (
        "current same-sign run length, in listed server dates ending at t, "
        "of sum_code_in_codes(daily_akd.net_size[code, date]) over only the "
        "codes present that date; None if no code in `codes` is listed on t"
    ),
    "custody_change": (
        "sum_code_in_codes(daily_custody_lots[code, t]) - "
        "sum_code_in_codes(daily_custody_lots[code, t_minus_lag]); requires "
        "every code in `codes` to be listed on both dates, else None; "
        "t_minus_lag is the server date `lag_days` positions earlier in "
        "server_dates, never a calendar-day offset"
    ),
    "vwap_proxy_vs_close": (
        "(daily_market_proxy.vwap_proxy[t] - price_bar.close[t]) / "
        "price_bar.close[t]; None unless both the AKD-derived proxy and a "
        "same-date price bar close are present and close != 0"
    ),
}


class FeatureError(ValueError):
    """A feature was invoked with structurally invalid parameters."""


@dataclass(frozen=True, slots=True)
class FeatureValue:
    """One feature's result plus lookahead/leakage provenance.

    ``min_date``/``max_date`` bound the inclusive range of dates the
    feature actually read (``None`` when the value itself is ``None``);
    ``data_families`` is a subset of ``("akd", "custody", "price")``.
    """

    value: Decimal | None
    data_families: tuple[str, ...]
    min_date: date | None
    max_date: date | None


_NOT_EVALUABLE = FeatureValue(None, (), None, None)


def truncate_period_capture(capture: PeriodCapture, as_of: date) -> PeriodCapture:
    """A copy of ``capture`` containing no dated row after ``as_of``.

    This is the structural lookahead guard: callers pass the *result* of
    this function to every feature, so a feature has no representation of
    ``t+1`` in hand at all, independent of what its own parameters say.
    Period-level aggregates (``period_akd_net``, ``custodian_diff``) span
    the whole requested period and could describe the last server date, so
    they are dropped entirely rather than partially truncated; no feature
    in this module uses them.
    """
    if not isinstance(as_of, date):
        raise TypeError("as_of must be a date")
    return replace(
        capture,
        server_dates=tuple(d for d in capture.server_dates if d <= as_of),
        period_akd_net=(),
        daily_akd_net=tuple(day for day in capture.daily_akd_net if day.date <= as_of),
        daily_akd_market=tuple(day for day in capture.daily_akd_market if day.date <= as_of),
        daily_custody_lots=tuple(day for day in capture.daily_custody_lots if day.date <= as_of),
        custodian_diff=(),
        metadata=tuple(day for day in capture.metadata if day.date <= as_of),
    )


def _require_codes(params: dict, path: str) -> tuple[str, ...]:
    raw = params.get("codes")
    if not isinstance(raw, (list, tuple)) or not raw:
        raise FeatureError(f"{path}.codes: expected a non-empty list of strings")
    codes = tuple(raw)
    if any(not isinstance(code, str) or not code for code in codes):
        raise FeatureError(f"{path}.codes: every entry must be a non-empty string")
    if len(set(codes)) != len(codes):
        raise FeatureError(f"{path}.codes: duplicate code")
    return codes


def _require_lag_days(params: dict, path: str) -> int:
    value = params.get("lag_days")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise FeatureError(f"{path}.lag_days: expected a positive integer")
    return value


def validate_feature_params(name: str, params: dict, path: str) -> None:
    """Raise ``FeatureError`` if ``params`` is not what ``name`` accepts.

    Called once at spec-parse time so a malformed condition is rejected
    before any capture is ever loaded, not discovered mid-run.
    """
    if name not in FEATURE_FORMULAS:
        raise FeatureError(f"{path}: unknown feature '{name}'")
    if name in ("top1_akd_net_share_of_volume", "vwap_proxy_vs_close"):
        if params:
            raise FeatureError(f"{path}: feature '{name}' takes no parameters")
    elif name == "flow_run_length":
        _require_codes(params, path)
        if set(params) - {"codes"}:
            raise FeatureError(f"{path}: feature 'flow_run_length' takes only 'codes'")
    elif name == "custody_change":
        _require_codes(params, path)
        _require_lag_days(params, path)
        if set(params) - {"codes", "lag_days"}:
            raise FeatureError(
                f"{path}: feature 'custody_change' takes only 'codes' and 'lag_days'"
            )


def _top1_akd_net_share_of_volume(capture: PeriodCapture, t: date) -> FeatureValue:
    market_day = next((day for day in capture.daily_akd_market if day.date == t), None)
    if market_day is None or not market_day.rows:
        return _NOT_EVALUABLE
    sum_total_size = sum((row.total_size for row in market_day.rows), Decimal(0))
    if sum_total_size == 0:
        return FeatureValue(None, ("akd",), t, t)
    net_day = next((day for day in capture.daily_akd_net if day.date == t), None)
    net_by_code = net_day.by_code() if net_day is not None else {}
    magnitudes = [abs(net_by_code[row.code]) for row in market_day.rows if row.code in net_by_code]
    if not magnitudes:
        return FeatureValue(None, ("akd",), t, t)
    value = max(magnitudes) / sum_total_size
    return FeatureValue(value, ("akd",), t, t)


def _flow_run_length(capture: PeriodCapture, t: date, params: dict) -> FeatureValue:
    codes = _require_codes(params, "flow_run_length")
    dates = capture.server_dates
    if t not in dates:
        return _NOT_EVALUABLE
    daily = {day.date: day.by_code() for day in capture.daily_akd_net}

    def combined(day: date) -> Decimal | None:
        present = [daily.get(day, {}).get(code) for code in codes]
        present = [value for value in present if value is not None]
        return sum(present, Decimal(0)) if present else None

    def sign(value: Decimal) -> int:
        return (value > 0) - (value < 0)

    value_t = combined(t)
    if value_t is None:
        return _NOT_EVALUABLE
    run_sign = sign(value_t)
    run_length = 0
    run_start = t
    for day in reversed(dates):
        if day > t:
            continue
        value = combined(day)
        if value is None or sign(value) != run_sign:
            break
        run_length += 1
        run_start = day
    return FeatureValue(Decimal(run_length), ("akd",), run_start, t)


def _custody_change(capture: PeriodCapture, t: date, params: dict) -> FeatureValue:
    codes = _require_codes(params, "custody_change")
    lag_days = _require_lag_days(params, "custody_change")
    dates = capture.server_dates
    if t not in dates:
        return _NOT_EVALUABLE
    index = dates.index(t)
    target_index = index - lag_days
    if target_index < 0:
        return _NOT_EVALUABLE
    prev_date = dates[target_index]
    custody = {day.date: day.by_code() for day in capture.daily_custody_lots}
    current = custody.get(t, {})
    previous = custody.get(prev_date, {})
    if any(code not in current or code not in previous for code in codes):
        return FeatureValue(None, ("custody",), prev_date, t)
    value = sum((current[code] for code in codes), Decimal(0)) - sum(
        (previous[code] for code in codes), Decimal(0)
    )
    return FeatureValue(value, ("custody",), prev_date, t)


def _vwap_proxy_vs_close(capture: PeriodCapture, t: date, close_by_date: dict[date, Decimal]) -> FeatureValue:
    close = close_by_date.get(t)
    if close is None or close == 0:
        return FeatureValue(None, ("akd", "price"), t, t)
    proxy_day = next((day for day in daily_market_proxy(capture).days if day.date == t), None)
    if proxy_day is None or proxy_day.vwap_proxy is None:
        return FeatureValue(None, ("akd", "price"), t, t)
    value = (proxy_day.vwap_proxy - close) / close
    return FeatureValue(value, ("akd", "price"), t, t)


def evaluate_feature(
    name: str, params: dict, capture: PeriodCapture, t: date, *, close_by_date: dict[date, Decimal],
) -> FeatureValue:
    """Evaluate one named feature against an already-truncated ``capture``.

    ``close_by_date`` is the full (untruncated) price series' close-by-date
    map; only the single entry at ``t`` is ever read (a same-day close is
    legitimately known after close of ``t``), so passing the full map does
    not reintroduce lookahead.
    """
    if name == "top1_akd_net_share_of_volume":
        return _top1_akd_net_share_of_volume(capture, t)
    if name == "flow_run_length":
        return _flow_run_length(capture, t, params)
    if name == "custody_change":
        return _custody_change(capture, t, params)
    if name == "vwap_proxy_vs_close":
        return _vwap_proxy_vs_close(capture, t, close_by_date)
    raise FeatureError(f"unknown feature '{name}'")
