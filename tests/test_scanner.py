"""Offline tests for the observation scanner (``fintr.scanner`` +
``scripts/scan_observations.py``).

All symbols, dates, codes and numbers here are synthetic; nothing is a real
capture or a real financial figure. Capture/price objects are built the same
way ``tests/test_study_engine.py`` does (dataclasses, bypassing the JSON
loaders) except for the script-level tests, which reuse
``tests/test_period_analysis.py``'s on-disk capture/price fixture writers and
``tests/test_symbol_guard.py``'s synthetic split/exclusions environment, so
the real ``configs/`` files are never required for those either.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fintr.data.period_capture import (
    BrokerageDirectoryInfo,
    CodeValue,
    DatedCodeValues,
    PeriodCapture,
)
from fintr.data.price_history import DailyBar, PriceHistoryCapture, PriceHistoryCaptureMeta
from fintr.evaluation import symbol_exclusions, symbol_split
from fintr.research.study_spec import parse_study_spec
from fintr.scanner.freshness import build_freshness_report
from fintr.scanner.observe import evaluate_rule_for_symbol
from fintr.scanner.report import build_document, observation_payload
from fintr.scanner.rules import ScannerRule, ScannerRulesError, load_scanner_rules

import test_period_analysis as period_fixtures
from test_symbol_guard import DEVELOPMENT, EXCLUDED, HOLDOUT, GuardEnvMixin


ROOT = Path(__file__).resolve().parents[1]
REAL_RULES_FILE = ROOT / "configs" / "scanner" / "rules_v1.json"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"scanner_test_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = _load_script("scan_observations")


D = [date(2027, 3, day) for day in range(1, 11)]  # D[0] = 2027-03-01 .. D[9]


def _capture(
    symbol: str = "TST", *, server_dates=(), daily_akd_net=(), daily_akd_market=(),
    daily_custody_lots=(),
) -> PeriodCapture:
    dates = tuple(sorted(server_dates))
    return PeriodCapture(
        symbol=symbol, start=dates[0], end=dates[-1], captured_at_utc="2027-03-01T00:00:00Z",
        packet_sha256="0" * 64, server_dates=dates, period_akd_net=(),
        daily_akd_net=tuple(sorted(daily_akd_net, key=lambda d: d.date)),
        daily_akd_market=tuple(sorted(daily_akd_market, key=lambda d: d.date)),
        daily_custody_lots=tuple(sorted(daily_custody_lots, key=lambda d: d.date)),
        custodian_diff_start=dates[0], custodian_diff_end=dates[-1], custodian_diff=(),
        metadata=(), brokerage_directory=BrokerageDirectoryInfo(present=False, row_count=0, index=None),
    )


def _bar(d: date, *, o=None, h=None, l=None, c=None, adj=None, v=None) -> DailyBar:
    def dec(x):
        return None if x is None else Decimal(str(x))
    return DailyBar(
        trade_date=d, timestamp_utc=0, open=dec(o), high=dec(h), low=dec(l), close=dec(c),
        adjusted_close=dec(adj), volume=v,
    )


def _price(symbol: str, bars: tuple[DailyBar, ...]) -> PriceHistoryCapture:
    return PriceHistoryCapture(
        meta=PriceHistoryCaptureMeta(
            schema="fintr_price_history_capture_v1", source_id="synthetic",
            request_url="synthetic://test", accessed_at_utc="2027-03-01T00:00:00Z",
            permission_status="user_asserted_not_independently_verified", unofficial_source=True,
            raw_response_sha256="0" * 64, normalized_sha256="0" * 64,
        ),
        symbol=symbol, requested_start=bars[0].trade_date, requested_end=bars[-1].trade_date,
        date_source="synthetic", dropped_before_start_count=0, dropped_after_end_count=0, bars=bars,
    )


def _spec_doc(*, condition: dict, hypothesis_id="X1") -> dict:
    return {
        "schema": "fintr_study_spec_v1",
        "spec_id": f"unit_test_{hypothesis_id}",
        "description": "synthetic unit-test spec",
        "frozen": True,
        "symbols": ["TST"],
        "symbol_holdout": [],
        "train_test_cutoff": "2027-06-01",
        "entry_timing": "next_open",
        "horizons_trading_days": [1],
        "min_event_warning_threshold": 1,
        "adjustment_boundary_policy": "use_adjusted",
        "bootstrap": {"seed": 42, "resamples": 10, "confidence_level": "0.90"},
        "condition": condition,
        "block_bootstrap": {
            "method": "moving_block_by_signal_date", "block_length_signal_dates": 1,
            "resamples": 10, "seed": 1,
        },
        "preregistration": {
            "hypothesis_id": hypothesis_id, "family_id": "unit_test_family",
            "family_role": "primary", "primary_family_members": [hypothesis_id],
            "family_size": 1, "multiple_comparison": "holm", "familywise_alpha": "0.05",
            "predicted_direction": "negative", "primary_horizon_trading_days": 1,
            "min_events_primary": 1, "min_distinct_signal_dates_primary": 1,
            "sesoi_difference": "0.0025",
        },
    }


def _rule(*, condition: dict, hypothesis_id="X1") -> ScannerRule:
    spec = parse_study_spec(_spec_doc(condition=condition, hypothesis_id=hypothesis_id))
    return ScannerRule(rule_id=hypothesis_id, spec_path="synthetic", spec_sha256="0" * 64, spec=spec)


_CUSTODY_CHANGE_GE_ZERO = {
    "op": "ge",
    "left": {"feature": "custody_change", "params": {"codes": ["AAA"], "lag_days": 1}},
    "right": {"const": "0"},
}

_VWAP_PROXY_CONDITION = {
    "op": "and",
    "terms": [
        {"op": "le", "left": {"feature": "vwap_proxy_vs_close"}, "right": {"const": "-0.01"}},
        {"op": "ge", "left": {"feature": "vwap_proxy_vs_close"}, "right": {"const": "-0.10"}},
    ],
}

_DEFAULT_PARAMS = dict(
    adjustment_ratio_epsilon=Decimal("0.0005"), custody_scale_jump_ratio=Decimal("3"),
    ratio_regime_tolerance=Decimal("0.01"),
)


class RulesLoaderTests(unittest.TestCase):
    def _write_spec(self, directory: Path, *, frozen: bool = True) -> tuple[Path, str, str]:
        doc = _spec_doc(condition={"op": "ge", "left": {"const": "0"}, "right": {"const": "0"}})
        doc["frozen"] = frozen
        path = directory / "spec.json"
        text = json.dumps(doc)
        path.write_text(text, encoding="utf-8")
        raw_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        canonical_sha256 = parse_study_spec(doc).spec_sha256
        return path, raw_sha256, canonical_sha256

    def _write_rules_file(self, directory: Path, entry: dict) -> Path:
        rules_path = directory / "rules.json"
        rules_path.write_text(json.dumps({
            "schema": "fintr_scanner_rules_v1", "rules": [entry],
        }), encoding="utf-8")
        return rules_path

    def test_valid_rules_load_with_family_role(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path, raw_sha256, canonical_sha256 = self._write_spec(root)
            entry = {
                "rule_id": "X1", "spec_path": "spec.json",
                "raw_sha256": raw_sha256, "canonical_sha256": canonical_sha256,
            }
            rules_path = self._write_rules_file(root, entry)
            rules = load_scanner_rules(rules_path, project_root=root)
            self.assertEqual(len(rules), 1)
            self.assertEqual(rules[0].rule_id, "X1")
            self.assertEqual(rules[0].family_role, "primary")

    def test_raw_hash_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path, raw_sha256, canonical_sha256 = self._write_spec(root)
            entry = {
                "rule_id": "X1", "spec_path": "spec.json",
                "raw_sha256": "0" * 64, "canonical_sha256": canonical_sha256,
            }
            rules_path = self._write_rules_file(root, entry)
            with self.assertRaises(ScannerRulesError) as ctx:
                load_scanner_rules(rules_path, project_root=root)
            self.assertIn("raw_sha256 mismatch", str(ctx.exception))

    def test_canonical_hash_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path, raw_sha256, canonical_sha256 = self._write_spec(root)
            entry = {
                "rule_id": "X1", "spec_path": "spec.json",
                "raw_sha256": raw_sha256, "canonical_sha256": "0" * 64,
            }
            rules_path = self._write_rules_file(root, entry)
            with self.assertRaises(ScannerRulesError) as ctx:
                load_scanner_rules(rules_path, project_root=root)
            self.assertIn("canonical_sha256 mismatch", str(ctx.exception))

    def test_frozen_false_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path, raw_sha256, canonical_sha256 = self._write_spec(root, frozen=False)
            entry = {
                "rule_id": "X1", "spec_path": "spec.json",
                "raw_sha256": raw_sha256, "canonical_sha256": canonical_sha256,
            }
            rules_path = self._write_rules_file(root, entry)
            with self.assertRaises(ScannerRulesError) as ctx:
                load_scanner_rules(rules_path, project_root=root)
            self.assertIn("not frozen", str(ctx.exception))

    def test_real_rules_file_loads(self):
        rules = load_scanner_rules(REAL_RULES_FILE, project_root=ROOT)
        self.assertEqual({r.rule_id for r in rules}, {"H1", "H2", "H3", "H4"})
        self.assertEqual(
            {r.rule_id: r.family_role for r in rules},
            {"H1": "primary", "H2": "primary", "H3": "secondary_descriptive",
             "H4": "secondary_descriptive"},
        )


class LookaheadTests(unittest.TestCase):
    def test_future_data_change_never_affects_as_of_result(self):
        rule = _rule(condition=_CUSTODY_CHANGE_GE_ZERO)
        bars = tuple(_bar(d, o=10, h=10, l=10, c=10, adj=10, v=100) for d in D[:3])
        price = _price("TST", bars)

        def make_capture(d2_value: int) -> PeriodCapture:
            return _capture(
                server_dates=D[:3],
                daily_custody_lots=(
                    DatedCodeValues(D[0], (CodeValue("AAA", Decimal(100)),)),
                    DatedCodeValues(D[1], (CodeValue("AAA", Decimal(150)),)),
                    DatedCodeValues(D[2], (CodeValue("AAA", Decimal(d2_value)),)),
                ),
            )

        obs_a = evaluate_rule_for_symbol(rule, make_capture(150), price, as_of=D[1], **_DEFAULT_PARAMS)
        obs_b = evaluate_rule_for_symbol(rule, make_capture(999999), price, as_of=D[1], **_DEFAULT_PARAMS)
        self.assertEqual(obs_a.condition_met, obs_b.condition_met)
        self.assertEqual(
            [f.value for f in obs_a.features], [f.value for f in obs_b.features],
        )
        self.assertTrue(obs_a.condition_met)  # 150 - 100 = 50 >= 0


class SuppressionTests(unittest.TestCase):
    def test_ratio_regime_suppresses_observation(self):
        # A single AAA code with total_size=1000, total_cost=100 makes
        # implied_traded_lots=500 and vwap_proxy=100; a bar with volume=1000
        # gives volume_ratio = 500/1000 = 1/2 exactly (k=2), and
        # vwap_proxy/2 = 50 sits inside [low, high] = [0, 1000].
        from fintr.data.period_capture import DailyMarketProxyRow, DatedMarketProxyValues

        rule = _rule(condition=_VWAP_PROXY_CONDITION)
        capture = _capture(
            symbol="TST", server_dates=D[:1],
            daily_akd_market=(
                DatedMarketProxyValues(D[0], (DailyMarketProxyRow("AAA", Decimal(1000), Decimal(100)),)),
            ),
        )
        bars = (_bar(D[0], o=100, h=1000, l=0, c=100, adj=100, v=1000),)
        price = _price("TST", bars)

        obs = evaluate_rule_for_symbol(rule, capture, price, as_of=D[0], **_DEFAULT_PARAMS)
        self.assertEqual(obs.suppressed, "ratio_regime")
        self.assertIsNone(obs.condition_met)
        self.assertEqual(obs.features, ())

    def test_custody_scale_jump_suppresses_observation(self):
        rule = _rule(condition=_CUSTODY_CHANGE_GE_ZERO)
        bars = tuple(_bar(d, o=10, h=10, l=10, c=10, adj=10, v=100) for d in D[:3])
        price = _price("TST", bars)
        capture = _capture(
            server_dates=D[:3],
            daily_custody_lots=(
                DatedCodeValues(D[0], (CodeValue("AAA", Decimal(100)),)),
                DatedCodeValues(D[1], (CodeValue("AAA", Decimal(100)),)),
                # 10x jump D1 -> D2, well past the default 3x threshold.
                DatedCodeValues(D[2], (CodeValue("AAA", Decimal(1000)),)),
            ),
        )
        obs = evaluate_rule_for_symbol(rule, capture, price, as_of=D[2], **_DEFAULT_PARAMS)
        self.assertEqual(obs.suppressed, "custody_scale_jump")
        self.assertIsNone(obs.condition_met)
        self.assertEqual(obs.features, ())


class MissingDataTests(unittest.TestCase):
    def test_condition_not_evaluable_reports_null_not_false(self):
        rule = _rule(condition=_CUSTODY_CHANGE_GE_ZERO)
        bars = (_bar(D[0], o=10, h=10, l=10, c=10, adj=10, v=100),)
        price = _price("TST", bars)
        # No daily_custody_lots at all: custody_change cannot be evaluated.
        capture = _capture(server_dates=D[:1])
        obs = evaluate_rule_for_symbol(rule, capture, price, as_of=D[0], **_DEFAULT_PARAMS)
        self.assertIsNone(obs.suppressed)
        self.assertIsNone(obs.condition_met)
        self.assertEqual(obs.not_evaluable_reason, "missing_data")


class FreshnessTests(unittest.TestCase):
    def test_custody_behind_akd_flags_t_plus_2(self):
        bars = tuple(_bar(d, o=10, h=10, l=10, c=10, adj=10, v=100) for d in D[:3])
        price = _price("TST", bars)
        capture = _capture(
            server_dates=D[:3],
            daily_custody_lots=(DatedCodeValues(D[0], (CodeValue("AAA", Decimal(1)),)),),
        )
        report = build_freshness_report(capture, price, as_of=D[2])
        self.assertEqual(report.last_akd_server_date, D[2])
        self.assertEqual(report.last_custody_date, D[0])
        self.assertEqual(report.custody_behind_akd_server_dates, 2)
        self.assertTrue(report.custody_flagged_stale)

    def test_missing_custody_reported_as_none_not_zero(self):
        bars = (_bar(D[0], o=10, h=10, l=10, c=10, adj=10, v=100),)
        price = _price("TST", bars)
        capture = _capture(server_dates=D[:1])  # no custody data at all
        report = build_freshness_report(capture, price, as_of=D[0])
        self.assertIsNone(report.last_custody_date)
        self.assertIsNone(report.custody_behind_akd_server_dates)
        self.assertFalse(report.custody_flagged_stale)


class ForbiddenFieldsTests(unittest.TestCase):
    _FORBIDDEN_KEYS = {"score", "rank", "return", "target", "net_return", "gross_return"}

    def _assert_no_forbidden_keys(self, value) -> None:
        if isinstance(value, dict):
            for key, sub in value.items():
                self.assertNotIn(str(key).lower(), self._FORBIDDEN_KEYS, f"forbidden field: {key}")
                self._assert_no_forbidden_keys(sub)
        elif isinstance(value, list):
            for item in value:
                self._assert_no_forbidden_keys(item)

    def test_observation_payload_has_no_forbidden_fields(self):
        rule = _rule(condition=_CUSTODY_CHANGE_GE_ZERO)
        bars = tuple(_bar(d, o=10, h=10, l=10, c=10, adj=10, v=100) for d in D[:2])
        price = _price("TST", bars)
        capture = _capture(
            server_dates=D[:2],
            daily_custody_lots=(
                DatedCodeValues(D[0], (CodeValue("AAA", Decimal(100)),)),
                DatedCodeValues(D[1], (CodeValue("AAA", Decimal(150)),)),
            ),
        )
        obs = evaluate_rule_for_symbol(rule, capture, price, as_of=D[1], **_DEFAULT_PARAMS)
        payload = observation_payload(obs)
        self._assert_no_forbidden_keys(payload)

    def test_full_document_has_no_forbidden_fields(self):
        document = build_document(
            project_root=ROOT, rules_file="configs/scanner/rules_v1.json",
            rules_file_sha256="0" * 64, split_guard={"guard_bypassed": False},
            symbols={},
        )
        self._assert_no_forbidden_keys(document)


class HelpAndGuardTests(GuardEnvMixin, unittest.TestCase):
    def test_help_runs(self):
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                scan.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_holdout_symbol_rejected(self):
        self.make_env()
        with redirect_stderr(io.StringIO()) as errors:
            code = scan.main([
                "--symbols", HOLDOUT[0], "--rules-file", str(REAL_RULES_FILE),
            ])
        self.assertEqual(code, 1)
        self.assertIn("holdout", errors.getvalue())

    def test_excluded_symbol_rejected(self):
        self.make_env()
        with redirect_stderr(io.StringIO()) as errors:
            code = scan.main([
                "--symbols", EXCLUDED, "--rules-file", str(REAL_RULES_FILE),
            ])
        self.assertEqual(code, 1)
        self.assertIn("excluded", errors.getvalue())

    def test_no_split_guard_flag_does_not_exist(self):
        self.assertNotIn("--no-split-guard", scan._parser().format_help())


class ScriptEndToEndTests(GuardEnvMixin, unittest.TestCase):
    def _write_fixture_data(self, root: Path) -> tuple[Path, Path]:
        period_root = root / "period_analysis"
        price_root = root / "price_history"
        symbol = DEVELOPMENT[0]
        packet = period_fixtures._packet()
        packet["requested_period"]["symbol"] = symbol
        capture_doc = period_fixtures._capture(packet)
        period_fixtures._write_capture(period_root, symbol, "2026-01-05", "2026-01-08", capture_doc)
        bars = [
            period_fixtures._price_bar(d, o=str(10 + i), h=str(10 + i), l=str(10 + i),
                                        c=str(10 + i), adj=str(10 + i), v=100)
            for i, d in enumerate(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"])
        ]
        period_fixtures._write_price_capture(price_root, symbol, "2026-01-05", "2026-01-08", bars)
        return period_root, price_root

    def test_success_path_json_output_and_save_no_overwrite(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            period_root, price_root = self._write_fixture_data(root)
            out_dir = root / "out"
            argv = [
                "--symbols", DEVELOPMENT[0],
                "--period-dir", str(period_root),
                "--price-dir", str(price_root),
                "--rules-file", str(REAL_RULES_FILE),
                "--as-of", "2026-01-08",
                "--out-dir", str(out_dir),
                "--format", "json",
                "--save",
            ]
            stdout = io.BytesIO()
            with redirect_stderr(io.StringIO()), patch.object(
                scan.sys, "stdout", io.TextIOWrapper(stdout, encoding="utf-8"),
            ) as wrapped:
                code = scan.main(argv)
                wrapped.flush()
            self.assertEqual(code, 0)
            document = json.loads(stdout.getvalue().decode("utf-8"))
            self.assertEqual(document["schema"], "fintr_scanner_observations_v1")
            self.assertIn(DEVELOPMENT[0], document["symbols"])
            saved = out_dir / "2026-01-08" / "observations.json"
            self.assertTrue(saved.exists())

            # Re-running with --save must never overwrite the existing file.
            with redirect_stderr(io.StringIO()) as errors:
                code = scan.main(argv)
            self.assertEqual(code, 1)
            self.assertIn("already exists", errors.getvalue())

    def test_scanner_never_reads_studies_directory(self):
        # DEFAULT_PERIOD_DIR/DEFAULT_PRICE_DIR/DEFAULT_OUT_DIR are the only
        # data/interim paths the script ever constructs a Path from; none of
        # them is (or is derived from) data/interim/studies.
        for value in (scan.DEFAULT_PERIOD_DIR, scan.DEFAULT_PRICE_DIR, scan.DEFAULT_OUT_DIR):
            self.assertNotIn("studies", str(value))


if __name__ == "__main__":
    unittest.main()
