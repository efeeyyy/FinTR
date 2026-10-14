"""Weekly scanner replay (``fintr.scanner.replay``, ``scripts/replay_scans.py``)
and the opt-in ``scan_observations.py --data-cutoff-utc`` it relies on.

Synthetic temp data only; never touches ``data/interim``. The end-to-end
tests reuse the ``test_scanner`` daily-root fixture (base capture + two
rolling captures + price history) and the real frozen rules file.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from fintr.scanner import replay

import test_period_analysis as period_fixtures
import test_scanner
from test_scanner import REAL_RULES_FILE, ROOT, DailyRootEndToEndTests, scan
from test_symbol_guard import GuardEnvMixin


def _load_replay_script():
    spec = importlib.util.spec_from_file_location("replay_scans_test", ROOT / "scripts" / "replay_scans.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


replay_cli = _load_replay_script()


# --- synthetic documents (unit level) ---------------------------------------

def _block(*, value="1.5", met=True, sha="a" * 64, extra_input=None):
    inputs = [
        {"kind": "base", "path": "data\\interim\\period_analysis\\AAA\\b.json", "sha256": "b" * 64},
        {"kind": "rolling", "path": "data\\interim\\daily_capture\\AAA\\r.json", "sha256": sha},
    ]
    if extra_input:
        inputs.append(extra_input)
    return {
        "as_of_source": "default_common_last_day",
        "input_files": {
            "period_capture": {"path": "data\\interim\\period_analysis\\AAA\\b.json", "sha256": "b" * 64},
            "price_history": {"path": "data\\interim\\price_history\\AAA\\p.json", "sha256": "c" * 64},
        },
        "composition": {
            "composition_sha256": "d" * 64, "composer_version": "v1", "inputs": inputs,
            "gaps": {"count": 0, "dates": []},
            "unsettled": {"day_count": 0, "entries": []},
            "revisions": {"day_count": 0, "entries": []},
        },
        "scans": [{
            "as_of": "2026-01-12",
            "freshness": {"last_akd_server_date": "2026-01-12"},
            "observations": [{
                "rule_id": "H1", "family_role": "primary", "as_of": "2026-01-12",
                "suppressed": None, "condition_met": met, "not_evaluable_reason": None,
                "features": [{"name": "f1", "value": value, "formula": "x", "params": {}}],
                "validation_status": "pre_registered",
            }],
            "data_status": {"unsettled_families": [], "revised_families": []},
        }],
    }


def _document(**kwargs):
    return {
        "schema": "fintr_scanner_observations_v2",
        "generated_at_utc": "2026-01-12T20:00:00Z",
        "code_commit": "c0ffee", "python_version": "3.12.10",
        "rules_file": "configs\\scanner\\rules_v1.json", "rules_file_sha256": "e" * 64,
        "split_guard": {"split_id": "s"}, "warning": "w",
        "symbols": {"AAA": _block(**kwargs)},
    }


class DiffTests(unittest.TestCase):
    def test_identical_ignores_volatile_and_replay_only_fields(self):
        original = _document()
        again = copy.deepcopy(original)
        again["generated_at_utc"] = "2026-01-19T20:00:00Z"
        again["code_commit"] = "deadbeef"
        again["python_version"] = "3.13.0"
        again["data_cutoff_utc"] = "2026-01-12T20:00:00Z"
        again["symbols"]["AAA"]["as_of_source"] = "explicit"
        self.assertEqual(replay.diff_documents(original, again), [])
        volatile = replay.volatile_report(original, again)
        self.assertTrue(volatile["code_commit"]["changed"])  # reported, not counted
        self.assertEqual(volatile["code_commit"]["original"], "c0ffee")

    def test_path_prefix_does_not_matter_only_the_tail_and_sha(self):
        original = _document()
        moved = copy.deepcopy(original)
        for item in moved["symbols"]["AAA"]["composition"]["inputs"]:
            item["path"] = "C:\\elsewhere\\" + item["path"]
        for info in moved["symbols"]["AAA"]["input_files"].values():
            info["path"] = "/abs/" + info["path"].replace("\\", "/")
        moved["rules_file"] = "C:/repo/configs/scanner/rules_v1.json"
        moved["symbols"]["AAA"]["composition"]["composition_sha256"] = "f" * 64
        # different path spellings -> the derived hash is listed as not comparable, not a diff
        self.assertEqual(replay.not_comparable_symbols(original, moved), ["AAA"])
        self.assertEqual(replay.diff_documents(original, moved), [])
        # same spelling -> the hash is compared
        same_spelling = copy.deepcopy(original)
        same_spelling["symbols"]["AAA"]["composition"]["composition_sha256"] = "f" * 64
        diffs = replay.diff_documents(original, same_spelling)
        self.assertEqual([d["field"] for d in diffs], ["composition.composition_sha256"])

    def test_value_difference_is_keyed_by_symbol_rule_field(self):
        diffs = replay.diff_documents(_document(value="1.5"), _document(value="1.6", met=False))
        keys = {(d["symbol"], d["as_of"], d["rule"], d["field"]) for d in diffs}
        self.assertEqual(keys, {
            ("AAA", "2026-01-12", "H1", "observation.condition_met"),
            ("AAA", "2026-01-12", "H1", "observation.features.f1.value"),
        })
        by_field = {d["field"]: d for d in diffs}
        self.assertEqual(by_field["observation.features.f1.value"]["original"], "1.5")
        self.assertEqual(by_field["observation.features.f1.value"]["replay"], "1.6")
        summary = replay.summarize_diffs(diffs, {"AAA": {"class": "code_drift"}})
        self.assertEqual(summary["differing_symbol_rule_field_count"], 2)
        self.assertEqual(summary["by_rule"], {"H1": 2})
        self.assertEqual(summary["by_symbol"], {"AAA": 2})

    def test_missing_and_extra_fields_are_reported(self):
        original = _document()
        shorter = copy.deepcopy(original)
        del shorter["symbols"]["AAA"]["scans"][0]["data_status"]
        diffs = replay.diff_documents(original, shorter)
        self.assertTrue(diffs)
        self.assertTrue(all(d["kind"] == "missing_in_replay" for d in diffs))
        self.assertTrue(all(d["kind"] == "extra_in_replay"
                            for d in replay.diff_documents(shorter, original)))

    def test_classification(self):
        original = _document()
        drift = _document(value="9")
        diffs = replay.diff_documents(original, drift)
        classes = replay.classify_symbols(original, drift, diffs, mode="current", rules_changed=False)
        self.assertEqual(classes["AAA"]["class"], "code_drift")
        classes = replay.classify_symbols(original, drift, diffs, mode="current", rules_changed=True)
        self.assertEqual(classes["AAA"]["class"], "rule_change")

        revised = _document(value="9", sha="9" * 64)
        diffs = replay.diff_documents(original, revised)
        classes = replay.classify_symbols(original, revised, diffs, mode="current", rules_changed=False)
        self.assertEqual(classes["AAA"]["class"], "data_revision")
        self.assertEqual(classes["AAA"]["input_changes"]["sha256_changed"], ["composition.rolling:AAA/r.json"])
        classes = replay.classify_symbols(original, revised, diffs, mode="point-in-time", rules_changed=False)
        self.assertEqual(classes["AAA"]["class"], "input_set_mismatch")

        extra = {"kind": "rolling", "path": "data\\interim\\daily_capture\\AAA\\new.json", "sha256": "1" * 64}
        grown = _document(extra_input=extra)
        diffs = replay.diff_documents(original, grown)
        classes = replay.classify_symbols(original, grown, diffs, mode="current", rules_changed=False)
        self.assertEqual(classes["AAA"]["input_changes"]["added"], ["composition.rolling:AAA/new.json"])


class BuildArgvTests(unittest.TestCase):
    def _config(self, root: Path) -> replay.ReplayConfig:
        return replay.ReplayConfig(
            scanner_dir=root / "s", period_dir=root / "p", price_dir=root / "pr",
            daily_root=root / "d", project_root=ROOT,
        )

    def test_point_in_time_passes_cutoff_equal_to_generated_at_current_does_not(self):
        original = _document()
        config = self._config(Path("x"))
        pit = replay.build_scan_argv(original, config, mode="point-in-time", as_of="2026-01-12",
                                      lookback=2, symbols=["AAA"])
        cur = replay.build_scan_argv(original, config, mode="current", as_of="2026-01-12",
                                      lookback=2, symbols=["AAA"])
        self.assertEqual(pit[pit.index("--data-cutoff-utc") + 1], "2026-01-12T20:00:00Z")
        self.assertNotIn("--data-cutoff-utc", cur)
        self.assertIn("--daily-root", cur)
        self.assertEqual(cur[cur.index("--lookback-days") + 1], "2")

    def test_v1_document_is_replayed_without_daily_root(self):
        original = _document()
        original["schema"] = "fintr_scanner_observations_v1"
        argv = replay.build_scan_argv(original, self._config(Path("x")), mode="current",
                                       as_of="2026-01-12", lookback=1, symbols=["AAA"])
        self.assertNotIn("--daily-root", argv)

    def test_symbols_are_grouped_by_their_own_last_as_of(self):
        original = _document()
        original["symbols"]["BBB"] = copy.deepcopy(original["symbols"]["AAA"])
        original["symbols"]["BBB"]["scans"][0]["as_of"] = "2026-01-09"
        groups = replay._scan_groups(original)
        self.assertEqual(groups, [("2026-01-09", 1, ["BBB"]), ("2026-01-12", 1, ["AAA"])])


# --- CLI with a fake scanner runner -----------------------------------------

class FakeRunnerCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.scanner_dir = self.tmp / "scanner_daily"
        self.original = _document()
        for label in ("2026-01-09", "2026-01-12"):
            directory = self.scanner_dir / label
            directory.mkdir(parents=True)
            (directory / "observations.json").write_text(json.dumps(self.original), encoding="utf-8")
        self.calls: list[list[str]] = []

    def _main(self, *extra, runner):
        out = self.tmp / "out" / "report.json"
        argv = ["--scanner-dir", str(self.scanner_dir), "--out", str(out), *extra]
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = replay_cli.main(argv, runner=runner)
        return code, stdout.getvalue(), stderr.getvalue(), out

    def _runner_returning(self, document):
        def runner(argv):
            self.calls.append(argv)
            return 0, json.dumps(document), ""
        return runner

    def test_identical_exit_0_and_summary_last_line(self):
        again = copy.deepcopy(self.original)
        again["generated_at_utc"] = "later"
        code, stdout, _, out = self._main("--mode", "current", runner=self._runner_returning(again))
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip().splitlines()[-1], "REPLAY: as_of=2 identical=2 diff=0 errors=0")
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["schema"], replay.REPLAY_REPORT_SCHEMA)
        self.assertEqual(report["totals"]["identical"], 2)

    def test_last_selects_most_recent_saved_dates(self):
        code, stdout, _, _ = self._main("--last", "1", "--mode", "current",
                                          runner=self._runner_returning(self.original))
        self.assertEqual(code, 0)
        self.assertTrue(stdout.strip().splitlines()[-1].startswith("REPLAY: as_of=1 "))

    def test_differences_exit_2_with_classification_in_report(self):
        changed = _document(value="9.9")
        code, stdout, _, out = self._main("--mode", "current", runner=self._runner_returning(changed))
        self.assertEqual(code, 2)
        self.assertEqual(stdout.strip().splitlines()[-1], "REPLAY: as_of=2 identical=0 diff=2 errors=0")
        unit = json.loads(out.read_text(encoding="utf-8"))["units"][0]
        self.assertEqual(unit["status"], "diff")
        self.assertEqual(unit["symbol_classes"]["AAA"]["class"], "code_drift")
        self.assertEqual(unit["summary"]["by_rule"], {"H1": 1})
        self.assertIn("observation.features.f1.value", unit["summary"]["by_field"])

    def test_scanner_failure_is_an_error_exit_1_and_takes_precedence(self):
        def failing(argv):
            return 1, "", "Scan failed: no price history found for AAA\n"
        code, stdout, _, out = self._main("--mode", "current", runner=failing)
        self.assertEqual(code, 1)
        self.assertEqual(stdout.strip().splitlines()[-1], "REPLAY: as_of=2 identical=0 diff=0 errors=2")
        self.assertIn("no price history", json.loads(out.read_text(encoding="utf-8"))["units"][0]["error"])

    def test_unknown_as_of_and_empty_dir_are_errors(self):
        code, _, stderr, _ = self._main("--as-of", "2026-02-02", runner=self._runner_returning(self.original))
        self.assertEqual(code, 1)
        self.assertIn("2026-02-02", stderr)
        self.scanner_dir = self.tmp / "nothing"
        code, _, _, _ = self._main(runner=self._runner_returning(self.original))
        self.assertEqual(code, 1)

    def test_existing_report_is_never_overwritten(self):
        out = self.tmp / "out" / "report.json"
        out.parent.mkdir(parents=True)
        out.write_text("precious", encoding="utf-8")
        code, _, stderr, _ = self._main("--mode", "current", runner=self._runner_returning(self.original))
        self.assertEqual(code, 1)
        self.assertIn("no overwrite", stderr)
        self.assertEqual(out.read_text(encoding="utf-8"), "precious")
        self.assertEqual(self.calls, [])  # refused before any scan work
        with self.assertRaises(FileExistsError):
            replay.write_report(out, {"schema": "x"})
        self.assertEqual(out.read_text(encoding="utf-8"), "precious")

    def test_both_mode_runs_two_units_per_as_of(self):
        code, stdout, _, out = self._main("--as-of", "2026-01-12", runner=self._runner_returning(self.original))
        self.assertEqual(code, 0)
        modes = [(u["as_of"], u["mode"]) for u in json.loads(out.read_text(encoding="utf-8"))["units"]]
        self.assertEqual(modes, [("2026-01-12", "point-in-time"), ("2026-01-12", "current")])
        self.assertEqual(stdout.strip().splitlines()[-1], "REPLAY: as_of=1 identical=2 diff=0 errors=0")


# --- end to end with the real scanner on synthetic data ---------------------

FIXTURE_AS_OF = "2026-01-12"


class EndToEndReplayTests(GuardEnvMixin, unittest.TestCase):
    SYMBOL = DailyRootEndToEndTests.SYMBOL

    def _fixture(self):
        self.make_env()
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        holder = DailyRootEndToEndTests("test_composed_observation_schema_and_composition_block")
        period_root, daily_root, price_root = holder._write_fixture_data(root)
        # fixture price file says accessed 2026-09-26; make it exist before the captures
        for path in price_root.rglob("*.json"):
            document = json.loads(path.read_text(encoding="utf-8"))
            document["capture_metadata"]["accessed_at_utc"] = "2026-01-09T10:00:00Z"
            path.write_text(json.dumps(document), encoding="utf-8")
        self.paths = (period_root, daily_root, price_root)
        self.scanner_dir = root / "scanner_daily"
        self.root = root
        return root

    def _scan_argv(self, *extra):
        period_root, daily_root, price_root = self.paths
        return [
            "--symbols", self.SYMBOL, "--period-dir", str(period_root),
            "--daily-root", str(daily_root), "--price-dir", str(price_root),
            "--rules-file", str(REAL_RULES_FILE), "--as-of", FIXTURE_AS_OF,
            "--lookback-days", "2", "--format", "json", *extra,
        ]

    def _save_original(self) -> dict:
        code, stdout, stderr = replay.run_scan_script(self._scan_argv())
        self.assertEqual(code, 0, stderr)
        directory = self.scanner_dir / FIXTURE_AS_OF
        directory.mkdir(parents=True)
        (directory / "observations.json").write_text(stdout, encoding="utf-8")
        return json.loads(stdout)

    def _replay(self, *extra, out_name="report.json"):
        period_root, daily_root, price_root = self.paths
        out = self.root / "replay_out" / out_name
        argv = [
            "--scanner-dir", str(self.scanner_dir), "--period-dir", str(period_root),
            "--daily-root", str(daily_root), "--price-dir", str(price_root),
            "--out", str(out), *extra,
        ]
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = replay_cli.main(argv)
        report = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
        return code, stdout.getvalue(), report

    def _rewrite_saved(self, mutate) -> None:
        path = self.scanner_dir / FIXTURE_AS_OF / "observations.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        mutate(document)
        path.write_text(json.dumps(document), encoding="utf-8")

    def test_unchanged_code_and_data_replays_identically_in_both_modes(self):
        self._fixture()
        original = self._save_original()
        self.assertEqual(original["schema"], "fintr_scanner_observations_v2")
        self.assertNotIn("data_cutoff_utc", original)  # default scan output unchanged
        code, stdout, report = self._replay()
        self.assertEqual(code, 0, stdout)
        self.assertEqual(stdout.strip().splitlines()[-1], "REPLAY: as_of=1 identical=2 diff=0 errors=0")
        pit = report["units"][0]
        self.assertEqual(pit["mode"], "point-in-time")
        self.assertEqual(pit["data_cutoff_utc"], original["generated_at_utc"])
        self.assertTrue(pit["rules_file_sha256"]["equal"])
        # same path spellings -> the composition hash was actually compared
        self.assertEqual(pit["composition_sha256_not_comparable"], [])

    def test_late_revision_is_data_revision_in_current_and_invisible_point_in_time(self):
        self._fixture()
        self._save_original()
        _, daily_root, _ = self.paths
        # a capture made after the original scan (T+2 style custody revision of 01-12)
        late = test_scanner._daily_root_capture_document(
            self.SYMBOL, start="2026-01-12", end="2026-01-13", server_dates=["2026-01-12", "2026-01-13"],
            custody_by_date={"2026-01-12": "1350", "2026-01-13": "1400"},
            captured_at_utc="2999-01-01T00:00:00Z",
        )
        period_fixtures._write_capture(daily_root, self.SYMBOL, "2026-01-12", "2026-01-13", late)

        code, stdout, report = self._replay()
        self.assertEqual(code, 2, stdout)
        units = {u["mode"]: u for u in report["units"]}
        self.assertEqual(units["point-in-time"]["status"], "identical")
        current = units["current"]
        self.assertEqual(current["status"], "diff")
        info = current["symbol_classes"][self.SYMBOL]
        self.assertEqual(info["class"], "data_revision")
        self.assertEqual(info["input_changes"]["added"], ["composition.rolling:%s/2026-01-12_2026-01-13.json" % self.SYMBOL])
        self.assertEqual(stdout.strip().splitlines()[-1], "REPLAY: as_of=1 identical=1 diff=1 errors=0")

    def test_tampered_saved_observation_is_code_drift_in_both_modes(self):
        self._fixture()
        self._save_original()

        def tamper(document):
            observation = document["symbols"][self.SYMBOL]["scans"][-1]["observations"][0]
            observation["condition_met"] = not observation["condition_met"] if observation["condition_met"] is not None else True
            observation["features"][0]["value"] = "123456789"
        self._rewrite_saved(tamper)

        code, _, report = self._replay()
        self.assertEqual(code, 2)
        for unit in report["units"]:
            self.assertEqual(unit["status"], "diff", unit["mode"])
            self.assertEqual(unit["symbol_classes"][self.SYMBOL]["class"], "code_drift", unit["mode"])
            fields = unit["summary"]["by_field"]
            self.assertTrue(any(k.startswith("observation.features.") and k.endswith(".value")
                                for k in fields), fields)
            self.assertEqual(set(unit["summary"]["by_symbol"]), {self.SYMBOL})
    def test_changed_rules_hash_is_reported_as_rule_change(self):
        self._fixture()
        self._save_original()

        def tamper(document):
            document["rules_file_sha256"] = "0" * 64
            document["symbols"][self.SYMBOL]["scans"][-1]["observations"][0]["features"][0]["value"] = "7"
        self._rewrite_saved(tamper)

        code, _, report = self._replay("--mode", "current")
        self.assertEqual(code, 2)
        unit = report["units"][0]
        self.assertFalse(unit["rules_file_sha256"]["equal"])
        self.assertEqual(unit["symbol_classes"][self.SYMBOL]["class"], "rule_change")
        self.assertGreaterEqual(unit["document_level_diff_count"], 1)

    def test_replaying_a_missing_input_file_is_an_error_not_a_pass(self):
        self._fixture()
        self._save_original()
        _, _, price_root = self.paths
        for path in price_root.rglob("*.json"):
            path.unlink()
        code, stdout, report = self._replay("--mode", "current")
        self.assertEqual(code, 1)
        self.assertEqual(report["units"][0]["status"], "error")
        self.assertTrue(stdout.strip().splitlines()[-1].endswith("errors=1"))


# --- scan_observations.py --data-cutoff-utc ---------------------------------

class DataCutoffTests(GuardEnvMixin, unittest.TestCase):
    SYMBOL = DailyRootEndToEndTests.SYMBOL

    def _prepare(self):
        self.make_env()
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        holder = DailyRootEndToEndTests("test_composed_observation_schema_and_composition_block")
        period_root, daily_root, price_root = holder._write_fixture_data(root)
        for path in price_root.rglob("*.json"):
            document = json.loads(path.read_text(encoding="utf-8"))
            document["capture_metadata"]["accessed_at_utc"] = "2026-01-09T10:00:00Z"
            path.write_text(json.dumps(document), encoding="utf-8")
        return period_root, daily_root, price_root

    def _argv(self, paths, *extra):
        period_root, daily_root, price_root = paths
        return [
            "--symbols", self.SYMBOL, "--period-dir", str(period_root),
            "--daily-root", str(daily_root), "--price-dir", str(price_root),
            "--rules-file", str(REAL_RULES_FILE), "--as-of", "2026-01-09",
            "--format", "json", *extra,
        ]

    def test_cutoff_excludes_later_daily_captures_and_is_recorded(self):
        paths = self._prepare()
        code, stdout, stderr = replay.run_scan_script(self._argv(paths))
        self.assertEqual(code, 0, stderr)
        everything = json.loads(stdout)
        self.assertNotIn("data_cutoff_utc", everything)
        kinds = [i["kind"] for i in everything["symbols"][self.SYMBOL]["composition"]["inputs"]]
        self.assertEqual(kinds, ["base", "rolling", "rolling"])

        # daily1 captured 2026-01-12T09:00Z, daily2 2026-01-13T09:00Z
        code, stdout, stderr = replay.run_scan_script(
            self._argv(paths, "--data-cutoff-utc", "2026-01-12T10:00:00Z"))
        self.assertEqual(code, 0, stderr)
        cut = json.loads(stdout)
        self.assertEqual(cut["data_cutoff_utc"], "2026-01-12T10:00:00Z")
        names = [Path(i["path"]).name for i in cut["symbols"][self.SYMBOL]["composition"]["inputs"]]
        self.assertEqual(names, ["2026-01-05_2026-01-09.json", "2026-01-07_2026-01-12.json"])

    def test_cutoff_is_inclusive(self):
        paths = self._prepare()
        code, stdout, _ = replay.run_scan_script(
            self._argv(paths, "--data-cutoff-utc", "2026-01-13T09:00:00Z"))
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(stdout)["symbols"][self.SYMBOL]["composition"]["inputs"]), 3)

    def test_price_file_accessed_after_cutoff_is_not_used(self):
        paths = self._prepare()
        code, _, stderr = replay.run_scan_script(
            self._argv(paths, "--data-cutoff-utc", "2026-01-09T09:59:59Z"))
        self.assertEqual(code, 1)
        self.assertTrue("no period capture" in stderr or "no price history" in stderr, stderr)

    def test_naive_cutoff_is_rejected(self):
        paths = self._prepare()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            scan.main(self._argv(paths, "--data-cutoff-utc", "2026-01-12T10:00:00"))


if __name__ == "__main__":
    unittest.main()
