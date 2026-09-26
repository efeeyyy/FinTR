"""Short live collector smoke test with opt-in, bounded pilot-context capture."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import re
import tempfile
from time import monotonic

from fintr.collector import (
    CollectorConfig,
    CollectorConnectionError,
    InteractiveSecretProvider,
    WebSocketCollector,
    LiveEvidenceError,
    build_authenticated_url,
    build_live_collection_evidence,
    redact_url,
)
from fintr.collector.subscriptions import (
    build_akd_request,
    build_brokerages_request,
    build_custodian_request,
    build_custodian_diff_request,
    build_custody_analysis_request,
    build_custody_distribution_request,
    build_custody_stats_request,
)
from fintr.collector.models import AttemptDiagnostic, SendObservation
from fintr.data.models import (
    LiveAKDResponse, LiveCustodianResponse, LiveCustodianDiffResponse,
    LiveCustodyAnalysisResponse, LiveCustodyDistributionResponse,
    LiveCustodyStatsResponse,
    LiveBrokeragesResponse,
)
from fintr.inference import (
    LiveContextLimits, build_live_analysis_context, build_live_analysis_prompt,
)
from fintr.evaluation.symbol_guard import SymbolGuardError, load_active_exclusions
from fintr.evaluation.live_capture import (
    DATA_PILOT_DIR,
    LivePilotCaptureError,
    PILOT_CAPTURE_FILENAME,
    capture_live_pilot_context,
    validate_context_contains_no_secrets,
)


# Smoke acceptance contract; control frames aren't market responses.
REQUEST_RESPONSE_EVENTS: dict[str, str] = {
    "get_brokerages": "brokerages_data",
    "get_akd": "akd_data",
    "get_custodian": "custodian_data",
    "get_custody_distribution": "custody_distribution_data",
    "get_custody_stats": "custody_stats_data",
    "custody_analysis": "custody_analysis_data",
    "get_custodian_diff": "custodian_diff_data",
}
EVIDENCE_REQUEST_TYPES = frozenset({
    "get_brokerages", "get_akd", "get_custodian",
    "get_custody_distribution", "get_custody_stats",
})

SESSION_TAKEOVER_WARNING = (
    "WARNING: this WebSocket connection may close the active Telegram Mini App "
    "session because the provider allows only one active session per account/initData."
)
ANALYSIS_CONTEXT_BEGIN = "<<<FINTR_LIVE_ANALYSIS_CONTEXT_JSON_BEGIN>>>"
ANALYSIS_CONTEXT_END = "<<<FINTR_LIVE_ANALYSIS_CONTEXT_JSON_END>>>"
PERIOD_ANALYSIS_CONTEXT_BEGIN = "<<<FINTR_PERIOD_ANALYSIS_CONTEXT_JSON_BEGIN>>>"
PERIOD_ANALYSIS_CONTEXT_END = "<<<FINTR_PERIOD_ANALYSIS_CONTEXT_JSON_END>>>"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PERIOD_ANALYSIS_REQUEST_TYPES = (
    "get_brokerages", "get_akd", "custody_analysis", "get_custodian_diff",
)
_EVENT_STATUS_ALLOWLIST = frozenset({
    "welcome", "hb", "error", "banned",
    "depth_update", "akd_snapshot", "akd_data", "custodian_data",
    "custodian_diff_data", "custody_distribution_data",
    "custody_analysis_data", "custody_stats_data", "brokerages_data",
})
_UNKNOWN_EVENT_TYPE_LABEL = "[UNKNOWN_EVENT_TYPE]"
_SHOWABLE_UNKNOWN_EVENT_TYPE = re.compile(r"^[a-z][a-z_]{0,39}$")
_FUTURE_DATE_FLAGS: tuple[tuple[str, str], ...] = (
    ("--end-date", "end_date"),
    ("--to-date", "to_date"),
    ("--custody-date", "custody_date"),
)

# Multi-symbol period capture: one bounded connection per symbol, sequentially.
MULTI_SYMBOL_UNIVERSE_SCHEMA = "fintr_universe_v1"
MAX_SYMBOLS_PER_RUN = 40
_MULTI_SYMBOL_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9]{1,16}$")
_CONSECUTIVE_SESSION_FAILURES_TO_STOP = 2


def _today() -> date:
    """Local calendar date; tests override via ``patch.object(smoke, "_today", ...)``."""
    return date.today()


def _reject_future_dates(args: argparse.Namespace, parser: argparse.ArgumentParser) -> date:
    """Reject any --end-date/--to-date/--custody-date after today, before any
    secret prompt or connection (including in --dry-run); the provider has no
    data for future dates yet (a live run observed a missing
    ``akd_data``/``event.data.data.source`` failure for a future --end-date)."""
    today = _today()
    for flag, attr in _FUTURE_DATE_FLAGS:
        value = getattr(args, attr)
        if value is not None and value > today:
            parser.error(
                f"{flag} {value.isoformat()} is after today ({today.isoformat()}); "
                "the provider has no data for future dates yet (a live run "
                "previously failed with a missing akd_data event.data.data.source "
                "field for a future --end-date)"
            )
    return today


def _same_day_note(today: date, args: argparse.Namespace) -> str | None:
    """A same-day date is allowed, but its data may still be incomplete."""
    if today in (args.end_date, args.to_date, args.custody_date):
        return (
            f"Note: same-day data ({today.isoformat()}) may be incomplete or not "
            "yet available from the provider."
        )
    return None


class PeriodAnalysisHandoffError(ValueError):
    """A fixed, payload-free reason why a complete period handoff was rejected."""

    def __init__(self, reason: str, *, safe_detail: str | None = None) -> None:
        self.reason = reason
        super().__init__(reason if safe_detail is None else f"{reason}; {safe_detail}")


class PeriodAnalysisCaptureError(ValueError):
    """A fixed, payload-free reason why a period packet could not be saved."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("expected positive integer")
    return number


def _event_status_label(
    event_type: str, *, show_unknown_event_types: bool = False,
) -> str:
    if isinstance(event_type, str) and event_type in _EVENT_STATUS_ALLOWLIST:
        return event_type
    if (
        show_unknown_event_types
        and isinstance(event_type, str)
        and _SHOWABLE_UNKNOWN_EVENT_TYPE.fullmatch(event_type)
    ):
        return event_type
    return _UNKNOWN_EVENT_TYPE_LABEL


def _stats_provenance_matches_request(result, requests) -> bool:
    sent_context = getattr(result, "normalized", None)
    if not isinstance(sent_context, LiveCustodyStatsResponse):
        return False
    provenance = sent_context.request_provenance
    candidates = [request for request in requests
                  if request.request_type == "get_custody_stats"]
    if provenance is None or len(candidates) != 1:
        return False
    data = candidates[0].data
    return (
        isinstance(data, dict)
        and data.get("symbol") == provenance.symbol
        and data.get("history") is provenance.history
    )


def _matched_response_events(results, requests, expected_events: set[str]) -> set[str]:
    """Event types that are expected, known, fully normalized, and (for
    families that bind local provenance) tied to exactly one sent request.

    Shared by the early-stop predicate (evaluated during collection) and the
    post-collection success check, so both use the same completion rule.
    """
    return {
        result.event_type for result in results
        if result.known and result.normalized is not None and not result.normalization_pending
        and result.event_type in expected_events
        and (
            result.event_type != "akd_data"
            or (
                isinstance(result.normalized, LiveAKDResponse)
                and result.normalized.request_provenance is not None
            )
        )
        and (
            result.event_type != "custodian_data"
            or (
                isinstance(result.normalized, LiveCustodianResponse)
                and result.normalized.request_provenance is not None
            )
        )
        and (
            result.event_type != "custodian_diff_data"
            or (
                isinstance(result.normalized, LiveCustodianDiffResponse)
                and result.normalized.request_provenance is not None
            )
        )
        and (
            result.event_type != "custody_analysis_data"
            or (
                isinstance(result.normalized, LiveCustodyAnalysisResponse)
                and result.normalized.request_provenance is not None
                and result.normalized.symbol == result.normalized.request_provenance.symbol
            )
        )
        and (
            result.event_type != "custody_distribution_data"
            or (
                isinstance(result.normalized, LiveCustodyDistributionResponse)
                and result.normalized.request_provenance is not None
            )
        )
        and (
            result.event_type != "custody_stats_data"
            or (
                isinstance(result.normalized, LiveCustodyStatsResponse)
                and _stats_provenance_matches_request(result, requests)
            )
        )
        and (
            result.event_type != "brokerages_data"
            or isinstance(result.normalized, LiveBrokeragesResponse)
        )
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one short WebSocket collector smoke test; persistence is opt-in."
    )
    parser.add_argument("--symbol", help="BIST symbol, for example ASELS")
    parser.add_argument(
        "--akd-only", action="store_true",
        help="Send only one get_akd request; requires --symbol, --start-date, --end-date.",
    )
    parser.add_argument(
        "--custodian-only", action="store_true",
        help="Send only one get_custodian request; requires --symbol and --custody-date.",
    )
    parser.add_argument(
        "--custodian-diff-only", action="store_true",
        help="Send only one get_custodian_diff request; requires --symbol, --start-date, --end-date.",
    )
    parser.add_argument(
        "--custody-analysis-only", action="store_true",
        help="Send only one custody_analysis request; requires --symbol, --from-date, --to-date.",
    )
    parser.add_argument(
        "--custody-distribution-only", action="store_true",
        help="Send only one get_custody_distribution request; requires --symbol.",
    )
    parser.add_argument(
        "--custody-stats-only", action="store_true",
        help="Send only one get_custody_stats request; requires --symbol.",
    )
    symbols_group = parser.add_mutually_exclusive_group()
    symbols_group.add_argument(
        "--symbols-file", type=Path,
        help=(
            "Path to a JSON file with schema fintr_universe_v1 "
            '({"symbols": ["A", "B", ...]}) for multi-symbol period capture; '
            "combine with --start-date/--end-date/--save-period-analysis-context."
        ),
    )
    symbols_group.add_argument(
        "--symbols", type=str,
        help="Comma-separated symbols for multi-symbol period capture, e.g. A,B,C.",
    )
    parser.add_argument(
        "--symbol-pause-seconds", type=float, default=3.0,
        help=(
            "Bounded pause between symbols in multi-symbol mode, in seconds "
            "(default: 3; maximum: 30)."
        ),
    )
    parser.add_argument(
        "--max-wall-seconds", type=int, default=1800,
        help=(
            "Total wall-time guard for a multi-symbol run, in seconds "
            "(default: 1800; maximum: 21600)."
        ),
    )
    parser.add_argument("--start-date", type=_date)
    parser.add_argument("--end-date", type=_date)
    parser.add_argument("--custody-date", type=_date)
    parser.add_argument("--include-custody-history", action="store_true")
    parser.add_argument("--from-date", type=_date)
    parser.add_argument("--to-date", type=_date)
    parser.add_argument("--allow-large-range", action="store_true")
    parser.add_argument(
        "--allow-session-takeover",
        action="store_true",
        help=(
            "Acknowledge that this connection may close the active Telegram Mini App "
            "session. Required for every live run."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Validate arguments and print the planned requests, expected responses, "
            "limits, and persistence target, then exit; never requires "
            "--allow-session-takeover, never prompts for initData, never builds an "
            "authenticated URL, and never opens a connection."
        ),
    )
    parser.add_argument(
        "--require-all-responses",
        action="store_true",
        help="Require every planned request to be sent and every expected response to normalize.",
    )
    parser.add_argument(
        "--validate-evidence-bundle",
        action="store_true",
        help="Also validate the five-response in-memory evidence view; requires --require-all-responses.",
    )
    parser.add_argument(
        "--validate-live-context", action="store_true",
        help="Build an in-memory bounded LLM context and prompt; requires evidence validation and explicit limits.",
    )
    parser.add_argument(
        "--emit-analysis-context", action="store_true",
        help="Print the validated bounded context as marked JSON; requires --validate-live-context.",
    )
    period_output = parser.add_mutually_exclusive_group()
    period_output.add_argument(
        "--emit-period-analysis-context", action="store_true",
        help=(
            "After all four period requests are sent and matched, print the full "
            "sanitized brokerage-directory/AKD/daily analysis/custodian-difference "
            "packet as marked JSON."
        ),
    )
    period_output.add_argument(
        "--save-period-analysis-context", action="store_true",
        help=(
            "After all four period requests are sent and matched, save the full "
            "sanitized packet under data/interim/period_analysis without printing it."
        ),
    )
    parser.add_argument(
        "--capture-eval-context", action="store_true", help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--capture-pilot-context", action="store_true",
        help=(
            "After evidence and context validation, save only the bounded AKFIS "
            "2026-09-23 development pilot under data/interim/pilot; never final "
            "evaluation or training; requires --validate-live-context."
        ),
    )
    parser.add_argument("--akd-context-rows", type=_positive_int)
    parser.add_argument("--custodian-context-rows", type=_positive_int)
    parser.add_argument("--distribution-context-rows", type=_positive_int)
    parser.add_argument("--stats-context-points", type=_positive_int)
    parser.add_argument(
        "--timeout", type=float, default=5.0,
        help="Timeout for each receive, in seconds (default: 5; maximum: 30).",
    )
    parser.add_argument(
        "--max-frames", "--max-events", dest="max_events", type=int, default=8,
        help="Total frame limit per attempt, including control frames (default: 8; maximum: 50).",
    )
    parser.add_argument(
        "--max-message-mib", type=int, default=1,
        help="Largest accepted WebSocket message in MiB (default: 1; maximum: 8); "
             "raise only for long period ranges.",
    )
    parser.add_argument(
        "--show-unknown-event-types", action="store_true",
        help=(
            "For protocol observation only: show an unknown event type's name "
            "when it strictly matches ^[a-z][a-z_]{0,39}$ (1-40 lowercase "
            "letters/underscores only; no digits, no uppercase). Anything else, "
            "including any digit/uppercase/long/URL-like/secret-like string, "
            "still prints as [UNKNOWN_EVENT_TYPE]. Default: always masked."
        ),
    )
    return parser


def _requests(args: argparse.Namespace, parser: argparse.ArgumentParser):
    period_context_output = (
        args.emit_period_analysis_context or args.save_period_analysis_context
    )
    if period_context_output:
        output_flag = (
            "--save-period-analysis-context"
            if args.save_period_analysis_context
            else "--emit-period-analysis-context"
        )
        if not args.symbol or args.start_date is None or args.end_date is None:
            parser.error(
                f"{output_flag} requires --symbol, --start-date, and --end-date"
            )
        if args.start_date > args.end_date:
            parser.error("--start-date must not be after --end-date")
        incompatible = (
            args.akd_only, args.custodian_only, args.custodian_diff_only,
            args.custody_analysis_only, args.custody_distribution_only,
            args.custody_stats_only, args.custody_date is not None,
            args.include_custody_history, args.from_date is not None,
            args.to_date is not None, args.validate_evidence_bundle,
            args.validate_live_context, args.emit_analysis_context,
            args.capture_pilot_context, args.akd_context_rows is not None,
            args.custodian_context_rows is not None,
            args.distribution_context_rows is not None,
            args.stats_context_points is not None,
        )
        if any(incompatible):
            parser.error(
                f"{output_flag} cannot be combined with single-family, "
                "custody-history, bounded-context, or pilot-capture options"
            )
        range_days = (args.end_date - args.start_date).days
        if range_days > 90 and not args.allow_large_range:
            parser.error("period analysis over 90 days requires --allow-large-range")
        try:
            return _period_analysis_requests(
                args.symbol, args.start_date, args.end_date,
            )
        except ValueError:
            parser.error("invalid symbol or period-analysis request")

    if sum((args.akd_only, args.custodian_only, args.custodian_diff_only,
            args.custody_analysis_only, args.custody_distribution_only,
            args.custody_stats_only)) > 1:
        parser.error("only one --*-only mode may be selected")
    if args.akd_only:
        if not args.symbol or args.start_date is None or args.end_date is None:
            parser.error("--akd-only requires --symbol, --start-date, and --end-date")
        if args.custody_date or args.include_custody_history or args.from_date or args.to_date or args.allow_large_range:
            parser.error("--akd-only cannot be combined with custody/history options")
        return (build_akd_request(args.symbol, args.start_date, args.end_date),)
    if args.custodian_only:
        if not args.symbol or args.custody_date is None:
            parser.error("--custodian-only requires --symbol and --custody-date")
        if (args.start_date or args.end_date or args.include_custody_history
                or args.from_date or args.to_date or args.allow_large_range):
            parser.error("--custodian-only cannot be combined with AKD/history options")
        return (build_custodian_request(args.symbol, args.custody_date),)
    if args.custodian_diff_only:
        if not args.symbol or args.start_date is None or args.end_date is None:
            parser.error("--custodian-diff-only requires --symbol, --start-date, and --end-date")
        if (args.custody_date or args.include_custody_history or args.from_date
                or args.to_date or args.allow_large_range):
            parser.error("--custodian-diff-only cannot be combined with custody/history options")
        return (build_custodian_diff_request(args.symbol, args.start_date, args.end_date),)
    if args.custody_analysis_only:
        if not args.symbol or args.from_date is None or args.to_date is None:
            parser.error("--custody-analysis-only requires --symbol, --from-date, and --to-date")
        if (args.start_date or args.end_date or args.custody_date
                or args.include_custody_history):
            parser.error("--custody-analysis-only cannot be combined with other snapshot/history options")
        range_days = (args.to_date - args.from_date).days
        if range_days < 0:
            parser.error("--from-date must not be after --to-date")
        if range_days > 90 and not args.allow_large_range:
            parser.error("custody analysis over 90 days requires --allow-large-range")
        return (build_custody_analysis_request(args.symbol, args.from_date, args.to_date),)
    if args.custody_distribution_only:
        if not args.symbol:
            parser.error("--custody-distribution-only requires --symbol")
        if (args.start_date or args.end_date or args.custody_date
                or args.include_custody_history or args.from_date or args.to_date
                or args.allow_large_range):
            parser.error(
                "--custody-distribution-only cannot be combined with date/history options"
            )
        return (build_custody_distribution_request(args.symbol),)
    if args.custody_stats_only:
        if not args.symbol:
            parser.error("--custody-stats-only requires --symbol")
        if (args.start_date or args.end_date or args.custody_date
                or args.include_custody_history or args.from_date or args.to_date
                or args.allow_large_range):
            parser.error(
                "--custody-stats-only cannot be combined with date/history options"
            )
        return (build_custody_stats_request(args.symbol),)
    requests = [build_brokerages_request()]
    if args.symbol:
        missing = [
            name
            for name, value in (
                ("--start-date", args.start_date),
                ("--end-date", args.end_date),
                ("--custody-date", args.custody_date),
            )
            if value is None
        ]
        if missing:
            parser.error(f"--symbol requires {', '.join(missing)}")
        requests.extend(
            (
                build_akd_request(args.symbol, args.start_date, args.end_date),
                build_custodian_request(args.symbol, args.custody_date),
                build_custody_distribution_request(args.symbol),
                build_custody_stats_request(args.symbol),
            )
        )

    if args.include_custody_history:
        if not args.symbol or args.from_date is None or args.to_date is None:
            parser.error(
                "--include-custody-history requires --symbol, --from-date, and --to-date"
            )
        range_days = (args.to_date - args.from_date).days
        if range_days > 90 and not args.allow_large_range:
            parser.error("custody history over 90 days requires --allow-large-range")
        requests.append(
            build_custody_analysis_request(
                args.symbol,
                args.from_date,
                args.to_date,
            )
        )
    return tuple(requests)


def _period_analysis_requests(symbol: str, start: date, end: date):
    """Build the four distinct query families for one date-agnostic period profile."""
    return (
        build_brokerages_request(),
        build_akd_request(symbol, start, end),
        build_custody_analysis_request(symbol, start, end),
        build_custodian_diff_request(symbol, start, end),
    )


class _CachedSecretProvider:
    """Wrap a secret provider so initData is prompted for at most once per run.

    The value is kept only in this process's memory for the run's duration
    (never written to disk or logged) and reused across every per-symbol
    ``WebSocketCollector`` connection.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self._cached: str | None = None

    def get_init_data(self) -> str:
        if self._cached is None:
            self._cached = self._inner.get_init_data()
        return self._cached


def _multi_symbol_period_target(symbol: str, start: date, end: date) -> Path:
    return (
        PROJECT_ROOT / "data" / "interim" / "period_analysis" / symbol
        / f"{start.isoformat()}_{end.isoformat()}.json"
    )


def _normalize_multi_symbol(value: str, parser: argparse.ArgumentParser) -> str:
    candidate = value.strip().upper()
    if not _MULTI_SYMBOL_TOKEN_PATTERN.fullmatch(candidate):
        parser.error(
            f"invalid symbol in multi-symbol input: {value!r} (expected 1-16 "
            "ASCII letters or digits)"
        )
    return candidate


def _load_symbols_file(path: Path, parser: argparse.ArgumentParser) -> tuple[str, ...]:
    """Read and validate a fintr_universe_v1 symbols file; never echoes its raw bytes."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        parser.error(f"--symbols-file could not be read: {path}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        parser.error(f"--symbols-file is not valid JSON: {path}")
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != MULTI_SYMBOL_UNIVERSE_SCHEMA
    ):
        parser.error(
            "--symbols-file must be a JSON object with "
            f'"schema": "{MULTI_SYMBOL_UNIVERSE_SCHEMA}"'
        )
    symbols = payload.get("symbols")
    if (
        not isinstance(symbols, list)
        or not symbols
        or not all(isinstance(item, str) for item in symbols)
    ):
        parser.error('--symbols-file "symbols" must be a non-empty list of strings')
    return tuple(symbols)


def _load_multi_symbols(
    args: argparse.Namespace, parser: argparse.ArgumentParser,
) -> tuple[str, ...]:
    if args.symbols_file is not None:
        raw = _load_symbols_file(args.symbols_file, parser)
    else:
        raw = tuple(
            part for part in (item.strip() for item in args.symbols.split(",")) if part
        )
        if not raw:
            parser.error("--symbols must contain at least one non-empty symbol")
    symbols: list[str] = []
    seen: set[str] = set()
    for item in raw:
        normalized = _normalize_multi_symbol(item, parser)
        if normalized not in seen:
            seen.add(normalized)
            symbols.append(normalized)
    if not symbols:
        parser.error("no symbols found in multi-symbol input")
    if len(symbols) > MAX_SYMBOLS_PER_RUN:
        parser.error(
            "multi-symbol run exceeds the hard cap of "
            f"{MAX_SYMBOLS_PER_RUN} symbols per run (got {len(symbols)})"
        )
    return tuple(symbols)


def _print_multi_symbol_table(rows: list[tuple[str, str, int]]) -> None:
    print("")
    print(f"{'Symbol':<16}{'Status':<34}Frames")
    for symbol, status, frames_used in rows:
        print(f"{symbol:<16}{status:<34}{frames_used}")


def _print_multi_symbol_dry_run(
    args: argparse.Namespace, symbols: tuple[str, ...], start: date, end: date, today: date,
    *, excluded: tuple[str, ...] = (),
) -> int:
    """Print the multi-symbol plan; never prompts for initData or opens a connection."""
    print("Dry run: argument validation passed; no initData prompt, no connection.")
    print(
        f"Multi-symbol period mode: {len(symbols)} symbol(s) "
        f"(hard cap {MAX_SYMBOLS_PER_RUN} per run)"
    )
    print(
        f"Limits: {args.max_events} total frames per attempt per symbol; "
        f"{args.timeout:g}s per receive; {args.max_message_mib} MiB per message; "
        f"at most 2 retries per symbol; {args.symbol_pause_seconds:g}s pause between "
        f"symbols; {args.max_wall_seconds}s total wall-time guard"
    )
    for symbol in symbols:
        requests = _period_analysis_requests(symbol, start, end)
        target = _multi_symbol_period_target(symbol, start, end)
        print(
            f"  {symbol}: requests=" + ",".join(request.request_type for request in requests)
            + f"; target={target.relative_to(PROJECT_ROOT).as_posix()}"
            + f"; exists={target.exists()}"
        )
    for symbol in excluded:
        print(f"  {symbol}: skipped_excluded (configs/exclusions/symbols.json; no request)")
    note = _same_day_note(today, args)
    if note is not None:
        print(note)
    print("Dry run: no connection opened")
    return 0


async def _collect_multi_symbol(
    args: argparse.Namespace, symbols: tuple[str, ...], start: date, end: date, today: date,
    *, excluded: tuple[str, ...] = (),
) -> int:
    """Capture one period packet per symbol, sequentially, one connection each.

    Prompts for initData once (cached only in memory for this run) and never
    shares a connection between symbols, because akd_data responses carry no
    server-echoed symbol/period: local provenance only binds a response when
    exactly one request of that type was sent on that connection.
    """
    print(
        f"Multi-symbol period mode: {len(symbols)} symbol(s) "
        f"(hard cap {MAX_SYMBOLS_PER_RUN} per run)"
    )
    print(
        f"Limits: {args.max_events} total frames per attempt per symbol; "
        f"{args.timeout:g}s per receive; {args.max_message_mib} MiB per message; "
        f"{args.symbol_pause_seconds:g}s pause between symbols; "
        f"{args.max_wall_seconds}s total wall-time guard"
    )
    same_day_note = _same_day_note(today, args)
    if same_day_note is not None:
        print(same_day_note)

    config = CollectorConfig(
        receive_timeout_seconds=args.timeout,
        max_events=args.max_events,
        max_message_bytes=args.max_message_mib * 1_048_576,
    )
    period_expected_events = {
        REQUEST_RESPONSE_EVENTS[request_type] for request_type in PERIOD_ANALYSIS_REQUEST_TYPES
    }
    secret_provider = _CachedSecretProvider(InteractiveSecretProvider())

    rows: list[tuple[str, str, int]] = [(symbol, "skipped_excluded", 0) for symbol in excluded]
    consecutive_session_failures = 0
    run_started = monotonic()
    remaining = list(symbols)
    while remaining:
        if monotonic() - run_started > args.max_wall_seconds:
            print(
                f"Stopping: total wall-time guard ({args.max_wall_seconds}s) reached; "
                f"{len(remaining)} symbol(s) not attempted."
            )
            for symbol in remaining:
                rows.append((symbol, "skipped_wall_time_guard", 0))
            remaining = []
            break

        symbol = remaining.pop(0)
        target = _multi_symbol_period_target(symbol, start, end)
        if target.exists():
            print(f"{symbol}: skipped_existing")
            rows.append((symbol, "skipped_existing", 0))
            if remaining:
                await asyncio.sleep(args.symbol_pause_seconds)
            continue

        requests = _period_analysis_requests(symbol, start, end)
        send_observations: list[SendObservation] = []

        def report_send(observation: SendObservation) -> None:
            send_observations.append(observation)
            print(
                f"{symbol} attempt {observation.attempt}: "
                f"{observation.request_type} - {observation.status}"
            )

        def report_failure(diagnostic: AttemptDiagnostic) -> None:
            print(
                f"{symbol} attempt {diagnostic.attempt}: failure at "
                f"{diagnostic.stage} ({diagnostic.category})"
            )

        def stop_when(observed: tuple, _requests=requests) -> bool:
            return period_expected_events <= _matched_response_events(
                observed, _requests, period_expected_events,
            )

        collector = WebSocketCollector(
            secret_provider, config=config, send_observer=report_send,
            diagnostic_observer=report_failure,
        )
        results: tuple | None
        try:
            results = await collector.collect(requests, stop_when=stop_when)
        except CollectorConnectionError as exc:
            print(f"{symbol}: {exc}")
            results = None

        any_sent = any(
            observation.status == "sent" for observation in send_observations
        )
        frames_used = 0
        if results is None or not any_sent:
            status = "failed:session_no_send" if not any_sent else "failed:connection_error"
        else:
            frames_used = len(results)
            try:
                packet = _period_analysis_packet(
                    results, requests, send_observations,
                    symbol=symbol, start=start, end=end,
                )
                validate_context_contains_no_secrets(_secret_validation_copy(packet))
                _save_period_analysis_capture(packet)
                status = "saved"
            except PeriodAnalysisHandoffError as exc:
                print(f"{symbol}: period-analysis handoff validation ({exc})")
                status = f"failed:handoff_{exc.reason}"
            except PeriodAnalysisCaptureError as exc:
                if exc.reason == "capture_already_exists":
                    print(f"{symbol}: period-analysis capture already exists (no overwrite)")
                else:
                    print(f"{symbol}: period-analysis capture could not be safely saved")
                status = f"failed:capture_{exc.reason}"
            except (LivePilotCaptureError, TypeError, ValueError):
                print(f"{symbol}: safe period-analysis export validation failed")
                status = "failed:export_validation"

        print(f"{symbol}: {status}")
        rows.append((symbol, status, frames_used))

        consecutive_session_failures = (
            consecutive_session_failures + 1 if status == "failed:session_no_send" else 0
        )
        if consecutive_session_failures >= _CONSECUTIVE_SESSION_FAILURES_TO_STOP:
            print(
                "Stopping: session-level failure pattern detected for "
                f"{_CONSECUTIVE_SESSION_FAILURES_TO_STOP} consecutive symbols "
                "(connection closed before any request was sent). Check the "
                "Telegram Mini App session/initData before retrying."
            )
            for skipped in remaining:
                rows.append((skipped, "skipped_session_failure_stop", 0))
            remaining = []
            break

        if remaining:
            await asyncio.sleep(args.symbol_pause_seconds)

    _print_multi_symbol_table(rows)

    session_failure_stop = any(
        status == "skipped_session_failure_stop" for _, status, _ in rows
    )
    return 1 if session_failure_stop else 0


async def _run_multi_symbol(
    args: argparse.Namespace, parser: argparse.ArgumentParser, today: date, exclusions,
) -> int:
    incompatible = (
        bool(args.symbol), args.akd_only, args.custodian_only, args.custodian_diff_only,
        args.custody_analysis_only, args.custody_distribution_only,
        args.custody_stats_only, args.custody_date is not None,
        args.include_custody_history, args.from_date is not None,
        args.to_date is not None, args.emit_period_analysis_context,
        args.require_all_responses, args.validate_evidence_bundle,
        args.validate_live_context, args.emit_analysis_context,
        args.capture_pilot_context,
        args.akd_context_rows is not None, args.custodian_context_rows is not None,
        args.distribution_context_rows is not None, args.stats_context_points is not None,
    )
    if any(incompatible):
        parser.error(
            "--symbols-file/--symbols cannot be combined with --symbol, "
            "single-family, custody-history, bounded-context, or pilot-capture options"
        )
    if not args.save_period_analysis_context:
        parser.error("--symbols-file/--symbols requires --save-period-analysis-context")
    if args.start_date is None or args.end_date is None:
        parser.error("--symbols-file/--symbols requires --start-date and --end-date")
    if args.start_date > args.end_date:
        parser.error("--start-date must not be after --end-date")
    range_days = (args.end_date - args.start_date).days
    if range_days > 90 and not args.allow_large_range:
        parser.error("period analysis over 90 days requires --allow-large-range")
    if not 0 <= args.symbol_pause_seconds <= 30:
        parser.error("--symbol-pause-seconds must be between 0 and 30 seconds")
    if not 60 <= args.max_wall_seconds <= 21600:
        parser.error("--max-wall-seconds must be between 60 and 21600 seconds")

    requested = _load_multi_symbols(args, parser)
    start, end = args.start_date, args.end_date
    # Excluded symbols are dropped here, before any initData prompt or
    # connection, and reported as skipped_excluded (no override flag).
    excluded = tuple(symbol for symbol in requested if exclusions.is_excluded(symbol))
    symbols = tuple(symbol for symbol in requested if not exclusions.is_excluded(symbol))
    for symbol in excluded:
        print(f"{symbol}: skipped_excluded (configs/exclusions/symbols.json)")

    if args.dry_run:
        return _print_multi_symbol_dry_run(args, symbols, start, end, today, excluded=excluded)
    if not symbols:
        print("No symbols left after exclusions: no initData prompt, no connection.")
        _print_multi_symbol_table([(symbol, "skipped_excluded", 0) for symbol in excluded])
        return 0
    return await _collect_multi_symbol(args, symbols, start, end, today, excluded=excluded)


def _dry_run_persistence_target(
    args: argparse.Namespace, requests,
) -> tuple[str, bool] | None:
    """Relative POSIX path and on-disk existence for the one opt-in save target, if any."""
    if args.save_period_analysis_context:
        symbol = requests[1].data["company"]
        target = (
            PROJECT_ROOT / "data" / "interim" / "period_analysis" / symbol
            / f"{args.start_date.isoformat()}_{args.end_date.isoformat()}.json"
        )
    elif args.capture_pilot_context:
        target = DATA_PILOT_DIR / PILOT_CAPTURE_FILENAME
    else:
        return None
    return target.relative_to(PROJECT_ROOT).as_posix(), target.exists()


def _print_dry_run(
    args: argparse.Namespace,
    requests,
    expected_events: set[str],
    response_requirement: str,
    today: date,
) -> int:
    """Print the same plan a real run would send/expect; never touch secrets or the network."""
    print("Dry run: argument validation passed; no initData prompt, no connection.")
    print("Planned requests, in order:")
    for request in requests:
        print(f"  {request.request_type}: {request.to_json()}")
    print(f"Expected responses ({response_requirement}): " + ", ".join(sorted(expected_events)))
    print(
        f"Limits: {args.max_events} total frames per attempt; "
        f"{args.timeout:g}s per receive; {args.max_message_mib} MiB per message; "
        "at most 2 retries"
    )
    target = _dry_run_persistence_target(args, requests)
    if target is None:
        print("Persistent storage: disabled")
    else:
        relative_path, exists = target
        label = (
            "bounded AKFIS development pilot" if args.capture_pilot_context
            else "validated period-analysis packet"
        )
        print(f"Persistent storage: opt-in requested for the {label}")
        print(f"Persistence target: {relative_path}")
        if exists:
            print(
                "Persistence target already exists: a real run would fail with "
                "\"already exists\" (no overwrite)"
            )
        else:
            print("Persistence target does not exist yet")
    note = _same_day_note(today, args)
    if note is not None:
        print(note)
    print("Dry run: no connection opened")
    return 0


def _period_response(results, event_type: str, response_type):
    matches = [result for result in results if result.event_type == event_type]
    if len(matches) != 1:
        raise PeriodAnalysisHandoffError("missing_or_duplicate_response")
    result = matches[0]
    if (
        not result.known or result.normalization_pending
        or not isinstance(result.normalized, response_type)
    ):
        raise PeriodAnalysisHandoffError("response_not_normalized")
    return result.normalized


def _period_analysis_packet(
    results,
    requests,
    send_observations,
    *,
    symbol: str,
    start: date,
    end: date,
) -> dict[str, object]:
    """Validate sent/echoed scope and build a full, explicitly grained packet."""
    expected_requests = _period_analysis_requests(symbol, start, end)
    symbol = expected_requests[1].data["company"]
    if tuple(
        (request.request_type, request.data) for request in requests
    ) != tuple(
        (request.request_type, request.data) for request in expected_requests
    ):
        raise PeriodAnalysisHandoffError("request_profile_mismatch")

    final_attempt = max(
        (observation.attempt for observation in send_observations), default=0,
    )
    sent_types = tuple(
        observation.request_type for observation in send_observations
        if observation.attempt == final_attempt and observation.status == "sent"
    )
    if sent_types != PERIOD_ANALYSIS_REQUEST_TYPES:
        raise PeriodAnalysisHandoffError("not_all_requests_sent")

    brokerages = _period_response(results, "brokerages_data", LiveBrokeragesResponse)
    akd = _period_response(results, "akd_data", LiveAKDResponse)
    analysis = _period_response(
        results, "custody_analysis_data", LiveCustodyAnalysisResponse,
    )
    custodian_diff = _period_response(
        results, "custodian_diff_data", LiveCustodianDiffResponse,
    )

    akd_request = akd.request_provenance
    if not (
        akd_request is not None
        and akd_request.origin == "local_sent_request"
        and akd_request.symbol == symbol
        and akd_request.start == start
        and akd_request.end == end
    ):
        raise PeriodAnalysisHandoffError("akd_request_provenance_mismatch")

    analysis_request = analysis.request_provenance
    if not (
        analysis_request is not None
        and analysis_request.origin == "local_sent_request"
        and analysis_request.symbol == symbol
        and analysis_request.requested_from == start
        and analysis_request.requested_to == end
    ):
        raise PeriodAnalysisHandoffError("analysis_request_provenance_mismatch")
    if (
        analysis.symbol != symbol
        or analysis.data_range.requested_from != start
        or analysis.data_range.requested_to != end
    ):
        raise PeriodAnalysisHandoffError(
            "analysis_server_echo_mismatch",
            safe_detail=(
                f"requested_range={start.isoformat()}..{end.isoformat()}; "
                "server_requested_range="
                f"{analysis.data_range.requested_from.isoformat()}.."
                f"{analysis.data_range.requested_to.isoformat()}; "
                f"actual_from={analysis.data_range.actual_from.isoformat()}"
            ),
        )

    diff_request = custodian_diff.request_provenance
    if not (
        diff_request is not None
        and diff_request.origin == "local_sent_request"
        and diff_request.symbol == symbol
        and diff_request.requested_start == start
        and diff_request.requested_end == end
    ):
        raise PeriodAnalysisHandoffError("custodian_diff_request_provenance_mismatch")
    if custodian_diff.start_date != start or custodian_diff.end_date != end:
        raise PeriodAnalysisHandoffError(
            "custodian_diff_server_echo_mismatch",
            safe_detail=(
                f"requested_range={start.isoformat()}..{end.isoformat()}; "
                "server_reported_range="
                f"{custodian_diff.start_date.isoformat()}.."
                f"{custodian_diff.end_date.isoformat()}"
            ),
        )

    def analysis_day(day) -> dict[str, object]:
        return {
            "date": day.date.isoformat(),
            "row_count": len(day.rows),
            "rows": [
                {
                    "broker_code": row.broker_code,
                    "net_size": row.net_size,
                    "net_cost": row.net_cost,
                    "net_percentage": row.net_percentage,
                    "total_size": row.total_size,
                    "total_cost": row.total_cost,
                }
                for row in day.rows
            ],
        }

    def custody_day(day) -> dict[str, object]:
        return {
            "date": day.date.isoformat(),
            "row_count": len(day.rows),
            "rows": [
                {
                    "broker_code": row.broker_code,
                    "lot_count": row.lot_count,
                    "percentage": row.percentage,
                }
                for row in day.rows
            ],
        }

    server_dates = tuple(day.isoformat() for day in analysis.dates)
    akd_dates = tuple(day.date.isoformat() for day in analysis.akd)
    custody_dates = tuple(day.date.isoformat() for day in analysis.custody)
    metadata_dates = tuple(day.date.isoformat() for day in analysis.metadata)

    def day_coverage(dates: tuple[str, ...]) -> dict[str, object]:
        date_set = set(dates)
        server_date_set = set(server_dates)
        return {
            "date_count": len(dates),
            "dates_in_provider_order": list(dates),
            "missing_from_family_dates": [
                day for day in server_dates if day not in date_set
            ],
            "not_listed_in_server_dates": [
                day for day in dates if day not in server_date_set
            ],
        }

    packet: dict[str, object] = {
        "schema": "fintr_period_analysis_handoff_v2",
        "brokerage_directory": {
            "grain": (
                "undated_provider_snapshot_rows; exact_code_match_only; "
                "not_ultimate_investor"
            ),
            "source_event": "brokerages_data",
            "row_count": len(brokerages.rows),
            "rows": [
                {
                    "code": row.code,
                    "title": row.title,
                    "short_title": row.short_title,
                }
                for row in brokerages.rows
            ],
            "flags": {
                "undated": True,
                "exact_code_match_only": True,
                "identifies_ultimate_investor": False,
            },
        },
        "requested_period": {
            "symbol": symbol,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "origin": "local_cli_arguments",
        },
        "get_akd_period": {
            "grain": "provider_ordered_period_response_rows; no daily partition echoed",
            "provenance": {
                "local_sent_request": {
                    "origin": akd_request.origin,
                    "symbol": akd_request.symbol,
                    "start_date": akd_request.start.isoformat(),
                    "end_date": akd_request.end.isoformat(),
                },
                "server_echo": {
                    "symbol_echoed": False,
                    "period_echoed": False,
                    "as_of": akd.as_of,
                    "source": akd.source,
                },
            },
            "timestamp": akd.timestamp,
            "processing_time_ms": akd.processing_time_ms,
            "row_count": len(akd.results),
            "rows_in_provider_order": [
                {
                    "brokerage": row.brokerage,
                    "net": {
                        "size": row.net.size,
                        "percentage": row.net.percentage,
                        "cost": row.net.cost,
                    },
                    "total": {
                        "size": row.total.size,
                        "percentage": row.total.percentage,
                        "cost": row.total.cost,
                        "volume": row.total.volume,
                    },
                }
                for row in akd.results
            ],
        },
        "custody_analysis_daily": {
            "grain": "separate_daily_akd_flow_and_custody_balance_series",
            "provenance": {
                "local_sent_request": {
                    "origin": analysis_request.origin,
                    "symbol": analysis_request.symbol,
                    "from_date": analysis_request.requested_from.isoformat(),
                    "to_date": analysis_request.requested_to.isoformat(),
                },
                "server_echo": {
                    "symbol": analysis.symbol,
                    "dates_in_provider_order": list(server_dates),
                    "data_range": {
                        "requested_from": analysis.data_range.requested_from.isoformat(),
                        "requested_to": analysis.data_range.requested_to.isoformat(),
                        "actual_from": analysis.data_range.actual_from.isoformat(),
                        "actual_to_reported": False,
                    },
                },
            },
            "coverage": {
                "server_date_count": len(server_dates),
                "daily_akd": {
                    **day_coverage(akd_dates),
                    "row_count": sum(len(day.rows) for day in analysis.akd),
                },
                "daily_custody_balance": {
                    **day_coverage(custody_dates),
                    "row_count": sum(len(day.rows) for day in analysis.custody),
                },
                "metadata": day_coverage(metadata_dates),
            },
            "daily_akd_flow_in_provider_order": [
                analysis_day(day) for day in analysis.akd
            ],
            "daily_custody_balance_in_provider_order": [
                custody_day(day) for day in analysis.custody
            ],
            "metadata_in_provider_order": [
                {
                    "date": day.date.isoformat(),
                    "null_fields": list(day.null_fields),
                    "uninterpreted_keys": list(day.uninterpreted_keys),
                    "retail_pct": day.retail_pct,
                    "institutional_pct": day.institutional_pct,
                }
                for day in analysis.metadata
            ],
        },
        "get_custodian_diff_period": {
            "grain": "provider_ordered_period_difference_rows",
            "provenance": {
                "local_sent_request": {
                    "origin": diff_request.origin,
                    "symbol": diff_request.symbol,
                    "start_date": diff_request.requested_start.isoformat(),
                    "end_date": diff_request.requested_end.isoformat(),
                },
                "server_echo": {
                    "symbol_echoed": False,
                    "start_date": custodian_diff.start_date.isoformat(),
                    "end_date": custodian_diff.end_date.isoformat(),
                    "source": custodian_diff.source,
                },
            },
            "timestamp": custodian_diff.timestamp,
            "processing_time_ms": custodian_diff.processing_time_ms,
            "row_count": len(custodian_diff.results),
            "rows_in_provider_order": [
                {
                    "custodian": row.custodian,
                    "difference": row.difference,
                    "first_value": row.first_value,
                    "last_value": row.last_value,
                    "first_percentage": row.first_percentage,
                    "last_percentage": row.last_percentage,
                    "percentage_change": row.percentage_change,
                }
                for row in custodian_diff.results
            ],
        },
        "numeric_encoding": "finite Decimal values are emitted as exact JSON numbers",
        "row_selection": "all normalized rows included; no row truncation",
    }
    return packet


def _secret_validation_copy(value):
    """Convert packet values to the validator's accepted JSON-compatible shapes."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal")
        return str(value)
    if isinstance(value, dict):
        return {key: _secret_validation_copy(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_secret_validation_copy(nested) for nested in value]
    return value


def _compact_json(value) -> str:
    """Serialize JSON with exact Decimal numeric tokens and no unsupported types."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal")
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + _compact_json(nested)
            for key, nested in value.items()
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_compact_json(item) for item in value) + "]"
    if value is None or type(value) in (str, int, float, bool):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    raise TypeError("unsupported handoff value")


def _canonical_period_json(value) -> str:
    """Serialize a JSON packet with sorted object keys and exact Decimal tokens."""
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("unsupported handoff key")
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + _canonical_period_json(value[key])
            for key in sorted(value)
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical_period_json(item) for item in value) + "]"
    return _compact_json(value)


def _period_analysis_capture_payload(
    packet: dict[str, object], *, captured_at: datetime | None = None,
) -> str:
    """Build and secret-check the persisted packet and its minimal provenance."""
    period = packet.get("requested_period")
    if not isinstance(period, dict):
        raise PeriodAnalysisCaptureError("invalid_packet_scope")
    symbol = period.get("symbol")
    start_date = period.get("start_date")
    end_date = period.get("end_date")
    if (
        not isinstance(symbol, str)
        or re.fullmatch(r"[A-Z0-9]{1,16}", symbol) is None
        or not isinstance(start_date, str)
        or not isinstance(end_date, str)
    ):
        raise PeriodAnalysisCaptureError("invalid_packet_scope")
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        raise PeriodAnalysisCaptureError("invalid_packet_scope") from None
    if start.isoformat() != start_date or end.isoformat() != end_date or start > end:
        raise PeriodAnalysisCaptureError("invalid_packet_scope")

    try:
        source_map = {
            "get_brokerages": "not_reported_by_normalized_schema",
            "get_akd": packet["get_akd_period"]["provenance"]["server_echo"]["source"],
            "custody_analysis": "not_reported_by_normalized_schema",
            "get_custodian_diff": packet["get_custodian_diff_period"]
                ["provenance"]["server_echo"]["source"],
        }
        for source in source_map.values():
            if source is not None and not isinstance(source, str):
                raise TypeError
        captured_at = captured_at or datetime.now(timezone.utc)
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError
        captured_utc = captured_at.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z",
        )
        canonical_packet = _canonical_period_json(packet).encode("utf-8")
        capture = {
            "capture_metadata": {
                "schema": "fintr_period_analysis_capture_v1",
                "captured_at_utc": captured_utc,
                "provider_sources": source_map,
                "permission_provenance": {
                    "status": "user_asserted_not_independently_verified",
                    "basis": "user_asserted_by_explicit_opt_in_cli_invocation",
                    "independently_verified": False,
                },
                "packet_schema": packet.get("schema"),
                "packet_sha256_canonical_json": hashlib.sha256(
                    canonical_packet,
                ).hexdigest(),
                "transformation": (
                    "four validated normalized typed responses to full "
                    "fintr_period_analysis_handoff_v2 packet; all normalized rows, "
                    "family coverage, and the accepted brokerage directory retained"
                ),
            },
            "packet": packet,
        }
        validate_context_contains_no_secrets(_secret_validation_copy(capture))
        return _compact_json(capture)
    except (KeyError, LivePilotCaptureError, TypeError, ValueError):
        raise PeriodAnalysisCaptureError("safe_export_validation_failed") from None


def _save_period_analysis_capture(packet: dict[str, object]) -> Path:
    """Atomically create one ignored per-symbol capture and refuse overwrite."""
    payload = (_period_analysis_capture_payload(packet) + "\n").encode("utf-8")
    period = packet["requested_period"]
    symbol = period["symbol"]
    start_date = period["start_date"]
    end_date = period["end_date"]
    directory = PROJECT_ROOT / "data" / "interim" / "period_analysis"
    if (
        directory.parent.parent.is_symlink()
        or directory.parent.is_symlink()
        or directory.is_symlink()
    ):
        raise PeriodAnalysisCaptureError("capture_directory_unavailable")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise OSError
        symbol_directory = directory / symbol
        if symbol_directory.is_symlink():
            raise OSError
        symbol_directory.mkdir(exist_ok=True)
        if (
            symbol_directory.is_symlink()
            or not symbol_directory.is_dir()
            or symbol_directory.resolve().parent != directory.resolve()
        ):
            raise OSError
    except OSError:
        raise PeriodAnalysisCaptureError("capture_directory_unavailable") from None

    target = symbol_directory / f"{start_date}_{end_date}.json"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".period-analysis-", suffix=".tmp",
            dir=symbol_directory, delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary_path, target)
    except FileExistsError:
        raise PeriodAnalysisCaptureError("capture_already_exists") from None
    except OSError:
        raise PeriodAnalysisCaptureError("capture_write_failed") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return target


async def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    today = _reject_future_dates(args, parser)
    # Exclusions are enforced before anything else that could prompt for
    # initData or open a connection; there is no override flag. Holdout
    # symbols are deliberately not checked: collecting them is allowed.
    try:
        _exclusions_path, exclusions = load_active_exclusions()
    except SymbolGuardError as exc:
        parser.error(str(exc))
    if args.symbol and exclusions.is_excluded(args.symbol):
        parser.error(
            f"symbol {args.symbol.strip().upper()} is excluded by "
            "configs/exclusions/symbols.json (no override); no initData prompt, "
            "no connection"
        )
    if args.capture_eval_context:
        parser.error(
            "--capture-eval-context is retired: this case was used for prompt/output "
            "development and is not held-out evaluation data; use "
            "--capture-pilot-context only for development capture."
        )
    if args.emit_analysis_context and args.capture_pilot_context:
        parser.error(
            "--emit-analysis-context cannot be combined with --capture-pilot-context"
        )
    if not args.dry_run and not args.allow_session_takeover:
        parser.error(
            f"{SESSION_TAKEOVER_WARNING} Close the Mini App first, then rerun with "
            "--allow-session-takeover."
        )

    if not args.dry_run:
        print(SESSION_TAKEOVER_WARNING)
    if not 0 < args.timeout <= 30:
        parser.error("--timeout must be greater than 0 and at most 30 seconds")
    if not 1 <= args.max_events <= 50:
        parser.error("--max-events must be between 1 and 50")
    if not 1 <= args.max_message_mib <= 8:
        parser.error("--max-message-mib must be between 1 and 8")

    if args.symbols_file is not None or args.symbols is not None:
        return await _run_multi_symbol(args, parser, today, exclusions)

    requests = _requests(args, parser)
    if args.capture_pilot_context:
        if not args.validate_live_context:
            parser.error("--capture-pilot-context requires --validate-live-context")
        if (
            args.symbol != "AKFIS"
            or args.start_date != date(2026, 9, 23)
            or args.end_date != date(2026, 9, 23)
            or args.custody_date != date(2026, 9, 23)
            or args.include_custody_history
            or args.from_date is not None
            or args.to_date is not None
            or args.allow_large_range
            or args.akd_only
            or args.custodian_only
            or args.custodian_diff_only
            or args.custody_analysis_only
            or args.custody_distribution_only
            or args.custody_stats_only
        ):
            parser.error(
                "--capture-pilot-context is limited to AKFIS and 2026-09-23 "
                "with the default five-family request profile"
            )
    if args.validate_evidence_bundle and (
        not args.require_all_responses
        or len(requests) != len(EVIDENCE_REQUEST_TYPES)
        or {request.request_type for request in requests} != EVIDENCE_REQUEST_TYPES
    ):
        parser.error(
            "--validate-evidence-bundle requires --require-all-responses "
            "and the default five-request --symbol/date profile"
        )
    context_limit_values = (
        args.akd_context_rows, args.custodian_context_rows,
        args.distribution_context_rows, args.stats_context_points,
    )
    if args.validate_live_context:
        if not args.validate_evidence_bundle or any(value is None for value in context_limit_values):
            parser.error(
                "--validate-live-context requires --validate-evidence-bundle "
                "and all four positive context limits"
            )
    elif any(value is not None for value in context_limit_values):
        parser.error("context limits require --validate-live-context")
    if args.emit_analysis_context and not args.validate_live_context:
        parser.error("--emit-analysis-context requires --validate-live-context")
    expected_events = {REQUEST_RESPONSE_EVENTS[request.request_type] for request in requests}
    response_requirement = (
        "all" if (
            args.require_all_responses
            or args.emit_period_analysis_context
            or args.save_period_analysis_context
        )
        else "at least one"
    )
    if args.dry_run:
        return _print_dry_run(args, requests, expected_events, response_requirement, today)
    config = CollectorConfig(
        receive_timeout_seconds=args.timeout,
        max_events=args.max_events,
        max_message_bytes=args.max_message_mib * 1_048_576,
    )
    send_observations: list[SendObservation] = []

    def report_send(observation: SendObservation) -> None:
        send_observations.append(observation)
        labels = {
            "sent": "send completed",
            "send_failed": "send failed",
            "rate_limited": "rate limiter skipped",
            "not_sent_no_welcome": "not sent: no top-level welcome",
        }
        print(
            f"Attempt {observation.attempt}: {observation.request_type} - "
            f"{labels[observation.status]}"
        )

    def report_failure(diagnostic: AttemptDiagnostic) -> None:
        detail = (
            f"Attempt {diagnostic.attempt}: failure at {diagnostic.stage} "
            f"({diagnostic.category})"
        )
        if diagnostic.event_type is not None:
            detail += f"; event={diagnostic.event_type}"
        if diagnostic.field_path is not None:
            detail += f"; field={diagnostic.field_path}"
        safe_kinds = {
            "present", "missing_or_null", "empty_string", "string", "number",
            "object", "array", "date", "null", "boolean", "unknown",
        }
        if (diagnostic.expected_kind in safe_kinds
                and diagnostic.actual_kind in safe_kinds):
            detail += (
                f"; expected={diagnostic.expected_kind}; "
                f"actual={diagnostic.actual_kind}"
            )
        if diagnostic.shape_summary:
            detail += "; shape=" + " | ".join(diagnostic.shape_summary)
        print(detail)

    collector = WebSocketCollector(
        InteractiveSecretProvider(), config=config, send_observer=report_send,
        diagnostic_observer=report_failure,
    )
    display_url = redact_url(build_authenticated_url(config.endpoint, "display-only"))
    print(f"Endpoint: {display_url}")
    print("Planned requests (not proof of send): " + ", ".join(request.request_type for request in requests))
    print(f"Expected responses ({response_requirement}): " + ", ".join(sorted(expected_events)))
    print(
        f"Limits: {args.max_events} total frames per attempt; "
        f"{args.timeout:g}s per receive; {args.max_message_mib} MiB per message; "
        "at most 2 retries"
    )
    if args.capture_pilot_context:
        print("Persistent storage: opt-in requested for the bounded AKFIS development pilot")
    elif args.save_period_analysis_context:
        print("Persistent storage: opt-in requested for the validated period-analysis packet")
    else:
        print("Persistent storage: disabled")
    same_day_note = _same_day_note(today, args)
    if same_day_note is not None:
        print(same_day_note)

    def stop_when(observed: tuple) -> bool:
        # End the attempt as soon as every expected response is known, fully
        # normalized (not pending), and provenance-bound where applicable, so
        # the provider connection closes without waiting out extra frames.
        return expected_events <= _matched_response_events(observed, requests, expected_events)

    try:
        results = await collector.collect(requests, stop_when=stop_when)
    except CollectorConnectionError as exc:
        print(f"Smoke failed: {exc}")
        if not any(item.status == "sent" for item in send_observations):
            print("No request send completed in any attempt.")
        return 1

    if not any(item.status == "sent" for item in send_observations):
        print("No request send completed in any attempt.")

    if not results:
        print("Smoke failed: no event received before the bounded timeout.")
        return 1
    for result in results:
        status = "known" if result.known else "unknown"
        if result.normalization_pending:
            status += "; normalization pending: incomplete verified schema"
        label = _event_status_label(
            result.event_type,
            show_unknown_event_types=args.show_unknown_event_types,
        )
        print(f"Event: {label} ({status})")
    if args.emit_period_analysis_context or args.save_period_analysis_context:
        saved_path = None
        try:
            packet = _period_analysis_packet(
                results,
                requests,
                send_observations,
                symbol=args.symbol,
                start=args.start_date,
                end=args.end_date,
            )
            validate_context_contains_no_secrets(_secret_validation_copy(packet))
            if args.save_period_analysis_context:
                saved_path = _save_period_analysis_capture(packet)
            else:
                packet_json = _compact_json(packet)
        except PeriodAnalysisHandoffError as exc:
            print(f"Smoke failed: period-analysis handoff validation ({exc})")
            return 1
        except PeriodAnalysisCaptureError as exc:
            if exc.reason == "capture_already_exists":
                print("Smoke failed: period-analysis capture already exists (no overwrite)")
            else:
                print("Smoke failed: period-analysis capture could not be safely saved")
            return 1
        except (LivePilotCaptureError, TypeError, ValueError):
            print("Smoke failed: safe period-analysis export validation")
            return 1
        if args.save_period_analysis_context:
            print(
                "Period-analysis context saved: "
                f"{saved_path.relative_to(PROJECT_ROOT).as_posix()}"
            )
        else:
            print(
                f"{PERIOD_ANALYSIS_CONTEXT_BEGIN}\n"
                f"{packet_json}\n"
                f"{PERIOD_ANALYSIS_CONTEXT_END}"
            )
        print("Smoke completed: all period-analysis requests and responses validated")
        return 0
    matched_events = _matched_response_events(results, requests, expected_events)
    if not matched_events:
        if any(result.normalization_pending and result.event_type in expected_events
               for result in results):
            print("Smoke failed: requested event parsed, but safe local send context is unavailable.")
        else:
            print("Smoke failed: no requested response normalized within collection limits.")
        return 1
    if args.require_all_responses:
        final_attempt = max((observation.attempt for observation in send_observations), default=0)
        sent_types = {
            observation.request_type for observation in send_observations
            if observation.attempt == final_attempt and observation.status == "sent"
        }
        missing_sends = {request.request_type for request in requests} - sent_types
        missing_responses = expected_events - matched_events
        if missing_sends or missing_responses:
            if missing_sends:
                print("Smoke failed: requests not sent: " + ", ".join(sorted(missing_sends)))
            if missing_responses:
                print("Smoke failed: responses not received: " + ", ".join(sorted(missing_responses)))
            return 1
    if args.validate_evidence_bundle:
        try:
            evidence = build_live_collection_evidence(results)
        except LiveEvidenceError as exc:
            print(f"Smoke failed: in-memory evidence view: {exc}")
            return 1
        print("In-memory evidence view: validated")
        if args.validate_live_context:
            try:
                context = build_live_analysis_context(
                    evidence,
                    generated_at=datetime.now().astimezone(),
                    limits=LiveContextLimits(
                        akd_rows=args.akd_context_rows,
                        custodian_rows=args.custodian_context_rows,
                        distribution_rows=args.distribution_context_rows,
                        stats_points_per_series=args.stats_context_points,
                    ),
                )
                build_live_analysis_prompt(context)
            except (TypeError, ValueError):
                print("Smoke failed: in-memory LLM context validation")
                return 1
            print("In-memory LLM context and prompt: validated")
            if args.emit_analysis_context:
                try:
                    context_data = context.to_dict()
                    validate_context_contains_no_secrets(context_data)
                    context_json = json.dumps(
                        context_data, ensure_ascii=False, indent=2, allow_nan=False,
                    )
                    context_packet = (
                        f"{ANALYSIS_CONTEXT_BEGIN}\n"
                        f"{context_json}\n"
                        f"{ANALYSIS_CONTEXT_END}"
                    )
                except (LivePilotCaptureError, TypeError, ValueError):
                    print("Smoke failed: safe analysis context export validation")
                    return 1
                print(context_packet)
            if args.capture_pilot_context:
                try:
                    saved_path = capture_live_pilot_context(
                        evidence,
                        context,
                        requests,
                        LiveContextLimits(
                            akd_rows=args.akd_context_rows,
                            custodian_rows=args.custodian_context_rows,
                            distribution_rows=args.distribution_context_rows,
                            stats_points_per_series=args.stats_context_points,
                        ),
                    )
                except LivePilotCaptureError as exc:
                    print(f"Pilot context capture failed: {exc}")
                    return 1
                print(
                    "Pilot context captured: "
                    f"{saved_path.name}; classification=pilot/development; "
                    "not held-out; training/evaluation disabled"
                )
    print("Smoke completed: requested response received: " + ", ".join(sorted(matched_events)))
    if "akd_data" in matched_events:
        print("AKD symbol/date range: local sent-request provenance only; not echoed by server.")
    if "custodian_data" in matched_events:
        response = next(
            result.normalized for result in results
            if result.event_type == "custodian_data"
            and isinstance(result.normalized, LiveCustodianResponse)
            and result.normalized.request_provenance is not None
        )
        print(
            "Custodian requested date (local): "
            f"{response.request_provenance.requested_date.isoformat()}; "
            f"response date (server): {response.date.isoformat()}"
        )
    if "custodian_diff_data" in matched_events:
        response = next(
            result.normalized for result in results
            if result.event_type == "custodian_diff_data"
            and isinstance(result.normalized, LiveCustodianDiffResponse)
            and result.normalized.request_provenance is not None
        )
        provenance = response.request_provenance
        print(
            "Custodian diff requested range (local): "
            f"{provenance.requested_start.isoformat()}..{provenance.requested_end.isoformat()}; "
            "response range (server): "
            f"{response.start_date.isoformat()}..{response.end_date.isoformat()}"
        )
    if "custody_analysis_data" in matched_events:
        response = next(
            result.normalized for result in results
            if result.event_type == "custody_analysis_data"
            and isinstance(result.normalized, LiveCustodyAnalysisResponse)
            and result.normalized.request_provenance is not None
        )
        provenance = response.request_provenance
        print(
            "Custody analysis local requested range: "
            f"{provenance.requested_from.isoformat()}..{provenance.requested_to.isoformat()}; "
            "response data_range requested: "
            f"{response.data_range.requested_from.isoformat()}.."
            f"{response.data_range.requested_to.isoformat()}; "
            f"actual_from: {response.data_range.actual_from.isoformat()}"
        )
    if "custody_distribution_data" in matched_events:
        response = next(
            result.normalized for result in results
            if result.event_type == "custody_distribution_data"
            and isinstance(result.normalized, LiveCustodyDistributionResponse)
            and result.normalized.request_provenance is not None
        )
        print(
            "Custody distribution symbol (local sent-request provenance only; "
            f"not echoed by server): {response.request_provenance.symbol}"
        )
    if "custody_stats_data" in matched_events:
        response = next(
            result.normalized for result in results
            if result.event_type == "custody_stats_data"
            and isinstance(result.normalized, LiveCustodyStatsResponse)
            and result.normalized.request_provenance is not None
        )
        provenance = response.request_provenance
        print(
            "Custody stats symbol/history (local sent-request provenance only; "
            f"not echoed by server): {provenance.symbol}/{str(provenance.history).lower()}"
        )
    return 0


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    return asyncio.run(_run(args, parser))


if __name__ == "__main__":
    raise SystemExit(main())
