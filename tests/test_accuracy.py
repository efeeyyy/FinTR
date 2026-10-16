"""Offline tests for the walk-forward accuracy tracker (``fintr.accuracy`` and
``scripts/track_accuracy.py``).

Every symbol, date, price and AKD number here is synthetic; nothing is a real
capture or a real financial figure. The scenario uses one rule whose condition
is ``vwap_proxy_vs_close`` in [-0.10, -0.01] and a hand-built price path whose
forward returns are known exactly (see ``_World``).
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fintr.accuracy.core import (
    COST_BPS_PER_SIDE,
    OUTCOME_DROPPED,
    OUTCOME_HIT,
    OUTCOME_MISS,
    OUTCOME_PENDING,
    OUTCOME_ZERO,
    PERIOD_POST_TRAIN,
    PERIOD_TRAIN,
    AccuracyError,
    EvalParams,
    SavedPrediction,
    SymbolInput,
    classify_outcome,
    cost_aware_hit,
    custody_visible_cutoff,
    period_of,
    point_in_time_view,
    select_window,
    summarize,
    track_accuracy,
    validate_symbols,
    wilson_interval,
)
from fintr.accuracy.saved import load_saved_predictions
from fintr.data.period_capture import (
    CodeValue,
    DailyMarketProxyRow,
    DatedCodeValues,
    DatedMarketProxyValues,
)
from fintr.evaluation.symbol_guard import load_active_guards
from fintr.forward_log.core import append_as_of
from fintr.research.study_spec import parse_study_spec
from fintr.scanner.observe import evaluate_rule_for_symbol
from fintr.scanner.rules import ScannerRule

import test_scanner as scanner_fixtures
from test_symbol_guard import DEVELOPMENT, EXCLUDED, HOLDOUT, GuardEnvMixin


ROOT = Path(__file__).resolve().parents[1]
REAL_RULES_FILE = ROOT / "configs" / "scanner" / "rules_v1.json"
PARAMS = EvalParams(
    adjustment_ratio_epsilon=Decimal("0.0005"), custody_scale_jump_ratio=Decimal("3"),
    ratio_regime_tolerance=Decimal("0.01"),
)
LAG2 = lambda t: 2  # noqa: E731 - T+2 custody settlement


def _load_script():
    spec = importlib.util.spec_from_file_location("accuracy_test_track", ROOT / "scripts" / "track_accuracy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


track_cli = _load_script()


def _weekdays(start: date, n: int) -> list[date]:
    days, current = [], start
    while len(days) < n:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


# D[0..4] = 2026-09-21..25 (train_window), D[5..13] = 2026-09-28..10-08 (post_train)
D = _weekdays(date(2026, 9, 21), 14)


def _rule(*, rule_id="X1", direction="negative", policy="drop") -> ScannerRule:
    doc = scanner_fixtures._spec_doc(
        condition=scanner_fixtures._VWAP_PROXY_CONDITION, hypothesis_id=rule_id,
    )
    doc["horizons_trading_days"] = [1, 5]
    doc["adjustment_boundary_policy"] = policy
    doc["preregistration"]["predicted_direction"] = direction
    doc["preregistration"]["primary_horizon_trading_days"] = 5
    spec = parse_study_spec(doc)
    return ScannerRule(rule_id=rule_id, spec_path="synthetic", spec_sha256="0" * 64, spec=spec)


class _World:
    """One symbol, 14 days. Opens are all 100. The condition (vwap 98 vs close 100 =
    -0.02) holds on ``signal_days`` only; every other day vwap = 100.

    Default closes (index -> close) give these exact forward gross returns for a
    signal on index 2 / 3 / 11 (entry = next open = 100; horizon h exit = close
    at entry index + h):
      idx 2: h1 -> close[4] = 99    = -0.0100 ; h5 -> close[8] = 100 = 0 (zero return)
      idx 3: h1 -> close[5] = 100.1 = +0.0010 ; h5 -> close[9] = 99.9 = -0.0010
      idx 11: h1 -> close[13] = 100.9 = +0.0090 ; h5 -> beyond the data (pending)
    """

    CLOSES = {4: "99", 5: "100.1", 8: "100", 9: "99.9", 13: "100.9"}

    def __init__(self, *, signal_days=(2, 3, 11), closes=None, vwap_after=None, symbol="TST",
                 adjusted_from=None, price_bars_override=None):
        closes = {**self.CLOSES, **(closes or {})}
        market = []
        for index, day in enumerate(D):
            vwap = 98 if index in signal_days else 100
            if vwap_after is not None and index > vwap_after[0]:
                vwap = vwap_after[1]
            market.append(DatedMarketProxyValues(
                day, (DailyMarketProxyRow("AAA", Decimal(2000), Decimal(vwap)),),
            ))
        self.capture = scanner_fixtures._capture(symbol, server_dates=D, daily_akd_market=tuple(market))
        bars = []
        for index, day in enumerate(D):
            close = Decimal(closes.get(index, "100"))
            adj = close * Decimal("0.9") if adjusted_from is not None and index >= adjusted_from else close
            bars.append(scanner_fixtures._bar(day, o=100, h=102, l=95, c=close, adj=adj, v=1000))
        if price_bars_override is not None:
            bars = price_bars_override(bars)
        self.price = scanner_fixtures._price(symbol, tuple(bars))
        self.symbol = symbol

    @property
    def item(self) -> SymbolInput:
        return SymbolInput(self.symbol, self.capture, self.price)


def _track(world=None, *, rules=None, saved=None, days=None):
    world = world or _World()
    rules = rules or (_rule(),)
    return track_accuracy(
        [world.item], rules, tuple(days or D), params=PARAMS, custody_lag_on=LAG2, saved=saved,
    )


def _rows(result, **match):
    return [r for r in result.rows if all(r[k] == v for k, v in match.items())]


class StatisticsTests(unittest.TestCase):
    def test_wilson_known_values(self):
        low, high = wilson_interval(5, 10)
        self.assertAlmostEqual(low, 0.2366, places=3)
        self.assertAlmostEqual(high, 0.7634, places=3)
        self.assertIsNone(wilson_interval(0, 0))
        low0, high0 = wilson_interval(0, 5)
        self.assertEqual(low0, 0.0)
        self.assertGreater(high0, 0.0)
        with self.assertRaises(AccuracyError):
            wilson_interval(6, 5)

    def test_period_boundary_is_2026_09_25(self):
        self.assertEqual(period_of(date(2026, 9, 25)), PERIOD_TRAIN)
        self.assertEqual(period_of(date(2026, 9, 26)), PERIOD_POST_TRAIN)

    def test_classify_and_cost_aware_hit(self):
        self.assertEqual(classify_outcome("negative", Decimal("-0.001")), OUTCOME_HIT)
        self.assertEqual(classify_outcome("negative", Decimal("0.001")), OUTCOME_MISS)
        self.assertEqual(classify_outcome("positive", Decimal("0.001")), OUTCOME_HIT)
        self.assertEqual(classify_outcome("positive", Decimal("0")), OUTCOME_ZERO)
        self.assertEqual(classify_outcome("negative", Decimal("0")), OUTCOME_ZERO)
        cost = COST_BPS_PER_SIDE  # 2 x 10 bp = 0.0020 round trip
        self.assertFalse(cost_aware_hit("negative", Decimal("-0.0020"), cost))  # must exceed
        self.assertTrue(cost_aware_hit("negative", Decimal("-0.0021"), cost))
        self.assertFalse(cost_aware_hit("negative", Decimal("0.01"), cost))  # wrong direction
        self.assertTrue(cost_aware_hit("positive", Decimal("0.0021"), cost))
        self.assertFalse(cost_aware_hit("positive", Decimal("-0.5"), cost))
        self.assertFalse(cost_aware_hit("positive", Decimal("0.0020"), cost))


class PointInTimeTests(unittest.TestCase):
    def test_custody_cutoff_is_lag_server_dates_before_t(self):
        self.assertEqual(custody_visible_cutoff(tuple(D[:6]), 2), D[3])
        self.assertEqual(custody_visible_cutoff(tuple(D[:6]), 1), D[4])
        self.assertIsNone(custody_visible_cutoff(tuple(D[:2]), 2))
        with self.assertRaises(AccuracyError):
            custody_visible_cutoff(tuple(D[:3]), -1)

    def test_view_hides_later_rows_unsettled_custody_and_later_bars(self):
        custody = tuple(
            DatedCodeValues(day, (CodeValue("AAA", Decimal(100 + i)),)) for i, day in enumerate(D[:9])
        )
        world = _World()
        capture = scanner_fixtures._capture(
            "TST", server_dates=D, daily_custody_lots=custody,
            daily_akd_market=world.capture.daily_akd_market,
        )
        view_capture, view_price = point_in_time_view(capture, world.price, D[5], custody_lag=2)
        self.assertEqual(view_capture.server_dates[-1], D[5])
        self.assertEqual(max(d.date for d in view_capture.daily_akd_market), D[5])
        self.assertEqual(max(d.date for d in view_capture.daily_custody_lots), D[3])  # T+2 settled
        self.assertEqual(max(b.trade_date for b in view_price.bars), D[5])
        # the full objects are untouched
        self.assertEqual(max(d.date for d in capture.daily_custody_lots), D[8])
        self.assertEqual(len(world.price.bars), 14)

    def test_prediction_at_t_ignores_everything_dated_after_t(self):
        # World B differs from A only after D[3]: later AKD rows, later prices, later closes.
        a = _World(signal_days=(2, 3))
        b = _World(signal_days=(2, 3), vwap_after=(3, 90),
                   closes={6: "55", 7: "300", 12: "1", 13: "9999"})
        rule = _rule()
        for index in (1, 2, 3):
            t = D[index]
            seen = []
            for world in (a, b):
                cap, px = point_in_time_view(world.capture, world.price, t, custody_lag=2)
                obs = evaluate_rule_for_symbol(
                    rule, cap, px, as_of=t,
                    adjustment_ratio_epsilon=PARAMS.adjustment_ratio_epsilon,
                    custody_scale_jump_ratio=PARAMS.custody_scale_jump_ratio,
                    ratio_regime_tolerance=PARAMS.ratio_regime_tolerance,
                )
                seen.append((obs.condition_met, obs.suppressed, [f.value for f in obs.features]))
            self.assertEqual(seen[0], seen[1], f"prediction at index {index} changed with later data")
        self.assertTrue(seen[0][0])  # D[3] is a signal in both worlds

    def test_track_rows_for_early_days_do_not_depend_on_later_akd(self):
        a = _track(_World(signal_days=(2, 3)))
        b = _track(_World(signal_days=(2, 3), vwap_after=(3, 90)))  # only AKD after D[3] differs
        early = lambda res: [r for r in res.rows if r["t"] <= D[3].isoformat()]  # noqa: E731
        self.assertEqual(early(a), early(b))
        self.assertEqual({r["t"] for r in early(a)}, {D[2].isoformat(), D[3].isoformat()})


class TrackTests(unittest.TestCase):
    def test_rows_outcomes_and_periods_match_hand_derived_returns(self):
        result = _track()
        self.assertEqual(result.horizons, (1, 5))
        self.assertEqual(result.primary_horizons, (5,))
        self.assertEqual(len(result.rows), 6)  # 3 signals x 2 horizons

        r = _rows(result, t=D[2].isoformat(), horizon=1)[0]
        self.assertEqual(
            (r["outcome"], r["cost_aware_hit"], r["period"], r["prediction_source"]),
            (OUTCOME_HIT, True, PERIOD_TRAIN, "reconstructed"),
        )
        self.assertEqual(Decimal(r["gross_return"]), Decimal("-0.01"))
        self.assertEqual(Decimal(r["net_return"]), Decimal("-0.01") - Decimal("0.002"))
        self.assertEqual((r["entry_date"], r["exit_date"]), (D[3].isoformat(), D[4].isoformat()))
        self.assertEqual(r["predicted_direction"], "negative")

        r = _rows(result, t=D[2].isoformat(), horizon=5)[0]
        self.assertEqual((r["outcome"], r["cost_aware_hit"]), (OUTCOME_ZERO, False))
        self.assertEqual(Decimal(r["gross_return"]), 0)

        r = _rows(result, t=D[3].isoformat(), horizon=1)[0]
        self.assertEqual((r["outcome"], r["cost_aware_hit"]), (OUTCOME_MISS, False))

        r = _rows(result, t=D[3].isoformat(), horizon=5)[0]
        self.assertEqual((r["outcome"], r["cost_aware_hit"]), (OUTCOME_HIT, False))  # |0.001| < 2x10bp

        r = _rows(result, t=D[11].isoformat(), horizon=1)[0]
        self.assertEqual((r["outcome"], r["period"], r["in_test_window"]), (OUTCOME_MISS, PERIOD_POST_TRAIN, True))
        self.assertFalse(_rows(result, t=D[3].isoformat(), horizon=1)[0]["in_test_window"])

    def test_pending_is_never_guessed(self):
        result = _track()
        r = _rows(result, t=D[11].isoformat(), horizon=5)[0]
        self.assertEqual(r["outcome"], OUTCOME_PENDING)
        self.assertIsNone(r["gross_return"])
        self.assertIsNone(r["cost_aware_hit"])
        self.assertIsNone(r["exit_date"])
        stats = summarize(result, ["X1"])[PERIOD_POST_TRAIN]["5"]["by_rule"]["X1"]
        self.assertEqual((stats["signals"], stats["resolved"], stats["pending"]), (1, 0, 1))
        self.assertIsNone(stats["directional"]["hit_rate"])
        self.assertIsNone(stats["directional"]["wilson95_low"])

    def test_signal_on_last_bar_has_no_entry_and_is_pending(self):
        world = _World(signal_days=(13,))
        result = _track(world)
        self.assertEqual({r["outcome"] for r in result.rows}, {OUTCOME_PENDING})
        self.assertEqual(len(result.rows), 2)

    def test_summary_counts_and_periods_are_separate(self):
        summary = summarize(_track(), ["X1"])
        train5 = summary[PERIOD_TRAIN]["5"]["by_rule"]["X1"]
        self.assertEqual((train5["signals"], train5["resolved"], train5["zero_return"], train5["misses"]), (2, 2, 1, 0))
        self.assertEqual(train5["directional"]["hits"], 1)
        self.assertEqual(train5["directional"]["hit_rate"], 0.5)
        self.assertEqual(train5["cost_aware"]["hits"], 0)
        self.assertEqual(train5["distinct_signal_dates"], 2)
        train1 = summary[PERIOD_TRAIN]["1"]["by_rule"]["X1"]
        self.assertEqual((train1["directional"]["hits"], train1["misses"], train1["cost_aware"]["hits"]), (1, 1, 1))
        post1 = summary[PERIOD_POST_TRAIN]["1"]["by_rule"]["X1"]
        self.assertEqual((post1["signals"], post1["resolved"], post1["misses"]), (1, 1, 1))
        self.assertEqual(set(summary), {PERIOD_TRAIN, PERIOD_POST_TRAIN})  # no pooled branch
        overall = summary[PERIOD_TRAIN]["1"]["overall"]
        self.assertEqual(overall["signals"], train1["signals"])  # one rule: pooled == rule
        by_dir = summary[PERIOD_TRAIN]["1"]["by_direction"]
        self.assertEqual(by_dir["negative"]["signals"], 2)
        self.assertEqual(by_dir["positive"]["signals"], 0)

    def test_base_rate_is_the_unconditional_direction_fraction(self):
        world = _World()
        result = _track(world)
        # independent recomputation: every day t whose entry and exit bars exist
        closes = [bar.close for bar in world.price.bars]
        opens = [bar.open for bar in world.price.bars]
        for horizon in (1, 5):
            for period in (PERIOD_TRAIN, PERIOD_POST_TRAIN):
                down = up = zero = resolved = pending = 0
                for i, day in enumerate(D):
                    if period_of(day) != period:
                        continue
                    entry, exit_ = i + 1, i + 1 + horizon
                    if exit_ >= len(D):
                        pending += 1
                        continue
                    gross = closes[exit_] / opens[entry] - 1
                    resolved += 1
                    down += gross < 0
                    up += gross > 0
                    zero += gross == 0
                tally = result.baseline[(period, horizon)]
                self.assertEqual(
                    (tally.resolved, tally.down, tally.up, tally.zero, tally.pending),
                    (resolved, down, up, zero, pending), (period, horizon),
                )
        stats = summarize(result, ["X1"])[PERIOD_TRAIN]["1"]
        tally = result.baseline[(PERIOD_TRAIN, 1)]
        rule_stats = stats["by_rule"]["X1"]["directional"]
        self.assertAlmostEqual(rule_stats["base_rate"], tally.down / tally.resolved, places=6)  # negative rule
        self.assertAlmostEqual(
            rule_stats["difference"], rule_stats["hit_rate"] - tally.down / tally.resolved, places=5,
        )
        self.assertAlmostEqual(
            stats["by_rule"]["X1"]["cost_aware"]["base_rate"], tally.down_cost / tally.resolved, places=6,
        )
        self.assertEqual(stats["baseline"]["resolved"], tally.resolved)

    def test_positive_rule_is_judged_against_the_up_fraction(self):
        result = _track(rules=(_rule(direction="positive"),))
        r = _rows(result, t=D[2].isoformat(), horizon=1)[0]
        self.assertEqual(r["outcome"], OUTCOME_MISS)  # realized -1% vs predicted up
        r = _rows(result, t=D[3].isoformat(), horizon=1)[0]
        self.assertEqual(r["outcome"], OUTCOME_HIT)
        stats = summarize(result, ["X1"])[PERIOD_TRAIN]["1"]
        tally = result.baseline[(PERIOD_TRAIN, 1)]
        self.assertAlmostEqual(
            stats["by_rule"]["X1"]["directional"]["base_rate"], tally.up / tally.resolved, places=6,
        )

    def test_adjustment_boundary_window_is_dropped_not_guessed(self):
        # adjusted_close/close ratio changes at index 8 -> any window with entry < d <= exit spanning it is dropped
        world = _World(adjusted_from=8)
        result = _track(world)
        r = _rows(result, t=D[3].isoformat(), horizon=5)[0]  # entry idx 4, exit idx 9 spans idx 8
        self.assertEqual((r["outcome"], r["drop_reason"], r["gross_return"]), (OUTCOME_DROPPED, "adjustment_boundary_drop", None))
        self.assertEqual(_rows(result, t=D[3].isoformat(), horizon=1)[0]["outcome"], OUTCOME_MISS)
        stats = summarize(result, ["X1"])[PERIOD_TRAIN]["5"]["by_rule"]["X1"]
        self.assertEqual(stats["dropped"], 2)

    def test_placeholder_bars_are_not_trading_days(self):
        # a holiday placeholder bar (flat OHLC, no volume) between signal and entry is skipped
        def with_placeholder(bars):
            placeholder = scanner_fixtures._bar(D[3], o=100, h=100, l=100, c=100, adj=100, v=0)
            return [placeholder if b.trade_date == D[3] else b for b in bars]
        world = _World(signal_days=(2,), price_bars_override=with_placeholder)
        result = _track(world)
        r = _rows(result, t=D[2].isoformat(), horizon=1)[0]
        # entry skips the placeholder day D[3]: entry D[4] open 100, exit D[5] close 100.1
        self.assertEqual((r["entry_date"], r["exit_date"]), (D[4].isoformat(), D[5].isoformat()))
        self.assertEqual(Decimal(r["gross_return"]), Decimal("100.1") / 100 - 1)
        self.assertEqual(result.skipped_days, {"TST": [D[3].isoformat()]})  # the placeholder day is no data day

    def test_evaluation_counts_cover_non_signals(self):
        result = _track()
        counts = result.evaluation_counts
        total_train = sum(counts[PERIOD_TRAIN]["reconstructed"].values())
        self.assertEqual(total_train, 5)  # D[0..4]
        self.assertEqual(counts[PERIOD_TRAIN]["reconstructed"]["condition_met"], 2)
        self.assertEqual(counts[PERIOD_POST_TRAIN]["reconstructed"]["condition_met"], 1)

    def test_days_without_data_are_listed_not_invented(self):
        world = _World()
        extra = D[-1] + timedelta(days=3)  # a window day nothing covers
        result = _track(world, days=tuple(D) + (extra,))
        self.assertEqual(result.skipped_days, {"TST": [extra.isoformat()]})

    def test_select_window_default_last_n_and_explicit_range(self):
        item = _World().item
        self.assertEqual(select_window([item], start=None, end=None, last_n=3), tuple(D[-3:]))
        self.assertEqual(select_window([item], start=None, end=D[9], last_n=2), (D[8], D[9]))
        self.assertEqual(select_window([item], start=D[4], end=D[6], last_n=99), (D[4], D[5], D[6]))


class SavedPredictionTests(unittest.TestCase):
    def test_saved_prediction_is_preferred_and_cross_checked(self):
        saved = {
            ("TST", "X1", D[2]): SavedPrediction("forward_log", True, None),   # agrees with reconstruction
            ("TST", "X1", D[3]): SavedPrediction("saved_scan", False, None),   # disagrees: no signal then
        }
        result = _track(saved=saved)
        self.assertEqual({r["prediction_source"] for r in _rows(result, t=D[2].isoformat())}, {"forward_log"})
        self.assertEqual(_rows(result, t=D[3].isoformat()), [])  # the saved (earlier) prediction wins
        self.assertEqual({r["prediction_source"] for r in _rows(result, t=D[11].isoformat())}, {"reconstructed"})
        check = result.reconstruction_check
        self.assertEqual((check["compared"], check["agree"], check["disagree"]), (2, 1, 1))
        self.assertEqual(check["disagreements"][0]["t"], D[3].isoformat())
        self.assertEqual(check["disagreements"][0]["saved"], "condition_not_met")
        self.assertEqual(check["disagreements"][0]["reconstructed"], "condition_met")

    def _write_scan(self, scanner_dir: Path, label: str, *, rules_sha: str, met: bool = True) -> Path:
        folder = scanner_dir / label
        folder.mkdir(parents=True)
        path = folder / "observations.json"
        path.write_text(json.dumps({
            "schema": "fintr_scanner_observations_v2", "generated_at_utc": "2026-10-01T10:00:00Z",
            "rules_file_sha256": rules_sha,
            "symbols": {"TST": {"scans": [{"as_of": label, "observations": [
                {"rule_id": "X1", "condition_met": met, "suppressed": None},
            ]}]}},
        }), encoding="utf-8")
        return path

    def test_forward_log_provenance_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory)
            scanner_dir = data_root / "scanner_daily"
            manifest = data_root / "manifest.jsonl"
            for label in ("2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05"):
                self._write_scan(scanner_dir, label, rules_sha="r" * 64)
            self._write_scan(scanner_dir, "2026-10-06", rules_sha="other" + "0" * 59)
            now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
            append_as_of(manifest, data_root, "2026-09-30", backfilled=True, now=now)   # backfilled
            append_as_of(manifest, data_root, "2026-10-01", backfilled=False, now=now)  # real forward log
            append_as_of(manifest, data_root, "2026-10-02", backfilled=False, now=now)
            # 10-02 changes after being logged -> hash differs from the log
            scan_path = scanner_dir / "2026-10-02" / "observations.json"
            scan_path.write_text(scan_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
            days = tuple(date(2026, 10, 1) + timedelta(days=i) for i in range(-1, 6))
            predictions, provenance = load_saved_predictions(
                scanner_dir, days=days, rules_file_sha256="r" * 64, forward_manifest=manifest,
            )
            source = lambda label: predictions[("TST", "X1", date.fromisoformat(label))].source  # noqa: E731
            self.assertEqual(source("2026-09-30"), "saved_scan")  # backfilled entry proves no timing
            self.assertIn("forward_log_entry_is_backfilled_not_proof_of_timing", provenance["2026-09-30"]["notes"])
            self.assertEqual(source("2026-10-01"), "forward_log")
            self.assertEqual(provenance["2026-10-01"]["forward_log"]["backfilled"], False)
            self.assertEqual(source("2026-10-02"), "saved_scan")  # file no longer matches the logged hash
            self.assertIn("forward_log_hash_differs_from_file", provenance["2026-10-02"]["notes"])
            self.assertEqual(source("2026-10-05"), "saved_scan")  # never logged
            self.assertIn("no_forward_log_entry", provenance["2026-10-05"]["notes"])
            self.assertNotIn(("TST", "X1", date(2026, 10, 6)), predictions)  # different rules hash: not used
            self.assertFalse(provenance["2026-10-06"]["used"])
            self.assertIn("rules_hash_differs_not_used", provenance["2026-10-06"]["notes"])

    def test_no_manifest_means_saved_scan_only(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner_dir = Path(directory) / "scanner_daily"
            self._write_scan(scanner_dir, "2026-10-01", rules_sha="r" * 64)
            predictions, provenance = load_saved_predictions(
                scanner_dir, days=(date(2026, 10, 1),), rules_file_sha256="r" * 64, forward_manifest=None,
            )
            self.assertEqual(predictions[("TST", "X1", date(2026, 10, 1))].source, "saved_scan")
            self.assertIn("no_forward_log_manifest_given", provenance["2026-10-01"]["notes"])


class HoldoutRefusalTests(GuardEnvMixin, unittest.TestCase):
    def test_validate_symbols_accepts_development_only(self):
        self.make_env()
        guards = load_active_guards()
        self.assertEqual(validate_symbols(["devb", "DEVA"], guards), ["DEVA", "DEVB"])
        with self.assertRaises(AccuracyError) as ctx:
            validate_symbols([DEVELOPMENT[0], HOLDOUT[0]], guards)
        self.assertIn("holdout", str(ctx.exception))
        with self.assertRaises(AccuracyError):
            validate_symbols([EXCLUDED], guards)
        with self.assertRaises(AccuracyError):
            validate_symbols(["OUTSD"], guards)  # not in the split's development set

    def test_cli_refuses_holdout_before_reading_any_data(self):
        self.make_env()
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(errors), redirect_stdout(io.StringIO()):
            code = track_cli.main([
                "--symbols", HOLDOUT[0], "--rules-file", str(REAL_RULES_FILE),
                "--period-dir", directory, "--price-dir", directory, "--out-dir", directory,
            ])
            self.assertEqual(code, 1)
            self.assertIn("holdout", errors.getvalue())
            self.assertEqual(list(Path(directory).iterdir()), [])  # nothing written

    def test_cli_default_universe_is_the_development_symbols(self):
        self.make_env()
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(errors), redirect_stdout(io.StringIO()):
            code = track_cli.main([
                "--rules-file", str(REAL_RULES_FILE), "--period-dir", directory,
                "--price-dir", directory, "--out-dir", directory,
            ])
        self.assertEqual(code, 1)  # no capture on disk: fails on the first development symbol
        self.assertIn(DEVELOPMENT[0], errors.getvalue())
        self.assertNotIn(HOLDOUT[0], errors.getvalue())


class CliEndToEndTests(GuardEnvMixin, unittest.TestCase):
    SYMBOL = scanner_fixtures.DailyRootEndToEndTests.SYMBOL

    def _fixture(self):
        self.make_env()
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        holder = scanner_fixtures.DailyRootEndToEndTests("test_composed_observation_schema_and_composition_block")
        period_root, daily_root, price_root = holder._write_fixture_data(root)
        self.root = root
        self.argv = [
            "--symbols", self.SYMBOL, "--period-dir", str(period_root), "--daily-root", str(daily_root),
            "--price-dir", str(price_root), "--scanner-dir", str(root / "scanner_daily"),
            "--forward-log", str(root / "none.jsonl"), "--rules-file", str(REAL_RULES_FILE),
            "--out-dir", str(root / "accuracy"), "--run-date", "2026-10-16", "--last-n-days", "4",
        ]

    def test_writes_json_and_markdown_and_never_overwrites(self):
        self._fixture()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = track_cli.main(self.argv)
        self.assertEqual(code, 0, err.getvalue())
        target = self.root / "accuracy" / "2026-10-16"
        document = json.loads((target / "accuracy.json").read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], "fintr_accuracy_v1")
        self.assertEqual(document["decision"]["holdout_symbols_monitored"], False)
        self.assertEqual(document["decision"]["telegram_output"], False)
        self.assertEqual(document["parameters"]["symbols"], [self.SYMBOL])
        self.assertEqual(document["parameters"]["window_day_count"], 4)
        self.assertEqual(document["rules"]["rules_file_sha256"], hashlib.sha256(REAL_RULES_FILE.read_bytes()).hexdigest())
        self.assertEqual(sorted(document["input_files"][self.SYMBOL]), [
            "composition_sha256", "daily_captures", "period_capture", "price_history",
        ])
        self.assertEqual(set(document["summary"]), {PERIOD_TRAIN, PERIOD_POST_TRAIN})
        self.assertIn("look_ahead", document)
        markdown = (target / "accuracy.md").read_text(encoding="utf-8")
        self.assertIn("tavsiye", markdown)
        self.assertIn("train_window", markdown)
        # stdout: path and counts only
        self.assertIn("ACCURACY_SAVED: ", out.getvalue())
        self.assertIn("counts period=train_window horizon=5", out.getvalue())
        self.assertNotIn("hit_rate", out.getvalue())

        before = (target / "accuracy.json").read_bytes()
        err2 = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err2):
            code = track_cli.main(self.argv)
        self.assertEqual(code, track_cli.EXIT_EXISTS)
        self.assertIn("no overwrite", err2.getvalue())
        self.assertEqual((target / "accuracy.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
