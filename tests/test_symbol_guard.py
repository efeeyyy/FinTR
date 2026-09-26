"""Offline tests for the frozen-split and symbol-exclusion guards.

Every split, pointer, exclusions file, capture, manifest and universe here
is synthetic and written to a temporary directory; the module-level default
paths (``symbol_split.DEFAULT_CURRENT_POINTER_PATH`` and
``symbol_exclusions.DEFAULT_EXCLUSIONS_PATH``) are patched to point at them,
so the repository's real ``configs/`` files are never read. Symbols are
synthetic: HOLDA/HOLDB = holdout, DEVA/DEVB = development, EXCLX = excluded,
OUTSD = outside the split (non-BIST 30 exploration). No network, no model.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fintr.evaluation import symbol_exclusions, symbol_split
from fintr.evaluation.leakage_guard import find_leakage_violations
from fintr.evaluation.manifest import MANIFEST_SCHEMA_VERSION, parse_eval_case
from fintr.evaluation.symbol_exclusions import (
    SymbolExclusionsError,
    load_active_symbol_exclusions,
    parse_symbol_exclusions,
)
from fintr.evaluation.symbol_guard import SymbolGuardError, load_active_guards, render_guard_lines
from fintr.evaluation.symbol_split import (
    SymbolSplitError,
    load_symbol_split,
    parse_symbol_split,
    resolve_active_split,
)
from fintr.research.study_spec import parse_study_spec

import test_period_analysis as period_fixtures


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"guard_test_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_study_cli = _load_script("run_study")
analyze = _load_script("analyze_period")
custody = _load_script("custody_flow")
validate = _load_script("validate_eval_manifest")
fetch_single = _load_script("fetch_price_history")
fetch_batch = _load_script("fetch_price_history_batch")
smoke = _load_script("live_collector_smoke")

HOLDOUT = ["HOLDA", "HOLDB"]
DEVELOPMENT = ["DEVA", "DEVB"]
EXCLUDED = "EXCLX"
OUTSIDE = "OUTSD"


def _split_doc(**overrides) -> dict:
    doc = {
        "schema": "fintr_symbol_split_v1",
        "split_id": "synthetic_split_v1",
        "seed": 7,
        "source_universes": [{"path": "synthetic/universe.json", "sha256": "0" * 64}],
        "pool_size": 4,
        "holdout_symbols": list(HOLDOUT),
        "development_symbols": list(DEVELOPMENT),
        "frozen": True,
    }
    doc.update(overrides)
    return doc


def _exclusions_doc(*symbols: str) -> dict:
    return {
        "schema": "fintr_symbol_exclusions_v1",
        "updated_at_utc": "2027-01-01T00:00:00Z",
        "symbols": {
            symbol: {
                "decided_by": "synthetic",
                "decided_on": "2027-01-01",
                "scope": "all collection, analysis, studies, evaluation and signals",
                "reason": "synthetic test reason",
                "retained_data": "synthetic informational note; opens no exception",
            }
            for symbol in symbols
        },
    }


class GuardEnvMixin:
    """Writes a synthetic ``<tmp>/configs/{splits,exclusions}`` layout and
    points the guard defaults at it for the duration of one test."""

    def make_env(
        self, *, split: dict | None = None, exclusions: dict | None = None,
        pointer: dict | None = None, write_pointer: bool = True, write_exclusions: bool = True,
    ) -> Path:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        splits = root / "configs" / "splits"
        splits.mkdir(parents=True)
        split_path = splits / "synthetic_split_v1.json"
        split_path.write_text(json.dumps(split or _split_doc()), encoding="utf-8")
        pointer_path = splits / "current.json"
        if write_pointer:
            pointer_path.write_text(json.dumps(pointer or {
                "schema": "fintr_current_split_v1",
                "current": "configs/splits/synthetic_split_v1.json",
            }), encoding="utf-8")
        exclusions_path = root / "configs" / "exclusions" / "symbols.json"
        exclusions_path.parent.mkdir(parents=True)
        if write_exclusions:
            exclusions_path.write_text(
                json.dumps(exclusions or _exclusions_doc(EXCLUDED)), encoding="utf-8",
            )
        self.enterContext(patch.object(symbol_split, "DEFAULT_CURRENT_POINTER_PATH", pointer_path))
        self.enterContext(patch.object(symbol_exclusions, "DEFAULT_EXCLUSIONS_PATH", exclusions_path))
        self.split_path = split_path
        return root


# --- loaders -----------------------------------------------------------------


class SplitLoaderTests(GuardEnvMixin, unittest.TestCase):
    def test_active_split_resolves_only_through_pointer_and_hashes_file_bytes(self):
        self.make_env()
        path, split = resolve_active_split()
        self.assertEqual(path.resolve(), self.split_path.resolve())
        self.assertEqual(split.split_id, "synthetic_split_v1")
        self.assertEqual(split.split_sha256, hashlib.sha256(self.split_path.read_bytes()).hexdigest())
        self.assertEqual(split.holdout_symbols, tuple(HOLDOUT))

    def test_pointer_may_name_an_absolute_path(self):
        root = self.make_env()
        pointer = root / "configs" / "splits" / "current.json"
        pointer.write_text(json.dumps({
            "schema": "fintr_current_split_v1", "current": str(self.split_path),
        }), encoding="utf-8")
        path, _split = resolve_active_split()
        self.assertEqual(path, self.split_path)

    def test_unfrozen_split_is_rejected_as_active(self):
        self.make_env(split=_split_doc(frozen=False))
        with self.assertRaisesRegex(SymbolSplitError, "split_not_frozen"):
            resolve_active_split()

    def test_superseded_split_is_rejected_as_active(self):
        self.make_env(split=_split_doc(superseded_by="synthetic_split_v2"))
        with self.assertRaisesRegex(SymbolSplitError, "split_superseded"):
            resolve_active_split()

    def test_superseded_by_key_rejects_even_when_null(self):
        self.make_env(split=_split_doc(superseded_by=None))
        with self.assertRaisesRegex(SymbolSplitError, "split_superseded"):
            resolve_active_split()

    def test_superseded_split_still_parses_for_audit(self):
        self.make_env(split=_split_doc(frozen=False, superseded_by="synthetic_split_v2"))
        split = load_symbol_split(self.split_path, require_active=False)
        self.assertEqual(split.superseded_by, "synthetic_split_v2")

    def test_missing_pointer_fails_closed(self):
        self.make_env(write_pointer=False)
        with self.assertRaisesRegex(SymbolSplitError, "current_split_pointer_missing"):
            resolve_active_split()

    def test_pointer_with_wrong_schema_fails(self):
        self.make_env(pointer={"schema": "other", "current": "configs/splits/synthetic_split_v1.json"})
        with self.assertRaisesRegex(SymbolSplitError, "unsupported_current_split_pointer_schema"):
            resolve_active_split()

    def test_pointer_to_missing_split_fails(self):
        self.make_env(pointer={"schema": "fintr_current_split_v1", "current": "configs/splits/nope.json"})
        with self.assertRaisesRegex(SymbolSplitError, "current_split_file_missing"):
            resolve_active_split()

    def test_structural_validation(self):
        cases = {
            "holdout_development_overlap": _split_doc(
                development_symbols=["DEVA", "HOLDA"], pool_size=4,
            ),
            "holdout_symbols_not_sorted": _split_doc(holdout_symbols=["HOLDB", "HOLDA"]),
            "pool_size_mismatch": _split_doc(pool_size=5),
            "unsupported_split_schema": _split_doc(schema="other"),
            "holdout_symbols_not_uppercase": _split_doc(holdout_symbols=["HOLDA", "holdb"]),
            "invalid_frozen": _split_doc(frozen="yes"),
        }
        for reason, document in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(SymbolSplitError, reason):
                    parse_symbol_split(document)


class ExclusionLoaderTests(GuardEnvMixin, unittest.TestCase):
    def test_missing_exclusions_file_fails_closed(self):
        self.make_env(write_exclusions=False)
        with self.assertRaisesRegex(SymbolExclusionsError, "exclusions_file_missing"):
            load_active_symbol_exclusions()
        with self.assertRaises(SymbolGuardError):
            load_active_guards()

    def test_retained_data_is_informational_only(self):
        self.make_env()
        _path, exclusions = load_active_symbol_exclusions()
        entry = exclusions.entry_for("exclx")
        self.assertIsNotNone(entry.retained_data)
        self.assertTrue(exclusions.is_excluded(" exclx "))

    def test_structural_validation(self):
        document = _exclusions_doc(EXCLUDED)
        del document["symbols"][EXCLUDED]["reason"]
        with self.assertRaisesRegex(SymbolExclusionsError, "reason"):
            parse_symbol_exclusions(document)
        with self.assertRaisesRegex(SymbolExclusionsError, "unsupported_exclusions_schema"):
            parse_symbol_exclusions({"schema": "other", "symbols": {}})
        lowercase = _exclusions_doc("exclx")
        with self.assertRaisesRegex(SymbolExclusionsError, "invalid_exclusion_symbol"):
            parse_symbol_exclusions(lowercase)

    def test_active_split_containing_excluded_symbol_is_rejected(self):
        self.make_env(exclusions=_exclusions_doc("HOLDA"))
        with self.assertRaisesRegex(SymbolGuardError, "split_contains_excluded_symbol"):
            load_active_guards()


class GuardReportTests(GuardEnvMixin, unittest.TestCase):
    def test_report_names_split_and_exclusions_files_with_sha256(self):
        self.make_env()
        report = load_active_guards().report(processed_symbols=["HOLDA"])
        self.assertEqual(report["split_file"], str(self.split_path.resolve()))
        self.assertEqual(report["split_file_sha256"], hashlib.sha256(self.split_path.read_bytes()).hexdigest())
        self.assertEqual(report["split_id"], "synthetic_split_v1")
        self.assertEqual(len(report["exclusions_file_sha256"]), 64)
        self.assertEqual(report["excluded_symbols"], [EXCLUDED])
        self.assertFalse(report["guard_bypassed"])
        self.assertIsNone(report["bypass_warning"])
        self.assertEqual(report["bypassed_holdout_symbols"], [])

    def test_bypass_report_lists_holdout_symbols_and_text_leads_with_warning(self):
        self.make_env()
        report = load_active_guards(bypass_split=True).report(processed_symbols=["holda", "DEVA", OUTSIDE])
        self.assertTrue(report["guard_bypassed"])
        self.assertIn("--no-split-guard", report["bypass_warning"])
        self.assertEqual(report["bypassed_holdout_symbols"], ["HOLDA"])
        lines = render_guard_lines(report)
        self.assertIn("--no-split-guard AKTİF", lines[0])
        self.assertIn("HOLDA", lines[0])


# --- run_study ---------------------------------------------------------------


def _spec(**overrides):
    doc = {
        "schema": "fintr_study_spec_v1",
        "spec_id": "synthetic_guard_spec",
        "description": "synthetic guard test spec",
        "frozen": False,
        "symbols": ["DEVA"],
        "symbol_holdout": ["HOLDA"],
        "train_test_cutoff": "2026-01-01",
        "entry_timing": "next_open",
        "horizons_trading_days": [1],
        "min_event_warning_threshold": 30,
        "adjustment_boundary_policy": "use_adjusted",
        "bootstrap": {"seed": 42, "resamples": 50, "confidence_level": "0.90"},
        "condition": {"op": "ge", "left": {"const": "0"}, "right": {"const": "0"}},
    }
    doc.update(overrides)
    return doc


def _write_symbol_data(directory: Path, symbol: str) -> str:
    """Synthetic period capture + price history for ``symbol``; returns the
    ``--data`` argument value."""
    packet = period_fixtures._packet()
    packet["requested_period"]["symbol"] = symbol
    period_path = directory / f"{symbol}_period.json"
    period_path.write_text(
        smoke._compact_json(period_fixtures._capture(packet)), encoding="utf-8",
    )
    days = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
            "2026-01-09", "2026-01-12", "2026-01-13"]
    bars = [period_fixtures._price_bar(d, v=100) for d in days]
    price_path = directory / f"{symbol}_price.json"
    price_path.write_text(json.dumps(period_fixtures._price_capture_document(
        f"{symbol}.IS", days[0], days[-1], bars,
    )), encoding="utf-8")
    return f"{symbol}={period_path},{price_path}"


class RunStudyGuardTests(GuardEnvMixin, unittest.TestCase):
    def setUp(self):
        self.root = self.make_env()

    def _write_spec(self, **overrides) -> Path:
        path = self.root / "spec.json"
        path.write_text(json.dumps(_spec(**overrides)), encoding="utf-8")
        return path

    def _run(self, argv):
        stdout, stderr = io.BytesIO(), io.StringIO()
        with redirect_stderr(stderr), patch.object(
            run_study_cli.sys, "stdout", io.TextIOWrapper(stdout, encoding="utf-8"),
        ) as wrapped:
            code = run_study_cli.main(argv)
            wrapped.flush()
        return code, stdout.getvalue().decode("utf-8"), stderr.getvalue()

    def test_train_rejects_holdout_data_symbol(self):
        spec = self._write_spec()
        code, _out, err = self._run([
            "--spec", str(spec), "--data", "DEVA=a,b", "--data", "HOLDA=a,b", "--split", "train",
        ])
        self.assertEqual(code, 1)
        self.assertIn("holdout symbols not allowed in --split train (HOLDA)", err)
        self.assertIn(hashlib.sha256(self.split_path.read_bytes()).hexdigest(), err)

    def test_train_rejects_spec_whose_train_symbols_include_holdout(self):
        spec = self._write_spec(symbols=["DEVA", "HOLDB"])
        code, _out, err = self._run(["--spec", str(spec), "--data", "DEVA=a,b", "--split", "train"])
        self.assertEqual(code, 1)
        self.assertIn("spec train symbols include holdout symbols (HOLDB)", err)

    def test_train_on_development_data_passes_the_guard_and_reports_split(self):
        data = _write_symbol_data(self.root, "DEVA")
        spec = self._write_spec()
        code, out, err = self._run(["--spec", str(spec), "--data", data, "--split", "train", "--format", "json"])
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["split_guard"]["split_file_sha256"],
                         hashlib.sha256(self.split_path.read_bytes()).hexdigest())
        self.assertFalse(result["split_guard"]["guard_bypassed"])
        self.assertEqual(result["symbols"], ["DEVA"])

    def test_test_split_rejects_unfrozen_spec(self):
        spec = self._write_spec(frozen=False)
        code, _out, err = self._run(["--spec", str(spec), "--data", "HOLDA=a,b", "--split", "test"])
        self.assertEqual(code, 1)
        self.assertIn("requires a frozen spec", err)

    def test_test_split_rejects_non_holdout_data_symbol(self):
        spec = self._write_spec(frozen=True)
        code, _out, err = self._run([
            "--spec", str(spec), "--data", "HOLDA=a,b", "--data", "DEVA=a,b", "--split", "test",
        ])
        self.assertEqual(code, 1)
        self.assertIn("accepts only holdout symbols of the frozen split (DEVA are not)", err)

    def test_test_split_rejects_spec_holdout_outside_split_holdout(self):
        spec = self._write_spec(frozen=True, symbol_holdout=["DEVB"])
        code, _out, err = self._run(["--spec", str(spec), "--data", "DEVB=a,b", "--split", "test"])
        self.assertEqual(code, 1)
        self.assertIn("not holdout symbols of the frozen split (DEVB)", err)

    def test_test_split_rejects_spec_without_symbol_holdout(self):
        spec = self._write_spec(frozen=True, symbol_holdout=[])
        code, _out, err = self._run(["--spec", str(spec), "--data", "HOLDA=a,b", "--split", "test"])
        self.assertEqual(code, 1)
        self.assertIn("requires the spec to declare symbol_holdout", err)

    def test_test_split_frozen_spec_evaluates_only_holdout_symbols(self):
        data = _write_symbol_data(self.root, "HOLDA")
        spec = self._write_spec(frozen=True)
        code, out, err = self._run(["--spec", str(spec), "--data", data, "--split", "test", "--format", "json"])
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["symbol_scope"], "holdout_only")
        self.assertEqual(result["symbols"], ["HOLDA"])
        self.assertEqual(result["split_guard"]["split_id"], "synthetic_split_v1")

    def test_excluded_symbol_rejected_even_with_no_split_guard(self):
        spec = self._write_spec()
        for extra in ([], ["--no-split-guard"]):
            with self.subTest(extra=extra):
                code, _out, err = self._run([
                    "--spec", str(spec), "--data", "DEVA=a,b", "--data", "exclx=a,b",
                    "--split", "train", *extra,
                ])
                self.assertEqual(code, 1)
                self.assertIn("excluded symbol(s) EXCLX", err)

    def test_spec_declaring_excluded_symbol_rejected(self):
        spec = self._write_spec(symbol_holdout=[EXCLUDED])
        code, _out, err = self._run(["--spec", str(spec), "--data", "DEVA=a,b", "--split", "train"])
        self.assertEqual(code, 1)
        self.assertIn("excluded symbol(s) EXCLX", err)

    def test_no_split_guard_is_reported_loudly(self):
        data = _write_symbol_data(self.root, "HOLDA")
        spec = self._write_spec(symbols=["HOLDA"], symbol_holdout=[])
        code, out, err = self._run([
            "--spec", str(spec), "--data", data, "--split", "train", "--no-split-guard",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("--no-split-guard is set", err)
        self.assertTrue(out.startswith("!!! UYARI: --no-split-guard AKTİF"), out[:200])
        self.assertIn("HOLDA", out.splitlines()[0])
        code, out, err = self._run([
            "--spec", str(spec), "--data", data, "--split", "train", "--no-split-guard",
            "--format", "json",
        ])
        guard = json.loads(out)["split_guard"]
        self.assertTrue(guard["guard_bypassed"])
        self.assertEqual(guard["bypassed_holdout_symbols"], ["HOLDA"])

    def test_superseded_active_split_blocks_every_run(self):
        self.split_path.write_text(json.dumps(_split_doc(superseded_by="x")), encoding="utf-8")
        spec = self._write_spec()
        code, _out, err = self._run(["--spec", str(spec), "--data", "DEVA=a,b", "--no-split-guard"])
        self.assertEqual(code, 1)
        self.assertIn("split_superseded", err)

    def test_check_study_symbols_allows_outside_symbol_in_train(self):
        guards = load_active_guards()
        spec = parse_study_spec(_spec(symbols=[OUTSIDE], symbol_holdout=[]))
        self.assertIsNone(run_study_cli.check_study_symbols(spec, [OUTSIDE], split="train", guards=guards))


# --- analyze_period / custody_flow ------------------------------------------


def _write_capture(root: Path, symbol: str) -> Path:
    packet = period_fixtures._packet()
    packet["requested_period"]["symbol"] = symbol
    directory = root / symbol
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "2026-01-05_2026-01-08.json"
    path.write_text(smoke._compact_json(period_fixtures._capture(packet)), encoding="utf-8")
    return path


class _BinaryStdoutMixin:
    def run_main(self, module, argv):
        stdout, stderr = io.BytesIO(), io.StringIO()
        with redirect_stderr(stderr), patch.object(
            module.sys, "stdout", io.TextIOWrapper(stdout, encoding="utf-8"),
        ) as wrapped:
            try:
                code = module.main(argv)
            except SystemExit as exc:
                code = exc.code
            wrapped.flush()
        return code, stdout.getvalue().decode("utf-8"), stderr.getvalue()


class AnalyzePeriodGuardTests(GuardEnvMixin, _BinaryStdoutMixin, unittest.TestCase):
    def setUp(self):
        self.root = self.make_env()
        self.captures = self.root / "captures"

    def test_single_holdout_capture_rejected_by_default(self):
        path = _write_capture(self.captures, "HOLDA")
        code, _out, err = self.run_main(analyze, [str(path)])
        self.assertEqual(code, 1)
        self.assertIn("HOLDA is a holdout symbol", err)

    def test_single_excluded_capture_rejected_even_with_bypass(self):
        path = _write_capture(self.captures, EXCLUDED)
        for extra in ([], ["--no-split-guard"]):
            with self.subTest(extra=extra):
                code, _out, err = self.run_main(analyze, [str(path), *extra])
                self.assertEqual(code, 1)
                self.assertIn("EXCLX is excluded", err)

    def test_single_development_capture_reports_split_file(self):
        path = _write_capture(self.captures, "DEVA")
        code, out, err = self.run_main(analyze, [str(path), "--format", "json"])
        self.assertEqual(code, 0, err)
        guard = json.loads(out)["split_guard"]
        self.assertEqual(guard["split_file_sha256"], hashlib.sha256(self.split_path.read_bytes()).hexdigest())

    def test_single_holdout_with_bypass_runs_and_is_loud(self):
        path = _write_capture(self.captures, "HOLDA")
        code, out, err = self.run_main(analyze, [str(path), "--no-split-guard"])
        self.assertEqual(code, 0, err)
        self.assertIn("--no-split-guard is set", err)
        self.assertTrue(out.startswith("!!! UYARI: --no-split-guard AKTİF"))

    def test_batch_skips_holdout_and_excluded(self):
        for symbol in ("DEVA", "HOLDA", EXCLUDED):
            _write_capture(self.captures, symbol)
        code, out, err = self.run_main(analyze, ["--all", str(self.captures), "--format", "json"])
        self.assertEqual(code, 0, err)
        index = json.loads(out)
        by_symbol = {entry["symbol"]: entry["status"] for entry in index["captures"]}
        self.assertEqual(by_symbol, {"DEVA": "ok", "HOLDA": "skipped_holdout", EXCLUDED: "skipped_excluded"})
        self.assertEqual(index["totals"]["skipped_holdout"], 1)
        self.assertEqual(index["totals"]["skipped_excluded"], 1)
        self.assertEqual(index["totals"]["failed"], 0)
        self.assertEqual(index["split_guard"]["split_id"], "synthetic_split_v1")

    def test_batch_checks_capture_symbol_not_only_directory_name(self):
        path = _write_capture(self.captures, "HOLDA")
        renamed = self.captures / "DEVB"
        path.parent.rename(renamed)
        code, out, _err = self.run_main(analyze, ["--all", str(self.captures), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["captures"][0]["status"], "skipped_holdout")

    def test_batch_bypass_processes_holdout_but_still_skips_excluded(self):
        for symbol in ("HOLDA", EXCLUDED):
            _write_capture(self.captures, symbol)
        code, out, _err = self.run_main(
            analyze, ["--all", str(self.captures), "--format", "json", "--no-split-guard"],
        )
        self.assertEqual(code, 0)
        index = json.loads(out)
        by_symbol = {entry["symbol"]: entry["status"] for entry in index["captures"]}
        self.assertEqual(by_symbol, {"HOLDA": "ok", EXCLUDED: "skipped_excluded"})
        self.assertTrue(index["split_guard"]["guard_bypassed"])
        self.assertEqual(index["split_guard"]["bypassed_holdout_symbols"], ["HOLDA"])

    def test_batch_saved_report_carries_split_guard(self):
        path = _write_capture(self.captures, "DEVA")
        code, _out, _err = self.run_main(analyze, ["--all", str(self.captures), "--format", "json", "--save"])
        self.assertEqual(code, 0)
        saved = analyze.report_filename(path, price_join_version=None, registry_sha8=None)
        report = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual(report["split_guard"]["split_id"], "synthetic_split_v1")

    def test_broken_active_split_fails_before_analysis(self):
        self.split_path.write_text(json.dumps(_split_doc(frozen=False)), encoding="utf-8")
        path = _write_capture(self.captures, "DEVA")
        code, _out, err = self.run_main(analyze, [str(path)])
        self.assertEqual(code, 1)
        self.assertIn("split_not_frozen", err)


class CustodyFlowGuardTests(GuardEnvMixin, _BinaryStdoutMixin, unittest.TestCase):
    def setUp(self):
        self.root = self.make_env()
        self.captures = self.root / "captures"

    def _run(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = custody.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_holdout_rejected_by_default(self):
        code, _out, err = self._run([str(_write_capture(self.captures, "HOLDA"))])
        self.assertEqual(code, 2)
        self.assertIn("HOLDA is a holdout symbol", err)

    def test_excluded_rejected_even_with_bypass(self):
        path = _write_capture(self.captures, EXCLUDED)
        code, _out, err = self._run([str(path), "--no-split-guard"])
        self.assertEqual(code, 2)
        self.assertIn("EXCLX is excluded", err)

    def test_development_symbol_output_reports_split(self):
        code, out, err = self._run([str(_write_capture(self.captures, "DEVA"))])
        self.assertEqual(code, 0, err)
        guard = json.loads(out)["split_guard"]
        self.assertEqual(guard["split_file_sha256"], hashlib.sha256(self.split_path.read_bytes()).hexdigest())
        self.assertFalse(guard["guard_bypassed"])

    def test_bypass_is_reported(self):
        code, out, err = self._run([str(_write_capture(self.captures, "HOLDA")), "--no-split-guard"])
        self.assertEqual(code, 0, err)
        self.assertIn("--no-split-guard is set", err)
        guard = json.loads(out)["split_guard"]
        self.assertTrue(guard["guard_bypassed"])
        self.assertEqual(guard["bypassed_holdout_symbols"], ["HOLDA"])


# --- eval validation / leakage guard -----------------------------------------


def _case(symbol: str, index: int) -> dict:
    sha = hashlib.sha256(f"synthetic-{index}".encode()).hexdigest()
    start, end = f"2030-01-{index + 1:02d}", f"2030-01-{index + 2:02d}"
    return {
        "case_id": f"synthetic_case_{index:04d}",
        "split": "eval",
        "symbol": symbol,
        "data_families": ["depth"],
        "observation_period": {"start": start, "end": end},
        "horizons": ["1d"],
        "source": {
            "path": f"data/eval/cases/synthetic_case_{index:04d}/capture.json",
            "sha256": sha,
            "captured_at": f"{end}T10:00:00+03:00",
        },
        "permission": {
            "basis": "user_asserted_local_storage_approval",
            "asserted_on": end,
            "independently_verified": False,
        },
        "leakage_group": f"{symbol}|{start}|{end}",
        "notes": "sentetik",
    }


class EvalGuardTests(GuardEnvMixin, unittest.TestCase):
    def setUp(self):
        self.root = self.make_env()

    def test_leakage_guard_flags_case_outside_holdout_when_split_given(self):
        guards = load_active_guards()
        cases = tuple(parse_eval_case(_case(symbol, i)) for i, symbol in enumerate(["HOLDA", "DEVA", OUTSIDE]))
        reasons = {
            (v.case_id, v.reason)
            for v in find_leakage_violations(cases, (), split=guards.split, exclusions=guards.exclusions)
        }
        self.assertEqual(reasons, {
            ("synthetic_case_0001", "not_in_holdout_split"),
            ("synthetic_case_0002", "not_in_holdout_split"),
        })
        self.assertEqual(find_leakage_violations(cases, ()), ())

    def test_leakage_guard_flags_excluded_symbol(self):
        guards = load_active_guards()
        cases = (parse_eval_case(_case(EXCLUDED, 0)),)
        reasons = {v.reason for v in find_leakage_violations(cases, (), exclusions=guards.exclusions)}
        self.assertEqual(reasons, {"excluded_symbol"})

    def _validate(self, symbols):
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({
            "schema": MANIFEST_SCHEMA_VERSION,
            "cases": [_case(symbol, i) for i, symbol in enumerate(symbols)],
        }), encoding="utf-8")
        registry = self.root / "registry.json"
        registry.write_text(json.dumps({"schema": "fintr_dev_registry_v1", "items": [{
            "item_id": "synthetic_dev_item", "symbol": "ZZDEV", "start": "2030-02-01",
            "end": "2030-02-02", "source_path": "data/interim/pilot/synthetic.json",
            "classification": "synthetic_development",
        }]}), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = validate.main([str(manifest), "--registry", str(registry)])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_validate_script_accepts_holdout_only_and_prints_split_sha(self):
        code, out, err = self._validate(["HOLDA", "HOLDB"])
        self.assertEqual(code, 0, out + err)
        self.assertIn(hashlib.sha256(self.split_path.read_bytes()).hexdigest(), out)
        self.assertIn("Dışlama dosyası", out)

    def test_validate_script_rejects_non_holdout_and_excluded(self):
        code, out, _err = self._validate(["HOLDA", "DEVA", EXCLUDED])
        self.assertEqual(code, 1)
        self.assertIn("synthetic_case_0001: not_in_holdout_split", out)
        self.assertIn("synthetic_case_0002: excluded_symbol", out)


# --- collection: holdout allowed, exclusion enforced -------------------------


class FetchPriceHistoryGuardTests(GuardEnvMixin, unittest.TestCase):
    def setUp(self):
        self.root = self.make_env()

    def _run(self, module, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = module.main(list(argv))
        except SystemExit as exc:
            code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_single_excluded_refused_before_any_network(self):
        for extra in (["--dry-run"], ["--save"]):
            with self.subTest(extra=extra), \
                 patch.object(fetch_single, "_today", return_value=date(2026, 6, 1)), \
                 patch.object(fetch_single, "_fetch_raw") as fetch_raw:
                code, _out, err = self._run(fetch_single, [
                    "--symbol", "exclx", "--start-date", "2026-01-05", "--end-date", "2026-01-09", *extra,
                ])
                self.assertEqual(code, 2)
                self.assertIn("EXCLX is excluded", err)
                fetch_raw.assert_not_called()

    def test_single_holdout_is_not_blocked(self):
        with patch.object(fetch_single, "_today", return_value=date(2026, 6, 1)), \
             patch.object(fetch_single, "PROJECT_ROOT", self.root):
            code, out, err = self._run(fetch_single, [
                "--symbol", "HOLDA", "--start-date", "2026-01-05", "--end-date", "2026-01-09", "--dry-run",
            ])
        self.assertEqual(code, 0, err)
        self.assertIn("Symbol: HOLDA", out)

    def test_batch_skips_excluded_and_collects_holdout(self):
        universe = self.root / "universe.json"
        universe.write_text(json.dumps({"symbols": ["DEVA", EXCLUDED, "HOLDA"]}), encoding="utf-8")
        raw = (ROOT / "tests" / "fixtures" / "market" / "price_history_chart.json").read_bytes()
        requested: list[str] = []

        def _fetch_raw(url, *, timeout):
            requested.append(url)
            return raw

        with patch.object(fetch_batch.fetch, "PROJECT_ROOT", self.root / "project"), \
             patch.object(fetch_batch.fetch, "_today", return_value=date(2026, 6, 1)), \
             patch.object(fetch_batch.fetch, "_fetch_raw", side_effect=_fetch_raw), \
             patch.object(fetch_batch, "time"):
            code, out, _err = self._run(fetch_batch, [
                "--universe", str(universe), "--start-date", "2026-01-05", "--end-date", "2026-01-09",
                "--save",
            ])
        self.assertEqual(code, 0, out)
        self.assertIn("skipped_excluded", out)
        self.assertEqual(len(requested), 2)
        self.assertFalse(any(EXCLUDED in url for url in requested))
        self.assertTrue(any("HOLDA" in url for url in requested))
        self.assertTrue((self.root / "project" / "data" / "interim" / "price_history" / "HOLDA").is_dir())
        self.assertFalse((self.root / "project" / "data" / "interim" / "price_history" / EXCLUDED).exists())


class LiveCollectorGuardTests(GuardEnvMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = self.make_env()

    async def test_single_symbol_excluded_refused_before_initdata_or_connection(self):
        for argv in (
            ("--symbol", "EXCLX", "--allow-session-takeover"),
            ("--symbol", "exclx", "--dry-run"),
            ("--symbol", "EXCLX"),  # even before the takeover-flag check
        ):
            with self.subTest(argv=argv):
                parser = smoke._parser()
                args = parser.parse_args(argv)
                error = io.StringIO()
                with (
                    patch.object(smoke, "WebSocketCollector") as collector,
                    patch.object(smoke, "InteractiveSecretProvider") as provider,
                    redirect_stderr(error), redirect_stdout(io.StringIO()),
                ):
                    with self.assertRaises(SystemExit) as captured:
                        await smoke._run(args, parser)
                self.assertEqual(captured.exception.code, 2)
                self.assertIn("EXCLX is excluded", error.getvalue())
                provider.assert_not_called()
                collector.assert_not_called()

    async def test_single_symbol_holdout_is_not_blocked(self):
        parser = smoke._parser()
        args = parser.parse_args(("--symbol", "HOLDA", "--custody-distribution-only", "--dry-run"))
        output = io.StringIO()
        with patch.object(smoke, "InteractiveSecretProvider") as provider, redirect_stdout(output):
            code = await smoke._run(args, parser)
        self.assertEqual(code, 0)
        provider.assert_not_called()

    def _multi_args(self, symbols: str, *extra: str):
        parser = smoke._parser()
        return parser, parser.parse_args((
            "--symbols", symbols, "--start-date", "2026-06-29", "--end-date", "2026-09-25",
            "--save-period-analysis-context", *extra,
        ))

    async def test_multi_symbol_all_excluded_never_prompts_or_connects(self):
        parser, args = self._multi_args("EXCLX", "--allow-session-takeover")
        output = io.StringIO()
        with (
            patch.object(smoke, "PROJECT_ROOT", self.root),
            patch.object(smoke, "WebSocketCollector") as collector,
            patch.object(smoke, "InteractiveSecretProvider") as provider,
            patch.object(smoke, "_today", return_value=date(2026, 9, 25)),
            redirect_stdout(output),
        ):
            code = await smoke._run(args, parser)
        self.assertEqual(code, 0)
        self.assertIn("EXCLX: skipped_excluded", output.getvalue())
        provider.assert_not_called()
        collector.assert_not_called()

    async def test_multi_symbol_skips_excluded_and_collects_holdout(self):
        parser, args = self._multi_args("HOLDA,EXCLX", "--allow-session-takeover")
        output = io.StringIO()
        with (
            patch.object(smoke, "PROJECT_ROOT", self.root),
            patch.object(smoke, "WebSocketCollector") as collector,
            patch.object(smoke, "InteractiveSecretProvider"),
            patch.object(smoke, "_today", return_value=date(2026, 9, 25)),
            patch.object(smoke, "_period_analysis_requests", wraps=smoke._period_analysis_requests) as build,
            redirect_stdout(output),
        ):
            collector.return_value.collect = AsyncMock(return_value=())
            await smoke._run(args, parser)
        rendered = output.getvalue()
        self.assertEqual([call.args[0] for call in build.call_args_list], ["HOLDA"])
        self.assertEqual(collector.call_count, 1)
        self.assertIn("EXCLX: skipped_excluded", rendered)
        self.assertRegex(rendered, r"EXCLX\s+skipped_excluded")

    async def test_multi_symbol_dry_run_lists_excluded_as_skipped(self):
        parser, args = self._multi_args("HOLDA,EXCLX", "--dry-run")
        output = io.StringIO()
        with (
            patch.object(smoke, "PROJECT_ROOT", self.root),
            patch.object(smoke, "InteractiveSecretProvider") as provider,
            patch.object(smoke, "_today", return_value=date(2026, 9, 25)),
            redirect_stdout(output),
        ):
            code = await smoke._run(args, parser)
        self.assertEqual(code, 0)
        rendered = output.getvalue()
        self.assertIn("Multi-symbol period mode: 1 symbol(s)", rendered)
        self.assertIn("EXCLX: skipped_excluded", rendered)
        provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
