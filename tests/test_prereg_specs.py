"""Pre-registration proposal specs under configs/studies/prereg/ must load
with the repository's own study-spec loader and stay consistent with the
frozen symbol split, the exclusion list and their hypothesis documents.

No market data is read: these checks cover only the spec files, the split
file and docs/prereg/*.md.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from fintr.research.conditions import BoolAnd, BoolNot, BoolOr, Comparison, FeatureExpr
from fintr.research.features import FEATURE_FORMULAS
from fintr.research.study_spec import load_study_spec


ROOT = Path(__file__).resolve().parents[1]
PREREG_DIR = ROOT / "configs" / "studies" / "prereg"
SPLIT_PATH = ROOT / "configs" / "splits" / "bist30_2026_holdout_v2.json"
CURRENT_SPLIT_PATH = ROOT / "configs" / "splits" / "current.json"
EXCLUSIONS_PATH = ROOT / "configs" / "exclusions" / "symbols.json"
REQUIRED_DESCRIPTION_PREFIX = "ÖNERİ — Efe'nin onayını bekliyor; tavsiye değildir."


def _feature_names(node) -> set[str]:
    if isinstance(node, Comparison):
        return {expr.name for expr in (node.left, node.right) if isinstance(expr, FeatureExpr)}
    if isinstance(node, (BoolAnd, BoolOr)):
        return set().union(*(_feature_names(term) for term in node.terms))
    if isinstance(node, BoolNot):
        return _feature_names(node.term)
    raise AssertionError(f"unexpected node {type(node).__name__}")


class PreregSpecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths = sorted(PREREG_DIR.glob("*.json"))
        cls.split = json.loads(SPLIT_PATH.read_text(encoding="utf-8"))
        cls.split_sha256 = hashlib.sha256(SPLIT_PATH.read_bytes()).hexdigest()
        cls.excluded = set(json.loads(EXCLUSIONS_PATH.read_text(encoding="utf-8"))["symbols"])

    def test_family_has_four_to_six_specs(self):
        self.assertGreaterEqual(len(self.paths), 4)
        self.assertLessEqual(len(self.paths), 6)

    def test_current_split_pointer_is_the_split_the_specs_use(self):
        current = json.loads(CURRENT_SPLIT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(current["current"], "configs/splits/bist30_2026_holdout_v2.json")
        self.assertTrue(self.split["frozen"])

    def test_every_spec_loads_and_is_an_unfrozen_proposal(self):
        for path in self.paths:
            with self.subTest(spec=path.name):
                spec = load_study_spec(path)
                self.assertFalse(spec.frozen)
                self.assertTrue(spec.description.startswith(REQUIRED_DESCRIPTION_PREFIX))
                self.assertTrue(spec.spec_id.startswith("prereg_bist30_"))
                self.assertEqual(spec.raw_document["preregistration"]["status"],
                                 "proposal_pending_efe_approval")

    def test_symbols_match_frozen_split_and_exclude_excluded_symbols(self):
        for path in self.paths:
            with self.subTest(spec=path.name):
                spec = load_study_spec(path)
                self.assertEqual(list(spec.symbols), self.split["development_symbols"])
                self.assertEqual(list(spec.symbol_holdout), self.split["holdout_symbols"])
                self.assertFalse(set(spec.all_declared_symbols()) & self.excluded)
                prereg = spec.raw_document["preregistration"]
                self.assertEqual(prereg["split_file"], "configs/splits/bist30_2026_holdout_v2.json")
                self.assertEqual(prereg["split_file_sha256"], self.split_sha256)

    def test_shared_design_is_identical_across_the_family(self):
        specs = [load_study_spec(path) for path in self.paths]
        shared = {
            (
                str(spec.train_test_cutoff), spec.entry_timing, spec.horizons_trading_days,
                spec.min_event_warning_threshold, spec.adjustment_boundary_policy,
                spec.bootstrap.seed, spec.bootstrap.resamples, str(spec.bootstrap.confidence_level),
                spec.block_bootstrap,
                json.dumps({key: value for key, value in spec.raw_document["preregistration"].items()
                            if key not in ("hypothesis_id", "hypothesis_doc", "predicted_direction",
                                           "family_role", "primary_test")},
                           sort_keys=True),
            )
            for spec in specs
        }
        self.assertEqual(len(shared), 1)
        self.assertEqual(str(specs[0].train_test_cutoff), "2026-10-01")
        self.assertEqual(specs[0].entry_timing, "next_open")

    def test_preregistration_block_is_internally_consistent(self):
        ids = []
        for path in self.paths:
            with self.subTest(spec=path.name):
                spec = load_study_spec(path)
                prereg = spec.raw_document["preregistration"]
                ids.append(prereg["hypothesis_id"])
                self.assertTrue(path.name.startswith(prereg["hypothesis_id"].lower() + "_"))
                self.assertIn(prereg["primary_horizon_trading_days"], spec.horizons_trading_days)
                self.assertIn(prereg["predicted_direction"], ("negative", "positive"))
                self.assertEqual(prereg["multiple_comparison"], "holm")
                self.assertEqual(prereg["family_size"], len(prereg["primary_family_members"]))
                self.assertEqual(prereg["min_events_primary"], spec.min_event_warning_threshold)
                self.assertTrue((ROOT / prereg["hypothesis_doc"]).is_file())
                doc_text = (ROOT / prereg["hypothesis_doc"]).read_text(encoding="utf-8")
                self.assertIn(spec.spec_id, doc_text)
                self.assertIn(path.name, doc_text)
        self.assertEqual(len(set(ids)), len(ids))

    def test_primary_family_is_h1_h2_and_h3_h4_are_secondary_descriptive(self):
        # docs/prereg/DECISIONS.md 2026-09-27: Holm family m = 2, alpha 0.05.
        roles = {}
        for path in self.paths:
            with self.subTest(spec=path.name):
                info = load_study_spec(path).preregistration
                self.assertIsNotNone(info)
                self.assertEqual(info.primary_family_members, ("H1", "H2"))
                self.assertEqual(info.family_size, 2)
                self.assertEqual(str(info.familywise_alpha), "0.05")
                self.assertEqual(info.multiple_comparison, "holm")
                roles[info.hypothesis_id] = info.family_role
        self.assertEqual(roles, {
            "H1": "primary", "H2": "primary",
            "H3": "secondary_descriptive", "H4": "secondary_descriptive",
        })

    def test_block_bootstrap_parameters_match_the_preregistered_primary_test(self):
        for path in self.paths:
            with self.subTest(spec=path.name):
                spec = load_study_spec(path)
                block = spec.block_bootstrap
                self.assertEqual(
                    (block.block_length_signal_dates, block.resamples, block.seed), (5, 9999, 20261001),
                )
                self.assertEqual(spec.preregistration.primary_horizon_trading_days, 5)
                self.assertEqual(spec.preregistration.min_distinct_signal_dates_primary, 30)

    def test_conditions_use_only_existing_engine_features(self):
        for path in self.paths:
            with self.subTest(spec=path.name):
                names = _feature_names(load_study_spec(path).condition)
                self.assertTrue(names)
                self.assertLessEqual(names, set(FEATURE_FORMULAS))

    def test_spec_ids_are_unique(self):
        spec_ids = [load_study_spec(path).spec_id for path in self.paths]
        self.assertEqual(len(set(spec_ids)), len(spec_ids))


if __name__ == "__main__":
    unittest.main()
