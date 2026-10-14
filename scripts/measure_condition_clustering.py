"""Measure how strongly the frozen H1-H4 CONDITION FLAGS cluster in time and across symbols.

Why: the synthetic engine calibration (``docs/research/engine_calibration.md``)
found the frozen E1 block bootstrap (L=5) nominal on the ``iid`` synthetic
world but liberal on the ``dependent`` world, whose condition flags cluster in
time (per-symbol AR phi=0.3) and across symbols (market latent phi=0.9). How
strongly REAL BIST flags cluster is unknown; this script measures it, on the
same flag definition and the same statistics, for the real development panel
and for synthetic worlds, so the numbers are comparable.

Hard scope (``docs/prereg/TRIAL_LOG.md`` T-29): only the 20 development
symbols, train window 2025-09-26..2026-09-25, and ONLY condition flags -- the
frozen conditions are evaluated with the engine's own code
(``fintr.research.conditions.evaluate_condition`` on ``truncate_period_capture``
output, after the engine's ratio-regime signal-date drop and custody
scale-jump drop, exactly as ``fintr.research.engine.build_events`` does). No
forward return, mean, p-value or study output is computed or read. Nothing here
says anything about prices; it is a statistic of when a threshold condition
holds.

Flag semantics per (symbol, signal date): 1 = condition True and not dropped;
0 = condition False; missing = ratio-regime date, condition not evaluable, or a
True condition whose inputs span a custody scale jump (the engine drops those
events). Horizon-dependent event drops (insufficient bars, adjustment boundary)
need the price path and are NOT applied: flags only.

Statistics (formulas are repeated in the JSON ``formulas`` block):
dispersion index, share of flagged symbol-days on dates with >= 3 flagged
symbols, per-symbol flag autocorrelation, run length, autocorrelation of the
daily cross-sectional flag count, and a design effect / effective-date proxy.
Sampling noise is shown by a within-symbol shuffle reference (real data) and by
several synthetic seeds.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import platform
import random
import statistics
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import synthetic_market as sm  # noqa: E402  (tests/synthetic_market.py, synthetic only)

from fintr.data.period_capture import load_period_capture  # noqa: E402
from fintr.data.price_history import load_price_history_capture  # noqa: E402
from fintr.research.capture_merge import CaptureSource, merge_period_captures  # noqa: E402
from fintr.research.conditions import evaluate_condition  # noqa: E402
from fintr.research.engine import (  # noqa: E402
    SymbolData,
    drop_ratio_regime_signal_dates,
    load_symbol_data,
    signal_dates_for_split,
    tradeable_price_bars,
)
from fintr.research.features import truncate_period_capture  # noqa: E402
from fintr.research.report import resolve_code_commit  # noqa: E402
from fintr.research.study_spec import load_study_spec  # noqa: E402


SCHEMA = "fintr_condition_clustering_v1"
SPEC_DIR = PROJECT_ROOT / "configs" / "studies" / "prereg"
SPEC_FILES = {
    "H1": "h1_close_premium_vs_vwap_proxy_v1.json",
    "H2": "h2_close_discount_vs_vwap_proxy_v1.json",
    "H3": "h3_concentrated_flow_close_premium_v1.json",
    "H4": "h4_concentrated_flow_close_discount_v1.json",
}
SPLIT_POINTER = PROJECT_ROOT / "configs" / "splits" / "current.json"
REAL_WINDOW = (date(2025, 9, 26), date(2026, 9, 25))
REAL_PERIOD_FILE = "2025-09-26_2026-09-25.json"
REAL_PRICE_FILE = "2023-01-02_2026-09-25.json"
COUNT_ACF_LAGS = 20  # printed 1..10 in the table; 20 feeds the K=20 Bartlett window
FLAG_ACF_LAGS = 5
DEFF_WINDOWS = (5, 10, 20)  # Bartlett truncation lags K; K=10 is the headline
HEADLINE_K = 10
TAIL_K = 3  # "clustered" date: at least this many flagged symbols
VARIANTS = ("iid", "dependent")

FORMULAS = {
    "flag": "1 if frozen condition True (and not custody-scale-jump dropped), 0 if False; "
            "missing if ratio-regime date, condition not evaluable or custody-jump dropped",
    "frequency": "flagged symbol-days / evaluated symbol-days",
    "dispersion_index_poibin": "sum_t (k_t - E_t)^2 / sum_t V_t; k_t = flagged symbols on date t, "
                               "E_t = sum_i p_i, V_t = sum_i p_i (1 - p_i) over symbols evaluable at t, "
                               "p_i = symbol i's own flag frequency. 1 = independent across symbols "
                               "(given each symbol's frequency)",
    "dispersion_index_binomial": "sum_t (k_t - n_t p)^2 / sum_t n_t p (1 - p); n_t = evaluable symbols at t, "
                                 "p = pooled frequency",
    "share_events_ge3": "sum_{t: k_t >= 3} k_t / sum_t k_t (observed) vs the same quantity with k_t "
                        "Poisson-binomial(p_i) independent (expected)",
    "flag_acf_lag_h": "per-symbol autocorrelation of the 0/1 flag series on the shared date axis, "
                      "pairs (t, t+h) both non-missing, mean over symbols with variance > 0",
    "mean_run_length": "flagged symbol-days in runs / number of runs of consecutive 1s (missing breaks a run); "
                       "independent expectation = sum n_i p_i / sum n_i p_i (1 - p_i)",
    "count_acf_lag_h": "autocorrelation of the daily cross-sectional flag count k_t (mean-centred, pairs both present)",
    "intra_date_correlation": "(dispersion_index_poibin - 1) / (mean n_t - 1): flag correlation between symbols on the same date",
    "deff_time_K": "Bartlett long-run variance of e_t = k_t - E_t divided by its variance: "
                   "1 + 2 sum_{h=1..K} (1 - h/(K+1)) gamma_h / gamma_0",
    "deff_total_K": "dispersion_index_poibin * deff_time_K = long-run variance of e_t / mean V_t: variance of "
                    "the flag total relative to independent Bernoulli flags",
    "effective_independent_dates": "T / deff_time_K (T = dates with >= 1 evaluable symbol): rough count of "
                                   "independent date-level observations of the flag count",
    "effective_independent_symbol_days": "evaluated symbol-days / deff_total_K",
}


# --------------------------------------------------------------------------- statistics

def _poibin_pmf(probs: list[float]) -> list[float]:
    pmf = [1.0]
    for p in probs:
        nxt = [0.0] * (len(pmf) + 1)
        for k, mass in enumerate(pmf):
            nxt[k] += mass * (1.0 - p)
            nxt[k + 1] += mass * p
        pmf = nxt
    return pmf


def _lagged_sum(values: list, lag: int, center: float) -> tuple[float, int]:
    total, pairs = 0.0, 0
    for index in range(len(values) - lag):
        a, b = values[index], values[index + lag]
        if a is None or b is None:
            continue
        total += (a - center) * (b - center)
        pairs += 1
    return total, pairs


def _acf(values: list, lags: int) -> list[float | None]:
    """Autocorrelation at lags 1..``lags`` of a series with ``None`` gaps."""
    present = [v for v in values if v is not None]
    if len(present) < 3:
        return [None] * lags
    mean = sum(present) / len(present)
    c0 = sum((v - mean) ** 2 for v in present) / len(present)
    if c0 <= 0.0:
        return [None] * lags
    out: list[float | None] = []
    for lag in range(1, lags + 1):
        total, pairs = _lagged_sum(values, lag, mean)
        out.append(None if pairs == 0 else total / pairs / c0)
    return out


def _mean_or_none(values) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def _runs(row: list) -> list[int]:
    lengths, current = [], 0
    for value in row:
        if value == 1:
            current += 1
        else:
            if current:
                lengths.append(current)
            current = 0
    if current:
        lengths.append(current)
    return lengths


def measure_matrix(matrix: list[list], *, detail: bool = True) -> dict:
    """Clustering statistics of one flag matrix (rows = symbols, columns = dates).

    Entries are 1, 0 or ``None`` (missing). ``detail=False`` skips the
    Poisson-binomial tail / histogram work (used for the shuffle reference).
    """
    n_symbols = len(matrix)
    n_dates = len(matrix[0]) if matrix else 0
    evaluated = [sum(1 for v in row if v is not None) for row in matrix]
    flagged = [sum(1 for v in row if v == 1) for row in matrix]
    total_eval, total_flag = sum(evaluated), sum(flagged)
    p_i = [(f / e if e else 0.0) for f, e in zip(flagged, evaluated)]
    pooled = total_flag / total_eval if total_eval else None

    k_series: list[float | None] = []
    n_series: list[int] = []
    e_resid: list[float | None] = []
    var_sum = 0.0
    var_dates = 0
    binom_num = binom_den = 0.0
    poibin_num = 0.0
    hist_obs: dict[int, int] = {}
    hist_exp: dict[int, float] = {}
    expected_k_total = expected_tail = observed_tail = 0.0
    for t in range(n_dates):
        live = [i for i in range(n_symbols) if matrix[i][t] is not None]
        if not live:
            k_series.append(None)
            n_series.append(0)
            e_resid.append(None)
            continue
        k = sum(1 for i in live if matrix[i][t] == 1)
        expected = sum(p_i[i] for i in live)
        variance = sum(p_i[i] * (1.0 - p_i[i]) for i in live)
        k_series.append(float(k))
        n_series.append(len(live))
        e_resid.append(k - expected)
        var_sum += variance
        var_dates += 1
        poibin_num += (k - expected) ** 2
        if pooled is not None:
            binom_num += (k - len(live) * pooled) ** 2
            binom_den += len(live) * pooled * (1.0 - pooled)
        if detail:
            hist_obs[k] = hist_obs.get(k, 0) + 1
            pmf = _poibin_pmf([p_i[i] for i in live])
            for j, mass in enumerate(pmf):
                hist_exp[j] = hist_exp.get(j, 0.0) + mass
            expected_k_total += expected
            expected_tail += expected - sum(j * pmf[j] for j in range(min(TAIL_K, len(pmf))))
            if k >= TAIL_K:
                observed_tail += k

    metrics: dict = {
        "n_symbols": n_symbols,
        "n_dates": n_dates,
        "n_dates_with_evaluable_symbols": var_dates,
        "evaluated_symbol_days": total_eval,
        "flagged_symbol_days": total_flag,
        "frequency": pooled,
    }
    if var_dates == 0 or var_sum <= 0.0:
        return metrics  # no flags at all: clustering statistics undefined
    metrics["dispersion_index_poibin"] = poibin_num / var_sum
    metrics["dispersion_index_binomial"] = binom_num / binom_den if binom_den > 0 else None
    mean_n = sum(n_series) / var_dates
    metrics["mean_evaluable_symbols_per_date"] = mean_n
    metrics["intra_date_correlation"] = (
        (metrics["dispersion_index_poibin"] - 1.0) / (mean_n - 1.0) if mean_n > 1.0 else None
    )
    if detail:
        metrics["share_events_ge3_observed"] = observed_tail / total_flag if total_flag else None
        metrics["share_events_ge3_expected_independent"] = (
            expected_tail / expected_k_total if expected_k_total > 0 else None
        )
        metrics["flag_count_histogram"] = {
            "observed_dates": {str(k): hist_obs[k] for k in sorted(hist_obs)},
            "expected_dates_independent": {str(k): round(hist_exp[k], 3) for k in sorted(hist_exp)},
        }

    per_symbol_acf = [_acf(row, FLAG_ACF_LAGS) for row in matrix]
    used = [acf for acf in per_symbol_acf if acf[0] is not None]
    metrics["flag_acf_symbols_used"] = len(used)
    for lag in range(1, FLAG_ACF_LAGS + 1):
        metrics[f"flag_acf_lag{lag}_mean"] = _mean_or_none(acf[lag - 1] for acf in per_symbol_acf)
    lengths = [length for row in matrix for length in _runs(row)]
    metrics["run_count"] = len(lengths)
    metrics["mean_run_length"] = (sum(lengths) / len(lengths)) if lengths else None
    run_den = sum(e * p * (1.0 - p) for e, p in zip(evaluated, p_i))
    metrics["mean_run_length_expected_independent"] = (total_flag / run_den) if run_den > 0 else None

    count_acf = _acf(k_series, COUNT_ACF_LAGS)
    for lag in range(1, COUNT_ACF_LAGS + 1):
        metrics[f"count_acf_lag{lag}"] = count_acf[lag - 1]
    fraction = [None if k is None else k / n for k, n in zip(k_series, n_series)]
    fraction_acf = _acf(fraction, 10)
    metrics["fraction_acf_lag1"] = fraction_acf[0]
    metrics["fraction_acf_lag5"] = fraction_acf[4]

    mean_v = var_sum / var_dates
    gamma0 = sum(e * e for e in e_resid if e is not None) / var_dates
    gammas = [
        _lagged_sum(e_resid, lag, 0.0)[0] / var_dates for lag in range(1, max(DEFF_WINDOWS) + 1)
    ]
    for window in DEFF_WINDOWS:
        lrv = gamma0 + 2.0 * sum(
            (1.0 - lag / (window + 1.0)) * gammas[lag - 1] for lag in range(1, window + 1)
        )
        deff_time = lrv / gamma0 if gamma0 > 0 else None
        metrics[f"deff_time_K{window}"] = deff_time
        metrics[f"deff_total_K{window}"] = lrv / mean_v
        metrics[f"effective_independent_dates_K{window}"] = (
            var_dates / deff_time if deff_time and deff_time > 0 else None
        )
        metrics[f"effective_independent_symbol_days_K{window}"] = (
            total_eval / (lrv / mean_v) if lrv > 0 else None
        )
    return metrics


def shuffle_reference(matrix: list[list], *, shuffles: int, seed: int) -> dict:
    """Independence reference: permute each symbol's non-missing flags in time,
    independently per symbol (missing positions stay put), and re-measure."""
    rng = random.Random(seed)
    keys = ("dispersion_index_poibin", "flag_acf_lag1_mean", "count_acf_lag1", "count_acf_lag5",
            f"deff_time_K{HEADLINE_K}", f"deff_total_K{HEADLINE_K}")
    samples: dict[str, list[float]] = {key: [] for key in keys}
    for _ in range(shuffles):
        shuffled = []
        for row in matrix:
            positions = [i for i, v in enumerate(row) if v is not None]
            values = [row[i] for i in positions]
            rng.shuffle(values)
            new = list(row)
            for position, value in zip(positions, values):
                new[position] = value
            shuffled.append(new)
        metrics = measure_matrix(shuffled, detail=False)
        for key in keys:
            if metrics.get(key) is not None:
                samples[key].append(metrics[key])
    out = {"shuffles": shuffles, "seed": seed}
    for key, values in samples.items():
        values = sorted(values)
        if not values:
            out[key] = None
            continue
        out[key] = {
            "mean": statistics.fmean(values),
            "p025": values[int(0.025 * (len(values) - 1))],
            "p975": values[int(math.ceil(0.975 * (len(values) - 1)))],
        }
    return out


# --------------------------------------------------------------------------- flags

def condition_flag_panel(symbol_data: dict[str, SymbolData], specs: dict, *, split: str = "train"):
    """Evaluate every spec's frozen condition at every in-scope signal date.

    Mirrors ``fintr.research.engine.build_events``' condition branch: the same
    ``signal_dates_for_split`` -> ``drop_ratio_regime_signal_dates`` ->
    ``truncate_period_capture`` -> ``evaluate_condition`` path and the same
    custody-scale-jump drop. Returns ``(axis, matrices, accounting)``;
    ``matrices[hid][i]`` is symbol ``i``'s row over ``axis`` with 1 / 0 / None.
    """
    cutoffs = {spec.train_test_cutoff for spec in specs.values()}
    if len(cutoffs) != 1:
        raise ValueError("specs disagree on train_test_cutoff")
    cutoff = next(iter(cutoffs))
    symbols = sorted(symbol_data)
    raw_by_symbol = {
        sym: signal_dates_for_split(symbol_data[sym].capture, cutoff, split) for sym in symbols
    }
    axis = tuple(sorted({d for dates in raw_by_symbol.values() for d in dates}))
    position = {d: i for i, d in enumerate(axis)}
    matrices = {hid: [[None] * len(axis) for _ in symbols] for hid in specs}
    accounting = {
        hid: {"in_scope_symbol_days": 0, "dropped_ratio_regime": 0, "condition_not_evaluable": 0,
              "dropped_custody_scale_jump": 0, "evaluated": 0, "flagged": 0}
        for hid in specs
    }
    for row, sym in enumerate(symbols):
        data = symbol_data[sym]
        raw = raw_by_symbol[sym]
        kept, dropped = drop_ratio_regime_signal_dates(raw, data.ratio_regime_dates)
        for hid in specs:
            accounting[hid]["in_scope_symbol_days"] += len(raw)
            accounting[hid]["dropped_ratio_regime"] += dropped
        for t in kept:
            truncated = truncate_period_capture(data.capture, t)
            for hid, spec in specs.items():
                acct = accounting[hid]
                result = evaluate_condition(
                    spec.condition, truncated, t, close_by_date=data.close_by_date,
                )
                if result.value is None:
                    acct["condition_not_evaluable"] += 1
                    continue
                if result.value is True and "custody" in result.data_families and any(
                    result.min_date is not None and result.max_date is not None
                    and result.min_date < jump <= result.max_date
                    for jump in data.custody_scale_jump_dates
                ):
                    acct["dropped_custody_scale_jump"] += 1
                    continue
                acct["evaluated"] += 1
                if result.value:
                    acct["flagged"] += 1
                matrices[hid][row][position[t]] = 1 if result.value else 0
    return axis, matrices, accounting


def _load_specs() -> dict:
    return {hid: load_study_spec(SPEC_DIR / name) for hid, name in SPEC_FILES.items()}


def _run_parameters(spec) -> dict:
    doc = spec.raw_document["preregistration"]["run_parameters"]
    return {
        "adjustment_ratio_epsilon": Decimal(doc["adjustment_ratio_epsilon"]),
        "custody_scale_jump_ratio": Decimal(doc["custody_scale_jump_ratio"]),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def development_symbols() -> tuple[list[str], dict]:
    pointer = json.loads(SPLIT_POINTER.read_text(encoding="utf-8"))
    split_path = PROJECT_ROOT / pointer["current"]
    split = json.loads(split_path.read_text(encoding="utf-8"))
    dev, holdout = list(split["development_symbols"]), set(split["holdout_symbols"])
    if holdout & set(dev):
        raise ValueError("split lists a symbol as both development and holdout")
    return dev, {"split_file": pointer["current"], "split_file_sha256": _sha256(split_path)}


def load_real_symbol_data(data_root: Path, symbols: list[str], params: dict):
    """Load the real development panel exactly as ``scripts/run_study.py`` does."""
    out, hashes = {}, {}
    for sym in symbols:
        period_path = data_root / "period_analysis" / sym / REAL_PERIOD_FILE
        price_path = data_root / "price_history" / sym / REAL_PRICE_FILE
        price = load_price_history_capture(price_path)
        period_sha = _sha256(period_path)
        merged = merge_period_captures(
            [CaptureSource(load_period_capture(period_path), label=str(period_path),
                           file_sha256=period_sha)],
            tradeable_dates=[bar.trade_date for bar in tradeable_price_bars(price)],
        )
        dates = merged.capture.server_dates
        if not dates or dates[0] < REAL_WINDOW[0] or dates[-1] > REAL_WINDOW[1]:
            raise ValueError(f"{sym}: server dates outside the train window {REAL_WINDOW}")
        out[sym] = load_symbol_data(sym, merged.capture, price, **params)
        hashes[f"{sym}:period_capture"] = period_sha
        hashes[f"{sym}:price_history"] = _sha256(price_path)
    return out, hashes


def synthetic_symbol_data(variant: str, seed_label: str, params: dict):
    cfg = sm.MarketConfig(variant=variant)  # default MarketConfig of the variant, NULL world
    world = sm.generate_world(cfg, seed_label)
    return world, {
        sym: load_symbol_data(sym, world.captures[sym], world.prices[sym], **params)
        for sym in world.symbols
    }


# --------------------------------------------------------------------------- aggregation

SCALAR_KEYS = (
    "frequency", "dispersion_index_poibin", "dispersion_index_binomial",
    "intra_date_correlation", "share_events_ge3_observed", "share_events_ge3_expected_independent",
    "flag_acf_lag1_mean", "flag_acf_lag2_mean", "flag_acf_lag3_mean", "flag_acf_lag4_mean",
    "flag_acf_lag5_mean", "mean_run_length", "mean_run_length_expected_independent",
    "count_acf_lag1", "count_acf_lag2", "count_acf_lag3", "count_acf_lag4", "count_acf_lag5",
    "count_acf_lag6", "count_acf_lag7", "count_acf_lag8", "count_acf_lag9", "count_acf_lag10",
    "deff_time_K5", "deff_time_K10", "deff_time_K20", "deff_total_K5", "deff_total_K10",
    "deff_total_K20", "effective_independent_dates_K10", "effective_independent_symbol_days_K10",
)


def aggregate(metric_list: list[dict]) -> dict:
    out = {}
    for key in SCALAR_KEYS:
        values = [m[key] for m in metric_list if m.get(key) is not None]
        out[key] = (
            {"mean": statistics.fmean(values), "min": min(values), "max": max(values), "n": len(values)}
            if values else None
        )
    return out


# --------------------------------------------------------------------------- report

TABLE_ROWS = (
    ("flag frequency", "frequency"),
    ("dispersion index (Poisson-binomial)", "dispersion_index_poibin"),
    ("intra-date correlation", "intra_date_correlation"),
    ("share of flags on dates with >=3 flagged: observed", "share_events_ge3_observed"),
    ("  ... expected if independent", "share_events_ge3_expected_independent"),
    ("per-symbol flag autocorr lag 1", "flag_acf_lag1_mean"),
    ("per-symbol flag autocorr lag 5", "flag_acf_lag5_mean"),
    ("mean run length", "mean_run_length"),
    ("  ... expected if independent", "mean_run_length_expected_independent"),
    ("daily flag-count autocorr lag 1", "count_acf_lag1"),
    ("daily flag-count autocorr lag 5", "count_acf_lag5"),
    ("daily flag-count autocorr lag 10", "count_acf_lag10"),
    ("design effect, time part (K=10)", "deff_time_K10"),
    ("design effect, total (K=10)", "deff_total_K10"),
    ("effective independent dates (K=10)", "effective_independent_dates_K10"),
    ("effective independent symbol-days (K=10)", "effective_independent_symbol_days_K10"),
)


def _fmt(value, digits=3) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.{digits}f}"


def _cell(agg: dict | None) -> str:
    if agg is None:
        return "n/a"
    return f"{_fmt(agg['mean'])} [{_fmt(agg['min'])}..{_fmt(agg['max'])}]"


def render_markdown(payload: dict) -> str:
    seeds = payload["parameters"]["synthetic_seeds"]
    lines = [
        "# Condition-flag clustering (T-29): real development panel vs synthetic worlds",
        "",
        f"Generated {payload['generated_at_utc']} from code commit `{payload['code_commit']}`; "
        f"script `scripts/measure_condition_clustering.py`. Only condition flags; no forward "
        f"returns, no study output. Real = {len(payload['symbols'])} development symbols, train "
        f"window {payload['window'][0]}..{payload['window'][1]}. Synthetic = default `MarketConfig`, "
        f"null worlds, {seeds} seeds each (mean [min..max]); `independent` = within-symbol shuffle of "
        f"the real flags ({payload['parameters']['shuffles']} shuffles, mean).",
        "",
    ]
    for hid in payload["real"]["metrics"]:
        acct = payload["real"]["accounting"][hid]
        lines += [
            f"## {hid}",
            "",
            f"Real accounting: {acct['in_scope_symbol_days']} symbol-days in scope, "
            f"{acct['dropped_ratio_regime']} ratio-regime dropped, {acct['condition_not_evaluable']} "
            f"not evaluable, {acct['dropped_custody_scale_jump']} custody-jump dropped, "
            f"{acct['evaluated']} evaluated, {acct['flagged']} flagged.",
            "",
            "| metric | real | independent (shuffle) | synthetic iid | synthetic dependent |",
            "|---|---|---|---|---|",
        ]
        real = payload["real"]["metrics"][hid]
        shuffle = payload["real"]["shuffle_reference"][hid]
        shuffle_map = {
            "dispersion_index_poibin": "dispersion_index_poibin",
            "flag_acf_lag1_mean": "flag_acf_lag1_mean",
            "count_acf_lag1": "count_acf_lag1",
            "count_acf_lag5": "count_acf_lag5",
            "deff_time_K10": "deff_time_K10",
            "deff_total_K10": "deff_total_K10",
        }
        for label, key in TABLE_ROWS:
            ref = shuffle.get(shuffle_map[key]) if key in shuffle_map else None
            ref_text = "" if ref is None else f"{_fmt(ref['mean'])} [{_fmt(ref['p025'])}..{_fmt(ref['p975'])}]"
            lines.append(
                f"| {label} | {_fmt(real.get(key))} | {ref_text} | "
                f"{_cell(payload['synthetic']['iid']['aggregate'][hid][key])} | "
                f"{_cell(payload['synthetic']['dependent']['aggregate'][hid][key])} |"
            )
        lines.append("")
    lines += ["## Caveats", ""] + [f"- {text}" for text in payload["caveats"]] + [""]
    return "\n".join(lines)


CAVEATS = (
    "Flags only: horizon-dependent event drops (insufficient bars, adjustment boundary) are not applied, "
    "and no return is read; whether flags correlate with returns (vol-dependence in the synthetic "
    "`dependent` world) is NOT measured here.",
    "Real panel is one realisation of 20 symbols x ~250 dates in a single market regime; the synthetic "
    "columns show seed-to-seed spread of the same estimator, the shuffle column shows the estimator's "
    "own noise under independence (including small-sample bias of autocorrelations).",
    "Synthetic flags come from a hand-built generator (`tests/synthetic_market.py`); resemblance of real "
    "numbers to one column is a screening judgement, not an equivalence test.",
    "Dispersion index uses each symbol's own in-sample frequency as p_i, which slightly understates "
    "dispersion in short series (order 1/T).",
    "Design effects use a Bartlett long-run variance with truncation K; K=10 is the headline, K=5 and "
    "K=20 are in the JSON. Persistence beyond K (e.g. a latent with phi=0.9) is not captured.",
    "Holdout symbols and test-window data were not read.",
)


# --------------------------------------------------------------------------- main

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data-root", type=Path, default=PROJECT_ROOT / "data" / "interim",
                   help="directory holding period_analysis/ and price_history/ (default: ./data/interim)")
    p.add_argument("--out", type=Path, default=None,
                   help="output JSON (refuses to overwrite); default <data-root>/research/"
                        "condition_clustering_<today>.json; a .md table is written next to it")
    p.add_argument("--synthetic-seeds", type=int, default=5)
    p.add_argument("--shuffles", type=int, default=200)
    p.add_argument("--shuffle-seed", type=int, default=20261014)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    today = datetime.now(timezone.utc).date().isoformat()
    out_json = args.out or args.data_root / "research" / f"condition_clustering_{today}.json"
    out_md = out_json.with_suffix(".md")
    for target in (out_json, out_md):
        if target.exists():
            print(f"refusing to overwrite {target}", file=sys.stderr)
            return 1

    specs = _load_specs()
    params = _run_parameters(specs["H1"])
    for hid, spec in specs.items():
        if _run_parameters(spec) != params:
            print(f"{hid} run parameters differ from H1", file=sys.stderr)
            return 1
    symbols, split_info = development_symbols()

    real_data, input_hashes = load_real_symbol_data(args.data_root, symbols, params)
    axis, matrices, accounting = condition_flag_panel(real_data, specs)
    sorted_symbols = sorted(real_data)
    real = {"axis": [axis[0].isoformat(), axis[-1].isoformat()], "n_dates": len(axis),
            "accounting": accounting, "metrics": {}, "per_symbol_frequency": {},
            "shuffle_reference": {}}
    for hid, matrix in matrices.items():
        real["metrics"][hid] = measure_matrix(matrix)
        real["shuffle_reference"][hid] = shuffle_reference(
            matrix, shuffles=args.shuffles, seed=args.shuffle_seed)
        real["per_symbol_frequency"][hid] = {
            sym: (sum(1 for v in row if v == 1) / n if (n := sum(1 for v in row if v is not None)) else None)
            for sym, row in zip(sorted_symbols, matrix)
        }
    print("real panel done", file=sys.stderr)

    synthetic = {}
    for variant in VARIANTS:
        per_seed = []
        for index in range(args.synthetic_seeds):
            label = f"condition_clustering:{variant}:{index}"
            world, sdata = synthetic_symbol_data(variant, label, params)
            _, smatrices, sacct = condition_flag_panel(sdata, specs)
            per_seed.append({
                "seed": label, "n_dates": len(world.dates), "accounting": sacct,
                "metrics": {hid: measure_matrix(m) for hid, m in smatrices.items()},
            })
            print(f"synthetic {variant} seed {index} done", file=sys.stderr)
        synthetic[variant] = {
            "seeds": per_seed,
            "aggregate": {hid: aggregate([s["metrics"][hid] for s in per_seed]) for hid in specs},
        }

    payload = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "code_commit": resolve_code_commit(PROJECT_ROOT),
        "python_version": platform.python_version(),
        "trial_log": "docs/prereg/TRIAL_LOG.md T-29",
        "window": [REAL_WINDOW[0].isoformat(), REAL_WINDOW[1].isoformat()],
        "symbols": sorted_symbols,
        **split_info,
        "spec_sha256": {hid: spec.spec_sha256 for hid, spec in specs.items()},
        "input_file_sha256": input_hashes,
        "parameters": {
            "synthetic_seeds": args.synthetic_seeds, "shuffles": args.shuffles,
            "shuffle_seed": args.shuffle_seed, "deff_windows": list(DEFF_WINDOWS),
            "headline_K": HEADLINE_K, "tail_k": TAIL_K,
            "synthetic_config": "tests/synthetic_market.MarketConfig defaults, NULL world",
        },
        "formulas": FORMULAS,
        "real": real,
        "synthetic": synthetic,
        "caveats": list(CAVEATS),
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    with out_md.open("x", encoding="utf-8") as handle:
        handle.write(render_markdown(payload))
    print(f"wrote {out_json} and {out_md}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
