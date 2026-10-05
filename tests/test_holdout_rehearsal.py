"""End-to-end rehearsal of the April 2027 one-shot holdout test.

Runs ``scripts/run_holdout_test.py`` (dry-run, ``--execute``, lock refusal,
output artifacts) against the REAL frozen specs, the REAL test plan
(``configs/studies/prereg/test_plan_v1.json``) and the REAL active split,
but over SYNTHETIC period records and price histories generated into a
temporary directory by ``tests/holdout_fixtures.py``. No real market data,
no network, no model call, no change to any frozen spec, threshold or plan;
git is used only against a throwaway repository. The synthetic returns say
nothing about any hypothesis.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path

from fintr.data.period_capture import load_period_capture
from fintr.data.price_history import load_price_history_capture
from fintr.features.price_join import join_price_history
from fintr.research.test_plan import load_test_plan

import holdout_fixtures as fixtures


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "configs" / "studies" / "prereg" / "test_plan_v1.json"
FREEZE_PATH = ROOT / "docs" / "prereg" / "FREEZE.md"
FROZEN_SEED = 20261001  # every frozen spec's bootstrap seed


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"holdout_rehearsal_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_script("run_holdout_test")
run_study_cli = _load_script("run_study")

_WORLD_TMP: tempfile.TemporaryDirectory | None = None
WORLD: Path
DEVELOPMENT: tuple[str, ...]
HOLDOUT: tuple[str, ...]
FIXTURES: dict[str, fixtures.SymbolFixture]


def setUpModule():
    global _WORLD_TMP, WORLD, DEVELOPMENT, HOLDOUT, FIXTURES
    _WORLD_TMP = tempfile.TemporaryDirectory()
    WORLD = Path(_WORLD_TMP.name)
    DEVELOPMENT, HOLDOUT = fixtures.declared_symbols()
    _, _, FIXTURES = fixtures.write_world(WORLD)


def tearDownModule():
    _WORLD_TMP.cleanup()


def _frozen_canonical_hashes() -> dict[str, str]:
    """Canonical spec hashes from docs/prereg/FREEZE.md, keyed by file name."""
    rows = re.findall(r"^\| `([^`]+\.json)` \| `([0-9a-f]{64})` \|$", FREEZE_PATH.read_text(encoding="utf-8"),
                      re.MULTILINE)
    return dict(rows)


def _git_repo(parent: Path) -> tuple[Path, str]:
    repo = parent / "repo"
    repo.mkdir()
    for args in (["git", "init", "-q"], ["git", "config", "user.email", "rehearsal@example.com"],
                 ["git", "config", "user.name", "Rehearsal"]):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("synthetic rehearsal repository\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "synthetic"], cwd=repo, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()
    return repo, head


class RehearsalCase(unittest.TestCase):
    """A throwaway working area: output dirs and git repo per test; the
    synthetic world is shared read-only unless a test copies it."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo, self.head = _git_repo(self.tmp)
        self.period_root = WORLD / "period_analysis"
        self.price_root = WORLD / "price_history"
        self.test_dir = self.tmp / "studies_test"
        self.out_dir = self.tmp / "studies"

    def copy_world(self) -> Path:
        copy = self.tmp / "world"
        shutil.copytree(WORLD, copy)
        self.period_root, self.price_root = copy / "period_analysis", copy / "price_history"
        return copy

    def argv(self, *extra: str) -> list[str]:
        return [
            "--period-analysis-root", str(self.period_root),
            "--price-history-root", str(self.price_root),
            "--out-dir", str(self.out_dir), "--test-dir", str(self.test_dir),
            "--repo-root", str(self.repo), *extra,
        ]

    def run_main(self, *extra: str):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = runner.main(self.argv(*extra))
        return code, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def failing(out: str) -> dict[int, str]:
        return {int(m.group(1)): m.group(2) for m in re.finditer(r"^\[FAIL\] (\d+)\. [^:]+: (.*)$", out, re.MULTILINE)}


class FixtureGeneratorTests(RehearsalCase):
    def test_world_has_30_declared_symbols_and_no_excluded_symbol(self):
        self.assertEqual((len(DEVELOPMENT), len(HOLDOUT)), (20, 10))
        self.assertEqual(sorted(FIXTURES), sorted(DEVELOPMENT + HOLDOUT))
        self.assertNotIn("DSTKF", FIXTURES)

    def test_generation_is_deterministic_and_lives_outside_the_repository(self):
        first = fixtures.build_symbol_fixture("THYAO")
        again = fixtures.build_symbol_fixture("THYAO")
        self.assertEqual(first.period_document, again.period_document)
        self.assertEqual(first.price_document, again.price_document)
        other_seed = fixtures.build_symbol_fixture("THYAO", seed=1)
        self.assertNotEqual(first.price_document, other_seed.price_document)
        self.assertFalse(WORLD.resolve().is_relative_to(ROOT))

    def test_period_record_and_price_history_load_with_the_real_loaders(self):
        fixture = FIXTURES["AKBNK"]
        capture = load_period_capture(self.period_root / "AKBNK" / fixture.period_name)
        price = load_price_history_capture(self.price_root / "AKBNK" / fixture.price_name)
        self.assertEqual((capture.start, capture.end), (fixtures.WINDOW_START, fixtures.WINDOW_END))
        self.assertEqual(capture.server_dates[0] >= fixtures.WINDOW_START, True)
        self.assertEqual(capture.server_dates[-1] <= fixtures.WINDOW_END, True)
        self.assertEqual(tuple(capture.server_dates), fixture.server_dates)
        self.assertGreater(len(price.bars), 150)

    def test_holiday_placeholders_are_price_only_days_never_server_dates(self):
        fixture = FIXTURES["AKBNK"]
        capture = load_period_capture(self.period_root / "AKBNK" / fixture.period_name)
        price = load_price_history_capture(self.price_root / "AKBNK" / fixture.price_name)
        joined = join_price_history(capture, price)
        in_window = {d for d in fixtures.HOLIDAY_PLACEHOLDERS if fixtures.WINDOW_START <= d <= fixtures.WINDOW_END}
        self.assertEqual(set(joined.placeholder_dates), in_window)
        self.assertTrue(in_window.isdisjoint(capture.server_dates))

    def test_one_symbol_has_a_scale_regime_and_the_others_do_not(self):
        for symbol in (fixtures.SCALE_REGIME_SYMBOL, "AKBNK"):
            fixture = FIXTURES[symbol]
            capture = load_period_capture(self.period_root / symbol / fixture.period_name)
            price = load_price_history_capture(self.price_root / symbol / fixture.price_name)
            regimes = join_price_history(capture, price).ratio_regimes
            if symbol == fixtures.SCALE_REGIME_SYMBOL:
                self.assertEqual(len(regimes), 1)
                self.assertEqual((regimes[0].k, regimes[0].day_count), (2, fixtures.SCALE_REGIME_DAY_COUNT))
                self.assertEqual((regimes[0].start, regimes[0].end),
                                 (fixture.scale_regime_dates[0], fixture.scale_regime_dates[-1]))
            else:
                self.assertEqual(regimes, ())

    def test_some_days_meet_the_h1_condition(self):
        self.assertTrue(any(f.conditioned_h1_dates for f in FIXTURES.values()))


class DryRunRejectionTests(RehearsalCase):
    def test_dry_run_passes_on_a_complete_world_and_writes_nothing(self):
        code, out, _err = self.run_main("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.failing(out), {})
        self.assertFalse(self.test_dir.exists())
        self.assertFalse(self.out_dir.exists())

    def test_symbol_with_only_a_pre_window_record_is_rejected(self):
        self.copy_world()
        victim = "THYAO"
        old = fixtures.build_symbol_fixture(victim, window_start=date(2026, 9, 1), window_end=date(2026, 9, 30))
        for path in (self.period_root / victim).glob("*.json"):
            path.unlink()
        fixtures.write_symbol_fixture(self.period_root, self.price_root, old)
        code, out, _err = self.run_main("--dry-run")
        self.assertEqual(code, 1)
        failing = self.failing(out)
        self.assertEqual(sorted(failing), [3, 5])
        self.assertIn("THYAO=no period_analysis record with start >= 2026-10-01", failing[3])
        self.assertIn("1 pre-window record(s) found, not used", failing[3])

    def test_pre_window_record_next_to_a_valid_one_is_not_used(self):
        self.copy_world()
        victim = "THYAO"
        old = fixtures.build_symbol_fixture(victim, window_start=date(2026, 9, 1), window_end=date(2026, 9, 30))
        # fixtures.write_symbol_fixture overwrites the price history file of
        # the same name only; the pre-window record gets its own file name.
        pre_path, _price = fixtures.write_symbol_fixture(self.period_root, self.price_root, old)
        args = runner._parser().parse_args(self.argv())
        checks, context = runner.run_dry_run_checks(args)
        self.assertTrue(all(c["ok"] for c in checks), checks)
        symbol_check = context["symbol_checks"][victim]
        self.assertEqual(symbol_check.period_paths_pre_window_ignored, [pre_path])
        self.assertNotIn(pre_path, symbol_check.period_paths_used)

    def test_record_that_starts_after_the_window_end_does_not_cover_the_window(self):
        self.copy_world()
        victim = "AKBNK"
        late = fixtures.build_symbol_fixture(victim, window_start=date(2027, 4, 1), window_end=date(2027, 4, 30))
        for path in (self.period_root / victim).glob("*.json"):
            path.unlink()
        fixtures.write_symbol_fixture(self.period_root, self.price_root, late)
        code, out, _err = self.run_main("--dry-run")
        self.assertEqual(code, 1)
        failing = self.failing(out)
        self.assertEqual(sorted(failing), [5])
        self.assertIn("AKBNK=missing AKD server date(s)", failing[5])

    def test_missing_symbol_is_rejected_by_name(self):
        self.copy_world()
        shutil.rmtree(self.period_root / "TTKOM")
        shutil.rmtree(self.price_root / "SASA")
        code, out, _err = self.run_main("--dry-run")
        self.assertEqual(code, 1)
        failing = self.failing(out)
        self.assertEqual(sorted(failing), [3, 4, 5])
        self.assertIn("TTKOM=no period_analysis record", failing[3])
        self.assertIn("SASA=no price history file found", failing[4])
        self.assertNotIn("TTKOM", failing[4])

    def test_execute_with_a_missing_symbol_is_refused_and_leaves_no_lock(self):
        self.copy_world()
        shutil.rmtree(self.period_root / "TTKOM")
        code, _out, err = self.run_main("--execute")
        self.assertEqual(code, 1)
        self.assertIn("Execute refused: not every dry-run check passed", err)
        self.assertFalse((self.test_dir / "LOCK").exists())
        self.assertFalse(self.out_dir.exists())

    def test_dstkf_is_never_a_study_symbol(self):
        world = self.copy_world()
        fixtures.write_world(world, symbols=("DSTKF",))
        self.assertTrue((self.period_root / "DSTKF").is_dir())
        args = runner._parser().parse_args(self.argv())
        checks, context = runner.run_dry_run_checks(args)
        self.assertTrue(all(c["ok"] for c in checks), checks)
        self.assertNotIn("DSTKF", context["primary_symbols"])
        self.assertNotIn("DSTKF", context["secondary_symbols"])
        self.assertNotIn("DSTKF", context["symbol_checks"])
        self.assertEqual(len(context["primary_symbols"]), 30)
        self.assertEqual(len(context["secondary_symbols"]), 10)

    def test_run_study_refuses_dstkf_data_with_the_exclusion_reason(self):
        fixture = fixtures.build_symbol_fixture("DSTKF")
        period_path, price_path = fixtures.write_symbol_fixture(self.tmp / "p", self.tmp / "q", fixture)
        spec_path = ROOT / "configs/studies/prereg/h1_close_premium_vs_vwap_proxy_v1.json"
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = run_study_cli.main([
                "--spec", str(spec_path), "--split", "test", "--test-plan", str(PLAN_PATH),
                "--test-scope", "primary", "--out-dir", str(self.out_dir), "--save",
                "--data", f"DSTKF={period_path},{price_path}",
            ])
        self.assertEqual(code, 1)
        self.assertIn("excluded symbol(s) DSTKF", stderr.getvalue())
        self.assertFalse(self.out_dir.exists())

    def test_run_before_the_window_end_is_rejected(self):
        early_end = date(2027, 3, 15)
        early = self.tmp / "early"
        self.period_root, self.price_root, _ = fixtures.write_world(early, window_end=early_end, price_end=early_end)
        code, out, _err = self.run_main("--dry-run")
        self.assertEqual(code, 1)
        failing = self.failing(out)
        # Price bars and AKD both stop at the same early date, so AKD covers
        # every bar that exists; check 4 (post-window bars) is what stops it.
        self.assertEqual(sorted(failing), [4])
        self.assertIn("0 tradeable bar(s) after 2027-03-31 (need >= 25)", failing[4])
        code, _out, err = self.run_main("--execute")
        self.assertEqual(code, 1)
        self.assertIn("Execute refused: not every dry-run check passed", err)
        self.assertFalse((self.test_dir / "LOCK").exists())

    def test_post_window_bar_minimum_is_exactly_25(self):
        def last_day_with(count: int) -> date:
            return fixtures.trading_days(date(2027, 4, 1), fixtures.PRICE_END)[count - 1]

        self.copy_world()
        victim = "EKGYO"
        for count, expect_ok in ((24, False), (25, True)):
            fixture = fixtures.build_symbol_fixture(victim, price_end=last_day_with(count))
            shutil.rmtree(self.price_root / victim)
            fixtures.write_symbol_fixture(self.period_root, self.price_root, fixture)
            code, out, _err = self.run_main("--dry-run")
            self.assertEqual(code == 0, expect_ok, (count, out))
            if not expect_ok:
                self.assertEqual(sorted(self.failing(out)), [4])
                self.assertIn(f"EKGYO={count} tradeable bar(s) after 2027-03-31 (need >= 25)", self.failing(out)[4])


class OneShotExecutionTests(RehearsalCase):
    """One real ``--execute`` (4 frozen specs x 2 scopes + Holm), shared by
    every assertion below; the run itself is the expensive part."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp_root = Path(cls._tmp.name)
        cls.repo, cls.head = _git_repo(cls.tmp_root)
        cls.test_dir = cls.tmp_root / "studies_test"
        cls.out_dir = cls.tmp_root / "studies"
        argv = [
            "--period-analysis-root", str(WORLD / "period_analysis"),
            "--price-history-root", str(WORLD / "price_history"),
            "--out-dir", str(cls.out_dir), "--test-dir", str(cls.test_dir), "--repo-root", str(cls.repo),
        ]
        cls.argv_base = argv

        def cli(extra):
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = runner.main(argv + extra)
            return code, stdout.getvalue(), stderr.getvalue()

        cls.cli = staticmethod(cli)
        cls.dry = cli(["--dry-run"])
        cls.first = cli(["--execute"])
        cls.result_hashes_after_first = cls._hash_tree(cls.tmp_root / "studies"), cls._hash_tree(cls.test_dir)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @staticmethod
    def _hash_tree(directory: Path) -> dict[str, str]:
        return {
            str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob("*")) if path.is_file()
        }

    def setUp(self):  # shared state lives on the class; no per-test tmp needed
        pass

    @property
    def manifest(self) -> dict:
        return json.loads((self.test_dir / "manifest.json").read_text(encoding="utf-8"))

    def results(self) -> dict[str, dict]:
        return {key: json.loads(Path(entry["path"]).read_text(encoding="utf-8"))
                for key, entry in self.manifest["results"].items()}

    def test_dry_run_before_execute_passed_all_six_checks(self):
        code, out, _err = self.dry
        self.assertEqual(code, 0, out)
        self.assertEqual(len(re.findall(r"^\[OK\] \d\.", out, re.MULTILINE)), 6)

    def test_execute_succeeded(self):
        code, out, err = self.first
        self.assertEqual(code, 0, err)
        self.assertEqual(len(re.findall(r"^run_study \S+ scope=(primary|secondary): ok$", out, re.MULTILINE)), 8)
        self.assertIn("evaluate_study_family (primary H1+H2): ok", out)
        self.assertIn("Manifest written", out)

    def test_stdout_carries_only_paths_and_statuses(self):
        # Every stdout line must be a check status, a run status or a file
        # path; no statistic is ever printed (they live in the JSON files).
        _code, out, _err = self.first
        allowed = (
            r"\[(OK|FAIL)\] \d\. \w+: .*",
            r"run_study \S+ scope=(primary|secondary): ok",
            r"  saved: \S+\.json",
            r"evaluate_study_family \(primary H1\+H2\): ok",
            r"Manifest written: \S+manifest\.json",
        )
        for line in filter(str.strip, out.splitlines()):
            self.assertTrue(any(re.fullmatch(pattern, line) for pattern in allowed), line)
        self.assertNotIn("p_value", out)
        self.assertNotIn("difference_mean", out)
    def test_manifest_records_commit_plan_hash_and_inputs(self):
        manifest = self.manifest
        plan = load_test_plan(PLAN_PATH)
        self.assertEqual(manifest["schema"], "fintr_holdout_test_manifest_v1")
        self.assertIs(manifest["completed"], True)
        self.assertEqual(manifest["code_commit"], self.head)
        self.assertEqual(manifest["test_plan_sha256"], plan.plan_sha256)
        self.assertEqual(sorted(manifest["results"]), sorted(
            f"{ref.spec_id}:{scope}" for ref in plan.specs for scope in ("primary", "secondary")))
        for entry in manifest["results"].values():
            self.assertEqual(entry["sha256"], hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest())
        inputs = manifest["input_file_sha256"]
        self.assertEqual(sum(1 for key in inputs if key.endswith(":price_history")), 30)
        self.assertEqual(sum(1 for key in inputs if ":period_capture:" in key), 30)
        self.assertFalse(any("DSTKF" in key for key in inputs))
        for symbol, fixture in FIXTURES.items():
            self.assertEqual(
                inputs[f"{symbol}:period_capture:{fixture.period_name}"],
                hashlib.sha256((WORLD / "period_analysis" / symbol / fixture.period_name).read_bytes()).hexdigest())

    def test_manifest_records_spec_hashes_and_seeds(self):
        manifest = self.manifest
        frozen = _frozen_canonical_hashes()
        plan = load_test_plan(PLAN_PATH)
        for ref in plan.specs:
            spec_file = Path(ref.path).name
            record = manifest["specs"][ref.spec_id]
            self.assertEqual(record["raw_sha256"], ref.raw_sha256)
            self.assertEqual(record["canonical_sha256"], frozen[spec_file])
            self.assertEqual(record["bootstrap_seed"], FROZEN_SEED)
            self.assertEqual(record["block_bootstrap_seed"], FROZEN_SEED)
        split_hashes = {r["split_guard"]["split_file_sha256"] for r in self.results().values()}
        self.assertEqual(split_hashes, {manifest["split_file_sha256"]})

    def test_every_result_records_spec_plan_hash_seed_commit_and_scope(self):
        frozen = _frozen_canonical_hashes()
        plan = load_test_plan(PLAN_PATH)
        by_spec_id = {ref.spec_id: Path(ref.path).name for ref in plan.specs}
        results = self.results()
        self.assertEqual(len(results), 8)
        for key, result in results.items():
            spec_id, scope = key.rsplit(":", 1)
            with self.subTest(result=key):
                self.assertEqual(result["schema"], "fintr_study_result_v1")
                self.assertEqual(result["spec_id"], spec_id)
                self.assertEqual(result["spec_sha256"], frozen[by_spec_id[spec_id]])
                self.assertEqual(result["test_plan_sha256"], plan.plan_sha256)
                self.assertEqual(result["split"], "test")
                self.assertEqual(result["test_scope"], scope)
                self.assertEqual(result["test_window_end"], "2027-03-31")
                self.assertIs(result["split_guard"]["guard_bypassed"], False)
                self.assertEqual(result["split_guard"]["excluded_symbols"], ["DSTKF"])
                repro = result["reproducibility"]
                self.assertEqual(repro["bootstrap_seed"], FROZEN_SEED)
                self.assertEqual(repro["block_bootstrap"]["seed"], FROZEN_SEED)
                self.assertEqual(repro["spec_sha256"], result["spec_sha256"])
                self.assertRegex(repro["code_commit"], r"^[0-9a-f]{40}$")
                self.assertTrue(repro["python_version"])
                self.assertEqual(result["parameters"]["transaction_cost_bps"], "10")
                if scope == "primary":
                    self.assertEqual((result["symbol_scope"], len(result["symbols"])), ("declared", 30))
                    self.assertEqual(sorted(result["symbols"]), sorted(DEVELOPMENT + HOLDOUT))
                else:
                    self.assertEqual((result["symbol_scope"], len(result["symbols"])), ("holdout_only", 10))
                    self.assertEqual(sorted(result["symbols"]), sorted(HOLDOUT))
                self.assertNotIn("DSTKF", result["symbols"])

    def test_result_input_hashes_match_the_synthetic_files(self):
        for key, result in self.results().items():
            inputs = result["reproducibility"]["input_file_sha256"]
            self.assertEqual(len(inputs), 2 * len(result["symbols"]), key)
            for symbol in result["symbols"]:
                fixture = FIXTURES[symbol]
                self.assertEqual(inputs[f"{symbol}:price_history"], hashlib.sha256(
                    (WORLD / "price_history" / symbol / fixture.price_name).read_bytes()).hexdigest())

    def test_signal_dates_stay_inside_the_window_and_scale_regime_days_are_dropped(self):
        for key, result in self.results().items():
            if not key.endswith(":primary"):
                continue
            quality = result["ratio_regime_quality"]
            self.assertEqual(quality[fixtures.SCALE_REGIME_SYMBOL]["dropped_signal_dates"],
                             fixtures.SCALE_REGIME_DAY_COUNT, key)
            self.assertEqual([q["dropped_signal_dates"] for s, q in quality.items()
                              if s != fixtures.SCALE_REGIME_SYMBOL], [0] * 29, key)
            self.assertEqual(result["overall"]["5"]["baseline_event_count"] > 0, True)

    def test_family_decision_is_primary_h1_h2_holm_only(self):
        manifest = self.manifest
        family_path = Path(manifest["family_decision_path"])
        self.assertEqual(family_path, self.test_dir / "family_decision_primary.json")
        self.assertEqual(manifest["family_decision_sha256"], hashlib.sha256(family_path.read_bytes()).hexdigest())
        family = json.loads(family_path.read_text(encoding="utf-8"))
        self.assertEqual(family["schema"], "fintr_study_family_result_v1")
        self.assertEqual((family["family_id"], family["split"], family["test_scope"]),
                         ("prereg_bist30_akd_price_v1", "test", "primary"))
        self.assertEqual((family["m"], family["multiple_comparison"], family["familywise_alpha"]),
                         (2, "holm", "0.05"))
        self.assertIs(family["confirmatory"], True)
        self.assertEqual(sorted(row["hypothesis_id"] for row in family["primary"]), ["H1", "H2"])
        # The executor hands the Holm step only the H1+H2 primary results
        # (docs/prereg/README.md "Holdout testi çalıştırma"); H3/H4 stay in
        # their own result files as descriptive, outside Holm.
        self.assertEqual(family["secondary_descriptive"], [])
        for key, result in self.results().items():
            if "_h3_" in key or "_h4_" in key:
                self.assertIs(result["hypothesis_test"]["enters_holm_family"], False, key)
                self.assertEqual(result["hypothesis_test"]["family_role"], "secondary_descriptive", key)
        self.assertTrue(all(row["holm"]["threshold"] for row in family["primary"]))
        primary_files = {entry["path"] for key, entry in manifest["results"].items()
                         if key.endswith(":primary") and ("_h1_" in key or "_h2_" in key)}
        self.assertEqual(set(family["input_result_sha256"]), primary_files)
        self.assertEqual(len(primary_files), 2)

    def test_second_execute_is_refused_by_the_lock_and_changes_nothing(self):
        before = self._hash_tree(self.out_dir), self._hash_tree(self.test_dir)
        self.assertEqual(before, self.result_hashes_after_first)
        lock = self.test_dir / "LOCK"
        self.assertTrue(lock.is_file())
        self.assertEqual(sorted(json.loads(lock.read_text(encoding="utf-8"))), ["created_at_utc", "pid"])
        code, out, err = self.cli(["--execute"])
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] 6. no_lock_or_previous_output", out)
        self.assertIn("Execute refused: not every dry-run check passed", err)
        self.assertEqual((self._hash_tree(self.out_dir), self._hash_tree(self.test_dir)), before)

    def test_dry_run_after_completion_reports_the_previous_output(self):
        code, out, _err = self.cli(["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("önceki test zaten tamamlanmış", out)

    def test_o_excl_lock_refuses_even_when_every_check_passes(self):
        fresh = Path(tempfile.mkdtemp(dir=self.tmp_root))
        argv = [a if a != str(self.test_dir) else str(fresh / "t") for a in self.argv_base]
        argv = [a if a != str(self.out_dir) else str(fresh / "o") for a in argv]
        (fresh / "t").mkdir()
        original_status = runner._previous_output_status
        calls = []

        def racing_status(lock_path, manifest_path):
            # Checks run clean, then a concurrent invocation creates the lock
            # before our O_EXCL open: the first call (check 6) sees nothing.
            calls.append(1)
            if len(calls) == 1:
                result = original_status(lock_path, manifest_path)
                lock_path.write_text("{}\n", encoding="utf-8")
                return result
            return original_status(lock_path, manifest_path)

        runner._previous_output_status = racing_status
        try:
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = runner.main(argv + ["--execute"])
        finally:
            runner._previous_output_status = original_status
        self.assertEqual(code, 1)
        self.assertIn("Execute refused: yarım kalmış çalışma", stderr.getvalue())
        self.assertFalse((fresh / "o").exists())
        self.assertFalse((fresh / "t" / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
