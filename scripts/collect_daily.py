"""CLI: plan (and, in a later PR, run) the daily incremental Mini App collection.

This PR implements **only** ``--dry-run``: it prints the per-symbol window
plan and a ``fintr_daily_collection_run_v1`` run-record skeleton
(``status: "planned"``) to stdout. It never opens a network connection,
never prompts for ``initData``, and never writes to
``data/interim/daily_capture/``. Calling it without ``--dry-run`` exits with
an error -- the live executor (reusing
``scripts.live_collector_smoke._collect_multi_symbol``, per
``docs/research/daily_miniapp_collection_adr.md`` "Uygulama sırası" madde 2)
is not implemented yet.

See ``docs/research/daily_miniapp_collection_adr.md`` for the full design
and ``src/fintr/collection/planner.py`` for the pure planning logic this
script only prints the output of.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from fintr.collection.planner import (
    RUN_RECORD_SCHEMA,
    CollectionPlannerError,
    config_sha256,
    load_daily_collection_config,
    plan_daily_collection,
    scan_last_success,
)
from fintr.evaluation.symbol_guard import SymbolGuardError, load_active_guards


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = Path("configs/collection/daily_miniapp_v1.json")
RUN_RECORDS_SUBDIR = "_runs"


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan the daily incremental Mini App collection window per symbol. "
            "This PR supports --dry-run only; see module docstring."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan and a run-record skeleton; never opens a connection "
             "or prompts for initData. Required in this PR.",
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help=f"default: {DEFAULT_CONFIG_PATH}",
    )
    parser.add_argument(
        "--run-date", type=_date, default=None,
        help="Run day R (Europe/Istanbul calendar date); default: today.",
    )
    parser.add_argument(
        "--format", choices=("text", "json"), default="text",
    )
    return parser


def _code_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True,
            text=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def _display_path(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def build_run_record_skeleton(
    *, run_date: date, config_path: Path, config_hash: str, guards, plan,
) -> dict[str, object]:
    """The ``fintr_daily_collection_run_v1`` skeleton this dry-run prints.

    ``status: "planned"`` throughout -- this is what a real run would start
    from, not a completed run record; no financial value, brokerage name,
    ``initData`` or URL is ever included (matches
    ``validate_context_contains_no_secrets``'s scope, reused unmodified by
    the executor PR, not re-implemented here since this PR opens no
    network and captures nothing).
    """
    run_id = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    symbols = []
    for entry in plan.symbols:
        window = None
        if entry.window is not None:
            window = {
                "start": entry.window.start.isoformat(),
                "end": entry.window.end.isoformat(),
                "catch_up": entry.window.catch_up,
            }
        symbols.append({
            "symbol": entry.symbol,
            "role": entry.role,
            "status": entry.status,
            "window": window,
            "capture_path": None,
            "capture_packet_sha256": None,
            "frames_used": 0,
            "diagnostics": [],
            "unknown_event_types_count": 0,
        })
    return {
        "schema": RUN_RECORD_SCHEMA,
        "run_id": run_id,
        "started_at_utc": run_id,
        "finished_at_utc": None,
        "run_date_local": run_date.isoformat(),
        "timezone": "Europe/Istanbul",
        "git_commit": _code_commit(),
        "git_tree_clean": None,
        "config_path": _display_path(config_path),
        "config_sha256": config_hash,
        "split_config_sha256": guards.split.split_sha256,
        "exclusions_sha256": guards.exclusions.file_sha256,
        "environment": None,
        "init_data": {"prompted": False, "age_seconds_bucket": None},
        "symbols": symbols,
        "rate_limit": {"http_429_count": 0, "stopped_on_429": False},
        "price_history": {"status": "planned", "files": []},
        "notes": "dry-run plan only; no network opened, no initData prompted",
        "status": "planned",
    }


def _print_text(plan, run_record: dict[str, object]) -> None:
    print("Dry run: no connection opened, no initData prompted.")
    print(
        f"Run date (R): {plan.run_date.isoformat()}; window end (E=R-{plan.end_offset_days}d): "
        f"{plan.window_end.isoformat()}; lookback: {plan.lookback_calendar_days}d; "
        f"max span: {plan.max_span_days}d"
    )
    print(f"{'Symbol':<10}{'Role':<14}{'Status':<28}Window")
    for entry in plan.symbols:
        window_text = ""
        if entry.window is not None:
            window_text = (
                f"{entry.window.start.isoformat()}..{entry.window.end.isoformat()}"
                + (" (catch_up)" if entry.window.catch_up else "")
            )
        print(f"{entry.symbol:<10}{entry.role:<14}{entry.status:<28}{window_text}")
    print("")
    print("Run record skeleton (fintr_daily_collection_run_v1, status=planned):")
    print(json.dumps(run_record, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if not args.dry_run:
        parser.error(
            "--dry-run olmadan çalıştırma henüz uygulanmadı: bu PR yalnız planlayıcıyı "
            "içerir; ağ toplayıcı (initData istemi ve WebSocket bağlantısı) ayrı bir "
            "PR'da eklenecek (ADR 'Uygulama sırası' madde 2)"
        )

    run_date = args.run_date if args.run_date is not None else date.today()

    try:
        config = load_daily_collection_config(args.config)
    except CollectionPlannerError as exc:
        print(f"Plan failed: {exc}", file=sys.stderr)
        return 1

    try:
        guards = load_active_guards()
    except SymbolGuardError as exc:
        print(f"Plan failed: {exc}", file=sys.stderr)
        return 1

    run_records_dir = PROJECT_ROOT / config.capture_root / RUN_RECORDS_SUBDIR
    last_success = scan_last_success(run_records_dir)

    plan = plan_daily_collection(
        run_date=run_date, guards=guards, last_success_by_symbol=last_success,
        end_offset_days=config.end_offset_days,
        lookback_calendar_days=config.lookback_calendar_days,
        max_span_days=config.max_span_days,
    )

    run_record = build_run_record_skeleton(
        run_date=run_date, config_path=args.config,
        config_hash=config_sha256(args.config), guards=guards, plan=plan,
    )

    if args.format == "json":
        print(json.dumps(run_record, ensure_ascii=False, indent=2))
    else:
        _print_text(plan, run_record)

    blocked = plan.blocked()
    if blocked:
        symbols = ", ".join(entry.symbol for entry in blocked)
        print(
            f"Plan requires a decision: {symbols} exceed the {plan.max_span_days}-day "
            "max span (no large-range override exists in this PR)",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
