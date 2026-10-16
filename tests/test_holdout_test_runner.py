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
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fintr.research import cost_sensitivity
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


class _FixedDatetime:
    """Frozen clock for ``fintr.research.report`` so result bytes are comparable."""

    @staticmethod
    def now(tz=None):
        return datetime(2027, 4, 1, 6, 0, tzinfo=tz or timezone.utc)


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
        self._write_sensitivity(path)
        return path

    def _write_sensitivity(self, plan_path: Path, **overrides) -> None:
        """Synthetic pre-registered sensitivity config bound to this plan, and a
        synthetic DECISIONS.md recording its sha256 (like the real pair)."""
        doc = {
            "schema": "fintr_cost_sensitivity_v1", "config_id": "synthetic_cost_sensitivity",
            "decision_status": "non_decisional",
            "test_plan": {"sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest()},
            "primary_cost_bps_per_side": "10",
            "grid_cost_bps_per_side": ["0", "5", "20", "30"],
            "scopes": ["primary", "secondary"], "hypotheses": ["H1", "H2"],
        }
        doc.update(overrides)
        self.sens_config = self.tmp / "cost_sensitivity.json"
        self.sens_config.write_text(json.dumps(doc), encoding="utf-8")
        self._record_sensitivity_hash(hashlib.sha256(self.sens_config.read_bytes()).hexdigest())

    def _record_sensitivity_hash(self, sha256: str) -> None:
        self.decisions = self.tmp / "DECISIONS.md"
        self.decisions.write_text(
            "# synthetic decisions\n\nDuyarlılık config'i: "
            f"`{cost_sensitivity.CONFIG_RELATIVE_PATH}`, SHA-256 `{sha256}`.\n",
            encoding="utf-8",
        )

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
            "--cost-sensitivity-config", str(self.sens_config), "--decisions-doc", str(self.decisions),
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

    # --- pre-registered cost-sensitivity grid (DECISIONS.md 2026-10-16) ------------
    # Non-decisional: separate artifacts, never an input to the Holm step; the
    # primary results must not change. Synthetic fixtures only.

    def _direct_study(self, plan: Path, *, sensitivity: bool, out_dir: Path, scope: str = "primary",
                      hypothesis: str = "H1", extra_cost: str | None = None):
        """Runs run_study once through the runner's own argv builder (the
        exact invocation ``--execute`` uses), with a frozen clock so the
        result bytes are comparable."""
        checks, context = runner.run_dry_run_checks(self._parsed_args(plan))
        self.assertTrue(all(c["ok"] for c in checks), checks)
        spec_ref = next(r for r in context["plan"].specs if r.spec_id == f"synthetic_{hypothesis.lower()}")
        symbols = context["primary_symbols"] if scope == "primary" else context["secondary_symbols"]
        with patch("fintr.research.report.datetime", _FixedDatetime):
            return runner._run_study_for(
                spec_path=Path(spec_ref.path), test_plan_path=plan, test_scope=scope,
                cost_bps=Decimal(extra_cost or context["plan"].cost_bps_per_side), symbols=symbols,
                symbol_checks=context["symbol_checks"], out_dir=out_dir,
                sensitivity=(self.sens_config, self.decisions) if sensitivity else None,
            )

    def test_dry_run_lists_the_sensitivity_check_and_default_is_on(self):
        plan = self._standard_plan()
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 0, out)
        self.assertIn("[OK] 7. cost_sensitivity_config: ok", out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("[OK]")]), 7)
        defaults = runner._parser().parse_args([])
        self.assertFalse(defaults.no_cost_sensitivity)
        self.assertEqual(defaults.cost_sensitivity_config, runner.DEFAULT_COST_SENSITIVITY)
        self.assertEqual(defaults.decisions_doc, runner.DEFAULT_DECISIONS_DOC)

    def test_exit_code_constant_matches_run_study(self):
        self.assertEqual(runner._EXIT_SENSITIVITY_FAILED, runner._load_script_module("run_study").EXIT_SENSITIVITY_FAILED)

    def test_execute_writes_separate_non_decisional_artifacts(self):
        plan = self._standard_plan()
        code, out, _err = self._run(self._base_argv(plan) + ["--execute"])
        self.assertEqual(code, 0, out)
        manifest = json.loads((self.test_dir / "manifest.json").read_text(encoding="utf-8"))
        block = manifest["cost_sensitivity"]
        self.assertTrue(block["enabled"] and block["non_decisional"])
        self.assertEqual(block["grid_cost_bps_per_side"], ["0", "5", "20", "30"])
        self.assertEqual(block["config_sha256"], hashlib.sha256(self.sens_config.read_bytes()).hexdigest())
        self.assertEqual(block["errors"], {})
        self.assertEqual(sorted(block["artifacts"]), sorted(manifest["results"]))
        sensitivity_dir = self.out_dir / "cost_sensitivity"
        # primary result directory holds only the 4 primary/secondary results
        self.assertEqual(len(list(self.out_dir.glob("*.json"))), 4)
        self.assertEqual(len(list(sensitivity_dir.glob("*_cost_sensitivity.json"))), 4)
        decision = json.loads(Path(manifest["family_decision_path"]).read_text(encoding="utf-8"))
        for key, entry in block["artifacts"].items():
            path = Path(entry["path"])
            self.assertEqual(path.parent, sensitivity_dir)
            self.assertEqual(entry["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertNotIn(str(path), decision["input_result_sha256"])  # never reaches Holm
            artifact = json.loads(path.read_text(encoding="utf-8"))
            primary = json.loads(Path(manifest["results"][key]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(artifact["schema"], "fintr_cost_sensitivity_result_v1")
            self.assertEqual(artifact["label"], "duyarlılık, karar girdisi değil")
            self.assertIs(artifact["decisional"], False)
            self.assertEqual(artifact["primary_cost_bps_per_side"], "10")
            self.assertEqual(artifact["primary_result_sha256"], manifest["results"][key]["sha256"])
            self.assertEqual(artifact["config"]["sha256"], block["config_sha256"])
            self.assertEqual(artifact["test_plan_sha256"], manifest["test_plan_sha256"])
            self.assertEqual(artifact["spec_sha256"], primary["spec_sha256"])
            self.assertEqual(artifact["test_scope"], primary["test_scope"])
            costs = [run["transaction_cost_bps"] for run in artifact["runs"]]
            self.assertEqual(costs, ["0", "5", "20", "30"])
            self.assertNotIn("10", costs)  # the primary cost is not in the grid
            self.assertEqual([run["round_trip_cost_bps"] for run in artifact["runs"]], ["0", "10", "40", "60"])
            for run in artifact["runs"]:
                self.assertEqual(set(run["overall"]), set(primary["overall"]))
                self.assertEqual(run["parameters"]["transaction_cost_bps"], run["transaction_cost_bps"])
                for horizon, stats in run["overall"].items():
                    # same events, same difference/bootstrap; only the net mean level moves
                    self.assertEqual(stats["event_count"], primary["overall"][horizon]["event_count"])
                    self.assertLess(
                        abs(Decimal(stats["difference_mean"]) - Decimal(primary["overall"][horizon]["difference_mean"])),
                        Decimal("1e-12"),
                    )  # the cost cancels in the difference (numerically; formatting may differ)
                    boot, primary_boot = stats["block_bootstrap_difference"], primary["overall"][horizon]["block_bootstrap_difference"]
                    for field in ("status", "distinct_signal_dates", "seed", "resamples"):
                        self.assertEqual(boot[field], primary_boot[field])
                    gap = Decimal(stats["mean_return"]) - Decimal(primary["overall"][horizon]["mean_return"])
                    expected = (Decimal(10) - Decimal(run["transaction_cost_bps"])) * 2 / Decimal(10000)
                    self.assertLess(abs(gap - expected), Decimal("1e-12"))
                test = run["hypothesis_test"]
                for holm_field in ("p_value_for_holm", "enters_holm_family", "family_size", "familywise_alpha"):
                    self.assertNotIn(holm_field, test)
                self.assertEqual(test["p_value_one_sided"], primary["hypothesis_test"]["p_value_one_sided"])

    def test_primary_artifacts_are_byte_identical_with_and_without_sensitivity(self):
        plan = self._standard_plan()
        without_dir, with_dir = self.tmp / "without", self.tmp / "with"
        for scope in ("primary", "secondary"):
            for hypothesis in ("H1", "H2"):
                code0, saved0, _ = self._direct_study(plan, sensitivity=False, out_dir=without_dir, scope=scope,
                                                      hypothesis=hypothesis)
                code1, saved1, err1 = self._direct_study(plan, sensitivity=True, out_dir=with_dir, scope=scope,
                                                         hypothesis=hypothesis)
                self.assertEqual((code0, code1), (0, 0), err1)
                self.assertEqual(saved0.name, saved1.name)
                self.assertEqual(saved0.read_bytes(), saved1.read_bytes())
        self.assertFalse((without_dir / "cost_sensitivity").exists())
        self.assertEqual(len(list((with_dir / "cost_sensitivity").glob("*.json"))), 4)

    def test_full_holdout_primary_results_and_family_unchanged_by_sensitivity(self):
        plan = self._standard_plan()
        self.assertEqual(self._run(self._base_argv(plan) + ["--execute"])[0], 0)
        first = self._normalized_outputs(self.out_dir, self.test_dir)
        shutil.rmtree(self.out_dir)
        shutil.rmtree(self.test_dir)
        self.assertEqual(self._run(self._base_argv(plan) + ["--execute", "--no-cost-sensitivity"])[0], 0)
        second = self._normalized_outputs(self.out_dir, self.test_dir)
        self.assertEqual(first, second)
        manifest = json.loads((self.test_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertIs(manifest["cost_sensitivity"]["enabled"], False)
        self.assertFalse((self.out_dir / "cost_sensitivity").exists())

    @staticmethod
    def _normalized_outputs(out_dir: Path, test_dir: Path) -> dict:
        """Primary results and the family decision, minus the wall-clock stamp
        and the (hash-of-bytes-including-that-stamp) input digests."""
        outputs = {}
        for path in sorted(out_dir.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            document["reproducibility"].pop("generated_at_utc")
            outputs[path.name] = document
        family = json.loads((test_dir / "family_decision_primary.json").read_text(encoding="utf-8"))
        family.pop("input_result_sha256")
        outputs["family"] = family
        return outputs

    def test_hash_mismatch_with_decisions_doc_fails_closed(self):
        plan = self._standard_plan()
        self._record_sensitivity_hash("0" * 64)
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] 7. cost_sensitivity_config", out)
        self.assertIn("cost_sensitivity_config_sha256_mismatch", out)
        code, _out, err = self._run(self._base_argv(plan) + ["--execute"])
        self.assertEqual(code, 1)
        self.assertIn("Execute refused", err)
        self.assertFalse((self.test_dir / "LOCK").exists())  # the single run was not consumed
        self.assertFalse(self.out_dir.exists())

    def test_config_edited_after_recording_fails_closed(self):
        plan = self._standard_plan()
        self.sens_config.write_bytes(self.sens_config.read_bytes() + b"\n")
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("cost_sensitivity_config_sha256_mismatch", out)

    def test_missing_decisions_record_fails_closed(self):
        plan = self._standard_plan()
        self.decisions.write_text("# no record of the sensitivity config here\n", encoding="utf-8")
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("no_recorded_cost_sensitivity_hash_in_decisions_doc", out)

    def test_run_study_refuses_a_mismatched_config_before_computing_anything(self):
        plan = self._standard_plan()
        checks, context = runner.run_dry_run_checks(self._parsed_args(plan))
        self.assertTrue(all(c["ok"] for c in checks), checks)
        self._record_sensitivity_hash("1" * 64)
        out_dir = self.tmp / "refused"
        code, saved, err = self._direct_study_unchecked(plan, context, out_dir)
        self.assertEqual(code, 1)
        self.assertIsNone(saved)
        self.assertIn("cost sensitivity config rejected", err)
        self.assertFalse(out_dir.exists())  # not even the primary was computed

    def _direct_study_unchecked(self, plan, context, out_dir, *, cost: str = "10"):
        spec_ref = next(r for r in context["plan"].specs if r.spec_id == "synthetic_h1")
        return runner._run_study_for(
            spec_path=Path(spec_ref.path), test_plan_path=plan, test_scope="primary",
            cost_bps=Decimal(cost), symbols=context["primary_symbols"],
            symbol_checks=context["symbol_checks"], out_dir=out_dir,
            sensitivity=(self.sens_config, self.decisions),
        )

    def test_run_study_refuses_a_non_primary_cost(self):
        plan = self._standard_plan()
        _checks, context = runner.run_dry_run_checks(self._parsed_args(plan))
        out_dir = self.tmp / "refused_cost"
        code, saved, err = self._direct_study_unchecked(plan, context, out_dir, cost="5")
        self.assertEqual(code, 1)
        self.assertIsNone(saved)
        self.assertIn("is not the primary cost", err)
        self.assertFalse(out_dir.exists())

    def test_config_bound_to_the_plan_primary_cost_and_hash(self):
        plan = self._standard_plan()
        # Same plan hash recorded, but the config pre-registers another primary cost.
        self._write_sensitivity(plan, primary_cost_bps_per_side="12")
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("cost_sensitivity_config_primary_cost_mismatch", out)
        self._write_sensitivity(plan, test_plan={"sha256": "2" * 64})
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("cost_sensitivity_config_test_plan_sha256_mismatch", out)

    def test_config_must_cover_every_spec_and_scope(self):
        plan = self._standard_plan()
        self._write_sensitivity(plan, hypotheses=["H1"])
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("hypothesis 'H2' is not covered", out)
        self._write_sensitivity(plan, scopes=["primary"])
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("test_scope 'secondary' is not covered", out)

    def test_grid_containing_the_primary_cost_is_rejected(self):
        plan = self._standard_plan()
        self._write_sensitivity(plan, grid_cost_bps_per_side=["0", "10", "20"])
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("must not contain the primary cost", out)

    def test_decisional_config_is_rejected(self):
        plan = self._standard_plan()
        self._write_sensitivity(plan, decision_status="decisional")
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("cost_sensitivity_must_be_non_decisional", out)

    def test_run_study_sensitivity_requires_save(self):
        plan = self._standard_plan()
        _checks, context = runner.run_dry_run_checks(self._parsed_args(plan))
        spec_ref = next(r for r in context["plan"].specs if r.spec_id == "synthetic_h1")
        module = runner._load_script_module("run_study")
        argv = ["--spec", spec_ref.path, "--test-plan", str(plan), "--test-scope", "primary", "--split", "test",
                "--cost-bps", "10", "--out-dir", str(self.tmp / "nosave"),
                "--cost-sensitivity-config", str(self.sens_config), "--decisions-doc", str(self.decisions)]
        for symbol in context["primary_symbols"]:
            sc = context["symbol_checks"][symbol]
            argv += ["--data", f"{symbol}={sc.period_paths_used[0]},{sc.price_path}"]
        code, _stderr = runner._run_module_main(module, argv)
        self.assertEqual(code, 1)
        self.assertFalse((self.tmp / "nosave").exists())

    def test_sensitivity_failure_never_touches_primary_results_or_the_family(self):
        plan = self._standard_plan()
        with patch("fintr.research.cost_sensitivity.build_artifact", side_effect=RuntimeError("boom")):
            code, out, _err = self._run(self._base_argv(plan) + ["--execute"])
        self.assertEqual(code, 0, out)
        manifest = json.loads((self.test_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertIs(manifest["completed"], True)
        self.assertEqual(len(manifest["results"]), 4)
        self.assertTrue(Path(manifest["family_decision_path"]).exists())
        block = manifest["cost_sensitivity"]
        self.assertEqual(block["artifacts"], {})
        self.assertEqual(sorted(block["errors"]), sorted(manifest["results"]))
        self.assertIn("cost_sensitivity FAILED", out)
        self.assertFalse((self.out_dir / "cost_sensitivity").exists())

    def test_lock_is_still_o_excl_with_sensitivity_enabled(self):
        plan = self._standard_plan()
        checks, context = runner.run_dry_run_checks(self._parsed_args(plan))
        self.assertTrue(all(c["ok"] for c in checks))
        self.test_dir.mkdir(parents=True, exist_ok=True)
        (self.test_dir / "LOCK").write_text("{}", encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = runner.execute(self._parsed_args(plan), checks, context)
        self.assertEqual(code, 1)
        self.assertFalse(self.out_dir.exists())

    def test_no_cost_sensitivity_flag_skips_check_and_artifacts(self):
        plan = self._standard_plan()
        self._record_sensitivity_hash("0" * 64)  # would fail if it were checked
        code, out, _err = self._run(self._base_argv(plan) + ["--dry-run", "--no-cost-sensitivity"])
        self.assertEqual(code, 0, out)
        self.assertIn("[OK] 7. cost_sensitivity_config: disabled by --no-cost-sensitivity", out)

    def test_execute_stdout_has_no_sensitivity_numbers(self):
        plan = self._standard_plan()
        code, out, _err = self._run(self._base_argv(plan) + ["--execute"])
        self.assertEqual(code, 0, out)
        for forbidden in ("transaction_cost_bps", "mean_return", "p_value", "round_trip"):
            self.assertNotIn(forbidden, out)
        self.assertEqual(out.count("cost_sensitivity saved:"), 4)


if __name__ == "__main__":
    unittest.main()
