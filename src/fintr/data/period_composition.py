"""Schema-only dataclasses for a base+rolling period-capture composition.

See ``fintr.research.compose_period_capture`` for the pure function that
builds these from already-loaded, hash-verified ``PeriodCapture`` objects,
and ``docs/research/daily_miniapp_collection_adr.md`` §3 for the binding
rules this schema records (``fintr_period_composition_v1``).

``fintr.data.period_capture.PeriodCapture.composition`` (default ``None``)
holds one of these when the capture was produced by that composer instead
of loaded directly from a single saved file. This module holds only plain
data shapes -- no composition logic -- so that
``fintr.data.period_capture`` can depend on it without creating an import
cycle with ``fintr.research``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


COMPOSITION_SCHEMA = "fintr_period_composition_v1"
COMPOSER_VERSION = "compose_period_capture_v1"

# The four independently-selected dated series families (ADR §3 rule 1).
COMPOSITION_FAMILIES = ("akd", "akd_market", "custody", "metadata")


@dataclass(frozen=True, slots=True)
class CompositionInputRecord:
    """One base/rolling capture that was given to the composer."""

    kind: str  # "base" | "rolling"
    packet_sha256: str
    captured_at_utc: str
    label: str | None = None  # e.g. the source file path, for the report only


@dataclass(frozen=True, slots=True)
class CompositionSelectionEntry:
    """The winning input for one (date, family); rule 1."""

    date: date
    family: str
    packet_sha256: str  # the winning input's PeriodCapture.packet_sha256


@dataclass(frozen=True, slots=True)
class CompositionRevisionEntry:
    """A (date, family) seen with a different row-set hash in >1 input; rule 3."""

    date: date
    family: str
    superseded_packet_sha256: str
    winning_packet_sha256: str
    differing_code_count: int


@dataclass(frozen=True, slots=True)
class CompositionUnsettledDay:
    """A (date, family) that has not yet met the settlement rule; rule 4."""

    date: date
    family: str  # "akd" | "custody"


@dataclass(frozen=True, slots=True)
class CompositionBrokerageSource:
    """Which input's brokerage directory the composed capture carries; rule 6."""

    kind: str
    captured_at_utc: str
    packet_sha256: str


@dataclass(frozen=True, slots=True)
class CompositionLineage:
    schema: str
    symbol: str
    composer_version: str
    inputs: tuple[CompositionInputRecord, ...]
    selection: tuple[CompositionSelectionEntry, ...]
    gaps: tuple[date, ...]
    revisions: tuple[CompositionRevisionEntry, ...]
    unsettled: tuple[CompositionUnsettledDay, ...]
    brokerage_directory_source: CompositionBrokerageSource
    period_aggregates_available: bool
    composition_sha256: str
