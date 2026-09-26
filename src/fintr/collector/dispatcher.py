"""Dispatch validated events to existing normalized-data parsers."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
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
from fintr.data.parsers.akd import parse_live_akd_data
from fintr.data.parsers.custodian_live import parse_live_custodian_data
from fintr.data.parsers.custodian_diff_live import parse_live_custodian_diff
from fintr.data.parsers.custody_analysis_live import parse_live_custody_analysis
from fintr.data.parsers.custody_distribution_live import parse_live_custody_distribution
from fintr.data.parsers.custody_stats_live import parse_live_custody_stats
from fintr.data.parsers.brokerages_live import parse_live_brokerages_data


Parser = Callable[[Any], Any]
_SAFE_EVENT_TYPE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


PARSERS: dict[str, Parser] = {
    "depth_update": parse_depth_update,
    "akd_snapshot": parse_akd_snapshot,
    "custody_distribution_data": parse_custody_distribution,
    "custody_stats_data": parse_custody_stats,
    "brokerages_data": parse_brokerages,
}


class EventDispatcher:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(__name__)

    def dispatch_decoded(self, decoded: DecodedEvent) -> DispatchResult:
        if decoded.event_type == "akd_data":
            normalized = parse_live_akd_data(decoded.event)
            return DispatchResult(
                event_type="akd_data",
                known=True,
                normalized=normalized,
                wrapper_type=decoded.wrapper_type,
                normalization_pending=True,
            )
        if decoded.event_type == "custodian_data":
            event = decoded.event
            payload = event.get("data")
            legacy_flat = (
                "symbol" in event
                and "date" in event
                and isinstance(payload, Mapping)
                and "results" in payload
                and not any(
                    key in payload for key in
                    ("request_id", "processing_time_ms", "timestamp", "data")
                )
            )
            return DispatchResult(
                event_type="custodian_data",
                known=True,
                normalized=(
                    parse_custodian_snapshot(event) if legacy_flat
                    else parse_live_custodian_data(event)
                ),
                wrapper_type=decoded.wrapper_type,
                normalization_pending=not legacy_flat,
            )
        if decoded.event_type == "custodian_diff_data":
            event = decoded.event
            payload = event.get("data")
            legacy_flat = (
                "symbol" in event
                and "first_date" in event
                and "last_date" in event
                and isinstance(payload, Mapping)
                and "results" in payload
                and not any(
                    key in payload for key in
                    ("request_id", "processing_time_ms", "timestamp", "data")
                )
            )
            return DispatchResult(
                event_type="custodian_diff_data",
                known=True,
                normalized=(
                    parse_custodian_diff(event) if legacy_flat
                    else parse_live_custodian_diff(event)
                ),
                wrapper_type=decoded.wrapper_type,
                normalization_pending=not legacy_flat,
            )
        if decoded.event_type == "custody_analysis_data":
            event = decoded.event
            payload = event.get("data")
            legacy_flat = (
                isinstance(payload, Mapping)
                and all(key in payload for key in ("symbol", "dates", "custody"))
                and not any(key in payload for key in (
                    "request_id", "processing_time_ms", "timestamp", "data",
                    "akd", "data_range", "metadata",
                ))
            )
            return DispatchResult(
                event_type="custody_analysis_data",
                known=True,
                normalized=(
                    parse_custody_history(event) if legacy_flat
                    else parse_live_custody_analysis(event)
                ),
                wrapper_type=decoded.wrapper_type,
                normalization_pending=not legacy_flat,
            )
        if decoded.event_type == "custody_distribution_data":
            event = decoded.event
            envelope = event.get("data")
            legacy_flat = (
                isinstance(envelope, Mapping)
                and ("symbol" in event or "symbol" in envelope)
                and isinstance(envelope.get("periods"), list)
                and isinstance(envelope.get("data"), list)
                and not any(key in envelope for key in (
                    "request_id", "timestamp", "processing_time_ms",
                ))
            )
            return DispatchResult(
                event_type="custody_distribution_data",
                known=True,
                normalized=(
                    parse_custody_distribution(event) if legacy_flat
                    else parse_live_custody_distribution(event)
                ),
                wrapper_type=decoded.wrapper_type,
                normalization_pending=not legacy_flat,
            )
        if decoded.event_type == "custody_stats_data":
            event = decoded.event
            envelope = event.get("data")
            legacy_flat = (
                isinstance(envelope, Mapping)
                and ("symbol" in event or "symbol" in envelope)
                and all(key in envelope for key in (
                    "retail_percentage", "institution_percentage", "investor_count",
                ))
                and all(
                    isinstance(envelope.get(key), Mapping)
                    and "format" not in envelope[key]
                    for key in (
                        "retail_percentage", "institution_percentage", "investor_count",
                    )
                )
                and not any(key in envelope for key in (
                    "request_id", "cached", "data", "format",
                ))
            )
            return DispatchResult(
                event_type="custody_stats_data",
                known=True,
                normalized=(
                    parse_custody_stats(event) if legacy_flat
                    else parse_live_custody_stats(event)
                ),
                wrapper_type=decoded.wrapper_type,
                normalization_pending=not legacy_flat,
            )
        if decoded.event_type == "brokerages_data":
            event = decoded.event
            payload = event.get("data")
            legacy_flat = (
                isinstance(payload, Mapping)
                and isinstance(payload.get("results"), list)
                and not isinstance(payload.get("data"), str)
                and not any(key in payload for key in (
                    "request_id", "timestamp", "processing_time_ms",
                ))
            )
            return DispatchResult(
                event_type="brokerages_data",
                known=True,
                normalized=(
                    parse_brokerages(event) if legacy_flat
                    else parse_live_brokerages_data(event)
                ),
                wrapper_type=decoded.wrapper_type,
            )
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
