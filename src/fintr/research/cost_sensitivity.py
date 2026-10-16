"""Pre-registered transaction-cost SENSITIVITY grid for the one-shot holdout test.

``configs/studies/prereg/cost_sensitivity_v1.json`` (``docs/prereg/
DECISIONS.md`` 2026-10-16, ``docs/prereg/TRIAL_LOG.md`` P-03) lists extra
per-side costs; the holdout test re-runs the *same frozen computation* with
only ``--cost-bps`` changed and writes the result to separate artifacts.

Non-decisional by construction (label "duyarlılık, karar girdisi değil"):
nothing here is read by ``scripts/evaluate_study_family.py``, and the
Holm-related fields of a ``hypothesis_test`` block are stripped from the
artifact. Like ``test_plan_v1.json``, the config file's sha256 must equal the
value recorded in ``docs/prereg/DECISIONS.md``; any mismatch fails closed
(``CostSensitivityError``). Nothing here computes a financial statistic: the
numbers come from ``fintr.research.report.run_study``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from fintr.research.test_plan import ALLOWED_TEST_SCOPES, TestPlan


CONFIG_SCHEMA = "fintr_cost_sensitivity_v1"
RESULT_SCHEMA = "fintr_cost_sensitivity_result_v1"
DECISION_STATUS = "non_decisional"
LABEL_TR = "duyarlılık, karar girdisi değil"
CONFIG_RELATIVE_PATH = "configs/studies/prereg/cost_sensitivity_v1.json"

# Same convention as scripts/check_env.py's test-plan record in DECISIONS.md:
# the config path in backticks, then "SHA-256 `<hex>`" on the same line.
_DECISIONS_RECORD = re.compile(
    r"`" + re.escape(CONFIG_RELATIVE_PATH) + r"`[^\n]*?SHA-256\s*`([0-9a-f]{64})`",
)
_SHA256_LEN = 64

# Holm-related fields of ``report.build_hypothesis_test``: a sensitivity run
# never carries a Holm input, so they are dropped from the artifact.
_HOLM_FIELDS = (
    "enters_holm_family", "primary_family_members", "family_size", "multiple_comparison",
    "familywise_alpha", "p_value_for_holm",
)


class CostSensitivityError(ValueError):
    """The sensitivity config is invalid, is not the one recorded in
    DECISIONS.md, or does not match the test plan / spec it is applied to."""


@dataclass(frozen=True, slots=True)
class CostSensitivityConfig:
    config_id: str
    test_plan_sha256: str
    primary_cost_bps_per_side: Decimal
    grid_cost_bps_per_side: tuple[Decimal, ...]
    scopes: tuple[str, ...]
    hypotheses: tuple[str, ...]
    config_sha256: str
    path: str


def _decimal_string(value: Any, field: str) -> Decimal:
    if not isinstance(value, str):
        raise CostSensitivityError(f"{field}: expected a decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise CostSensitivityError(f"{field}: invalid decimal string") from None
    if not number.is_finite() or number < 0:
        raise CostSensitivityError(f"{field}: must be a finite non-negative number")
    return number


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(i, str) and i for i in value):
        raise CostSensitivityError(f"{field}: expected a non-empty list of strings")
    if len(set(value)) != len(value):
        raise CostSensitivityError(f"{field}: duplicate entries")
    return tuple(value)


def parse_cost_sensitivity_config(
    document: Any, *, config_sha256: str, path: str = CONFIG_RELATIVE_PATH,
) -> CostSensitivityConfig:
    if not isinstance(document, dict) or document.get("schema") != CONFIG_SCHEMA:
        raise CostSensitivityError("unsupported_cost_sensitivity_schema")
    if document.get("decision_status") != DECISION_STATUS:
        raise CostSensitivityError("cost_sensitivity_must_be_non_decisional")
    config_id = document.get("config_id")
    if not isinstance(config_id, str) or not config_id:
        raise CostSensitivityError("invalid_config_id")
    plan_block = document.get("test_plan")
    plan_sha = plan_block.get("sha256") if isinstance(plan_block, dict) else None
    if (not isinstance(plan_sha, str) or len(plan_sha) != _SHA256_LEN
            or any(c not in "0123456789abcdef" for c in plan_sha)):
        raise CostSensitivityError("test_plan.sha256: expected a lowercase hex sha256 string")
    primary_cost = _decimal_string(document.get("primary_cost_bps_per_side"), "primary_cost_bps_per_side")
    grid_raw = document.get("grid_cost_bps_per_side")
    if not isinstance(grid_raw, list) or not grid_raw:
        raise CostSensitivityError("grid_cost_bps_per_side: expected a non-empty list")
    grid = tuple(_decimal_string(item, f"grid_cost_bps_per_side[{i}]") for i, item in enumerate(grid_raw))
    if len(set(grid)) != len(grid):
        raise CostSensitivityError("grid_cost_bps_per_side: duplicate cost")
    if primary_cost in grid:
        raise CostSensitivityError(
            "grid_cost_bps_per_side must not contain the primary cost (the primary analysis stays primary-only)"
        )
    scopes = _string_list(document.get("scopes"), "scopes")
    unknown = sorted(set(scopes) - set(ALLOWED_TEST_SCOPES))
    if unknown:
        raise CostSensitivityError(f"scopes: unknown scope(s) {','.join(unknown)}")
    hypotheses = _string_list(document.get("hypotheses"), "hypotheses")
    return CostSensitivityConfig(
        config_id=config_id, test_plan_sha256=plan_sha, primary_cost_bps_per_side=primary_cost,
        grid_cost_bps_per_side=grid, scopes=scopes, hypotheses=hypotheses,
        config_sha256=config_sha256, path=path,
    )


def load_cost_sensitivity_config(path: Path) -> CostSensitivityConfig:
    try:
        raw = Path(path).read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise CostSensitivityError("unreadable_cost_sensitivity_config") from None
    return parse_cost_sensitivity_config(
        document, config_sha256=hashlib.sha256(raw).hexdigest(), path=str(path),
    )


def recorded_config_sha256(decisions_text: str) -> str | None:
    match = _DECISIONS_RECORD.search(decisions_text)
    return None if match is None else match.group(1)


def verify_recorded_hash(config: CostSensitivityConfig, decisions_path: Path) -> None:
    """Fails closed unless DECISIONS.md records exactly this config's sha256."""
    try:
        decisions_text = Path(decisions_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise CostSensitivityError(f"unreadable_decisions_doc: {decisions_path}") from None
    recorded = recorded_config_sha256(decisions_text)
    if recorded is None:
        raise CostSensitivityError("no_recorded_cost_sensitivity_hash_in_decisions_doc")
    if recorded != config.config_sha256:
        raise CostSensitivityError(
            "cost_sensitivity_config_sha256_mismatch "
            f"(file={config.config_sha256}, DECISIONS.md={recorded})"
        )


def validate_against_plan(config: CostSensitivityConfig, plan: TestPlan) -> None:
    """The config is bound to the frozen plan it was pre-registered against."""
    if config.test_plan_sha256 != plan.plan_sha256:
        raise CostSensitivityError(
            "cost_sensitivity_config_test_plan_sha256_mismatch "
            f"(config={config.test_plan_sha256}, plan={plan.plan_sha256})"
        )
    if config.primary_cost_bps_per_side != plan.cost_bps_per_side:
        raise CostSensitivityError(
            "cost_sensitivity_config_primary_cost_mismatch "
            f"(config={config.primary_cost_bps_per_side}, plan={plan.cost_bps_per_side})"
        )


def load_verified_config(
    path: Path, decisions_path: Path, plan: TestPlan,
) -> CostSensitivityConfig:
    """Load, verify against DECISIONS.md and against the plan; raise on any problem."""
    config = load_cost_sensitivity_config(path)
    verify_recorded_hash(config, decisions_path)
    validate_against_plan(config, plan)
    return config


def require_covered(config: CostSensitivityConfig, *, hypothesis_id: str | None, test_scope: str) -> None:
    if test_scope not in config.scopes:
        raise CostSensitivityError(f"test_scope {test_scope!r} is not covered by the sensitivity config")
    if hypothesis_id is None or hypothesis_id not in config.hypotheses:
        raise CostSensitivityError(f"hypothesis {hypothesis_id!r} is not covered by the sensitivity config")


def _strip_holm(hypothesis_test: dict | None) -> dict | None:
    if hypothesis_test is None:
        return None
    return {key: value for key, value in hypothesis_test.items() if key not in _HOLM_FIELDS}


def run_payload(result: dict) -> dict:
    """The statistics fields of one re-run, as kept in the sensitivity artifact."""
    return {
        "transaction_cost_bps": result["parameters"]["transaction_cost_bps"],
        "round_trip_cost_bps": str(Decimal(result["parameters"]["transaction_cost_bps"]) * 2),
        "parameters": result["parameters"],
        "overall": result["overall"],
        "hypothesis_test": _strip_holm(result.get("hypothesis_test")),
    }


def build_artifact(
    *, config: CostSensitivityConfig, primary_result: dict, primary_payload_sha256: str,
    runs: list[dict],
) -> dict:
    """``runs`` are full ``run_study`` results, one per grid cost (grid order)."""
    return {
        "schema": RESULT_SCHEMA,
        "label": LABEL_TR,
        "decisional": False,
        "decision_status": DECISION_STATUS,
        "config": {
            "config_id": config.config_id, "path": config.path, "sha256": config.config_sha256,
            "recorded_in": "docs/prereg/DECISIONS.md 2026-10-16",
        },
        "test_plan_sha256": primary_result["test_plan_sha256"],
        "spec_id": primary_result["spec_id"],
        "spec_sha256": primary_result["spec_sha256"],
        "hypothesis_id": (primary_result.get("hypothesis_test") or {}).get("hypothesis_id"),
        "split": primary_result["split"],
        "test_scope": primary_result["test_scope"],
        "symbol_scope": primary_result["symbol_scope"],
        "symbols": primary_result["symbols"],
        "test_window_end": primary_result["test_window_end"],
        "only_changed_parameter": "transaction_cost_bps",
        "primary_cost_bps_per_side": str(config.primary_cost_bps_per_side),
        "primary_result_sha256": primary_payload_sha256,
        "grid_cost_bps_per_side": [str(cost) for cost in config.grid_cost_bps_per_side],
        "cost_basis": primary_result["parameters"]["transaction_cost_basis"],
        "runs": [run_payload(run) for run in runs],
        "reproducibility": primary_result["reproducibility"],
        "constraints": (
            "Duyarlılık, karar girdisi değil: bu artefakt yalnız tanımlayıcıdır; Holm kararına, "
            "hipotez sonucuna ve birincil rapora girmez.",
            "Her koşu birincil analizle aynı dondurulmuş hesabı yalnız işlem maliyeti (taraf "
            "başına bps) değiştirilerek yeniden çalıştırır; birincil maliyet (10 bps) bu ızgarada yoktur.",
            "Bu rapor tarihsel istatistiklerdir; yatırım tavsiyesi veya al/sat sinyali değildir.",
        ),
    }
