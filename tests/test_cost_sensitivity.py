"""Unit tests for ``fintr.research.cost_sensitivity`` and the real pre-registered
config (``configs/studies/prereg/cost_sensitivity_v1.json``, DECISIONS.md
2026-10-16). No data of any kind is read; the end-to-end behaviour on synthetic
fixtures is in ``tests/test_holdout_test_runner.py``.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from fintr.research import cost_sensitivity as cs
from fintr.research.test_plan import load_test_plan


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "configs" / "studies" / "prereg" / "cost_sensitivity_v1.json"
PLAN_PATH = ROOT / "configs" / "studies" / "prereg" / "test_plan_v1.json"
DECISIONS_PATH = ROOT / "docs" / "prereg" / "DECISIONS.md"
PLAN_SHA = "b5070b39ed17c5c432cb2567fdb1917d52c1a4e4f552c4ae114a747f11d9182a"


def _document(**overrides) -> dict:
    document = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    document.update(overrides)
    return document


def _parse(document: dict) -> cs.CostSensitivityConfig:
    return cs.parse_cost_sensitivity_config(document, config_sha256="0" * 64)


class RealConfigTests(unittest.TestCase):
    def test_grid_is_the_pre_registered_one_and_excludes_the_primary_cost(self):
        config = cs.load_cost_sensitivity_config(CONFIG_PATH)
        self.assertEqual(config.grid_cost_bps_per_side, tuple(Decimal(v) for v in ("0", "5", "20", "30")))
        self.assertEqual(config.primary_cost_bps_per_side, Decimal("10"))
        self.assertNotIn(config.primary_cost_bps_per_side, config.grid_cost_bps_per_side)
        self.assertEqual(config.scopes, ("primary", "secondary"))
        self.assertEqual(config.hypotheses, ("H1", "H2", "H3", "H4"))

    def test_config_hash_is_recorded_in_decisions_and_matches(self):
        config = cs.load_cost_sensitivity_config(CONFIG_PATH)
        text = DECISIONS_PATH.read_text(encoding="utf-8")
        self.assertEqual(cs.recorded_config_sha256(text), config.config_sha256)
        self.assertEqual(config.config_sha256, hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest())
        cs.verify_recorded_hash(config, DECISIONS_PATH)

    def test_config_is_bound_to_the_frozen_test_plan(self):
        plan = load_test_plan(PLAN_PATH)
        self.assertEqual(plan.plan_sha256, PLAN_SHA)  # the plan file was not touched
        config = cs.load_verified_config(CONFIG_PATH, DECISIONS_PATH, plan)
        self.assertEqual(config.test_plan_sha256, plan.plan_sha256)
        self.assertEqual(config.primary_cost_bps_per_side, plan.cost_bps_per_side)

    def test_plan_scopes_and_hypotheses_are_covered(self):
        config = cs.load_cost_sensitivity_config(CONFIG_PATH)
        plan = load_test_plan(PLAN_PATH)
        self.assertEqual(config.scopes, ("primary", "secondary"))
        for hypothesis in plan.primary.primary_family_members + ("H3", "H4"):
            for scope in config.scopes:
                cs.require_covered(config, hypothesis_id=hypothesis, test_scope=scope)

    def test_config_is_non_decisional(self):
        document = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(document["decision_status"], "non_decisional")
        self.assertEqual(document["label_tr"], "duyarlılık, karar girdisi değil")
        self.assertEqual(document["only_changed_parameter"], "transaction_cost_bps")


class ParseValidationTests(unittest.TestCase):
    def test_grid_containing_the_primary_cost_is_rejected(self):
        for grid in (["0", "10"], ["10.0", "5"]):
            with self.subTest(grid=grid):
                with self.assertRaisesRegex(cs.CostSensitivityError, "primary cost"):
                    _parse(_document(grid_cost_bps_per_side=grid))

    def test_other_invalid_configs_are_rejected(self):
        bad = {
            "schema": {"schema": "other"},
            "decisional": {"decision_status": "decisional"},
            "negative cost": {"grid_cost_bps_per_side": ["-1", "5"]},
            "non-decimal": {"grid_cost_bps_per_side": ["abc"]},
            "number not string": {"grid_cost_bps_per_side": [5]},
            "empty grid": {"grid_cost_bps_per_side": []},
            "duplicate cost": {"grid_cost_bps_per_side": ["5", "5.0"]},
            "unknown scope": {"scopes": ["primary", "other"]},
            "empty hypotheses": {"hypotheses": []},
            "bad plan sha": {"test_plan": {"sha256": "abc"}},
        }
        for name, overrides in bad.items():
            with self.subTest(name=name):
                with self.assertRaises(cs.CostSensitivityError):
                    _parse(_document(**overrides))

    def test_unreadable_config_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cs.CostSensitivityError):
                cs.load_cost_sensitivity_config(Path(tmp) / "missing.json")


class HashVerificationTests(unittest.TestCase):
    def _copy(self, tmp: Path, *, mutate_config: bool = False, decisions: str | None = None):
        config_path = tmp / "cost_sensitivity_v1.json"
        data = CONFIG_PATH.read_bytes() + (b" " if mutate_config else b"")
        config_path.write_bytes(data)
        decisions_path = tmp / "DECISIONS.md"
        decisions_path.write_text(
            decisions if decisions is not None else DECISIONS_PATH.read_text(encoding="utf-8"), encoding="utf-8",
        )
        return config_path, decisions_path

    def test_unmodified_copy_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, decisions_path = self._copy(Path(tmp))
            cs.verify_recorded_hash(cs.load_cost_sensitivity_config(config_path), decisions_path)

    def test_modified_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, decisions_path = self._copy(Path(tmp), mutate_config=True)
            with self.assertRaisesRegex(cs.CostSensitivityError, "sha256_mismatch"):
                cs.verify_recorded_hash(cs.load_cost_sensitivity_config(config_path), decisions_path)

    def test_missing_record_or_unreadable_decisions_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, decisions_path = self._copy(Path(tmp), decisions="# nothing recorded\n")
            config = cs.load_cost_sensitivity_config(config_path)
            with self.assertRaisesRegex(cs.CostSensitivityError, "no_recorded"):
                cs.verify_recorded_hash(config, decisions_path)
            with self.assertRaisesRegex(cs.CostSensitivityError, "unreadable_decisions_doc"):
                cs.verify_recorded_hash(config, Path(tmp) / "absent.md")

    def test_a_different_file_name_in_the_record_does_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = cs.load_cost_sensitivity_config(CONFIG_PATH)
            text = f"`configs/studies/prereg/other_v1.json`, SHA-256 `{config.config_sha256}`\n"
            _config_path, decisions_path = self._copy(Path(tmp), decisions=text)
            with self.assertRaisesRegex(cs.CostSensitivityError, "no_recorded"):
                cs.verify_recorded_hash(config, decisions_path)

    def test_plan_binding_failures(self):
        plan = load_test_plan(PLAN_PATH)
        wrong_plan = cs.parse_cost_sensitivity_config(
            _document(test_plan={"sha256": "3" * 64}), config_sha256="0" * 64)
        with self.assertRaisesRegex(cs.CostSensitivityError, "test_plan_sha256_mismatch"):
            cs.validate_against_plan(wrong_plan, plan)
        wrong_cost = cs.parse_cost_sensitivity_config(
            _document(primary_cost_bps_per_side="12"), config_sha256="0" * 64)
        with self.assertRaisesRegex(cs.CostSensitivityError, "primary_cost_mismatch"):
            cs.validate_against_plan(wrong_cost, plan)


class ArtifactTests(unittest.TestCase):
    def _result(self, cost: str) -> dict:
        return {
            "parameters": {"transaction_cost_bps": cost, "transaction_cost_basis": "per_side"},
            "overall": {"5": {"event_count": 1}},
            "hypothesis_test": {
                "hypothesis_id": "H1", "p_value_one_sided": 0.5, "p_value_for_holm": 0.5,
                "enters_holm_family": True, "primary_family_members": ["H1", "H2"], "family_size": 2,
                "multiple_comparison": "holm", "familywise_alpha": "0.05", "family_role": "primary",
            },
            "test_plan_sha256": PLAN_SHA, "spec_id": "s", "spec_sha256": "4" * 64, "split": "test",
            "test_scope": "primary", "symbol_scope": "declared", "symbols": ["A"],
            "test_window_end": "2027-03-31", "reproducibility": {"code_commit": None},
        }

    def test_holm_fields_are_stripped_and_artifact_is_labelled_non_decisional(self):
        config = cs.load_cost_sensitivity_config(CONFIG_PATH)
        runs = [self._result(str(cost)) for cost in config.grid_cost_bps_per_side]
        artifact = cs.build_artifact(
            config=config, primary_result=self._result("10"), primary_payload_sha256="5" * 64, runs=runs,
        )
        self.assertEqual(artifact["label"], "duyarlılık, karar girdisi değil")
        self.assertIs(artifact["decisional"], False)
        self.assertEqual(artifact["hypothesis_id"], "H1")
        self.assertEqual([r["transaction_cost_bps"] for r in artifact["runs"]], ["0", "5", "20", "30"])
        self.assertEqual([r["round_trip_cost_bps"] for r in artifact["runs"]], ["0", "10", "40", "60"])
        text = json.dumps(artifact)
        for forbidden in ("p_value_for_holm", "enters_holm_family", "family_size", "familywise_alpha",
                          "primary_family_members", "multiple_comparison"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(artifact["runs"][0]["hypothesis_test"]["p_value_one_sided"], 0.5)

    def test_require_covered_rejects_unknown_scope_and_hypothesis(self):
        config = cs.load_cost_sensitivity_config(CONFIG_PATH)
        with self.assertRaises(cs.CostSensitivityError):
            cs.require_covered(config, hypothesis_id="H9", test_scope="primary")
        with self.assertRaises(cs.CostSensitivityError):
            cs.require_covered(config, hypothesis_id=None, test_scope="primary")
        with self.assertRaises(cs.CostSensitivityError):
            cs.require_covered(config, hypothesis_id="H1", test_scope="tertiary")


if __name__ == "__main__":
    unittest.main()
