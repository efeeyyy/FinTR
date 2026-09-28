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
    canonical_json,
)
from fintr.data.price_history import DailyBar, PriceHistoryCapture, PriceHistoryCaptureMeta
from fintr.evaluation import symbol_exclusions, symbol_split
from fintr.research.compose_period_capture import CaptureInput, compose_period_capture
from fintr.research.study_spec import parse_study_spec
from fintr.scanner.freshness import build_freshness_report
from fintr.scanner.observe import evaluate_rule_for_symbol
from fintr.scanner.report import RESULT_SCHEMA_V1, RESULT_SCHEMA_V2, build_document, observation_payload
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
    daily_custody_lots=(), captured_at_utc="2027-03-01T00:00:00Z", packet_sha256="0" * 64,
    start=None, end=None,
) -> PeriodCapture:
    dates = tuple(sorted(server_dates))
    return PeriodCapture(
        symbol=symbol, start=start or dates[0], end=end or dates[-1],
        captured_at_utc=captured_at_utc,
        packet_sha256=packet_sha256, server_dates=dates, period_akd_net=(),
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

    def test_composed_capture_future_data_change_never_affects_as_of_result(self):
        # ADR §4: the composed capture is evaluated the same way as a plain
        # one -- evaluate_rule_for_symbol only ever sees
        # truncate_period_capture(composed, as_of). A rolling capture that
        # only adds/changes data strictly after as_of must not move the
        # as_of result, even though it changes the composed object's own
        # composition.unsettled/revisions bookkeeping (which is allowed to
        # look past as_of; see docs/scanner.md).
        rule = _rule(condition=_CUSTODY_CHANGE_GE_ZERO)
        bars = tuple(_bar(d, o=10, h=10, l=10, c=10, adj=10, v=100) for d in D[:3])
        price = _price("TST", bars)
        base = _capture(
            server_dates=D[:2],
            daily_custody_lots=(
                DatedCodeValues(D[0], (CodeValue("AAA", Decimal(100)),)),
                DatedCodeValues(D[1], (CodeValue("AAA", Decimal(150)),)),
            ),
            captured_at_utc="2027-03-01T00:00:00Z", packet_sha256="base",
        )

        def make_composed(d2_value: int) -> PeriodCapture:
            future = _capture(
                server_dates=D[2:3],
                daily_custody_lots=(
                    DatedCodeValues(D[2], (CodeValue("AAA", Decimal(d2_value)),)),
                ),
                captured_at_utc="2027-03-02T00:00:00Z", packet_sha256=f"future-{d2_value}",
            )
            return compose_period_capture([
                CaptureInput(base, kind="base"), CaptureInput(future, kind="rolling"),
            ])

        obs_a = evaluate_rule_for_symbol(rule, make_composed(999), price, as_of=D[1], **_DEFAULT_PARAMS)
        obs_b = evaluate_rule_for_symbol(rule, make_composed(-999), price, as_of=D[1], **_DEFAULT_PARAMS)
        self.assertEqual(obs_a.condition_met, obs_b.condition_met)
        self.assertEqual([f.value for f in obs_a.features], [f.value for f in obs_b.features])
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


def _assert_no_forbidden_keys(testcase: unittest.TestCase, value, forbidden_keys: set[str]) -> None:
    if isinstance(value, dict):
        for key, sub in value.items():
            testcase.assertNotIn(str(key).lower(), forbidden_keys, f"forbidden field: {key}")
            _assert_no_forbidden_keys(testcase, sub, forbidden_keys)
    elif isinstance(value, list):
        for item in value:
            _assert_no_forbidden_keys(testcase, item, forbidden_keys)


class ForbiddenFieldsTests(unittest.TestCase):
    _FORBIDDEN_KEYS = {"score", "rank", "return", "target", "net_return", "gross_return"}

    def _assert_no_forbidden_keys(self, value) -> None:
        _assert_no_forbidden_keys(self, value, self._FORBIDDEN_KEYS)

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
            # ADR §4: without --daily-root, no composition/data_status field
            # is added anywhere -- the shape is exactly what it was before
            # that option existed.
            symbol_block = document["symbols"][DEVELOPMENT[0]]
            self.assertNotIn("composition", symbol_block)
            for scan_entry in symbol_block["scans"]:
                self.assertNotIn("data_status", scan_entry)
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


def _daily_root_capture_document(
    symbol: str, *, start: str, end: str, server_dates: list[str],
    custody_by_date: dict[str, str], captured_at_utc: str,
) -> dict:
    """A minimal, synthetic, hash-valid ``fintr_period_analysis_capture_v1``
    document (same shape ``test_period_analysis``'s ``_packet``/``_capture``
    build, but with caller-chosen dates/values/``captured_at_utc`` so two
    overlapping records can be made to agree, disagree (a revision) or omit
    a day (a gap), as ``compose_period_capture`` requires for its tests)."""
    descending = sorted(server_dates, reverse=True)
    packet = {
        "schema": "fintr_period_analysis_handoff_v1",
        "requested_period": {
            "symbol": symbol, "start_date": start, "end_date": end,
            "origin": "local_cli_arguments",
        },
        "get_akd_period": {"rows_in_provider_order": []},
        "custody_analysis_daily": {
            "provenance": {"server_echo": {"dates_in_provider_order": descending}},
            "daily_akd_flow_in_provider_order": [
                {"date": d, "rows": [
                    {"broker_code": "AAA", "net_size": Decimal("10"),
                     "total_size": Decimal("100"), "total_cost": Decimal("5")},
                ]}
                for d in descending
            ],
            "daily_custody_balance_in_provider_order": [
                {"date": d, "rows": [
                    {"broker_code": "AAA", "lot_count": Decimal(custody_by_date[d])},
                ]}
                for d in descending
            ],
            "metadata_in_provider_order": [
                {"date": d, "null_fields": [], "retail_pct": Decimal("40"),
                 "institutional_pct": Decimal("60")}
                for d in descending
            ],
        },
        "get_custodian_diff_period": {
            "provenance": {"server_echo": {"start_date": start, "end_date": end}},
            "rows_in_provider_order": [],
        },
    }
    return {
        "capture_metadata": {
            "schema": "fintr_period_analysis_capture_v1",
            "captured_at_utc": captured_at_utc,
            "packet_schema": packet["schema"],
            "packet_sha256_canonical_json": hashlib.sha256(
                canonical_json(packet).encode("utf-8")
            ).hexdigest(),
        },
        "packet": packet,
    }


class DailyRootEndToEndTests(GuardEnvMixin, unittest.TestCase):
    """``scan_observations.py --daily-root``, end to end: a base capture plus
    two overlapping daily (rolling-window) captures (ADR §4). All dates are
    business days; ``2026-01-08`` is deliberately never mentioned by any
    capture's own server dates to exercise the gap marker.

    Hand-derived expectations (custody family, symbol AAA=code, lot_count):
      - 2026-01-05, 2026-01-06: only in base -> AKD family unsettled
        (single holder), custody family settled (base's own window already
        has >=2 later server dates).
      - 2026-01-07: base=1000, daily1=1500 (daily1 captured later) ->
        custody revision (daily1 wins); settled (daily1's own window has 2
        later server dates: 01-09, 01-12).
      - 2026-01-09: base/daily1/daily2 all agree (1000) -> no revision;
        settled (daily2's own window has 2 later server dates: 01-12, 01-13).
      - 2026-01-12: daily1=daily2=1300, no revision; unsettled (neither
        capture's own window has 2 later server dates).
      - 2026-01-13: only in daily2 -> unsettled.
      - 2026-01-08: in no capture's server_dates but is a priced trading
        day within [2026-01-05, 2026-01-13] -> gap.
    """

    SYMBOL = DEVELOPMENT[0]
    D_BASE = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-09"]
    D_GAP = "2026-01-08"
    D_DAILY1 = ["2026-01-07", "2026-01-09", "2026-01-12"]
    D_DAILY2 = ["2026-01-09", "2026-01-12", "2026-01-13"]
    ALL_TRADE_DATES = [
        "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09",
        "2026-01-12", "2026-01-13",
    ]

    def _write_fixture_data(self, root: Path) -> tuple[Path, Path, Path]:
        period_root = root / "period_analysis"
        daily_root = root / "daily_capture"
        price_root = root / "price_history"

        base_doc = _daily_root_capture_document(
            self.SYMBOL, start="2026-01-05", end="2026-01-09", server_dates=self.D_BASE,
            custody_by_date={"2026-01-05": "1000", "2026-01-06": "1000",
                              "2026-01-07": "1000", "2026-01-09": "1000"},
            captured_at_utc="2026-01-09T12:00:00Z",
        )
        period_fixtures._write_capture(period_root, self.SYMBOL, "2026-01-05", "2026-01-09", base_doc)

        daily1_doc = _daily_root_capture_document(
            self.SYMBOL, start="2026-01-07", end="2026-01-12", server_dates=self.D_DAILY1,
            custody_by_date={"2026-01-07": "1500", "2026-01-09": "1000", "2026-01-12": "1300"},
            captured_at_utc="2026-01-12T09:00:00Z",
        )
        period_fixtures._write_capture(daily_root, self.SYMBOL, "2026-01-07", "2026-01-12", daily1_doc)

        daily2_doc = _daily_root_capture_document(
            self.SYMBOL, start="2026-01-09", end="2026-01-13", server_dates=self.D_DAILY2,
            custody_by_date={"2026-01-09": "1000", "2026-01-12": "1300", "2026-01-13": "1400"},
            captured_at_utc="2026-01-13T09:00:00Z",
        )
        period_fixtures._write_capture(daily_root, self.SYMBOL, "2026-01-09", "2026-01-13", daily2_doc)

        bars = [
            period_fixtures._price_bar(d, o="10", h="10", l="10", c="10", adj="10", v=100)
            for d in self.ALL_TRADE_DATES
        ]
        period_fixtures._write_price_capture(
            price_root, self.SYMBOL, "2026-01-05", "2026-01-13", bars,
        )
        return period_root, daily_root, price_root

    def _run(self, argv: list[str]) -> tuple[int, dict, str]:
        stdout = io.BytesIO()
        with redirect_stderr(io.StringIO()) as errors, patch.object(
            scan.sys, "stdout", io.TextIOWrapper(stdout, encoding="utf-8"),
        ) as wrapped:
            code = scan.main(argv)
            wrapped.flush()
        text = stdout.getvalue().decode("utf-8")
        document = json.loads(text) if text else {}
        return code, document, errors.getvalue()

    def _argv(self, period_root: Path, daily_root: Path, price_root: Path, **extra) -> list[str]:
        argv = [
            "--symbols", self.SYMBOL,
            "--period-dir", str(period_root),
            "--daily-root", str(daily_root),
            "--price-dir", str(price_root),
            "--rules-file", str(REAL_RULES_FILE),
            "--as-of", "2026-01-12",
            "--lookback-days", "5",  # 01-05, 01-06, 01-07, 01-09, 01-12
            "--format", "json",
        ]
        for key, value in extra.items():
            argv.extend([key, value])
        return argv

    def test_composed_observation_schema_and_composition_block(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            period_root, daily_root, price_root = self._write_fixture_data(root)
            code, document, errors = self._run(self._argv(period_root, daily_root, price_root))
            self.assertEqual(code, 0, errors)
            self.assertEqual(document["schema"], RESULT_SCHEMA_V2)

            block = document["symbols"][self.SYMBOL]
            composition = block["composition"]
            self.assertRegex(composition["composition_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(composition["composer_version"], "compose_period_capture_v1")
            kinds = sorted(item["kind"] for item in composition["inputs"])
            self.assertEqual(kinds, ["base", "rolling", "rolling"])
            for item in composition["inputs"]:
                self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")

            # Gap: 2026-01-08 is priced but in no capture's server_dates.
            self.assertEqual(composition["gaps"]["count"], 1)
            self.assertEqual(composition["gaps"]["dates"], [self.D_GAP])

            # Revision: 2026-01-07 custody differs between base and daily1.
            self.assertEqual(composition["revisions"]["day_count"], 1)
            self.assertIn(
                {"date": "2026-01-07", "family": "custody"}, composition["revisions"]["entries"],
            )

            # Unsettled: at least 2026-01-12 (custody, neither capture's own
            # window reaches 2 server dates past it) and 2026-01-13/akd
            # single-holder dates are not scanned (only up to 01-12 here).
            unsettled_dates = {e["date"] for e in composition["unsettled"]["entries"]}
            self.assertIn("2026-01-12", unsettled_dates)
            self.assertIn({"date": "2026-01-12", "family": "custody"},
                          composition["unsettled"]["entries"])

    def test_window_counts_exclude_base_only_days_before_first_rolling_date(self):
        # Rolling captures start 2026-01-07: base-only 01-05/01-06 stay in the
        # composer's totals (unsettled: akd single-holder) but not the window.
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            period_root, daily_root, price_root = self._write_fixture_data(root)
            code, document, errors = self._run(self._argv(period_root, daily_root, price_root))
            self.assertEqual(code, 0, errors)
            composition = document["symbols"][self.SYMBOL]["composition"]
            self.assertEqual(composition["window"], {"start": "2026-01-07", "end": "2026-01-12"})
            total_days = {e["date"] for e in composition["unsettled"]["entries"]}
            self.assertLessEqual({"2026-01-05", "2026-01-06"}, total_days)
            self.assertEqual(composition["unsettled"]["day_count"], len(total_days))  # unchanged
            in_window = {d for d in total_days if "2026-01-07" <= d <= "2026-01-12"}
            self.assertEqual(composition["unsettled"]["in_window_day_count"], len(in_window))
            self.assertLess(len(in_window), len(total_days))
            self.assertEqual(composition["revisions"]["in_window_day_count"], 1)  # 01-07
            self.assertEqual(composition["gaps"]["in_window_count"], 1)  # 01-08

    def test_window_includes_single_observation_day_inside_window(self):
        # 2026-01-12 is unsettled and inside the window.
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            period_root, daily_root, price_root = self._write_fixture_data(root)
            code, document, errors = self._run(self._argv(period_root, daily_root, price_root))
            self.assertEqual(code, 0, errors)
            composition = document["symbols"][self.SYMBOL]["composition"]
            self.assertIn("2026-01-12", {e["date"] for e in composition["unsettled"]["entries"]})
            self.assertGreaterEqual(composition["unsettled"]["in_window_day_count"], 1)

    def test_window_excludes_dates_after_as_of(self):
        # as_of 01-12: 01-13 (only in daily2, unsettled) is in totals, not window.
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            period_root, daily_root, price_root = self._write_fixture_data(root)
            code, document, errors = self._run(self._argv(period_root, daily_root, price_root))
            self.assertEqual(code, 0, errors)
            composition = document["symbols"][self.SYMBOL]["composition"]
            entries = {e["date"] for e in composition["unsettled"]["entries"]}
            self.assertIn("2026-01-13", entries)
            self.assertEqual(composition["window"]["end"], "2026-01-12")
            self.assertEqual(
                composition["unsettled"]["in_window_day_count"],
                len({d for d in entries if "2026-01-07" <= d <= "2026-01-12"}),
            )

    def test_window_and_in_window_counts_null_without_rolling_inputs(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            period_root, _daily_root, price_root = self._write_fixture_data(root)
            empty_daily_root = root / "empty_daily_capture"
            code, document, errors = self._run(
                self._argv(period_root, empty_daily_root, price_root)
            )
            self.assertEqual(code, 0, errors)
            composition = document["symbols"][self.SYMBOL]["composition"]
            self.assertIsNone(composition["window"])
            self.assertIsNone(composition["gaps"]["in_window_count"])
            self.assertIsNone(composition["unsettled"]["in_window_day_count"])
            self.assertIsNone(composition["revisions"]["in_window_day_count"])
            self.assertGreater(composition["unsettled"]["day_count"], 0)  # totals kept

    def test_data_status_marks_revised_and_unsettled_as_of_days_not_suppressed(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            period_root, daily_root, price_root = self._write_fixture_data(root)
            code, document, errors = self._run(self._argv(period_root, daily_root, price_root))
            self.assertEqual(code, 0, errors)
            scans_by_as_of = {
                scan_entry["as_of"]: scan_entry
                for scan_entry in document["symbols"][self.SYMBOL]["scans"]
            }

            revised_scan = scans_by_as_of["2026-01-07"]
            self.assertIn("custody", revised_scan["data_status"]["revised_families"])
            # Marked, never suppressed: the revised as_of day still gets one
            # observation per loaded rule, exactly like every other day --
            # data_status is an addition, not a replacement for the normal
            # per-rule observation list.
            self.assertEqual(
                {obs["rule_id"] for obs in revised_scan["observations"]},
                {obs["rule_id"] for obs in scans_by_as_of["2026-01-09"]["observations"]},
            )

            unsettled_scan = scans_by_as_of["2026-01-12"]
            self.assertIn("custody", unsettled_scan["data_status"]["unsettled_families"])

            settled_clean_scan = scans_by_as_of["2026-01-09"]
            self.assertEqual(settled_clean_scan["data_status"]["revised_families"], [])
            self.assertEqual(settled_clean_scan["data_status"]["unsettled_families"], [])

    def test_default_run_without_daily_root_unaffected_by_daily_root_data(self):
        # Same fixture, but --daily-root omitted: must behave exactly like
        # ScriptEndToEndTests (v1 schema, no composition/data_status), even
        # though daily_capture/ data exists on disk right next to period_dir.
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            period_root, daily_root, price_root = self._write_fixture_data(root)
            argv = [
                "--symbols", self.SYMBOL,
                "--period-dir", str(period_root),
                "--price-dir", str(price_root),
                "--rules-file", str(REAL_RULES_FILE),
                "--as-of", "2026-01-09",
                "--format", "json",
            ]
            code, document, errors = self._run(argv)
            self.assertEqual(code, 0, errors)
            self.assertEqual(document["schema"], RESULT_SCHEMA_V1)
            block = document["symbols"][self.SYMBOL]
            self.assertNotIn("composition", block)
            for scan_entry in block["scans"]:
                self.assertNotIn("data_status", scan_entry)

    def test_empty_daily_root_directory_still_composes_from_base_alone(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            period_root, _daily_root, price_root = self._write_fixture_data(root)
            empty_daily_root = root / "empty_daily_capture"
            argv = self._argv(period_root, empty_daily_root, price_root)
            code, document, errors = self._run(argv)
            self.assertEqual(code, 0, errors)
            composition = document["symbols"][self.SYMBOL]["composition"]
            self.assertEqual(len(composition["inputs"]), 1)
            self.assertEqual(composition["inputs"][0]["kind"], "base")


class ForbiddenFieldsComposedTests(unittest.TestCase):
    _FORBIDDEN_KEYS = ForbiddenFieldsTests._FORBIDDEN_KEYS

    def test_composed_document_has_no_forbidden_fields(self):
        base = _capture(
            server_dates=D[:2],
            daily_custody_lots=(
                DatedCodeValues(D[0], (CodeValue("AAA", Decimal(100)),)),
                DatedCodeValues(D[1], (CodeValue("AAA", Decimal(150)),)),
            ),
            captured_at_utc="2027-03-01T00:00:00Z", packet_sha256="base",
        )
        rolling = _capture(
            server_dates=D[1:2],
            daily_custody_lots=(
                DatedCodeValues(D[1], (CodeValue("AAA", Decimal(999)),)),
            ),
            captured_at_utc="2027-03-02T00:00:00Z", packet_sha256="rolling",
        )
        composed = compose_period_capture([
            CaptureInput(base, kind="base"), CaptureInput(rolling, kind="rolling"),
        ])
        rule = _rule(condition=_CUSTODY_CHANGE_GE_ZERO)
        bars = tuple(_bar(d, o=10, h=10, l=10, c=10, adj=10, v=100) for d in D[:2])
        price = _price("TST", bars)
        obs = evaluate_rule_for_symbol(rule, composed, price, as_of=D[1], **_DEFAULT_PARAMS)
        payload = observation_payload(obs)
        _assert_no_forbidden_keys(self, payload, ForbiddenFieldsTests._FORBIDDEN_KEYS)
        lineage = composed.composition
        composition_payload = {
            "composition_sha256": lineage.composition_sha256,
            "gaps": {"count": len(lineage.gaps), "dates": [d.isoformat() for d in lineage.gaps]},
            "unsettled": {"entries": [
                {"date": e.date.isoformat(), "family": e.family} for e in lineage.unsettled
            ]},
            "revisions": {"entries": [
                {"date": e.date.isoformat(), "family": e.family} for e in lineage.revisions
            ]},
        }
        _assert_no_forbidden_keys(self, composition_payload, ForbiddenFieldsTests._FORBIDDEN_KEYS)


if __name__ == "__main__":
    unittest.main()
