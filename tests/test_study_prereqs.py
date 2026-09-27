"""Offline tests for the pre-registration engine prerequisites.

E1 (moving block bootstrap by signal date, one-sided p), the Holm primary
family (m = 2; H3/H4 secondary_descriptive, outside Holm), E4 (merging
several period captures of one symbol) and the per-side cost wording. Every
symbol, date, code and number is synthetic; no market data, no network.
"""

from __future__ import annotations

import copy
import io
import json
from contextlib import redirect_stderr
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import random
import unittest
from unittest.mock import patch

from fintr.data.period_capture import (
    BrokerageDirectoryInfo,
    CodeValue,
    DailyMarketProxyRow,
    DatedCodeValues,
    DatedMarketProxyValues,
    PeriodCapture,
    PeriodMetadataDay,
)
from fintr.research.capture_merge import CaptureMergeError, CaptureSource, merge_period_captures
from fintr.research.engine import (
    EngineError,
    EventOutcome,
    block_bootstrap_replicates,
    build_date_panel,
    draw_moving_block_indices,
    moving_block_bootstrap,
    nearest_rank_percentile,
    one_sided_p_value,
    summarize_horizon,
)
from fintr.research.family import FamilyError, evaluate_primary_family, holm_decisions
from fintr.research.report import RunParameters, run_study
from fintr.research.study_spec import (
    BlockBootstrapConfig,
    StudySpecError,
    load_study_spec,
    parse_study_spec,
)

import test_period_analysis as period_fixtures
import test_study_engine as engine_fixtures
import test_symbol_guard as guard_fixtures


ROOT = Path(__file__).resolve().parents[1]
BASE_DAY = date(2027, 2, 1)


def _day(i: int) -> date:
    return BASE_DAY + timedelta(days=i)


def _event(symbol: str, day: date, net: str | float, *, conditioned: bool, dropped: bool = False) -> EventOutcome:
    value = None if dropped else Decimal(str(net))
    return EventOutcome(
        symbol, day, None if dropped else day, None if dropped else day, 5,
        None if dropped else "raw", value, value, dropped,
        "insufficient_bars" if dropped else None, True if conditioned else None,
    )


def _config(**overrides) -> BlockBootstrapConfig:
    values = {"block_length_signal_dates": 5, "resamples": 499, "seed": 20261001}
    values.update(overrides)
    return BlockBootstrapConfig(**values)


def _random_panel_events(n_dates: int = 40, symbols=("AAA", "BBB", "CCC"), seed: int = 7):
    rng = random.Random(seed)
    conditioned, baseline = [], []
    for i in range(n_dates):
        for symbol in symbols:
            value = round(rng.uniform(-0.05, 0.05), 6)
            baseline.append(_event(symbol, _day(i), value, conditioned=False))
            if rng.random() < 0.4:
                conditioned.append(_event(symbol, _day(i), value, conditioned=True))
    return conditioned, baseline


class BlockBootstrapReproducibilityTests(unittest.TestCase):
    def test_same_seed_same_inputs_give_identical_results(self):
        conditioned, baseline = _random_panel_events()
        first = moving_block_bootstrap(conditioned, baseline, config=_config(), predicted_direction="negative")
        second = moving_block_bootstrap(conditioned, baseline, config=_config(), predicted_direction="negative")
        self.assertEqual(first, second)
        self.assertEqual(first.status, "computed")
        self.assertEqual((first.seed, first.resamples, first.block_length_signal_dates), (20261001, 499, 5))

    def test_different_seed_changes_the_replicates(self):
        conditioned, baseline = _random_panel_events()
        panel = build_date_panel(conditioned, baseline)
        a = block_bootstrap_replicates(panel, block_length=5, resamples=50, seed=1)
        b = block_bootstrap_replicates(panel, block_length=5, resamples=50, seed=2)
        self.assertNotEqual(a, b)

    def test_global_random_state_is_not_used(self):
        conditioned, baseline = _random_panel_events()
        random.seed(1)
        first = moving_block_bootstrap(conditioned, baseline, config=_config(), predicted_direction="positive")
        random.seed(999)
        second = moving_block_bootstrap(conditioned, baseline, config=_config(), predicted_direction="positive")
        self.assertEqual(first, second)

    def test_seed_and_resamples_are_read_from_the_spec_and_written_to_the_result(self):
        doc = engine_fixtures._spec_doc(
            block_bootstrap={"method": "moving_block_by_signal_date",
                             "block_length_signal_dates": 3, "resamples": 77, "seed": 5},
        )
        spec = parse_study_spec(doc)
        bars = tuple(
            engine_fixtures._bar(engine_fixtures.D[i], o=100 + i, h=100 + i, l=100 + i,
                                 c=100 + i, adj=100 + i, v=1000)
            for i in range(12)
        )
        capture = engine_fixtures._capture(server_dates=tuple(engine_fixtures.D[:8]))
        from fintr.research.engine import load_symbol_data
        data = load_symbol_data(
            "TST", capture, engine_fixtures._price("TST", bars),
            adjustment_ratio_epsilon=Decimal("0.0005"), custody_scale_jump_ratio=Decimal("3"),
        )
        params = RunParameters(Decimal(0), Decimal("0.5"), Decimal("0.0005"), Decimal("3"))
        result = run_study(spec, {"TST": data}, split="train", params=params)
        block = result["overall"]["1"]["block_bootstrap_difference"]
        self.assertEqual((block["seed"], block["resamples"], block["block_length_signal_dates"]), (5, 77, 3))
        self.assertEqual(block["method"], "moving_block_by_signal_date")
        # The i.i.d. bootstrap stays as the secondary diagnostic.
        self.assertIn("bootstrap_difference_ci", result["overall"]["1"])
        # E1 is pooled-panel only; per-symbol blocks carry no block bootstrap.
        self.assertIsNone(result["per_symbol"]["TST"]["1"]["block_bootstrap_difference"])

    def test_spec_without_block_bootstrap_gets_none_not_a_default(self):
        stats = summarize_horizon(
            [_event("AAA", _day(0), "0.01", conditioned=True)],
            [_event("AAA", _day(0), "0.01", conditioned=False)],
            horizon=5, min_event_warning_threshold=1, bootstrap_seed=1, bootstrap_resamples=10,
            bootstrap_confidence_level=Decimal("0.9"),
        )
        self.assertIsNone(stats.block_bootstrap)

    def test_invalid_block_bootstrap_block_is_rejected(self):
        for bad in (
            {"method": "iid", "block_length_signal_dates": 5, "resamples": 10, "seed": 1},
            {"method": "moving_block_by_signal_date", "block_length_signal_dates": 0, "resamples": 10, "seed": 1},
            {"method": "moving_block_by_signal_date", "block_length_signal_dates": 5, "resamples": 0, "seed": 1},
            {"method": "moving_block_by_signal_date", "block_length_signal_dates": 5, "resamples": 10, "seed": True},
        ):
            with self.subTest(block=bad), self.assertRaises(StudySpecError):
                parse_study_spec(engine_fixtures._spec_doc(block_bootstrap=bad))


class SameDateDrawnTogetherTests(unittest.TestCase):
    def test_indices_are_consecutive_blocks_cut_to_n(self):
        rng = random.Random(3)
        indices = draw_moving_block_indices(rng, 23, 5)
        self.assertEqual(len(indices), 23)
        for start in range(0, 23, 5):
            block = indices[start:start + 5]
            self.assertEqual(block, list(range(block[0], block[0] + len(block))))
            self.assertLessEqual(block[0], 23 - 5)

    def test_block_longer_than_panel_is_refused(self):
        with self.assertRaises(EngineError):
            draw_moving_block_indices(random.Random(0), 4, 5)

    def test_all_symbols_of_a_date_enter_a_replicate_together(self):
        # On every date two symbols carry exactly offsetting conditioned
        # returns (+r, -r). If events of one date were ever split across a
        # resample, some replicate's conditioned mean would be non-zero.
        conditioned, baseline = [], []
        for i in range(30):
            r = 0.001 * (i + 1)
            for symbol, value in (("AAA", r), ("BBB", -r)):
                conditioned.append(_event(symbol, _day(i), value, conditioned=True))
                baseline.append(_event(symbol, _day(i), value, conditioned=False))
            baseline.append(_event("CCC", _day(i), 0.01 * i, conditioned=False))
        panel = build_date_panel(conditioned, baseline)
        self.assertEqual(len(panel.dates), 30)
        self.assertEqual(panel.conditioned_count, (2,) * 30)
        replicates = block_bootstrap_replicates(panel, block_length=5, resamples=300, seed=11)
        self.assertTrue(all(c == 0.0 for c, _ in replicates))
        # ... while the baseline means do vary across replicates.
        self.assertGreater(len({b for _, b in replicates}), 1)

    def test_conditioned_and_baseline_means_use_the_same_drawn_dates(self):
        # Every event on date i returns exactly i; the conditioned
        # population is one symbol, the baseline two symbols with the same
        # date value, so both means of a replicate equal the mean drawn
        # date value: delta* is 0 in every replicate only if both
        # populations were computed from the same drawn dates.
        conditioned, baseline = [], []
        for i in range(25):
            conditioned.append(_event("AAA", _day(i), i, conditioned=True))
            baseline.append(_event("AAA", _day(i), i, conditioned=False))
            baseline.append(_event("BBB", _day(i), i, conditioned=False))
        panel = build_date_panel(conditioned, baseline)
        for c, b in block_bootstrap_replicates(panel, block_length=5, resamples=200, seed=4):
            self.assertAlmostEqual(c, b, places=12)

    def test_dropped_events_are_not_resampling_units(self):
        conditioned = [_event("AAA", _day(0), 0, conditioned=True, dropped=True)]
        baseline = [_event("AAA", _day(0), 0, conditioned=False, dropped=True),
                    _event("AAA", _day(1), "0.01", conditioned=False)]
        panel = build_date_panel(conditioned, baseline)
        self.assertEqual(panel.dates, (_day(1),))


class OneSidedPValueTests(unittest.TestCase):
    def test_formula_negative_direction_counts_ge_zero(self):
        deltas = [-0.02, -0.01, 0.0, 0.01]
        # (1 + #{>= 0}) / (B + 1) = (1 + 2) / 5
        self.assertEqual(one_sided_p_value(deltas, "negative"), 3 / 5)

    def test_formula_positive_direction_counts_le_zero(self):
        deltas = [-0.02, -0.01, 0.0, 0.01]
        # (1 + #{<= 0}) / (B + 1) = (1 + 3) / 5
        self.assertEqual(one_sided_p_value(deltas, "positive"), 4 / 5)

    def test_undefined_replicate_counts_against_the_prediction(self):
        self.assertEqual(one_sided_p_value([-0.1, None], "negative"), 2 / 3)
        self.assertEqual(one_sided_p_value([0.1, None], "positive"), 2 / 3)

    def test_unknown_direction_is_refused(self):
        with self.assertRaises(EngineError):
            one_sided_p_value([0.1], "two_sided")

    def test_end_to_end_minimum_p_is_one_over_b_plus_one(self):
        conditioned, baseline = [], []
        for i in range(20):
            conditioned.append(_event("AAA", _day(i), "-0.05", conditioned=True))
            baseline.append(_event("AAA", _day(i), "-0.05", conditioned=False))
            baseline.append(_event("BBB", _day(i), "0.05", conditioned=False))
        result = moving_block_bootstrap(
            conditioned, baseline, config=_config(resamples=999), predicted_direction="negative",
        )
        self.assertEqual(result.p_value_one_sided, 1 / 1000)
        self.assertAlmostEqual(result.observed_difference, -0.05)
        opposite = moving_block_bootstrap(
            conditioned, baseline, config=_config(resamples=999), predicted_direction="positive",
        )
        self.assertEqual(opposite.p_value_one_sided, 1.0)

    def test_no_direction_reports_no_p_value(self):
        conditioned, baseline = _random_panel_events()
        result = moving_block_bootstrap(conditioned, baseline, config=_config(), predicted_direction=None)
        self.assertIsNone(result.p_value_one_sided)
        self.assertIsNotNone(result.percentile_05)

    def test_nearest_rank_percentile(self):
        values = [float(v) for v in range(1, 101)]
        self.assertEqual(nearest_rank_percentile(values, Decimal("0.05")), 5.0)
        self.assertEqual(nearest_rank_percentile(values, Decimal("0.95")), 95.0)
        self.assertIsNone(nearest_rank_percentile([], Decimal("0.05")))

    def test_too_few_dates_is_reported_not_guessed(self):
        conditioned = [_event("AAA", _day(i), "0.01", conditioned=True) for i in range(3)]
        baseline = [_event("AAA", _day(i), "0.01", conditioned=False) for i in range(3)]
        result = moving_block_bootstrap(conditioned, baseline, config=_config(), predicted_direction="negative")
        self.assertEqual(result.status, "too_few_signal_dates")
        self.assertIsNone(result.p_value_one_sided)


class HolmTests(unittest.TestCase):
    def test_m2_both_rejected(self):
        d = holm_decisions({"H1": 0.02, "H2": 0.04}, "0.05", order=["H1", "H2"])
        self.assertTrue(d["H1"]["rejected"])  # 0.02 <= 0.05/2
        self.assertTrue(d["H2"]["rejected"])  # 0.04 <= 0.05/1
        self.assertEqual(d["H1"]["threshold"], "0.025")
        self.assertEqual(d["H2"]["threshold"], "0.05")

    def test_m2_first_step_fails_so_nothing_is_rejected(self):
        d = holm_decisions({"H1": 0.03, "H2": 0.04}, "0.05")
        self.assertFalse(d["H1"]["rejected"])  # 0.03 > 0.025
        self.assertFalse(d["H2"]["rejected"])  # stops at first failure though 0.04 <= 0.05

    def test_m2_only_smallest_rejected(self):
        d = holm_decisions({"H1": 0.06, "H2": 0.01}, "0.05")
        self.assertTrue(d["H2"]["rejected"])
        self.assertFalse(d["H1"]["rejected"])
        self.assertEqual(d["H2"]["rank"], 1)

    def test_boundary_equality_rejects_exactly(self):
        # B = 9999, 249 replicates against: p = 250/10000 = 0.025 = alpha/2.
        d = holm_decisions({"H1": 250 / 10000, "H2": 0.5}, "0.05")
        self.assertTrue(d["H1"]["rejected"])

    def test_invalid_inputs_are_refused(self):
        with self.assertRaises(FamilyError):
            holm_decisions({}, "0.05")
        with self.assertRaises(FamilyError):
            holm_decisions({"H1": 1.5}, "0.05")


def _hypothesis_result(hid: str, *, role: str, p: float, split: str = "test",
                       min_met: bool = True, bound_excludes: bool = False) -> dict:
    primary = role == "primary"
    return {
        "spec_id": f"spec_{hid}",
        "spec_sha256": hid * 8,
        "split": split,
        "parameters": {"transaction_cost_bps": "10"},
        "hypothesis_test": {
            "hypothesis_id": hid,
            "family_id": "fam",
            "family_role": role,
            "enters_holm_family": primary,
            "primary_family_members": ["H1", "H2"],
            "family_size": 2,
            "multiple_comparison": "holm",
            "familywise_alpha": "0.05",
            "predicted_direction": "negative",
            "difference_mean": "-0.01",
            "min_events_met": min_met,
            "p_value_one_sided": p,
            "p_value_for_holm": (p if min_met else 1.0) if primary else None,
            "one_sided_bound": -0.001,
            "sesoi_excluded_in_predicted_direction": bound_excludes,
        },
    }


class FamilyTests(unittest.TestCase):
    def test_h3_h4_never_enter_the_holm_family(self):
        results = [
            _hypothesis_result("H1", role="primary", p=0.03),
            _hypothesis_result("H2", role="primary", p=0.04),
            # Tiny p values that would change Holm (m = 4) if they entered.
            _hypothesis_result("H3", role="secondary_descriptive", p=0.0001),
            _hypothesis_result("H4", role="secondary_descriptive", p=0.0001),
        ]
        decision = evaluate_primary_family(results)
        self.assertEqual(decision["m"], 2)
        self.assertEqual([row["hypothesis_id"] for row in decision["primary"]], ["H1", "H2"])
        self.assertFalse(any(row["holm"]["rejected"] for row in decision["primary"]))
        self.assertEqual(decision["primary"][0]["holm"]["threshold"], "0.025")
        secondary = {row["hypothesis_id"]: row for row in decision["secondary_descriptive"]}
        self.assertEqual(set(secondary), {"H3", "H4"})
        self.assertTrue(all(row["enters_holm_family"] is False and "holm" not in row
                            for row in secondary.values()))
        self.assertTrue(decision["confirmatory"])

    def test_outcome_classes(self):
        decision = evaluate_primary_family([
            _hypothesis_result("H1", role="primary", p=0.01),
            _hypothesis_result("H2", role="primary", p=0.5, bound_excludes=True),
        ])
        outcomes = {row["hypothesis_id"]: row["outcome"] for row in decision["primary"]}
        self.assertEqual(outcomes, {"H1": "supported", "H2": "falsified"})
        decision = evaluate_primary_family([
            _hypothesis_result("H1", role="primary", p=0.001, min_met=False),
            _hypothesis_result("H2", role="primary", p=0.5),
        ])
        outcomes = {row["hypothesis_id"]: row["outcome"] for row in decision["primary"]}
        self.assertEqual(outcomes, {"H1": "inconclusive_insufficient_events", "H2": "inconclusive"})
        self.assertEqual(decision["primary"][0]["p_value_for_holm"], 1.0)

    def test_missing_primary_member_is_an_error_not_a_smaller_family(self):
        with self.assertRaises(FamilyError):
            evaluate_primary_family([_hypothesis_result("H1", role="primary", p=0.01)])

    def test_mixed_splits_or_duplicates_are_refused(self):
        with self.assertRaises(FamilyError):
            evaluate_primary_family([
                _hypothesis_result("H1", role="primary", p=0.01, split="train"),
                _hypothesis_result("H2", role="primary", p=0.01, split="test"),
            ])
        with self.assertRaises(FamilyError):
            evaluate_primary_family([
                _hypothesis_result("H1", role="primary", p=0.01),
                _hypothesis_result("H1", role="primary", p=0.02),
                _hypothesis_result("H2", role="primary", p=0.01),
            ])

    def test_secondary_spec_claiming_primary_membership_is_refused_at_load(self):
        doc = copy.deepcopy(json.loads(
            (ROOT / "configs/studies/prereg/h3_concentrated_flow_close_premium_v1.json").read_text(encoding="utf-8")
        ))
        doc["preregistration"]["family_role"] = "primary"
        with self.assertRaises(StudySpecError):
            parse_study_spec(doc)
        doc["preregistration"]["family_role"] = "secondary_descriptive"
        doc["preregistration"]["primary_family_members"] = ["H1", "H2", "H3"]
        with self.assertRaises(StudySpecError):  # family_size 2 != 3 members
            parse_study_spec(doc)

    def test_run_study_hypothesis_block_for_primary_and_secondary(self):
        bars = tuple(
            engine_fixtures._bar(engine_fixtures.D[i], o=100, h=100, l=100, c=100 + i, adj=100 + i, v=1000)
            for i in range(20)
        )
        capture = engine_fixtures._capture(server_dates=tuple(engine_fixtures.D[:12]))
        from fintr.research.engine import load_symbol_data
        data = load_symbol_data(
            "TST", capture, engine_fixtures._price("TST", bars),
            adjustment_ratio_epsilon=Decimal("0.0005"), custody_scale_jump_ratio=Decimal("3"),
        )
        params = RunParameters(Decimal(0), Decimal("0.5"), Decimal("0.0005"), Decimal("3"))
        base = json.loads((ROOT / "configs/studies/prereg/h1_close_premium_vs_vwap_proxy_v1.json")
                          .read_text(encoding="utf-8"))["preregistration"]
        for hid, role in (("H1", "primary"), ("H3", "secondary_descriptive")):
            with self.subTest(hypothesis=hid):
                prereg = dict(base, hypothesis_id=hid, family_role=role, primary_horizon_trading_days=1)
                doc = engine_fixtures._spec_doc(
                    block_bootstrap={"method": "moving_block_by_signal_date",
                                     "block_length_signal_dates": 5, "resamples": 99, "seed": 1},
                    preregistration=prereg,
                )
                result = run_study(parse_study_spec(doc), {"TST": data}, split="train", params=params)
                test = result["hypothesis_test"]
                self.assertEqual(test["family_role"], role)
                # 11 events on 11 dates: below 100 events / 30 dates.
                self.assertFalse(test["min_events_met"])
                if role == "primary":
                    self.assertTrue(test["enters_holm_family"])
                    self.assertEqual(test["p_value_for_holm"], 1.0)
                else:
                    self.assertFalse(test["enters_holm_family"])
                    self.assertIsNone(test["p_value_for_holm"])

    def test_preregistration_without_block_bootstrap_is_refused(self):
        prereg = json.loads((ROOT / "configs/studies/prereg/h1_close_premium_vs_vwap_proxy_v1.json")
                            .read_text(encoding="utf-8"))["preregistration"]
        doc = engine_fixtures._spec_doc(preregistration=dict(prereg, primary_horizon_trading_days=1))
        with self.assertRaises(StudySpecError):
            parse_study_spec(doc)


def _merge_capture(*, captured_at: str, start: date, end: date, days: dict[date, int],
                   symbol: str = "TST", packet: str = "0") -> PeriodCapture:
    """Synthetic capture; ``days`` maps date -> custody lot value for code CUS
    (AKD net/market rows derived from the same number)."""
    dates = tuple(sorted(days))
    return PeriodCapture(
        symbol=symbol, start=start, end=end, captured_at_utc=captured_at,
        packet_sha256=packet * 64, server_dates=dates, period_akd_net=(CodeValue("AAA", Decimal(1)),),
        daily_akd_net=tuple(DatedCodeValues(d, (CodeValue("AAA", Decimal(days[d])),)) for d in dates),
        daily_akd_market=tuple(
            DatedMarketProxyValues(d, (DailyMarketProxyRow("AAA", Decimal(10), Decimal(50)),)) for d in dates
        ),
        daily_custody_lots=tuple(DatedCodeValues(d, (CodeValue("CUS", Decimal(days[d])),)) for d in dates),
        custodian_diff_start=start, custodian_diff_end=end, custodian_diff=(),
        metadata=tuple(PeriodMetadataDay(d, None, None, ()) for d in dates),
        brokerage_directory=BrokerageDirectoryInfo(present=False, row_count=0, index=None),
    )


class CaptureMergeTests(unittest.TestCase):
    def setUp(self):
        d = [date(2027, 3, i) for i in range(1, 16)]
        self.d = d
        # First capture: d1..d5, last custody day d5 provisional (value 500).
        self.first = _merge_capture(
            captured_at="2027-03-06T18:00:00Z", start=d[0], end=d[4],
            days={d[0]: 100, d[1]: 200, d[2]: 300, d[3]: 400, d[4]: 500}, packet="a",
        )
        # Second capture overlaps on d5 with a revised value (555).
        self.second = _merge_capture(
            captured_at="2027-03-11T18:00:00Z", start=d[4], end=d[9],
            days={d[4]: 555, d[5]: 600, d[8]: 900, d[9]: 1000}, packet="b",
        )

    def test_later_capture_wins_the_whole_overlapping_date(self):
        merged = merge_period_captures([
            CaptureSource(self.second, "second", "f2"), CaptureSource(self.first, "first", "f1"),
        ])
        capture = merged.capture
        lots = {day.date: day.by_code()["CUS"] for day in capture.daily_custody_lots}
        akd = {day.date: day.by_code()["AAA"] for day in capture.daily_akd_net}
        self.assertEqual(lots[self.d[4]], Decimal(555))
        self.assertEqual(akd[self.d[4]], Decimal(555))
        self.assertEqual(lots[self.d[0]], Decimal(100))
        self.assertEqual(capture.server_dates, tuple(sorted(
            {self.d[i] for i in (0, 1, 2, 3, 4, 5, 8, 9)})))
        self.assertEqual((capture.start, capture.end), (self.d[0], self.d[9]))
        self.assertEqual(capture.period_akd_net, ())  # period aggregates are not merged
        overlap = merged.report["overlap_dates"]
        self.assertEqual([row["date"] for row in overlap], [self.d[4].isoformat()])
        self.assertEqual(overlap[0]["owner"], "second")
        self.assertEqual(overlap[0]["series_differing_from_owner"], ["daily_akd_net", "daily_custody_lots"])
        sources = merged.report["sources"]
        self.assertEqual([s["label"] for s in sources], ["first", "second"])
        self.assertEqual([s["file_sha256"] for s in sources], ["f1", "f2"])
        self.assertEqual([s["packet_sha256"] for s in sources], ["a" * 64, "b" * 64])
        self.assertEqual(sources[0]["overridden_date_count"], 1)
        self.assertNotIn(capture.packet_sha256, ("a" * 64, "b" * 64))

    def test_merge_is_independent_of_argument_order(self):
        a = merge_period_captures([CaptureSource(self.first, "first"), CaptureSource(self.second, "second")])
        b = merge_period_captures([CaptureSource(self.second, "second"), CaptureSource(self.first, "first")])
        self.assertEqual(a.capture, b.capture)
        self.assertEqual(a.report, b.report)

    def test_seam_gap_and_missing_tradeable_dates_are_reported(self):
        third = _merge_capture(
            captured_at="2027-03-16T18:00:00Z", start=self.d[12], end=self.d[14],
            days={self.d[12]: 1, self.d[14]: 2}, packet="c",
        )
        tradeable = [self.d[i] for i in range(15)]
        merged = merge_period_captures(
            [CaptureSource(self.first, "first"), CaptureSource(self.second, "second"),
             CaptureSource(third, "third")],
            tradeable_dates=tradeable,
        )
        first_seam, second_seam = merged.report["seams"]
        self.assertTrue(first_seam["requested_periods_contiguous_or_overlapping"])
        self.assertFalse(first_seam["gap_detected"])
        self.assertEqual(first_seam["missing_tradeable_dates"], [])
        self.assertFalse(second_seam["requested_periods_contiguous_or_overlapping"])
        self.assertEqual(second_seam["calendar_days_between_requested_periods"], 2)
        self.assertEqual(second_seam["missing_tradeable_dates"],
                         [self.d[10].isoformat(), self.d[11].isoformat()])
        self.assertTrue(merged.report["any_seam_gap"])
        # Nothing interpolated into the gap.
        self.assertNotIn(self.d[10], merged.capture.server_dates)

    def test_capture_time_order_disagreeing_with_period_order_is_refused(self):
        backfill = _merge_capture(
            captured_at="2027-04-01T00:00:00Z", start=date(2027, 2, 1), end=date(2027, 3, 1),
            days={date(2027, 3, 1): 7}, packet="d",
        )
        with self.assertRaises(CaptureMergeError):
            merge_period_captures([CaptureSource(self.first), CaptureSource(backfill)])

    def test_equal_capture_times_symbol_mismatch_and_duplicates_are_refused(self):
        same_time = _merge_capture(
            captured_at=self.first.captured_at_utc, start=self.d[5], end=self.d[6],
            days={self.d[5]: 1}, packet="e",
        )
        other_symbol = _merge_capture(
            captured_at="2027-03-20T00:00:00Z", start=self.d[5], end=self.d[6],
            days={self.d[5]: 1}, symbol="OTH", packet="f",
        )
        for sources in (
            [CaptureSource(self.first), CaptureSource(same_time)],
            [CaptureSource(self.first), CaptureSource(other_symbol)],
            [CaptureSource(self.first), CaptureSource(self.first)],
            [],
        ):
            with self.subTest(n=len(sources)), self.assertRaises(CaptureMergeError):
                merge_period_captures(sources)

    def test_single_capture_is_returned_unchanged(self):
        merged = merge_period_captures([CaptureSource(self.first, "only", "f1")])
        self.assertIs(merged.capture, self.first)
        self.assertEqual(merged.report["capture_count"], 1)
        self.assertEqual(merged.report["seams"], [])


class RunStudyCliMergeAndCostTests(guard_fixtures.GuardEnvMixin, unittest.TestCase):
    def setUp(self):
        self.root = self.make_env()
        self.cli = guard_fixtures.run_study_cli

    def _run(self, argv):
        stdout, stderr = io.BytesIO(), io.StringIO()
        with redirect_stderr(stderr), patch.object(
            self.cli.sys, "stdout", io.TextIOWrapper(stdout, encoding="utf-8"),
        ) as wrapped:
            code = self.cli.main(argv)
            wrapped.flush()
        return code, stdout.getvalue().decode("utf-8"), stderr.getvalue()

    def _write_inputs(self):
        symbol = guard_fixtures.OUTSIDE
        data_arg = guard_fixtures._write_symbol_data(self.root, symbol)
        _, rest = data_arg.split("=", 1)
        first_path, price_path = rest.split(",", 1)
        packet = period_fixtures._packet()
        packet["requested_period"].update(symbol=symbol, start_date="2026-01-06", end_date="2026-01-09")
        daily = packet["custody_analysis_daily"]["daily_custody_balance_in_provider_order"]
        revised = next(day for day in daily if day["date"] == "2026-01-08")
        revised["rows"][0]["lot_count"] += 1
        document = period_fixtures._capture(packet)
        document["capture_metadata"]["captured_at_utc"] = "2026-01-09T18:00:00Z"
        second_path = self.root / f"{symbol}_period_2.json"
        second_path.write_text(guard_fixtures.smoke._compact_json(document), encoding="utf-8")
        spec_path = self.root / "spec.json"
        spec_path.write_text(json.dumps(guard_fixtures._spec(symbols=[symbol], symbol_holdout=[])),
                             encoding="utf-8")
        return symbol, spec_path, first_path, second_path, price_path

    def test_repeated_data_entries_merge_captures_and_hash_each_input(self):
        symbol, spec, first, second, price = self._write_inputs()
        code, out, err = self._run([
            "--spec", str(spec), "--data", f"{symbol}={first},{price}",
            "--data", f"{symbol}={second},{price}", "--split", "train", "--format", "json",
        ])
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        report = result["capture_merge"][symbol]
        self.assertEqual(report["capture_count"], 2)
        self.assertEqual(report["sources"][1]["label"], str(second))
        revised = next(row for row in report["overlap_dates"] if row["date"] == "2026-01-08")
        self.assertIn("daily_custody_lots", revised["series_differing_from_owner"])
        hashes = result["reproducibility"]["input_file_sha256"]
        self.assertIn(f"{symbol}:period_capture:0", hashes)
        self.assertIn(f"{symbol}:period_capture:1", hashes)
        self.assertIn(f"{symbol}:price_history", hashes)
        self.assertEqual(result["parameters"]["transaction_cost_basis"],
                         "per_side (round_trip = 2 x transaction_cost_bps)")

    def test_conflicting_price_paths_for_one_symbol_are_refused(self):
        symbol, spec, first, second, price = self._write_inputs()
        code, _out, err = self._run([
            "--spec", str(spec), "--data", f"{symbol}={first},{price}",
            "--data", f"{symbol}={second},{price}.other", "--split", "train",
        ])
        self.assertEqual(code, 1)
        self.assertIn("conflicting price history paths", err)

    def test_cost_help_text_says_per_side(self):
        help_text = " ".join(self.cli._parser().format_help().split())
        self.assertIn("PER SIDE", help_text)
        self.assertIn("net = gross - 2 * bps / 10000", help_text)
        self.assertNotIn("round-trip transaction cost", help_text)


class PreregSpecsCarryE1AndFamilyTests(unittest.TestCase):
    def test_every_prereg_spec_loads_frozen_with_block_bootstrap(self):
        paths = sorted((ROOT / "configs/studies/prereg").glob("*.json"))
        self.assertEqual(len(paths), 4)
        for path in paths:
            spec = load_study_spec(path)
            self.assertTrue(spec.frozen)  # frozen 2026-09-27, see docs/prereg/FREEZE.md
            self.assertIsNotNone(spec.block_bootstrap)


if __name__ == "__main__":
    unittest.main()
