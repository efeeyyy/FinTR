"""Offline arithmetic checks; no model answers or provider data are used."""

from dataclasses import replace
import unittest

from fintr.evaluation.scoring import (
    CaseAssessment,
    DetectionCounts,
    EvaluationError,
    RatioCounts,
    score_assessments,
)


class EvaluationScoringTests(unittest.TestCase):
    def test_zero_denominators_are_not_fabricated_as_zero_scores(self):
        report = score_assessments([CaseAssessment("synthetic_1")])
        self.assertIsNone(report["numerical_faithfulness"]["value"])
        self.assertIsNone(report["contradiction"]["f1"])
        self.assertIsNone(report["unnecessary_request_rate"]["value"])
        self.assertIsNone(report["akd_custody"]["mean"])
        self.assertEqual(report["critical_failures"], [])

    def test_counts_are_aggregated_and_critical_failures_stay_visible(self):
        first = CaseAssessment(
            "synthetic_1",
            numerical=RatioCounts(2, 3),
            akd_custody_score=3,
            contradiction=DetectionCounts(1, 1, 0),
            missing_data=DetectionCounts(0, 0, 1),
            unnecessary_request=False,
            causal_overclaim=RatioCounts(1, 2),
            hallucination=RatioCounts(1, 3),
            uncertainty_score=2,
            unsupported_prediction=RatioCounts(0, 0),
            critical_failures=("invented_number",),
        )
        second = CaseAssessment(
            "synthetic_2",
            numerical=RatioCounts(1, 1),
            akd_custody_score=1,
            contradiction=DetectionCounts(1, 0, 1),
            unnecessary_request=True,
            hallucination=RatioCounts(0, 2),
            unsupported_prediction=RatioCounts(1, 1),
        )
        report = score_assessments([first, second])
        self.assertEqual(report["case_count"], 2)
        self.assertEqual(report["numerical_faithfulness"]["value"], 0.75)
        self.assertEqual(report["akd_custody"]["mean"], 2)
        self.assertEqual(report["contradiction"]["f1"], 2 / 3)
        self.assertEqual(report["missing_data"]["f1"], 0)
        self.assertEqual(report["unnecessary_request_rate"]["value"], 0.5)
        self.assertEqual(report["case_hallucination_rate"]["value"], 0.5)
        self.assertEqual(report["unsupported_price_prediction_rate"]["value"], 1)
        self.assertEqual(report["critical_failures"], [
            {"case_id": "synthetic_1", "codes": ["invented_number"]},
        ])

    def test_detection_no_predictions_but_gold_exists_is_zero_f1(self):
        report = score_assessments([CaseAssessment(
            "synthetic_1", multi_horizon=DetectionCounts(0, 0, 2),
        )])
        self.assertIsNone(report["multi_horizon"]["precision"])
        self.assertEqual(report["multi_horizon"]["recall"], 0)
        self.assertEqual(report["multi_horizon"]["f1"], 0)

    def test_wrong_types_and_out_of_range_scores_are_rejected(self):
        with self.assertRaisesRegex(EvaluationError, "^invalid_ratio_counts$"):
            RatioCounts(True, 1)
        with self.assertRaisesRegex(EvaluationError, "^invalid_detection_counts$"):
            DetectionCounts(0, -1, 0)
        with self.assertRaisesRegex(EvaluationError, "^invalid_akd_custody_score$"):
            CaseAssessment("synthetic_1", akd_custody_score=4)
        with self.assertRaisesRegex(EvaluationError, "^invalid_unnecessary_request$"):
            CaseAssessment("synthetic_1", unnecessary_request=1)
        with self.assertRaisesRegex(EvaluationError, "^duplicate_critical_failure$"):
            CaseAssessment("synthetic_1", critical_failures=("one", "one"))

    def test_empty_duplicate_and_wrong_rubric_inputs_are_rejected(self):
        with self.assertRaisesRegex(EvaluationError, "^invalid_assessments$"):
            score_assessments([])
        case = CaseAssessment("synthetic_1")
        with self.assertRaisesRegex(EvaluationError, "^duplicate_case_id$"):
            score_assessments([case, case])
        with self.assertRaisesRegex(EvaluationError, "^unsupported_rubric_version$"):
            replace(case, rubric_version="other")


if __name__ == "__main__":
    unittest.main()
