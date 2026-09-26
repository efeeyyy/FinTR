"""Aggregate human-annotated microstructure judgments without judging text by regex.

The model response and gold labels must be reviewed separately before creating
these counts. This module does not generate gold labels or run an LLM.
"""

from collections.abc import Iterable
from dataclasses import dataclass


RUBRIC_VERSION = "market_microstructure_v0"


class EvaluationError(ValueError):
    """An assessment cannot be used for a reproducible score report."""


def _nonnegative(value: object) -> bool:
    return type(value) is int and value >= 0


@dataclass(frozen=True, slots=True)
class RatioCounts:
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if not _nonnegative(self.numerator) or not _nonnegative(self.denominator):
            raise EvaluationError("invalid_ratio_counts")
        if self.numerator > self.denominator:
            raise EvaluationError("invalid_ratio_counts")


@dataclass(frozen=True, slots=True)
class DetectionCounts:
    true_positive: int
    false_positive: int
    false_negative: int

    def __post_init__(self) -> None:
        if not all(_nonnegative(value) for value in (
            self.true_positive, self.false_positive, self.false_negative,
        )):
            raise EvaluationError("invalid_detection_counts")


@dataclass(frozen=True, slots=True)
class CaseAssessment:
    case_id: str
    rubric_version: str = RUBRIC_VERSION
    numerical: RatioCounts | None = None
    akd_custody_score: int | None = None
    multi_horizon: DetectionCounts | None = None
    multi_horizon_reasoning_score: int | None = None
    contradiction: DetectionCounts | None = None
    missing_data: DetectionCounts | None = None
    unnecessary_request: bool | None = None
    causal_overclaim: RatioCounts | None = None
    hallucination: RatioCounts | None = None
    uncertainty_score: int | None = None
    unsupported_prediction: RatioCounts | None = None
    critical_failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id or any(
            char.isspace() for char in self.case_id
        ):
            raise EvaluationError("invalid_case_id")
        if self.rubric_version != RUBRIC_VERSION:
            raise EvaluationError("unsupported_rubric_version")
        for field in ("akd_custody_score", "multi_horizon_reasoning_score", "uncertainty_score"):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or not 0 <= value <= 3):
                raise EvaluationError(f"invalid_{field}")
        for field in ("numerical", "causal_overclaim", "hallucination", "unsupported_prediction"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, RatioCounts):
                raise EvaluationError(f"invalid_{field}")
        for field in ("multi_horizon", "contradiction", "missing_data"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, DetectionCounts):
                raise EvaluationError(f"invalid_{field}")
        if self.unnecessary_request is not None and type(self.unnecessary_request) is not bool:
            raise EvaluationError("invalid_unnecessary_request")
        if not isinstance(self.critical_failures, tuple) or any(
            not isinstance(code, str) or not code or any(char.isspace() for char in code)
            for code in self.critical_failures
        ):
            raise EvaluationError("invalid_critical_failures")
        if len(set(self.critical_failures)) != len(self.critical_failures):
            raise EvaluationError("duplicate_critical_failure")


def _ratio(items: tuple[RatioCounts | None, ...]) -> dict[str, int | float | None]:
    numerator = sum(item.numerator for item in items if item is not None)
    denominator = sum(item.denominator for item in items if item is not None)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def _detection(items: tuple[DetectionCounts | None, ...]) -> dict[str, int | float | None]:
    tp = sum(item.true_positive for item in items if item is not None)
    fp = sum(item.false_positive for item in items if item is not None)
    fn = sum(item.false_negative for item in items if item is not None)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    if tp + fp + fn == 0:
        f1 = None
    elif tp == 0:
        f1 = 0.0
    else:
        f1 = 2 * tp / (2 * tp + fp + fn)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall,
            "f1": f1}


def _ordinal(items: tuple[int | None, ...]) -> dict[str, int | float | None]:
    scores = tuple(value for value in items if value is not None)
    return {"assessed": len(scores), "mean": sum(scores) / len(scores) if scores else None}


def score_assessments(assessments: Iterable[CaseAssessment]) -> dict[str, object]:
    """Return aggregate metrics; N/A denominators remain ``None``.

    Each supplied assessment must have been independently annotated against a
    frozen gold case. Critical failures are reported separately from averages.
    """
    cases = tuple(assessments)
    if not cases or any(not isinstance(case, CaseAssessment) for case in cases):
        raise EvaluationError("invalid_assessments")
    if len({case.case_id for case in cases}) != len(cases):
        raise EvaluationError("duplicate_case_id")

    unnecessary = tuple(
        RatioCounts(int(case.unnecessary_request), 1)
        for case in cases if case.unnecessary_request is not None
    )
    assessed_hallucination = tuple(
        case for case in cases
        if case.hallucination is not None and case.hallucination.denominator > 0
    )
    case_hallucination = tuple(
        RatioCounts(int(case.hallucination.numerator > 0), 1)
        for case in assessed_hallucination
    )
    return {
        "rubric_version": RUBRIC_VERSION,
        "case_count": len(cases),
        "numerical_faithfulness": _ratio(tuple(case.numerical for case in cases)),
        "akd_custody": _ordinal(tuple(case.akd_custody_score for case in cases)),
        "multi_horizon": _detection(tuple(case.multi_horizon for case in cases)),
        "multi_horizon_reasoning": _ordinal(tuple(
            case.multi_horizon_reasoning_score for case in cases
        )),
        "contradiction": _detection(tuple(case.contradiction for case in cases)),
        "missing_data": _detection(tuple(case.missing_data for case in cases)),
        "unnecessary_request_rate": _ratio(unnecessary),
        "causal_overclaim_rate": _ratio(tuple(case.causal_overclaim for case in cases)),
        "hallucination_rate": _ratio(tuple(case.hallucination for case in cases)),
        "case_hallucination_rate": _ratio(case_hallucination),
        "uncertainty_calibration": _ordinal(tuple(case.uncertainty_score for case in cases)),
        "unsupported_price_prediction_rate": _ratio(tuple(
            case.unsupported_prediction for case in cases
        )),
        "critical_failures": [
            {"case_id": case.case_id, "codes": list(case.critical_failures)}
            for case in cases if case.critical_failures
        ],
    }
