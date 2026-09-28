"""Offline tests for the daily-collection live executor (ADR PR2).

No network anywhere: ``WebSocketCollector`` and ``InteractiveSecretProvider``
are mocked exactly like ``tests/test_live_collector_smoke.py``'s own
multi-symbol tests. All splits, exclusions and run records are synthetic,
written to a temporary directory via ``test_symbol_guard.GuardEnvMixin``.
The one non-synthetic input is the AKFIS market-response fixture set
already used by ``test_live_collector_smoke.py``'s ``period_analysis_case``
helper (no captured market data; every field is synthetic).
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from fintr.collector.models import AttemptDiagnostic, SendObservation

import test_live_collector_smoke as smoke_fixtures
import test_symbol_guard as guard_fixtures


ROOT = Path(__file__).resolve().parents[1]

# Window matching the AKFIS fixtures' own baked dates exactly (custody
# analysis' data_range and the custodian-diff fixture's start_date), via
# lookback_calendar_days=9 rather than the config's real-world default of
# 14 -- see the module docstring in scripts/collect_daily.py and
# test_live_collector_smoke.LiveSmokeTests.period_analysis_case.
WINDOW_START = date(2026, 9, 14)
WINDOW_END = date(2026, 9, 23)
RUN_DATE = WINDOW_END + timedelta(days=1)  # E = R - 1


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"live_run_test_{name}", ROOT / "scripts" / f"{name}.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collect_daily = _load_script("collect_daily")


def _config(directory: Path, **overrides) -> Path:
    document = {
        "schema": "fintr_daily_collection_config_v1",
        "source_id": "synthetic_miniapp_v1",
        "symbol_scope": "split_development_only",
        "split_config": "configs/splits/current.json",
        "exclusions_config": "configs/exclusions/symbols.json",
        "window": {"end_offset_days": 1, "lookback_calendar_days": 9, "max_span_days": 90},
        "pacing": {"symbol_pause_seconds": 0, "max_wall_seconds": 3600},
        "collector": {"max_frames": 8, "timeout_seconds": 5, "max_message_mib": 1},
        "stop_on_http_status": [429],
        "capture_root": "data/interim/daily_capture",
        "permission_status": "user_asserted_2026-09-28_not_independently_verified",
    }
    document.update(overrides)
    path = directory / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _collector_factory(plan):
    calls: list[MagicMock] = []

    def factory(secret_provider, *, config, send_observer, diagnostic_observer):
        spec = plan[len(calls)]
        mock = MagicMock(secret_provider=secret_provider)

        async def collect(requests, stop_when=None):
            # The real WebSocketCollector reads initData to build the
            # authenticated URL before every connection attempt, 429s
            # included; this mock does the same so K1b's age-bucket/
            # accepted bookkeeping in _CachedSecretProvider is exercised.
            secret_provider.get_init_data()
            for request_type in spec.get("sent", ()):
                send_observer(SendObservation(1, request_type, "sent"))
            for diagnostic in spec.get("diagnostics", ()):
                diagnostic_observer(diagnostic)
            if spec.get("raises"):
                raise collect_daily.smoke.CollectorConnectionError("redacted: offline test")
            return spec.get("results", ())

        mock.collect = collect
        calls.append(mock)
        return mock

    return factory, calls


class LiveRunCliTests(guard_fixtures.GuardEnvMixin, unittest.TestCase):
    """``collect_daily.main`` drives its own event loop (``asyncio.run``),
    so these are plain sync tests, not ``IsolatedAsyncioTestCase`` -- calling
    it from an already-running loop would raise."""

    def _akfis_split(
        self, *, development_symbols=("AKFIS",), holdout_symbols=("HOLDA", "HOLDB"), **overrides,
    ):
        return guard_fixtures._split_doc(
            development_symbols=list(development_symbols),
            holdout_symbols=list(holdout_symbols),
            pool_size=len(development_symbols) + len(holdout_symbols),
            **overrides,
        )

    def _env(self, split=None):
        root = self.make_env(split=split or self._akfis_split())
        return root

    def _period_analysis_case(self, **kwargs):
        return asyncio.run(
            smoke_fixtures.LiveSmokeTests.period_analysis_case(self, **kwargs)
        )

    def _run(self, root, argv, plan, *, init_data="query_id=1&auth_date=100&hash=abc"):
        # These tests cover the Mini App step; the price step (own tests in
        # test_daily_price_step.py) must never reach the network from here.
        argv = [*argv, "--skip-price"]
        factory, calls = _collector_factory(plan)
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(collect_daily, "PROJECT_ROOT", root),
            patch.object(collect_daily.smoke, "PROJECT_ROOT", root),
            patch.object(collect_daily.smoke, "WebSocketCollector") as collector,
            patch.object(
                collect_daily.smoke, "InteractiveSecretProvider",
                return_value=MagicMock(get_init_data=MagicMock(return_value=init_data)),
            ),
            redirect_stdout(out), redirect_stderr(err),
        ):
            collector.side_effect = factory
            code = collect_daily.main(argv)
        return code, out.getvalue(), err.getvalue(), calls

    def _run_record(self, root: Path) -> dict:
        run_dir = root / "data" / "interim" / "daily_capture" / "_runs"
        files = list(run_dir.glob("*.json"))
        self.assertEqual(len(files), 1, files)
        return json.loads(files[0].read_text(encoding="utf-8"))

    # -- Gate before any prompt/connection -----------------------------------

    def test_live_run_requires_allow_session_takeover(self):
        root = self._env()
        _config(root)
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                collect_daily.main([
                    "--config", str(root / "config.json"), "--run-date", RUN_DATE.isoformat(),
                ])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--allow-session-takeover", err.getvalue())

    # -- Successful save + run record -----------------------------------------

    def test_live_run_saves_and_writes_run_record(self):
        root = self._env()
        config_path = _config(root)
        results, _requests = self._period_analysis_case()
        code, out, err, calls = self._run(
            root,
            [
                "--allow-session-takeover", "--config", str(config_path),
                "--run-date", RUN_DATE.isoformat(),
            ],
            [{"sent": collect_daily.smoke.PERIOD_ANALYSIS_REQUEST_TYPES, "results": results}],
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 1)
        capture_path = (
            root / "data" / "interim" / "daily_capture" / "AKFIS"
            / f"{WINDOW_START.isoformat()}_{WINDOW_END.isoformat()}.json"
        )
        self.assertTrue(capture_path.is_file())
        saved = json.loads(capture_path.read_text(encoding="utf-8"))
        self.assertIn("source", saved["capture_metadata"])
        self.assertIn("run", saved["capture_metadata"])
        self.assertEqual(
            saved["capture_metadata"]["run"]["run_date_local"], RUN_DATE.isoformat(),
        )

        record = self._run_record(root)
        self.assertEqual(record["schema"], "fintr_daily_collection_run_v1")
        self.assertEqual(record["run_date_local"], RUN_DATE.isoformat())
        self.assertEqual(record["timezone"], "Europe/Istanbul")
        by_symbol = {entry["symbol"]: entry for entry in record["symbols"]}
        self.assertEqual(by_symbol["AKFIS"]["status"], "saved")
        self.assertEqual(
            by_symbol["AKFIS"]["capture_path"],
            f"data/interim/daily_capture/AKFIS/{WINDOW_START.isoformat()}_{WINDOW_END.isoformat()}.json",
        )
        self.assertEqual(len(by_symbol["AKFIS"]["capture_packet_sha256"]), 64)
        self.assertEqual(by_symbol["HOLDA"]["status"], "skipped_holdout")
        self.assertEqual(by_symbol["HOLDB"]["status"], "skipped_holdout")
        self.assertIn("Run record:", out)

    # -- Secret/financial-value check on the run record ------------------------

    def test_run_record_never_contains_secret_or_financial_value(self):
        root = self._env()
        config_path = _config(root)
        results, _requests = self._period_analysis_case()
        code, _out, err, _calls = self._run(
            root,
            [
                "--allow-session-takeover", "--config", str(config_path),
                "--run-date", RUN_DATE.isoformat(),
            ],
            [{"sent": collect_daily.smoke.PERIOD_ANALYSIS_REQUEST_TYPES, "results": results}],
        )
        self.assertEqual(code, 0, err)
        record = self._run_record(root)
        # Same check the run record's own writer runs before persisting it.
        from fintr.evaluation.live_capture import validate_context_contains_no_secrets
        validate_context_contains_no_secrets(record)  # must not raise
        text = json.dumps(record)
        for forbidden in ("query_id=1&auth_date=100&hash=abc", "22.84", "13.258", "wss://"):
            self.assertNotIn(forbidden, text)

    # -- K1b: age bucket + accepted, never the raw value ------------------------

    def test_k1b_age_bucket_and_accepted_are_recorded_never_the_raw_value(self):
        root = self._env()
        config_path = _config(root)
        results, _requests = self._period_analysis_case()
        now = datetime.now(timezone.utc)
        auth_date = int((now - timedelta(hours=2)).timestamp())
        code, _out, err, _calls = self._run(
            root,
            [
                "--allow-session-takeover", "--config", str(config_path),
                "--run-date", RUN_DATE.isoformat(),
            ],
            [{"sent": collect_daily.smoke.PERIOD_ANALYSIS_REQUEST_TYPES, "results": results}],
            init_data=f"query_id=1&auth_date={auth_date}&hash=abc",
        )
        self.assertEqual(code, 0, err)
        record = self._run_record(root)
        self.assertEqual(record["init_data_status"]["prompted"], True)
        self.assertEqual(record["init_data_status"]["age_seconds_bucket"], "1–24h")
        self.assertTrue(record["init_data_status"]["accepted"])
        self.assertNotIn(str(auth_date), json.dumps(record))

    def test_k1b_not_prompted_when_every_symbol_is_skipped_existing(self):
        root = self._env()
        config_path = _config(root)
        existing = (
            root / "data" / "interim" / "daily_capture" / "AKFIS"
            / f"{WINDOW_START.isoformat()}_{WINDOW_END.isoformat()}.json"
        )
        existing.parent.mkdir(parents=True)
        existing.write_text("{}", encoding="utf-8")
        code, _out, err, calls = self._run(
            root,
            [
                "--allow-session-takeover", "--config", str(config_path),
                "--run-date", RUN_DATE.isoformat(),
            ],
            [],
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 0)
        record = self._run_record(root)
        self.assertEqual(record["init_data_status"], {
            "prompted": False, "age_seconds_bucket": None, "accepted": None,
        })
        by_symbol = {entry["symbol"]: entry for entry in record["symbols"]}
        self.assertEqual(by_symbol["AKFIS"]["status"], "skipped_existing")

    # -- 429 stops the whole run, not just the current symbol -------------------

    def test_429_stops_the_run_and_skips_remaining_symbols(self):
        root = self._env(split=self._akfis_split(development_symbols=["AKFIS", "ZZZZZ"]))
        config_path = _config(root)
        code, out, err, calls = self._run(
            root,
            [
                "--allow-session-takeover", "--config", str(config_path),
                "--run-date", RUN_DATE.isoformat(),
            ],
            [{
                "raises": True, "sent": (),
                "diagnostics": (AttemptDiagnostic(1, "connect", "handshake_rejected_http_429"),),
            }],
        )
        self.assertEqual(code, 1, err)
        self.assertEqual(len(calls), 1)
        record = self._run_record(root)
        by_symbol = {entry["symbol"]: entry for entry in record["symbols"]}
        self.assertEqual(by_symbol["AKFIS"]["status"], "failed:session_no_send")
        self.assertEqual(by_symbol["ZZZZZ"]["status"], "skipped_rate_limited")
        self.assertTrue(record["rate_limit"]["stopped_on_429"])
        self.assertEqual(record["rate_limit"]["http_429_count"], 1)

    # -- Fail-closed on an unknown event type ------------------------------------

    def test_unknown_event_type_fails_closed_and_does_not_save(self):
        root = self._env()
        config_path = _config(root)
        results, _requests = self._period_analysis_case()
        from fintr.collector.client import replay_frames
        unknown = asyncio.run(replay_frames(('{"type":"authorization:abc123"}',)))[0]
        results_with_unknown = (*results, unknown)
        code, _out, err, _calls = self._run(
            root,
            [
                "--allow-session-takeover", "--config", str(config_path),
                "--run-date", RUN_DATE.isoformat(),
            ],
            [{
                "sent": collect_daily.smoke.PERIOD_ANALYSIS_REQUEST_TYPES,
                "results": results_with_unknown,
            }],
        )
        self.assertEqual(code, 1, err)
        akfis_dir = root / "data" / "interim" / "daily_capture" / "AKFIS"
        self.assertFalse(akfis_dir.exists())
        record = self._run_record(root)
        by_symbol = {entry["symbol"]: entry for entry in record["symbols"]}
        self.assertEqual(by_symbol["AKFIS"]["status"], "failed:unknown_event_type")
        self.assertEqual(by_symbol["AKFIS"]["unknown_event_types_count"], 1)

    # -- Catch-up is read from a prior run record --------------------------------

    def test_catch_up_window_is_read_from_a_prior_run_record(self):
        root = self._env()
        run_dir = root / "data" / "interim" / "daily_capture" / "_runs"
        run_dir.mkdir(parents=True)
        earlier_end = WINDOW_END - timedelta(days=5)
        (run_dir / "old_run.json").write_text(json.dumps({
            "schema": "fintr_daily_collection_run_v1",
            "symbols": [{
                "symbol": "AKFIS", "status": "saved",
                "window": {"start": (earlier_end - timedelta(days=9)).isoformat(),
                           "end": earlier_end.isoformat()},
            }],
        }), encoding="utf-8")
        config_path = _config(root)
        out = io.StringIO()
        with (
            patch.object(collect_daily, "PROJECT_ROOT", root),
            patch.object(collect_daily.smoke, "PROJECT_ROOT", root),
            redirect_stdout(out),
        ):
            code = collect_daily.main([
                "--dry-run", "--config", str(config_path), "--run-date", RUN_DATE.isoformat(),
            ])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn('"catch_up": true', text)
        self.assertIn(f'"start": "{(earlier_end - timedelta(days=9)).isoformat()}"', text)

    # -- --dry-run is unaffected --------------------------------------------------

    def test_dry_run_still_never_prompts_or_connects(self):
        root = self._env()
        config_path = _config(root)
        out = io.StringIO()
        with (
            patch.object(collect_daily, "PROJECT_ROOT", root),
            patch.object(collect_daily.smoke, "PROJECT_ROOT", root),
            patch.object(collect_daily.smoke, "WebSocketCollector") as collector,
            patch.object(collect_daily.smoke, "InteractiveSecretProvider") as provider,
            redirect_stdout(out),
        ):
            code = collect_daily.main([
                "--dry-run", "--config", str(config_path), "--run-date", RUN_DATE.isoformat(),
            ])
        self.assertEqual(code, 0)
        collector.assert_not_called()
        provider.assert_not_called()
        self.assertIn("Dry run: no connection opened, no initData prompted.", out.getvalue())
        self.assertFalse((root / "data" / "interim" / "daily_capture").exists())


class IstanbulTodayTests(unittest.TestCase):
    """ADR §7: run day R from Europe/Istanbul, independent of host tz."""

    def test_utc_midnight_boundary_is_already_the_next_istanbul_day(self):
        # 22:00 UTC on the 27th is 01:00 the next day in Istanbul (UTC+3).
        now = datetime(2026, 9, 27, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(collect_daily._istanbul_today(now=now), date(2026, 9, 28))

    def test_just_before_utc_midnight_boundary_is_still_the_same_istanbul_day(self):
        now = datetime(2026, 9, 27, 20, 59, tzinfo=timezone.utc)
        self.assertEqual(collect_daily._istanbul_today(now=now), date(2026, 9, 27))

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises((TypeError, ValueError)):
            collect_daily._istanbul_today(now=datetime(2026, 9, 27, 22, 0))

    def test_zoneinfo_missing_falls_back_to_fixed_utc_plus_3(self):
        now = datetime(2026, 9, 27, 22, 0, tzinfo=timezone.utc)
        with patch.object(
            collect_daily, "ZoneInfo", side_effect=collect_daily.ZoneInfoNotFoundError("x"),
        ):
            self.assertEqual(collect_daily._istanbul_today(now=now), date(2026, 9, 28))


if __name__ == "__main__":
    unittest.main()
