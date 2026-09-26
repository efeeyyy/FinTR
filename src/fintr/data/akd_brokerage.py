"""Attach one live brokerage directory to one live AKD response in memory."""

from __future__ import annotations

from dataclasses import dataclass, field

from fintr.data.brokerage_index import BrokerageResolution, LiveBrokerageIndex
from fintr.data.models import LiveAKDResponse, LiveAKDRow


@dataclass(frozen=True, slots=True)
class ResolvedAKDRow:
    """Original AKD numbers plus an exact-code label lookup result."""

    row: LiveAKDRow = field(repr=False)
    resolution: BrokerageResolution = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResolvedAKDResponse:
    """A read-only view; neither input is changed or persisted."""

    source: LiveAKDResponse = field(repr=False)
    directory: LiveBrokerageIndex = field(repr=False)
    rows: tuple[ResolvedAKDRow, ...] = field(repr=False)


def resolve_live_akd_brokerages(
    response: LiveAKDResponse, index: LiveBrokerageIndex,
) -> ResolvedAKDResponse:
    """Join AKD broker codes to labels from the supplied directory only.

    A label is not evidence of a provider-wide, dated, or licensed mapping.
    Unknown and conflicting codes retain their explicit resolution status.
    """

    if not isinstance(response, LiveAKDResponse):
        raise TypeError("response must be LiveAKDResponse")
    if not isinstance(index, LiveBrokerageIndex):
        raise TypeError("index must be LiveBrokerageIndex")
    return ResolvedAKDResponse(
        source=response,
        directory=index,
        rows=tuple(
            ResolvedAKDRow(row=row, resolution=index.resolve(row.brokerage))
            for row in response.results
        ),
    )
