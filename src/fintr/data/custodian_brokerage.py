"""Optional, in-memory brokerage labels for live custody rows."""

from __future__ import annotations

from dataclasses import dataclass, field

from fintr.data.brokerage_index import BrokerageResolution, LiveBrokerageIndex
from fintr.data.models import (
    LiveCustodianDiffResponse,
    LiveCustodianDiffRow,
    LiveCustodianResponse,
    LiveCustodianRow,
)


@dataclass(frozen=True, slots=True)
class ResolvedCustodianRow:
    row: LiveCustodianRow = field(repr=False)
    resolution: BrokerageResolution = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResolvedCustodianResponse:
    source: LiveCustodianResponse = field(repr=False)
    directory: LiveBrokerageIndex = field(repr=False)
    rows: tuple[ResolvedCustodianRow, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResolvedCustodianDiffRow:
    row: LiveCustodianDiffRow = field(repr=False)
    resolution: BrokerageResolution = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResolvedCustodianDiffResponse:
    source: LiveCustodianDiffResponse = field(repr=False)
    directory: LiveBrokerageIndex = field(repr=False)
    rows: tuple[ResolvedCustodianDiffRow, ...] = field(repr=False)


def resolve_live_custodian_brokerages(
    response: LiveCustodianResponse, index: LiveBrokerageIndex,
) -> ResolvedCustodianResponse:
    """Associate daily custody codes without changing values or dates."""

    if not isinstance(response, LiveCustodianResponse):
        raise TypeError("response must be LiveCustodianResponse")
    if not isinstance(index, LiveBrokerageIndex):
        raise TypeError("index must be LiveBrokerageIndex")
    return ResolvedCustodianResponse(
        source=response,
        directory=index,
        rows=tuple(
            ResolvedCustodianRow(row=row, resolution=index.resolve(row.custodian))
            for row in response.results
        ),
    )


def resolve_live_custodian_diff_brokerages(
    response: LiveCustodianDiffResponse, index: LiveBrokerageIndex,
) -> ResolvedCustodianDiffResponse:
    """Associate custody-difference codes without changing values or range."""

    if not isinstance(response, LiveCustodianDiffResponse):
        raise TypeError("response must be LiveCustodianDiffResponse")
    if not isinstance(index, LiveBrokerageIndex):
        raise TypeError("index must be LiveBrokerageIndex")
    return ResolvedCustodianDiffResponse(
        source=response,
        directory=index,
        rows=tuple(
            ResolvedCustodianDiffRow(
                row=row, resolution=index.resolve(row.custodian),
            )
            for row in response.results
        ),
    )
