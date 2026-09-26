"""Fail-closed, in-memory view of one supplied live collection result."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

from fintr.collector.models import DispatchResult
from fintr.data.akd_brokerage import ResolvedAKDResponse, resolve_live_akd_brokerages
from fintr.data.brokerage_index import LiveBrokerageIndex
from fintr.data.custodian_brokerage import (
    ResolvedCustodianResponse,
    resolve_live_custodian_brokerages,
)
from fintr.data.models import (
    LiveAKDResponse,
    LiveBrokeragesResponse,
    LiveCustodianResponse,
    LiveCustodyDistributionResponse,
    LiveCustodyStatsResponse,
    SentAKDRequestProvenance,
    SentCustodianRequestProvenance,
    SentCustodyDistributionRequestProvenance,
    SentCustodyStatsRequestProvenance,
)


_RESPONSE_TYPES = {
    "brokerages_data": LiveBrokeragesResponse,
    "akd_data": LiveAKDResponse,
    "custodian_data": LiveCustodianResponse,
    "custody_distribution_data": LiveCustodyDistributionResponse,
    "custody_stats_data": LiveCustodyStatsResponse,
}


class LiveEvidenceError(ValueError):
    """A fixed, payload-free reason why a coherent view cannot be formed."""


@dataclass(frozen=True, slots=True)
class LiveCollectionEvidence:
    """Five typed responses and optional exact-code labels, without persistence.

    Source responses retain their distinct dates, units, and local provenance.
    This is not a MarketSnapshot or an LLM-ready context.
    """

    symbol: str = field(repr=False)
    brokerages: LiveBrokeragesResponse = field(repr=False)
    brokerage_index: LiveBrokerageIndex = field(repr=False)
    akd: ResolvedAKDResponse = field(repr=False)
    custodian: ResolvedCustodianResponse = field(repr=False)
    custody_distribution: LiveCustodyDistributionResponse = field(repr=False)
    custody_stats: LiveCustodyStatsResponse = field(repr=False)


def build_live_collection_evidence(
    results: Sequence[DispatchResult],
) -> LiveCollectionEvidence:
    """Require exactly one normalized response per family from one collection.

    The caller must pass the result sequence from a single ``collect`` call;
    individual DispatchResult objects do not carry a connection identifier.
    Broker labels are undated and never applied to distribution ``code`` rows.
    """

    selected: dict[str, object] = {}
    for result in results:
        if not isinstance(result, DispatchResult):
            raise LiveEvidenceError("invalid_dispatch_result")
        expected_type = _RESPONSE_TYPES.get(result.event_type)
        if expected_type is None:
            continue
        if result.event_type in selected:
            raise LiveEvidenceError("duplicate_response")
        if (
            not result.known
            or result.normalization_pending
            or not isinstance(result.normalized, expected_type)
        ):
            raise LiveEvidenceError("invalid_response")
        selected[result.event_type] = result.normalized

    if selected.keys() != _RESPONSE_TYPES.keys():
        raise LiveEvidenceError("missing_response")

    brokerages = cast(LiveBrokeragesResponse, selected["brokerages_data"])
    akd = cast(LiveAKDResponse, selected["akd_data"])
    custodian = cast(LiveCustodianResponse, selected["custodian_data"])
    distribution = cast(LiveCustodyDistributionResponse, selected["custody_distribution_data"])
    stats = cast(LiveCustodyStatsResponse, selected["custody_stats_data"])

    if not (
        isinstance(akd.request_provenance, SentAKDRequestProvenance)
        and isinstance(custodian.request_provenance, SentCustodianRequestProvenance)
        and isinstance(distribution.request_provenance, SentCustodyDistributionRequestProvenance)
        and isinstance(stats.request_provenance, SentCustodyStatsRequestProvenance)
    ):
        raise LiveEvidenceError("missing_local_send_provenance")
    provenance = (
        akd.request_provenance,
        custodian.request_provenance,
        distribution.request_provenance,
        stats.request_provenance,
    )
    if any(item.origin != "local_sent_request" for item in provenance):
        raise LiveEvidenceError("missing_local_send_provenance")
    symbols = {item.symbol for item in provenance}
    if len(symbols) != 1 or not next(iter(symbols)):
        raise LiveEvidenceError("symbol_mismatch")
    if custodian.date != custodian.request_provenance.requested_date:
        raise LiveEvidenceError("custodian_date_mismatch")

    index = LiveBrokerageIndex.from_response(brokerages)
    return LiveCollectionEvidence(
        symbol=akd.request_provenance.symbol,
        brokerages=brokerages,
        brokerage_index=index,
        akd=resolve_live_akd_brokerages(akd, index),
        custodian=resolve_live_custodian_brokerages(custodian, index),
        custody_distribution=distribution,
        custody_stats=stats,
    )
