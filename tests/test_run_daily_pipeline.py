"""Offline tests for scripts/run_daily_pipeline.py (subprocess.run mocked)."""

from __future__ import annotations

import importlib.util
import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("run_daily_pipeline_t", ROOT / "scripts" / "run_daily_pipeline.py")
rdp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rdp)

SAVED = os.path.join("data", "interim", "scanner_daily", "2026-10-01", "observations.json")
STEP_INDEX = {"collect_daily.py": 0, "scan_observations.py": 1, "notify_observations.py": 2}


def _go(argv, codes=(0, 0, 0), saved=True, scan_err=None, notify_err=""):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        idx = STEP_INDEX[Path(cmd[1]).name]
        err = ""
        if idx == 1:
            err = scan_err if scan_err is not None else (f"Observations saved: {SAVED}\n" if saved else "")
        elif idx == 2:
            err = notify_err
        return subprocess.CompletedProcess(cmd, codes[idx], stdout="SECRET-OBSERVATION", stderr=err)

    out, err = io.StringIO(), io.StringIO()
    with patch.object(rdp.subprocess, "run", run), redirect_stdout(out), redirect_stderr(err):
        rc = rdp.main(argv)
    return rc, calls, out.getvalue(), err.getvalue()


def _names(calls):
    return [Path(c[0][1]).name for c in calls]


class PipelineTests(unittest.TestCase):
    def test_order_and_arguments(self):
        rc, calls, out, err = _go(["--allow-session-takeover", "--skip-price", "--run-date", "2026-10-02"])
        self.assertEqual(rc, 0)
        self.assertEqual(_names(calls), ["collect_daily.py", "scan_observations.py", "notify_observations.py"])
        collect, scan, notify = (c[0][2:] for c in calls)
        self.assertEqual(collect, ["--allow-session-takeover", "--skip-price", "--run-date", "2026-10-02"])
        self.assertEqual(scan, ["--daily-root", rdp.DAILY_ROOT, "--out-dir", rdp.SCANNER_DIR,
                                "--save", "--format", "json"])
        self.assertEqual(notify, ["--as-of", "2026-10-01", "--scanner-dir", rdp.SCANNER_DIR])
        self.assertNotIn("--resend", notify)
        self.assertTrue(all(c[0][0] == rdp.sys.executable for c in calls))
        self.assertNotIn("stdout", calls[0][1])  # collect inherits stdio (getpass prompt)
        self.assertNotIn("stdout", calls[2][1])
        self.assertNotIn("SECRET-OBSERVATION", out + err)

    def test_send_forwarded_only_when_given(self):
        _, calls, out, _ = _go(["--allow-session-takeover", "--send"])
        self.assertIn("--send", calls[2][0])
        self.assertNotIn("--resend", calls[2][0])
        self.assertIn("sent: yes", out)
        _, calls, out, _ = _go(["--allow-session-takeover"])
        self.assertNotIn("--send", calls[2][0])
        self.assertIn("no (notify dry-run)", out)

    def test_stop_on_each_failure_and_never_send(self):
        for failing, code, expected_calls in [(0, 3, 1), (1, 1, 2), (2, 4, 3)]:
            codes = [0, 0, 0]
            codes[failing] = code
            rc, calls, out, _ = _go(["--allow-session-takeover", "--send"], tuple(codes))
            self.assertEqual(rc, code)
            self.assertEqual(len(calls), expected_calls)
            if failing < 2:
                self.assertNotIn("notify_observations.py", _names(calls))
            self.assertIn("failed with exit code", out)

    def test_skip_collect(self):
        rc, calls, _, _ = _go(["--skip-collect"])
        self.assertEqual(rc, 0)
        self.assertEqual(_names(calls), ["scan_observations.py", "notify_observations.py"])

    def test_dry_run_skips_scan_and_notify(self):
        rc, calls, out, _ = _go(["--dry-run", "--send"])
        self.assertEqual(rc, 0)
        self.assertEqual(_names(calls), ["collect_daily.py"])
        self.assertIn("--dry-run", calls[0][0])
        self.assertIn("skipped", out)

    def test_missing_saved_line_stops_before_notify(self):
        rc, calls, _, _ = _go([], saved=False)
        self.assertEqual(rc, 1)
        self.assertEqual(_names(calls), ["collect_daily.py", "scan_observations.py"])

    def test_same_day_rerun_is_idempotent(self):
        exists = f"Scan failed: output file already exists (no overwrite): {SAVED}\n"
        receipt = "Notify failed: bu as_of için receipt zaten var (x); --resend ile yeniden gönder\n"
        rc, calls, out, _ = _go(["--allow-session-takeover", "--send"], codes=(0, 1, 1),
                                scan_err=exists, notify_err=receipt)
        self.assertEqual(rc, 0)
        self.assertEqual(_names(calls), ["collect_daily.py", "scan_observations.py", "notify_observations.py"])
        self.assertEqual(calls[2][0][2:4], ["--as-of", "2026-10-01"])
        self.assertNotIn("--resend", calls[2][0])
        self.assertIn("already_scanned", out)
        self.assertIn("already_sent", out)
        self.assertIn("no resend", out)

    def test_exists_scan_then_dry_run_notify_continues(self):
        exists = f"Scan failed: output file already exists (no overwrite): {SAVED}\n"
        rc, calls, out, _ = _go([], codes=(0, 1, 0), scan_err=exists)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 3)

    def test_real_scan_error_stops(self):
        rc, calls, out, _ = _go(["--send"], codes=(0, 1, 0), scan_err="Scan failed: no period capture found for X\n")
        self.assertEqual(rc, 1)
        self.assertEqual(_names(calls), ["collect_daily.py", "scan_observations.py"])
        self.assertIn("failed with exit code", out)

    def test_other_notify_failure_with_send_still_fails(self):
        rc, calls, out, _ = _go(["--send"], codes=(0, 0, 1), notify_err="Notify failed: token\n")
        self.assertEqual(rc, 1)

    def test_extract_as_of(self):
        self.assertEqual(rdp.extract_as_of(f"x\nObservations saved: {SAVED}\n"), ("2026-10-01", SAVED))
        self.assertEqual(rdp.extract_as_of("Observations saved: data/a/2026-09-30/observations.json")[0],
                         "2026-09-30")
        self.assertIsNone(rdp.extract_as_of("nothing"))


if __name__ == "__main__":
    unittest.main()
