"""Holm (1979) step-down decision over a pre-registered primary family.

The family is declared in each spec's ``preregistration`` block
(``primary_family_members``, ``familywise_alpha``); this module only reads
study result dicts built by ``fintr.research.report.run_study`` (their
``hypothesis_test`` block) and never recomputes a return statistic. A
``secondary_descriptive`` hypothesis is listed but never enters the Holm
family, so its p value can neither be rejected nor change ``m``.

Nothing here is a buy/sell recommendation; outcome classes describe only
whether a historical-difference hypothesis was supported by the
pre-registered test.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any, Mapping, Sequence


FAMILY_SCHEMA = "fintr_study_family_result_v1"

FORMULAS: dict[str, str] = {
    "holm": (
        "sort the m primary p values ascending p(1)..p(m); reject H(i) while "
        "p(i) <= alpha / (m - i + 1), stop at the first failure (every later "
        "hypothesis is not rejected). Ties keep primary_family_members order. "
        "Comparison is exact (fractions.Fraction; a float p is read through "
        "its shortest repr)."
    ),
    "p_value_for_holm": (
        "the E1 one-sided p of a primary member, or 1 when its minimum-event "
        "rule failed (fintr.research.report.build_hypothesis_test); m never "
        "shrinks"
    ),
    "outcome": (
        "inconclusive_insufficient_events if the minimum-event rule failed; "
        "supported if Holm rejects; falsified if Holm does not reject and the "
        "one-sided E1 bound excludes the SESOI in the predicted direction; "
        "otherwise inconclusive. Confirmatory only for split == 'test'."
    ),
}


class FamilyError(ValueError):
    """Study results do not form one consistent pre-registered family."""


def _exact(value: Any) -> Fraction:
    if isinstance(value, bool):
        raise FamilyError("invalid_p_value")
    if isinstance(value, float):
        return Fraction(repr(value))
    if isinstance(value, (int, Decimal, Fraction)):
        return Fraction(value)
    if isinstance(value, str):
        return Fraction(value)
    raise FamilyError("invalid_p_value")


def holm_decisions(
    p_values: Mapping[str, Any], alpha: Decimal | str, order: Sequence[str] | None = None,
) -> dict[str, dict]:
    """``{id: {"p", "rank", "threshold", "rejected"}}`` for Holm at ``alpha``.

    ``order`` fixes tie-breaking (default: mapping order). ``m`` is
    ``len(p_values)``.
    """
    ids = list(order) if order is not None else list(p_values)
    if sorted(ids) != sorted(p_values) or len(set(ids)) != len(ids):
        raise FamilyError("holm_order_mismatch")
    if not ids:
        raise FamilyError("empty_family")
    alpha_exact = _exact(alpha)
    if not (0 < alpha_exact < 1):
        raise FamilyError("invalid_alpha")
    exact = {hid: _exact(p_values[hid]) for hid in ids}
    if any(not (0 <= p <= 1) for p in exact.values()):
        raise FamilyError("p_value_out_of_range")
    m = len(ids)
    ranked = sorted(ids, key=lambda hid: (exact[hid], ids.index(hid)))
    decisions: dict[str, dict] = {}
    still_rejecting = True
    for rank, hid in enumerate(ranked, start=1):
        threshold = alpha_exact / (m - rank + 1)
        rejected = still_rejecting and exact[hid] <= threshold
        still_rejecting = rejected
        decisions[hid] = {
            "p": p_values[hid],
            "rank": rank,
            "threshold": str(Decimal(threshold.numerator) / Decimal(threshold.denominator)),
            "rejected": rejected,
        }
    return {hid: decisions[hid] for hid in ids}


def _outcome(test: Mapping[str, Any], rejected: bool) -> str:
    if not test["min_events_met"]:
        return "inconclusive_insufficient_events"
    if rejected:
        return "supported"
    if test["sesoi_excluded_in_predicted_direction"] is True:
        return "falsified"
    return "inconclusive"


_FAMILY_KEYS = ("family_id", "primary_family_members", "familywise_alpha", "multiple_comparison")


def evaluate_primary_family(results: Sequence[Mapping[str, Any]]) -> dict:
    """Holm over the primary members of one family, from saved study results.

    Refuses (``FamilyError``) unless: every result carries a
    ``hypothesis_test`` block; all share split, family definition and run
    parameters; every declared primary member appears exactly once. A
    missing primary member is an error, never a silently smaller ``m``.
    """
    if not results:
        raise FamilyError("no_results")
    tests = []
    for result in results:
        test = result.get("hypothesis_test")
        if not isinstance(test, Mapping):
            raise FamilyError(f"result_without_hypothesis_test: {result.get('spec_id')}")
        tests.append(test)
    first = tests[0]
    for key in _FAMILY_KEYS:
        if any(test[key] != first[key] for test in tests):
            raise FamilyError(f"inconsistent_family_definition: {key}")
    for key in ("split", "parameters"):
        if any(result.get(key) != results[0].get(key) for result in results):
            raise FamilyError(f"inconsistent_{key}")
    if first["multiple_comparison"] != "holm":
        raise FamilyError("unsupported_multiple_comparison")

    members = list(first["primary_family_members"])
    ids = [test["hypothesis_id"] for test in tests]
    duplicates = sorted({hid for hid in ids if ids.count(hid) > 1})
    if duplicates:
        raise FamilyError(f"duplicate_hypothesis: {','.join(duplicates)}")
    by_id = dict(zip(ids, zip(results, tests)))
    missing = [hid for hid in members if hid not in by_id]
    if missing:
        raise FamilyError(f"missing_primary_family_member: {','.join(missing)}")
    for hid, (_, test) in by_id.items():
        if (test["family_role"] == "primary") != (hid in members) or test["enters_holm_family"] != (hid in members):
            raise FamilyError(f"family_role_mismatch: {hid}")
        if hid in members and test["p_value_for_holm"] is None:
            raise FamilyError(f"primary_member_without_p_value: {hid}")

    decisions = holm_decisions(
        {hid: by_id[hid][1]["p_value_for_holm"] for hid in members},
        first["familywise_alpha"], order=members,
    )
    split = results[0].get("split")
    primary = []
    for hid in members:
        result, test = by_id[hid]
        primary.append({
            "hypothesis_id": hid,
            "spec_id": result.get("spec_id"),
            "spec_sha256": result.get("spec_sha256"),
            "predicted_direction": test["predicted_direction"],
            "difference_mean": test["difference_mean"],
            "min_events_met": test["min_events_met"],
            "p_value_one_sided": test["p_value_one_sided"],
            "p_value_for_holm": test["p_value_for_holm"],
            "holm": decisions[hid],
            "one_sided_bound": test["one_sided_bound"],
            "sesoi_excluded_in_predicted_direction": test["sesoi_excluded_in_predicted_direction"],
            "outcome": _outcome(test, decisions[hid]["rejected"]),
        })
    secondary = [
        {
            "hypothesis_id": hid,
            "spec_id": result.get("spec_id"),
            "spec_sha256": result.get("spec_sha256"),
            "family_role": test["family_role"],
            "enters_holm_family": False,
            "difference_mean": test["difference_mean"],
            "min_events_met": test["min_events_met"],
            "p_value_one_sided_unadjusted_descriptive": test["p_value_one_sided"],
            "outcome": "descriptive_only",
        }
        for hid, (result, test) in by_id.items() if hid not in members
    ]
    return {
        "schema": FAMILY_SCHEMA,
        "family_id": first["family_id"],
        "split": split,
        "confirmatory": split == "test",
        "multiple_comparison": "holm",
        "familywise_alpha": first["familywise_alpha"],
        "m": len(members),
        "primary": primary,
        "secondary_descriptive": secondary,
        "formulas": dict(FORMULAS),
        "constraints": (
            "Tarihsel istatistiktir; yatırım tavsiyesi veya al/sat sinyali değildir.",
            "secondary_descriptive hipotezler Holm ailesine girmez; p değerleri "
            "düzeltilmemiş ve yalnız tanımlayıcıdır.",
            "Doğrulayıcı karar yalnız test aşamasınındır (confirmatory = split == 'test').",
        ),
    }
