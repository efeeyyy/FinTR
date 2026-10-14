"""Candidate inference methods for the SYNTHETIC engine calibration only.

NOT part of the frozen pre-registered test. The frozen primary test is E1
(``fintr.research.engine.moving_block_bootstrap`` with the spec's
``block_bootstrap``: L = 5, B = 9999); nothing here is called by
``run_study`` or ``scripts/run_study.py``. These functions exist so that
``scripts/calibrate_engine.py --method`` can compare alternatives on
synthetic panels (``docs/research/engine_calibration.md`` "Aday
yöntemler", ``docs/prereg/TRIAL_LOG.md`` T-30). Adopting any of them would
be a pre-registered amendment (Efe decision in ``docs/prereg/DECISIONS.md``
before the holdout test), not a code default.

Candidates:

- ``mbb``: the frozen engine's own moving block bootstrap with another block
  length (same date axis, same resampling unit, same p-value formula).
- ``dk``: Driscoll-Kraay standard error -- a Bartlett-kernel HAC (Newey-West)
  variance over the signal-date axis of the linearized date scores of the
  conditioned-minus-baseline difference; all symbols' events of a date are
  summed into that date's score, so same-day cross-symbol dependence is
  absorbed, and the kernel covers time dependence up to ``lags`` dates.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from fintr.research.engine import EngineError, EventOutcome, build_date_panel


DK_METHOD = "driscoll_kraay_bartlett_by_signal_date"

FORMULAS = {
    "dk_date_score": (
        "for each signal date t on the engine's date axis (sorted distinct signal "
        "dates with a kept conditioned or baseline event): u_t = (C_t - n_t * m_c) / N_c "
        "- (B_t - b_t * m_b) / N_b, where C_t, n_t (B_t, b_t) are the sum and count of "
        "kept conditioned (baseline) net returns on t across all symbols, N_c, N_b the "
        "totals and m_c, m_b the means; sum_t u_t = 0 and difference - true difference "
        "~ sum_t u_t to first order"
    ),
    "dk_variance": (
        "V = sum_t u_t^2 + 2 * sum_{k=1..M} (1 - k / (M + 1)) * sum_t u_t * u_{t+k} "
        "(Bartlett kernel, M = lags, positions on the date axis); SE = sqrt(V)"
    ),
    "dk_p_value": (
        "z = difference / SE; predicted_direction negative: p = Phi(z); positive: "
        "p = 1 - Phi(z) (standard normal reference, no small-sample correction)"
    ),
}


@dataclass(frozen=True, slots=True)
class DriscollKraayResult:
    method: str
    lags: int
    distinct_signal_dates: int
    observed_difference: float | None
    standard_error: float | None
    z: float | None
    predicted_direction: str | None
    p_value_one_sided: float | None
    lower_05: float | None  # observed - 1.644854 * SE (normal approximation)
    upper_95: float | None
    status: str  # "computed" | "no_conditioned_events" | "no_baseline_events" | "too_few_signal_dates" | "zero_variance"


Z_95 = 1.6448536269514722


def _normal_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def driscoll_kraay_test(
    conditioned_events: list[EventOutcome], baseline_events: list[EventOutcome], *,
    lags: int, predicted_direction: str | None,
) -> DriscollKraayResult:
    """See ``FORMULAS``. Deterministic; computed in float (an estimate, not a source value)."""
    if predicted_direction not in (None, "negative", "positive"):
        raise EngineError("invalid_predicted_direction")
    if lags < 0:
        raise EngineError("invalid_lags")
    panel = build_date_panel(conditioned_events, baseline_events)
    n_dates = len(panel.dates)
    n_cond = sum(panel.conditioned_count)
    n_base = sum(panel.baseline_count)

    def empty(status: str, observed: float | None = None) -> DriscollKraayResult:
        return DriscollKraayResult(DK_METHOD, lags, n_dates, observed, None, None,
                                   predicted_direction, None, None, None, status)

    if not n_cond:
        return empty("no_conditioned_events")
    if not n_base:
        return empty("no_baseline_events")
    m_c = math.fsum(panel.conditioned_sum) / n_cond
    m_b = math.fsum(panel.baseline_sum) / n_base
    observed = m_c - m_b
    if n_dates <= lags:
        return empty("too_few_signal_dates", observed)
    scores = [
        (panel.conditioned_sum[i] - panel.conditioned_count[i] * m_c) / n_cond
        - (panel.baseline_sum[i] - panel.baseline_count[i] * m_b) / n_base
        for i in range(n_dates)
    ]
    variance = math.fsum(u * u for u in scores)
    for k in range(1, lags + 1):
        weight = 1.0 - k / (lags + 1)
        variance += 2.0 * weight * math.fsum(scores[i] * scores[i + k] for i in range(n_dates - k))
    if not variance > 0.0:
        return empty("zero_variance", observed)
    se = math.sqrt(variance)
    z = observed / se
    p = None
    if predicted_direction == "negative":
        p = _normal_cdf(z)
    elif predicted_direction == "positive":
        p = 1.0 - _normal_cdf(z)
    return DriscollKraayResult(
        method=DK_METHOD, lags=lags, distinct_signal_dates=n_dates, observed_difference=observed,
        standard_error=se, z=z, predicted_direction=predicted_direction, p_value_one_sided=p,
        lower_05=observed - Z_95 * se, upper_95=observed + Z_95 * se, status="computed",
    )
