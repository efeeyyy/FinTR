"""Deterministic request builders for confirmed WebSocket protocol messages."""

from __future__ import annotations

from datetime import date
import re

from fintr.collector.models import CollectorRequest


_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{1,16}$")


def _symbol(value: str) -> str:
    normalized = value.strip().upper()
    if not _SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError("symbol must contain 1-16 ASCII letters or digits")
    return normalized


def _date_range(start: date, end: date) -> tuple[str, str]:
    if start > end:
        raise ValueError("start date must not be after end date")
    return start.isoformat(), end.isoformat()


def build_akd_request(symbol: str, start: date, end: date) -> CollectorRequest:
    start_text, end_text = _date_range(start, end)
    return CollectorRequest(
        "get_akd",
        {"company": _symbol(symbol), "start": start_text, "end": end_text},
    )


def build_custodian_request(symbol: str, target_date: date) -> CollectorRequest:
    return CollectorRequest(
        "get_custodian",
        {"company": _symbol(symbol), "date": target_date.isoformat()},
    )


def build_custodian_diff_request(
    symbol: str,
    start: date,
    end: date,
) -> CollectorRequest:
    start_text, end_text = _date_range(start, end)
    return CollectorRequest(
        "get_custodian_diff",
        {"company": _symbol(symbol), "start": start_text, "end": end_text},
    )


def build_custody_distribution_request(symbol: str) -> CollectorRequest:
    return CollectorRequest("get_custody_distribution", {"code": _symbol(symbol)})


def build_custody_analysis_request(
    symbol: str,
    from_date: date,
    to_date: date,
) -> CollectorRequest:
    from_text, to_text = _date_range(from_date, to_date)
    return CollectorRequest(
        "custody_analysis",
        {
            "symbol": _symbol(symbol),
            "from_date": from_text,
            "to_date": to_text,
        },
    )


def build_custody_stats_request(
    symbol: str,
    *,
    history: bool = True,
) -> CollectorRequest:
    if not isinstance(history, bool):
        raise TypeError("history must be bool")
    return CollectorRequest(
        "get_custody_stats",
        {"symbol": _symbol(symbol), "history": history},
    )


def build_brokerages_request() -> CollectorRequest:
    return CollectorRequest("get_brokerages")
