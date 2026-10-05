"""Offline tests for scripts/check_env.py (read-only environment doctor).

No network, no subprocess, no write: the suite patches sockets and process
creation to fail, uses temporary directories for run records and receipts, and
asserts that environment-variable values never reach the output.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import date, datetime, timezone
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_env_t", ROOT / "scripts" / "check_env.py")
ce = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ce)

SECRET_TOKEN = "SECRET-TOKEN-0123456789abcdef"
SECRET_CHAT = "-1001234567890"
FAKE_ENV = {"FINTR_TELEGRAM_BOT_TOKEN": SECRET_TOKEN, "FINTR_TELEGRAM_CHAT_ID": SECRET_CHAT}

PYPROJECT = {
    "project": {
        "requires-python": ">=3.12,<3.13",
        "dependencies": ["websockets>=17.1,<18", "tzdata>=2024.1"],
    },
}
HASH_LABELS = (
    "spec hash (FREEZE.md)", "tarayıcı kuralları", "split hash (spec kaydı)",
    "test planı hash (DECISIONS.md)",
)


def _by_label(results, label):
    matches = [result for result in results if result.label == label]
    assert len(matches) == 1, (label, [r.label for r in results])
    return matches[0]


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _run_record(run_date: str, *, status="completed", saved=2, failed=0, end="2026-10-01") -> dict:
    symbols = [
        {"symbol": f"SYM{i}", "status": "saved", "window": {"start": "2026-09-17", "end": end}}
        for i in range(saved)
    ] + [
        {"symbol": f"BAD{i}", "status": "failed:network_error", "window": None}
        for i in range(failed)
    ]
    return {
        "schema": ce.RUN_RECORD_SCHEMA, "run_date_local": run_date, "status": status,
        "symbols": symbols,
    }


class SchemaPinTests(unittest.TestCase):
    def test_run_record_schema_matches_planner(self):
        from fintr.collection.planner import RUN_RECORD_SCHEMA

        self.assertEqual(ce.RUN_RECORD_SCHEMA, RUN_RECORD_SCHEMA)


class VersionHelperTests(unittest.TestCase):
    def test_satisfies(self):
        self.assertTrue(ce._satisfies("17.2", ">=17.1,<18"))
        self.assertFalse(ce._satisfies("18.0", ">=17.1,<18"))
        self.assertFalse(ce._satisfies("17.0.9", ">=17.1,<18"))
        self.assertTrue(ce._satisfies("2026.2", ">=2024.1"))
        self.assertIsNone(ce._satisfies("1.0", "~=1.0"))
        self.assertIsNone(ce._satisfies("dev", ">=1"))


class PythonAndPackageTests(unittest.TestCase):
    def test_python_version(self):
        self.assertEqual(ce.check_python(PYPROJECT, version="3.12.3").level, ce.OK)
        self.assertEqual(ce.check_python(PYPROJECT, version="3.11.9").level, ce.ERR)
        self.assertEqual(ce.check_python(PYPROJECT, version="3.13.0").level, ce.ERR)
        self.assertEqual(ce.check_python(None, version="3.12.3").level, ce.ERR)

    def _versions(self, mapping):
        def version(name):
            if name not in mapping:
                raise importlib.metadata.PackageNotFoundError(name)
            return mapping[name]
        return patch.object(ce.importlib.metadata, "version", side_effect=version)

    def test_packages_installed(self):
        with self._versions({"websockets": "17.2", "tzdata": "2026.1"}):
            results = ce.check_packages(PYPROJECT)
        self.assertEqual([r.level for r in results], [ce.OK, ce.OK])

    def test_missing_package_is_an_error(self):
        with self._versions({"tzdata": "2026.1"}):
            results = ce.check_packages(PYPROJECT)
        websockets = _by_label(results, "paket websockets")
        self.assertEqual(websockets.level, ce.ERR)
        self.assertIn("kurulu değil", websockets.detail)

    def test_wrong_version_is_an_error(self):
        with self._versions({"websockets": "16.0", "tzdata": "2026.1"}):
            results = ce.check_packages(PYPROJECT)
        self.assertEqual(_by_label(results, "paket websockets").level, ce.ERR)

    def test_missing_tzdata_warns_about_utc_plus_3_fallback(self):
        with self._versions({"websockets": "17.2"}), \
                patch.object(ce, "_zoneinfo_available", return_value=False):
            results = ce.check_packages(PYPROJECT)
        tzdata = _by_label(results, "paket tzdata")
        self.assertEqual(tzdata.level, ce.WARN)
        self.assertIn("UTC+3", tzdata.detail)
        self.assertNotIn(ce.ERR, [r.level for r in results if r.label != "paket tzdata"])

    def test_missing_tzdata_with_system_zoneinfo_still_warns(self):
        with self._versions({"websockets": "17.2"}), \
                patch.object(ce, "_zoneinfo_available", return_value=True):
            tzdata = _by_label(ce.check_packages(PYPROJECT), "paket tzdata")
        self.assertEqual(tzdata.level, ce.WARN)
        self.assertIn("UTC+3", tzdata.detail)

    def test_pyproject_declares_tzdata(self):
        pyproject = ce._load_pyproject(ROOT)
        declared = pyproject["project"]["dependencies"]
        names = [ce.re.match(r"[A-Za-z0-9_.\-]+", d).group(0) for d in declared]
        self.assertIn("tzdata", names)
        self.assertIn("websockets", names)

    def test_istanbul_today_falls_back_to_utc_plus_3_without_tzdata(self):
        now = datetime(2026, 10, 4, 22, 30, tzinfo=timezone.utc)
        with patch.object(ce, "ZoneInfo", side_effect=ZoneInfoNotFoundError("Europe/Istanbul")):
            self.assertEqual(ce._istanbul_today(now=now), date(2026, 10, 5))


class TelegramEnvTests(unittest.TestCase):
    def test_present_and_absent_without_values(self):
        results = ce.check_telegram_env(FAKE_ENV)
        self.assertEqual([r.level for r in results], [ce.OK, ce.OK])
        self.assertEqual([r.detail for r in results], ["var", "var"])
        text = "\n".join(ce.render(results))
        self.assertNotIn(SECRET_TOKEN, text)
        self.assertNotIn(SECRET_CHAT, text)
        self.assertNotIn(str(len(SECRET_TOKEN)), text)

    def test_missing_or_blank_is_a_warning(self):
        results = ce.check_telegram_env({"FINTR_TELEGRAM_BOT_TOKEN": "   "})
        self.assertEqual([r.level for r in results], [ce.WARN, ce.WARN])
        self.assertTrue(all(r.detail.startswith("yok") for r in results))


class ClaudeCliTests(unittest.TestCase):
    def _root_with_cli(self, directory: Path, cli: Path) -> Path:
        cli_path = {"windows": str(cli), "default": str(cli)}
        _write_json(directory / ce.LLM_NOTE_CONFIG, {"cli_path": cli_path})
        return directory

    def test_existence_only_and_never_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            marker = directory / "executed.marker"
            cli = directory / "fake_claude"
            cli.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
            cli.chmod(0o755)
            root = self._root_with_cli(directory, cli)
            result = ce.check_claude_cli(root)
            self.assertEqual(result.level, ce.OK)
            self.assertFalse(marker.exists())
            cli.unlink()
            self.assertEqual(ce.check_claude_cli(root).level, ce.WARN)

    def test_unreadable_config_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ce.check_claude_cli(Path(tmp)).level, ce.ERR)


class HashCheckTests(unittest.TestCase):
    def _copy_tree(self, destination: Path) -> Path:
        for relative in (ce.FREEZE_DOC, ce.DECISIONS_DOC, ce.TEST_PLAN, ce.SCANNER_RULES):
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / relative, target)
        for spec in (ROOT / ce.PREREG_DIR).glob("h*.json"):
            shutil.copy(spec, destination / ce.PREREG_DIR / spec.name)
        return destination

    def test_real_repository_hashes_match_recorded_values(self):
        for check in (ce.check_freeze_hashes, ce.check_scanner_rules, ce.check_test_plan_hash):
            with self.subTest(check=check.__name__):
                self.assertEqual(check(ROOT).level, ce.OK, check(ROOT).detail)

    def test_split_hash_matches_frozen_specs(self):
        from fintr.evaluation.symbol_guard import load_active_guards

        sha = load_active_guards().split.split_sha256
        self.assertEqual(ce.check_split_hash_in_specs(ROOT, sha).level, ce.OK)
        self.assertEqual(ce.check_split_hash_in_specs(ROOT, "0" * 64).level, ce.ERR)
        self.assertEqual(ce.check_split_hash_in_specs(ROOT, None).level, ce.ERR)

    def test_modified_spec_is_a_hash_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._copy_tree(Path(tmp))
            self.assertEqual(ce.check_freeze_hashes(root).level, ce.OK)
            spec = next((root / ce.PREREG_DIR).glob("h1_*.json"))
            spec.write_bytes(spec.read_bytes() + b"\n")
            result = ce.check_freeze_hashes(root)
            self.assertEqual(result.level, ce.ERR)
            self.assertIn(spec.name, result.detail)
            self.assertEqual(ce.check_scanner_rules(root).level, ce.ERR)

    def test_modified_test_plan_is_a_hash_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._copy_tree(Path(tmp))
            self.assertEqual(ce.check_test_plan_hash(root).level, ce.OK)
            plan = root / ce.TEST_PLAN
            plan.write_bytes(plan.read_bytes() + b" ")
            self.assertEqual(ce.check_test_plan_hash(root).level, ce.ERR)

    def test_missing_freeze_doc_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ce.check_freeze_hashes(Path(tmp)).level, ce.ERR)


class RunStateTests(unittest.TestCase):
    def test_latest_run_record_shows_date_status_and_counts_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "_runs"
            _write_json(runs / "2026-10-02T05:00:00Z.json", _run_record("2026-10-02", saved=3))
            _write_json(runs / "2026-10-03T05:00:00Z.json", _run_record("2026-10-03", saved=18))
            result = ce.check_last_run(runs)
        self.assertEqual(result.level, ce.OK)
        self.assertEqual(
            result.detail, "tarih=2026-10-03, durum=completed, saved=18, failed=0, toplam=18",
        )
        self.assertNotIn("SYM", result.detail)

    def test_failed_symbols_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "_runs"
            _write_json(runs / "a.json", _run_record("2026-10-03", saved=17, failed=3))
            result = ce.check_last_run(runs)
        self.assertEqual(result.level, ce.WARN)
        self.assertIn("saved=17, failed=3, toplam=20", result.detail)
        self.assertNotIn("BAD", result.detail)

    def test_skips_corrupt_and_foreign_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "_runs"
            _write_json(runs / "2026-10-01T05:00:00Z.json", _run_record("2026-10-01"))
            _write_json(runs / "2026-10-02T05:00:00Z.json", {"schema": "other"})
            (runs / "2026-10-03T05:00:00Z.json").write_text("{not json", encoding="utf-8")
            result = ce.check_last_run(runs)
        self.assertIn("tarih=2026-10-01", result.detail)

    def test_unsafe_status_text_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "_runs"
            _write_json(runs / "a.json", _run_record("2026-10-03", status="close 123.45 TL"))
            result = ce.check_last_run(runs)
        self.assertEqual(result.level, ce.WARN)
        self.assertNotIn("123.45", result.detail)
        self.assertIn("durum=bilinmiyor", result.detail)

    def test_missing_runs_directory_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ce.check_last_run(Path(tmp) / "none").level, ce.WARN)

    def _receipt(self, sent: int, total: int = 2) -> dict:
        return {
            "schema": "fintr_notify_receipt_v2",
            "message_sha256": [f"{i:064x}" for i in range(total)],
            "message_status": [
                {"index": i, "status": "sent", "telegram_message_id": 100 + i}
                for i in range(1, sent + 1)
            ],
        }

    def test_scanner_and_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner = Path(tmp) / "scanner_daily"
            self.assertEqual(ce.check_last_scanner(scanner)[0].level, ce.WARN)
            self.assertEqual(ce.check_last_receipt(scanner, None).level, ce.WARN)
            for as_of in ("2026-10-01", "2026-10-02"):
                _write_json(scanner / as_of / "observations.json", {"x": 1})
            _write_json(scanner / "2026-10-01" / "receipt.json", self._receipt(2))
            result, as_of = ce.check_last_scanner(scanner)
            self.assertEqual(
                (result.level, result.detail, as_of), (ce.OK, "2026-10-02", date(2026, 10, 2)),
            )
            # newest scanner as_of has no receipt yet
            stale = ce.check_last_receipt(scanner, as_of)
            self.assertEqual(stale.level, ce.WARN)
            self.assertIn("2026-10-02 için receipt yok", stale.detail)
            _write_json(scanner / "2026-10-02" / "receipt.json", self._receipt(2))
            done = ce.check_last_receipt(scanner, as_of)
            self.assertEqual(done.level, ce.OK)
            self.assertEqual(done.detail, "as_of=2026-10-02, tamamlandı (2/2 mesaj)")

    def test_partial_receipt_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner = Path(tmp) / "scanner_daily"
            _write_json(scanner / "2026-10-02" / "receipt.json", self._receipt(1))
            result = ce.check_last_receipt(scanner, date(2026, 10, 2))
        self.assertEqual(result.level, ce.WARN)
        self.assertIn("KISMİ (1/2 mesaj gönderildi)", result.detail)

    def test_note_receipt_is_not_counted_as_notice_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner = Path(tmp) / "scanner_daily"
            _write_json(scanner / "2026-10-02" / "note_receipt.json", self._receipt(2))
            self.assertEqual(ce.check_last_receipt(scanner, None).level, ce.WARN)


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fintr.evaluation.symbol_guard import load_active_guards

        cls.guards = load_active_guards()
        cls.config = ce.check_collection_config(ROOT, ce.DEFAULT_COLLECTION_CONFIG)[1]

    def _window(self, run_date: date, data_root: Path):
        return ce.check_window(self.config, self.guards, data_root, run_date)

    def test_weekend_window_end_expects_no_new_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            for run_date, end in ((date(2026, 10, 4), "2026-10-03 (Cumartesi)"),
                                  (date(2026, 10, 5), "2026-10-04 (Pazar)")):
                with self.subTest(run_date=run_date):
                    result = self._window(run_date, Path(tmp))
                    self.assertEqual(result.level, ce.OK)
                    self.assertIn(f"E={end}", result.detail)
                    self.assertIn("yeni veri beklenmiyor (E hafta sonu)", result.detail)

    def test_weekday_window_end_expects_data_and_discloses_holiday_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._window(date(2026, 10, 6), Path(tmp))
        self.assertEqual(result.level, ce.OK)
        self.assertIn("E=2026-10-05 (Pazartesi)", result.detail)
        self.assertIn("yeni veri bekleniyor", result.detail)
        self.assertIn("20 sembol planlı", result.detail)
        self.assertIn("tatil olup olmadığı denetlenmedi", result.detail)

    def test_window_end_already_collected(self):
        symbols = list(self.guards.split.development_symbols)
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / self.config.capture_root / ce.RUNS_SUBDIR
            record = _run_record("2026-10-06", saved=0)
            record["symbols"] = [
                {"symbol": s, "status": "saved",
                 "window": {"start": "2026-09-21", "end": "2026-10-05"}}
                for s in symbols
            ]
            _write_json(runs / "a.json", record)
            result = self._window(date(2026, 10, 6), Path(tmp))
            self.assertIn("yeni veri beklenmiyor (E zaten toplanmış)", result.detail)
            # one symbol behind: new data expected again
            record["symbols"][0]["window"]["end"] = "2026-10-02"
            _write_json(runs / "a.json", record)
            self.assertIn("yeni veri bekleniyor", self._window(date(2026, 10, 6), Path(tmp)).detail)

    def test_window_line_has_no_financial_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._window(date(2026, 10, 6), Path(tmp))
        for symbol in self.guards.split.development_symbols:
            self.assertNotIn(symbol, result.detail)


class DiskTests(unittest.TestCase):
    def test_thresholds(self):
        gib = 1 << 30
        for free, level in ((50 << 20, ce.ERR), (500 << 20, ce.WARN), (5 * gib, ce.OK)):
            with self.subTest(free=free):
                usage = shutil._ntuple_diskusage(10 * gib, 10 * gib - free, free)
                with patch.object(ce.shutil, "disk_usage", return_value=usage):
                    self.assertEqual(ce.check_disk(ROOT).level, level)


class EndToEndTests(unittest.TestCase):
    def _snapshot(self) -> set[str]:
        found = set()
        for directory, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
            found.update(os.path.join(directory, name) for name in filenames)
        return found

    def test_repository_hash_and_config_lines_are_ok(self):
        results = ce.collect_results(ROOT, env={}, run_date=date(2026, 10, 6))
        for label in HASH_LABELS + ("config toplama", "split", "exclusions", "config analiz notu",
                                    "config bildirim"):
            with self.subTest(label=label):
                result = _by_label(results, label)
                self.assertEqual(result.level, ce.OK, result.detail)

    def test_secret_values_never_reach_output_and_nothing_is_written_or_sent(self):
        before = self._snapshot()
        buffer = io.StringIO()
        with patch.dict(os.environ, FAKE_ENV), \
                patch("socket.socket.connect", side_effect=AssertionError("network")), \
                patch("socket.create_connection", side_effect=AssertionError("network")), \
                patch("subprocess.Popen", side_effect=AssertionError("subprocess")), \
                redirect_stdout(buffer):
            code = ce.main(["--run-date", "2026-10-04"])
        output = buffer.getvalue()
        self.assertIn(code, (0, 1))
        self.assertNotIn(SECRET_TOKEN, output)
        self.assertNotIn(SECRET_CHAT, output)
        self.assertIn("ortam FINTR_TELEGRAM_BOT_TOKEN: var", output)
        self.assertIn("ortam FINTR_TELEGRAM_CHAT_ID: var", output)
        self.assertIn("yeni veri beklenmiyor (E hafta sonu)", output)
        self.assertEqual(self._snapshot(), before)

    def test_one_line_per_item_and_summary(self):
        lines = ce.render([
            ce.Result(ce.OK, "a", "x"), ce.Result(ce.WARN, "b", "y"), ce.Result(ce.ERR, "c", "z"),
        ])
        self.assertEqual(lines[:3], ["[OK   ] a: x", "[UYARI] b: y", "[HATA ] c: z"])
        self.assertEqual(lines[3], "Özet: 1 OK, 1 UYARI, 1 HATA")

    def test_exit_code_is_one_only_on_error(self):
        for levels, expected in (((ce.OK, ce.WARN), 0), ((ce.OK, ce.ERR), 1)):
            with self.subTest(levels=levels):
                fake = [ce.Result(level, "x", "d") for level in levels]
                with patch.object(ce, "collect_results", return_value=fake), \
                        redirect_stdout(io.StringIO()):
                    self.assertEqual(ce.main(["--run-date", "2026-10-06"]), expected)

    def test_unexpected_check_failure_becomes_one_error_line(self):
        def boom():
            raise RuntimeError("secret detail must not leak")

        result = ce._guarded("x", boom)
        self.assertEqual(result.level, ce.ERR)
        self.assertNotIn("secret detail", result.detail)
        self.assertIn("RuntimeError", result.detail)


if __name__ == "__main__":
    unittest.main()
