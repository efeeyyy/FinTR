"""In-memory, exact-code lookup for a typed brokerage response."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from fintr.data.models import LiveBrokerageRow, LiveBrokeragesResponse


class BrokerageResolutionStatus(Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class BrokerageLabel:
    """Labels copied from one typed response; values are hidden from repr."""

    title: str = field(repr=False)
    short_title: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class BrokerageResolution:
    status: BrokerageResolutionStatus
    label: BrokerageLabel | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class LiveBrokerageIndex:
    """Immutable in-memory index; it carries no date or provider-wide guarantee."""

    _by_code: Mapping[str, tuple[BrokerageLabel, ...]] = field(
        repr=False, compare=False, hash=False,
    )

    def __post_init__(self) -> None:
        protected: dict[str, tuple[BrokerageLabel, ...]] = {}
        for code, labels in self._by_code.items():
            if not isinstance(code, str) or not isinstance(labels, tuple) or any(
                not isinstance(label, BrokerageLabel) for label in labels
            ):
                raise TypeError("brokerage index entries have an invalid type")
            protected[code] = labels
        object.__setattr__(self, "_by_code", MappingProxyType(protected))

    @classmethod
    def from_response(cls, response: LiveBrokeragesResponse) -> LiveBrokerageIndex:
        """Build from a validated response; conflicting label pairs stay ambiguous."""

        if not isinstance(response, LiveBrokeragesResponse):
            raise TypeError("response must be LiveBrokeragesResponse")

        grouped: dict[str, dict[tuple[str, str], BrokerageLabel]] = {}
        for row in response.rows:
            if not isinstance(row, LiveBrokerageRow) or not all(
                isinstance(value, str)
                for value in (row.code, row.title, row.short_title)
            ):
                raise TypeError("response contains an invalid brokerage row")
            labels = grouped.setdefault(row.code, {})
            labels.setdefault(
                (row.title, row.short_title),
                BrokerageLabel(row.title, row.short_title),
            )

        return cls({
            code: tuple(labels.values())
            for code, labels in grouped.items()
        })

    def resolve(self, code: str) -> BrokerageResolution:
        """Resolve an exact code without case folding or other normalization."""

        if not isinstance(code, str):
            raise TypeError("brokerage code must be a string")
        labels = self._by_code.get(code, ())
        if not labels:
            return BrokerageResolution(BrokerageResolutionStatus.UNRESOLVED, None)
        if len(labels) != 1:
            return BrokerageResolution(BrokerageResolutionStatus.AMBIGUOUS, None)
        return BrokerageResolution(BrokerageResolutionStatus.RESOLVED, labels[0])
