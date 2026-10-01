"""Offline tests for the daily-collection window planner and its dry-run CLI.

No network, no Telegram, no model call, no `initData` prompt anywhere in
this file. All splits, exclusions and run records are synthetic and written
to a temporary directory (via ``test_symbol_guard.GuardEnvMixin``); the
repository's real ``configs/`` files are never read except by the one test
that exercises the real ``configs/collection/daily_miniapp_v1.json``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path

from fintr.collection.planner import (
    CollectionPlannerError,
    CollectionWindow,
    DEFAULT_LOOKBACK_CALENDAR_DAYS,
    STATUS_BLOCKED_MAX_SPAN_EXCEEDED,
    STATUS_PLANNED,
    STATUS_SKIPPED_EXCLUDED,
    STATUS_SKIPPED_HOLDOUT,
    base_capture_end_date,
    config_sha256,
    load_daily_collection_config,
    plan_daily_collection,
    scan_base_capture_ends,
    scan_last_success,
)
from fintr.evaluation.symbol_guard import load_active_guards

import test_symbol_guard as guard_fixtures


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"planner_test_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collect_daily = _load_script("collect_daily")


class GuardEnv(guard_fixtures.GuardEnvMixin, unittest.TestCase):
    """Base class giving every test in this file ``self.make_env()``."""


class WindowRuleTests(GuardEnv):
    def test_basic_window_is_r_minus_one_minus_fourteen(self):
        self.make_env()
        guards = load_active_guards()
        plan = plan_daily_collection(run_date=date(2027, 2, 10), guards=guards)
        self.assertEqual(plan.window_end, date(2027, 2, 9))
        dev = {entry.symbol: entry for entry in plan.symbols if entry.role == "development"}
        window = dev["DEVA"].window
        self.assertEqual(window.end, date(2027, 2, 9))
        self.assertEqual(window.start, date(2027, 1, 26))
        self.assertFalse(window.catch_up)
        self.assertEqual(dev["DEVA"].status, STATUS_PLANNED)

    def test_weekend_or_holiday_run_date_uses_plain_calendar_arithmetic(self):
        # 2027-02-14 is a Sunday; the planner has no trading-calendar
        # awareness of R itself (only the 14-day lookback absorbs
        # weekends/holidays), so E/S are still plain day arithmetic.
        self.make_env()
        guards = load_active_guards()
        plan = plan_daily_collection(run_date=date(2027, 2, 14), guards=guards)
        self.assertEqual(plan.window_end, date(2027, 2, 13))
        dev = {entry.symbol: entry for entry in plan.symbols if entry.role == "development"}
        self.assertEqual(dev["DEVA"].window.start, date(2027, 1, 30))

    def test_catch_up_extends_start_to_last_success_minus_lookback(self):
        self.make_env()
        guards = load_active_guards()
        last_success = {"DEVA": date(2027, 1, 1)}
        plan = plan_daily_collection(
            run_date=date(2027, 2, 10), guards=guards, last_success_by_symbol=last_success,
        )
        dev = {entry.symbol: entry for entry in plan.symbols if entry.role == "development"}
        window = dev["DEVA"].window
        # base start would be 2027-01-26; catch-up start is 2026-12-18
        # (2027-01-01 - 14d), which is earlier, so it wins (min).
        self.assertEqual(window.start, date(2026, 12, 18))
        self.assertTrue(window.catch_up)
        # A symbol with no recorded last success keeps the plain window.
        self.assertFalse(dev["DEVB"].window.catch_up)
        self.assertEqual(dev["DEVB"].window.start, date(2027, 1, 26))

    def test_catch_up_never_shrinks_the_window(self):
        # A very recent last_success (closer than the default lookback)
        # must not pull the start forward past the base window.
        self.make_env()
        guards = load_active_guards()
        last_success = {"DEVA": date(2027, 2, 9)}
        plan = plan_daily_collection(
            run_date=date(2027, 2, 10), guards=guards, last_success_by_symbol=last_success,
        )
        dev = {entry.symbol: entry for entry in plan.symbols if entry.role == "development"}
        self.assertEqual(dev["DEVA"].window.start, date(2027, 1, 26))
        self.assertFalse(dev["DEVA"].window.catch_up)

    def test_span_over_max_is_blocked_not_silently_widened(self):
        self.make_env()
        guards = load_active_guards()
        # last success far enough back that E - S > 90 days.
        last_success = {"DEVA": date(2026, 10, 1)}
        plan = plan_daily_collection(
            run_date=date(2027, 2, 10), guards=guards, last_success_by_symbol=last_success,
        )
        dev = {entry.symbol: entry for entry in plan.symbols if entry.role == "development"}
        self.assertEqual(dev["DEVA"].status, STATUS_BLOCKED_MAX_SPAN_EXCEEDED)
        self.assertIsNone(dev["DEVA"].window)
        self.assertIn(dev["DEVA"], plan.blocked())
        # An unaffected symbol still plans normally in the same run.
        self.assertEqual(dev["DEVB"].status, STATUS_PLANNED)

    def test_span_exactly_at_max_is_not_blocked(self):
        self.make_env()
        guards = load_active_guards()
        run_date = date(2027, 2, 10)
        window_end = run_date - timedelta(days=1)
        # catch_up_start = last_success - 14d; want start = window_end - 90d,
        # so last_success = window_end - 90d + 14d = window_end - 76d.
        last_success = {"DEVA": window_end - timedelta(days=76)}
        plan = plan_daily_collection(
            run_date=run_date, guards=guards, last_success_by_symbol=last_success,
        )
        dev = {entry.symbol: entry for entry in plan.symbols if entry.role == "development"}
        self.assertEqual(dev["DEVA"].status, STATUS_PLANNED)
        self.assertEqual(dev["DEVA"].window.span_days, 90)

    def test_invalid_window_raises(self):
        with self.assertRaises(CollectionPlannerError):
            CollectionWindow(start=date(2027, 1, 2), end=date(2027, 1, 1), catch_up=False)


class ScopeSkipTests(GuardEnv):
    def test_holdout_symbols_are_reported_skipped_not_dropped(self):
        self.make_env()
        guards = load_active_guards()
        plan = plan_daily_collection(run_date=date(2027, 2, 10), guards=guards)
        holdout = {entry.symbol: entry for entry in plan.symbols if entry.role == "holdout"}
        self.assertEqual(set(holdout), set(guard_fixtures.HOLDOUT))
        for entry in holdout.values():
            self.assertEqual(entry.status, STATUS_SKIPPED_HOLDOUT)
            self.assertIsNone(entry.window)

    def test_excluded_symbol_outside_the_split_is_still_reported(self):
        # EXCLX (guard_fixtures.EXCLUDED) is not a member of the synthetic
        # split at all -- the planner must still surface it, the way DSTKF
        # (excluded, dropped from the real split pool) must be surfaced.
        self.make_env()
        guards = load_active_guards()
        plan = plan_daily_collection(run_date=date(2027, 2, 10), guards=guards)
        excluded = {entry.symbol: entry for entry in plan.symbols if entry.role == "excluded"}
        self.assertEqual(set(excluded), {guard_fixtures.EXCLUDED})
        self.assertEqual(excluded[guard_fixtures.EXCLUDED].status, STATUS_SKIPPED_EXCLUDED)
        self.assertIsNone(excluded[guard_fixtures.EXCLUDED].window)


class ConfigLoaderTests(unittest.TestCase):
    def test_real_config_file_loads_and_matches_the_adr(self):
        path = ROOT / "configs" / "collection" / "daily_miniapp_v1.json"
        config = load_daily_collection_config(path)
        self.assertEqual(config.symbol_scope, "split_development_only")
        self.assertEqual(config.end_offset_days, 1)
        self.assertEqual(config.lookback_calendar_days, DEFAULT_LOOKBACK_CALENDAR_DAYS)
        self.assertEqual(config.max_span_days, 90)
        self.assertEqual(config.stop_on_http_status, (429,))
        self.assertEqual(config_sha256(path), hashlib.sha256(path.read_bytes()).hexdigest())

    def test_missing_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps({"schema": "wrong"}), encoding="utf-8")
            with self.assertRaisesRegex(CollectionPlannerError, "invalid_config_schema"):
                load_daily_collection_config(path)

    def test_missing_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps({"schema": "fintr_daily_collection_config_v1"}), encoding="utf-8")
            with self.assertRaisesRegex(CollectionPlannerError, "invalid_config_field"):
                load_daily_collection_config(path)


class RunRecordScanTests(unittest.TestCase):
    def _write(self, directory: Path, name: str, document: dict) -> None:
        (directory / name).write_text(json.dumps(document), encoding="utf-8")

    def test_missing_directory_returns_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(scan_last_success(Path(directory) / "nope"), {})

    def test_latest_saved_window_end_wins_per_symbol(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "run1.json", {
                "schema": "fintr_daily_collection_run_v1",
                "symbols": [
                    {"symbol": "DEVA", "status": "saved",
                     "window": {"start": "2027-01-01", "end": "2027-01-14"}},
                ],
            })
            self._write(root, "run2.json", {
                "schema": "fintr_daily_collection_run_v1",
                "symbols": [
                    {"symbol": "DEVA", "status": "saved",
                     "window": {"start": "2027-01-15", "end": "2027-01-28"}},
                    {"symbol": "DEVB", "status": "failed:connection_error",
                     "window": {"start": "2027-01-15", "end": "2027-01-28"}},
                ],
            })
            latest = scan_last_success(root)
            self.assertEqual(latest, {"DEVA": date(2027, 1, 28)})

    def test_malformed_and_wrong_schema_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "not_json.json").write_text("{not json", encoding="utf-8")
            self._write(root, "other_schema.json", {"schema": "other", "symbols": []})
            self._write(root, "bad_window.json", {
                "schema": "fintr_daily_collection_run_v1",
                "symbols": [{"symbol": "DEVA", "status": "saved", "window": {"end": "not-a-date"}}],
            })
            self.assertEqual(scan_last_success(root), {})


class BaseCaptureFloorTests(GuardEnv):
    """Base one-year capture end as a last-success floor (catch-up gap fix)."""

    BASE_END = date(2026, 9, 25)
    BASE_FILE = "2025-09-26_2026-09-25.json"

    def _dev(self, plan):
        return {entry.symbol: entry for entry in plan.symbols if entry.role == "development"}

    def test_floor_reaches_back_to_the_base_end_on_the_first_run(self):
        self.make_env()
        guards = load_active_guards()
        plan = plan_daily_collection(
            run_date=date(2026, 10, 1), guards=guards,
            base_capture_end_by_symbol={"DEVA": self.BASE_END},
        )
        window = self._dev(plan)["DEVA"].window
        self.assertEqual(window.start, date(2026, 9, 11))  # base end - 14d
        self.assertLessEqual(window.start, self.BASE_END)
        self.assertTrue(window.catch_up)

    def test_first_success_long_after_the_base_end_leaves_no_gap(self):
        # Without the floor S = E - 14d = 2026-10-05 here, and 2026-09-26..
        # 2026-10-04 would never be collected.
        self.make_env()
        guards = load_active_guards()
        plan = plan_daily_collection(
            run_date=date(2026, 10, 20), guards=guards,
            base_capture_end_by_symbol={"DEVA": self.BASE_END},
        )
        dev = self._dev(plan)
        self.assertEqual(dev["DEVA"].status, STATUS_PLANNED)
        self.assertEqual(dev["DEVA"].window.start, date(2026, 9, 11))
        self.assertLessEqual(dev["DEVA"].window.start, self.BASE_END)
        self.assertEqual(dev["DEVB"].window.start, date(2026, 10, 5))  # no floor: unchanged

    def test_floor_is_ignored_for_a_symbol_without_a_base_capture(self):
        self.make_env()
        guards = load_active_guards()
        with_floor = plan_daily_collection(
            run_date=date(2026, 10, 20), guards=guards,
            base_capture_end_by_symbol={"DEVA": self.BASE_END},
        )
        without = plan_daily_collection(run_date=date(2026, 10, 20), guards=guards)
        self.assertEqual(self._dev(with_floor)["DEVB"], self._dev(without)["DEVB"])
        self.assertFalse(self._dev(without)["DEVB"].window.catch_up)

    def test_later_of_run_record_success_and_base_end_wins(self):
        self.make_env()
        guards = load_active_guards()
        later_success = plan_daily_collection(
            run_date=date(2026, 10, 20), guards=guards,
            last_success_by_symbol={"DEVA": date(2026, 10, 10)},
            base_capture_end_by_symbol={"DEVA": self.BASE_END},
        )
        # last end 2026-10-10 -> catch-up start 2026-09-26 (earlier than 10-05).
        self.assertEqual(self._dev(later_success)["DEVA"].window.start, date(2026, 9, 26))
        earlier_success = plan_daily_collection(
            run_date=date(2026, 10, 20), guards=guards,
            last_success_by_symbol={"DEVA": date(2026, 9, 1)},
            base_capture_end_by_symbol={"DEVA": self.BASE_END},
        )
        # A run-record success older than the base end is ignored in favor of it.
        self.assertEqual(self._dev(earlier_success)["DEVA"].window.start, date(2026, 9, 11))

    def test_ninety_day_human_decision_rule_is_unchanged(self):
        self.make_env()
        guards = load_active_guards()
        plan = plan_daily_collection(
            run_date=date(2027, 1, 10), guards=guards,
            base_capture_end_by_symbol={"DEVA": self.BASE_END},
        )
        dev = self._dev(plan)
        self.assertEqual(dev["DEVA"].status, STATUS_BLOCKED_MAX_SPAN_EXCEEDED)
        self.assertEqual(dev["DEVB"].status, STATUS_PLANNED)

    def test_scan_reports_only_symbols_whose_base_file_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "DEVA").mkdir()
            (root / "DEVA" / self.BASE_FILE).write_text("{}", encoding="utf-8")
            (root / "DEVC").mkdir()
            (root / "DEVC" / "2025-09-26_2026-09-24.json").write_text("{}", encoding="utf-8")
            floors = scan_base_capture_ends(
                root, self.BASE_FILE, ["DEVA", "DEVB", "DEVC", "../DEVA", "DEVA/.."],
            )
        self.assertEqual(floors, {"DEVA": self.BASE_END})

    def test_scan_rejects_a_filename_without_a_valid_date_range(self):
        for bad in ("base.json", "2026-09-25_2025-09-26.json", "2025-13-01_2026-09-25.json",
                    "2025-09-26_2026-09-25.json.bak", None):
            with self.subTest(bad=bad):
                with self.assertRaises(CollectionPlannerError):
                    scan_base_capture_ends(Path("."), bad, ["DEVA"])

    def test_real_config_names_the_base_capture_and_its_end_date(self):
        config = load_daily_collection_config(
            ROOT / "configs" / "collection" / "daily_miniapp_v1.json",
        )
        self.assertEqual(config.base_capture_root, "data/interim/period_analysis")
        self.assertEqual(config.base_capture_filename, self.BASE_FILE)
        self.assertEqual(base_capture_end_date(config.base_capture_filename), self.BASE_END)

    def _config_document(self, **overrides):
        document = {
            "schema": "fintr_daily_collection_config_v1",
            "source_id": "synthetic",
            "symbol_scope": "split_development_only",
            "split_config": "configs/splits/current.json",
            "exclusions_config": "configs/exclusions/symbols.json",
            "window": {"end_offset_days": 1, "lookback_calendar_days": 14, "max_span_days": 90},
            "pacing": {"symbol_pause_seconds": 30, "max_wall_seconds": 3600},
            "capture_root": "unused",
            "permission_status": "user_asserted_2026-09-28_not_independently_verified",
            "stop_on_http_status": [429],
        }
        document.update(overrides)
        return document

    def test_config_without_base_capture_loads_with_no_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(self._config_document()), encoding="utf-8")
            config = load_daily_collection_config(path)
        self.assertIsNone(config.base_capture_root)
        self.assertIsNone(config.base_capture_filename)

    def test_config_rejects_malformed_base_capture(self):
        bad_values = (
            "not-an-object",
            {"root": "data", "filename": "base.json"},
            {"root": "", "filename": self.BASE_FILE},
            {"filename": self.BASE_FILE},
        )
        for bad in bad_values:
            with self.subTest(bad=bad):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    path.write_text(
                        json.dumps(self._config_document(base_capture=bad)), encoding="utf-8",
                    )
                    with self.assertRaisesRegex(CollectionPlannerError, "invalid_config_field"):
                        load_daily_collection_config(path)

    def test_dry_run_plans_from_the_base_end_only_where_the_file_exists(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            base_root = Path(directory) / "base"
            (base_root / "DEVA").mkdir(parents=True)
            (base_root / "DEVA" / self.BASE_FILE).write_text("{}", encoding="utf-8")
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(self._config_document(
                capture_root=str(Path(directory) / "daily_capture"),
                base_capture={"root": str(base_root), "filename": self.BASE_FILE},
            )), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                exit_code = collect_daily.main([
                    "--dry-run", "--format", "json", "--config", str(config_path),
                    "--run-date", "2026-10-20",
                ])
            self.assertEqual(exit_code, 0, err.getvalue())
        record = json.loads(out.getvalue())
        windows = {
            entry["symbol"]: entry["window"] for entry in record["symbols"]
            if entry["role"] == "development"
        }
        self.assertEqual(windows["DEVA"], {
            "start": "2026-09-11", "end": "2026-10-19", "catch_up": True,
        })
        self.assertEqual(windows["DEVB"], {
            "start": "2026-10-05", "end": "2026-10-19", "catch_up": False,
        })


class DryRunCliTests(GuardEnv):
    def test_dry_run_prints_plan_and_never_touches_network_or_secrets(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({
                "schema": "fintr_daily_collection_config_v1",
                "source_id": "synthetic",
                "symbol_scope": "split_development_only",
                "split_config": "configs/splits/current.json",
                "exclusions_config": "configs/exclusions/symbols.json",
                "window": {"end_offset_days": 1, "lookback_calendar_days": 14, "max_span_days": 90},
                "pacing": {"symbol_pause_seconds": 30, "max_wall_seconds": 3600},
                "capture_root": str(Path(directory) / "daily_capture"),
                "permission_status": "user_asserted_2026-09-28_not_independently_verified",
                "stop_on_http_status": [429],
            }), encoding="utf-8")

            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                exit_code = collect_daily.main([
                    "--dry-run", "--config", str(config_path), "--run-date", "2027-02-10",
                ])
            self.assertEqual(exit_code, 0, err.getvalue())
        text = out.getvalue()
        self.assertIn("Dry run: no connection opened, no initData prompted.", text)
        self.assertIn('"status": "planned"', text)
        self.assertIn("DEVA", text)
        self.assertIn("skipped_holdout", text)
        self.assertNotIn("wss://", text)

    def test_without_dry_run_or_takeover_flag_exits_before_any_prompt(self):
        """PR2: the live path exists now, but still requires explicit
        --allow-session-takeover before any initData prompt or connection
        (mirrors live_collector_smoke.py's own gate)."""
        self.make_env()
        err = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stderr(err):
            collect_daily.main(["--run-date", "2027-02-10"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--allow-session-takeover", err.getvalue())

    def test_blocked_symbol_exits_nonzero_and_names_it(self):
        self.make_env()
        with tempfile.TemporaryDirectory() as directory:
            run_records_dir = Path(directory) / "daily_capture" / "_runs"
            run_records_dir.mkdir(parents=True)
            (run_records_dir / "old_run.json").write_text(json.dumps({
                "schema": "fintr_daily_collection_run_v1",
                "symbols": [
                    {"symbol": "DEVA", "status": "saved",
                     "window": {"start": "2026-10-01", "end": "2026-10-01"}},
                ],
            }), encoding="utf-8")
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({
                "schema": "fintr_daily_collection_config_v1",
                "source_id": "synthetic",
                "symbol_scope": "split_development_only",
                "split_config": "configs/splits/current.json",
                "exclusions_config": "configs/exclusions/symbols.json",
                "window": {"end_offset_days": 1, "lookback_calendar_days": 14, "max_span_days": 90},
                "pacing": {"symbol_pause_seconds": 30, "max_wall_seconds": 3600},
                "capture_root": str(Path(directory) / "daily_capture"),
                "permission_status": "user_asserted_2026-09-28_not_independently_verified",
                "stop_on_http_status": [429],
            }), encoding="utf-8")

            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                exit_code = collect_daily.main([
                    "--dry-run", "--config", str(config_path), "--run-date", "2027-02-10",
                ])
            self.assertEqual(exit_code, 2)
            self.assertIn("DEVA", err.getvalue())


if __name__ == "__main__":
    unittest.main()
