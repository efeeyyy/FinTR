"""Offline tests for scripts/fetch_price_history_batch.py.

No network is ever contacted: ``fetch._fetch_raw`` is patched with fixture
bytes (or a failure), and ``time.sleep`` is patched so pacing never actually
waits.
"""

from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "market" / "price_history_chart.json"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


batch = _load_script("fetch_price_history_batch")


def _fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _run(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = batch.main(list(argv))
    except SystemExit as exc:
        code = exc.code
    return code, stdout.getvalue(), stderr.getvalue()


def _write_universe(path: Path, symbols: list[str]) -> None:
    path.write_text(json.dumps({
        "schema": "fintr_universe_v1", "index": "XU030", "symbols": symbols,
    }), encoding="utf-8")


class ArgAndUniverseValidationTests(unittest.TestCase):
    def test_missing_universe_file_rejected(self) -> None:
        with patch.object(batch.fetch, "_today", return_value=date(2026, 6, 1)):
            code, _out, err = _run([
                "--universe", "does-not-exist.json",
                "--start-date", "2026-01-01", "--end-date", "2026-01-05",
            ])
        self.assertEqual(code, 2)
        self.assertIn("unreadable file", err)

    def test_universe_without_symbols_key_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            universe_path = Path(temporary) / "universe.json"
            universe_path.write_text(json.dumps({"schema": "fintr_universe_v1"}), encoding="utf-8")
            with patch.object(batch.fetch, "_today", return_value=date(2026, 6, 1)):
                code, _out, err = _run([
                    "--universe", str(universe_path),
                    "--start-date", "2026-01-01", "--end-date", "2026-01-05",
                ])
        self.assertEqual(code, 2)
        self.assertIn("'symbols' list", err)

    def test_pacing_below_floor_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            universe_path = Path(temporary) / "universe.json"
            _write_universe(universe_path, ["AKFIS"])
            with patch.object(batch.fetch, "_today", return_value=date(2026, 6, 1)):
                code, _out, err = _run([
                    "--universe", str(universe_path),
                    "--start-date", "2026-01-01", "--end-date", "2026-01-05",
                    "--pacing-seconds", "0.1",
                ])
        self.assertEqual(code, 2)
        self.assertIn("--pacing-seconds must be at least", err)

    def test_duplicate_symbol_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            universe_path = Path(temporary) / "universe.json"
            _write_universe(universe_path, ["AKFIS", "akfis"])
            with patch.object(batch.fetch, "_today", return_value=date(2026, 6, 1)):
                code, _out, err = _run([
                    "--universe", str(universe_path),
                    "--start-date", "2026-01-01", "--end-date", "2026-01-05",
                ])
        self.assertEqual(code, 2)
        self.assertIn("duplicate symbol", err)


class PlanOnlyTests(unittest.TestCase):
    def test_without_save_no_network_and_prints_plan(self) -> None:
        def _fail_if_called(*_args, **_kwargs):
            raise AssertionError("no network call in plan-only mode")

        with tempfile.TemporaryDirectory() as temporary:
            universe_path = Path(temporary) / "universe.json"
            _write_universe(universe_path, ["BBB", "AAA"])
            project_root = Path(temporary) / "project"
            with patch.object(batch.fetch, "PROJECT_ROOT", project_root), \
                 patch.object(batch.fetch, "_today", return_value=date(2026, 6, 1)), \
                 patch.object(batch.fetch.urllib.request, "urlopen", side_effect=_fail_if_called):
                code, out, _err = _run([
                    "--universe", str(universe_path),
                    "--start-date", "2026-01-05", "--end-date", "2026-01-09",
                ])
        self.assertEqual(code, 0, out)
        # sorted order
        self.assertLess(out.index("AAA"), out.index("BBB"))
        self.assertIn("planned", out)
        self.assertIn("Total: 2 (planned: 2)", out)


class SaveTests(unittest.TestCase):
    def test_saves_new_skips_existing_continues_past_failure(self) -> None:
        raw_bytes = _fixture_bytes()

        def _fetch_raw(url, *, timeout):
            if "FAIL" in url:
                raise batch.fetch.FetchError("network_error: boom")
            return raw_bytes

        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary) / "project"
            universe_path = Path(temporary) / "universe.json"
            _write_universe(universe_path, ["NEWX", "FAILX", "OLDX"])

            # Pre-create OLDX's target so it is skipped.
            existing = project_root / "data" / "interim" / "price_history" / "OLDX"
            existing.mkdir(parents=True)
            (existing / "2026-01-05_2026-01-09.json").write_text("{}", encoding="utf-8")

            with patch.object(batch.fetch, "PROJECT_ROOT", project_root), \
                 patch.object(batch.fetch, "_today", return_value=date(2026, 6, 1)), \
                 patch.object(batch.fetch, "_fetch_raw", side_effect=_fetch_raw), \
                 patch.object(batch, "time") as mock_time:
                code, out, _err = _run([
                    "--universe", str(universe_path),
                    "--start-date", "2026-01-05", "--end-date", "2026-01-09",
                    "--save",
                ])

            self.assertEqual(code, 1, out)  # FAILX failed -> exit 1
            self.assertIn("saved", out)
            self.assertIn("skipped", out)
            self.assertIn("failed", out)
            self.assertIn("target_exists", out)
            saved_target = project_root / "data" / "interim" / "price_history" / "NEWX" / "2026-01-05_2026-01-09.json"
            self.assertTrue(saved_target.exists())
            saved = json.loads(saved_target.read_text(encoding="utf-8"))
            self.assertEqual(saved["normalized"]["bar_count"], 5)
            # No price value anywhere in the printed table.
            self.assertNotIn("10.2", out)
            self.assertNotIn("10.15", out)
            # Pacing: two symbols made a real request (NEWX, FAILX); sleep called once between them.
            mock_time.sleep.assert_called_once_with(1.5)

    def test_rate_limit_stops_without_hammering(self) -> None:
        def _fetch_raw(url, *, timeout):
            raise batch.fetch.FetchError("fetch_failed_after_2_attempts: http_429")

        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary) / "project"
            universe_path = Path(temporary) / "universe.json"
            _write_universe(universe_path, ["AAA", "BBB", "CCC"])

            with patch.object(batch.fetch, "PROJECT_ROOT", project_root), \
                 patch.object(batch.fetch, "_today", return_value=date(2026, 6, 1)), \
                 patch.object(batch.fetch, "_fetch_raw", side_effect=_fetch_raw), \
                 patch.object(batch, "time") as mock_time:
                code, out, _err = _run([
                    "--universe", str(universe_path),
                    "--start-date", "2026-01-05", "--end-date", "2026-01-09",
                    "--save",
                ])

        self.assertEqual(code, 1, out)
        self.assertIn("Rate limit detected on AAA", out)
        self.assertIn("stopped_after_rate_limit", out)
        # Only the first symbol's attempt actually called the network; no sleep needed yet.
        mock_time.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
