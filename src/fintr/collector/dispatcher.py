"""Dispatch validated events to existing normalized-data parsers."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from fintr.collector.events import DecodedEvent, decode_event_frame
from fintr.collector.models import DispatchResult
from fintr.data.parsers import (
    parse_akd_snapshot,
    parse_brokerages,
    parse_custodian_diff,
    parse_custodian_snapshot,
    parse_custody_distribution,
    parse_custody_history,
    parse_custody_stats,
    parse_depth_update,
)


Parser = Callable[[Any], Any]
_SAFE_EVENT_TYPE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


PARSERS: dict[str, Parser] = {
    "depth_update": parse_depth_update,
    "akd_snapshot": parse_akd_snapshot,
    "custodian_data": parse_custodian_snapshot,
    "custodian_diff_data": parse_custodian_diff,
    "custody_distribution_data": parse_custody_distribution,
    "custody_analysis_data": parse_custody_history,
    "custody_stats_data": parse_custody_stats,
    "brokerages_data": parse_brokerages,
}


class EventDispatcher:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(__name__)

    def dispatch_decoded(self, decoded: DecodedEvent) -> DispatchResult:
        parser = PARSERS.get(decoded.event_type)
        if parser is None:
            safe_type = (
                decoded.event_type
                if _SAFE_EVENT_TYPE.fullmatch(decoded.event_type)
                else "[INVALID_EVENT_TYPE]"
            )
            self._logger.debug("Ignoring unknown event type: %s", safe_type)
            return DispatchResult(
                event_type=safe_type,
                known=False,
                normalized=None,
                wrapper_type=decoded.wrapper_type,
            )
        return DispatchResult(
            event_type=decoded.event_type,
            known=True,
            normalized=parser(decoded.event),
            wrapper_type=decoded.wrapper_type,
        )

    def dispatch_frame(self, frame: str | bytes | bytearray) -> DispatchResult:
        return self.dispatch_decoded(decode_event_frame(frame))
