"""Offline tests for scripts/compare_daily_d0.py (ADR PR5, D0 comparison).

Captures are synthetic files written with valid packet hashes (the same
packet shape ``test_period_analysis`` uses); splits/exclusions come from
``test_symbol_guard.GuardEnvMixin``. The synthetic lot/price/code values are
distinctive strings so a test can prove none of them reaches stdout or the
report.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fintr.data.period_capture import canonical_json
from fintr.evaluation.live_capture import validate_context_contains_no_secrets

import test_period_analysis as period_fixtures
import test_symbol_guard as guard_fixtures


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"d0_test_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


d0 = _load_script("compare_daily_d0")

D18, D21, D22, D23, D24, D25, D26, D28 = (
    date(2026, 9, day) for day in (18, 21, 22, 23, 24, 25, 26, 28)
)
BASE_NAME = "2025-09-26_2026-09-25.json"
# Distinctive so absence from any output is meaningful.
CODES = ("KRMXYZ1", "KRMXYZ2")
SENTINELS = ("7654321", "987.654", "3141592", "55.55", *CODES)


def _day(*, net_aaa="7654321", custody="3141592", retail="55.55") -> dict:
    return {
        "akd": {CODES[0]: net_aaa, CODES[1]: "-11"},
        "market": {CODES[0]: ("500", "987.654"), CODES[1]: ("300", "987.654")},
        "custody": {CODES[0]: custody, CODES[1]: "42"},
        "meta": {"retail_pct": retail},
    }


def _packet(symbol: str, start: date, end: date, days: dict[date, dict]) -> dict:
    descending = sorted(days, reverse=True)
    return {
        "schema": "fintr_period_analysis_handoff_v1",
        "requested_period": {"symbol": symbol, "start_date": start.isoformat(),
                             "end_date": end.isoformat(), "origin": "local_cli_arguments"},
        "get_akd_period": {"rows_in_provider_order": [
            {"brokerage": CODES[0], "net": {"size": 1, "percentage": Decimal("0.1"),
                                            "cost": Decimal("1.5")}},
        ]},
        "custody_analysis_daily": {
            "provenance": {"server_echo": {
                "dates_in_provider_order": [day.isoformat() for day in descending]}},
            "daily_akd_flow_in_provider_order": [
                {"date": day.isoformat(), "rows": [
                    {"broker_code": code, "net_size": int(net),
                     "total_size": Decimal(days[day]["market"][code][0]),
                     "total_cost": Decimal(days[day]["market"][code][1])}
                    for code, net in days[day]["akd"].items()
                ]}
                for day in descending
            ],
            "daily_custody_balance_in_provider_order": [
                {"date": day.isoformat(), "rows": [
                    {"broker_code": code, "lot_count": Decimal(lots)}
                    for code, lots in days[day]["custody"].items()
                ]}
                for day in descending
            ],
            "metadata_in_provider_order": [
                {"date": day.isoformat(), "null_fields": [],
                 "retail_pct": Decimal(days[day]["meta"]["retail_pct"]),
                 "institutional_pct": Decimal("44.45")}
                for day in descending
            ],
        },
        "get_custodian_diff_period": {
            "provenance": {"server_echo": {"start_date": start.isoformat(),
                                           "end_date": end.isoformat()}},
            "rows_in_provider_order": [],
        },
    }


def _write_capture(
    root: Path, symbol: str, start: date, end: date, days: dict[date, dict], *,
    captured_at="2026-09-26T05:00:00Z", name: str | None = None,
) -> Path:
    packet = _packet(symbol, start, end, days)
    document = {
        "capture_metadata": {
            "schema": "fintr_period_analysis_capture_v1", "captured_at_utc": captured_at,
            "packet_schema": packet["schema"],
            "packet_sha256_canonical_json": hashlib.sha256(
                canonical_json(packet).encode("utf-8")).hexdigest(),
        },
        "packet": packet,
    }
    path = root / symbol / (name or f"{start.isoformat()}_{end.isoformat()}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(period_fixtures.smoke._compact_json(document), encoding="utf-8")
    return path


def _standard_base_days() -> dict[date, dict]:
    return {day: _day() for day in (D21, D22, D23, D24, D25)}


def _standard_daily_days() -> dict[date, dict]:
    days = {day: _day() for day in (D18, D21, D22, D23, D25, D26, D28)}
    days[D23] = _day(net_aaa="7654322")  # one code's AKD value differs
    days[D22] = _day(retail="56.56")  # metadata differs
    return days


class D0Tests(guard_fixtures.GuardEnvMixin, unittest.TestCase):
    def _env(self):
        split = guard_fixtures._split_doc(
            development_symbols=["AKFIS", "ZZZZZ"], holdout_symbols=["HOLDA", "HOLDB"], pool_size=4,
        )
        root = self.make_env(split=split)
        self.daily = root / "daily"
        self.base = root / "base"
        return root

    def _seed(self, symbol="AKFIS"):
        _write_capture(self.base, symbol, date(2025, 9, 26), D25, _standard_base_days(),
                       captured_at="2026-09-25T20:00:00Z", name=BASE_NAME)
        _write_capture(self.daily, symbol, D18, D28, _standard_daily_days())
        # Window entirely after the cutoff: must not even be opened.
        skipped = self.daily / symbol / "2026-09-26_2026-10-02.json"
        skipped.write_text("not json at all", encoding="utf-8")

    def _run(self, *extra, now=None):
        argv = ["--daily-root", str(self.daily), "--base-root", str(self.base), *extra]
        out, err = io.StringIO(), io.StringIO()
        patches = [patch.object(d0, "_utc_now", return_value=now)] if now else []
        for item in patches:
            item.start()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = d0.main(argv)
        finally:
            for item in patches:
                item.stop()
        return code, out.getvalue(), err.getvalue()

    def _report(self) -> dict:
        files = list((self.daily / "_d0").glob("*.json"))
        self.assertEqual(len(files), 1, files)
        return json.loads(files[0].read_text(encoding="utf-8"))

    # -- comparison semantics ------------------------------------------------------

    def test_match_difference_one_sided_days_and_cutoff(self):
        self._env()
        self._seed()
        code, out, err = self._run("--symbols", "AKFIS")
        self.assertEqual(code, 0, err)
        entry = self._report()["symbols"]["AKFIS"]
        self.assertEqual(entry["status"], "compared")
        self.assertEqual(entry["inputs"]["daily_files_skipped_after_cutoff"], 1)
        families = entry["families"]
        # 21/22/23/25 on both sides; 24 only in base; 18 only in daily; 26/28 ignored.
        self.assertEqual(families["akd"], {
            "compared_days": 4, "matched_days": 3, "match_rate": 0.75, "differing_days": 1,
            "differing_code_count_total": 1, "only_base": 1, "only_daily": 1,
        })
        self.assertEqual(families["akd_market"]["matched_days"], 4)
        self.assertEqual(families["akd_market"]["match_rate"], 1.0)
        self.assertEqual(families["custody"]["differing_days"], 0)
        self.assertEqual(families["custody"]["only_base"], 1)
        self.assertEqual(families["custody"]["only_daily"], 1)
        self.assertEqual(families["metadata"]["differing_days"], 1)
        self.assertEqual(families["metadata"]["differing_code_count_total"], 1)
        self.assertIn("AKFIS", out)

    def test_dates_after_the_cutoff_are_never_compared_or_counted(self):
        self._env()
        # Base and daily differ ONLY after the cutoff (and share D21 exactly).
        _write_capture(self.base, "AKFIS", date(2025, 9, 26), D25, {D21: _day()}, name=BASE_NAME)
        _write_capture(self.daily, "AKFIS", D21, D28, {
            D21: _day(), D26: _day(net_aaa="1"), D28: _day(net_aaa="2"),
        })
        self.assertEqual(self._run()[0], 0)
        akd = self._report()["symbols"]["AKFIS"]["families"]["akd"]
        self.assertEqual(akd["compared_days"], 1)
        self.assertEqual(akd["only_daily"], 0)
        self.assertEqual(akd["differing_days"], 0)

    def test_no_flag_can_move_the_cutoff(self):
        self.assertEqual(d0.D0_CUTOFF_DATE, date(2026, 9, 25))
        options = {opt for action in d0._parser()._actions for opt in action.option_strings}
        self.assertEqual(
            options, {"-h", "--help", "--daily-root", "--base-root", "--symbols", "--dry-run"},
        )

    def test_default_daily_root_matches_the_collection_config(self):
        config = json.loads(
            (ROOT / "configs" / "collection" / "daily_miniapp_v1.json").read_text(encoding="utf-8"))
        self.assertEqual(d0.DEFAULT_DAILY_ROOT, Path(config["capture_root"]))

    # -- symbols and guards ------------------------------------------------------------

    def test_holdout_excluded_and_non_development_symbols_are_rejected(self):
        self._env()
        self._seed()
        for symbol in ("HOLDA", guard_fixtures.EXCLUDED, "UNKNOWNX"):
            code, out, err = self._run("--symbols", f"AKFIS,{symbol}")
            self.assertEqual(code, 1, symbol)
            self.assertIn(symbol, err)
            self.assertEqual(out, "")
        self.assertFalse((self.daily / "_d0").exists())

    def test_default_symbols_are_development_directories_only(self):
        self._env()
        self._seed()
        for other in ("HOLDA", guard_fixtures.EXCLUDED, "_runs"):
            (self.daily / other).mkdir()
        code, _out, err = self._run()
        self.assertEqual(code, 0, err)
        report = self._report()
        self.assertEqual(list(report["symbols"]), ["AKFIS"])
        self.assertEqual(report["ignored_non_development_directory_count"], 2)

    # -- integrity ------------------------------------------------------------------------

    def test_hash_mismatch_fails_only_that_symbol(self):
        self._env()
        self._seed("AKFIS")
        self._seed("ZZZZZ")
        tampered = next((self.daily / "ZZZZZ").glob("2026-09-18_*.json"))
        tampered.write_text(
            tampered.read_text(encoding="utf-8").replace('"net_size":7654321', '"net_size":1', 1),
            encoding="utf-8",
        )
        code, _out, _err = self._run()
        self.assertEqual(code, 1)
        symbols = self._report()["symbols"]
        self.assertEqual(symbols["AKFIS"]["status"], "compared")
        self.assertEqual(symbols["ZZZZZ"]["status"], "failed:packet_hash_mismatch")

    def test_missing_base_capture_is_a_per_symbol_failure(self):
        self._env()
        self._seed("AKFIS")
        (self.base / "AKFIS" / BASE_NAME).unlink()
        code, _out, _err = self._run("--symbols", "AKFIS")
        self.assertEqual(code, 1)
        self.assertEqual(self._report()["symbols"]["AKFIS"]["status"], "failed:base_missing")

    # -- output ------------------------------------------------------------------------------

    def test_report_records_inputs_hashes_commit_and_no_values_or_codes(self):
        self._env()
        self._seed()
        code, out, err = self._run("--symbols", "AKFIS")
        self.assertEqual(code, 0, err)
        report_path = next((self.daily / "_d0").glob("*.json"))
        text = report_path.read_text(encoding="utf-8")
        report = json.loads(text)
        validate_context_contains_no_secrets(report)  # same check the writer ran
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)
            self.assertNotIn(sentinel, out)
        inputs = report["symbols"]["AKFIS"]["inputs"]
        base_path = self.base / "AKFIS" / BASE_NAME
        self.assertEqual(inputs["base"]["sha256"], hashlib.sha256(base_path.read_bytes()).hexdigest())
        self.assertEqual(len(inputs["daily"]), 1)
        self.assertEqual(len(inputs["daily"][0]["sha256"]), 64)
        self.assertEqual(report["cutoff_date"], "2026-09-25")
        self.assertIn("code_commit", report)
        self.assertEqual(report["schema"], "fintr_d0_comparison_v1")

        allowed_count_keys = {
            "compared_days", "matched_days", "match_rate", "differing_days",
            "differing_code_count_total", "only_base", "only_daily",
        }

        def leaves(value):
            if isinstance(value, dict):
                for nested in value.values():
                    yield from leaves(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from leaves(nested)
            else:
                yield value

        for counts in report["symbols"]["AKFIS"]["families"].values():
            self.assertEqual(set(counts), allowed_count_keys)
        for leaf in leaves(report):
            self.assertIn(type(leaf), (str, int, float, bool, type(None)))

    def test_report_is_never_overwritten(self):
        self._env()
        self._seed()
        now = datetime(2026, 9, 28, 8, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(self._run(now=now)[0], 0)
        target = self.daily / "_d0" / "2026-09-28T08-00-00Z.json"
        original = target.read_bytes()
        code, _out, err = self._run(now=now)
        self.assertEqual(code, 1)
        self.assertIn("no overwrite", err)
        self.assertEqual(target.read_bytes(), original)

    def test_dry_run_lists_files_loads_and_writes_nothing(self):
        self._env()
        self._seed()
        with patch.object(d0, "load_period_capture", side_effect=AssertionError("must not load")):
            code, out, err = self._run("--symbols", "AKFIS", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("2026-09-18_2026-09-28.json", out)
        self.assertIn("skipped after cutoff: 1", out)
        self.assertIn("present", out)
        self.assertFalse((self.daily / "_d0").exists())


if __name__ == "__main__":
    unittest.main()
