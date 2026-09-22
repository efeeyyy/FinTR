"""Short, non-persistent live collector smoke test with interactive initData."""

from __future__ import annotations

import argparse
import asyncio
from datetime import date

from fintr.collector import (
    CollectorConfig,
    CollectorConnectionError,
    InteractiveSecretProvider,
    WebSocketCollector,
    build_authenticated_url,
    redact_url,
)
from fintr.collector.subscriptions import (
    build_akd_request,
    build_brokerages_request,
    build_custodian_request,
    build_custody_analysis_request,
    build_custody_distribution_request,
    build_custody_stats_request,
)


# Smoke acceptance contract; control frames aren't market responses.
REQUEST_RESPONSE_EVENTS: dict[str, str] = {
    "get_brokerages": "brokerages_data",
    "get_akd": "akd_snapshot",
    "get_custodian": "custodian_data",
    "get_custody_distribution": "custody_distribution_data",
    "get_custody_stats": "custody_stats_data",
    "custody_analysis": "custody_analysis_data",
    "get_custodian_diff": "custodian_diff_data",
}


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one short, non-persistent WebSocket collector smoke test."
    )
    parser.add_argument("--symbol", help="BIST symbol, for example ASELS")
    parser.add_argument("--start-date", type=_date)
    parser.add_argument("--end-date", type=_date)
    parser.add_argument("--custody-date", type=_date)
    parser.add_argument("--include-custody-history", action="store_true")
    parser.add_argument("--from-date", type=_date)
    parser.add_argument("--to-date", type=_date)
    parser.add_argument("--allow-large-range", action="store_true")
    parser.add_argument(
        "--timeout", type=float, default=5.0,
        help="Timeout for each receive, in seconds (default: 5; maximum: 30).",
    )
    parser.add_argument(
        "--max-frames", "--max-events", dest="max_events", type=int, default=8,
        help="Total frame limit per attempt, including control frames (default: 8; maximum: 50).",
    )
    return parser


def _requests(args: argparse.Namespace, parser: argparse.ArgumentParser):
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


async def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if not 0 < args.timeout <= 30:
        parser.error("--timeout must be greater than 0 and at most 30 seconds")
    if not 1 <= args.max_events <= 50:
        parser.error("--max-events must be between 1 and 50")

    requests = _requests(args, parser)
    expected_events = {REQUEST_RESPONSE_EVENTS[request.request_type] for request in requests}
    config = CollectorConfig(
        receive_timeout_seconds=args.timeout,
        max_events=args.max_events,
    )
    collector = WebSocketCollector(InteractiveSecretProvider(), config=config)
    display_url = redact_url(build_authenticated_url(config.endpoint, "display-only"))
    print(f"Endpoint: {display_url}")
    print("Requests: " + ", ".join(request.request_type for request in requests))
    print("Expected responses (at least one): " + ", ".join(sorted(expected_events)))
    print(
        f"Limits: {args.max_events} total frames per attempt; "
        f"{args.timeout:g}s per receive; at most 2 retries"
    )
    print("Persistent storage: disabled")
    try:
        results = await collector.collect(requests)
    except CollectorConnectionError as exc:
        print(f"Smoke failed: {exc}")
        return 1

    if not results:
        print("Smoke failed: no event received before the bounded timeout.")
        return 1
    for result in results:
        status = "known" if result.known else "unknown"
        print(f"Event: {result.event_type} ({status})")
    matched_events = {
        result.event_type for result in results
        if result.known and result.normalized is not None
        and result.event_type in expected_events
    }
    if not matched_events:
        print("Smoke failed: no requested response received within collection limits.")
        return 1
    print("Smoke completed: requested response received: " + ", ".join(sorted(matched_events)))
    return 0


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    return asyncio.run(_run(args, parser))


if __name__ == "__main__":
    raise SystemExit(main())
