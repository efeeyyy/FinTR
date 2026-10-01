"""Offline tests for the daily price step of ``scripts/collect_daily.py`` (ADR PR5).

No network: ``fetch._fetch_raw`` is patched with the synthetic chart fixture
(or a failure) and ``price_batch.time`` is patched so pacing never waits.
The Mini App step is either skipped (its capture files pre-created) or driven
by the same mocked collector the live-executor tests use; splits/exclusions
are synthetic. Fixture prices (10.2, 10.35, ...) must never reach a run record.
"""

from __future__ import annotations

import hashlib
import io
import json
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fintr.collector.models import AttemptDiagnostic
from fintr.evaluation.live_capture import validate_context_contains_no_secrets

import test_daily_collection_live as live
import test_symbol_guard as guard_fixtures


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "market" / "price_history_chart.json"

collect_daily = live.collect_daily
price_batch = collect_daily.price_batch

# Fixture bars are 2026-01-05..09; lookback 9 => Mini App window 2025-12-31..01-09.
RUN_DATE = date(2026, 1, 10)
MINI_START, MINI_END = date(2025, 12, 31), date(2026, 1, 9)
PRICE_START = date(2026, 1, 5)
PRICE_NAME = f"{PRICE_START.isoformat()}_{MINI_END.isoformat()}.json"
FIXTURE_VALUES = ("10.2", "10.35", "10.6", "1100000")


def _price_config(directory: Path, **price_overrides) -> Path:
    price = {"start_date": PRICE_START.isoformat(), "pacing_seconds": 1.5}
    price.update(price_overrides)
    return live._config(directory, price_history=price)


class PriceStepTests(guard_fixtures.GuardEnvMixin, unittest.TestCase):
    def _env(self, development=("AKFIS", "ZZZZZ")):
        split = guard_fixtures._split_doc(
            development_symbols=list(development), holdout_symbols=["HOLDA", "HOLDB"],
            pool_size=len(development) + 2,
        )
        return self.make_env(split=split)

    def _precreate_mini_app(self, root: Path, symbols) -> None:
        for symbol in symbols:
            path = (
                root / "data" / "interim" / "daily_capture" / symbol
                / f"{MINI_START.isoformat()}_{MINI_END.isoformat()}.json"
            )
            path.parent.mkdir(parents=True)
            path.write_text("{}", encoding="utf-8")

    def _price_dir(self, root: Path, symbol: str) -> Path:
        return root / "data" / "interim" / "price_history" / symbol

    def _run(self, root, argv, *, fetch_side_effect=None, mini_app_plan=(), today=None):
        fetch_mock = MagicMock(side_effect=fetch_side_effect or (lambda url, timeout: FIXTURE.read_bytes()))
        factory, calls = live._collector_factory(mini_app_plan)
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(collect_daily, "PROJECT_ROOT", root),
            patch.object(collect_daily.smoke, "PROJECT_ROOT", root),
            patch.object(price_batch.fetch, "PROJECT_ROOT", root),
            patch.object(price_batch.fetch, "_today", return_value=today or RUN_DATE),
            patch.object(price_batch.fetch, "_fetch_raw", fetch_mock),
            patch.object(price_batch, "time") as time_mock,
            patch.object(collect_daily.smoke, "WebSocketCollector") as collector,
            patch.object(
                collect_daily.smoke, "InteractiveSecretProvider",
                return_value=MagicMock(get_init_data=MagicMock(
                    return_value="query_id=1&auth_date=100&hash=abc")),
            ),
            redirect_stdout(out), redirect_stderr(err),
        ):
            collector.side_effect = factory
            code = collect_daily.main(argv)
        return code, out.getvalue(), err.getvalue(), fetch_mock, time_mock, calls

    def _live_argv(self, root, *extra, **price_overrides):
        config = _price_config(root, **price_overrides)
        return [
            "--allow-session-takeover", "--config", str(config),
            "--run-date", RUN_DATE.isoformat(), *extra,
        ]

    def _record(self, root: Path) -> dict:
        files = list((root / "data" / "interim" / "daily_capture" / "_runs").glob("*.json"))
        self.assertEqual(len(files), 1, files)
        return json.loads(files[0].read_text(encoding="utf-8"))

    # -- saving + record ----------------------------------------------------------

    def test_saves_price_files_and_records_hashes_counts_never_values(self):
        root = self._env()
        self._precreate_mini_app(root, ("AKFIS", "ZZZZZ"))
        code, out, err, fetch_mock, time_mock, calls = self._run(root, self._live_argv(root))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 0)  # Mini App step skipped_existing
        self.assertEqual(fetch_mock.call_count, 2)
        time_mock.sleep.assert_called_once_with(1.5)
        record = self._record(root)
        price = record["price_history"]
        self.assertEqual(price["status"], "completed")
        self.assertEqual(price["window"], {"start": "2026-01-05", "end": "2026-01-09"})
        by_symbol = {entry["symbol"]: entry for entry in price["symbols"]}
        self.assertEqual(sorted(by_symbol), ["AKFIS", "ZZZZZ"])  # development only
        for symbol, entry in by_symbol.items():
            path = self._price_dir(root, symbol) / PRICE_NAME
            self.assertTrue(path.is_file())
            self.assertEqual(entry["status"], "saved")
            self.assertEqual(entry["file_path"], f"data/interim/price_history/{symbol}/{PRICE_NAME}")
            self.assertEqual(entry["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(entry["bar_count"], 5)
        self.assertEqual(sorted(price["files"]), sorted(e["file_path"] for e in by_symbol.values()))
        self.assertEqual(price["permission_status"], price_batch.fetch.PERMISSION_STATUS)
        validate_context_contains_no_secrets(record)
        text = json.dumps(record)
        for forbidden in FIXTURE_VALUES + ("query_id", "https://", "wss://"):
            self.assertNotIn(forbidden, text)
        for forbidden in FIXTURE_VALUES:
            self.assertNotIn(forbidden, out)

    def test_holdout_and_excluded_symbols_are_never_requested(self):
        root = self._env()
        self._precreate_mini_app(root, ("AKFIS", "ZZZZZ"))
        _code, _out, _err, fetch_mock, _t, _c = self._run(root, self._live_argv(root))
        urls = " ".join(call.args[0] for call in fetch_mock.call_args_list)
        for symbol in ("HOLDA", "HOLDB", guard_fixtures.EXCLUDED):
            self.assertNotIn(symbol, urls)
        for symbol in ("HOLDA", "HOLDB", guard_fixtures.EXCLUDED):
            self.assertFalse(self._price_dir(root, symbol).exists())

    def test_price_scope_guard_rejects_a_holdout_or_excluded_planned_symbol(self):
        root = self._env()
        loaded = collect_daily.load_daily_collection_config(live._config(root))
        guards = collect_daily.load_active_guards()
        settings = collect_daily._price_settings(loaded)
        for bad in ("HOLDA", guard_fixtures.EXCLUDED):
            forged = SimpleNamespace(
                planned=lambda bad=bad: (SimpleNamespace(symbol=bad),), window_end=MINI_END,
            )
            with self.assertRaisesRegex(collect_daily.CollectionPlannerError, "price_scope_violation"):
                collect_daily.build_price_plan(
                    forged, guards, settings, skip_price=False, today=RUN_DATE,
                )

    # -- no-overwrite, failure, 429 --------------------------------------------------

    def test_existing_target_is_never_overwritten(self):
        root = self._env()
        self._precreate_mini_app(root, ("AKFIS", "ZZZZZ"))
        existing = self._price_dir(root, "ZZZZZ") / PRICE_NAME
        existing.parent.mkdir(parents=True)
        original = json.dumps({"normalized": {"bar_count": 3}})
        existing.write_text(original, encoding="utf-8")
        code, _out, err, fetch_mock, _t, _c = self._run(root, self._live_argv(root))
        self.assertEqual(code, 0, err)
        self.assertEqual(fetch_mock.call_count, 1)
        self.assertEqual(existing.read_text(encoding="utf-8"), original)
        by_symbol = {e["symbol"]: e for e in self._record(root)["price_history"]["symbols"]}
        self.assertEqual(by_symbol["ZZZZZ"]["status"], "skipped_exists")
        self.assertEqual(by_symbol["ZZZZZ"]["bar_count"], 3)
        self.assertEqual(
            by_symbol["ZZZZZ"]["sha256"], hashlib.sha256(original.encode()).hexdigest(),
        )
        self.assertEqual(by_symbol["AKFIS"]["status"], "saved")

    def test_symbol_failure_continues_and_is_categorised_without_leaking_detail(self):
        def side_effect(url, timeout):
            if "AKFIS" in url:
                raise price_batch.fetch.FetchError(
                    "fetch_failed_after_2_attempts: network_error: https://secret.example/x?hash=abc"
                )
            return FIXTURE.read_bytes()

        root = self._env()
        self._precreate_mini_app(root, ("AKFIS", "ZZZZZ"))
        code, _out, _err, fetch_mock, _t, _c = self._run(
            root, self._live_argv(root), fetch_side_effect=side_effect,
        )
        self.assertEqual(code, 1)
        self.assertEqual(fetch_mock.call_count, 2)
        record = self._record(root)
        price = record["price_history"]
        self.assertEqual(price["status"], "partial")
        by_symbol = {e["symbol"]: e for e in price["symbols"]}
        self.assertEqual(by_symbol["AKFIS"]["status"], "failed:network_error")
        self.assertEqual(by_symbol["ZZZZZ"]["status"], "saved")
        self.assertNotIn("secret.example", json.dumps(record))

    def test_http_429_stops_remaining_price_requests(self):
        def side_effect(url, timeout):
            raise price_batch.fetch.FetchError("fetch_failed_after_2_attempts: http_429")

        root = self._env()
        self._precreate_mini_app(root, ("AKFIS", "ZZZZZ"))
        code, _out, _err, fetch_mock, time_mock, _c = self._run(
            root, self._live_argv(root), fetch_side_effect=side_effect,
        )
        self.assertEqual(code, 1)
        self.assertEqual(fetch_mock.call_count, 1)
        time_mock.sleep.assert_not_called()
        price = self._record(root)["price_history"]
        self.assertEqual(price["status"], "stopped_on_429")
        by_symbol = {e["symbol"]: e for e in price["symbols"]}
        self.assertEqual(by_symbol["AKFIS"]["status"], "failed:http_429")
        self.assertEqual(by_symbol["ZZZZZ"]["status"], "skipped_rate_limited")

    def test_price_step_runs_even_when_the_mini_app_step_stopped_on_429(self):
        root = self._env()
        code, _out, _err, fetch_mock, _t, calls = self._run(
            root, self._live_argv(root),
            mini_app_plan=[{
                "raises": True, "sent": (),
                "diagnostics": (AttemptDiagnostic(1, "connect", "handshake_rejected_http_429"),),
            }],
        )
        self.assertEqual(code, 1)  # Mini App 429 stop
        self.assertEqual(len(calls), 1)
        self.assertEqual(fetch_mock.call_count, 2)
        record = self._record(root)
        self.assertTrue(record["rate_limit"]["stopped_on_429"])
        self.assertEqual(record["price_history"]["status"], "completed")
        self.assertTrue((self._price_dir(root, "ZZZZZ") / PRICE_NAME).is_file())

    # -- pacing floor, skip flag, future window --------------------------------------

    def test_config_cannot_lower_the_pacing_floor(self):
        root = self._env()
        self._precreate_mini_app(root, ("AKFIS", "ZZZZZ"))
        _code, _out, _err, _f, time_mock, _c = self._run(
            root, self._live_argv(root, pacing_seconds=0.1),
        )
        time_mock.sleep.assert_called_once_with(price_batch.MIN_PACING_SECONDS)

    def test_skip_price_flag_makes_no_request_and_writes_no_file(self):
        root = self._env()
        self._precreate_mini_app(root, ("AKFIS", "ZZZZZ"))
        code, _out, err, fetch_mock, _t, _c = self._run(
            root, self._live_argv(root, "--skip-price"),
        )
        self.assertEqual(code, 0, err)
        fetch_mock.assert_not_called()
        self.assertFalse((root / "data" / "interim" / "price_history").exists())
        price = self._record(root)["price_history"]
        self.assertEqual(price, {"status": "skipped", "reason": "skip_price_flag", "files": []})

    def test_window_end_in_the_future_skips_the_price_step(self):
        root = self._env()
        self._precreate_mini_app(root, ("AKFIS", "ZZZZZ"))
        code, _out, _err, fetch_mock, _t, _c = self._run(
            root, self._live_argv(root), today=date(2026, 1, 8),
        )
        self.assertEqual(code, 0)
        fetch_mock.assert_not_called()
        self.assertEqual(self._record(root)["price_history"]["reason"], "window_end_in_future")

    # -- dry-run --------------------------------------------------------------------

    def test_dry_run_plans_only_no_network_no_files(self):
        root = self._env()
        existing = self._price_dir(root, "ZZZZZ") / PRICE_NAME
        existing.parent.mkdir(parents=True)
        existing.write_text("{}", encoding="utf-8")
        config = _price_config(root)

        def no_network(*_args, **_kwargs):
            raise AssertionError("dry-run must not touch the network")

        with patch.object(urllib.request, "urlopen", side_effect=no_network):
            code, out, err, fetch_mock, time_mock, calls = self._run(
                root, ["--dry-run", "--config", str(config), "--run-date", RUN_DATE.isoformat(),
                       "--format", "json"],
                fetch_side_effect=no_network,
            )
        self.assertEqual(code, 0, err)
        fetch_mock.assert_not_called()
        time_mock.sleep.assert_not_called()
        self.assertEqual(len(calls), 0)
        skeleton = json.loads(out)
        price = skeleton["price_history"]
        self.assertEqual(price["status"], "planned")  # skeleton keeps "planned"
        self.assertEqual(price["files"], [])
        by_symbol = {e["symbol"]: e["status"] for e in price["symbols"]}
        self.assertEqual(by_symbol, {"AKFIS": "planned", "ZZZZZ": "skipped_exists"})
        self.assertEqual(existing.read_text(encoding="utf-8"), "{}")  # untouched
        self.assertFalse((self._price_dir(root, "AKFIS")).exists())
        self.assertFalse((root / "data" / "interim" / "daily_capture").exists())

    def test_dry_run_text_shows_price_plan_and_skip_price_is_reflected(self):
        root = self._env()
        config = _price_config(root)
        base = ["--dry-run", "--config", str(config), "--run-date", RUN_DATE.isoformat()]
        _c, out, _e, _f, _t, _k = self._run(root, base)
        self.assertIn("Price history plan: 2026-01-05..2026-01-09", out)
        _c, out, _e, _f, _t, _k = self._run(root, [*base, "--skip-price"])
        self.assertIn("Price history: skipped (skip_price_flag).", out)


if __name__ == "__main__":
    unittest.main()
