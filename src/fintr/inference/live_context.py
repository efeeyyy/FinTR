"""Bounded, in-memory LLM context from one validated live collection."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import json

from fintr.collector.evidence import LiveCollectionEvidence
from fintr.data.brokerage_index import BrokerageResolution
from fintr.data.models import LiveCustodyStatsSeries
from fintr.inference.context import ANALYSIS_CONSTRAINTS


LIVE_SCHEMA_VERSION = "live_evidence_context_v0"


@dataclass(frozen=True, slots=True)
class LiveContextLimits:
    akd_rows: int
    custodian_rows: int
    distribution_rows: int
    stats_points_per_series: int

    def __post_init__(self) -> None:
        for value in (
            self.akd_rows, self.custodian_rows,
            self.distribution_rows, self.stats_points_per_series,
        ):
            if type(value) is not int or value < 1:
                raise ValueError("invalid_live_context_limit")


@dataclass(frozen=True, slots=True)
class LiveAnalysisContext:
    """Detached JSON data; no WebSocket frame, secret, or request ID."""

    _data: dict[str, object] = field(repr=False)

    def to_dict(self) -> dict[str, object]:
        return deepcopy(self._data)

    def to_json(self) -> str:
        return json.dumps(self._data, ensure_ascii=False, separators=(",", ":"))


def _number(value: Decimal | int | bool | str | None) -> str | int | bool | None:
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _label(resolution: BrokerageResolution) -> dict[str, object]:
    label = resolution.label
    return {
        "status": resolution.status.value,
        "source": "undated_brokerages_data" if label is not None else None,
        "title": label.title if label is not None else None,
        "short_title": label.short_title if label is not None else None,
    }


def _series(series: LiveCustodyStatsSeries, limit: int) -> dict[str, object]:
    points = series.points[:limit]
    return {
        "provider_format": [
            {"key": key, "value": _number(value)} for key, value in series.format.fields
        ],
        "total_points": len(series.points),
        "included_points": len(points),
        "truncated": len(points) < len(series.points),
        "selection": "first_in_provider_order",
        "points": [
            {"date": point.date.isoformat(), "value": _number(point.value)}
            for point in points
        ],
    }


def build_live_analysis_context(
    evidence: LiveCollectionEvidence,
    *,
    generated_at: datetime,
    limits: LiveContextLimits,
) -> LiveAnalysisContext:
    """Preserve family scopes and provider values, with explicit row omissions."""
    if not isinstance(evidence, LiveCollectionEvidence):
        raise TypeError("evidence must be LiveCollectionEvidence")
    if not isinstance(generated_at, datetime) or (
        generated_at.tzinfo is None or generated_at.utcoffset() is None
    ):
        raise ValueError("generated_at_requires_timezone")
    if not isinstance(limits, LiveContextLimits):
        raise TypeError("limits must be LiveContextLimits")

    akd = evidence.akd.source
    custodian = evidence.custodian.source
    distribution = evidence.custody_distribution
    stats = evidence.custody_stats
    akd_request = akd.request_provenance
    custodian_request = custodian.request_provenance
    distribution_request = distribution.request_provenance
    stats_request = stats.request_provenance
    if any(item is None for item in (
        akd_request, custodian_request, distribution_request, stats_request,
    )):
        raise ValueError("missing_local_request_provenance")

    akd_rows = evidence.akd.rows[:limits.akd_rows]
    custodian_rows = evidence.custodian.rows[:limits.custodian_rows]
    distribution_rows = distribution.rows[:limits.distribution_rows]
    selected_distribution = []
    for row in distribution_rows:
        if len(row.values) != len(distribution.periods):
            raise ValueError("distribution_period_mismatch")
        selected_distribution.append({
            "code": row.code,
            "percentage_as_received": _number(row.percentage),
            "period_values_as_received": [
                {"date": period.isoformat(), "value": _number(value)}
                for period, value in zip(distribution.periods, row.values)
            ],
        })

    data: dict[str, object] = {
        "schema_version": LIVE_SCHEMA_VERSION,
        "symbol": evidence.symbol,
        "generated_at_local": generated_at.isoformat(),
        "row_selection": "first_in_provider_order; omitted rows are not zero",
        "brokerage_labels": {
            "source": "brokerages_data",
            "dated": False,
            "directory_rows": len(evidence.brokerages.rows),
            "used_only_for_exact_code_matches": True,
        },
        "akd": {
            "source_event": "akd_data",
            "provider_source": akd.source,
            "server_timestamp_as_received": _number(akd.timestamp),
            "server_as_of": akd.as_of,
            "local_sent_request": {
                "symbol": akd_request.symbol,
                "start": akd_request.start.isoformat(),
                "end": akd_request.end.isoformat(),
            },
            "total_rows": len(evidence.akd.rows),
            "included_rows": len(akd_rows),
            "truncated": len(akd_rows) < len(evidence.akd.rows),
            "rows": [
                {
                    "brokerage_code": item.row.brokerage,
                    "brokerage_label": _label(item.resolution),
                    "net": {
                        "size_as_received": item.row.net.size,
                        "percentage_as_received": _number(item.row.net.percentage),
                        "cost_as_received": _number(item.row.net.cost),
                    },
                    "total": {
                        "size_as_received": _number(item.row.total.size),
                        "percentage_as_received": _number(item.row.total.percentage),
                        "cost_as_received": _number(item.row.total.cost),
                        "volume_as_received": _number(item.row.total.volume),
                    },
                }
                for item in akd_rows
            ],
        },
        "custodian": {
            "source_event": "custodian_data",
            "provider_source": custodian.source,
            "server_timestamp_as_received": _number(custodian.timestamp),
            "server_date": custodian.date.isoformat(),
            "local_sent_request": {
                "symbol": custodian_request.symbol,
                "date": custodian_request.requested_date.isoformat(),
            },
            "total_rows": len(evidence.custodian.rows),
            "included_rows": len(custodian_rows),
            "truncated": len(custodian_rows) < len(evidence.custodian.rows),
            "rows": [
                {
                    "custodian_code": item.row.custodian,
                    "brokerage_label": _label(item.resolution),
                    "value_as_received": _number(item.row.value),
                    "percentage_as_received": _number(item.row.percentage),
                }
                for item in custodian_rows
            ],
        },
        "custody_distribution": {
            "source_event": "custody_distribution_data",
            "provider_source": distribution.source,
            "server_timestamp_as_received": _number(distribution.timestamp),
            "local_sent_symbol": distribution_request.symbol,
            "periods_from_server": [period.isoformat() for period in distribution.periods],
            "code_is_not_brokerage_code": True,
            "total_rows": len(distribution.rows),
            "included_rows": len(distribution_rows),
            "truncated": len(distribution_rows) < len(distribution.rows),
            "rows": selected_distribution,
        },
        "custody_stats": {
            "source_event": "custody_stats_data",
            "cached": stats.cached,
            "local_sent_request": {
                "symbol": stats_request.symbol,
                "history": stats_request.history,
            },
            "retail_percentage": _series(stats.retail_percentage, limits.stats_points_per_series),
            "institution_percentage": _series(
                stats.institution_percentage, limits.stats_points_per_series
            ),
            "investor_count": _series(stats.investor_count, limits.stats_points_per_series),
        },
        "analysis_constraints": list(ANALYSIS_CONSTRAINTS) + [
            "Local request fields are not server-echoed proof of symbol or date.",
            "Brokerage labels have no as-of date and do not identify ultimate investors.",
            "Provider percentages and units are preserved without inferred conversion.",
            "Truncated or missing rows must not be treated as zero.",
        ],
    }
    return LiveAnalysisContext(data)
