"""Tests for ``scripts/measure_condition_clustering.py`` (T-29).

Synthetic data only; nothing reads ``data/``.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import random
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

from fintr.research.engine import load_symbol_data

import synthetic_market as sm


ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "measure_condition_clustering", ROOT / "scripts" / "measure_condition_clustering.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mcc = _load_script()


def _bernoulli_matrix(rng, n_symbols, n_dates, p):
    return [[1 if rng.random() < p else 0 for _ in range(n_dates)] for _ in range(n_symbols)]


class MeasureMatrixTest(unittest.TestCase):
    def test_independent_flags_look_independent(self):
        rng = random.Random(1)
        metrics = mcc.measure_matrix(_bernoulli_matrix(rng, 20, 400, 0.1))
        self.assertAlmostEqual(metrics["frequency"], 0.1, delta=0.01)
        self.assertAlmostEqual(metrics["dispersion_index_poibin"], 1.0, delta=0.15)
        self.assertAlmostEqual(metrics["flag_acf_lag1_mean"], 0.0, delta=0.05)
        self.assertAlmostEqual(metrics["count_acf_lag1"], 0.0, delta=0.15)
        self.assertAlmostEqual(
            metrics["share_events_ge3_observed"], metrics["share_events_ge3_expected_independent"],
            delta=0.08)
        self.assertAlmostEqual(
            metrics["mean_run_length"], metrics["mean_run_length_expected_independent"], delta=0.15)

    def test_cross_sectional_clustering_raises_dispersion(self):
        # every symbol flagged on the same ~10% of dates: perfectly correlated across symbols
        rng = random.Random(2)
        row = [1 if rng.random() < 0.1 else 0 for _ in range(400)]
        metrics = mcc.measure_matrix([list(row) for _ in range(20)])
        self.assertGreater(metrics["dispersion_index_poibin"], 15.0)  # max is n_symbols = 20
        self.assertGreater(metrics["intra_date_correlation"], 0.8)
        self.assertEqual(metrics["share_events_ge3_observed"], 1.0)
        self.assertGreater(metrics["deff_total_K10"], 15.0)
        self.assertLess(metrics["effective_independent_symbol_days_K10"], 20 * 400 / 15.0)

    def test_temporal_clustering_raises_autocorrelation_and_runs(self):
        rng = random.Random(3)
        matrix = []
        for _ in range(20):
            state, row = 0, []
            for _ in range(400):
                state = (1 if rng.random() < 0.6 else 0) if state else (1 if rng.random() < 0.05 else 0)
                row.append(state)
            matrix.append(row)
        metrics = mcc.measure_matrix(matrix)
        self.assertGreater(metrics["flag_acf_lag1_mean"], 0.4)
        self.assertGreater(metrics["mean_run_length"], 1.8)
        self.assertLess(metrics["mean_run_length_expected_independent"], 1.3)
        self.assertGreater(metrics["deff_time_K10"], 1.0)
        self.assertEqual(metrics["flag_acf_symbols_used"], 20)

    def test_hand_computed_tiny_case(self):
        # 2 symbols x 4 dates; p0 = 2/4, p1 = 1/4
        matrix = [[1, 1, 0, 0], [0, 1, 0, 0]]
        metrics = mcc.measure_matrix(matrix)
        # E_t = .75 each date, V_t = .25 + .1875 = .4375; k = [1, 2, 0, 0]
        expected = ((1 - .75) ** 2 + (2 - .75) ** 2 + .75 ** 2 + .75 ** 2) / (4 * .4375)
        self.assertAlmostEqual(metrics["dispersion_index_poibin"], expected)
        self.assertEqual(metrics["flagged_symbol_days"], 3)
        self.assertEqual(metrics["run_count"], 2)
        self.assertAlmostEqual(metrics["mean_run_length"], 1.5)

    def test_missing_entries_are_excluded(self):
        matrix = [[1, None, 0, 0, 1, 0], [0, 0, None, 1, 0, 0]]
        metrics = mcc.measure_matrix(matrix)
        self.assertEqual(metrics["evaluated_symbol_days"], 10)
        self.assertEqual(metrics["flagged_symbol_days"], 3)
        self.assertAlmostEqual(metrics["frequency"], 0.3)

    def test_no_flags_returns_counts_only(self):
        metrics = mcc.measure_matrix([[0, 0, 0], [0, 0, 0]])
        self.assertEqual(metrics["flagged_symbol_days"], 0)
        self.assertNotIn("dispersion_index_poibin", metrics)

    def test_shuffle_reference_is_centred_on_independence(self):
        rng = random.Random(4)
        row = [1 if rng.random() < 0.1 else 0 for _ in range(300)]
        ref = mcc.shuffle_reference([list(row) for _ in range(10)], shuffles=30, seed=7)
        self.assertAlmostEqual(ref["dispersion_index_poibin"]["mean"], 1.0, delta=0.25)
        self.assertAlmostEqual(ref["flag_acf_lag1_mean"]["mean"], 0.0, delta=0.05)


class SyntheticPanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.specs = mcc._load_specs()
        cls.params = mcc._run_parameters(cls.specs["H1"])
        cfg = sm.MarketConfig(variant="dependent", n_symbols=5, n_days=60)
        cls.world = sm.generate_world(cfg, "test_condition_clustering")
        cls.symbol_data = {
            sym: load_symbol_data(sym, cls.world.captures[sym], cls.world.prices[sym], **cls.params)
            for sym in cls.world.symbols
        }
        cls.axis, cls.matrices, cls.accounting = mcc.condition_flag_panel(cls.symbol_data, cls.specs)

    def test_flags_match_generator_truth(self):
        # the generator recomputes the flags from the final rows with its own code; the
        # engine-path panel must agree wherever the engine evaluates the condition
        self.assertEqual(len(self.axis), 60)
        compared = 0
        for hid in self.specs:
            for row, sym in enumerate(sorted(self.symbol_data)):
                truth = self.world.flags[hid][sym]
                for t, value in enumerate(self.matrices[hid][row]):
                    if value is None:
                        continue
                    compared += 1
                    self.assertEqual(bool(value), truth[t], (hid, sym, t))
        self.assertGreater(compared, 0)

    def test_accounting_adds_up(self):
        for hid, acct in self.accounting.items():
            self.assertEqual(
                acct["in_scope_symbol_days"],
                acct["dropped_ratio_regime"] + acct["condition_not_evaluable"]
                + acct["dropped_custody_scale_jump"] + acct["evaluated"],
            )
            self.assertEqual(
                acct["evaluated"],
                sum(1 for row in self.matrices[hid] for v in row if v is not None),
            )
            self.assertEqual(
                acct["flagged"], sum(1 for row in self.matrices[hid] for v in row if v == 1))

    def test_dependent_world_clusters_more_than_iid_in_aggregate(self):
        # smoke check of direction on a small panel, many seeds are in the CLI run
        def dispersion(variant):
            values = []
            for i in range(3):
                cfg = sm.MarketConfig(variant=variant, n_symbols=10, n_days=200)
                world = sm.generate_world(cfg, f"direction:{variant}:{i}")
                sdata = {s: load_symbol_data(s, world.captures[s], world.prices[s], **self.params)
                         for s in world.symbols}
                _, matrices, _ = mcc.condition_flag_panel(sdata, {"H1": self.specs["H1"]})
                values.append(mcc.measure_matrix(matrices["H1"])["dispersion_index_poibin"])
            return sum(values) / len(values)

        self.assertGreater(dispersion("dependent"), dispersion("iid"))


class CliGuardTest(unittest.TestCase):
    def test_refuses_to_overwrite_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            target.write_text("{}", encoding="utf-8")
            buffer = io.StringIO()
            with redirect_stderr(buffer):
                code = mcc.main(["--out", str(target), "--data-root", tmp])
            self.assertEqual(code, 1)
            self.assertIn("refusing to overwrite", buffer.getvalue())
            self.assertEqual(target.read_text(encoding="utf-8"), "{}")


if __name__ == "__main__":
    unittest.main()
