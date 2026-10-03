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
import itertools
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
from fintr.llm_note import numbers, packet as packet_mod, render, runner, validate as validate_mod
from fintr.llm_note.config import ConfigError, load_config
from fintr.llm_note.forbidden import find_forbidden
from fintr.llm_note.loader import (
    LoadError, discover_price_history, find_daily_captures, find_daily_captures_until, find_period_capture,
    load_composed_capture,
)
from fintr.llm_note.packet import GuardRefusal, PacketError, build_symbol_packets
from fintr.llm_note.record import latest_accepted, latest_attempt, symbol_dir
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

    def run_note(self, mode: str = "ok", *extra: str, probe: Path | None = None, only: str | None = None,
                 log: Path | None = None, probe_answer: str | None = None):
        """llm_note.main with the fake CLI; returns (code, stdout, stderr).
        ``only``: the mode applies to that symbol's call only; ``log``: one
        line per model call."""
        env = {"FAKE_CLAUDE_MODE": mode}
        if probe is not None:
            env["FAKE_CLAUDE_PROBE"] = str(probe)
        if only is not None:
            env["FAKE_CLAUDE_ONLY_SYMBOL"] = only
        if log is not None:
            env["FAKE_CLAUDE_LOG"] = str(log)
        if probe_answer is not None:
            env["FAKE_CLAUDE_PROBE_ANSWER"] = probe_answer
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

    def outcomes(self, **overrides):
        document = json.loads(self.obs_path.read_bytes())
        kwargs = dict(
            as_of=date.fromisoformat(AS_OF), document=document,
            observations_sha256=hashlib.sha256(self.obs_path.read_bytes()).hexdigest(),
            rules=load_scanner_rules(REAL_RULES_FILE, project_root=ROOT),
            rule_texts=json.loads(REAL_RULE_TEXTS.read_text(encoding="utf-8"))["rules"],
            project_root=ROOT, guards=load_active_guards(),
            load_capture=lambda s: load_composed_capture(
                s, base_root=self.period, daily_root=self.daily, price_dir=self.price,
                as_of=date.fromisoformat(AS_OF)),
            max_symbol_packet_bytes=400_000, max_symbols=30, fallback_window_days=15,
        )
        kwargs.update(overrides)
        return build_symbol_packets(**kwargs)

    def build(self, symbol: str = SYM_A, **overrides):
        """The ``PacketBuild`` of one symbol."""
        outcome = next(o for o in self.outcomes(**overrides) if o.symbol == symbol)
        assert outcome.build is not None, outcome.error
        return outcome.build

    def sym_dir(self, symbol: str, out: Path | None = None) -> Path:
        return (out or self.out_dir) / AS_OF / symbol

    def record_of(self, symbol: str, attempt: str = "attempt-001", out: Path | None = None) -> dict:
        return json.loads((self.sym_dir(symbol, out) / attempt / "record.json").read_text(encoding="utf-8"))


# --- packet -------------------------------------------------------------------


class PacketTests(Env, unittest.TestCase):
    def test_scanner_fixture_has_h2_for_both_symbols(self):
        document = json.loads(self.obs_path.read_bytes())
        for symbol in self.symbols:
            met = [o["rule_id"] for o in document["symbols"][symbol]["scans"][0]["observations"]
                   if o["condition_met"] is True]
            self.assertEqual(met, ["H2"])

    def test_nothing_after_as_of_enters_the_packet(self):
        for symbol in self.symbols:
            build = self.build(symbol)
            text = build.packet_bytes.decode("utf-8")
            self.assertNotIn(POST_AS_OF_MARKER, text)
            dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", text))
            self.assertTrue(dates)
            self.assertEqual(sorted(d for d in dates if d > AS_OF), [])
            days = build.packet["symbols"][symbol]["days"]
            self.assertEqual(sorted(days), ["2026-01-07", "2026-01-08", "2026-01-09"])
            self.assertEqual(build.packet["symbols"][symbol]["cutoff"], AS_OF)

    def test_no_price_series_return_or_generation_time(self):
        text = self.build().packet_bytes.decode("utf-8")
        for needle in ("generated_at_utc", "adjusted_close", "price_history", "daily_capture",
                       "period_analysis", "\"close\"", "return"):
            self.assertNotIn(needle, text)

    def test_each_symbol_has_its_own_packet(self):
        outcomes = self.outcomes()
        self.assertEqual([o.symbol for o in outcomes], sorted(self.symbols))
        for outcome in outcomes:
            packet = outcome.build.packet
            self.assertEqual(list(packet["symbols"]), [outcome.symbol])
            self.assertEqual(packet["required_assessments"],
                             [{"symbol": outcome.symbol, "rule_id": "H2", "as_of": AS_OF}])
            self.assertEqual(list(packet["rules"]), ["H2"])
            other = next(s for s in self.symbols if s != outcome.symbol)
            self.assertNotIn(other, outcome.build.packet_bytes.decode("utf-8"))
            self.assertEqual(outcome.size, len(outcome.build.packet_bytes))

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

    def test_oversize_symbol_is_an_error_never_truncated(self):
        outcomes = self.outcomes(max_symbol_packet_bytes=100)
        self.assertEqual([o.symbol for o in outcomes], sorted(self.symbols))
        for outcome in outcomes:
            self.assertIsNone(outcome.build)
            self.assertEqual(outcome.error.category, "packet_too_large")
            self.assertGreater(outcome.size, 100)
        # exactly at the cap is fine
        size = len(self.build().packet_bytes)
        self.assertIsNotNone(self.outcomes(max_symbol_packet_bytes=size)[0].build)
        self.assertIsNone(self.outcomes(max_symbol_packet_bytes=size - 1)[0].build)

    def test_symbol_limit_is_reported_per_symbol(self):
        outcomes = self.outcomes(max_symbols=1)
        self.assertIsNotNone(outcomes[0].build)
        self.assertEqual(outcomes[1].symbol, sorted(self.symbols)[1])
        self.assertEqual(outcomes[1].error.category, "symbol_limit")

    def test_only_symbols_restricts_the_set(self):
        outcomes = self.outcomes(only_symbols={SYM_B})
        self.assertEqual([o.symbol for o in outcomes], [SYM_B])

    def test_holdout_and_excluded_symbols_are_refused(self):
        for bad in (HOLDOUT[0], EXCLUDED):
            document = json.loads(self.obs_path.read_bytes())
            document["symbols"][bad] = document["symbols"][SYM_A]
            with self.assertRaises(GuardRefusal):
                self.outcomes(document=document)

    def test_scan_after_as_of_is_rejected(self):
        document = json.loads(self.obs_path.read_bytes())
        document["symbols"][SYM_A]["scans"][0]["as_of"] = "2026-01-12"
        with self.assertRaises(PacketError) as ctx:
            self.outcomes(document=document)
        self.assertEqual(ctx.exception.category, "scan_after_as_of")

    def test_bad_symbol_name_is_rejected(self):
        document = json.loads(self.obs_path.read_bytes())
        document["symbols"]["../X"] = document["symbols"][SYM_A]
        with self.assertRaises(PacketError) as ctx:
            self.outcomes(document=document)
        self.assertEqual(ctx.exception.category, "bad_symbol")

    def test_wrong_schema_and_no_observations(self):
        with self.assertRaises(PacketError):
            self.outcomes(document={"schema": "x", "symbols": {}})
        document = json.loads(self.obs_path.read_bytes())
        for block in document["symbols"].values():
            for obs in block["scans"][0]["observations"]:
                obs["condition_met"] = False
        with self.assertRaises(PacketError) as ctx:
            self.outcomes(document=document)
        self.assertEqual(ctx.exception.category, "no_observations")

    def test_missing_capture_is_a_per_symbol_data_load_error(self):
        outcomes = self.outcomes(load_capture=lambda s: (_ for _ in ()).throw(ValueError("no_period_capture:" + s)))
        for outcome in outcomes:
            self.assertIsNone(outcome.build)
            self.assertEqual(outcome.error.category, "data_load_failed")

    def test_positive_symbols(self):
        document = json.loads(self.obs_path.read_bytes())
        self.assertEqual(packet_mod.positive_symbols(document), sorted(self.symbols))
        for obs in document["symbols"][SYM_A]["scans"][0]["observations"]:
            obs["condition_met"] = False
        self.assertEqual(packet_mod.positive_symbols(document), [SYM_B])


class LoaderAsOfTests(Env, unittest.TestCase):
    """A newer daily capture must not break an older ``as_of`` (the live run
    writes the next day's capture before its price file; re-runs of older
    dates)."""

    def short_price_root(self) -> Path:
        root = self.tmp / "price_short"
        for symbol in self.symbols:
            bars = [period_fixtures._price_bar(d, o="10", h="10", l="10", c="10", adj="10", v=100)
                    for d in ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09")]
            period_fixtures._write_price_capture(root, symbol, "2026-01-05", "2026-01-09", bars)
        return root

    def test_price_file_only_has_to_cover_as_of(self):
        short = self.short_price_root()
        with self.assertRaises(LoadError):  # the rolling capture ends 01-13, past the price file
            load_composed_capture(SYM_A, base_root=self.period, daily_root=self.daily, price_dir=short)
        capture = load_composed_capture(SYM_A, base_root=self.period, daily_root=self.daily, price_dir=short,
                                        as_of=date.fromisoformat(AS_OF))
        self.assertEqual(capture.symbol, SYM_A)

    def test_capture_starting_after_as_of_is_not_opened(self):
        late = self.daily / SYM_A / "2026-01-12_2026-01-13.json"
        late.write_text("not json at all", encoding="utf-8")
        as_of = date.fromisoformat(AS_OF)
        self.assertIn(late, find_daily_captures(self.daily, SYM_A))
        self.assertNotIn(late, find_daily_captures_until(self.daily, SYM_A, as_of))
        self.assertEqual(len(find_daily_captures_until(self.daily, SYM_A, as_of)), 1)
        load_composed_capture(SYM_A, base_root=self.period, daily_root=self.daily, price_dir=self.price,
                              as_of=as_of)
        with self.assertRaises(LoadError):
            load_composed_capture(SYM_A, base_root=self.period, daily_root=self.daily, price_dir=self.price)

    def test_packet_with_newer_captures_has_nothing_after_as_of(self):
        short = self.short_price_root()
        outcomes = self.outcomes(load_capture=lambda s: load_composed_capture(
            s, base_root=self.period, daily_root=self.daily, price_dir=short, as_of=date.fromisoformat(AS_OF)))
        for outcome in outcomes:
            text = outcome.build.packet_bytes.decode("utf-8")
            self.assertNotIn(POST_AS_OF_MARKER, text)
            self.assertEqual(sorted(d for d in set(re.findall(r"\d{4}-\d{2}-\d{2}", text)) if d > AS_OF), [])


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

    def test_sign_is_matched(self):
        self.assertTrue(self.ok("-40", "-40"))
        self.assertTrue(self.ok("−40", "-40"))
        self.assertTrue(self.ok("net -40 lot", "-40"))
        self.assertTrue(self.ok("(-40)", "-40"))
        self.assertTrue(self.ok("40", "40"))
        self.assertFalse(self.ok("40", "-40"))    # a positive token never matches a negative value
        self.assertFalse(self.ok("-40", "40"))    # nor the reverse
        self.assertFalse(self.ok("net satış 40", "-40"))  # strict: wording is not read
        self.assertFalse(self.ok("41", "40"))

    def test_hyphen_between_digits_or_after_a_letter_is_not_a_sign(self):
        self.assertTrue(self.ok("10-15 arası", "10", "15"))
        self.assertTrue(self.ok("ABC-5", "5"))
        self.assertEqual([t.negative for t in _tokens("a -5 b 6-7 c−8 −9")], [True, False, False, False, True])

    def test_percent_matches_value_or_small_fraction_exactly(self):
        self.assertTrue(self.ok("%5", "0.05"))
        self.assertTrue(self.ok("5%", "0.05"))
        self.assertTrue(self.ok("5 %", "5"))
        self.assertTrue(self.ok("%50", "50"))      # p matches the packet value p exactly
        self.assertTrue(self.ok("%-1,23", "-0.0123"))
        self.assertTrue(self.ok("100%", "1"))
        self.assertFalse(self.ok("%5", "0.051"))    # no rounding tolerance
        self.assertFalse(self.ok("0,05", "5"))      # plain 0.05 is not 5
        self.assertFalse(self.ok("5000%", "50"))    # p/100 only against |v| <= 1
        self.assertFalse(self.ok("101%", "1.01"))
        self.assertFalse(self.ok("%5", "-0.05"))    # the sign still has to match

    def test_unparseable_and_wrong_groupings_fail(self):
        for text in ("1.2.3", "12,34,56", "1.234,5,6"):
            tokens = _tokens(text)
            self.assertTrue(any(not t.candidates for t in tokens), text)

    def test_digits_inside_words_are_still_numbers_but_names_can_be_masked(self):
        self.assertEqual([t.raw for t in _tokens("X5 ve H2")], ["5", "2"])
        masked = numbers.mask_names("kural H2 için ABC1 kodu", ["H2", "ABC1"])
        self.assertEqual(_tokens(masked), [])

    def test_a_longer_packet_number_does_not_match_a_substring(self):
        self.assertFalse(self.ok("234567", "1234567"))
        self.assertFalse(self.ok("123", "1234567"))
        self.assertFalse(self.ok("1234567", "234567"))

    def test_dates_are_extracted_separately(self):
        dates, rest = numbers.extract_dates("2026-01-09 ve 2026-01-12 arasında 40 lot")
        self.assertEqual(dates, ["2026-01-09", "2026-01-12"])
        self.assertEqual([t.raw for t in _tokens(rest)], ["40"])

    def test_number_words(self):
        for text in ("iki kurum", "Yüz lot", "yüzde yirmi", "YÜZDE beş", "on gün", "üç milyon", "altı gün",
                     "kırk beş"):
            self.assertTrue(numbers.find_number_words(text), text)
        for text in ("bir kurum", "altında", "onlar", "yüzdelik", "binek", "net alıcı", "yedekte"):
            self.assertEqual(numbers.find_number_words(text), [], text)

    def test_rounded_quotes_match_at_the_quoted_precision(self):
        self.assertTrue(self.ok("10,46", "10.4567"))
        self.assertTrue(self.ok("10.46", "10.4567"))
        self.assertTrue(self.ok("10,5", "10.4567"))
        self.assertTrue(self.ok("10.4567", "10.4567"))
        self.assertTrue(self.ok("-10,46", "-10.4567"))
        self.assertTrue(self.ok("0,05", "0.050000000000000000000000000001"))  # a 30-digit packet value
        self.assertTrue(self.ok("1.234.567,89", "1234567.891234"))
        self.assertFalse(self.ok("10,45", "10.4567"))   # wrong rounding
        self.assertFalse(self.ok("10,4", "10.4567"))
        self.assertFalse(self.ok("10,47", "10.4567"))
        self.assertFalse(self.ok("10,46", "-10.4567"))  # the sign must match
        self.assertFalse(self.ok("-10,46", "10.4567"))
        self.assertFalse(self.ok("10", "10.4567"))      # integers stay exact: no coarse range match
        self.assertFalse(self.ok("11", "10.9"))
        self.assertTrue(self.ok("10", "10.000000000"))

    def test_rounded_percent_quotes(self):
        self.assertTrue(self.ok("5,12%", "0.051234"))   # ratio printed as a percentage, 2 decimals
        self.assertTrue(self.ok("%5,12", "0.051234"))
        self.assertTrue(self.ok("%-5,12", "-0.051234"))
        self.assertTrue(self.ok("40,5%", "40.5"))       # a percent value already in the packet
        self.assertTrue(self.ok("40,52%", "40.5249"))
        self.assertFalse(self.ok("5,13%", "0.051234"))
        self.assertFalse(self.ok("%5,12", "-0.051234"))
        self.assertFalse(self.ok("512,34%", "5.1234"))  # the x100 reading only for |v| <= 1
        self.assertFalse(self.ok("5%", "0.051234"))     # integer percent: exact only

    def test_raw_unrounded_decimals_are_flagged(self):
        flagged = lambda text: [numbers.too_many_decimals(t) for t in _tokens(text)]
        self.assertEqual(flagged("0,123456 ve 10,5 ve 3"), [True, False, False])
        self.assertEqual(flagged("1.234"), [False])  # also a thousand grouping
        self.assertEqual(flagged("0.050000000000000000000000000001"), [True])
        self.assertEqual(flagged("0,1234"), [False])

    def test_packet_number_set(self):
        packet = {"a": ["100", "-40", "10.5", None, True], "n": 7, "text": "oran 1.234,5 ve 2026-01-09 −3",
                  "2026-01-10": {"BBB": ["0.0123"]}}
        allowed, dates = numbers.packet_number_set(packet)
        for value in ("100", "-40", "10.5", "7", "1234.5", "0.0123", "-3"):
            self.assertIn(numbers.normalise(Decimal(value)), allowed, value)
        self.assertNotIn(Decimal("40"), allowed)    # signs are kept
        self.assertEqual(dates, {"2026-01-09", "2026-01-10"})
        self.assertNotIn(Decimal("2026"), allowed)  # date components are not numbers


class ForbiddenTests(unittest.TestCase):
    def test_flags_advice_forecast_and_intent_wording(self):
        for text in ("Bu hisseyi al", "sat sinyali", "Hedef fiyat", "stop seviyesi", "pozisyon aç",
                     "kesin yükselir", "kesinlikle", "kâr eder", "getiri beklentisi", "tahmin",
                     "öneri", "yatırım tavsiyesi veririz", "puan 7", "skor", "fırsat var",
                     "manipülasyon", "niyetli", "kasıtlı", "buy now", "target price"):
            self.assertTrue(find_forbidden(text), text)

    def test_flags_inflected_advice_and_forecast_wording(self):
        for text in ("almalı", "Satmalı", "satılmalı", "alınmalı", "alınabilir", "elde tutmalı",
                     "tutulmalı", "yükselecek", "DÜŞECEK", "artacak", "bekleniyor", "garanti",
                     "mutlaka", "zarar riski"):
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


PLAIN = ("Bu kural, günün son fiyatının hacim ağırlıklı ortalama fiyatın altında kalmasını arar. "
         "Veri bu koşulun sağlandığını gösteriyor. Hipotez, bu tür baskının kısmen geri döneceğini varsayar.")


def _day(symbol: str, part: str = "") -> str:
    return f"/symbols/{symbol}/days/{AS_OF}{part}"


def _good_note(build) -> dict:
    return {
        "assessments": [
            {"symbol": item["symbol"], "rule_id": item["rule_id"], "as_of": item["as_of"],
             "assessment": "destekliyor",
             "plain_explanation": PLAIN,
             "reasons": [{"text": f"{AS_OF} tarihinde AAA kodunun net_size değeri 100; takas lot 5000.",
                          "evidence_paths": [_day(item["symbol"], "/akd/AAA"),
                                             _day(item["symbol"], "/custody/AAA")]}]}
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

    def with_reason(self, text: str, *paths: str) -> dict:
        def change(note):
            reason = note["assessments"][0]["reasons"][0]
            reason["text"] = text
            if paths:
                reason["evidence_paths"] = list(paths)
        return self.mutated(change)

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
        self.check(self.mutated(lambda n: n["assessments"][0].update(symbol=SYM_B)), "coverage")

    def test_evidence_paths_must_resolve(self):
        for path in ("/symbols/NOPE/days", f"/symbols/{SYM_A}/days/2026-01-12", "symbols", "/", "",
                     f"/symbols/{SYM_A}/days/{AS_OF}/akd/AAA/9"):
            self.check(self.mutated(
                lambda n: n["assessments"][0]["reasons"][0].update(evidence_paths=[path])), "evidence_path")
        self.assertTrue(resolve_pointer(self.packet, f"/symbols/{SYM_A}/days/{AS_OF}/akd/AAA/0"))
        self.assertFalse(resolve_pointer(self.packet, f"/symbols/{SYM_A}/days/{AS_OF}/akd/AAA/01"))

    def test_an_evidence_path_may_not_cover_many_values(self):
        note = self.with_reason("AAA net 100.", _day(SYM_A))
        self.check(note)
        with patch.object(validate_mod, "MAX_EVIDENCE_LEAVES", 5):
            self.check(note, "evidence_path")

    def test_forbidden_phrase(self):
        for text in ("Hedef fiyat yukarı.", "Bu hissede al yönlü görünüm.", "Kesin destekliyor.",
                     "Getiri artar.", "Stop seviyesi 10.", "Almalı.", "Satılmalı.", "Yükselecek."):
            self.check(self.mutated(
                lambda n: n["assessments"][0]["reasons"][0].update(text=text)), "forbidden_phrase")
        self.check(self.mutated(lambda n: n["data_caveats"].append("Pozisyon bilgisi yok.")), "forbidden_phrase")

    def test_institution_title_with_a_listed_word_is_allowed(self):
        self.check(self.with_reason(f"{TITLE_B} kodu BBB net_size değeri -40.", _day(SYM_A, "/akd/BBB")))

    def test_numbers_must_exist_in_the_packet(self):
        for text in ("Toplam 987654321 lot.", "Net 101.", "Oran %6.", "Net 1.5 lot.", "77 kurum var.",
                     "Takas 5001."):
            self.check(self.with_reason(text, _day(SYM_A)), "number_not_in_input")
        self.check(self.mutated(lambda n: n["data_caveats"].append("Toplam 31 gün.")), "number_not_in_input")
        # a date that is not in the packet
        self.check(self.with_reason("2026-01-12 tarihinde net 100.", _day(SYM_A, "/akd/AAA")),
                   "number_not_in_input")

    def test_a_number_must_be_under_the_reasons_own_evidence_paths(self):
        # 5000 exists in the packet (custody) but not under the akd path alone
        self.check(self.with_reason("AAA takas lot 5000.", _day(SYM_A, "/akd/AAA")), "number_not_in_input")
        self.check(self.with_reason("AAA takas lot 5000.", _day(SYM_A, "/custody/AAA")))
        # a value that exists under another reason's path does not count
        def two_reasons(note):
            reasons = note["assessments"][0]["reasons"]
            reasons[0].update(text="AAA net 100.", evidence_paths=[_day(SYM_A, "/akd/AAA")])
            reasons.append({"text": "AAA takas lot 5000.", "evidence_paths": [_day(SYM_A, "/akd/AAA")]})
        self.check(self.mutated(two_reasons), "number_not_in_input")
        # a date must be in a cited path (or its subtree)
        self.check(self.with_reason("2026-01-08 tarihinde net 100.", _day(SYM_A, "/akd/AAA")),
                   "number_not_in_input")
        self.check(self.with_reason(f"{AS_OF} tarihinde net 100.", _day(SYM_A, "/akd/AAA")))
        # data_caveats cite no path: any packet number is fine, others are not
        self.check(self.mutated(lambda n: n["data_caveats"].append("Takas 5000 lot görüldü.")))

    def test_an_assessments_own_as_of_is_allowed_without_a_path(self):
        rule = f"/rules/{self.packet['required_assessments'][0]['rule_id']}"
        # the cited rule entry carries no date, so only the as_of exemption can admit it
        self.check(self.with_reason(f"Gözlem tarihi {AS_OF}.", rule))
        self.check(self.with_plain(f"{AS_OF} günü.", rule))
        # any other date still has to be under the reason's paths
        self.check(self.with_reason("Gözlem tarihi 2026-01-08.", rule), "number_not_in_input")
        self.check(self.with_plain("2026-01-08 günü.", rule), "number_not_in_input")
        # numbers are not loosened
        self.check(self.with_reason(f"Gözlem tarihi {AS_OF}, net 100.", rule), "number_not_in_input")
        # data_caveats keep the whole-packet check (this date is not in the packet)
        self.check(self.mutated(lambda n: n["data_caveats"].append("2026-01-12 eksik.")), "number_not_in_input")

    def test_another_assessments_as_of_does_not_leak(self):
        import copy
        other = "2026-01-08"
        packet = copy.deepcopy(self.packet)
        # the fixture packet has one required assessment: add a second one (same symbol and rule, other as_of)
        packet["required_assessments"].append({**packet["required_assessments"][0], "as_of": other})
        rule = f"/rules/{packet['required_assessments'][0]['rule_id']}"
        note = _good_note(self.built)
        note["assessments"].append(copy.deepcopy(note["assessments"][0]))
        note["assessments"][1]["as_of"] = other
        for item in note["assessments"]:
            item["reasons"][0].update(text="Kayıt satırı incelendi.", evidence_paths=[rule])
        self.assertTrue(validate_note(note, packet).ok)
        note["assessments"][0]["reasons"][0]["text"] = f"Tarih {other}."
        self.assertEqual(validate_note(note, packet).category, "number_not_in_input")
        note["assessments"][0]["reasons"][0]["text"] = "Kayıt satırı incelendi."
        note["assessments"][1]["reasons"][0]["text"] = f"Tarih {other}."
        self.assertTrue(validate_note(note, packet).ok)
        note["assessments"][1]["reasons"][0]["text"] = f"Tarih {AS_OF}."
        self.assertEqual(validate_note(note, packet).category, "number_not_in_input")

    def test_sign_must_match_the_packet(self):
        self.check(self.with_reason("BBB net 40.", _day(SYM_A, "/akd/BBB")), "number_not_in_input")
        self.check(self.with_reason("BBB net satış 40.", _day(SYM_A, "/akd/BBB")), "number_not_in_input")
        self.check(self.with_reason("BBB net -40.", _day(SYM_A, "/akd/BBB")))
        self.check(self.with_reason("AAA net -100.", _day(SYM_A, "/akd/AAA")), "number_not_in_input")

    def test_percent_matches_exactly_or_as_a_small_fraction(self):
        meta = f"/symbols/{SYM_A}/metadata_by_day/{AS_OF}"
        self.check(self.with_reason("Perakende oranı %40,5.", meta))
        self.check(self.with_reason("Perakende oranı %4050.", meta), "number_not_in_input")
        self.check(self.with_reason("Perakende oranı %0,405.", meta), "number_not_in_input")

    def test_spelled_out_numbers_are_rejected(self):
        for text in ("AAA iki kurumdan biri.", "yüzde elli oranında.", "Yüz lot civarı.", "on gün boyunca."):
            self.check(self.with_reason(text, _day(SYM_A, "/akd/AAA")), "number_word")
        self.check(self.mutated(lambda n: n["data_caveats"].append("Üç gün eksik.")), "number_word")
        self.check(self.with_reason("Bir kurum AAA net 100.", _day(SYM_A, "/akd/AAA")))  # "bir" is allowed

    def with_plain(self, text: str, *paths: str) -> dict:
        def change(note):
            note["assessments"][0]["plain_explanation"] = text
            if paths:  # a number-free reason, so only the plain text is under test
                note["assessments"][0]["reasons"][0]["text"] = "Kayıt satırı incelendi."
                note["assessments"][0]["reasons"][0]["evidence_paths"] = list(paths)
        return self.mutated(change)

    def test_plain_explanation_is_required_and_bounded(self):
        self.check(self.mutated(lambda n: n["assessments"][0].pop("plain_explanation")), "schema")
        self.check(self.with_plain(""), "schema")
        self.check(self.mutated(lambda n: n["assessments"][0].update(plain_explanation=5)), "schema")
        self.check(self.with_plain("Kısa anlatım. " * 60), "schema")  # more than MAX_PLAIN_CHARS

    def test_plain_explanation_gets_the_same_checks(self):
        self.check(self.with_plain("Hedef fiyat yukarı gider."), "forbidden_phrase")
        self.check(self.with_plain("Kurumlar niyetle alım yaptı."), "forbidden_phrase")
        self.check(self.with_plain("Getiri artacak."), "forbidden_phrase")
        self.check(self.with_plain("Yüzde elli oranında."), "number_word")
        self.check(self.with_plain("Toplam 987654321 lot."), "number_not_in_input")
        self.check(self.with_plain("2026-01-12 günü."), "number_not_in_input")
        self.check(self.with_plain("Değer 0,123456789."), "number_format")

    def test_plain_explanation_numbers_come_from_the_reasons_paths_or_the_rule_entry(self):
        # 5000 is under the custody path the reason cites; 100 under the akd path
        self.check(self.with_plain("AAA net 100 ve takas 5000 lot."))
        # 5000 is not under the akd-only citation
        self.check(self.with_plain("Takas 5000 lot.", _day(SYM_A, "/akd/AAA")), "number_not_in_input")
        # the rule's own condition threshold (%1 = 0.01 in the condition) is allowed
        self.check(self.with_plain("Kapanış ortalamanın en az %1 altında olmalı.", _day(SYM_A, "/akd/AAA")))
        # a packet number the assessment did not cite is not
        self.check(self.with_plain("Yatırımcı sayısı 1234.", _day(SYM_A, "/akd/AAA")), "number_not_in_input")

    def test_rounded_quotes_are_accepted_in_reasons_and_plain_text(self):
        feature = self.feature_path("vwap_proxy_vs_close")
        self.check(self.with_reason("Fark %5,00.", feature))
        self.check(self.with_reason("Fark %5,01.", feature), "number_not_in_input")
        self.check(self.with_plain("Fark yaklaşık %5,0.", feature))
        meta = f"/symbols/{SYM_A}/metadata_by_day/{AS_OF}"
        self.check(self.with_reason("Perakende oranı %40,50.", meta))
        self.check(self.with_reason("Perakende oranı %40,51.", meta), "number_not_in_input")
        self.check(self.with_reason("Oran %0,123456.", meta), "number_format")

    def test_rule_entry_carries_the_prereg_hypothesis_and_rationale(self):
        rule = self.build().packet["rules"]["H2"]
        doc = (ROOT / "docs" / "prereg" / "H2.md").read_bytes()
        self.assertIn("H1 (beklenen)", rule["hypothesis_text"])
        self.assertIn("H1 ile aynı mekanizma", rule["rationale_text"])
        self.assertNotIn("## ", rule["hypothesis_text"] + rule["rationale_text"])
        self.assertNotIn("Koşul (motor dili", rule["rationale_text"])  # stops at the next section
        self.assertEqual(rule["prereg_doc_sha256"], hashlib.sha256(doc).hexdigest())
        self.assertEqual(sorted(set(re.findall(r"\d{4}-\d{2}-\d{2}", json.dumps(rule)))), [])

    def test_missing_prereg_document_gives_none_not_an_invented_text(self):
        entry = packet_mod._prereg_text(self.tmp, "H2")
        self.assertEqual(entry, {"hypothesis_text": None, "rationale_text": None, "prereg_doc_sha256": None})

    def test_numbers_in_other_formats_match(self):
        feature = self.feature_path("vwap_proxy_vs_close")
        meta = f"/symbols/{SYM_A}/metadata_by_day/{AS_OF}"
        for text, paths in (
            ("Takas lot 5.000 ve net 100,0.", (_day(SYM_A, "/akd/AAA"), _day(SYM_A, "/custody/AAA"))),
            ("Metadata oranı %40,5; yatırımcı sayısı 1.234.", (meta,)),
            ("Net −40, VWAP 10,5.", (_day(SYM_A, "/akd/BBB"),)),
            ("Fark %5 (vwap_proxy_vs_close 0.05).", (feature,)),
            ("H2 ve H1 kuralları.", (_day(SYM_A, "/akd/AAA"),)),
        ):
            result = validate_note(self.with_reason(text, *paths), self.packet)
            self.assertTrue(result.ok, (text, result))

    def feature_path(self, name: str) -> str:
        scans = self.packet["symbols"][SYM_A]["scans"]
        for s_index, scan in enumerate(scans):
            for o_index, obs in enumerate(scan["observations"]):
                for f_index, feature in enumerate(obs["features"]):
                    if feature["name"] == name:
                        return f"/symbols/{SYM_A}/scans/{s_index}/observations/{o_index}/features/{f_index}"
        raise AssertionError(name)

    def test_rule_and_symbol_ids_are_not_numbers(self):
        self.check(self.with_reason("H2 kuralı için AAA net 100.", _day(SYM_A, "/akd/AAA")))

    def test_sensitive_content(self):
        self.check(self.with_reason("Kaynak https://example.invalid/x net 100.", _day(SYM_A, "/akd/AAA")),
                   "sensitive_content")


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

    def test_usage_and_duration_fields_are_extracted_numerically(self):
        outcome = self.run_fake("ok")
        self.assertEqual(outcome.usage, {
            "duration_ms": 1234, "num_turns": 1, "total_cost_usd": 0.5,
            "usage": {"input_tokens": 11, "output_tokens": 22},  # non-numeric service_tier is not copied
        })
        self.assertEqual(self.run_fake("refusal").usage, {})

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

    def test_classify_probe_answer(self):
        canaries = ["Efe", "AvenoxBeyin", "İkinci Beyin", "Beynim"]
        for text, result in (("YOK", "clean"), ("yok.", "clean"), (" Yok\n", "clean"), ("None", "clean"),
                             ("Efe", "not_isolated"), ("Kullanıcı: efe", "not_isolated"),
                             ("EFE'nin notları", "not_isolated"), ("avenoxbeyin", "not_isolated"),
                             ("ikinci beyin klasörü", "not_isolated"), ("İKİNCİ BEYİN", "not_isolated"),
                             ("YOK ama Beynim", "not_isolated"),
                             ("", "unclear"), ("bilmiyorum", "unclear"), ("evet", "unclear"),
                             ("hayır", "unclear"), ("Efendi", "unclear"), ("yok yok yok", "unclear")):
            self.assertEqual(runner.classify_probe_answer(text, canaries), result, text)

    def test_probe_stdin_and_prompt_contain_no_canary(self):
        config = load_config(REAL_CONFIG, project_root=ROOT)
        hidden = runner._fold(runner.PROBE_STDIN + config.probe_prompt_path.read_text(encoding="utf-8"))
        for canary in config.probe_canaries:
            self.assertNotIn(runner._fold(canary), hidden, canary)
        self.assertEqual(config.probe_prompt_path.name, "probe_prompt_v2.md")
        self.assertTrue(config.probe_canaries_sha256)

    def test_build_command_flags_and_record_command(self):
        command = runner.build_command("claude", model="claude-opus-5-5", effort="high",
                                       prompt_path=Path("p.md"), schema_text='{"a":1}')
        self.assertEqual(command[:2], ["claude", "-p"])
        for flag, value in (("--model", "claude-opus-5-5"), ("--effort", "high"), ("--tools", ""),
                            ("--output-format", "json"), ("--json-schema", '{"a":1}'),
                            ("--system-prompt-file", "p.md")):
            self.assertEqual(command[command.index(flag) + 1], value, flag)
        self.assertIn("--no-session-persistence", command)
        # isolation: no MCP beyond an explicit config, no skills, only the local settings source
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--disable-slash-commands", command)
        self.assertEqual(command[command.index("--setting-sources") + 1], "local")
        self.assertIn("--exclude-dynamic-system-prompt-sections", command)
        self.assertNotIn("--bare", command)  # --bare never reads the subscription (OAuth) login
        for flag in ("--mcp-config", "--settings", "--plugin-dir", "--add-dir", "--dangerously-skip-permissions",
                     "--api-key"):
            self.assertNotIn(flag, command)
        custom = runner.build_command("claude", model="m", effort="e", prompt_path=Path("p.md"),
                                      schema_text=None, setting_sources="project")
        self.assertEqual(custom[custom.index("--setting-sources") + 1], "project")
        self.assertNotIn("--json-schema", custom)
        shown = runner.record_command(command, "abc", "configs/p.md")
        self.assertEqual(shown[0], "<claude-cli>")
        self.assertNotIn('{"a":1}', shown)
        self.assertIn("<output_schema sha256=abc>", shown)
        self.assertIn("configs/p.md", shown)

    def test_resolve_cli_and_child_env(self):
        cfg = {"windows": "%USERPROFILE%\\.local\\bin\\claude.exe", "default": "claude"}
        self.assertEqual(runner.resolve_cli(cfg, "X"), "X")
        self.assertIn("claude", runner.resolve_cli(cfg, None))
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "KEEP": "1", "EXTRA": "x", "CLAUDECODE": "1",
                                     "CLAUDE_CODE_ENTRYPOINT": "cli", "claude_config_dir": "c",
                                     "ANTHROPIC_BASE_URL": "u", "CLAUDIA": "keep"}):
            env = runner.child_env(["ANTHROPIC_API_KEY", "EXTRA"])
        for gone in ("ANTHROPIC_API_KEY", "EXTRA", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "claude_config_dir",
                     "ANTHROPIC_BASE_URL"):
            self.assertNotIn(gone, env)
        self.assertEqual(env["KEEP"], "1")
        self.assertEqual(env["CLAUDIA"], "keep")  # only the CLAUDE*/ANTHROPIC* prefixes


class ConfigTests(unittest.TestCase):
    def test_real_config_loads_and_hashes_files(self):
        config = load_config(REAL_CONFIG, project_root=ROOT)
        self.assertEqual((config.model, config.effort), ("claude-opus-5-5", "high"))
        self.assertEqual(config.prompt_sha256, hashlib.sha256(config.prompt_path.read_bytes()).hexdigest())
        self.assertEqual(config.schema_sha256, hashlib.sha256(config.schema_path.read_bytes()).hexdigest())
        self.assertEqual(json.loads(config.schema_text)["title"], "fintr_llm_note_v1")
        self.assertIn("{", config.schema_text)
        self.assertNotIn("\n", config.schema_text)
        self.assertEqual((config.max_symbol_packet_bytes, config.max_symbols_per_run, config.max_run_seconds),
                         (200_000, 30, 5400.0))
        self.assertEqual((config.setting_sources, config.isolation_probe), ("local", False))
        self.assertEqual(config.probe_prompt_sha256,
                         hashlib.sha256(config.probe_prompt_path.read_bytes()).hexdigest())
        self.assertEqual(config.strip_env, ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])

    def test_bad_configs_are_rejected(self):
        document = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            def write(doc) -> Path:
                path = Path(directory) / "c.json"
                path.write_text(json.dumps(doc), encoding="utf-8")
                return path
            for change in ({"schema": "x"}, {"model": ""}, {"timeout_seconds": 0}, {"max_symbol_packet_bytes": 1.5},
                           {"max_symbols_per_run": 0}, {"max_run_seconds": 0}, {"max_packet_bytes": None, "max_symbol_packet_bytes": 0},
                           {"setting_sources": ""}, {"isolation_probe": "yes"}, {"probe_prompt_file": "nope.md"},
                           {"probe_canaries": []}, {"probe_canaries": "Efe"}, {"probe_canaries": ["Efe", ""]},
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

    def test_prompt_v2_is_configured_and_states_the_limits(self):
        config = load_config(REAL_CONFIG, project_root=ROOT)
        self.assertEqual(config.prompt_path.name, "prompt_v2.md")
        prompt = config.prompt_path.read_text(encoding="utf-8")
        self.assertIn(f"1 ile {validate_mod.MAX_REASONS} arasında gerekçe (`reasons`)", prompt)
        self.assertIn(f"1 ile {validate_mod.MAX_PATHS_PER_REASON} arasında `evidence_paths`", prompt)
        self.assertIn("en az 1 gerekçe", prompt)
        for needle in ('"iki kurum"', '"üç gün"', '"beş satır"', "kendi `as_of` tarihini"):
            self.assertIn(needle, prompt)
        # v1 stays unchanged for provenance and v2 keeps every v1 rule
        self.assertTrue((ROOT / "configs" / "llm_note" / "prompt_v1.md").is_file())
        self.assertIn("Şu sözcükleri kullanma: al, sat, hedef, stop, pozisyon", prompt)

    def test_suggested_qualitative_phrases_are_not_number_words(self):
        for phrase in ("birden fazla kurum", "bazı kurumlar", "birkaç gün"):
            self.assertEqual(numbers.find_number_words(phrase), [], phrase)
            self.assertEqual(validate_mod.find_forbidden(phrase), [], phrase)
        for phrase in ("iki kurum", "üç gün", "beş satır"):
            self.assertTrue(numbers.find_number_words(phrase), phrase)

    def test_prompt_states_the_hard_rules(self):
        prompt = (ROOT / "configs" / "llm_note" / "prompt_v2.md").read_text(encoding="utf-8")
        for needle in ("destekliyor", "zayıflatıyor", "belirsiz", "bilinmiyor", "JSON Pointer",
                       "Hesap yapma", "manipülasyon", "pozisyon", "hedef", "stop", "yazıyla sayı",
                       "yollarının altındaki", "Eksi işaretli", "plain_explanation", "hypothesis_text",
                       "rationale_text", "en çok 2 ondalığa"):
            self.assertIn(needle, prompt)

class PromptLayoutTests(Env, unittest.TestCase):
    def test_prompt_paths_match_the_real_packet_layout(self):
        prompt = (ROOT / "configs" / "llm_note" / "prompt_v2.md").read_text(encoding="utf-8")
        self.assertNotIn("symbols.<SYM>.observations", prompt)  # observations live under scans[]
        for key in ("scans[].observations[]", "scans[].freshness", "scans[].data_status", "metadata_by_day",
                    "composition", "institutions", "days.<TARİH>.akd", "days.<TARİH>.custody"):
            self.assertIn(key, prompt)
        # ... and the keys the prompt names exist in a real packet
        block = json.loads(self.build().packet_bytes)["symbols"][SYM_A]
        self.assertEqual(sorted(block), ["composition", "cutoff", "days", "institutions", "metadata_by_day", "scans"])
        self.assertEqual(sorted(block["scans"][0]), ["as_of", "data_status", "freshness", "observations"])
        feature = block["scans"][0]["observations"][0]["features"][0]
        self.assertTrue({"name", "value", "data_families", "min_date", "max_date"} <= set(feature))
        self.assertEqual(sorted(block["metadata_by_day"][AS_OF]),
                         ["institutional_pct", "investor_count", "null_fields", "retail_pct"])


# --- script (llm_note.py) ---------------------------------------------------------------


class ScriptTests(Env, unittest.TestCase):
    def test_each_symbol_is_noted_recorded_without_secrets_and_with_lineage(self):
        probe, log = self.tmp / "probe.json", self.tmp / "calls.log"
        code, out, err = self.run_note("ok", probe=probe, log=log)
        self.assertEqual((code, self.status(out)), (0, "ok=2,rejected=0,failed=0"), err)
        self.assertEqual(sorted(p.name for p in (self.out_dir / AS_OF).iterdir()), sorted(self.symbols))
        self.assertEqual(log.read_text(encoding="utf-8").split("\n")[:-1],
                         [f"{s} ok" for s in sorted(self.symbols)])  # one call per symbol
        config = load_config(REAL_CONFIG, project_root=ROOT)
        for symbol in self.symbols:
            attempt = self.sym_dir(symbol) / "attempt-001"
            self.assertEqual(sorted(p.name for p in attempt.iterdir()),
                             ["note.json", "packet.json", "record.json", "response_raw.txt"])
            record = self.record_of(symbol)
            packet_bytes = (attempt / "packet.json").read_bytes()
            self.assertEqual((record["status"], record["category"], record["symbol"]), ("accepted", None, symbol))
            self.assertEqual(record["inputs"]["packet_sha256"], hashlib.sha256(packet_bytes).hexdigest())
            self.assertEqual(record["inputs"]["packet_bytes"], len(packet_bytes))
            self.assertEqual(record["inputs"]["observations_sha256"],
                             hashlib.sha256(self.obs_path.read_bytes()).hexdigest())
            self.assertEqual(record["prompt"]["sha256"], config.prompt_sha256)
            self.assertEqual(record["output_schema"]["sha256"], config.schema_sha256)
            self.assertEqual(record["config"]["sha256"], config.sha256)
            self.assertEqual(record["model_requested"], "claude-opus-5-5")
            self.assertEqual(record["model_reported"], ["claude-opus-5-5"])
            self.assertEqual(record["cli"]["version"], "fake-cli 0.0.1")
            self.assertIn("determin", record["determinism_note"].lower())
            self.assertEqual(record["validation"]["ok"], True)
            self.assertTrue(record["called"])
            self.assertGreaterEqual(record["duration_seconds"], 0)
            self.assertEqual(record["usage"]["usage"], {"input_tokens": 11, "output_tokens": 22})
            self.assertEqual(record["usage"]["duration_ms"], 1234)
            command = record["command"]
            self.assertEqual(command[0], "<claude-cli>")
            for flag in ("--model", "--effort", "--tools", "--output-format", "--json-schema",
                         "--no-session-persistence", "--system-prompt-file", "--strict-mcp-config",
                         "--disable-slash-commands", "--setting-sources"):
                self.assertIn(flag, command)
            self.assertEqual(command[command.index("--system-prompt-file") + 1], "configs/llm_note/prompt_v2.md")
            self.assertNotIn(str(ROOT), json.dumps(record["command"][2:]))
            self.assertTrue(json.loads((attempt / "note.json").read_text(encoding="utf-8"))["assessments"])
            # the packet file is the very packet validated and sent, and holds only this symbol
            packet = json.loads(packet_bytes)
            self.assertEqual(packet["required_assessments"], [{"symbol": symbol, "rule_id": "H2", "as_of": AS_OF}])
            self.assertEqual(list(packet["symbols"]), [symbol])
            from fintr.evaluation.live_capture import validate_context_contains_no_secrets
            for name in ("record.json", "note.json"):
                validate_context_contains_no_secrets(json.loads((attempt / name).read_text(encoding="utf-8")))
        # the child saw: no API key variables, an empty temp cwd, the flags
        seen = json.loads(probe.read_text(encoding="utf-8"))
        self.assertFalse(seen["has_api_key"] or seen["has_auth_token"])
        self.assertNotEqual(Path(seen["cwd"]).resolve(), ROOT.resolve())
        self.assertIn("--no-session-persistence", seen["argv"])
        self.assertEqual(seen["argv"][seen["argv"].index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(seen["argv"][seen["argv"].index("--tools") + 1], "")

    def test_api_key_variables_never_reach_the_child(self):
        probe = self.tmp / "probe.json"
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_AUTH_TOKEN": "t"}):
            self.run_note("ok", probe=probe)
        seen = json.loads(probe.read_text(encoding="utf-8"))
        self.assertFalse(seen["has_api_key"])
        self.assertFalse(seen["has_auth_token"])

    def test_accepted_notes_are_not_regenerated_and_never_overwritten(self):
        self.run_note("ok")
        before = {p: p.read_bytes() for p in (self.out_dir / AS_OF).rglob("*") if p.is_file()}
        log = self.tmp / "calls.log"
        code, out, _ = self.run_note("refusal", log=log)  # would fail if the model were called
        self.assertEqual((code, self.status(out)), (0, "already_noted"))
        self.assertFalse(log.exists())
        after = {p: p.read_bytes() for p in (self.out_dir / AS_OF).rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_a_rerun_retries_only_the_symbols_without_an_accepted_note(self):
        code, out, err = self.run_note("bad_number", only=SYM_B)
        self.assertEqual((code, self.status(out)), (3, "ok=1,rejected=1,failed=0"), err)
        self.assertIn(f"Analiz notu {SYM_B}: analiz notu doğrulamadan geçemedi (number_not_in_input)", err)
        self.assertIsNotNone(latest_accepted(self.sym_dir(SYM_A)))
        self.assertIsNone(latest_accepted(self.sym_dir(SYM_B)))
        first_b = (self.sym_dir(SYM_B) / "attempt-001" / "record.json").read_bytes()
        log = self.tmp / "calls.log"
        code, out, _ = self.run_note("ok", log=log)
        self.assertEqual((code, self.status(out)), (0, "ok=1,rejected=0,failed=0,already=1"))
        self.assertEqual(log.read_text(encoding="utf-8").split("\n")[:-1], [f"{SYM_B} ok"])
        self.assertEqual(sorted(p.name for p in self.sym_dir(SYM_A).iterdir()), ["attempt-001"])
        self.assertEqual(sorted(p.name for p in self.sym_dir(SYM_B).iterdir()), ["attempt-001", "attempt-002"])
        self.assertEqual((self.sym_dir(SYM_B) / "attempt-001" / "record.json").read_bytes(), first_b)
        self.assertEqual(latest_accepted(self.sym_dir(SYM_B))[0].name, "attempt-002")

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
                               ("bad_plain", "number_not_in_input"), ("raw_decimals", "number_format"),
                               ("missing", "coverage"), ("secret", "sensitive_content"),
                               ("result_garbage", "schema")):
            with self.subTest(mode=mode):
                out_dir = self.tmp / f"notes-{mode}"
                self.out_dir = out_dir
                code, out, err = self.run_note(mode)
                self.assertEqual(code, 3, (mode, err))
                self.assertEqual(self.status(out), "ok=0,rejected=2,failed=0")
                for symbol in self.symbols:
                    self.assertIn(f"Analiz notu {symbol}: analiz notu doğrulamadan geçemedi ({category})", err)
                    attempt = self.sym_dir(symbol, out_dir) / "attempt-001"
                    record = self.record_of(symbol, out=out_dir)
                    self.assertEqual((record["status"], record["category"]), ("rejected", category))
                    self.assertFalse(record["validation"]["ok"])
                    self.assertFalse((attempt / "note.json").exists())
                    self.assertIsNone(latest_accepted(self.sym_dir(symbol, out_dir)))

    def test_unavailable_categories_and_flow_continues(self):
        for mode, category in (("refusal", "refusal"), ("crash", "cli_error"), ("nonjson", "cli_error")):
            with self.subTest(mode=mode):
                self.out_dir = self.tmp / f"notes-{mode}"
                log = self.tmp / f"calls-{mode}.log"
                code, out, err = self.run_note(mode, log=log)
                self.assertEqual((code, self.status(out)), (3, "ok=0,rejected=0,failed=2"))
                self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 2)  # each symbol was tried
                for symbol in self.symbols:
                    self.assertIn(f"Analiz notu {symbol}: analiz notu alınamadı ({category})", err)
                    record = self.record_of(symbol)
                    self.assertEqual((record["status"], record["category"]), ("unavailable", category))
                    self.assertTrue(record["called"])
                    self.assertIsNone(record["validation"])

    def test_one_failing_symbol_does_not_stop_the_others(self):
        code, out, err = self.run_note("refusal", only=SYM_A)
        self.assertEqual((code, self.status(out)), (3, "ok=1,rejected=0,failed=1"))
        self.assertEqual(self.record_of(SYM_A)["category"], "refusal")
        self.assertEqual(self.record_of(SYM_B)["status"], "accepted")

    def test_usage_limit_stops_further_calls_but_every_symbol_is_recorded(self):
        log = self.tmp / "calls.log"
        code, out, err = self.run_note("limit", log=log)
        self.assertEqual((code, self.status(out)), (3, "ok=0,rejected=0,failed=2"))
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)  # the second was not tried
        first, second = sorted(self.symbols)
        self.assertTrue(self.record_of(first)["called"])
        record = self.record_of(second)
        self.assertEqual((record["status"], record["category"], record["called"]), ("unavailable", "usage_limit", False))
        self.assertIn(f"Analiz notu {second}: analiz notu alınamadı (usage_limit)", err)

    def test_probe_isolation_records_the_answer_and_sends_no_data(self):
        probe, log = self.tmp / "probe.json", self.tmp / "calls.log"
        code, out, err = self.run_note("ok", "--probe-isolation", probe=probe, log=log, probe_answer="YOK.")
        self.assertEqual((code, self.status(out)), (0, "probe:clean"), err)
        self.assertEqual(log.read_text(encoding="utf-8").split("\n")[:-1], ["PROBE"])  # one call, no packet
        seen = json.loads(probe.read_text(encoding="utf-8"))
        self.assertNotIn("--json-schema", seen["argv"])
        self.assertEqual(seen["argv"][seen["argv"].index("--setting-sources") + 1], "local")
        self.assertIn("--strict-mcp-config", seen["argv"])
        self.assertIn("--disable-slash-commands", seen["argv"])
        self.assertFalse(seen["has_api_key"] or seen["has_auth_token"])
        self.assertNotEqual(Path(seen["cwd"]).resolve(), ROOT.resolve())
        self.assertTrue((self.out_dir / "_probes" / "attempt-001" / "record.json").exists())
        record = json.loads((self.out_dir / "_probes" / "attempt-001" / "record.json").read_text(encoding="utf-8"))
        self.assertEqual((record["result"], record["answer_text"]), ("clean", "YOK."))
        self.assertEqual(record["setting_sources"], "local")
        self.assertEqual(record["classification"], "clean")
        self.assertEqual(record["canaries"]["sha256"],
                         load_config(REAL_CONFIG, project_root=ROOT).probe_canaries_sha256)
        self.assertEqual(record["cli"]["version"], "fake-cli 0.0.1")
        self.assertEqual(record["question"], runner.PROBE_STDIN)
        self.assertNotIn("--json-schema", record["command"])
        self.assertFalse((self.out_dir / AS_OF).exists())  # no note records, no data read
        # a second probe never overwrites the first
        self.run_note("ok", "--probe-isolation", probe_answer="YOK")
        self.assertTrue((self.out_dir / "_probes" / "attempt-002" / "record.json").exists())

    def test_probe_isolation_answers_other_than_no_fail(self):
        for answer, result in (("Efe", "not_isolated"), ("belki", "unclear"), ("", "unclear")):
            with self.subTest(answer=answer):
                code, out, _ = self.run_note("ok", "--probe-isolation", probe_answer=answer)
                self.assertEqual((code, self.status(out)), (3, f"probe:{result}"))
        code, out, _ = self.run_note("refusal", "--probe-isolation")
        self.assertEqual((code, self.status(out)), (3, "probe:unavailable:refusal"))

    def test_probe_isolation_needs_no_as_of_or_observations(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "ok"}), \
                patch.object(runner, "build_command", lambda cli, **kw: [sys.executable, str(FAKE_CLI)]), \
                patch.object(runner, "cli_version", lambda *a, **k: "fake"), \
                redirect_stdout(out), redirect_stderr(err):
            code = llm_script.main(["--probe-isolation", "--out-dir", str(self.out_dir)])
        self.assertEqual((code, self.status(out.getvalue())), (0, "probe:clean"))
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            llm_script.main(["--out-dir", str(self.out_dir)])  # --as-of is required otherwise

    def test_isolation_probe_in_the_config_is_fail_closed(self):
        document = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
        document["isolation_probe"] = True
        probing = self.tmp / "probing.json"
        probing.write_text(json.dumps(document), encoding="utf-8")
        log = self.tmp / "calls.log"
        code, out, err = self.run_note("ok", "--config", str(probing), log=log, probe_answer="Efe")
        self.assertEqual((code, self.status(out)), (1, "error:isolation_probe:not_isolated"))
        self.assertEqual(log.read_text(encoding="utf-8").split("\n")[:-1], ["PROBE"])  # no packet was sent
        self.assertFalse((self.out_dir / AS_OF).exists())
        log.unlink()
        code, out, _ = self.run_note("ok", "--config", str(probing), log=log, probe_answer="YOK")
        self.assertEqual((code, self.status(out)), (0, "ok=2,rejected=0,failed=0"))
        self.assertEqual(log.read_text(encoding="utf-8").split("\n")[:-1],
                         ["PROBE"] + [f"{s} ok" for s in sorted(self.symbols)])

    def test_the_probe_is_off_by_default(self):
        log = self.tmp / "calls.log"
        self.run_note("ok", log=log)
        self.assertNotIn("PROBE", log.read_text(encoding="utf-8"))

    def test_timeout(self):
        code, out, err = self.run_note("timeout", "--timeout-seconds", "1")
        self.assertEqual((code, self.status(out)), (3, "ok=0,rejected=0,failed=2"))
        self.assertEqual(self.record_of(sorted(self.symbols)[0])["category"], "timeout")

    def test_missing_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(runner, "cli_version", lambda *a, **k: "unknown"), \
                redirect_stdout(out), redirect_stderr(err):
            code = llm_script.main(self.argv("--claude-path", str(self.tmp / "no-claude")))
        self.assertEqual((code, self.status(out.getvalue())), (3, "ok=0,rejected=0,failed=2"))
        for symbol in self.symbols:
            self.assertEqual(self.record_of(symbol)["category"], "cli_not_found")

    def test_run_time_budget_stops_calls_but_records_the_rest(self):
        document = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
        document["max_run_seconds"] = 1
        small = self.tmp / "budget.json"
        small.write_text(json.dumps(document), encoding="utf-8")
        log = self.tmp / "calls.log"
        ticks = itertools.count(0, 1000)
        with patch.object(llm_script.time, "monotonic", side_effect=lambda: next(ticks)):
            code, out, _ = self.run_note("ok", "--config", str(small), log=log)
        self.assertEqual((code, self.status(out)), (3, "ok=0,rejected=0,failed=2"))
        self.assertFalse(log.exists())
        for symbol in self.symbols:
            record = self.record_of(symbol)
            self.assertEqual((record["category"], record["called"]), ("run_time_budget", False))

    def test_dry_run_builds_the_packets_but_calls_and_writes_nothing(self):
        code, out, err = self.run_note("refusal", "--dry-run")
        self.assertEqual((code, self.status(out)), (0, "dry_run"))
        self.assertFalse(self.out_dir.exists())
        self.assertIn("girdi paketi", err)
        for symbol in self.symbols:
            self.assertIn(f"Analiz notu girdi paketi {symbol}:", err)
        self.assertIn("en büyük", err)
        self.assertIn("ortanca", err)

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

    def test_bad_symbol_name_is_an_error_before_any_call(self):
        document = json.loads(self.obs_path.read_bytes())
        document["symbols"]["../X"] = document["symbols"][SYM_A]
        self.obs_path.write_text(json.dumps(document), encoding="utf-8")
        code, out, _ = self.run_note("ok")
        self.assertEqual((code, self.status(out)), (1, "error:bad_symbol"))
        self.assertFalse(self.out_dir.exists())

    def test_input_errors(self):
        code, out, _ = self.run_note("ok", "--scanner-dir", str(self.tmp / "nope"))
        self.assertEqual((code, self.status(out)), (1, "error:observations_unreadable"))
        self.obs_path.write_text(json.dumps({"schema": "x", "symbols": {}}), encoding="utf-8")
        code, out, _ = self.run_note("ok")
        self.assertEqual((code, self.status(out)), (1, "error:observations_schema"))

    def test_missing_capture_is_a_per_symbol_failure_without_a_call(self):
        log = self.tmp / "calls.log"
        code, out, err = self.run_note("ok", "--base-root", str(self.tmp / "empty"), log=log)
        self.assertEqual((code, self.status(out)), (3, "ok=0,rejected=0,failed=2"))
        self.assertFalse(log.exists())
        for symbol in self.symbols:
            record = self.record_of(symbol)
            self.assertEqual((record["status"], record["category"], record["called"]),
                             ("unavailable", "data_load_failed", False))
            self.assertIn(f"Analiz notu {symbol}: analiz notu alınamadı (data_load_failed)", err)

    def test_packet_too_large_is_that_symbols_own_failure_without_a_call(self):
        document = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
        document["max_symbol_packet_bytes"] = 100
        small = self.tmp / "small.json"
        small.write_text(json.dumps(document), encoding="utf-8")
        log = self.tmp / "calls.log"
        code, out, _ = self.run_note("refusal", "--config", str(small), log=log)
        self.assertEqual((code, self.status(out)), (3, "ok=0,rejected=0,failed=2"))
        self.assertFalse(log.exists())
        for symbol in self.symbols:
            record = self.record_of(symbol)
            self.assertEqual((record["status"], record["category"]), ("unavailable", "packet_too_large"))
            self.assertGreater(record["inputs"]["packet_bytes"], 100)
            self.assertFalse(record["called"])
            self.assertFalse((self.sym_dir(symbol) / "attempt-001" / "packet.json").exists())

    def test_symbol_limit_is_reported_not_silent(self):
        document = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
        document["max_symbols_per_run"] = 1
        small = self.tmp / "limit.json"
        small.write_text(json.dumps(document), encoding="utf-8")
        log = self.tmp / "calls.log"
        code, out, err = self.run_note("ok", "--config", str(small), log=log)
        first, second = sorted(self.symbols)
        self.assertEqual((code, self.status(out)), (3, "ok=1,rejected=0,failed=1"))
        self.assertEqual(log.read_text(encoding="utf-8").split("\n")[:-1], [f"{first} ok"])
        record = self.record_of(second)
        self.assertEqual((record["status"], record["category"]), ("unavailable", "symbol_limit"))
        self.assertIn(f"Analiz notu {second}: analiz notu alınamadı (symbol_limit)", err)

    def test_stdin_carries_exactly_the_recorded_packet(self):
        self.run_note("ok")
        for symbol in self.symbols:
            attempt = self.sym_dir(symbol) / "attempt-001"
            record = self.record_of(symbol)
            stdin = runner.STDIN_HEADER + (attempt / "packet.json").read_text(encoding="utf-8")
            self.assertEqual(record["inputs"]["stdin_sha256"], hashlib.sha256(stdin.encode("utf-8")).hexdigest())

    def test_newer_daily_captures_do_not_break_an_older_as_of(self):
        """Price files only reach AS_OF while a rolling capture runs to 01-13:
        the note step still works and nothing after AS_OF is sent."""
        short = self.tmp / "price_short"
        for symbol in self.symbols:
            bars = [period_fixtures._price_bar(d, o="10", h="10", l="10", c="10", adj="10", v=100)
                    for d in ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09")]
            period_fixtures._write_price_capture(short, symbol, "2026-01-05", "2026-01-09", bars)
        for symbol in self.symbols:  # a capture that starts after AS_OF is never opened
            (self.daily / symbol / "2026-01-12_2026-01-13.json").write_text("garbage", encoding="utf-8")
        code, out, err = self.run_note("ok", "--price-dir", str(short))
        self.assertEqual((code, self.status(out)), (0, "ok=2,rejected=0,failed=0"), err)
        for symbol in self.symbols:
            text = (self.sym_dir(symbol) / "attempt-001" / "packet.json").read_text(encoding="utf-8")
            self.assertNotIn(POST_AS_OF_MARKER, text)
            self.assertEqual(sorted(d for d in set(re.findall(r"\d{4}-\d{2}-\d{2}", text)) if d > AS_OF), [])


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

    def note_messages(self) -> list[str]:
        """With ``--llm-note-dir`` ONLY the note is produced (its own message,
        never appended to the deterministic notice)."""
        messages = self.messages("--llm-note-dir", str(self.out_dir))
        deterministic = self.messages()
        self.assertTrue(all(m not in deterministic for m in messages))
        self.assertTrue(messages[0].startswith("Opus 5.5 analiz notu") or messages[0].startswith(render.TITLE))
        return messages

    def joined(self, *extra: str) -> str:
        return "\n".join(self.messages("--llm-note-dir", str(self.out_dir), *extra))

    def test_without_the_flag_output_is_unchanged(self):
        self.run_note("ok")  # verified notes exist on disk, but the flag is off
        document = json.loads(self.obs_path.read_bytes())
        rule_texts = json.loads(REAL_RULE_TEXTS.read_text(encoding="utf-8"))["rules"]
        directions = {"H1": "negative", "H2": "positive", "H3": "negative", "H4": "positive"}
        expected = fmt.render_notification(document, rule_texts, directions)
        self.assertEqual(self.messages(), [m + "\n" for m in expected])
        plain = fmt.render_notification(document, rule_texts, None)
        self.assertEqual(self.messages("--no-direction-label"), [m + "\n" for m in plain])
        self.assertNotIn("analiz notu", "".join(self.messages()).lower())

    def test_verified_notes_are_one_message_per_symbol_plus_a_closing_message(self):
        self.run_note("ok")
        messages = self.note_messages()
        self.assertEqual(len(messages), 3)  # AAA/BBB symbols' notes, then the closing message
        for symbol, text in zip(sorted(self.symbols), messages):
            self.assertTrue(text.startswith(render.TITLE))
            self.assertIn(f"as_of: {AS_OF}", text)
            self.assertIn(fmt.DIRECTION_WARNING_TR, text)
            self.assertIn(f"{symbol} H2 (beklenen yön: yukarı (al yönlü)) — değerlendirme: destekliyor", text)
            self.assertIn(f"Sade anlatım: {PLAIN}", text)
            self.assertIn("• ", text)                        # the reasons as short bullets
            self.assertIn("Veri notları:\n• Takas verisi gecikmeli olabilir.", text)
            self.assertIn("Durum: preregistered_hypothesis_holdout_test_pending", text)
            other = next(s for s in self.symbols if s != symbol)
            self.assertNotIn(f"{other} H2", text)
            self.assertLessEqual(len(text), fmt.TELEGRAM_MESSAGE_LIMIT)
        closing = messages[-1]
        self.assertTrue(closing.startswith(render.TITLE))
        self.assertIn("Özet: doğrulanan 2, doğrulamadan geçemeyen 0, alınamayan 0.", closing)
        self.assertIn(render.VERIFIED_FOOTER, closing)
        self.assertIn(f"Kayıt: {(self.out_dir / AS_OF).as_posix()}", closing)
        self.assertNotIn("Sade anlatım", closing)

    def test_no_direction_label_flag_removes_the_label_from_the_note_too(self):
        self.run_note("ok")
        text = self.joined("--no-direction-label")
        for phrase in (*fmt.DIRECTION_LABELS_TR.values(), fmt.DIRECTION_WARNING_TR, "yön bilinmiyor"):
            self.assertNotIn(phrase, text)
        self.assertIn(f"{SYM_A} H2 — değerlendirme: destekliyor", text)

    def test_note_message_has_no_forbidden_words_outside_the_fixed_label(self):
        self.run_note("ok")
        text = self.joined()
        body = _clean(text)
        for word in _FORBIDDEN:
            self.assertIsNone(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", body), word)

    def test_status_lines_when_there_is_no_note(self):
        argv = ("--llm-note-dir", str(self.out_dir))
        last = lambda: self.messages(*argv)[-1].strip()
        self.assertEqual(last(), "Opus 5.5 analiz notu: bu as_of için not yok (adım çalıştırılmadı).")
        self.run_note("refusal")
        text = last()
        self.assertTrue(text.startswith(
            "Opus 5.5 analiz notu: doğrulanan 0, doğrulamadan geçemeyen 0, alınamayan 2."), text)
        for symbol in self.symbols:
            self.assertIn(f"{symbol}: analiz notu alınamadı (refusal).", text)
        self.assertNotIn(render.TITLE, text)
        self.out_dir = self.tmp / "rej"
        self.run_note("bad_number")
        text = self.messages("--llm-note-dir", str(self.out_dir))[-1].strip()
        self.assertTrue(text.startswith(
            "Opus 5.5 analiz notu: doğrulanan 0, doğrulamadan geçemeyen 2, alınamayan 0."), text)
        for symbol in self.symbols:
            self.assertIn(f"{symbol}: analiz notu doğrulamadan geçemedi (number_not_in_input).", text)
        for line in ("analiz notu alınamadı (usage_limit)", "analiz notu doğrulamadan geçemedi (schema)"):
            self.assertEqual(find_forbidden(line), [])

    def test_a_symbol_without_a_record_is_listed_as_not_run(self):
        self.run_note("ok")
        import shutil
        shutil.rmtree(self.sym_dir(SYM_B))
        text = self.joined()
        self.assertIn(f"{SYM_A} H2", text)
        self.assertIn(f"{SYM_B}: bu as_of için not yok (adım çalıştırılmadı).", text)
        self.assertIn("Özet: doğrulanan 1, doğrulamadan geçemeyen 0, alınamayan 0, çalıştırılmayan 1.", text)

    def test_partial_result_shows_the_good_note_and_each_symbols_own_status_line(self):
        self.run_note("bad_number", only=SYM_B)
        text = "\n".join(self.note_messages())
        self.assertTrue(text.startswith(render.TITLE))
        self.assertIn(f"{SYM_A} H2 (beklenen yön: yukarı (al yönlü)) — değerlendirme: destekliyor", text)
        self.assertNotIn(f"{SYM_B} H2 (", text)
        self.assertIn(f"{SYM_B}: analiz notu doğrulamadan geçemedi (number_not_in_input).", text)
        self.assertIn("Özet: doğrulanan 1, doğrulamadan geçemeyen 1, alınamayan 0.", text)
        self.assertNotIn("987654321", text)

    def test_symbol_limit_and_too_large_symbols_have_their_own_lines(self):
        document = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
        document["max_symbols_per_run"] = 1
        small = self.tmp / "limit.json"
        small.write_text(json.dumps(document), encoding="utf-8")
        self.run_note("ok", "--config", str(small))
        text = "\n".join(self.note_messages())
        first, second = sorted(self.symbols)
        self.assertIn(f"{first} H2", text)
        self.assertIn(f"{second}: analiz notu alınamadı (symbol_limit).", text)
        self.assertIn("Özet: doğrulanan 1, doğrulamadan geçemeyen 0, alınamayan 1.", text)

    def test_rejected_note_content_is_never_sent(self):
        self.run_note("forbidden")
        text = "\n".join(self.messages("--llm-note-dir", str(self.out_dir)))
        self.assertNotIn("Hedef fiyat yükselir", text)
        self.assertNotIn("net_size değeri", text)

    def test_tampered_note_is_not_shown(self):
        self.run_note("ok")
        note_path = self.sym_dir(SYM_A) / "attempt-001" / "note.json"
        note = json.loads(note_path.read_text(encoding="utf-8"))
        note["assessments"][0]["reasons"][0]["text"] = "Toplam 987654321 lot."
        note_path.write_text(json.dumps(note), encoding="utf-8")
        text = self.joined()
        self.assertIn(f"{SYM_A}: analiz notu doğrulamadan geçemedi (record_integrity).", text)
        self.assertNotIn("987654321", text)
        self.assertIn(f"{SYM_B} H2", text)  # the untouched symbol is still shown

    def test_tampered_packet_is_not_shown(self):
        self.run_note("ok")
        packet_path = self.sym_dir(SYM_A) / "attempt-001" / "packet.json"
        packet_path.write_bytes(packet_path.read_bytes() + b" ")
        text = self.joined()
        self.assertIn(f"{SYM_A}: analiz notu doğrulamadan geçemedi (record_integrity).", text)

    def test_long_note_is_split_and_every_chunk_keeps_the_title_and_warning(self):
        self.run_note("ok")
        states = [render.symbol_state(self.out_dir, AS_OF, s) for s in sorted(self.symbols)]
        states[0].note = json.loads(json.dumps(states[0].note))
        states[0].note["data_caveats"] = ["Takas verisi gecikmeli olabilir. " * 11] * 8
        chunks = render.render_note_messages(states, self.out_dir, AS_OF, directions={"H2": "positive"}, limit=700)
        self.assertGreater(len(chunks), 3)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 700)
            self.assertTrue(chunk.startswith(render.TITLE))
            self.assertEqual(chunk.count(fmt.DIRECTION_WARNING_TR), 1)
        self.assertIn("Durum: preregistered_hypothesis_holdout_test_pending", chunks[-1])

    def send(self, *extra: str, sent: list | None = None, fail_on: int | None = None):
        sent = sent if sent is not None else []

        def fake_send(token, chat_id, text, **kwargs):
            sent.append(text)
            return SendResult(message_id=len(sent))

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"FINTR_TELEGRAM_BOT_TOKEN": "123:abc", "FINTR_TELEGRAM_CHAT_ID": "9"}), \
                patch.object(notify, "send_message", fake_send), redirect_stdout(out), redirect_stderr(err):
            code = notify.main(self.notify_argv("--send", *extra))
        return code, err.getvalue(), sent

    def test_the_note_is_sent_as_its_own_message_with_its_own_receipt(self):
        self.run_note("ok")
        code, err, first = self.send()  # the deterministic notice, exactly as without the note step
        self.assertEqual(code, 0, err)
        receipt_path = self.tmp / "receipts" / AS_OF / "receipt.json"
        before = receipt_path.read_bytes()
        code, err, second = self.send("--llm-note-dir", str(self.out_dir))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(second), 3)  # one message per symbol + the closing message
        self.assertTrue(all(m.startswith(render.TITLE) for m in second))
        self.assertTrue(all(not m.startswith(render.TITLE) for m in first))
        self.assertEqual(receipt_path.read_bytes(), before)  # the notice's receipt is untouched
        note_receipt = json.loads(
            (self.tmp / "receipts" / AS_OF / "note_receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(note_receipt["schema"], "fintr_notify_note_receipt_v1")
        self.assertEqual(note_receipt["message_sha256"],
                         [hashlib.sha256(m.encode("utf-8")).hexdigest() for m in second])
        self.assertNotIn("llm_note_messages", json.loads(before))

    def test_the_note_receipt_blocks_a_second_send_but_not_the_notice(self):
        self.run_note("ok")
        self.assertEqual(self.send("--llm-note-dir", str(self.out_dir))[0], 0)
        code, err, sent = self.send("--llm-note-dir", str(self.out_dir))
        self.assertEqual((code, sent), (1, []))
        self.assertIn("analiz notu receipt zaten var", err)
        self.assertIn(pipeline.RECEIPT_EXISTS_MARK, err)  # the pipeline recognises it
        # --resend writes a timestamped note receipt; the notice's own guard is independent
        code, _, sent = self.send("--llm-note-dir", str(self.out_dir), "--resend")
        self.assertEqual((code, len(sent)), (0, 3))
        names = sorted(p.name for p in (self.tmp / "receipts" / AS_OF).iterdir())
        self.assertTrue(any(n.startswith("note_receipt-") for n in names), names)
        self.assertIn("note_receipt.json", names)
        self.assertEqual(self.send()[0], 0)  # the deterministic notice was never sent: still allowed

    def test_note_mode_with_no_condition_met_sends_nothing(self):
        document = json.loads(self.obs_path.read_bytes())
        for block in document["symbols"].values():
            for obs in block["scans"][0]["observations"]:
                obs["condition_met"] = False
        self.obs_path.write_text(json.dumps(document), encoding="utf-8")
        code, err, sent = self.send("--llm-note-dir", str(self.out_dir))
        self.assertEqual((code, sent), (0, []))

    def test_send_without_the_flag_has_no_llm_receipt_field(self):
        code, err, sent = self.send()
        self.assertEqual(code, 0, err)
        receipt = json.loads((self.tmp / "receipts" / AS_OF / "receipt.json").read_text(encoding="utf-8"))
        self.assertNotIn("llm_note_messages", receipt)
        self.assertEqual(receipt["schema"], "fintr_notify_receipt_v1")



# --- pipeline ------------------------------------------------------------------------------


SAVED = os.path.join("data", "interim", "scanner_daily", AS_OF, "observations.json")


def _go(argv, llm_stdout="LLM_NOTE_STATUS: ok=2,rejected=0,failed=0\n", llm_code=0, llm_raises=None,
        notify_code=0, notify_stderr="", note_notify_code=0, note_notify_stderr="", note_notify_raises=None,
        env=None):
    """Run the pipeline with every child replaced; returns (rc, calls, stdout,
    stderr, kwargs of each call)."""
    calls, kwargs_seen = [], []

    def run(cmd, **kwargs):
        calls.append(cmd)
        kwargs_seen.append(kwargs)
        name = Path(cmd[1]).name
        if name == "scan_observations.py":
            return subprocess.CompletedProcess(cmd, 0, stdout="OBS", stderr=f"Observations saved: {SAVED}\n")
        if name == "llm_note.py":
            if llm_raises:
                raise llm_raises
            return subprocess.CompletedProcess(cmd, llm_code, stdout=llm_stdout, stderr="note stderr\n")
        if name == "notify_observations.py":
            if "--llm-note-dir" in cmd:
                if note_notify_raises:
                    raise note_notify_raises
                return subprocess.CompletedProcess(cmd, note_notify_code, stdout="", stderr=note_notify_stderr)
            return subprocess.CompletedProcess(cmd, notify_code, stdout="", stderr=notify_stderr)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    out, err = io.StringIO(), io.StringIO()
    with patch.object(pipeline.subprocess, "run", run), redirect_stdout(out), redirect_stderr(err), \
            patch.dict(os.environ, env or {}):
        rc = pipeline.main(argv)
    return rc, calls, out.getvalue(), err.getvalue(), kwargs_seen


class PipelineTests(unittest.TestCase):
    def names(self, calls):
        return [Path(c[1]).name for c in calls]

    def test_default_off_behaviour_is_unchanged(self):
        rc, calls, out, _, _ = _go(["--allow-session-takeover"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.names(calls), ["collect_daily.py", "scan_observations.py", "notify_observations.py"])
        self.assertEqual(calls[2][2:], ["--as-of", AS_OF, "--scanner-dir", pipeline.SCANNER_DIR])
        self.assertNotIn("llm_note", out)
        self.assertNotIn("note_notify", out)
        self.assertEqual(pipeline.build_parser().parse_args([]).llm_note, False)

    def test_default_off_summary_is_the_same_text_as_before(self):
        rc, _, out, _, _ = _go(["--allow-session-takeover"])
        self.assertEqual(out.splitlines(), [
            "Pipeline summary:", "  collect: 0", "  scan: 0", "  notify: 0", f"  as_of: {AS_OF}",
            f"  scanner output: {SAVED}", "  sent: no (notify dry-run)"])
        rc, _, out, _, _ = _go(["--skip-collect", "--send"], notify_code=1,
                               notify_stderr="Notify failed: bu as_of için receipt zaten var (x)\n",
                               env={"FINTR_TELEGRAM_BOT_TOKEN": "t", "FINTR_TELEGRAM_CHAT_ID": "c"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[-1], "  sent: no (already sent earlier; no resend)")
        self.assertIn("  notify: already_sent", out)

    def test_the_deterministic_notice_goes_first_then_the_note_then_the_note_notice(self):
        rc, calls, out, err, kwargs = _go(["--allow-session-takeover", "--send", "--llm-note"],
                                          env={"FINTR_TELEGRAM_BOT_TOKEN": "t", "FINTR_TELEGRAM_CHAT_ID": "c"})
        self.assertEqual(rc, 0)
        self.assertEqual(self.names(calls), ["collect_daily.py", "scan_observations.py", "notify_observations.py",
                                             "llm_note.py", "notify_observations.py"])
        # the deterministic call is exactly what it is without --llm-note
        self.assertEqual(calls[2][2:], ["--as-of", AS_OF, "--scanner-dir", pipeline.SCANNER_DIR, "--send"])
        self.assertEqual(calls[3][2:], ["--as-of", AS_OF, "--scanner-dir", pipeline.SCANNER_DIR,
                                        "--daily-root", pipeline.DAILY_ROOT, "--out-dir", pipeline.LLM_NOTES_DIR])
        self.assertEqual(calls[4][2:], ["--as-of", AS_OF, "--scanner-dir", pipeline.SCANNER_DIR, "--send",
                                        "--llm-note-dir", pipeline.LLM_NOTES_DIR])
        self.assertNotIn("--resend", calls[4])
        self.assertIn("  llm_note: ok=2,rejected=0,failed=0", out)
        self.assertIn("  note_notify: 0", out)
        self.assertIn("note stderr", err)

    def test_captured_children_get_utf8_env_and_replace_errors_including_the_note_children(self):
        _, calls, _, _, kwargs = _go(["--llm-note"])
        for cmd, seen in zip(calls, kwargs):
            if Path(cmd[1]).name == "collect_daily.py":
                continue  # inherits stdio: it prompts itself
            self.assertEqual(seen["env"]["PYTHONIOENCODING"], "utf-8", cmd)
            self.assertEqual(seen["errors"], "replace", cmd)
            self.assertEqual(seen["encoding"], "utf-8", cmd)

    def test_note_failure_never_changes_the_notice_or_the_exit_code(self):
        cases = [
            dict(llm_stdout="LLM_NOTE_STATUS: ok=0,rejected=0,failed=2\n", llm_code=3),
            dict(llm_stdout="LLM_NOTE_STATUS: ok=0,rejected=2,failed=0\n", llm_code=3),
            dict(llm_stdout="LLM_NOTE_STATUS: error:data_load_failed\n", llm_code=1),
            dict(llm_stdout="", llm_code=1),
            dict(llm_stdout="garbage\n", llm_code=0),
            dict(llm_raises=OSError("boom")),
            dict(llm_raises=RuntimeError("boom")),
            dict(note_notify_code=1),
            dict(note_notify_code=2, note_notify_stderr="Notify failed: x\n"),
            dict(note_notify_raises=OSError("boom")),
        ]
        for case in cases:
            with self.subTest(case=case):
                rc, calls, out, _, _ = _go(["--llm-note", "--send"], **case,
                                           env={"FINTR_TELEGRAM_BOT_TOKEN": "t", "FINTR_TELEGRAM_CHAT_ID": "c"})
                self.assertEqual(rc, 0)
                self.assertEqual(self.names(calls)[:3], ["collect_daily.py", "scan_observations.py",
                                                         "notify_observations.py"])
                self.assertIn("--send", calls[2])
                self.assertNotIn("--llm-note-dir", calls[2])  # the notice ran before and without the note
                self.assertRegex(out, r"llm_note: \S+")
                self.assertRegex(out, r"note_notify: \S+")
                self.assertIn("notify: 0", out)

    def test_note_notice_status_in_the_summary(self):
        env = {"FINTR_TELEGRAM_BOT_TOKEN": "t", "FINTR_TELEGRAM_CHAT_ID": "c"}
        _, _, out, _, _ = _go(["--llm-note", "--send"], note_notify_code=1, env=env,
                              note_notify_stderr="Notify failed: bu as_of için analiz notu receipt zaten var (x)\n")
        self.assertIn("note_notify: already_sent", out)
        _, _, out, _, _ = _go(["--llm-note", "--send"], note_notify_code=1, env=env)
        self.assertIn("note_notify: failed:exit_1", out)
        _, calls, out, _, _ = _go(["--llm-note"], llm_stdout="LLM_NOTE_STATUS: no_observations\n")
        self.assertIn("note_notify: skipped", out)
        self.assertEqual(self.names(calls)[-1], "llm_note.py")

    def test_status_is_taken_from_the_status_line(self):
        _, _, out, _, _ = _go(["--llm-note"], llm_stdout="x\nLLM_NOTE_STATUS: already_noted\n")
        self.assertIn("llm_note: already_noted", out)
        _, _, out, _, _ = _go(["--llm-note"], llm_stdout="", llm_code=5)
        self.assertIn("llm_note: error:exit_5", out)

    def test_notify_failure_stops_before_the_note_step(self):
        rc, calls, out, _, _ = _go(["--llm-note"], notify_code=4)
        self.assertEqual(rc, 4)
        self.assertNotIn("llm_note.py", self.names(calls))
        self.assertIn("failed with exit code 4", out)

    def test_already_sent_notice_still_runs_the_note_steps(self):
        env = {"FINTR_TELEGRAM_BOT_TOKEN": "t", "FINTR_TELEGRAM_CHAT_ID": "c"}
        rc, calls, out, _, _ = _go(["--skip-collect", "--llm-note", "--send"], notify_code=1, env=env,
                                   notify_stderr="Notify failed: bu as_of için receipt zaten var (x)\n")
        self.assertEqual(rc, 0)
        self.assertEqual(self.names(calls), ["scan_observations.py", "notify_observations.py", "llm_note.py",
                                             "notify_observations.py"])
        self.assertIn("  notify: already_sent", out)
        self.assertIn("  note_notify: 0", out)
        self.assertEqual(out.splitlines()[-1], "  sent: no (already sent earlier; no resend)")

    def test_same_day_rerun_with_existing_note_is_idempotent(self):
        rc, calls, out, _, _ = _go(["--skip-collect", "--llm-note"], llm_stdout="LLM_NOTE_STATUS: already_noted\n")
        self.assertEqual(rc, 0)
        self.assertEqual(self.names(calls), ["scan_observations.py", "notify_observations.py", "llm_note.py",
                                             "notify_observations.py"])

    def test_dry_run_and_scan_failure_skip_the_note(self):
        rc, calls, _, _, _ = _go(["--dry-run", "--llm-note"])
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
