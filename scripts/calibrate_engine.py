"""Calibrate the frozen study engine on SYNTHETIC data (false-positive rate, power).

Item A4 of ``docs/research/signal_bot_best_practices_2026-10-13.md``. For
every replicate a synthetic panel is generated (``tests/synthetic_market.py``:
20 symbols x 250 days, seeded random-walk prices, synthetic AKD/custody/
metadata built as in-memory ``PeriodCapture``/``PriceHistoryCapture``), the
REAL frozen H1/H2 specs are loaded unchanged from ``configs/studies/prereg``
and run through the real ``fintr.research.report.run_study`` (E1 moving-block
bootstrap, 5-day horizon, 10 bps/side cost, per-spec run parameters), and the
real ``fintr.research.family.evaluate_primary_family`` applies Holm (m = 2).
Under NULL no return depends on any condition flag; under ALT per-event
log-return shifts are planted (solved per panel) so the noise-free conditioned-minus-baseline mean difference is exactly -d for H1 (negative) and
+d for H2 (positive).

NEVER reads ``data/``, a real study output, or any real price/AKD file; no
network, no model call. Reports calibration statistics of the engine, not
statements about any market. See ``docs/research/engine_calibration.md``.

Documented deviations from the real run (also written to the JSON): only the
primary horizon (5) is built (the other horizons never enter the Holm input);
the secondary i.i.d. bootstrap (diagnostic, not used for any decision) runs
with ``--iid-resamples`` draws instead of 2000; the E1 block-bootstrap seed
is derived per replicate (the frozen seed is one fixed number) so replicates
are independent; symbols are ``SYN01``..; ``--block-resamples`` defaults to
the frozen 9999.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import synthetic_market as sm  # noqa: E402  (tests/synthetic_market.py, synthetic only)

from fintr.research.candidate_inference import driscoll_kraay_test  # noqa: E402
from fintr.research.engine import load_symbol_data, moving_block_bootstrap  # noqa: E402
from fintr.research.family import evaluate_primary_family, holm_decisions  # noqa: E402
from fintr.research.report import RunParameters, resolve_code_commit, run_study  # noqa: E402
from fintr.research.study_spec import (  # noqa: E402
    BlockBootstrapConfig,
    BootstrapConfig,
    load_study_spec,
)


SCHEMA = "fintr_engine_calibration_v1"
SPEC_DIR = PROJECT_ROOT / "configs" / "studies" / "prereg"
SPEC_FILES = {
    "H1": "h1_close_premium_vs_vwap_proxy_v1.json",
    "H2": "h2_close_discount_vs_vwap_proxy_v1.json",
    "H3": "h3_concentrated_flow_close_premium_v1.json",
    "H4": "h4_concentrated_flow_close_discount_v1.json",
}
PRIMARY = ("H1", "H2")
# Predicted direction of the frozen specs: H1/H3 negative, H2/H4 positive.
DIRECTION_SIGN = {"H1": -1.0, "H2": +1.0, "H3": -1.0, "H4": +1.0}
SIGMA_5_NOMINAL = 0.05  # generator design (docs/prereg/README.md assumption)
README_N_EFF = (50, 170)  # README ~250 days x 20 symbols: n_eff ~ 50-170
Z_975 = 1.959964
P_THRESHOLDS = (0.01, 0.025, 0.05, 0.10, 0.25, 0.50)
# Inference methods (docs/prereg/TRIAL_LOG.md T-30, candidate set fixed before any run).
# "frozen" is the real run_study E1 result and is always computed; the others are
# opt-in candidates evaluated on the SAME pooled horizon-5 events of the same replicate
# (fintr.research.candidate_inference), never part of the frozen test.
METHODS = {
    "frozen": {"kind": "frozen", "definition": "E1 moving block bootstrap by signal date as frozen "
                                               "(spec block_bootstrap: L=5, B=9999)"},
    "mbb10": {"kind": "mbb", "block_length": 10,
              "definition": "engine moving_block_bootstrap, L=10, B as frozen, same seed"},
    "mbb20": {"kind": "mbb", "block_length": 20,
              "definition": "engine moving_block_bootstrap, L=20, B as frozen, same seed"},
    "dk10": {"kind": "dk", "lags": 10,
             "definition": "Driscoll-Kraay (Bartlett HAC over signal-date scores), 10 lags, normal p"},
    "dk20": {"kind": "dk", "lags": 20,
             "definition": "Driscoll-Kraay (Bartlett HAC over signal-date scores), 20 lags, normal p"},
}


# --------------------------------------------------------------------------- stats

def _binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k), X ~ Binomial(n, p)."""
    if k <= 0:
        return 1.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    log_p, log_q = math.log(p), math.log1p(-p)
    total = 0.0
    for i in range(k, n + 1):
        total += math.exp(
            math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
            + i * log_p + (n - i) * log_q
        )
    return min(1.0, total)


def clopper_pearson(k: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    """Exact binomial confidence interval for k successes in n trials."""
    if n <= 0:
        return (float("nan"), float("nan"))
    alpha = 1.0 - conf
    lo = 0.0
    if k > 0:
        a, b = 0.0, k / n
        for _ in range(60):
            mid = (a + b) / 2
            if _binom_sf(k, n, mid) < alpha / 2:
                a = mid
            else:
                b = mid
        lo = (a + b) / 2
    hi = 1.0
    if k < n:
        a, b = k / n, 1.0
        for _ in range(60):
            mid = (a + b) / 2
            if (1.0 - _binom_sf(k + 1, n, mid)) > alpha / 2:
                a = mid
            else:
                b = mid
        hi = (a + b) / 2
    return lo, hi


def rate(k: int, n: int) -> dict:
    lo, hi = clopper_pearson(k, n)
    return {"k": k, "n": n, "rate": (k / n if n else None), "ci95_lo": lo, "ci95_hi": hi}


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def readme_power(effect: float, n_eff: float, sigma5: float = SIGMA_5_NOMINAL) -> float:
    """README power model: one-sided level 0.025 (z = 1.96), SE = sigma_5 / sqrt(n_eff)."""
    return _phi(abs(effect) * math.sqrt(n_eff) / sigma5 - Z_975)


# --------------------------------------------------------------------------- worker

def _seed_int(label: str) -> int:
    return int.from_bytes(hashlib.sha256(label.encode()).digest()[:8], "big") % (2 ** 31 - 1)


_SPEC_CACHE: dict = {}


def _frozen_spec(hid: str):
    if hid not in _SPEC_CACHE:
        _SPEC_CACHE[hid] = load_study_spec(SPEC_DIR / SPEC_FILES[hid])
    return _SPEC_CACHE[hid]


def _run_params(spec) -> RunParameters:
    doc = spec.raw_document["preregistration"]["run_parameters"]
    return RunParameters(
        cost_bps=Decimal(doc["cost_bps_per_side"]),
        volume_ratio_tolerance=Decimal(doc["volume_ratio_tolerance"]),
        adjustment_ratio_epsilon=Decimal(doc["adjustment_ratio_epsilon"]),
        custody_scale_jump_ratio=Decimal(doc["custody_scale_jump_ratio"]),
    )


def run_replicate(task: dict) -> dict:
    """One synthetic panel -> engine -> Holm. Pure function of ``task``."""
    started = time.perf_counter()
    cfg = sm.MarketConfig(
        variant=task["variant"], n_symbols=task["n_symbols"], n_days=task["n_days"],
    )
    seed = f"{task['seed']}:{task['r']}"
    world = sm.generate_world(cfg, seed, target_difference=task["planted"])
    spec_ids = task["specs"]
    base_spec = _frozen_spec(spec_ids[0])
    params = _run_params(base_spec)
    symbol_data = {
        sym: load_symbol_data(
            sym, world.captures[sym], world.prices[sym],
            adjustment_ratio_epsilon=params.adjustment_ratio_epsilon,
            custody_scale_jump_ratio=params.custody_scale_jump_ratio,
        )
        for sym in world.symbols
    }
    bb_seed = _seed_int(f"bb:{seed}")
    candidates = task.get("candidates") or []
    results, pooled = [], []
    for hid in spec_ids:
        frozen = _frozen_spec(hid)
        if frozen.train_test_cutoff <= world.dates[-1]:
            raise ValueError("synthetic dates must precede the frozen train/test cutoff")
        bb = frozen.block_bootstrap
        spec = replace(
            frozen, symbols=world.symbols, symbol_holdout=(), horizons_trading_days=(5,),
            bootstrap=BootstrapConfig(bb_seed, task["iid_resamples"], frozen.bootstrap.confidence_level),
            block_bootstrap=BlockBootstrapConfig(
                bb.block_length_signal_dates, task["block_resamples"] or bb.resamples, bb_seed,
            ),
        )
        sink = {} if candidates else None
        results.append(run_study(spec, symbol_data, split="train", params=_run_params(frozen),
                                 event_sink=sink))
        pooled.append(sink)
    family = evaluate_primary_family(results)
    holm = {row["hypothesis_id"]: row for row in family["primary"]}
    out_h = {}
    for hid, res in zip(spec_ids, results):
        test = res["hypothesis_test"]
        block = res["overall"]["5"]["block_bootstrap_difference"]
        p05, p95 = block["percentile_05"], block["percentile_95"]
        out_h[hid] = {
            "p_raw": test["p_value_one_sided"],
            "p_for_holm": test["p_value_for_holm"],
            "min_events_met": test["min_events_met"],
            "event_count": test["event_count"],
            "distinct_conditioned_dates": test["distinct_conditioned_signal_dates"],
            "difference_mean": None if test["difference_mean"] is None else float(test["difference_mean"]),
            "boot_se": None if p05 is None or p95 is None else (p95 - p05) / (2 * 1.644854),
            "undefined_resamples": block["undefined_resamples"],
            "holm_rejected": holm[hid]["holm"]["rejected"] if hid in holm else None,
            "truth_event_count": world.event_counts[hid],
            "oracle_difference": world.oracle_difference[hid],
            "frequency": world.frequencies[hid],
        }
    record = {
        "scenario": task["scenario"], "r": task["r"], "seed": seed, "block_bootstrap_seed": bb_seed,
        "hypotheses": out_h, "per_event_shift": world.planted,
    }
    if candidates:
        record["methods"] = {
            name: candidate_results(name, spec_ids, pooled, out_h, bb_seed,
                                    task["block_resamples"] or _frozen_spec("H1").block_bootstrap.resamples)
            for name in candidates
        }
    record["seconds"] = time.perf_counter() - started
    return record


def candidate_results(name: str, spec_ids, pooled, frozen_rows: dict, seed: int, resamples: int) -> dict:
    """Per-hypothesis p, SE and Holm decision of candidate ``name`` on the pooled
    horizon-5 events the frozen run used. The minimum-event rule and Holm (m = 2,
    the spec's familywise alpha, real ``holm_decisions``) are applied exactly as
    for the frozen test; only the p-value/SE method differs."""
    method = METHODS[name]
    rows: dict[str, dict] = {}
    for hid, sink in zip(spec_ids, pooled):
        conditioned, baseline = sink[5]
        direction = _frozen_spec(hid).preregistration.predicted_direction
        if method["kind"] == "mbb":
            res = moving_block_bootstrap(
                conditioned, baseline, predicted_direction=direction,
                config=BlockBootstrapConfig(method["block_length"], resamples, seed))
            p05, p95 = res.percentile_05, res.percentile_95
            p, se, undefined = res.p_value_one_sided, (
                None if p05 is None or p95 is None else (p95 - p05) / (2 * 1.644854)), res.undefined_resamples
        elif method["kind"] == "dk":
            res = driscoll_kraay_test(conditioned, baseline, lags=method["lags"],
                                      predicted_direction=direction)
            p, se, undefined = res.p_value_one_sided, res.standard_error, 0
        else:
            raise ValueError(f"not a candidate method: {name}")
        met = frozen_rows[hid]["min_events_met"]
        rows[hid] = {"p_raw": p, "p_for_holm": (p if met and p is not None else 1.0),
                     "boot_se": se, "undefined_resamples": undefined, "status": res.status}
    primary = [h for h in spec_ids if h in PRIMARY]
    alpha = _frozen_spec(primary[0]).preregistration.familywise_alpha
    decisions = holm_decisions({h: rows[h]["p_for_holm"] for h in primary}, alpha, order=primary)
    for hid in spec_ids:
        rows[hid]["holm_rejected"] = decisions[hid]["rejected"] if hid in decisions else None
    return rows


def method_records(records: list[dict], name: str) -> list[dict]:
    """Records whose per-hypothesis rows carry method ``name``'s p/SE/Holm values
    (event counts, truth and observed difference are shared with the frozen run)."""
    if name == "frozen":
        return records
    return [
        {**rec, "hypotheses": {hid: {**row, **rec["methods"][name][hid]}
                               for hid, row in rec["hypotheses"].items()}}
        for rec in records
    ]


# --------------------------------------------------------------------------- summaries

def _mean(values):
    values = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return statistics.fmean(values) if values else None


def summarize(records: list[dict], spec_ids: tuple[str, ...], alt: bool) -> dict:
    n = len(records)
    summary: dict = {"n_replicates": n, "hypotheses": {}}
    primary = [h for h in spec_ids if h in PRIMARY]
    for hid in spec_ids:
        rows = [rec["hypotheses"][hid] for rec in records]
        ps = [row["p_raw"] for row in rows]
        block = {
            "min_events_met": rate(sum(1 for row in rows if row["min_events_met"]), n),
            "raw_p_le_0.025": rate(sum(1 for p in ps if p is not None and p <= 0.025), n),
            "raw_p_le_0.05": rate(sum(1 for p in ps if p is not None and p <= 0.05), n),
            "p_cdf": {str(a): rate(sum(1 for p in ps if p is not None and p <= a), n)
                      for a in P_THRESHOLDS},
            "mean_event_count": _mean([row["event_count"] for row in rows]),
            "mean_truth_event_count": _mean([row["truth_event_count"] for row in rows]),
            "event_count_matches_truth": all(
                row["event_count"] == row["truth_event_count"] for row in rows),
            "mean_condition_frequency": _mean([row["frequency"] for row in rows]),
            "mean_observed_difference": _mean([row["difference_mean"] for row in rows]),
            "sd_observed_difference": (
                statistics.stdev([row["difference_mean"] for row in rows])
                if n > 1 and all(row["difference_mean"] is not None for row in rows) else None),
            "mean_bootstrap_se": _mean([row["boot_se"] for row in rows]),
            "mean_oracle_difference": _mean([row["oracle_difference"] for row in rows]),
        }
        if hid in primary:
            block["holm_reject"] = rate(sum(1 for row in rows if row["holm_rejected"]), n)
        sd, se = block["sd_observed_difference"], block["mean_bootstrap_se"]
        block["bootstrap_se_over_empirical_sd"] = (se / sd) if sd and se else None
        block["implied_n_eff_from_bootstrap_se"] = (
            (SIGMA_5_NOMINAL / se) ** 2 if se else None)
        summary["hypotheses"][hid] = block
    if primary:
        any_reject = sum(1 for rec in records
                         if any(rec["hypotheses"][h]["holm_rejected"] for h in primary))
        summary["familywise_any_holm_reject"] = rate(any_reject, n)
        summary["both_holm_reject"] = rate(sum(
            1 for rec in records if all(rec["hypotheses"][h]["holm_rejected"] for h in primary)), n)
    summary["mean_seconds_per_replicate_cpu"] = _mean([rec["seconds"] for rec in records])
    return summary


def power_vs_readme(summary: dict, hid: str, planted_abs: float) -> dict:
    block = summary["hypotheses"][hid]
    oracle = block["mean_oracle_difference"]
    effect = abs(oracle) if oracle is not None else planted_abs
    implied = block["implied_n_eff_from_bootstrap_se"]
    return {
        "planted_forward_log_shift": planted_abs,
        "mean_oracle_difference_abs": effect,
        "empirical_power_holm": block.get("holm_reject"),
        "empirical_power_raw_p_le_0.025": block["raw_p_le_0.025"],
        "readme_model_power_at_n_eff": {
            **{str(n_eff): readme_power(effect, n_eff) for n_eff in README_N_EFF},
            **({"bootstrap_implied_%.0f" % implied: readme_power(effect, implied)} if implied else {}),
        },
    }


# --------------------------------------------------------------------------- reporting

def _fmt_rate(r: dict) -> str:
    if r["rate"] is None:
        return "n/a"
    return f"{100 * r['rate']:.1f}% [{100 * r['ci95_lo']:.1f}, {100 * r['ci95_hi']:.1f}] ({r['k']}/{r['n']})"


def render_markdown(doc: dict) -> str:
    lines = [
        "# Engine calibration (SYNTHETIC data only)", "",
        "Bu rapor sentetik veriyle motorun kalibrasyonunu olcer; piyasalar hakkinda bir sey soylemez.",
        "",
        f"- sema: `{doc['schema']}`, commit: `{doc['code_commit']}`, olusturma (UTC): {doc['created_utc']}",
        f"- panel: {doc['config']['n_symbols']} sembol x {doc['config']['n_days']} gun, "
        f"engine: E1 blok bootstrap L={doc['config']['block_length']}, "
        f"B={doc['config']['block_resamples']}, vade 5, maliyet {doc['config']['cost_bps_per_side']} bps/taraf, "
        f"Holm m=2 alpha=0.05",
        f"- toplam sure: {doc['timing']['wall_seconds']:.0f} s ({doc['timing']['workers']} isci), "
        f"toplam CPU: {doc['timing']['cpu_seconds']:.0f} s",
        "", "Sapmalar: " + "; ".join(doc["deviations"]), "",
    ]
    if any("method_summaries" in entry for entry in doc["scenarios"].values()):
        lines += ["## Yontem karsilastirmasi (ayni olaylar; adaylar on kayitli test DEGILDIR)", "",
                  "| senaryo | yontem | H1 Holm red | H2 Holm red | aile (herhangi red) | "
                  "SE/SD H1 | SE/SD H2 |", "|---|---|---|---|---|---|---|"]
        for scenario, entry in doc["scenarios"].items():
            for name, s in entry.get("method_summaries", {}).items():
                ratios = [s["hypotheses"][h]["bootstrap_se_over_empirical_sd"] for h in PRIMARY]
                ratio_txt = ["n/a" if r is None or entry["planted_abs_shift"] else f"{r:.2f}" for r in ratios]
                lines.append(
                    f"| {scenario} | {name} | {_fmt_rate(s['hypotheses']['H1']['holm_reject'])} | "
                    f"{_fmt_rate(s['hypotheses']['H2']['holm_reject'])} | "
                    f"{_fmt_rate(s['familywise_any_holm_reject'])} | {ratio_txt[0]} | {ratio_txt[1]} |")
        lines += ["", "Yontem tanimlari: " + "; ".join(
            f"{name}: {d['definition']}" for name, d in doc["config"]["method_definitions"].items()), ""]
    for scenario, entry in doc["scenarios"].items():
        s = entry["summary"]
        lines += [f"## {scenario} (R={s['n_replicates']})", ""]
        if entry["planted_abs_shift"]:
            lines += [f"Hedef etki (gurultusuz kosullu-baseline fark): H1 -{entry['planted_abs_shift']}, H2 +{entry['planted_abs_shift']} "
                      "(ileri 5 gun log-getiri kaymasi).", ""]
        lines += ["| hipotez | min olay saglandi | ham p<=0.025 | ham p<=0.05 | Holm red | "
                  "ort. olay (truth) | SE_boot/SD_gozlem |", "|---|---|---|---|---|---|---|"]
        for hid, b in s["hypotheses"].items():
            ratio = b["bootstrap_se_over_empirical_sd"]
            lines.append(
                f"| {hid} | {_fmt_rate(b['min_events_met'])} | {_fmt_rate(b['raw_p_le_0.025'])} | "
                f"{_fmt_rate(b['raw_p_le_0.05'])} | "
                f"{_fmt_rate(b['holm_reject']) if 'holm_reject' in b else '-'} | "
                f"{b['mean_event_count']:.0f} ({b['mean_truth_event_count']:.0f}) | "
                f"{'n/a' if ratio is None or entry['planted_abs_shift'] else f'{ratio:.2f}'} |")
        if "familywise_any_holm_reject" in s:
            lines += ["", f"- Ailece (herhangi bir Holm reddi): {_fmt_rate(s['familywise_any_holm_reject'])}"]
        if entry.get("power_vs_readme"):
            lines += ["", "Guc (Holm red orani) ve README guc modeli (tek yonlu 0.025, "
                          "SE = sigma_5/sqrt(n_eff), sigma_5=%.2f):" % SIGMA_5_NOMINAL, "",
                      "| hipotez | oracle mutlak etki | deneysel guc (Holm) | ham p<=0.025 | README modeli |",
                      "|---|---|---|---|---|"]
            for hid, row in entry["power_vs_readme"].items():
                model = ", ".join(f"n_eff={k.replace('bootstrap_implied_', '~')}: {100 * v:.0f}%"
                                  for k, v in row["readme_model_power_at_n_eff"].items())
                lines.append(
                    f"| {hid} | {row['mean_oracle_difference_abs']:.4f} | "
                    f"{_fmt_rate(row['empirical_power_holm'])} | "
                    f"{_fmt_rate(row['empirical_power_raw_p_le_0.025'])} | {model} |")
        lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- main

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", type=Path, required=True,
                   help="output directory (must not exist or be empty; never overwritten)")
    p.add_argument("--variants", nargs="+", choices=sm.VARIANTS, default=list(sm.VARIANTS))
    p.add_argument("--replicates-null", type=int, default=1000)
    p.add_argument("--replicates-alt", type=int, default=300)
    p.add_argument("--effect-sizes", type=float, nargs="*", default=[0.01, 0.02, 0.03],
                   help="planted |forward log-return shift| (H1 negative, H2 positive)")
    p.add_argument("--specs", nargs="+", choices=sorted(SPEC_FILES), default=list(PRIMARY),
                   help="H3/H4 are descriptive (outside Holm); H1 and H2 must stay first")
    p.add_argument("--seed", type=int, default=20261013)
    p.add_argument("--workers", type=int, default=max(1, min(12, (os.cpu_count() or 2) - 2)),
                   help="worker processes (default: up to 12, leaving 2 cores free; the run is "
                        "CPU-bound for hours, so on a laptop pick fewer, e.g. --workers 6)")
    p.add_argument("--resume", action="store_true",
                   help="continue an interrupted run: read replicates.partial.jsonl in --out, "
                        "check its seeds/config, skip finished (scenario, r) pairs, append the "
                        "rest, then write the same outputs as an uninterrupted run. Pass the "
                        "same options as the original command")
    p.add_argument("--n-symbols", type=int, default=20)
    p.add_argument("--n-days", type=int, default=250)
    p.add_argument("--block-resamples", type=int, default=None,
                   help="override the frozen E1 resamples (default: the frozen 9999)")
    p.add_argument("--iid-resamples", type=int, default=20,
                   help="draws of the secondary i.i.d. diagnostic bootstrap (frozen: 2000; not used for decisions)")
    p.add_argument("--method", nargs="+", choices=list(METHODS), default=["frozen"],
                   help="inference method(s) to calibrate (default: frozen only). The frozen E1 "
                        "result is always computed; candidates (TRIAL_LOG T-30) are evaluated on "
                        "the same events of the same replicate, so several in one run cost one "
                        "event build. Candidates are NOT the pre-registered test")
    return p


def candidate_names(methods: list[str]) -> list[str]:
    """Candidates in registry order, without the always-computed frozen method."""
    return [name for name in METHODS if name != "frozen" and name in methods]


PARTIAL_NAME = "replicates.partial.jsonl"


class ResumeError(ValueError):
    """The partial file does not belong to this configuration."""


def load_partial(path: Path, expected_reps: dict[str, int], fingerprint: dict) -> tuple[list[dict], bool]:
    """Validated records of an interrupted run, and whether the file needs a
    rewrite (truncated last line or missing final newline).

    Every record must belong to a planned (scenario, r), carry exactly the
    replicate seed and block-bootstrap seed this configuration derives, hold
    results for the same hypotheses, and appear once. A header line (written by
    new runs) must equal ``fingerprint``; a headerless file (first version)
    can only be checked through the seeds.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    rewrite = not text.endswith("\n")
    if lines and lines[-1] == "":
        lines.pop()
    records: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for index, line in enumerate(lines):
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:  # killed mid-write
                rewrite = True
                break
            raise ResumeError(f"unreadable line {index + 1} in {path.name}") from None
        if "_header" in doc:
            if index != 0 or doc["_header"] != fingerprint:
                raise ResumeError("partial file header differs from the current options")
            continue
        scenario, r = doc.get("scenario"), doc.get("r")
        if scenario not in expected_reps or not isinstance(r, int) or not 0 <= r < expected_reps[scenario]:
            raise ResumeError(f"record outside the planned scenarios/replicates: {scenario!r} r={r!r}")
        seed = f"{fingerprint['seed']}:{r}"
        if doc.get("seed") != seed or doc.get("block_bootstrap_seed") != _seed_int(f"bb:{seed}"):
            raise ResumeError(f"seed mismatch at {scenario} r={r}")
        if set(doc.get("hypotheses", {})) != set(fingerprint["specs"]):
            raise ResumeError(f"hypothesis set mismatch at {scenario} r={r}")
        if set(doc.get("methods", {})) != set(fingerprint.get("methods", [])):
            raise ResumeError(f"method set mismatch at {scenario} r={r}")
        if (scenario, r) in seen:
            raise ResumeError(f"duplicate record {scenario} r={r}")
        seen.add((scenario, r))
        records.append(doc)
    return records, rewrite


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if tuple(args.specs[:2]) != PRIMARY:
        print("--specs must start with H1 H2 (the Holm family)", file=sys.stderr)
        return 2
    out = args.out
    partial = out / PARTIAL_NAME  # crash safety; removed after the final write
    if args.resume:
        if not partial.is_file() or (out / "calibration.json").exists():
            print(f"--resume needs {partial} and no finished calibration.json in {out}",
                  file=sys.stderr)
            return 2
    elif out.exists() and any(out.iterdir()):
        print(f"refusing to overwrite: {out} exists and is not empty", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)

    candidates = candidate_names(args.method)
    scenarios: dict[str, dict] = {}
    tasks: list[dict] = []
    for variant in args.variants:
        plans = [("null", 0.0, args.replicates_null)] + [
            (f"alt_{d:g}", d, args.replicates_alt) for d in args.effect_sizes]
        for label, d, reps in plans:
            name = f"{variant}/{label}"
            planted = {} if d == 0 else {h: DIRECTION_SIGN[h] * d for h in PRIMARY}
            scenarios[name] = {"variant": variant, "planted_abs_shift": d, "planted": planted,
                               "replicates": reps}
            tasks += [{
                "scenario": name, "variant": variant, "r": r, "seed": args.seed, "planted": planted,
                "specs": list(args.specs), "n_symbols": args.n_symbols, "n_days": args.n_days,
                "block_resamples": args.block_resamples, "iid_resamples": args.iid_resamples,
                "candidates": candidates,
            } for r in range(reps)]
    order = {name: i for i, name in enumerate(scenarios)}
    # round-robin over scenarios so an interrupted run leaves balanced replicate counts
    tasks.sort(key=lambda task: (task["r"], order[task["scenario"]]))
    fingerprint = {
        "seed": args.seed, "n_symbols": args.n_symbols, "n_days": args.n_days,
        "specs": list(args.specs), "block_resamples": args.block_resamples,
        "iid_resamples": args.iid_resamples,
    }
    if candidates:  # absent for frozen-only runs: old partial files stay resumable
        fingerprint["methods"] = candidates
    total = len(tasks)
    records: list[dict] = []
    if args.resume:
        try:
            records, rewrite = load_partial(
                partial, {n: e["replicates"] for n, e in scenarios.items()}, fingerprint)
        except ResumeError as exc:
            print(f"cannot resume: {exc}", file=sys.stderr)
            return 2
        if rewrite:  # keep only validated lines (atomic replace)
            tmp = partial.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(rec, default=str) + "\n" for rec in records),
                           encoding="utf-8")
            os.replace(tmp, partial)
        done = {(rec["scenario"], rec["r"]) for rec in records}
        tasks = [task for task in tasks if (task["scenario"], task["r"]) not in done]
        print(f"resuming: {len(records)} replicates found, {len(tasks)} to run", file=sys.stderr)
    prior = len(records)
    started = time.perf_counter()
    with partial.open("a" if args.resume else "w", encoding="utf-8") as sink:
        if not args.resume:
            sink.write(json.dumps({"_header": fingerprint}) + "\n")

        def note(rec: dict) -> None:
            records.append(rec)
            sink.write(json.dumps(rec, default=str) + "\n")
            sink.flush()
            if len(records) % 25 == 0:
                print(f"{len(records)}/{total} done ({time.perf_counter() - started:.0f}s)",
                      file=sys.stderr)

        if args.workers <= 1 or not tasks:
            for task in tasks:
                note(run_replicate(task))
        else:
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                for rec in pool.map(run_replicate, tasks, chunksize=2):
                    note(rec)
    wall = time.perf_counter() - started

    spec_ids = tuple(args.specs)
    by_scenario: dict[str, list[dict]] = {name: [] for name in scenarios}
    for rec in records:
        by_scenario[rec["scenario"]].append(rec)
    for name, entry in scenarios.items():
        recs = sorted(by_scenario[name], key=lambda rec: rec["r"])
        entry["summary"] = summarize(recs, spec_ids, alt=bool(entry["planted_abs_shift"]))
        if entry["planted_abs_shift"]:
            entry["power_vs_readme"] = {
                hid: power_vs_readme(entry["summary"], hid, entry["planted_abs_shift"])
                for hid in PRIMARY}
        if candidates:
            entry["method_summaries"] = {
                name: summarize(method_records(recs, name), spec_ids, alt=bool(entry["planted_abs_shift"]))
                for name in ["frozen", *candidates]}
        entry["replicates_detail"] = recs

    spec_info = {}
    for hid in spec_ids:
        spec = _frozen_spec(hid)
        spec_info[hid] = {
            "file": f"configs/studies/prereg/{SPEC_FILES[hid]}",
            "raw_sha256": hashlib.sha256((SPEC_DIR / SPEC_FILES[hid]).read_bytes()).hexdigest(),
            "canonical_sha256": spec.spec_sha256,
            "frozen_block_bootstrap": {
                "block_length_signal_dates": spec.block_bootstrap.block_length_signal_dates,
                "resamples": spec.block_bootstrap.resamples, "seed": spec.block_bootstrap.seed},
        }
    first = _frozen_spec(spec_ids[0])
    doc = {
        "schema": SCHEMA,
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "code_commit": resolve_code_commit(PROJECT_ROOT),
        "synthetic_only": True,
        "argv": sys.argv[1:] if argv is None else list(argv),
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "cpu_count": os.cpu_count()},
        "config": {
            "n_symbols": args.n_symbols, "n_days": args.n_days, "seed": args.seed,
            "variants": args.variants, "specs": list(spec_ids),
            "replicates_null": args.replicates_null, "replicates_alt": args.replicates_alt,
            "effect_sizes": args.effect_sizes, "horizon": 5,
            "cost_bps_per_side": _run_params(first).cost_bps.__str__(),
            "block_length": first.block_bootstrap.block_length_signal_dates,
            "block_resamples": args.block_resamples or first.block_bootstrap.resamples,
            "iid_resamples": args.iid_resamples,
            "methods": ["frozen", *candidates],
            "method_definitions": {name: METHODS[name] for name in ["frozen", *candidates]},
            "market": {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                       for k, v in sm.MarketConfig().__dict__.items()},
            "seed_rule": "replicate seed = f'{seed}:{r}' (shared by null and alt: common random "
                         "numbers); block-bootstrap seed = sha256('bb:'+seed)[:8] mod 2**31-1",
        },
        "specs": spec_info,
        "deviations": [
            "only horizon 5 built (primary horizon; other horizons never enter the Holm input)",
            f"secondary i.i.d. bootstrap diagnostic uses {args.iid_resamples} draws (frozen 2000; not used for decisions)",
            "E1 block-bootstrap seed derived per replicate (frozen seed is one fixed number)",
            "symbols replaced by SYN01.. and symbol_holdout cleared; split=train; frozen spec files untouched",
        ] + ([f"E1 resamples overridden to {args.block_resamples}"] if args.block_resamples else [])
          + ([f"candidate inference methods {candidates} (TRIAL_LOG T-30) evaluated on the same pooled "
              "horizon-5 events; they are NOT the pre-registered test"] if candidates else []),
        "timing": {"wall_seconds": wall, "workers": args.workers,
                   "cpu_seconds": sum(rec["seconds"] for rec in records),
                   "resumed_from_replicates": prior if args.resume else 0,
                   "note": "wall_seconds covers this session only when resumed"},
        "scenarios": scenarios,
    }
    (out / "calibration.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    (out / "calibration.md").write_text(render_markdown(doc), encoding="utf-8")
    partial.unlink(missing_ok=True)
    print(f"written: {out / 'calibration.json'} ({wall:.0f}s wall)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
