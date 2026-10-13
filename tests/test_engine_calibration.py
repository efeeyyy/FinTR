"""Tests for the synthetic engine-calibration harness (A4).

Fast tests (default suite, a few seconds): generator determinism, null/alt
shape, agreement of the generator's truth flags with the REAL engine's own
condition evaluation, statistics helpers, and a tiny end-to-end smoke run of
``scripts/calibrate_engine.py``. The multi-minute calibration assertion
(false-positive rate close to nominal) is opt-in: ``FINTR_SLOW_TESTS=1``.

All data is synthetic; nothing reads ``data/`` or a real study output.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

from fintr.research.engine import build_events, load_symbol_data
from fintr.research.study_spec import load_study_spec

import synthetic_market as sm


ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = ROOT / "configs" / "studies" / "prereg"
SLOW_TESTS = os.environ.get("FINTR_SLOW_TESTS") == "1"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "calibrate_engine_under_test", ROOT / "scripts" / "calibrate_engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _forward_returns(world: sm.SyntheticWorld, symbol: str) -> list[float]:
    """Engine-style gross forward return per signal day: close[t+6] / open[t+1] - 1."""
    bars = world.prices[symbol].bars
    return [
        float(bars[t + 1 + sm.HORIZON].close / bars[t + 1].open) - 1.0
        for t in sm.eligible_signal_indices(len(bars))
    ]


def _cond_minus_base(world: sm.SyntheticWorld, hid: str) -> float:
    cond, base = [], []
    for sym in world.symbols:
        rets = _forward_returns(world, sym)
        flags = world.flags[hid][sym]
        base += rets
        cond += [r for t, r in enumerate(rets) if flags[t]]
    return sum(cond) / len(cond) - sum(base) / len(base)


class GeneratorTests(unittest.TestCase):
    def test_same_seed_is_identical_and_other_seed_differs(self):
        for variant in sm.VARIANTS:
            cfg = sm.MarketConfig(variant=variant, n_symbols=3, n_days=60)
            a = sm.generate_world(cfg, "s1")
            b = sm.generate_world(cfg, "s1")
            c = sm.generate_world(cfg, "s2")
            self.assertEqual(a.captures, b.captures)
            self.assertEqual(a.prices, b.prices)
            self.assertEqual(a.flags, b.flags)
            self.assertNotEqual(a.prices, c.prices)

    def test_shape_and_schema_objects(self):
        cfg = sm.MarketConfig(n_symbols=4, n_days=50)
        world = sm.generate_world(cfg, 7)
        self.assertEqual(world.symbols, ("SYN01", "SYN02", "SYN03", "SYN04"))
        self.assertTrue(all(d.weekday() < 5 for d in world.dates))
        self.assertEqual(len(set(world.dates)), 50)
        for sym in world.symbols:
            cap, price = world.captures[sym], world.prices[sym]
            self.assertEqual(cap.server_dates, world.dates)
            self.assertEqual(len(price.bars), 50)
            self.assertEqual(len(cap.daily_akd_net), 50)
            self.assertEqual(len(cap.daily_akd_market), 50)
            self.assertEqual(len(cap.daily_custody_lots), 50)
            self.assertEqual(len(cap.metadata), 50)
            self.assertTrue(all(bar.volume and bar.close > 0 and bar.open > 0 for bar in price.bars))
            for net, mkt in zip(cap.daily_akd_net, cap.daily_akd_market):
                self.assertEqual([r.code for r in net.rows], [r.code for r in mkt.rows])
                self.assertTrue(all(abs(n.value) <= m.total_size for n, m in zip(net.rows, mkt.rows)))

    def test_condition_frequencies_are_realistic_for_dev_scale(self):
        for variant in sm.VARIANTS:
            world = sm.generate_world(sm.MarketConfig(variant=variant), 11)
            self.assertTrue(0.04 <= world.frequencies["H1"] <= 0.16, (variant, world.frequencies))
            self.assertTrue(0.04 <= world.frequencies["H2"] <= 0.16, (variant, world.frequencies))
            for hid in ("H3", "H4"):  # rarer, as in the frozen design
                self.assertTrue(0.003 <= world.frequencies[hid] <= 0.05, (variant, world.frequencies))
            self.assertGreaterEqual(world.event_counts["H1"], 100)
            self.assertGreaterEqual(world.distinct_dates["H1"], 30)

    def test_flags_match_the_real_engine_event_sets(self):
        """The generator's truth flags are exactly what the frozen conditions
        evaluate to (real build_events over the synthetic captures)."""
        world = sm.generate_world(sm.MarketConfig(n_symbols=3, n_days=70), 5)
        files = {
            "H1": "h1_close_premium_vs_vwap_proxy_v1.json", "H2": "h2_close_discount_vs_vwap_proxy_v1.json",
            "H3": "h3_concentrated_flow_close_premium_v1.json", "H4": "h4_concentrated_flow_close_discount_v1.json",
        }
        for sym in world.symbols:
            data = load_symbol_data(
                sym, world.captures[sym], world.prices[sym],
                adjustment_ratio_epsilon=Decimal("0.0005"), custody_scale_jump_ratio=Decimal(3))
            self.assertEqual(data.ratio_regimes, ())
            self.assertEqual(data.adjustment_boundary_dates, frozenset())
            for hid, name in files.items():
                spec = load_study_spec(SPEC_DIR / name)
                conditioned, baseline = build_events(
                    data, spec, horizon=5, cost_bps=Decimal(10), signal_dates=world.dates)
                kept = {e.signal_date for e in conditioned if not e.dropped}
                expected = {world.dates[t] for t in sm.eligible_signal_indices(70)
                            if world.flags[hid][sym][t]}
                self.assertEqual(kept, expected, (sym, hid))
                self.assertEqual(sum(1 for e in baseline if not e.dropped), 70 - (sm.HORIZON + 1))

    def test_null_forward_return_is_independent_of_every_flag(self):
        for variant in sm.VARIANTS:
            world = sm.generate_world(sm.MarketConfig(variant=variant), 3)
            self.assertTrue(all(v == 0 for v in world.oracle_difference.values()))
            for hid in ("H1", "H2"):
                cond, base = [], []
                for sym in world.symbols:
                    rets = _forward_returns(world, sym)
                    flags = world.flags[hid][sym]
                    cond += [r for t, r in enumerate(rets) if flags[t]]
                    base += [r for t, r in enumerate(rets) if not flags[t]]
                mc, mb = sum(cond) / len(cond), sum(base) / len(base)
                var = lambda xs, m: sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
                se = math.sqrt(var(cond, mc) / len(cond) + var(base, mb) / len(base))
                # i.i.d. SE ignores the cross-symbol correlation (understates the
                # spread), so allow a generous 6 SE; a leak of a 1-2% effect fails.
                self.assertLess(abs(mc - mb), 6 * se, (variant, hid, mc - mb, se))

    def test_planted_difference_is_solved_exactly_and_appears_in_returns(self):
        cfg = sm.MarketConfig(variant="dependent")
        null = sm.generate_world(cfg, 21)
        alt = sm.generate_world(cfg, 21, target_difference={"H1": -0.02, "H2": 0.02})
        self.assertAlmostEqual(alt.oracle_difference["H1"], -0.02, places=9)
        self.assertAlmostEqual(alt.oracle_difference["H2"], 0.02, places=9)
        # common random numbers: conditions (flags) unchanged by the shift
        self.assertEqual(null.flags["H1"], alt.flags["H1"])
        self.assertEqual(null.flags["H2"], alt.flags["H2"])
        # realized change in the engine's own gross-return difference is the target
        # (log vs simple return and rare rounding flips: 20% tolerance)
        for hid, target in (("H1", -0.02), ("H2", 0.02)):
            delta = _cond_minus_base(alt, hid) - _cond_minus_base(null, hid)
            self.assertLess(abs(delta - target), 0.2 * abs(target), (hid, delta))

    def test_planted_and_target_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            sm.generate_world(sm.MarketConfig(n_symbols=2, n_days=40), 1,
                              planted={"H1": 0.01}, target_difference={"H1": 0.01})

    def test_generator_does_not_touch_data_dir(self):
        text = (ROOT / "tests" / "synthetic_market.py").read_text(encoding="utf-8")
        for needle in ("data/interim", "load_period_capture", "load_price_history_capture", "open("):
            self.assertNotIn(needle, text)


class StatisticsTests(unittest.TestCase):
    def test_clopper_pearson_known_values(self):
        cal = _load_script()
        lo, hi = cal.clopper_pearson(5, 10)
        self.assertAlmostEqual(lo, 0.1871, places=3)
        self.assertAlmostEqual(hi, 0.8129, places=3)
        lo, hi = cal.clopper_pearson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.3085, places=3)
        lo, hi = cal.clopper_pearson(10, 10)
        self.assertEqual(hi, 1.0)
        self.assertAlmostEqual(lo, 0.6915, places=3)

    def test_readme_power_model(self):
        cal = _load_script()
        # README MDE: effect = 2.80 * sigma_5 / sqrt(n_eff) gives power ~ 0.80
        mde = 2.80 * 0.05 / math.sqrt(100)
        self.assertAlmostEqual(cal.readme_power(mde, 100), 0.80, delta=0.01)


class CalibrationSmokeTests(unittest.TestCase):
    def test_tiny_end_to_end_run_and_overwrite_refusal(self):
        cal = _load_script()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            argv = ["--out", str(out), "--variants", "iid", "--replicates-null", "1",
                    "--replicates-alt", "1", "--effect-sizes", "0.02", "--workers", "1",
                    "--n-symbols", "6", "--n-days", "80", "--block-resamples", "99"]
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cal.main(argv), 0)
            doc = json.loads((out / "calibration.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["schema"], "fintr_engine_calibration_v1")
            self.assertTrue(doc["synthetic_only"])
            self.assertEqual(set(doc["scenarios"]), {"iid/null", "iid/alt_0.02"})
            self.assertEqual(doc["config"]["block_length"], 5)
            self.assertEqual(doc["config"]["cost_bps_per_side"], "10")
            null = doc["scenarios"]["iid/null"]["summary"]
            self.assertEqual(null["n_replicates"], 1)
            self.assertIn("familywise_any_holm_reject", null)
            self.assertTrue(null["hypotheses"]["H1"]["event_count_matches_truth"])
            for key in ("wall_seconds", "workers", "cpu_seconds"):
                self.assertIn(key, doc["timing"])
            self.assertTrue((out / "calibration.md").read_text(encoding="utf-8").startswith("# Engine"))
            # frozen spec files are only read: block-bootstrap parameters recorded as frozen
            self.assertEqual(doc["specs"]["H1"]["frozen_block_bootstrap"]["resamples"], 9999)
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cal.main(argv), 2)  # never overwrites


def _strip_timing(value):
    if isinstance(value, dict):
        return {k: _strip_timing(v) for k, v in value.items()
                if k not in ("seconds", "mean_seconds_per_replicate_cpu")}
    if isinstance(value, list):
        return [_strip_timing(v) for v in value]
    return value


class ResumeTests(unittest.TestCase):
    ARGV = ["--variants", "iid", "--replicates-null", "3", "--replicates-alt", "2",
            "--effect-sizes", "0.02", "--workers", "1", "--n-symbols", "6", "--n-days", "80",
            "--block-resamples", "99"]

    def test_resumed_run_equals_uninterrupted_run(self):
        cal = _load_script()
        with tempfile.TemporaryDirectory() as tmp:
            full, resumed = Path(tmp) / "full", Path(tmp) / "resumed"
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cal.main(["--out", str(full), *self.ARGV]), 0)
            doc_full = json.loads((full / "calibration.json").read_text(encoding="utf-8"))
            details = [rec for name in doc_full["scenarios"]
                       for rec in doc_full["scenarios"][name]["replicates_detail"]]
            self.assertEqual(len(details), 5)
            # interrupted run: only 3 of 5 replicates finished (in task order), the
            # last line cut mid-write, no header (as the first script version wrote)
            order = sorted(details, key=lambda rec: (rec["r"], rec["scenario"]))
            lines = [json.dumps(rec) for rec in order[:3]]
            resumed.mkdir()
            (resumed / "replicates.partial.jsonl").write_text(
                "\n".join(lines) + "\n" + json.dumps(order[3])[:40], encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cal.main(["--out", str(resumed), "--resume", *self.ARGV]), 0)
            doc_resumed = json.loads((resumed / "calibration.json").read_text(encoding="utf-8"))
            self.assertEqual(doc_resumed["timing"]["resumed_from_replicates"], 3)
            self.assertEqual(_strip_timing(doc_resumed["scenarios"]), _strip_timing(doc_full["scenarios"]))
            self.assertFalse((resumed / "replicates.partial.jsonl").exists())

    def test_resume_refuses_foreign_or_missing_partial(self):
        cal = _load_script()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            out.mkdir()
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cal.main(["--out", str(out), "--resume", *self.ARGV]), 2)  # no partial
            record = {"scenario": "iid/null", "r": 0, "seed": "1:0", "block_bootstrap_seed": 1,
                      "hypotheses": {"H1": {}, "H2": {}}}
            partial = out / "replicates.partial.jsonl"
            partial.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with redirect_stderr(io.StringIO()):  # seeds belong to another --seed
                self.assertEqual(cal.main(["--out", str(out), "--resume", *self.ARGV]), 2)
            self.assertTrue(partial.exists())  # left untouched
            self.assertIn('"seed": "1:0"', partial.read_text(encoding="utf-8"))
            header = {"_header": {"seed": 20261013, "n_symbols": 7, "n_days": 80, "specs": ["H1", "H2"],
                                  "block_resamples": 99, "iid_resamples": 20}}
            partial.write_text(json.dumps(header) + "\n", encoding="utf-8")
            with redirect_stderr(io.StringIO()):  # header from different options
                self.assertEqual(cal.main(["--out", str(out), "--resume", *self.ARGV]), 2)


@unittest.skipUnless(
    SLOW_TESTS,
    "slow calibration (several minutes, many cores): set FINTR_SLOW_TESTS=1 to run",
)
class SlowCalibrationTests(unittest.TestCase):
    def test_null_false_positive_rate_is_not_far_above_nominal(self):
        script = ROOT / "scripts" / "calibrate_engine.py"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "slow"
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
            subprocess.run(
                [sys.executable, str(script), "--out", str(out), "--variants", "iid", "dependent",
                 "--replicates-null", "300", "--replicates-alt", "0", "--effect-sizes",
                 "--n-symbols", "12", "--n-days", "150", "--block-resamples", "2999",
                 "--workers", str(max(1, min(8, (os.cpu_count() or 2) - 2)))],
                check=True, env=env, capture_output=True, text=True, timeout=3600)
            doc = json.loads((out / "calibration.json").read_text(encoding="utf-8"))
            for name in ("iid/null", "dependent/null"):
                fw = doc["scenarios"][name]["summary"]["familywise_any_holm_reject"]
                # nominal 0.05; fail only if the exact 95% CI sits above it or the
                # point estimate is more than double nominal
                self.assertLessEqual(fw["ci95_lo"], 0.05, (name, fw))
                self.assertLessEqual(fw["rate"], 0.10, (name, fw))


if __name__ == "__main__":
    unittest.main()
