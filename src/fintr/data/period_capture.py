"""Strict loader for saved ``fintr_period_analysis_capture_v1`` files."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

from fintr.data.brokerage_index import BrokerageResolutionStatus, LiveBrokerageIndex
from fintr.data.models import LiveBrokerageRow, LiveBrokeragesResponse
from fintr.data.parsers.common import (
    MarketDataParseError,
    require_date,
    require_decimal,
    require_field,
    require_list,
    require_mapping,
    require_string,
)


CAPTURE_SCHEMA = "fintr_period_analysis_capture_v1"
PACKET_SCHEMA_V1 = "fintr_period_analysis_handoff_v1"
PACKET_SCHEMA_V2 = "fintr_period_analysis_handoff_v2"
SUPPORTED_PACKET_SCHEMAS = (PACKET_SCHEMA_V1, PACKET_SCHEMA_V2)
# Kept for external callers written against the original, single-version name.
PACKET_SCHEMA = PACKET_SCHEMA_V1


class PeriodCaptureError(ValueError):
    """A saved period capture failed structural or integrity validation."""


@dataclass(frozen=True, slots=True)
class CodeValue:
    code: str
    value: Decimal


@dataclass(frozen=True, slots=True)
class DatedCodeValues:
    date: date
    rows: tuple[CodeValue, ...]

    def by_code(self) -> dict[str, Decimal]:
        return {row.code: row.value for row in self.rows}


@dataclass(frozen=True, slots=True)
class DailyMarketProxyRow:
    """One broker's daily AKD ``total_size``/``total_cost``, unverified semantics.

    These are additional fields on the same ``daily_akd_flow_in_provider_order``
    rows that ``daily_akd_net`` reads ``net_size`` from; every code present for
    a date in one series is present in the other, since both are read from the
    same row objects.
    """

    code: str
    total_size: Decimal
    total_cost: Decimal


@dataclass(frozen=True, slots=True)
class DatedMarketProxyValues:
    date: date
    rows: tuple[DailyMarketProxyRow, ...]

    def by_code(self) -> dict[str, DailyMarketProxyRow]:
        return {row.code: row for row in self.rows}


@dataclass(frozen=True, slots=True)
class CustodianDiffRow:
    code: str
    first_value: Decimal
    last_value: Decimal
    difference: Decimal


@dataclass(frozen=True, slots=True)
class PeriodMetadataDay:
    """Provider metadata; the calculation method of these fields is unverified."""

    date: date
    retail_pct: Decimal | None
    institutional_pct: Decimal | None
    null_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BrokerageDirectoryInfo:
    """Exact-code brokerage directory carried by a v2 capture, if any.

    ``present`` is False for a v1 capture (schema predates the directory): a
    code is then reported as ``directory_not_in_capture``, never as
    unresolved, because absence of the directory is not the same claim as an
    exact-code miss against a directory that was actually captured.
    """

    present: bool
    row_count: int
    index: LiveBrokerageIndex | None


@dataclass(frozen=True, slots=True)
class PeriodCapture:
    """Validated period capture; every dated series is in ascending date order."""

    symbol: str
    start: date
    end: date
    captured_at_utc: str
    packet_sha256: str
    server_dates: tuple[date, ...]
    period_akd_net: tuple[CodeValue, ...]
    daily_akd_net: tuple[DatedCodeValues, ...]
    daily_akd_market: tuple[DatedMarketProxyValues, ...]
    daily_custody_lots: tuple[DatedCodeValues, ...]
    custodian_diff_start: date
    custodian_diff_end: date
    custodian_diff: tuple[CustodianDiffRow, ...]
    metadata: tuple[PeriodMetadataDay, ...]
    brokerage_directory: BrokerageDirectoryInfo


def canonical_json(value: Any) -> str:
    """Sorted-key compact JSON with exact Decimal tokens, as used for capture hashes."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise PeriodCaptureError("non_string_json_key")
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + canonical_json(value[key])
            for key in sorted(value)
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise PeriodCaptureError("non_finite_number")
        return str(value)
    if value is None or type(value) in (str, int, float, bool):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    raise PeriodCaptureError("unsupported_json_value")


def _reject_constant(_: str) -> None:
    raise PeriodCaptureError("non_finite_number")


def _code_values(
    raw_rows: Any, path: str, code_key: str, value_key: str,
) -> tuple[CodeValue, ...]:
    rows: list[CodeValue] = []
    seen: set[str] = set()
    for index, raw in enumerate(require_list(raw_rows, path)):
        row_path = f"{path}[{index}]"
        row = require_mapping(raw, row_path)
        code = require_string(require_field(row, code_key, row_path), f"{row_path}.{code_key}")
        if code in seen:
            raise PeriodCaptureError(f"duplicate_code: {path}")
        seen.add(code)
        rows.append(CodeValue(
            code, require_decimal(require_field(row, value_key, row_path), f"{row_path}.{value_key}"),
        ))
    return tuple(rows)


def _dated_series(
    raw_days: Any, path: str, code_key: str, value_key: str,
) -> tuple[DatedCodeValues, ...]:
    days: list[DatedCodeValues] = []
    for index, raw in enumerate(require_list(raw_days, path)):
        day_path = f"{path}[{index}]"
        day = require_mapping(raw, day_path)
        days.append(DatedCodeValues(
            require_date(require_field(day, "date", day_path), f"{day_path}.date"),
            _code_values(require_field(day, "rows", day_path), f"{day_path}.rows",
                         code_key, value_key),
        ))
    if len({day.date for day in days}) != len(days):
        raise PeriodCaptureError(f"duplicate_date: {path}")
    return tuple(sorted(days, key=lambda day: day.date))


def _dated_market_proxy_rows(
    raw_days: Any, path: str, code_key: str, size_key: str, cost_key: str,
) -> tuple[DatedMarketProxyValues, ...]:
    days: list[DatedMarketProxyValues] = []
    for index, raw in enumerate(require_list(raw_days, path)):
        day_path = f"{path}[{index}]"
        day = require_mapping(raw, day_path)
        rows: list[DailyMarketProxyRow] = []
        seen: set[str] = set()
        rows_path = f"{day_path}.rows"
        for row_index, raw_row in enumerate(require_list(require_field(day, "rows", day_path),
                                                          rows_path)):
            row_path = f"{rows_path}[{row_index}]"
            row = require_mapping(raw_row, row_path)
            code = require_string(require_field(row, code_key, row_path), f"{row_path}.{code_key}")
            if code in seen:
                raise PeriodCaptureError(f"duplicate_code: {path}")
            seen.add(code)
            rows.append(DailyMarketProxyRow(
                code,
                require_decimal(require_field(row, size_key, row_path), f"{row_path}.{size_key}"),
                require_decimal(require_field(row, cost_key, row_path), f"{row_path}.{cost_key}"),
            ))
        days.append(DatedMarketProxyValues(
            require_date(require_field(day, "date", day_path), f"{day_path}.date"),
            tuple(rows),
        ))
    if len({day.date for day in days}) != len(days):
        raise PeriodCaptureError(f"duplicate_date: {path}")
    return tuple(sorted(days, key=lambda day: day.date))


def _optional_decimal(entry: Mapping[str, Any], key: str, path: str) -> Decimal | None:
    value = entry.get(key)
    return None if value is None else require_decimal(value, f"{path}.{key}")


def _brokerage_directory(packet: Mapping[str, Any], packet_schema: str) -> BrokerageDirectoryInfo:
    """Parse the v2 brokerage directory; a v1 packet carries none at all."""
    if packet_schema != PACKET_SCHEMA_V2:
        return BrokerageDirectoryInfo(present=False, row_count=0, index=None)

    directory = require_mapping(
        require_field(packet, "brokerage_directory", "packet"), "packet.brokerage_directory",
    )
    raw_rows = require_list(
        require_field(directory, "rows", "packet.brokerage_directory"),
        "packet.brokerage_directory.rows",
    )
    row_path = "packet.brokerage_directory.rows"
    rows: list[LiveBrokerageRow] = []
    for index, raw in enumerate(raw_rows):
        entry_path = f"{row_path}[{index}]"
        entry = require_mapping(raw, entry_path)
        rows.append(LiveBrokerageRow(
            code=require_string(require_field(entry, "code", entry_path), f"{entry_path}.code"),
            title=require_string(require_field(entry, "title", entry_path), f"{entry_path}.title"),
            short_title=require_string(
                require_field(entry, "short_title", entry_path), f"{entry_path}.short_title",
            ),
        ))
    declared_row_count = require_field(directory, "row_count", "packet.brokerage_directory")
    if not isinstance(declared_row_count, int) or isinstance(declared_row_count, bool):
        raise PeriodCaptureError("invalid_capture_field: packet.brokerage_directory.row_count")
    if declared_row_count != len(rows):
        raise PeriodCaptureError("brokerage_directory_row_count_mismatch")
    index = LiveBrokerageIndex.from_response(LiveBrokeragesResponse(tuple(rows)))
    return BrokerageDirectoryInfo(present=True, row_count=len(rows), index=index)


def parse_period_capture(document: Any) -> PeriodCapture:
    """Validate a decoded capture (numbers as Decimal) and its packet hash."""
    try:
        root = require_mapping(document, "capture")
        meta = require_mapping(require_field(root, "capture_metadata", "capture"),
                               "capture.capture_metadata")
        packet = require_mapping(require_field(root, "packet", "capture"), "capture.packet")
        packet_schema = packet.get("schema")
        if (meta.get("schema") != CAPTURE_SCHEMA
                or packet_schema not in SUPPORTED_PACKET_SCHEMAS
                or meta.get("packet_schema") != packet_schema):
            raise PeriodCaptureError("unsupported_schema")
        expected_hash = require_string(
            require_field(meta, "packet_sha256_canonical_json", "capture.capture_metadata"),
            "capture.capture_metadata.packet_sha256_canonical_json",
        )
        actual_hash = hashlib.sha256(canonical_json(packet).encode("utf-8")).hexdigest()
        if actual_hash != expected_hash:
            raise PeriodCaptureError("packet_hash_mismatch")

        period = require_mapping(require_field(packet, "requested_period", "packet"),
                                 "packet.requested_period")
        akd = require_mapping(require_field(packet, "get_akd_period", "packet"),
                              "packet.get_akd_period")
        daily = require_mapping(require_field(packet, "custody_analysis_daily", "packet"),
                                "packet.custody_analysis_daily")
        diff = require_mapping(require_field(packet, "get_custodian_diff_period", "packet"),
                               "packet.get_custodian_diff_period")

        period_akd: list[CodeValue] = []
        seen_akd: set[str] = set()
        akd_path = "packet.get_akd_period.rows_in_provider_order"
        for index, raw in enumerate(require_list(require_field(
                akd, "rows_in_provider_order", "packet.get_akd_period"), akd_path)):
            row_path = f"{akd_path}[{index}]"
            row = require_mapping(raw, row_path)
            code = require_string(require_field(row, "brokerage", row_path),
                                  f"{row_path}.brokerage")
            if code in seen_akd:
                raise PeriodCaptureError(f"duplicate_code: {akd_path}")
            seen_akd.add(code)
            net = require_mapping(require_field(row, "net", row_path), f"{row_path}.net")
            period_akd.append(CodeValue(
                code, require_decimal(require_field(net, "size", f"{row_path}.net"),
                                      f"{row_path}.net.size"),
            ))

        diff_rows: list[CustodianDiffRow] = []
        seen_diff: set[str] = set()
        diff_path = "packet.get_custodian_diff_period.rows_in_provider_order"
        for index, raw in enumerate(require_list(require_field(
                diff, "rows_in_provider_order", "packet.get_custodian_diff_period"),
                diff_path)):
            row_path = f"{diff_path}[{index}]"
            row = require_mapping(raw, row_path)
            code = require_string(require_field(row, "custodian", row_path),
                                  f"{row_path}.custodian")
            if code in seen_diff:
                raise PeriodCaptureError(f"duplicate_code: {diff_path}")
            seen_diff.add(code)
            diff_rows.append(CustodianDiffRow(code, *(
                require_decimal(require_field(row, key, row_path), f"{row_path}.{key}")
                for key in ("first_value", "last_value", "difference")
            )))
        echo = require_mapping(
            require_field(require_mapping(require_field(
                diff, "provenance", "packet.get_custodian_diff_period"), "diff.provenance"),
                "server_echo", "diff.provenance"),
            "diff.provenance.server_echo",
        )

        server_echo = require_mapping(
            require_field(require_mapping(require_field(
                daily, "provenance", "packet.custody_analysis_daily"), "daily.provenance"),
                "server_echo", "daily.provenance"),
            "daily.provenance.server_echo",
        )
        server_dates = tuple(sorted(
            require_date(value, f"daily.server_echo.dates[{index}]")
            for index, value in enumerate(require_list(require_field(
                server_echo, "dates_in_provider_order", "daily.provenance.server_echo"),
                "daily.server_echo.dates_in_provider_order"))
        ))
        if len(set(server_dates)) != len(server_dates):
            raise PeriodCaptureError("duplicate_date: server_dates")

        metadata: list[PeriodMetadataDay] = []
        meta_path = "packet.custody_analysis_daily.metadata_in_provider_order"
        for index, raw in enumerate(require_list(require_field(
                daily, "metadata_in_provider_order", "packet.custody_analysis_daily"),
                meta_path)):
            entry_path = f"{meta_path}[{index}]"
            entry = require_mapping(raw, entry_path)
            null_fields = require_list(require_field(entry, "null_fields", entry_path),
                                       f"{entry_path}.null_fields")
            metadata.append(PeriodMetadataDay(
                require_date(require_field(entry, "date", entry_path), f"{entry_path}.date"),
                _optional_decimal(entry, "retail_pct", entry_path),
                _optional_decimal(entry, "institutional_pct", entry_path),
                tuple(require_string(name, f"{entry_path}.null_fields") for name in null_fields),
            ))

        return PeriodCapture(
            symbol=require_string(require_field(period, "symbol", "packet.requested_period"),
                                  "packet.requested_period.symbol"),
            start=require_date(require_field(period, "start_date", "packet.requested_period"),
                               "packet.requested_period.start_date"),
            end=require_date(require_field(period, "end_date", "packet.requested_period"),
                             "packet.requested_period.end_date"),
            captured_at_utc=require_string(
                require_field(meta, "captured_at_utc", "capture.capture_metadata"),
                "capture.capture_metadata.captured_at_utc",
            ),
            packet_sha256=actual_hash,
            server_dates=server_dates,
            period_akd_net=tuple(period_akd),
            daily_akd_net=_dated_series(
                require_field(daily, "daily_akd_flow_in_provider_order", "daily"),
                "daily.daily_akd_flow_in_provider_order", "broker_code", "net_size",
            ),
            daily_akd_market=_dated_market_proxy_rows(
                require_field(daily, "daily_akd_flow_in_provider_order", "daily"),
                "daily.daily_akd_flow_in_provider_order", "broker_code",
                "total_size", "total_cost",
            ),
            daily_custody_lots=_dated_series(
                require_field(daily, "daily_custody_balance_in_provider_order", "daily"),
                "daily.daily_custody_balance_in_provider_order", "broker_code", "lot_count",
            ),
            custodian_diff_start=require_date(
                require_field(echo, "start_date", "diff.server_echo"),
                "diff.server_echo.start_date",
            ),
            custodian_diff_end=require_date(
                require_field(echo, "end_date", "diff.server_echo"), "diff.server_echo.end_date",
            ),
            custodian_diff=tuple(diff_rows),
            metadata=tuple(sorted(metadata, key=lambda day: day.date)),
            brokerage_directory=_brokerage_directory(packet, packet_schema),
        )
    except MarketDataParseError as exc:
        raise PeriodCaptureError(
            f"invalid_capture_field: {exc.field_path or 'unknown'}"
        ) from None


DIRECTORY_NOT_IN_CAPTURE = "directory_not_in_capture"


def resolve_brokerage_label(directory: BrokerageDirectoryInfo, code: str) -> dict[str, str | None]:
    """Look up one exact code; never guesses across a v1/no-directory capture.

    Returns a plain, JSON-ready ``{"status", "title", "short_title"}`` object.
    ``status`` is one of the ``BrokerageResolutionStatus`` values, or
    ``directory_not_in_capture`` when the capture predates the directory
    (v1) so absence of a match is not conflated with an exact-code miss.
    """
    if not isinstance(directory, BrokerageDirectoryInfo):
        raise TypeError("directory must be a BrokerageDirectoryInfo")
    if not isinstance(code, str):
        raise TypeError("code must be a string")
    if directory.index is None:
        return {"status": DIRECTORY_NOT_IN_CAPTURE, "title": None, "short_title": None}
    resolution = directory.index.resolve(code)
    if resolution.status is BrokerageResolutionStatus.RESOLVED:
        return {
            "status": resolution.status.value,
            "title": resolution.label.title,
            "short_title": resolution.label.short_title,
        }
    return {"status": resolution.status.value, "title": None, "short_title": None}


def load_period_capture(path: Path) -> PeriodCapture:
    """Read one saved capture with exact Decimal numbers and validate it."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        document = json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise PeriodCaptureError("unreadable_capture") from None
    return parse_period_capture(document)
