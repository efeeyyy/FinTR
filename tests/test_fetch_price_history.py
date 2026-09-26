"""Offline tests for scripts/fetch_price_history.py.

No network is ever contacted: --dry-run is checked against a urlopen stub
that fails the test if called, and --save/--compare-capture tests patch
``_fetch_raw`` directly with fixture bytes.
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
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fintr.data.period_capture import CAPTURE_SCHEMA, canonical_json


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "market" / "price_history_chart.json"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = _load_script("fetch_price_history")


def _fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _run(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = fetch.main(list(argv))
    except SystemExit as exc:
        code = exc.code
    return code, stdout.getvalue(), stderr.getvalue()


def _minimal_period_capture_packet(dates: list[str]) -> dict:
    """Just enough of ``fintr_period_analysis_handoff_v1`` for ``server_dates``."""
    return {
        "schema": "fintr_period_analysis_handoff_v1",
        "requested_period": {
            "symbol": "TESTX", "start_date": dates[0], "end_date": dates[-1],
            "origin": "local_cli_arguments",
        },
        "get_akd_period": {"rows_in_provider_order": []},
        "custody_analysis_daily": {
            "provenance": {"server_echo": {"dates_in_provider_order": dates}},
            "daily_akd_flow_in_provider_order": [],
            "daily_custody_balance_in_provider_order": [],
            "metadata_in_provider_order": [],
        },
        "get_custodian_diff_period": {
            "provenance": {"server_echo": {"start_date": dates[0], "end_date": dates[-1]}},
            "rows_in_provider_order": [],
        },
    }


def _write_period_capture(path: Path, dates: list[str]) -> None:
    packet = _minimal_period_capture_packet(dates)
    capture = {
        "capture_metadata": {
            "schema": CAPTURE_SCHEMA,
            "captured_at_utc": "2026-01-09T18:00:00Z",
            "packet_schema": packet["schema"],
            "packet_sha256_canonical_json": hashlib.sha256(
                canonical_json(packet).encode("utf-8")
            ).hexdigest(),
        },
        "packet": packet,
    }
    path.write_text(json.dumps(capture, ensure_ascii=False), encoding="utf-8")


class ArgValidationTests(unittest.TestCase):
    def test_end_date_after_today_rejected(self) -> None:
        with patch.object(fetch, "_today", return_value=date(2026, 1, 1)):
            code, _out, err = _run([
                "--symbol", "AKFIS", "--start-date", "2026-01-01",
                "--end-date", "2026-01-05", "--dry-run",
            ])
        self.assertEqual(code, 2)
        self.assertIn("after today", err)

    def test_start_after_end_rejected(self) -> None:
        with patch.object(fetch, "_today", return_value=date(2026, 6, 1)):
            code, _out, err = _run([
                "--symbol", "AKFIS", "--start-date", "2026-01-10",
                "--end-date", "2026-01-05", "--dry-run",
            ])
        self.assertEqual(code, 2)
        self.assertIn("--start-date must not be after --end-date", err)

    def test_dry_run_cannot_combine_with_save(self) -> None:
        with patch.object(fetch, "_today", return_value=date(2026, 6, 1)):
            code, _out, err = _run([
                "--symbol", "AKFIS", "--start-date", "2026-01-05",
                "--end-date", "2026-01-09", "--dry-run", "--save",
            ])
        self.assertEqual(code, 2)
        self.assertIn("--dry-run cannot be combined", err)

    def test_invalid_symbol_rejected(self) -> None:
        code, _out, err = _run([
            "--symbol", "not-a-symbol!", "--start-date", "2026-01-05",
            "--end-date", "2026-01-09", "--dry-run",
        ])
        self.assertEqual(code, 2)
        self.assertIn("BIST symbol", err)


class DryRunNoNetworkTests(unittest.TestCase):
    def test_dry_run_prints_url_and_target_without_opening_network(self) -> None:
        def _fail_if_called(*_args, **_kwargs):
            raise AssertionError("urlopen must not be called in --dry-run")

        with patch.object(fetch, "_today", return_value=date(2026, 6, 1)), \
             patch.object(fetch.urllib.request, "urlopen", side_effect=_fail_if_called):
            code, out, _err = _run([
                "--symbol", "akfis", "--start-date", "2026-01-05",
                "--end-date", "2026-01-09", "--dry-run",
            ])
        self.assertEqual(code, 0, out)
        self.assertIn("Request URL: https://query1.finance.yahoo.com/v8/finance/chart/AKFIS.IS", out)
        self.assertIn("Target path: data/interim/price_history/AKFIS/2026-01-05_2026-01-09.json", out)
        self.assertIn("Dry run: no network connection opened", out)

    def test_build_request_url_bounds_and_interval(self) -> None:
        url = fetch.build_request_url("AKFIS", date(2026, 1, 5), date(2026, 1, 9))
        parts = urlsplit(url)
        self.assertEqual(parts.netloc, "query1.finance.yahoo.com")
        self.assertEqual(parts.path, "/v8/finance/chart/AKFIS.IS")
        query = parse_qs(parts.query)
        self.assertEqual(query["interval"], ["1d"])
        period1, period2 = int(query["period1"][0]), int(query["period2"][0])
        self.assertLess(period1, period2)
        # The query window is widened beyond the requested range on both ends.
        five_days = 5 * 86400
        self.assertEqual(period2 - period1, (9 - 5) * 86400 + 2 * five_days)


class SaveAndCompareTests(unittest.TestCase):
    def test_save_writes_capture_and_refuses_overwrite(self) -> None:
        raw_bytes = _fixture_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            with patch.object(fetch, "PROJECT_ROOT", project_root), \
                 patch.object(fetch, "_today", return_value=date(2026, 6, 1)), \
                 patch.object(fetch, "_fetch_raw", return_value=raw_bytes):
                code, out, _err = _run([
                    "--symbol", "TESTX", "--start-date", "2026-01-05",
                    "--end-date", "2026-01-09", "--save",
                ])
                self.assertEqual(code, 0, out)
                target = (
                    project_root / "data" / "interim" / "price_history" / "TESTX"
                    / "2026-01-05_2026-01-09.json"
                )
                self.assertTrue(target.exists())
                saved = json.loads(target.read_text(encoding="utf-8"))
                metadata = saved["capture_metadata"]
                self.assertEqual(metadata["schema"], "fintr_price_history_capture_v1")
                self.assertEqual(metadata["source_id"], "yahoo_finance_chart_v8")
                self.assertEqual(
                    metadata["permission_provenance"]["status"],
                    "user_asserted_2026-09-26_not_independently_verified",
                )
                self.assertEqual(
                    metadata["raw_response_sha256"], hashlib.sha256(raw_bytes).hexdigest(),
                )
                normalized = saved["normalized"]
                self.assertEqual(normalized["bar_count"], 5)
                self.assertEqual(normalized["dropped_before_start_count"], 0)
                self.assertEqual(normalized["dropped_after_end_count"], 0)
                self.assertIsNone(normalized["bars"][2]["close"])
                before = target.read_bytes()

                code2, out2, _err2 = _run([
                    "--symbol", "TESTX", "--start-date", "2026-01-05",
                    "--end-date", "2026-01-09", "--save",
                ])
                self.assertEqual(code2, 1)
                self.assertIn("already exists", out2)
                self.assertEqual(target.read_bytes(), before)

    def test_dropped_counts_for_narrower_range(self) -> None:
        raw_bytes = _fixture_bytes()
        with patch.object(fetch, "_today", return_value=date(2026, 6, 1)), \
             patch.object(fetch, "_fetch_raw", return_value=raw_bytes):
            code, out, _err = _run([
                "--symbol", "TESTX", "--start-date", "2026-01-06",
                "--end-date", "2026-01-08",
            ])
        self.assertEqual(code, 0, out)
        self.assertIn("Bars in requested range: 3", out)
        self.assertIn("Dropped (outside requested range): 1 before start, 1 after end", out)

    def test_compare_capture_prints_only_dates(self) -> None:
        raw_bytes = _fixture_bytes()
        server_dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
        with tempfile.TemporaryDirectory() as temporary:
            capture_path = Path(temporary) / "period_capture.json"
            _write_period_capture(capture_path, server_dates)
            with patch.object(fetch, "_today", return_value=date(2026, 6, 1)), \
                 patch.object(fetch, "_fetch_raw", return_value=raw_bytes):
                code, out, _err = _run([
                    "--symbol", "TESTX", "--start-date", "2026-01-05",
                    "--end-date", "2026-01-09", "--compare-capture", str(capture_path),
                ])
        self.assertEqual(code, 0, out)
        self.assertIn("Period capture server date count: 4", out)
        self.assertIn("Price history date count (within requested range): 5", out)
        self.assertIn("Server dates missing from price data: 0", out)
        self.assertIn("Price dates not in server dates: 1", out)
        self.assertIn("2026-01-09", out)
        # No price value should ever appear in the comparison output.
        self.assertNotIn("10.2", out)
        self.assertNotIn("10.15", out)


if __name__ == "__main__":
    unittest.main()
