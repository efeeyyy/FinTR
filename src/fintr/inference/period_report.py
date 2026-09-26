"""JSON-compatible deterministic report for one saved period capture."""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from typing import Any

from fintr.data.period_capture import PeriodCapture, resolve_brokerage_label
from fintr.features.period import (
    FORMULA_VERSION,
    FORMULAS,
    broker_flow_series,
    check_custodian_diff,
    daily_panel_coverage,
    lagged_akd_custody_match,
    reconcile_daily_akd,
)


REPORT_SCHEMA = "fintr_period_report_v1"

REPORT_CONSTRAINTS: tuple[str, ...] = (
    "AKD is trading flow; custody is a holding balance. They are separate data families.",
    "Broker and custodian codes do not identify ultimate investors or their intent.",
    "Lag agreement is a timing observation; it does not prove provider method or causality.",
    "A code not listed on a date is missing data, not zero.",
    "Provider metadata percentages have an unverified calculation method.",
    "No price or volume series is included; no price direction can be inferred.",
    "Brokerage/custodian labels are resolved by exact code only, from an "
    "undated directory snapshot; unresolved or ambiguous codes are never "
    "guessed, and a label never identifies an ultimate investor.",
)


def _num(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _day(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _label(capture: PeriodCapture, code: str) -> dict[str, Any]:
    return resolve_brokerage_label(capture.brokerage_directory, code)


def build_period_report(
    capture: PeriodCapture, *, lags: tuple[int, ...], detail_lag: int, top_n: int,
) -> dict[str, Any]:
    """Build the report; every number is computed here, not by a model."""
    if type(top_n) is not int or top_n < 1:
        raise ValueError("top_n must be a positive integer")

    reconciliation = reconcile_daily_akd(capture)
    diff_check = check_custodian_diff(capture)
    flows = broker_flow_series(capture)
    lag = lagged_akd_custody_match(capture, lags=lags, detail_lag=detail_lag)
    coverage = daily_panel_coverage(capture)

    period_net = sorted(capture.period_akd_net, key=lambda row: (-row.value, row.code))
    diffs = sorted(capture.custodian_diff, key=lambda row: (-row.difference, row.code))
    flow_order = sorted(flows, key=lambda item: (-abs(item.listed_sum), item.code))

    return {
        "schema": REPORT_SCHEMA,
        "formula_version": FORMULA_VERSION,
        "formulas": dict(FORMULAS),
        "scope": {
            "symbol": capture.symbol,
            "requested_start": capture.start.isoformat(),
            "requested_end": capture.end.isoformat(),
            "server_date_count": len(capture.server_dates),
            "server_dates_ascending": [day.isoformat() for day in capture.server_dates],
            "captured_at_utc": capture.captured_at_utc,
            "packet_sha256_verified": capture.packet_sha256,
            "units": "as reported by the provider; not independently verified",
        },
        "period_akd_net": {
            "source": "get_akd_period.net.size",
            "code_count": len(period_net),
            "top_positive": [
                {"code": row.code, "net": _num(row.value), "label": _label(capture, row.code)}
                for row in period_net[:top_n] if row.value > 0
            ],
            "top_negative": [
                {"code": row.code, "net": _num(row.value), "label": _label(capture, row.code)}
                for row in reversed(period_net[-top_n:]) if row.value < 0
            ],
        },
        "custodian_diff": {
            "source": "get_custodian_diff_period",
            "code_count": len(diffs),
            "server_range": [capture.custodian_diff_start.isoformat(),
                             capture.custodian_diff_end.isoformat()],
            "largest_increases": [
                {"code": row.code, "difference": _num(row.difference),
                 "first_value": _num(row.first_value), "last_value": _num(row.last_value),
                 "label": _label(capture, row.code)}
                for row in diffs[:top_n] if row.difference > 0
            ],
            "largest_decreases": [
                {"code": row.code, "difference": _num(row.difference),
                 "first_value": _num(row.first_value), "last_value": _num(row.last_value),
                 "label": _label(capture, row.code)}
                for row in reversed(diffs[-top_n:]) if row.difference < 0
            ],
        },
        "akd_reconciliation": {
            "status_counts": dict(sorted(Counter(row.status for row in reconciliation).items())),
            "non_matching": [
                {"code": row.code, "period_net": _num(row.period_net),
                 "daily_sum": _num(row.daily_sum), "days_listed": row.days_listed,
                 "status": row.status}
                for row in reconciliation if row.status != "match"
            ],
        },
        "custodian_diff_check": {
            "daily_first_date": _day(diff_check.daily_first_date),
            "daily_last_date": _day(diff_check.daily_last_date),
            "range_aligned_with_diff": diff_check.range_aligned,
            "internally_inconsistent_codes": [
                row.code for row in diff_check.rows if not row.internally_consistent
            ],
            "status_counts": {
                "first": dict(sorted(Counter(row.first_status for row in diff_check.rows).items())),
                "last": dict(sorted(Counter(row.last_status for row in diff_check.rows).items())),
            },
            "exceptions": [
                {"code": row.code,
                 "first_value": _num(row.first_value),
                 "daily_first_value": _num(row.daily_first_value),
                 "first_status": row.first_status,
                 "last_value": _num(row.last_value),
                 "daily_last_value": _num(row.daily_last_value),
                 "last_status": row.last_status,
                 "label": _label(capture, row.code)}
                for row in diff_check.rows
                if row.first_status != "match" or row.last_status != "match"
            ],
        },
        "broker_flow_runs": {
            "order": "absolute listed_sum descending, then code",
            "included_codes": min(top_n, len(flow_order)),
            "total_codes": len(flow_order),
            "codes": [
                {
                    "code": item.code,
                    "label": _label(capture, item.code),
                    "listed_sum": _num(item.listed_sum),
                    "listed_days": sum(value is not None for _, value in item.points),
                    "runs": [
                        {"start": run.start.isoformat(), "end": run.end.isoformat(),
                         "days": run.days, "direction": run.direction,
                         "net_sum": _num(run.net_sum)}
                        for run in item.runs
                    ],
                }
                for item in flow_order[:top_n]
            ],
        },
        "lagged_akd_custody_match": {
            "summaries": [
                {"lag": item.lag, "comparable": item.comparable,
                 "exact_matches": item.exact_matches, "sign_matches": item.sign_matches}
                for item in lag.summaries
            ],
            "detail_lag": lag.detail_lag,
            "unmatched_count": len(lag.unmatched),
            "unmatched": [
                {"code": item.code, "akd_date": item.akd_date.isoformat(),
                 "custody_date": item.custody_date.isoformat(),
                 "akd_net": _num(item.akd_net), "custody_change": _num(item.custody_change)}
                for item in lag.unmatched
            ],
            "akd_dates_without_later_custody": [
                day.isoformat() for day in lag.pending_akd_dates
            ],
        },
        "daily_panel_gaps": [
            {"code": item.code,
             "akd_missing_dates": [day.isoformat() for day in item.akd_missing_dates],
             "custody_missing_dates": [day.isoformat() for day in item.custody_missing_dates]}
            for item in coverage
            if item.akd_missing_dates or item.custody_missing_dates
        ],
        "provider_metadata": {
            "method_verified": False,
            "days": [
                {"date": day.date.isoformat(), "retail_pct": _num(day.retail_pct),
                 "institutional_pct": _num(day.institutional_pct),
                 "null_fields": list(day.null_fields)}
                for day in capture.metadata
            ],
        },
        "constraints": list(REPORT_CONSTRAINTS),
    }
