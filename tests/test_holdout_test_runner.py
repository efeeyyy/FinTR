"""Offline tests for scripts/run_holdout_test.py, the one-shot pre-registered
holdout-test executor. Every plan, spec, split, period capture and price
history here is synthetic; nothing is a real market observation, and no
network, git push or model call happens. Git is used only against a
throwaway repository created in a temp directory for each test.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
import subprocess
import tempfile
import unittest

from fintr.research.study_spec import load_study_spec

import test_period_analysis as period_fixtures
import test_symbol_guard as guard_fixtures


ROOT = Path(__file__).resolve().parents[1]
TEST_START = date(2026, 1, 5)
TEST_END = date(2026, 1, 8)
PRICE_DAYS = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
              "2026-01-09", "2026-01-12", "2026-01-13"]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"holdout_test_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_script("run_holdout_test")


def _spec_doc(hypothesis_id: str, symbols: list[str], symbol_holdout: list[str], **overrides) -> dict:
    doc = {
        "schema": "fintr_study_spec_v1",
        "spec_id": f"synthetic_{hypothesis_id.lower()}",
        "description": "synthetic holdout-runner spec",
        "frozen": True,
        "symbols": list(symbols),
        "symbol_holdout": list(symbol_holdout),
        "train_test_cutoff": TEST_START.isoformat(),
        "entry_timing": "next_open",
        "horizons_trading_days": [1],
        "min_event_warning_threshold": 1,
        "adjustment_boundary_policy": "use_adjusted",
        "bootstrap": {"seed": 1, "resamples": 10, "confidence_level": "0.9"},
        "condition": {"op": "ge", "left": {"const": "0"}, "right": {"const": "0"}},
        "block_bootstrap": {
            "method": "moving_block_by_signal_date", "block_length_signal_dates": 1,
            "resamples": 9, "seed": 1,
        },
        "preregistration": {
            "hypothesis_id": hypothesis_id, "family_id": "synthetic_family",
            "family_role": "primary", "primary_family_members": ["H1", "H2"],
            "family_size": 2, "multiple_comparison": "holm", "familywise_alpha": "0.05",
            "predicted_direction": "negative", "primary_horizon_trading_days": 1,
            "min_events_primary": 1, "min_distinct_signal_dates_primary": 1,
            "sesoi_difference": "0",
        },
    }
    doc.update(overrides)
    return doc


def _write_symbol_files(
    period_root: Path, price_root: Path, symbol: str, *,
    period_start: str = "2026-01-05", period_end: str = "2026-01-08",
) -> None:
    packet = period_fixtures._packet()
    packet["requested_period"].update(symbol=symbol, start_date=period_start, end_date=period_end)
    document = period_fixtures._capture(packet)
    directory = period_root / symbol
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{period_start}_{period_end}.json").write_text(
        guard_fixtures.smoke._compact_json(document), encoding="utf-8",
    )
    bars = [period_fixtures._price_bar(d, v=100) for d in PRICE_DAYS]
    price_dir = price_root / symbol
    price_dir.mkdir(parents=True, exist_ok=True)
    (price_dir / f"{PRICE_DAYS[0]}_{PRICE_DAYS[-1]}.json").write_text(
        json.dumps(period_fixtures._price_capture_document(
            f"{symbol}.IS", PRICE_DAYS[0], PRICE_DAYS[-1], bars,
        )),
        encoding="utf-8",
    )


class HoldoutTestRunnerTests(guard_fixtures.GuardEnvMixin, unittest.TestCase):
    def setUp(self):
        self.guard_root = self.make_env()
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.period_root = self.tmp / "period_analysis"
        self.price_root = self.tmp / "price_history"
        self.test_dir = self.tmp / "studies_test"
        self.out_dir = self.tmp / "out"
        self.specs_dir = self.tmp / "specs"
        self.specs_dir.mkdir()
        self.repo_root = self._init_git_repo()

    def _init_git_repo(self) -> Path:
        repo = self.tmp / "repo"
        repo.mkdir()
        for args in (
            ["git", "init", "-q"], ["git", "config", "user.email", "test@example.com"],
            ["git", "config", "user.name", "Test"],
        ):
            subprocess.run(args, cwd=repo, check=True, capture_output=True)
        (repo / "README.md").write_text("synthetic\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "synthetic"], cwd=repo, check=True, capture_output=True,
        )
        return repo

    def _write_spec(self, hypothesis_id: str, **overrides) -> Path:
        doc = _spec_doc(hypothesis_id, ["DEVA", "DEVB"], ["HOLDA", "HOLDB"], **overrides)
        path = self.specs_dir / f"{hypothesis_id.lower()}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def _write_plan(self, spec_paths: dict[str, Path], **overrides) -> Path:
        specs = []
        for path in spec_paths.values():
            spec = load_study_spec(path)
            specs.append({
                "path": str(path), "spec_id": spec.spec_id,
                "raw_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "canonical_sha256": spec.spec_sha256,
            })
        doc = {
            "schema": "fintr_test_plan_v1", "plan_id": "synthetic_holdout_plan",
            "test_start": TEST_START.isoformat(), "test_end": TEST_END.isoformat(),
            "min_tradeable_bars_after_end": 2, "cost_bps_per_side": "10",
            "primary": {
                "symbol_scope": "all_declared", "family_id": "synthetic_family",
                "primary_family_members": ["H1", "H2"], "multiple_comparison": "holm",
            },
            "secondary": {"symbol_scope": "holdout_only"},
            "specs": specs,
        }
        doc.update(overrides)
        path = self.tmp / "test_plan.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def _write_all_symbol_files(self, **kwargs) -> None:
        for symbol in ("DEVA", "DEVB", "HOLDA", "HOLDB"):
            _write_symbol_files(self.period_root, self.price_root, symbol, **kwargs)

    def _standard_plan(self, **plan_overrides) -> Path:
        specs = {"H1": self._write_spec("H1"), "H2": self._write_spec("H2")}
        self._write_all_symbol_files()
        return self._write_plan(specs, **plan_overrides)

    def _base_argv(self, plan_path: Path) -> list[str]:
        return [
            "--test-plan", str(plan_path),
            "--period-analysis-root", str(self.period_root),
            "--price-history-root", str(self.price_root),
            "--out-dir", str(self.out_dir),
            "--test-dir", str(self.test_dir),
            "--repo-root", str(self.repo_root),
        ]

    def _run(self, argv: list[str]):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = runner.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    # --- dry-run never writes, and passes on a complete synthetic setup ----

    def test_dry_run_passes_and_writes_nothing(self):
        plan = self._standard_plan()
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 0, out)
        for line in out.splitlines():
            if line.strip():
                self.assertTrue(line.startswith("[OK]"), line)
        self.assertFalse(self.test_dir.exists())
        self.assertFalse(self.out_dir.exists())

    def test_dry_run_is_also_the_default_with_no_mode_flag(self):
        plan = self._standard_plan()
        code, out, _err = self._run(self._base_argv(plan))
        self.assertEqual(code, 0, out)
        self.assertFalse(self.out_dir.exists())

    # --- check 4: post-window tradeable-bar minimum -------------------------

    def test_dry_run_check4_fails_when_fewer_than_required_bars_after_window_end(self):
        plan = self._standard_plan(min_tradeable_bars_after_end=25)
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] 4. min_tradeable_bars_after_window_end", out)
        self.assertFalse(self.out_dir.exists())

    # --- check 3: pre-window-only period records are rejected --------------

    def test_dry_run_check3_rejects_pre_window_only_period_record(self):
        specs = {"H1": self._write_spec("H1"), "H2": self._write_spec("H2")}
        for symbol in ("DEVA", "DEVB", "HOLDA"):
            _write_symbol_files(self.period_root, self.price_root, symbol)
        # HOLDB only has a pre-window record (entirely before test_start).
        _write_symbol_files(
            self.period_root, self.price_root, "HOLDB",
            period_start="2025-12-01", period_end="2025-12-04",
        )
        plan = self._write_plan(specs)
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] 3. period_records_after_window_start", out)
        self.assertIn("HOLDB", out)
        self.assertIn("not used", out)

    # --- execute: full happy path -------------------------------------------

    def test_execute_runs_all_specs_and_scopes_and_writes_manifest(self):
        plan = self._standard_plan()
        code, out, _err = self._run(self._base_argv(plan) + ["--execute"])
        self.assertEqual(code, 0, out)

        manifest_path = self.test_dir / "manifest.json"
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(manifest["completed"])
        self.assertEqual(len(manifest["results"]), 4)  # 2 specs x 2 scopes
        self.assertIn("family_decision_path", manifest)
        self.assertTrue(Path(manifest["family_decision_path"]).exists())
        decision = json.loads(Path(manifest["family_decision_path"]).read_text(encoding="utf-8"))
        self.assertEqual(decision["m"], 2)
        self.assertTrue(decision["confirmatory"])

        result_files = list(self.out_dir.glob("*.json"))
        self.assertEqual(len(result_files), 4)
        scopes = {json.loads(p.read_text(encoding="utf-8"))["test_scope"] for p in result_files}
        self.assertEqual(scopes, {"primary", "secondary"})

        self.assertTrue((self.test_dir / "LOCK").exists())

    def test_execute_stdout_never_carries_result_numbers(self):
        plan = self._standard_plan()
        code, out, _err = self._run(self._base_argv(plan) + ["--execute"])
        self.assertEqual(code, 0, out)
        for forbidden in ("difference_mean", "p_value", "hypothesis_test", "\"overall\"",
                          "mean_return", "block_bootstrap"):
            self.assertNotIn(forbidden, out)

    def test_execute_refuses_a_second_run_after_completion(self):
        plan = self._standard_plan()
        code, _out, _err = self._run(self._base_argv(plan) + ["--execute"])
        self.assertEqual(code, 0)
        manifest_path = self.test_dir / "manifest.json"
        before = manifest_path.read_text(encoding="utf-8")

        code, out, err = self._run(self._base_argv(plan) + ["--execute"])
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] 6. no_lock_or_previous_output", out)
        self.assertEqual(manifest_path.read_text(encoding="utf-8"), before)

        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] 6. no_lock_or_previous_output", out)

    # --- lock / half-finished manifest ---------------------------------------

    def test_execute_refused_when_lock_present_without_manifest(self):
        plan = self._standard_plan()
        self.test_dir.mkdir(parents=True)
        (self.test_dir / "LOCK").write_text("{}", encoding="utf-8")
        code, out, _err = self._run(self._base_argv(plan) + ["--execute"])
        self.assertEqual(code, 1)
        self.assertIn("yarım kalmış çalışma", out)
        self.assertFalse((self.test_dir / "manifest.json").exists())

    def test_execute_stops_on_half_finished_manifest(self):
        plan = self._standard_plan()
        self.test_dir.mkdir(parents=True)
        (self.test_dir / "LOCK").write_text("{}", encoding="utf-8")
        (self.test_dir / "manifest.json").write_text(
            json.dumps({"schema": "fintr_holdout_test_manifest_v1", "completed": False}),
            encoding="utf-8",
        )
        code, out, _err = self._run(self._base_argv(plan) + ["--execute"])
        self.assertEqual(code, 1)
        self.assertIn("yarım kalmış çalışma", out)
        # Nothing was rerun: no result files, manifest untouched.
        self.assertFalse(self.out_dir.exists())
        manifest = json.loads((self.test_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["completed"])

    def test_execute_atomic_lock_race_is_refused_even_with_passing_checks(self):
        # Exercises the O_EXCL FileExistsError branch directly: a lock that
        # appears between the dry-run checks and the atomic create attempt.
        plan = self._standard_plan()
        checks, context = runner.run_dry_run_checks(self._parsed_args(plan))
        self.assertTrue(all(c["ok"] for c in checks))
        self.test_dir.mkdir(parents=True, exist_ok=True)
        (self.test_dir / "LOCK").write_text("{}", encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = runner.execute(self._parsed_args(plan), checks, context)
        self.assertEqual(code, 1)
        self.assertIn("yarım kalmış çalışma", stderr.getvalue())

    def _parsed_args(self, plan: Path):
        return runner._parser().parse_args(self._base_argv(plan))

    # --- git working tree -----------------------------------------------------

    def test_dry_run_check2_fails_on_dirty_working_tree(self):
        plan = self._standard_plan()
        (self.repo_root / "dirty.txt").write_text("x", encoding="utf-8")
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] 2. git_working_tree_clean", out)

    # --- plan/spec hash mismatch ------------------------------------------------

    def test_dry_run_check1_fails_on_spec_hash_mismatch(self):
        specs = {"H1": self._write_spec("H1"), "H2": self._write_spec("H2")}
        self._write_all_symbol_files()
        plan = self._write_plan(specs)
        # Edit H1's spec after the plan recorded its hash.
        specs["H1"].write_text(
            json.dumps(_spec_doc("H1", ["DEVA", "DEVB"], ["HOLDA", "HOLDB"], description="edited")),
            encoding="utf-8",
        )
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] 1. plan_and_spec_hashes", out)


if __name__ == "__main__":
    unittest.main()
