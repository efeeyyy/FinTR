"""Optional brokerage labels for the two separate live custody-analysis series."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from fintr.data.brokerage_index import BrokerageResolution, LiveBrokerageIndex
from fintr.data.models import (
    LiveAnalysisAKDRow,
    LiveAnalysisCustodyRow,
    LiveCustodyAnalysisResponse,
)


@dataclass(frozen=True, slots=True)
class ResolvedAnalysisAKDRow:
    row: LiveAnalysisAKDRow = field(repr=False)
    resolution: BrokerageResolution = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResolvedAnalysisCustodyRow:
    row: LiveAnalysisCustodyRow = field(repr=False)
    resolution: BrokerageResolution = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResolvedAnalysisAKDDay:
    date: date = field(repr=False)
    rows: tuple[ResolvedAnalysisAKDRow, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResolvedAnalysisCustodyDay:
    date: date = field(repr=False)
    rows: tuple[ResolvedAnalysisCustodyRow, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResolvedCustodyAnalysisResponse:
    """No series merging, time inference, persistence, or context conversion."""

    source: LiveCustodyAnalysisResponse = field(repr=False)
    directory: LiveBrokerageIndex = field(repr=False)
    akd: tuple[ResolvedAnalysisAKDDay, ...] = field(repr=False)
    custody: tuple[ResolvedAnalysisCustodyDay, ...] = field(repr=False)


def resolve_live_custody_analysis_brokerages(
    response: LiveCustodyAnalysisResponse, index: LiveBrokerageIndex,
) -> ResolvedCustodyAnalysisResponse:
    """Label exact broker codes independently within each observed series."""

    if not isinstance(response, LiveCustodyAnalysisResponse):
        raise TypeError("response must be LiveCustodyAnalysisResponse")
    if not isinstance(index, LiveBrokerageIndex):
        raise TypeError("index must be LiveBrokerageIndex")
    return ResolvedCustodyAnalysisResponse(
        source=response,
        directory=index,
        akd=tuple(
            ResolvedAnalysisAKDDay(
                date=day.date,
                rows=tuple(
                    ResolvedAnalysisAKDRow(
                        row=row, resolution=index.resolve(row.broker_code),
                    )
                    for row in day.rows
                ),
            )
            for day in response.akd
        ),
        custody=tuple(
            ResolvedAnalysisCustodyDay(
                date=day.date,
                rows=tuple(
                    ResolvedAnalysisCustodyRow(
                        row=row, resolution=index.resolve(row.broker_code),
                    )
                    for row in day.rows
                ),
            )
            for day in response.custody
        ),
    )
