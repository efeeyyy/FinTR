"""Offline tests for the daily analysis note (``fintr.llm_note``,
``scripts/llm_note.py``, the notifier's ``--llm-note-dir`` and the pipeline's
``--llm-note``).

No network and no real model: the Claude Code CLI is replaced by
``tests/fixtures/fake_claude_cli.py`` (behaviour chosen by an environment
variable). All symbols, brokers, dates and numbers are synthetic.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fintr.data.period_capture import canonical_json
from fintr.llm_note import numbers, packet as packet_mod, render, runner
from fintr.llm_note.config import ConfigError, load_config
from fintr.llm_note.forbidden import find_forbidden
from fintr.llm_note.loader import (
    discover_price_history, find_daily_captures, find_period_capture, load_composed_capture,
)
from fintr.llm_note.packet import GuardRefusal, PacketError, build_packet
from fintr.llm_note.record import latest_accepted, latest_attempt
from fintr.llm_note.validate import resolve_pointer, validate_note
from fintr.evaluation.symbol_guard import load_active_guards
from fintr.notify import format as fmt
from fintr.notify.telegram import SendResult
from fintr.scanner.rules import load_scanner_rules

import test_period_analysis as period_fixtures
from test_symbol_guard import DEVELOPMENT, EXCLUDED, HOLDOUT, GuardEnvMixin


ROOT = Path(__file__).resolve().parents[1]
FAKE_CLI = ROOT / "tests" / "fixtures" / "fake_claude_cli.py"
REAL_RULES_FILE = ROOT / "configs" / "scanner" / "rules_v1.json"
REAL_CONFIG = ROOT / "configs" / "llm_note" / "config_v1.json"
REAL_RULE_TEXTS = ROOT / "configs" / "notify" / "rule_texts_v1.json"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"llm_note_test_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = _load_script("scan_observations")
llm_script = _load_script("llm_note")
notify = _load_script("notify_observations")
pipeline = _load_script("run_daily_pipeline")

AS_OF = "2026-01-09"
SYM_A, SYM_B = DEVELOPMENT  # both get an H2 observation on AS_OF
TITLE_A, TITLE_B = "Alfa Yatırım Menkul", "Hedef Menkul Değerler"  # TITLE_B contains a forbidden stem
DIRECTORY = (("AAA", TITLE_A, "Alfa"), ("BBB", TITLE_B, "Hedef"))
POST_AS_OF_MARKER = "777001"  # appears only in rows dated after AS_OF


def _capture_document(symbol: str, *, start: str, end: str, akd: dict, custody: dict,
                      captured_at_utc: str) -> dict:
    """Synthetic, hash-valid v2 period capture. ``akd``: date -> code ->
    (net, size, cost); ``custody``: date -> code -> lot."""
    dates = sorted(akd, reverse=True)
    packet = {
        "schema": "fintr_period_analysis_handoff_v2",
        "requested_period": {"symbol": symbol, "start_date": start, "end_date": end,
                             "origin": "local_cli_arguments"},
        "get_akd_period": {"rows_in_provider_order": []},
        "custody_analysis_daily": {
            "provenance": {"server_echo": {"dates_in_provider_order": dates}},
            "daily_akd_flow_in_provider_order": [
                {"date": d, "rows": [
                    {"broker_code": code, "net_size": Decimal(net), "total_size": Decimal(size),
                     "total_cost": Decimal(cost)}
                    for code, (net, size, cost) in akd[d].items()]}
                for d in dates],
            "daily_custody_balance_in_provider_order": [
                {"date": d, "rows": [{"broker_code": code, "lot_count": Decimal(lot)}
                                     for code, lot in custody[d].items()]}
                for d in dates],
            "metadata_in_provider_order": [
                {"date": d, "null_fields": [], "retail_pct": Decimal("40.5"),
                 "institutional_pct": Decimal("59.5"), "investor_count": 1234}
                for d in dates],
        },
        "get_custodian_diff_period": {
            "provenance": {"server_echo": {"start_date": start, "end_date": end}},
            "rows_in_provider_order": [],
        },
        "brokerage_directory": {
            "row_count": len(DIRECTORY),
            "rows": [{"code": c, "title": t, "short_title": s} for c, t, s in DIRECTORY],
        },
    }
    return {
        "capture_metadata": {
            "schema": "fintr_period_analysis_capture_v1", "captured_at_utc": captured_at_utc,
            "packet_schema": packet["schema"],
            "packet_sha256_canonical_json": hashlib.sha256(canonical_json(packet).encode("utf-8")).hexdigest(),
        },
        "packet": packet,
    }


def _akd_day(net_a: str = "100", net_b: str = "-40") -> dict:
    # vwap proxy 10.5 vs close 10 -> vwap_proxy_vs_close = 0.05 -> H2 condition holds.
    return {"AAA": (net_a, "1000", "10.5"), "BBB": (net_b, "500", "10.5")}


def write_fixture_data(root: Path, symbols=(SYM_A, SYM_B)) -> tuple[Path, Path, Path]:
    """Base 01-05..01-09, one rolling capture 01-07..01-13 (rows dated after
    AS_OF carry POST_AS_OF_MARKER), prices 01-05..01-13."""
    period_root, daily_root, price_root = root / "period_analysis", root / "daily_capture", root / "price_history"
    base_dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    daily_dates = ["2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12", "2026-01-13"]
    for symbol in symbols:
        base = _capture_document(
            symbol, start="2026-01-05", end="2026-01-09",
            akd={d: _akd_day() for d in base_dates},
            custody={d: {"AAA": "5000", "BBB": "3000"} for d in base_dates},
            captured_at_utc="2026-01-09T12:00:00Z")
        period_fixtures._write_capture(period_root, symbol, "2026-01-05", "2026-01-09", base)
        akd = {d: _akd_day() for d in daily_dates[:3]}
        akd["2026-01-12"] = _akd_day(POST_AS_OF_MARKER, "-40")
        akd["2026-01-13"] = _akd_day("5", POST_AS_OF_MARKER)
        daily = _capture_document(
            symbol, start="2026-01-07", end="2026-01-13", akd=akd,
            custody={d: {"AAA": "5000", "BBB": "3000"} for d in daily_dates},
            captured_at_utc="2026-01-13T09:00:00Z")
        period_fixtures._write_capture(daily_root, symbol, "2026-01-07", "2026-01-13", daily)
        bars = [period_fixtures._price_bar(d, o="10", h="10", l="10", c="10", adj="10", v=100)
                for d in base_dates[:5] + ["2026-01-12", "2026-01-13"]]
        period_fixtures._write_price_capture(price_root, symbol, "2026-01-05", "2026-01-13", bars)
    return period_root, daily_root, price_root


def run_scan(period_root: Path, daily_root: Path, price_root: Path, out_dir: Path,
             symbols=(SYM_A, SYM_B), as_of: str = AS_OF) -> Path:
    stdout = io.BytesIO()
    with redirect_stderr(io.StringIO()) as errors, patch.object(
        scan.sys, "stdout", io.TextIOWrapper(stdout, encoding="utf-8"),
    ) as wrapped:
        code = scan.main([
            "--symbols", ",".join(symbols), "--period-dir", str(period_root),
            "--daily-root", str(daily_root), "--price-dir", str(price_root),
            "--rules-file", str(REAL_RULES_FILE), "--as-of", as_of, "--format", "json",
            "--save", "--out-dir", str(out_dir),
        ])
        wrapped.flush()
    assert code == 0, errors.getvalue()
    return out_dir / as_of / "observations.json"


class Env(GuardEnvMixin):
    """Fixture data + a scanner output on disk for each test."""

    symbols = (SYM_A, SYM_B)

    def setUp(self):
        self.make_env()
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.period, self.daily, self.price = write_fixture_data(self.tmp, self.symbols)
        self.scanner_dir = self.tmp / "scanner"
        self.obs_path = run_scan(self.period, self.daily, self.price, self.scanner_dir, self.symbols)
        self.out_dir = self.tmp / "llm_notes"

    def argv(self, *extra: str) -> list[str]:
        return [
            "--as-of", AS_OF, "--scanner-dir", str(self.scanner_dir), "--daily-root", str(self.daily),
            "--base-root", str(self.period), "--price-dir", str(self.price),
            "--out-dir", str(self.out_dir), *extra,
        ]

    def run_note(self, mode: str = "ok", *extra: str, probe: Path | None = None):
        """llm_note.main with the fake CLI; returns (code, stdout, stderr)."""
        env = {"FAKE_CLAUDE_MODE": mode}
        if probe is not None:
            env["FAKE_CLAUDE_PROBE"] = str(probe)
        real_build = runner.build_command

        def fake_build(cli, **kwargs):
            real = real_build(cli, **kwargs)
            return [sys.executable, str(FAKE_CLI), *real[1:]]

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env), patch.object(runner, "build_command", fake_build), \
                patch.object(runner, "cli_version", lambda *a, **k: "fake-cli 0.0.1"), \
                redirect_stdout(out), redirect_stderr(err):
            code = llm_script.main(self.argv(*extra))
        return code, out.getvalue(), err.getvalue()

    def status(self, stdout: str) -> str:
        line = [l for l in stdout.splitlines() if l.startswith("LLM_NOTE_STATUS: ")][-1]
        return line.split(": ", 1)[1]

    def build(self, **overrides):
        document = json.loads(self.obs_path.read_bytes())
        kwargs = dict(
            as_of=date.fromisoformat(AS_OF), document=document,
            observations_sha256=hashlib.sha256(self.obs_path.read_bytes()).hexdigest(),
            rules=load_scanner_rules(REAL_RULES_FILE, project_root=ROOT),
            rule_texts=json.loads(REAL_RULE_TEXTS.read_text(encoding="utf-8"))["rules"],
            project_root=ROOT, guards=load_active_guards(),
            load_capture=lambda s: load_composed_capture(
                s, base_root=self.period, daily_root=self.daily, price_dir=self.price),
            max_packet_bytes=400_000, fallback_window_days=15,
        )
        kwargs.update(overrides)
        return build_packet(**kwargs)


# --- packet -------------------------------------------------------------------


class PacketTests(Env, unittest.TestCase):
    def test_scanner_fixture_has_h2_for_both_symbols(self):
        document = json.loads(self.obs_path.read_bytes())
        for symbol in self.symbols:
            met = [o["rule_id"] for o in document["symbols"][symbol]["scans"][0]["observations"]
                   if o["condition_met"] is True]
            self.assertEqual(met, ["H2"])

    def test_nothing_after_as_of_enters_the_packet(self):
        build = self.build()
        text = build.packet_bytes.decode("utf-8")
        self.assertNotIn(POST_AS_OF_MARKER, text)
        dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", text))
        self.assertTrue(dates)
        self.assertEqual(sorted(d for d in dates if d > AS_OF), [])
        for symbol in self.symbols:
            days = build.packet["symbols"][symbol]["days"]
            self.assertEqual(sorted(days), ["2026-01-07", "2026-01-08", "2026-01-09"])
            self.assertEqual(build.packet["symbols"][symbol]["cutoff"], AS_OF)

    def test_no_price_series_return_or_generation_time(self):
        text = self.build().packet_bytes.decode("utf-8")
        for needle in ("generated_at_utc", "adjusted_close", "price_history", "daily_capture",
                       "period_analysis", "\"close\"", "return"):
            self.assertNotIn(needle, text)

    def test_contents_institutions_and_exact_decimal_strings(self):
        build = self.build()
        block = build.packet["symbols"][SYM_A]
        self.assertEqual(block["days"][AS_OF]["akd"]["AAA"], ["100", "1000", "10.5"])
        self.assertEqual(block["days"][AS_OF]["akd"]["BBB"], ["-40", "500", "10.5"])
        self.assertEqual(block["days"][AS_OF]["custody"], {"AAA": "5000", "BBB": "3000"})
        self.assertEqual(block["institutions"]["AAA"], {"title": TITLE_A, "short_title": "Alfa"})
        self.assertEqual(block["metadata_by_day"][AS_OF]["retail_pct"], "40.5")
        self.assertEqual(block["metadata_by_day"][AS_OF]["investor_count"], 1234)
        self.assertEqual(block["composition"]["unsettled_in_window"] is not None, True)
        self.assertEqual(
            build.packet["required_assessments"],
            [{"symbol": s, "rule_id": "H2", "as_of": AS_OF} for s in sorted(self.symbols)],
        )
        rule = build.packet["rules"]["H2"]
        self.assertEqual(rule["predicted_direction"], "positive")
        self.assertIn("VWAP", rule["description"])
        self.assertEqual(build.packet["observation_file"]["sha256"],
                         hashlib.sha256(self.obs_path.read_bytes()).hexdigest())

    def test_packet_is_deterministic(self):
        self.assertEqual(self.build().packet_bytes, self.build().packet_bytes)

    def test_window_falls_back_to_trailing_days_without_composition(self):
        document = json.loads(self.obs_path.read_bytes())
        for block in document["symbols"].values():
            block.pop("composition")
        build = self.build(document=document, fallback_window_days=2)
        self.assertEqual(sorted(build.packet["symbols"][SYM_A]["days"]), ["2026-01-08", "2026-01-09"])
        self.assertIsNone(build.packet["symbols"][SYM_A]["composition"])

    def test_size_limit_omits_whole_symbols_and_reports_them(self):
        full = self.build()
        build = self.build(max_packet_bytes=len(full.packet_bytes) - 1)
        self.assertEqual(build.included_symbols, [SYM_A])
        self.assertEqual(list(build.omitted), [{"symbol": SYM_B, "reason": "packet_size_limit"}])
        self.assertEqual([r["symbol"] for r in build.packet["required_assessments"]], [SYM_A])
        self.assertLessEqual(len(build.packet_bytes), len(full.packet_bytes) - 1)

    def test_single_symbol_over_limit_is_packet_too_large(self):
        with self.assertRaises(PacketError) as ctx:
            self.build(max_packet_bytes=100)
        self.assertEqual(ctx.exception.category, "packet_too_large")

    def test_holdout_and_excluded_symbols_are_refused(self):
        for bad in (HOLDOUT[0], EXCLUDED):
            document = json.loads(self.obs_path.read_bytes())
            document["symbols"][bad] = document["symbols"][SYM_A]
            with self.assertRaises(GuardRefusal):
                self.build(document=document)

    def test_scan_after_as_of_is_rejected(self):
        document = json.loads(self.obs_path.read_bytes())
        document["symbols"][SYM_A]["scans"][0]["as_of"] = "2026-01-12"
        with self.assertRaises(PacketError) as ctx:
            self.build(document=document)
        self.assertEqual(ctx.exception.category, "scan_after_as_of")

    def test_wrong_schema_and_no_observations(self):
        with self.assertRaises(PacketError):
            self.build(document={"schema": "x", "symbols": {}})
        document = json.loads(self.obs_path.read_bytes())
        for block in document["symbols"].values():
            for obs in block["scans"][0]["observations"]:
                obs["condition_met"] = False
        with self.assertRaises(PacketError) as ctx:
            self.build(document=document)
        self.assertEqual(ctx.exception.category, "no_observations")

    def test_missing_capture_is_a_data_load_error(self):
        with self.assertRaises(PacketError) as ctx:
            self.build(load_capture=lambda s: (_ for _ in ()).throw(ValueError("no_period_capture:" + s)))
        self.assertEqual(ctx.exception.category, "data_load_failed")


class DiscoveryParityTests(unittest.TestCase):
    """The loader's discovery helpers must match the scanner's."""

    def test_same_results_as_scan_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("2026-01-05_2026-01-09.json", "2026-01-05_2026-01-12.json",
                         "2026-01-07_2026-01-13.json", "notes.txt", "x_y.json"):
                (root / "AAA").mkdir(exist_ok=True)
                (root / "AAA" / name).write_text("{}", encoding="utf-8")
            self.assertEqual(find_period_capture(root, "AAA"), scan.find_period_capture(root, "AAA"))
            self.assertEqual(find_daily_captures(root, "AAA"), scan.find_daily_captures(root, "AAA"))
            args = (root, "AAA", date(2026, 1, 6), date(2026, 1, 10))
            self.assertEqual(discover_price_history(*args), scan.discover_price_history(*args))
            self.assertIsNone(find_period_capture(root, "NOPE"))
            self.assertEqual(find_daily_captures(root, "NOPE"), [])


# --- numbers / forbidden --------------------------------------------------------


def _tokens(text: str):
    return numbers.extract_numbers(text)


class NumberTests(unittest.TestCase):
    def allowed(self, *values: str) -> set:
        return {numbers.normalise(Decimal(v)) for v in values}

    def ok(self, text: str, *values: str) -> bool:
        allowed = self.allowed(*values)
        tokens = _tokens(text)
        return bool(tokens) and all(
            t.candidates and numbers.number_in_packet(t, allowed) for t in tokens)

    def test_separator_normalisation(self):
        self.assertTrue(self.ok("1.234.567", "1234567"))
        self.assertTrue(self.ok("1.234,56", "1234.56"))
        self.assertTrue(self.ok("1,234.56", "1234.56"))
        self.assertTrue(self.ok("1234,5", "1234.5"))
        self.assertTrue(self.ok("0,0123", "0.0123"))
        self.assertTrue(self.ok("1.234", "1.234"))   # read as decimal
        self.assertTrue(self.ok("1.234", "1234"))    # read as thousand grouping
        self.assertTrue(self.ok("1,50", "1.5"))      # trailing zeros do not matter

    def test_sign_is_ignored_but_magnitude_must_exist(self):
        self.assertTrue(self.ok("-40", "40"))
        self.assertTrue(self.ok("−40", "-40"))
        self.assertFalse(self.ok("41", "40"))

    def test_percent_matches_value_or_fraction_exactly(self):
        self.assertTrue(self.ok("%5", "0.05"))
        self.assertTrue(self.ok("5%", "0.05"))
        self.assertTrue(self.ok("5 %", "5"))
        self.assertTrue(self.ok("%-1,23", "-0.0123"))
        self.assertFalse(self.ok("%5", "0.051"))  # no rounding tolerance
        self.assertFalse(self.ok("0,05", "5"))    # plain 0.05 is not 5

    def test_unparseable_and_wrong_groupings_fail(self):
        for text in ("1.2.3", "12,34,56", "1.234,5,6"):
            tokens = _tokens(text)
            self.assertTrue(any(not t.candidates for t in tokens), text)

    def test_digits_inside_words_are_still_numbers_but_names_can_be_masked(self):
        self.assertEqual([t.raw for t in _tokens("X5 ve H2")], ["5", "2"])
        masked = numbers.mask_names("kural H2 için ABC1 kodu", ["H2", "ABC1"])
        self.assertEqual(_tokens(masked), [])

    def test_dates_are_extracted_separately(self):
        dates, rest = numbers.extract_dates("2026-01-09 ve 2026-01-12 arasında 40 lot")
        self.assertEqual(dates, ["2026-01-09", "2026-01-12"])
        self.assertEqual([t.raw for t in _tokens(rest)], ["40"])

    def test_packet_number_set(self):
        packet = {"a": ["100", "-40", "10.5", None, True], "n": 7, "text": "oran 1.234,5 ve 2026-01-09",
                  "2026-01-10": {"BBB": ["0.0123"]}}
        allowed, dates = numbers.packet_number_set(packet)
        for value in ("100", "40", "10.5", "7", "1234.5", "0.0123"):
            self.assertIn(numbers.normalise(Decimal(value)), allowed, value)
        self.assertEqual(dates, {"2026-01-09", "2026-01-10"})
        self.assertNotIn(Decimal("2026"), allowed)  # date components are not numbers


class ForbiddenTests(unittest.TestCase):
    def test_flags_advice_forecast_and_intent_wording(self):
        for text in ("Bu hisseyi al", "sat sinyali", "Hedef fiyat", "stop seviyesi", "pozisyon aç",
                     "kesin yükselir", "kesinlikle", "kâr eder", "getiri beklentisi", "tahmin",
                     "öneri", "yatırım tavsiyesi veririz", "puan 7", "skor", "fırsat var",
                     "manipülasyon", "niyetli", "kasıtlı", "buy now", "target price"):
            self.assertTrue(find_forbidden(text), text)

    def test_allows_data_wording_and_scanner_terms(self):
        for text in ("Kesinleşmemiş gün sayısı 1.", "net alıcı konumunda", "alınan veri", "satış hacmi",
                     "takas verisi gecikmeli", "veri durumu belirsiz", "yatırım tavsiyesi değildir"):
            self.assertEqual(find_forbidden(text), [], text)

    def test_allowed_phrases_mask_institution_titles(self):
        self.assertTrue(find_forbidden("Hedef Menkul Değerler net alıcı"))
        self.assertEqual(find_forbidden("Hedef Menkul Değerler net alıcı",
                                        allowed_phrases=("Hedef Menkul Değerler",)), [])

    def test_turkish_capital_i_is_handled(self):
        self.assertTrue(find_forbidden("KESİN"))
        self.assertTrue(find_forbidden("POZİSYON"))


# --- validator --------------------------------------------------------------------


def _good_note(build) -> dict:
    return {
        "assessments": [
            {"symbol": item["symbol"], "rule_id": item["rule_id"], "as_of": item["as_of"],
             "assessment": "destekliyor",
             "reasons": [{"text": f"{AS_OF} tarihinde AAA kodunun net_size değeri 100; takas lot 5000.",
                          "evidence_paths": [f"/symbols/{item['symbol']}/days/{AS_OF}/akd/AAA"]}]}
            for item in build.packet["required_assessments"]
        ],
        "data_caveats": ["Metadata sağlayıcı hesabıdır; doğrulanmadı."],
    }


class ValidatorTests(Env, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.built = self.build()
        self.packet = self.built.packet

    def check(self, note, category=None):
        result = validate_note(note, self.packet)
        self.assertEqual(result.category, category, result)
        self.assertEqual(result.ok, category is None)
        return result

    def mutated(self, fn):
        note = _good_note(self.built)
        fn(note)
        return note

    def test_good_note_passes(self):
        self.check(_good_note(self.built))

    def test_schema_failures(self):
        self.check(None, "schema")
        self.check([], "schema")
        self.check({"assessments": []}, "schema")
        self.check(self.mutated(lambda n: n.update(extra=1)), "schema")
        self.check(self.mutated(lambda n: n["assessments"][0].update(extra=1)), "schema")
        self.check(self.mutated(lambda n: n["assessments"][0].pop("as_of")), "schema")
        self.check(self.mutated(lambda n: n["assessments"][0].update(reasons=[])), "schema")
        self.check(self.mutated(lambda n: n["assessments"][0]["reasons"][0].update(evidence_paths=[])), "schema")
        self.check(self.mutated(lambda n: n["assessments"][0]["reasons"][0].update(text="x" * 401)), "schema")
        self.check(self.mutated(lambda n: n["assessments"][0].update(assessment=1)), "schema")
        self.check(self.mutated(lambda n: n.update(data_caveats="x")), "schema")

    def test_assessment_value(self):
        for value in ("güçlü", "supports", ""):
            self.check(self.mutated(lambda n: n["assessments"][0].update(assessment=value)), "assessment_value")
        for value in ("destekliyor", "zayıflatıyor", "belirsiz"):
            self.check(self.mutated(lambda n: n["assessments"][0].update(assessment=value)))

    def test_coverage(self):
        self.check(self.mutated(lambda n: n["assessments"].pop()), "coverage")
        self.check(self.mutated(lambda n: n["assessments"].append(dict(n["assessments"][0]))), "coverage")
        self.check(self.mutated(lambda n: n["assessments"][0].update(rule_id="H1")), "coverage")
        self.check(self.mutated(lambda n: n["assessments"][0].update(symbol="NOPE")), "coverage")

    def test_evidence_paths_must_resolve(self):
        for path in ("/symbols/NOPE/days", f"/symbols/{SYM_A}/days/2026-01-12", "symbols", "/", "",
                     f"/symbols/{SYM_A}/days/{AS_OF}/akd/AAA/9"):
            self.check(self.mutated(
                lambda n: n["assessments"][0]["reasons"][0].update(evidence_paths=[path])), "evidence_path")
        self.assertTrue(resolve_pointer(self.packet, f"/symbols/{SYM_A}/days/{AS_OF}/akd/AAA/0"))
        self.assertFalse(resolve_pointer(self.packet, f"/symbols/{SYM_A}/days/{AS_OF}/akd/AAA/01"))

    def test_forbidden_phrase(self):
        for text in ("Hedef fiyat yukarı.", "Bu hissede al yönlü görünüm.", "Kesin destekliyor.",
                     "Getiri artar.", "Stop seviyesi 10."):
            self.check(self.mutated(
                lambda n: n["assessments"][0]["reasons"][0].update(text=text)), "forbidden_phrase")
        self.check(self.mutated(lambda n: n["data_caveats"].append("Pozisyon bilgisi yok.")), "forbidden_phrase")

    def test_institution_title_with_a_listed_word_is_allowed(self):
        self.check(self.mutated(lambda n: n["assessments"][0]["reasons"][0].update(
            text=f"{TITLE_B} kodu BBB net_size değeri -40.")))

    def test_numbers_must_exist_in_the_packet(self):
        for text in ("Toplam 987654321 lot.", "Net 101.", "Oran %6.", "Net 1.5 lot.", "77 kurum var.",
                     "Takas 5001."):
            self.check(self.mutated(
                lambda n: n["assessments"][0]["reasons"][0].update(text=text)), "number_not_in_input")
        self.check(self.mutated(lambda n: n["data_caveats"].append("Toplam 31 gün.")), "number_not_in_input")
        # a date that is not in the packet
        self.check(self.mutated(lambda n: n["assessments"][0]["reasons"][0].update(
            text="2026-01-12 tarihinde net 100.")), "number_not_in_input")

    def test_numbers_in_other_formats_match(self):
        for text in ("Takas lot 5.000 ve net 100,0.", "Metadata oranı %40,5; yatırımcı sayısı 1.234.",
                     "Net −40, VWAP 10,5.", "Fark %5 (vwap_proxy_vs_close 0.05).", "H2 ve H1 kuralları."):
            note = self.mutated(lambda n: n["assessments"][0]["reasons"][0].update(text=text))
            result = validate_note(note, self.packet)
            self.assertTrue(result.ok, (text, result))

    def test_rule_and_symbol_ids_are_not_numbers(self):
        self.check(self.mutated(lambda n: n["assessments"][0]["reasons"][0].update(
            text="H2 kuralı için AAA net 100.")))

    def test_sensitive_content(self):
        self.check(self.mutated(lambda n: n["assessments"][0]["reasons"][0].update(
            text="Kaynak https://example.invalid/x net 100.")), "sensitive_content")


# --- runner ------------------------------------------------------------------------


class RunnerTests(unittest.TestCase):
    PACKET = {"required_assessments": [{"symbol": "S", "rule_id": "H2", "as_of": AS_OF}],
              "symbols": {"S": {"days": {AS_OF: {"akd": {"AAA": ["1", "2", "3"]}}}}}}

    def run_fake(self, mode: str, timeout: float = 20, extra_env: dict | None = None):
        stdin = runner.STDIN_HEADER + json.dumps(self.PACKET)
        env = {**os.environ, "FAKE_CLAUDE_MODE": mode, **(extra_env or {})}
        return runner.run_claude([sys.executable, str(FAKE_CLI)], stdin_text=stdin, timeout=timeout, env=env)

    def test_valid_response_structured_output(self):
        outcome = self.run_fake("ok")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.note["assessments"][0]["rule_id"], "H2")
        self.assertEqual(outcome.model_reported, ["claude-opus-5-5"])
        self.assertEqual(outcome.returncode, 0)

    def test_result_text_with_code_fence_is_decoded(self):
        outcome = self.run_fake("result_text")
        self.assertTrue(outcome.ok)
        self.assertIsInstance(outcome.note, dict)

    def test_unusable_result_is_ok_with_no_note(self):
        for mode in ("result_garbage",):
            outcome = self.run_fake(mode)
            self.assertTrue(outcome.ok)
            self.assertIsNone(outcome.note)

    def test_unavailable_categories(self):
        self.assertEqual(self.run_fake("refusal").category, "refusal")
        self.assertEqual(self.run_fake("limit").category, "usage_limit")
        self.assertEqual(self.run_fake("crash").category, "cli_error")
        self.assertEqual(self.run_fake("nonjson").category, "cli_error")
        self.assertEqual(self.run_fake("timeout", timeout=1).category, "timeout")
        missing = runner.run_claude([str(ROOT / "no-such-claude")], stdin_text="x", timeout=5, env=dict(os.environ))
        self.assertEqual((missing.ok, missing.category), (False, "cli_not_found"))
        for outcome in (self.run_fake("refusal"), self.run_fake("limit")):
            self.assertFalse(outcome.ok)

    def test_build_command_flags_and_record_command(self):
        command = runner.build_command("claude", model="claude-opus-5-5", effort="high",
                                       prompt_path=Path("p.md"), schema_text='{"a":1}')
        self.assertEqual(command[:2], ["claude", "-p"])
        for flag, value in (("--model", "claude-opus-5-5"), ("--effort", "high"), ("--tools", ""),
                            ("--output-format", "json"), ("--json-schema", '{"a":1}'),
                            ("--system-prompt-file", "p.md")):
            self.assertEqual(command[command.index(flag) + 1], value, flag)
        self.assertIn("--no-session-persistence", command)
        shown = runner.record_command(command, "abc", "configs/p.md")
        self.assertEqual(shown[0], "<claude-cli>")
        self.assertNotIn('{"a":1}', shown)
        self.assertIn("<output_schema sha256=abc>", shown)
        self.assertIn("configs/p.md", shown)

    def test_resolve_cli_and_child_env(self):
        cfg = {"windows": "%USERPROFILE%\\.local\\bin\\claude.exe", "default": "claude"}
        self.assertEqual(runner.resolve_cli(cfg, "X"), "X")
        self.assertIn("claude", runner.resolve_cli(cfg, None))
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "KEEP": "1"}):
            env = runner.child_env(["ANTHROPIC_API_KEY"])
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["KEEP"], "1")


class ConfigTests(unittest.TestCase):
    def test_real_config_loads_and_hashes_files(self):
        config = load_config(REAL_CONFIG, project_root=ROOT)
        self.assertEqual((config.model, config.effort), ("claude-opus-5-5", "high"))
        self.assertEqual(config.prompt_sha256, hashlib.sha256(config.prompt_path.read_bytes()).hexdigest())
        self.assertEqual(config.schema_sha256, hashlib.sha256(config.schema_path.read_bytes()).hexdigest())
        self.assertEqual(json.loads(config.schema_text)["title"], "fintr_llm_note_v1")
        self.assertIn("{", config.schema_text)
        self.assertNotIn("\n", config.schema_text)

    def test_bad_configs_are_rejected(self):
        document = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            def write(doc) -> Path:
                path = Path(directory) / "c.json"
                path.write_text(json.dumps(doc), encoding="utf-8")
                return path
            for change in ({"schema": "x"}, {"model": ""}, {"timeout_seconds": 0}, {"max_packet_bytes": 1.5},
                           {"prompt_file": "nope.md"}, {"cli_path": {"default": "x"}}, {"strip_env": "x"}):
                with self.assertRaises(ConfigError, msg=str(change)):
                    load_config(write({**document, **change}), project_root=ROOT)
            with self.assertRaises(ConfigError):
                load_config(Path(directory) / "missing.json", project_root=ROOT)

    def test_output_schema_matches_validator_constants(self):
        from fintr.llm_note import validate as v
        schema = json.loads((ROOT / "configs" / "llm_note" / "output_schema_v1.json").read_text(encoding="utf-8"))
        item = schema["properties"]["assessments"]["items"]
        self.assertEqual(set(item["required"]), v._ASSESSMENT_KEYS)
        self.assertEqual(tuple(item["properties"]["assessment"]["enum"]), v.ASSESSMENT_VALUES)
        self.assertEqual(set(schema["required"]), v._TOP_KEYS)
        reason = item["properties"]["reasons"]["items"]
        self.assertEqual(set(reason["required"]), v._REASON_KEYS)

    def test_prompt_states_the_hard_rules(self):
        prompt = (ROOT / "configs" / "llm_note" / "prompt_v1.md").read_text(encoding="utf-8")
        for needle in ("destekliyor", "zayıflatıyor", "belirsiz", "bilinmiyor", "JSON Pointer",
                       "Hesap yapma", "manipülasyon", "pozisyon", "hedef", "stop"):
            self.assertIn(needle, prompt)


# --- script (llm_note.py) ---------------------------------------------------------------


class ScriptTests(Env, unittest.TestCase):
    def attempts(self):
        parent = self.out_dir / AS_OF
        return sorted(p.name for p in parent.iterdir()) if parent.exists() else []

    def test_accepted_note_is_recorded_without_secrets_and_with_lineage(self):
        probe = self.tmp / "probe.json"
        code, out, err = self.run_note("ok", probe=probe)
        self.assertEqual((code, self.status(out)), (0, "accepted"), err)
        self.assertEqual(self.attempts(), ["attempt-001"])
        attempt = self.out_dir / AS_OF / "attempt-001"
        self.assertEqual(sorted(p.name for p in attempt.iterdir()),
                         ["note.json", "packet.json", "record.json", "response_raw.txt"])
        record = json.loads((attempt / "record.json").read_text(encoding="utf-8"))
        config = load_config(REAL_CONFIG, project_root=ROOT)
        packet_bytes = (attempt / "packet.json").read_bytes()
        self.assertEqual(record["status"], "accepted")
        self.assertIsNone(record["category"])
        self.assertEqual(record["inputs"]["packet_sha256"], hashlib.sha256(packet_bytes).hexdigest())
        self.assertEqual(record["inputs"]["observations_sha256"],
                         hashlib.sha256(self.obs_path.read_bytes()).hexdigest())
        self.assertEqual(record["inputs"]["included_symbols"], sorted(self.symbols))
        self.assertEqual(record["prompt"]["sha256"], config.prompt_sha256)
        self.assertEqual(record["output_schema"]["sha256"], config.schema_sha256)
        self.assertEqual(record["config"]["sha256"], config.sha256)
        self.assertEqual(record["model_requested"], "claude-opus-5-5")
        self.assertEqual(record["model_reported"], ["claude-opus-5-5"])
        self.assertEqual(record["cli"]["version"], "fake-cli 0.0.1")
        self.assertIn("determin", record["determinism_note"].lower())
        self.assertEqual(record["validation"]["ok"], True)
        command = record["command"]
        self.assertEqual(command[0], "<claude-cli>")
        for flag in ("--model", "--effort", "--tools", "--output-format", "--json-schema",
                     "--no-session-persistence", "--system-prompt-file"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--system-prompt-file") + 1], "configs/llm_note/prompt_v1.md")
        self.assertNotIn(str(ROOT), json.dumps(record["command"][2:]))
        self.assertTrue(json.loads((attempt / "note.json").read_text(encoding="utf-8"))["assessments"])
        # the packet file is the very packet validated and sent
        self.assertEqual(json.loads(packet_bytes)["required_assessments"],
                         [{"symbol": s, "rule_id": "H2", "as_of": AS_OF} for s in sorted(self.symbols)])
        # the child saw: no API key variables, an empty temp cwd, the flags
        seen = json.loads(probe.read_text(encoding="utf-8"))
        self.assertFalse(seen["has_api_key"] or seen["has_auth_token"])
        self.assertNotEqual(Path(seen["cwd"]).resolve(), ROOT.resolve())
        self.assertIn("--no-session-persistence", seen["argv"])
        self.assertEqual(seen["argv"][seen["argv"].index("--model") + 1], "claude-opus-5-5")
        from fintr.evaluation.live_capture import validate_context_contains_no_secrets
        for name in ("record.json", "note.json"):
            validate_context_contains_no_secrets(json.loads((attempt / name).read_text(encoding="utf-8")))

    def test_api_key_variables_never_reach_the_child(self):
        probe = self.tmp / "probe.json"
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_AUTH_TOKEN": "t"}):
            self.run_note("ok", probe=probe)
        seen = json.loads(probe.read_text(encoding="utf-8"))
        self.assertFalse(seen["has_api_key"])
        self.assertFalse(seen["has_auth_token"])

    def test_accepted_note_is_not_regenerated_and_never_overwritten(self):
        self.run_note("ok")
        before = {p: p.read_bytes() for p in (self.out_dir / AS_OF).rglob("*") if p.is_file()}
        code, out, _ = self.run_note("refusal")  # would fail if the model were called
        self.assertEqual((code, self.status(out)), (0, "already_noted"))
        after = {p: p.read_bytes() for p in (self.out_dir / AS_OF).rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_failed_attempt_allows_a_new_attempt_and_keeps_the_old_one(self):
        code, out, err = self.run_note("refusal")
        self.assertEqual((code, self.status(out)), (3, "unavailable:refusal"))
        first = (self.out_dir / AS_OF / "attempt-001" / "record.json").read_bytes()
        code, out, _ = self.run_note("ok")
        self.assertEqual((code, self.status(out)), (0, "accepted"))
        self.assertEqual(self.attempts(), ["attempt-001", "attempt-002"])
        self.assertEqual((self.out_dir / AS_OF / "attempt-001" / "record.json").read_bytes(), first)
        self.assertEqual(latest_accepted(self.out_dir, AS_OF)[0].name, "attempt-002")

    def test_write_new_refuses_to_overwrite(self):
        from fintr.llm_note.record import write_new
        target = self.tmp / "f.json"
        write_new(target, b"1")
        with self.assertRaises(FileExistsError):
            write_new(target, b"2")
        self.assertEqual(target.read_bytes(), b"1")

    def test_rejections_name_the_category_and_are_recorded(self):
        for mode, category in (("schema_bad", "schema"), ("bad_enum", "assessment_value"),
                               ("forbidden", "forbidden_phrase"), ("forbidden_buy", "forbidden_phrase"),
                               ("bad_number", "number_not_in_input"), ("bad_path", "evidence_path"),
                               ("missing", "coverage"), ("secret", "sensitive_content"),
                               ("result_garbage", "schema")):
            with self.subTest(mode=mode):
                out_dir = self.tmp / f"notes-{mode}"
                self.out_dir = out_dir
                code, out, err = self.run_note(mode)
                self.assertEqual(code, 3, (mode, err))
                self.assertEqual(self.status(out), f"rejected:{category}")
                self.assertIn(f"analiz notu doğrulamadan geçemedi ({category})", err)
                attempt = out_dir / AS_OF / "attempt-001"
                record = json.loads((attempt / "record.json").read_text(encoding="utf-8"))
                self.assertEqual((record["status"], record["category"]), ("rejected", category))
                self.assertFalse(record["validation"]["ok"])
                self.assertFalse((attempt / "note.json").exists())
                self.assertIsNone(latest_accepted(out_dir, AS_OF))

    def test_unavailable_categories_and_flow_continues(self):
        for mode, category in (("refusal", "refusal"), ("limit", "usage_limit"), ("crash", "cli_error"),
                               ("nonjson", "cli_error")):
            with self.subTest(mode=mode):
                self.out_dir = self.tmp / f"notes-{mode}"
                code, out, err = self.run_note(mode)
                self.assertEqual((code, self.status(out)), (3, f"unavailable:{category}"))
                self.assertIn(f"analiz notu alınamadı ({category})", err)
                record = json.loads((self.out_dir / AS_OF / "attempt-001" / "record.json").read_text(encoding="utf-8"))
                self.assertEqual((record["status"], record["category"]), ("unavailable", category))
                self.assertIsNone(record["validation"])

    def test_timeout(self):
        code, out, err = self.run_note("timeout", "--timeout-seconds", "1")
        self.assertEqual((code, self.status(out)), (3, "unavailable:timeout"))

    def test_missing_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(runner, "cli_version", lambda *a, **k: "unknown"), \
                redirect_stdout(out), redirect_stderr(err):
            code = llm_script.main(self.argv("--claude-path", str(self.tmp / "no-claude")))
        self.assertEqual((code, self.status(out.getvalue())), (3, "unavailable:cli_not_found"))

    def test_dry_run_builds_the_packet_but_calls_and_writes_nothing(self):
        code, out, err = self.run_note("refusal", "--dry-run")
        self.assertEqual((code, self.status(out)), (0, "dry_run"))
        self.assertFalse(self.out_dir.exists())
        self.assertIn("girdi paketi", err)

    def test_no_condition_met_means_no_call(self):
        document = json.loads(self.obs_path.read_bytes())
        for block in document["symbols"].values():
            for obs in block["scans"][0]["observations"]:
                obs["condition_met"] = False
        self.obs_path.write_text(json.dumps(document), encoding="utf-8")
        code, out, _ = self.run_note("refusal")
        self.assertEqual((code, self.status(out)), (0, "no_observations"))
        self.assertFalse(self.out_dir.exists())

    def test_holdout_or_excluded_symbol_is_refused_before_any_call(self):
        for bad in (HOLDOUT[0], EXCLUDED):
            document = json.loads(self.obs_path.read_bytes())
            document["symbols"][bad] = document["symbols"][SYM_A]
            self.obs_path.write_text(json.dumps(document), encoding="utf-8")
            code, out, err = self.run_note("ok")
            self.assertEqual((code, self.status(out)), (1, "refused:guard"))
            self.assertIn(bad, err)
            self.assertFalse(self.out_dir.exists())

    def test_input_errors(self):
        code, out, _ = self.run_note("ok", "--scanner-dir", str(self.tmp / "nope"))
        self.assertEqual((code, self.status(out)), (1, "error:observations_unreadable"))
        self.obs_path.write_text(json.dumps({"schema": "x", "symbols": {}}), encoding="utf-8")
        code, out, _ = self.run_note("ok")
        self.assertEqual((code, self.status(out)), (1, "error:observations_schema"))

    def test_missing_capture_is_an_error_not_a_call(self):
        code, out, _ = self.run_note("ok", "--base-root", str(self.tmp / "empty"))
        self.assertEqual((code, self.status(out)), (1, "error:data_load_failed"))
        self.assertFalse(self.out_dir.exists())

    def test_packet_too_large_is_unavailable_without_a_call(self):
        document = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
        document["max_packet_bytes"] = 100
        small = self.tmp / "small.json"
        small.write_text(json.dumps(document), encoding="utf-8")
        code, out, _ = self.run_note("refusal", "--config", str(small))
        self.assertEqual((code, self.status(out)), (3, "unavailable:packet_too_large"))
        record = json.loads((self.out_dir / AS_OF / "attempt-001" / "record.json").read_text(encoding="utf-8"))
        self.assertEqual((record["status"], record["category"]), ("unavailable", "packet_too_large"))

    def test_omitted_symbols_are_recorded(self):
        full = len(self.build().packet_bytes)
        document = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
        document["max_packet_bytes"] = full - 1
        small = self.tmp / "omit.json"
        small.write_text(json.dumps(document), encoding="utf-8")
        code, out, _ = self.run_note("ok", "--config", str(small))
        self.assertEqual((code, self.status(out)), (0, "accepted"))
        record = json.loads((self.out_dir / AS_OF / "attempt-001" / "record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["inputs"]["included_symbols"], [SYM_A])
        self.assertEqual(record["inputs"]["omitted"], [{"symbol": SYM_B, "reason": "packet_size_limit"}])

    def test_stdin_carries_exactly_the_recorded_packet(self):
        self.run_note("ok")
        attempt = self.out_dir / AS_OF / "attempt-001"
        record = json.loads((attempt / "record.json").read_text(encoding="utf-8"))
        stdin = runner.STDIN_HEADER + (attempt / "packet.json").read_text(encoding="utf-8")
        self.assertEqual(record["inputs"]["stdin_sha256"], hashlib.sha256(stdin.encode("utf-8")).hexdigest())


# --- notifier ------------------------------------------------------------------------------


_FORBIDDEN = ("score", "rank", "return", "target", "al", "sat", "öneri", "kesin", "hedef", "stop", "kâr")


def _clean(text: str) -> str:
    lowered = text.lower()
    for phrase in fmt.DIRECTION_LABELS_TR.values():
        lowered = lowered.replace(phrase.lower(), "")
    return lowered


class NotifyIntegrationTests(Env, unittest.TestCase):
    def notify_argv(self, *extra: str) -> list[str]:
        return ["--as-of", AS_OF, "--scanner-dir", str(self.scanner_dir),
                "--out-dir", str(self.tmp / "receipts"), *extra]

    def dry_run(self, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = notify.main(self.notify_argv(*extra))
        return code, out.getvalue(), err.getvalue()

    def messages(self, *extra: str) -> list[str]:
        code, out, err = self.dry_run(*extra)
        self.assertEqual(code, 0, err)
        parts = re.split(r"--- mesaj \d+/\d+ \(dry-run; ağ kullanılmadı\) ---\n", out)
        return [p for p in parts if p]

    def test_without_the_flag_output_is_unchanged(self):
        self.run_note("ok")  # a verified note exists on disk, but the flag is off
        document = json.loads(self.obs_path.read_bytes())
        rule_texts = json.loads(REAL_RULE_TEXTS.read_text(encoding="utf-8"))["rules"]
        directions = {"H1": "negative", "H2": "positive", "H3": "negative", "H4": "positive"}
        expected = fmt.render_notification(document, rule_texts, directions)
        self.assertEqual(self.messages(), [m + "\n" for m in expected])
        plain = fmt.render_notification(document, rule_texts, None)
        self.assertEqual(self.messages("--no-direction-label"), [m + "\n" for m in plain])
        self.assertNotIn("analiz notu", "".join(self.messages()).lower())

    def test_verified_note_is_a_separate_message_with_fixed_framing(self):
        self.run_note("ok")
        base = self.messages()
        with_note = self.messages("--llm-note-dir", str(self.out_dir))
        self.assertEqual(with_note[:len(base)], base)
        note_messages = with_note[len(base):]
        self.assertEqual(len(note_messages), 1)
        text = note_messages[0]
        self.assertTrue(text.startswith(render.TITLE))
        self.assertIn(f"as_of: {AS_OF}", text)
        self.assertIn(fmt.DIRECTION_WARNING_TR, text)
        for symbol in self.symbols:
            self.assertIn(f"{symbol} H2 (beklenen yön: yukarı (al yönlü)) — değerlendirme: destekliyor", text)
        self.assertIn("• Takas verisi gecikmeli olabilir.", text)
        self.assertIn(render.VERIFIED_FOOTER, text)
        self.assertIn("attempt-001", text)
        self.assertIn("Durum: preregistered_hypothesis_holdout_test_pending", text)
        self.assertLessEqual(len(text), fmt.TELEGRAM_MESSAGE_LIMIT)

    def test_no_direction_label_flag_removes_the_label_from_the_note_too(self):
        self.run_note("ok")
        text = self.messages("--llm-note-dir", str(self.out_dir), "--no-direction-label")[-1]
        for phrase in (*fmt.DIRECTION_LABELS_TR.values(), fmt.DIRECTION_WARNING_TR, "yön bilinmiyor"):
            self.assertNotIn(phrase, text)
        self.assertIn(f"{SYM_A} H2 — değerlendirme: destekliyor", text)

    def test_note_message_has_no_forbidden_words_outside_the_fixed_label(self):
        self.run_note("ok")
        text = self.messages("--llm-note-dir", str(self.out_dir))[-1]
        body = _clean(text)
        for word in _FORBIDDEN:
            self.assertIsNone(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", body), word)

    def test_status_lines_when_there_is_no_note(self):
        argv = ("--llm-note-dir", str(self.out_dir))
        last = lambda: self.messages(*argv)[-1].strip()
        self.assertEqual(last(), "Opus 5.5 analiz notu: bu as_of için not yok (adım çalıştırılmadı).")
        self.run_note("limit")
        self.assertEqual(last(), "Opus 5.5 analiz notu: analiz notu alınamadı (usage_limit).")
        self.out_dir = self.tmp / "rej"
        self.run_note("bad_number")
        self.assertEqual(
            self.messages("--llm-note-dir", str(self.out_dir))[-1].strip(),
            "Opus 5.5 analiz notu: analiz notu doğrulamadan geçemedi (number_not_in_input).")
        for text in ("analiz notu alınamadı (usage_limit)", "analiz notu doğrulamadan geçemedi (schema)"):
            self.assertEqual(find_forbidden(text), [])

    def test_rejected_note_content_is_never_sent(self):
        self.run_note("forbidden")
        text = "\n".join(self.messages("--llm-note-dir", str(self.out_dir)))
        self.assertNotIn("Hedef fiyat yükselir", text)
        self.assertNotIn("net_size değeri", text)

    def test_tampered_note_is_not_shown(self):
        self.run_note("ok")
        note_path = self.out_dir / AS_OF / "attempt-001" / "note.json"
        note = json.loads(note_path.read_text(encoding="utf-8"))
        note["assessments"][0]["reasons"][0]["text"] = "Toplam 987654321 lot."
        note_path.write_text(json.dumps(note), encoding="utf-8")
        last = self.messages("--llm-note-dir", str(self.out_dir))[-1].strip()
        self.assertEqual(last, "Opus 5.5 analiz notu: analiz notu doğrulamadan geçemedi (record_integrity).")
        self.assertNotIn("987654321", last)

    def test_tampered_packet_is_not_shown(self):
        self.run_note("ok")
        packet_path = self.out_dir / AS_OF / "attempt-001" / "packet.json"
        packet_path.write_bytes(packet_path.read_bytes() + b" ")
        last = self.messages("--llm-note-dir", str(self.out_dir))[-1].strip()
        self.assertIn("record_integrity", last)

    def test_long_note_is_split_and_every_chunk_keeps_the_title_and_warning(self):
        self.run_note("ok")
        attempt = self.out_dir / AS_OF / "attempt-001"
        record = json.loads((attempt / "record.json").read_text(encoding="utf-8"))
        note, packet = render.load_verified_note(attempt, record)
        note = json.loads(json.dumps(note))
        note["data_caveats"] = ["Takas verisi gecikmeli olabilir. " * 11] * 8
        chunks = render.render_note_messages(
            attempt, record, note, packet,
            directions={"H2": "positive"}, limit=700)
        self.assertGreater(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 700)
            self.assertTrue(chunk.startswith(render.TITLE))
            self.assertEqual(chunk.count(fmt.DIRECTION_WARNING_TR), 1)
        self.assertIn("Durum: preregistered_hypothesis_holdout_test_pending", chunks[-1])

    def test_omitted_symbols_are_listed_in_the_note(self):
        full = len(self.build().packet_bytes)
        document = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
        document["max_packet_bytes"] = full - 1
        small = self.tmp / "omit.json"
        small.write_text(json.dumps(document), encoding="utf-8")
        self.run_note("ok", "--config", str(small))
        text = self.messages("--llm-note-dir", str(self.out_dir))[-1]
        self.assertIn(f"Kapsam dışı (girdi paketi boyut sınırı): {SYM_B}", text)
        self.assertNotIn(f"{SYM_B} H2", text)

    def test_send_includes_note_messages_in_one_receipt(self):
        self.run_note("ok")
        sent = []

        def fake_send(token, chat_id, text, **kwargs):
            sent.append(text)
            return SendResult(message_id=len(sent))

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"FINTR_TELEGRAM_BOT_TOKEN": "123:abc", "FINTR_TELEGRAM_CHAT_ID": "9"}), \
                patch.object(notify, "send_message", fake_send), redirect_stdout(out), redirect_stderr(err):
            code = notify.main(self.notify_argv("--send", "--llm-note-dir", str(self.out_dir)))
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue(sent[-1].startswith(render.TITLE))
        receipt = json.loads((self.tmp / "receipts" / AS_OF / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["llm_note_messages"], 1)
        self.assertEqual(len(receipt["message_sha256"]), len(sent))
        self.assertEqual(receipt["message_sha256"][-1], hashlib.sha256(sent[-1].encode("utf-8")).hexdigest())
        with patch.dict(os.environ, {"FINTR_TELEGRAM_BOT_TOKEN": "123:abc", "FINTR_TELEGRAM_CHAT_ID": "9"}), \
                patch.object(notify, "send_message", fake_send), redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()) as again:
            code = notify.main(self.notify_argv("--send", "--llm-note-dir", str(self.out_dir)))
        self.assertEqual(code, 1)  # existing receipt still blocks a re-send

    def test_send_without_the_flag_has_no_llm_receipt_field(self):
        sent = []
        with patch.dict(os.environ, {"FINTR_TELEGRAM_BOT_TOKEN": "123:abc", "FINTR_TELEGRAM_CHAT_ID": "9"}), \
                patch.object(notify, "send_message", lambda t, c, text, **k: (sent.append(text), SendResult(1))[1]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(notify.main(self.notify_argv("--send")), 0)
        receipt = json.loads((self.tmp / "receipts" / AS_OF / "receipt.json").read_text(encoding="utf-8"))
        self.assertNotIn("llm_note_messages", receipt)


# --- pipeline ------------------------------------------------------------------------------


SAVED = os.path.join("data", "interim", "scanner_daily", AS_OF, "observations.json")
STEP = {"collect_daily.py": 0, "scan_observations.py": 1, "llm_note.py": 2, "notify_observations.py": 3}


def _go(argv, llm_stdout="LLM_NOTE_STATUS: accepted\n", llm_code=0, llm_raises=None, notify_code=0):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        name = Path(cmd[1]).name
        if name == "scan_observations.py":
            return subprocess.CompletedProcess(cmd, 0, stdout="OBS", stderr=f"Observations saved: {SAVED}\n")
        if name == "llm_note.py":
            if llm_raises:
                raise llm_raises
            return subprocess.CompletedProcess(cmd, llm_code, stdout=llm_stdout, stderr="note stderr\n")
        if name == "notify_observations.py":
            return subprocess.CompletedProcess(cmd, notify_code, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    out, err = io.StringIO(), io.StringIO()
    with patch.object(pipeline.subprocess, "run", run), redirect_stdout(out), redirect_stderr(err):
        rc = pipeline.main(argv)
    return rc, calls, out.getvalue(), err.getvalue()


class PipelineTests(unittest.TestCase):
    def names(self, calls):
        return [Path(c[1]).name for c in calls]

    def test_default_off_behaviour_is_unchanged(self):
        rc, calls, out, _ = _go(["--allow-session-takeover"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.names(calls), ["collect_daily.py", "scan_observations.py", "notify_observations.py"])
        self.assertEqual(calls[2][2:], ["--as-of", AS_OF, "--scanner-dir", pipeline.SCANNER_DIR])
        self.assertNotIn("llm_note", out)
        self.assertEqual(pipeline.build_parser().parse_args([]).llm_note, False)

    def test_flag_adds_the_step_between_scan_and_notify(self):
        rc, calls, out, err = _go(["--allow-session-takeover", "--send", "--llm-note"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.names(calls),
                         ["collect_daily.py", "scan_observations.py", "llm_note.py", "notify_observations.py"])
        self.assertEqual(calls[2][2:], ["--as-of", AS_OF, "--scanner-dir", pipeline.SCANNER_DIR,
                                        "--daily-root", pipeline.DAILY_ROOT, "--out-dir", pipeline.LLM_NOTES_DIR])
        self.assertEqual(calls[3][2:], ["--as-of", AS_OF, "--scanner-dir", pipeline.SCANNER_DIR, "--send",
                                        "--llm-note-dir", pipeline.LLM_NOTES_DIR])
        self.assertNotIn("--resend", calls[3])
        self.assertIn("llm_note: accepted", out)
        self.assertIn("note stderr", err)

    def test_note_failure_never_stops_the_pipeline(self):
        cases = [
            dict(llm_stdout="LLM_NOTE_STATUS: unavailable:usage_limit\n", llm_code=3),
            dict(llm_stdout="LLM_NOTE_STATUS: rejected:number_not_in_input\n", llm_code=3),
            dict(llm_stdout="LLM_NOTE_STATUS: error:data_load_failed\n", llm_code=1),
            dict(llm_stdout="", llm_code=1),
            dict(llm_stdout="garbage\n", llm_code=0),
            dict(llm_raises=OSError("boom")),
            dict(llm_raises=RuntimeError("boom")),
        ]
        for case in cases:
            with self.subTest(case=case):
                rc, calls, out, _ = _go(["--llm-note", "--send"], **case)
                self.assertEqual(rc, 0)
                self.assertEqual(self.names(calls)[-1], "notify_observations.py")
                self.assertIn("--send", calls[-1])
                self.assertRegex(out, r"llm_note: \S+")
                self.assertIn("notify: 0", out)

    def test_status_is_taken_from_the_status_line(self):
        _, _, out, _ = _go(["--llm-note"], llm_stdout="x\nLLM_NOTE_STATUS: already_noted\n")
        self.assertIn("llm_note: already_noted", out)
        _, _, out, _ = _go(["--llm-note"], llm_stdout="", llm_code=5)
        self.assertIn("llm_note: error:exit_5", out)

    def test_notify_failure_is_still_reported_with_the_note_step(self):
        rc, calls, out, _ = _go(["--llm-note"], notify_code=4)
        self.assertEqual(rc, 4)
        self.assertIn("llm_note: accepted", out)
        self.assertIn("failed with exit code 4", out)

    def test_same_day_rerun_with_existing_note_is_idempotent(self):
        rc, calls, out, _ = _go(["--skip-collect", "--llm-note"], llm_stdout="LLM_NOTE_STATUS: already_noted\n")
        self.assertEqual(rc, 0)
        self.assertEqual(self.names(calls), ["scan_observations.py", "llm_note.py", "notify_observations.py"])

    def test_dry_run_and_scan_failure_skip_the_note(self):
        rc, calls, _, _ = _go(["--dry-run", "--llm-note"])
        self.assertEqual(self.names(calls), ["collect_daily.py"])


class RealPipelineSmokeTests(Env, unittest.TestCase):
    """The real ``llm_note.py`` as a subprocess, ``--dry-run``: argument
    wiring and the status-line contract, no model call."""

    def test_status_line_contract_over_a_subprocess(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "llm_note.py"), *self.argv("--dry-run")],
            capture_output=True, text=True, encoding="utf-8", cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        # Real repo guards apply in the subprocess; the synthetic symbols are
        # ordinary non-holdout symbols there, so the dry-run reaches the packet.
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip().splitlines()[-1], "LLM_NOTE_STATUS: dry_run")
        self.assertFalse(self.out_dir.exists())
        self.assertEqual(pipeline.LLM_STATUS_PREFIX, llm_script.STATUS_PREFIX)


if __name__ == "__main__":
    unittest.main()
