"""Candidate inference methods (SYNTHETIC calibration only, TRIAL_LOG T-30).

Proves (1) the frozen path is unchanged: ``run_study`` output and the frozen
calibration replicate records hash to the digests computed at commit
``44fed3a`` (before the opt-in ``event_sink`` parameter and the candidates
existed), and ``event_sink`` never alters the result; (2) the candidate
code path reproduces the frozen E1 p exactly when given the frozen L = 5;
(3) the Driscoll-Kraay formula on hand-checked panels; (4) ``--method`` runs
and resumes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr

from fintr.research.candidate_inference import driscoll_kraay_test
from fintr.research.engine import EventOutcome, load_symbol_data
from fintr.research.report import run_study
from fintr.research.study_spec import BlockBootstrapConfig, BootstrapConfig

import synthetic_market as sm

ROOT = Path(__file__).resolve().parents[1]

# sha256 of the outputs below, computed at commit 44fed3a (frozen path before this change)
GOLDEN_REPLICATE_DIGEST = "db26bdd3917565b0604db01b7407e94e0721ae5465d2e15209ab7afd4cf7660c"
GOLDEN_RUN_STUDY_DIGEST = "16e3a1bdd93b332a8f82e87ff2784f323d4034e6cba013ec7e4b214b5831a839"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "calibrate_engine_candidates_under_test", ROOT / "scripts" / "calibrate_engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _strip_seconds(value):
    if isinstance(value, dict):
        return {k: _strip_seconds(v) for k, v in value.items() if k != "seconds"}
    if isinstance(value, list):
        return [_strip_seconds(v) for v in value]
    return value


def _task(variant, planted, candidates=None):
    task = {"scenario": variant, "variant": variant, "r": 3, "seed": 20261013, "planted": planted,
            "specs": ["H1", "H2"], "n_symbols": 6, "n_days": 80, "block_resamples": 199,
            "iid_resamples": 20}
    if candidates is not None:
        task["candidates"] = candidates
    return task


def _small_studies(cal, event_sinks=None):
    world = sm.generate_world(sm.MarketConfig(variant="dependent", n_symbols=6, n_days=80), "7:0")
    params = cal._run_params(cal._frozen_spec("H1"))
    data = {s: load_symbol_data(s, world.captures[s], world.prices[s],
                                adjustment_ratio_epsilon=params.adjustment_ratio_epsilon,
                                custody_scale_jump_ratio=params.custody_scale_jump_ratio)
            for s in world.symbols}
    outs = []
    for i, hid in enumerate(("H1", "H2")):
        frozen = cal._frozen_spec(hid)
        spec = replace(frozen, symbols=world.symbols, symbol_holdout=(), horizons_trading_days=(5,),
                       bootstrap=BootstrapConfig(11, 50, frozen.bootstrap.confidence_level),
                       block_bootstrap=BlockBootstrapConfig(5, 199, 11))
        sink = None if event_sinks is None else event_sinks[i]
        outs.append(run_study(spec, data, split="train", params=params, event_sink=sink))
    return outs


class FrozenPathUnchangedTests(unittest.TestCase):
    def test_run_study_output_matches_pre_change_digest_with_and_without_sink(self):
        cal = _load_script()
        self.assertEqual(_digest(_small_studies(cal)), GOLDEN_RUN_STUDY_DIGEST)
        sinks = [{}, {}]
        with_sink = _small_studies(cal, sinks)
        self.assertEqual(_digest(with_sink), GOLDEN_RUN_STUDY_DIGEST)
        for result, sink in zip(with_sink, sinks):
            conditioned, baseline = sink[5]
            kept = [e for e in conditioned if not e.dropped]
            self.assertEqual(len(kept), result["overall"]["5"]["event_count"])

    def test_frozen_replicates_match_pre_change_digest(self):
        cal = _load_script()
        recs = [_strip_seconds(cal.run_replicate(_task(v, p)))
                for v in ("iid", "dependent") for p in ({}, {"H1": -0.02, "H2": 0.02})]
        self.assertEqual(_digest(recs), GOLDEN_REPLICATE_DIGEST)

    def test_candidates_leave_frozen_fields_unchanged_and_mbb_l5_equals_frozen(self):
        cal = _load_script()
        cal.METHODS["mbb5_check"] = {"kind": "mbb", "block_length": 5, "definition": "test only"}
        plain = cal.run_replicate(_task("dependent", {}))
        rec = cal.run_replicate(_task("dependent", {}, ["mbb5_check", "mbb10", "dk10"]))
        self.assertEqual(_strip_seconds(plain["hypotheses"]), _strip_seconds(rec["hypotheses"]))
        for hid in ("H1", "H2"):
            same = rec["methods"]["mbb5_check"][hid]
            self.assertEqual(same["p_raw"], rec["hypotheses"][hid]["p_raw"])
            self.assertEqual(same["boot_se"], rec["hypotheses"][hid]["boot_se"])
            self.assertEqual(same["holm_rejected"], rec["hypotheses"][hid]["holm_rejected"])
            for name in ("mbb10", "dk10"):
                self.assertTrue(0.0 < rec["methods"][name][hid]["p_raw"] < 1.0)


def _event(day: int, value: str, conditioned: bool, symbol: str = "A") -> EventOutcome:
    d = date(2025, 1, 1) + timedelta(days=day)
    return EventOutcome(symbol, d, d, d, 5, "raw", Decimal(value), Decimal(value), False, None,
                        True if conditioned else None)


class DriscollKraayTests(unittest.TestCase):
    def _panel(self):
        base_vals = ["0.01", "-0.02", "0.03", "0.00", "-0.01", "0.02"]
        baseline = [_event(i, v, False) for i, v in enumerate(base_vals)]
        baseline += [_event(i, "0.005", False, "B") for i in range(6)]
        conditioned = [_event(0, "0.01", True), _event(2, "0.03", True), _event(3, "0.00", True)]
        return conditioned, baseline

    def _scores(self, conditioned, baseline):
        days = sorted({e.signal_date for e in baseline})
        cvals = [float(e.net_return) for e in conditioned]
        bvals = [float(e.net_return) for e in baseline]
        mc, mb = sum(cvals) / len(cvals), sum(bvals) / len(bvals)
        scores = []
        for d in days:
            c = [float(e.net_return) for e in conditioned if e.signal_date == d]
            b = [float(e.net_return) for e in baseline if e.signal_date == d]
            scores.append((sum(c) - len(c) * mc) / len(cvals) - (sum(b) - len(b) * mb) / len(bvals))
        return mc - mb, scores

    def test_variance_matches_hand_formula_and_p_is_one_sided(self):
        conditioned, baseline = self._panel()
        diff, u = self._scores(conditioned, baseline)
        self.assertAlmostEqual(sum(u), 0.0, places=15)
        for lags in (0, 1, 2):
            var = sum(x * x for x in u) + 2 * sum(
                (1 - k / (lags + 1)) * sum(u[i] * u[i + k] for i in range(len(u) - k))
                for k in range(1, lags + 1))
            neg = driscoll_kraay_test(conditioned, baseline, lags=lags, predicted_direction="negative")
            pos = driscoll_kraay_test(conditioned, baseline, lags=lags, predicted_direction="positive")
            self.assertEqual(neg.status, "computed")
            self.assertAlmostEqual(neg.observed_difference, diff, places=15)
            self.assertAlmostEqual(neg.standard_error, math.sqrt(var), places=15)
            z = diff / math.sqrt(var)
            self.assertAlmostEqual(neg.p_value_one_sided, 0.5 * math.erfc(-z / math.sqrt(2)), places=12)
            self.assertAlmostEqual(neg.p_value_one_sided + pos.p_value_one_sided, 1.0, places=12)

    def test_edge_statuses(self):
        conditioned, baseline = self._panel()
        self.assertEqual(driscoll_kraay_test([], baseline, lags=2, predicted_direction="negative").status,
                         "no_conditioned_events")
        self.assertEqual(driscoll_kraay_test(conditioned, baseline, lags=6,
                                             predicted_direction="negative").status, "too_few_signal_dates")
        self.assertIsNone(driscoll_kraay_test(conditioned, baseline, lags=1,
                                              predicted_direction=None).p_value_one_sided)


class MethodHarnessTests(unittest.TestCase):
    ARGV = ["--variants", "iid", "--replicates-null", "3", "--replicates-alt", "1",
            "--effect-sizes", "0.02", "--workers", "1", "--n-symbols", "6", "--n-days", "80",
            "--block-resamples", "99", "--method", "frozen", "mbb10", "dk20"]

    def test_method_run_summaries_and_resume(self):
        cal = _load_script()
        with tempfile.TemporaryDirectory() as tmp:
            full, resumed = Path(tmp) / "full", Path(tmp) / "resumed"
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cal.main(["--out", str(full), *self.ARGV]), 0)
            doc = json.loads((full / "calibration.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["config"]["methods"], ["frozen", "mbb10", "dk20"])
            null = doc["scenarios"]["iid/null"]
            self.assertEqual(set(null["method_summaries"]), {"frozen", "mbb10", "dk20"})
            self.assertEqual(null["method_summaries"]["frozen"], null["summary"])
            self.assertIn("familywise_any_holm_reject", null["method_summaries"]["dk20"])
            self.assertIn("Yontem karsilastirmasi", (full / "calibration.md").read_text(encoding="utf-8"))
            # interrupted after 2 replicates (header with methods, cut last line) -> same result
            records = sorted((rec for e in doc["scenarios"].values() for rec in e["replicates_detail"]),
                             key=lambda rec: (rec["r"], rec["scenario"]))
            header = {"_header": {"seed": 20261013, "n_symbols": 6, "n_days": 80, "specs": ["H1", "H2"],
                                  "block_resamples": 99, "iid_resamples": 20, "methods": ["mbb10", "dk20"]}}
            resumed.mkdir()
            (resumed / "replicates.partial.jsonl").write_text(
                "\n".join([json.dumps(header)] + [json.dumps(rec) for rec in records[:2]]) + "\n"
                + json.dumps(records[2])[:30], encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cal.main(["--out", str(resumed), "--resume", *self.ARGV]), 0)
            again = json.loads((resumed / "calibration.json").read_text(encoding="utf-8"))
            def strip(d):
                drop = ("mean_seconds_per_replicate_cpu", "seconds")
                def clean(value):
                    if isinstance(value, dict):
                        return {k: clean(v) for k, v in value.items() if k not in drop}
                    if isinstance(value, list):
                        return [clean(v) for v in value]
                    return value
                return clean(d["scenarios"])
            self.assertEqual(strip(again), strip(doc))

    def test_resume_refuses_other_method_set(self):
        cal = _load_script()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            out.mkdir()
            header = {"_header": {"seed": 20261013, "n_symbols": 6, "n_days": 80, "specs": ["H1", "H2"],
                                  "block_resamples": 99, "iid_resamples": 20, "methods": ["mbb20"]}}
            (out / "replicates.partial.jsonl").write_text(json.dumps(header) + "\n", encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cal.main(["--out", str(out), "--resume", *self.ARGV]), 2)


if __name__ == "__main__":
    unittest.main()
