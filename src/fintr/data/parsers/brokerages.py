"""Parser for brokerage code/name directories."""

from __future__ import annotations

from typing import Any

from fintr.data.models import BrokerInfo, BrokerageDirectory
from fintr.data.parsers.common import (
    header_field,
    require_date,
    require_event,
    require_field,
    require_list,
    require_mapping,
    require_payload,
    require_string,
)


def parse_brokerages(event: Any) -> BrokerageDirectory:
    root = require_event(event, {"get_brokerages", "brokerages_data"})
    payload = require_payload(root)
    results = require_list(require_field(payload, "results", "event.data"), "event.data.results")
    brokers: list[BrokerInfo] = []
    for index, value in enumerate(results):
        path = f"event.data.results[{index}]"
        row = require_mapping(value, path)
        brokers.append(
            BrokerInfo(
                code=require_string(require_field(row, "code", path), f"{path}.code"),
                name=require_string(require_field(row, "name", path), f"{path}.name"),
            )
        )
    return BrokerageDirectory(
        as_of=require_date(header_field(root, payload, "date"), "event.date"),
        brokers=tuple(brokers),
    )
