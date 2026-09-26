"""Explicit, development-only persistence for the bounded AKFIS pilot context."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from fintr.collector.evidence import LiveCollectionEvidence
from fintr.collector.models import CollectorRequest
from fintr.data.models import (
    SentAKDRequestProvenance,
    SentCustodianRequestProvenance,
    SentCustodyDistributionRequestProvenance,
    SentCustodyStatsRequestProvenance,
)
from fintr.inference.live_context import (
    LIVE_SCHEMA_VERSION,
    LiveAnalysisContext,
    LiveContextLimits,
    build_live_analysis_context,
)
from fintr.inference.prompts import build_live_analysis_prompt


PILOT_SYMBOL = "AKFIS"
PILOT_DATE = date(2026, 9, 23)
EVENT_FAMILIES = (
    "brokerages_data",
    "akd_data",
    "custodian_data",
    "custody_distribution_data",
    "custody_stats_data",
)
DATA_PILOT_DIR = Path(__file__).resolve().parents[3] / "data" / "interim" / "pilot"
PILOT_CAPTURE_FILENAME = "live_context_AKFIS_2026-09-23.json"
PERMISSION_ASSERTED_ON = "2026-09-24"

_EXPECTED_REQUESTS = (
    ("get_brokerages", None),
    ("get_akd", {"company": PILOT_SYMBOL, "start": PILOT_DATE.isoformat(), "end": PILOT_DATE.isoformat()}),
    ("get_custodian", {"company": PILOT_SYMBOL, "date": PILOT_DATE.isoformat()}),
    ("get_custody_distribution", {"code": PILOT_SYMBOL}),
    ("get_custody_stats", {"symbol": PILOT_SYMBOL, "history": True}),
)
_SENSITIVE_KEY_PARTS = frozenset({
    "initdata", "url", "query", "cookie", "requestid", "authorization",
    "accesstoken", "refreshtoken", "rawmodeloutput", "modeloutput",
})
_SENSITIVE_TEXT = re.compile(
    r"(?:https?://|wss?://|\binitdata\b|\bset-cookie\b|"
    r"\brequest[_ -]?id\b|"
    r"(?:^|[;\s])(?:cookie|session(?:id)?|sid|authorization|auth|"
    r"access[_ -]?token|refresh[_ -]?token)\s*[:=]|"
    r"(?:^|[?&])(?:query_id|auth_date|hash)=)",
    re.IGNORECASE,
)


class LivePilotCaptureError(ValueError):
    """A fixed, payload-free reason why the pilot capture was rejected."""


def _reject_sensitive_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise LivePilotCaptureError("invalid_context_shape")
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized in _SENSITIVE_KEY_PARTS:
                raise LivePilotCaptureError("sensitive_context_field")
            _reject_sensitive_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_fields(nested)
    elif isinstance(value, str) and _SENSITIVE_TEXT.search(value):
        raise LivePilotCaptureError("sensitive_context_value")
    elif value is not None and type(value) not in (str, int, float, bool):
        raise LivePilotCaptureError("invalid_context_shape")


def validate_context_contains_no_secrets(value: object) -> None:
    """Reject sensitive keys or values before context is persisted or exported."""
    _reject_sensitive_fields(value)


def _validate_requests(requests: Sequence[CollectorRequest]) -> None:
    if not isinstance(requests, (tuple, list)) or len(requests) != len(_EXPECTED_REQUESTS):
        raise LivePilotCaptureError("pilot_scope_mismatch")
    for request, (request_type, data) in zip(requests, _EXPECTED_REQUESTS):
        if (
            not isinstance(request, CollectorRequest)
            or request.request_type != request_type
            or request.data != data
        ):
            raise LivePilotCaptureError("pilot_scope_mismatch")


def _validate_evidence(evidence: LiveCollectionEvidence) -> None:
    if not isinstance(evidence, LiveCollectionEvidence) or evidence.symbol != PILOT_SYMBOL:
        raise LivePilotCaptureError("pilot_scope_mismatch")
    akd = evidence.akd.source.request_provenance
    custodian = evidence.custodian.source.request_provenance
    distribution = evidence.custody_distribution.request_provenance
    stats = evidence.custody_stats.request_provenance
    if not (
        isinstance(akd, SentAKDRequestProvenance)
        and akd.symbol == PILOT_SYMBOL
        and akd.start == PILOT_DATE
        and akd.end == PILOT_DATE
        and isinstance(custodian, SentCustodianRequestProvenance)
        and custodian.symbol == PILOT_SYMBOL
        and custodian.requested_date == PILOT_DATE
        and evidence.custodian.source.date == PILOT_DATE
        and isinstance(distribution, SentCustodyDistributionRequestProvenance)
        and distribution.symbol == PILOT_SYMBOL
        and isinstance(stats, SentCustodyStatsRequestProvenance)
        and stats.symbol == PILOT_SYMBOL
        and stats.history is True
    ):
        raise LivePilotCaptureError("pilot_scope_mismatch")


def _validated_context_data(
    evidence: LiveCollectionEvidence,
    context: LiveAnalysisContext,
    limits: LiveContextLimits,
) -> dict[str, object]:
    if not isinstance(context, LiveAnalysisContext) or not isinstance(limits, LiveContextLimits):
        raise LivePilotCaptureError("invalid_context")
    data = context.to_dict()
    if not isinstance(data, dict):
        raise LivePilotCaptureError("invalid_context")
    try:
        generated_at_value = data.get("generated_at_local")
        if not isinstance(generated_at_value, str):
            raise ValueError
        generated_at = datetime.fromisoformat(generated_at_value)
        rebuilt = build_live_analysis_context(
            evidence, generated_at=generated_at, limits=limits,
        ).to_dict()
        build_live_analysis_prompt(context)
    except (TypeError, ValueError):
        raise LivePilotCaptureError("invalid_context") from None
    if data != rebuilt or data.get("schema_version") != LIVE_SCHEMA_VERSION:
        raise LivePilotCaptureError("invalid_context")
    if data.get("symbol") != PILOT_SYMBOL:
        raise LivePilotCaptureError("pilot_scope_mismatch")
    validate_context_contains_no_secrets(data)
    return data


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _capture_artifact(
    evidence: LiveCollectionEvidence,
    context: LiveAnalysisContext,
    limits: LiveContextLimits,
    *,
    captured_at: datetime,
) -> dict[str, Any]:
    if not isinstance(captured_at, datetime) or (
        captured_at.tzinfo is None or captured_at.utcoffset() is None
    ):
        raise LivePilotCaptureError("capture_time_requires_timezone")
    context_data = _validated_context_data(evidence, context, limits)
    context_bytes = _canonical_json(context_data)
    return {
        "artifact_schema_version": 2,
        "artifact_id": "live_context_AKFIS_2026-09-23",
        "source": {
            "source_id": "telegram_mini_app_live_collector",
            "capture_kind": "normalized_typed_pilot_context",
            "event_families": list(EVENT_FAMILIES),
        },
        "captured_at": captured_at.isoformat(),
        "permission_provenance": {
            "basis": "user_asserted_local_storage_approval",
            "asserted_on": PERMISSION_ASSERTED_ON,
            "independent_license_verification": False,
        },
        "context_schema_version": LIVE_SCHEMA_VERSION,
        "context_limits": {
            "akd_rows": limits.akd_rows,
            "custodian_rows": limits.custodian_rows,
            "distribution_rows": limits.distribution_rows,
            "stats_points_per_series": limits.stats_points_per_series,
        },
        "context_sha256": hashlib.sha256(context_bytes).hexdigest(),
        "transform_lineage": [
            "normalized_typed_responses_to_live_collection_evidence:v1",
            f"build_live_analysis_context:{LIVE_SCHEMA_VERSION}",
            "canonical_json_sha256:v1",
        ],
        "classification": "pilot/development",
        "split": "pilot",
        "heldout_eligible": False,
        "training_allowed": False,
        "evaluation_allowed": False,
        "gold_label_included": False,
        "retention": "manual_local_management",
        "context": context_data,
    }


def capture_live_pilot_context(
    evidence: LiveCollectionEvidence,
    context: LiveAnalysisContext,
    requests: Sequence[CollectorRequest],
    limits: LiveContextLimits,
    *,
    captured_at: datetime | None = None,
) -> Path:
    """Write one validated AKFIS development pilot, refusing scope drift and overwrite."""
    _validate_requests(requests)
    _validate_evidence(evidence)
    artifact = _capture_artifact(
        evidence,
        context,
        limits,
        captured_at=captured_at or datetime.now().astimezone(),
    )
    payload = (json.dumps(artifact, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    directory = DATA_PILOT_DIR
    if (
        directory.parent.parent.is_symlink()
        or directory.parent.is_symlink()
        or not directory.parent.is_dir()
        or directory.is_symlink()
    ):
        raise LivePilotCaptureError("pilot_capture_directory_unavailable")
    try:
        directory.mkdir(exist_ok=True)
    except OSError:
        raise LivePilotCaptureError("pilot_capture_directory_unavailable") from None
    if directory.is_symlink() or not directory.is_dir():
        raise LivePilotCaptureError("pilot_capture_directory_unavailable")
    target = directory / PILOT_CAPTURE_FILENAME
    if target.is_symlink():
        raise LivePilotCaptureError("capture_already_exists")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".live-context-", suffix=".tmp",
            dir=directory, delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary_path, target)
    except FileExistsError:
        raise LivePilotCaptureError("capture_already_exists") from None
    except OSError:
        raise LivePilotCaptureError("capture_write_failed") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return target
