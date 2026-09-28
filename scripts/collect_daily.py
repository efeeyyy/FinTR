"""CLI: plan and run the daily incremental Mini App collection.

``--dry-run`` prints the per-symbol window plan and a
``fintr_daily_collection_run_v1`` run-record skeleton (``status:
"planned"``) to stdout; it never opens a network connection, never prompts
for ``initData``, and never writes to ``data/interim/daily_capture/``.

Without ``--dry-run`` (and with ``--allow-session-takeover``), the live
executor collects the planner's output through
``scripts.live_collector_smoke._collect_multi_symbol`` -- the same
per-symbol capture flow the manual smoke tool uses, reused rather than
rewritten (``docs/research/daily_miniapp_collection_adr.md`` "Uygulama
sırası" madde 2) -- and writes one ``fintr_daily_collection_run_v1`` record
to ``<capture_root>/_runs/<run_id>.json``.

See ``docs/research/daily_miniapp_collection_adr.md`` for the full design
and ``src/fintr/collection/planner.py`` for the pure planning logic.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from time import monotonic
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fintr.collection.planner import (
    RUN_RECORD_SCHEMA,
    CollectionPlannerError,
    DailyCollectionConfig,
    DailyCollectionPlan,
    config_sha256,
    load_daily_collection_config,
    plan_daily_collection,
    scan_last_success,
)
from fintr.evaluation.live_capture import LivePilotCaptureError, validate_context_contains_no_secrets
from fintr.evaluation.symbol_guard import ActiveGuards, SymbolGuardError, load_active_guards


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = Path("configs/collection/daily_miniapp_v1.json")
RUN_RECORDS_SUBDIR = "_runs"
_ISTANBUL_FIXED_OFFSET_HOURS = 3  # Türkiye: UTC+3 year-round since 2016, no DST.


def _load_live_collector_smoke():
    """Load ``live_collector_smoke.py`` the way this repo's tests already do.

    ``scripts/`` is not a package, so this can't be a normal ``import``; a
    plain ``importlib`` load (as opposed to inserting ``scripts/`` into
    ``sys.path``) keeps this script's import behavior identical whether it
    is run directly or loaded by a test via
    ``importlib.util.spec_from_file_location`` (see
    ``tests/test_daily_collection_planner.py``'s own ``_load_script``).
    """
    spec = importlib.util.spec_from_file_location(
        "fintr_scripts_live_collector_smoke",
        Path(__file__).resolve().parent / "live_collector_smoke.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load_live_collector_smoke()


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _istanbul_today(*, now: datetime | None = None) -> date:
    """Run day R (ADR §7): the Europe/Istanbul calendar date, never the host's.

    ``now`` is UTC-or-aware and injectable for tests; production callers
    omit it. On a host without the IANA tzdata (observed on some Windows
    installs missing the ``tzdata`` pip package -- Windows itself ships no
    system tz database for ``zoneinfo`` to read), this falls back to a
    fixed UTC+3 offset, which is exactly Türkiye's actual offset (no DST
    since 2016) -- not an approximation. Efe's runbook documents installing
    ``tzdata`` as the primary fix; this fallback is a safety net, not a
    substitute for it.
    """
    now = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    try:
        tzinfo = ZoneInfo("Europe/Istanbul")
    except ZoneInfoNotFoundError:
        tzinfo = timezone(timedelta(hours=_ISTANBUL_FIXED_OFFSET_HOURS))
    return now.astimezone(tzinfo).date()


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
             "or prompts for initData.",
    )
    parser.add_argument(
        "--allow-session-takeover", action="store_true",
        help=(
            "Acknowledge that a live run may close the active Telegram Mini App "
            "session. Required for every live (non-dry-run) run."
        ),
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


def _git_tree_clean() -> bool | None:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"], cwd=PROJECT_ROOT, capture_output=True,
            text=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() == ""


def _websockets_version() -> str:
    try:
        return importlib.metadata.version("websockets")
    except importlib.metadata.PackageNotFoundError:
        return "not_installed"


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


def _write_run_record(run_record: dict[str, object], run_records_dir: Path) -> Path:
    run_records_dir.mkdir(parents=True, exist_ok=True)
    filename = str(run_record["run_id"]).replace(":", "-") + ".json"
    target = run_records_dir / filename
    payload = json.dumps(run_record, ensure_ascii=False, indent=2) + "\n"
    temporary_path: Path | None = None
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=".daily-collection-run-", suffix=".tmp",
        dir=run_records_dir, delete=False,
    ) as output:
        temporary_path = Path(output.name)
        output.write(payload)
    os.replace(temporary_path, target)
    return target


def _daily_collection_metadata(
    *, config: DailyCollectionConfig, config_path: Path, run_id: str, run_date: date,
) -> dict[str, object]:
    """ADR "Şemalar" §"Kayan kayıt capture_metadata eki"."""
    return {
        "source": {
            "source_id": config.source_id,
            "protocol_reference": "docs/protocol_inventory.md",
            "collection_mode": "daily_rolling_window_v1",
        },
        "permission_provenance": {
            "status": config.permission_status,
            "basis": "Efe beyanı 2026-09-28: Mini App lisansı var, veri çekimi ücretsiz",
            "record": "docs/research/continuous_data_sources.md#karar-2026-09-28",
            "independently_verified": False,
        },
        "run": {
            "run_id": run_id,
            "run_date_local": run_date.isoformat(),
            "window_rule": "E=R-1; S=E-14d; catch-up from last successful end",
            "config_sha256": config_sha256(config_path),
            "git_commit": _code_commit(),
            "git_tree_clean": _git_tree_clean(),
        },
    }


async def _run_live(
    args: argparse.Namespace,
    *,
    config: DailyCollectionConfig,
    guards: ActiveGuards,
    plan: DailyCollectionPlan,
    run_date: date,
    run_records_dir: Path,
) -> int:
    """The live executor (ADR "Uygulama sırası" madde 2): one run record.

    Collects every ``planned`` symbol by grouping them by identical
    (start, end) window -- almost always all of them, since catch-up only
    widens a window for a symbol whose last successful run fell behind --
    and calling ``live_collector_smoke._collect_multi_symbol`` once per
    group, unmodified in its own per-symbol behavior. A group past its
    turn because an earlier group hit the wall-time guard, a 429, or the
    session-failure pattern gets every remaining symbol marked with the
    matching ``skipped_*`` status; nothing is silently dropped.
    """
    started_at = datetime.now(timezone.utc)
    run_id = started_at.isoformat().replace("+00:00", "Z")
    capture_root = PROJECT_ROOT / config.capture_root
    collector_raw = config.raw.get("collector", {})
    max_events = int(collector_raw.get("max_frames", 8))
    timeout_seconds = float(collector_raw.get("timeout_seconds", 5.0))
    max_message_mib = int(collector_raw.get("max_message_mib", 1))
    stop_on_http_status = frozenset(config.stop_on_http_status)
    daily_collection_metadata = _daily_collection_metadata(
        config=config, config_path=args.config, run_id=run_id, run_date=run_date,
    )

    print(smoke.SESSION_TAKEOVER_WARNING)
    secret_provider = smoke._CachedSecretProvider(smoke.InteractiveSecretProvider())
    results_sink: list = []

    planned = plan.planned()
    windows: dict[tuple[date, date], list[str]] = {}
    for entry in planned:
        windows.setdefault((entry.window.start, entry.window.end), []).append(entry.symbol)
    # The main (non-catch-up) batch, almost always everyone, goes first.
    ordered_windows = sorted(
        windows.items(), key=lambda item: (-len(item[1]), item[0][0], item[0][1]),
    )

    run_started = monotonic()
    stopped_reason: str | None = None
    attempted_groups: set[tuple[date, date]] = set()
    for index, ((start, end), symbols) in enumerate(ordered_windows):
        if stopped_reason is not None:
            break
        remaining_budget = config.max_wall_seconds - (monotonic() - run_started)
        if remaining_budget <= 0:
            print(
                f"Stopping: total wall-time guard ({config.max_wall_seconds}s) reached "
                f"before window {start.isoformat()}..{end.isoformat()}; "
                f"{len(symbols)} symbol(s) not attempted."
            )
            for symbol in symbols:
                results_sink.append(smoke.SymbolCollectionResult(
                    symbol, "skipped_wall_time_guard", 0, None, None, (), 0,
                ))
            continue
        if index > 0:
            await asyncio.sleep(config.symbol_pause_seconds)
        group_args = argparse.Namespace(
            max_events=max_events, timeout=timeout_seconds, max_message_mib=max_message_mib,
            symbol_pause_seconds=config.symbol_pause_seconds,
            max_wall_seconds=remaining_budget,
            end_date=end, to_date=None, custody_date=None,
        )
        sink_before = len(results_sink)
        code = await smoke._collect_multi_symbol(
            group_args, tuple(symbols), start, end, run_date,
            capture_root=capture_root,
            daily_collection_metadata=daily_collection_metadata,
            stop_on_http_status=stop_on_http_status,
            fail_closed_on_unknown_events=True,
            secret_provider=secret_provider,
            results_sink=results_sink,
        )
        attempted_groups.add((start, end))
        if code == 1:
            new_entries = results_sink[sink_before:]
            stopped_reason = (
                "rate_limited"
                if any(entry.status == "skipped_rate_limited" for entry in new_entries)
                else "session_failure"
            )

    if stopped_reason is not None:
        skip_status = (
            "skipped_rate_limited" if stopped_reason == "rate_limited"
            else "skipped_session_failure_stop"
        )
        for (start, end), symbols in ordered_windows:
            if (start, end) in attempted_groups:
                continue
            for symbol in symbols:
                results_sink.append(smoke.SymbolCollectionResult(
                    symbol, skip_status, 0, None, None, (), 0,
                ))

    sink_by_symbol = {result.symbol: result for result in results_sink}
    symbols_out: list[dict[str, object]] = []
    for entry in plan.symbols:
        window = None
        if entry.window is not None:
            window = {
                "start": entry.window.start.isoformat(),
                "end": entry.window.end.isoformat(),
                "catch_up": entry.window.catch_up,
            }
        result = sink_by_symbol.get(entry.symbol)
        if result is None:
            symbols_out.append({
                "symbol": entry.symbol, "role": entry.role, "window": window,
                "status": entry.status, "capture_path": None, "capture_packet_sha256": None,
                "frames_used": 0, "diagnostics": [], "unknown_event_types_count": 0,
            })
        else:
            symbols_out.append({
                "symbol": entry.symbol, "role": entry.role, "window": window,
                "status": result.status, "capture_path": result.capture_path,
                "capture_packet_sha256": result.capture_packet_sha256,
                "frames_used": result.frames_used,
                "diagnostics": [dict(diagnostic) for diagnostic in result.diagnostics],
                "unknown_event_types_count": result.unknown_event_types_count,
            })

    rate_limit_categories = {f"handshake_rejected_http_{code}" for code in stop_on_http_status}
    http_429_count = sum(
        1 for result in results_sink for diagnostic in result.diagnostics
        if diagnostic.get("category") in rate_limit_categories
    )
    stopped_on_429 = any(result.status == "skipped_rate_limited" for result in results_sink)

    init_data_prompted = secret_provider.was_prompted
    init_data_accepted = (
        init_data_prompted and any(result.frames_used > 0 for result in results_sink)
    )
    finished_at = datetime.now(timezone.utc)
    run_record: dict[str, object] = {
        "schema": RUN_RECORD_SCHEMA,
        "run_id": run_id,
        "started_at_utc": run_id,
        "finished_at_utc": finished_at.isoformat().replace("+00:00", "Z"),
        "run_date_local": run_date.isoformat(),
        "timezone": "Europe/Istanbul",
        "git_commit": _code_commit(),
        "git_tree_clean": _git_tree_clean(),
        "config_path": _display_path(args.config),
        "config_sha256": config_sha256(args.config),
        "split_config_sha256": guards.split.split_sha256,
        "exclusions_sha256": guards.exclusions.file_sha256,
        "environment": {
            "python": platform.python_version(),
            "websockets": _websockets_version(),
            "os": platform.platform(),
        },
        "init_data": {
            "prompted": init_data_prompted,
            "age_seconds_bucket": (
                secret_provider.age_seconds_bucket() if init_data_prompted else None
            ),
            "accepted": init_data_accepted if init_data_prompted else None,
        },
        "symbols": symbols_out,
        "rate_limit": {"http_429_count": http_429_count, "stopped_on_429": stopped_on_429},
        "price_history": {"status": "skipped", "files": []},
        "notes": (
            "period-analysis capture only; price history not yet implemented "
            "(ADR 'Uygulama sırası' madde 5); only fixed categories, no financial value"
        ),
        "status": "completed",
    }

    try:
        validate_context_contains_no_secrets(run_record)
    except LivePilotCaptureError as exc:
        print(f"Run record failed the secret/financial-value check ({exc}); not written.",
              file=sys.stderr)
        return 1

    run_record_path = _write_run_record(run_record, run_records_dir)
    print(f"Run record: {_display_path(run_record_path)}")

    saved = sum(1 for entry in symbols_out if entry["status"] == "saved")
    failed = sum(1 for entry in symbols_out if str(entry["status"]).startswith("failed"))
    print(f"Symbols: {saved} saved, {failed} failed, {len(symbols_out)} total.")
    return 1 if stopped_reason is not None or failed > 0 else 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if not args.dry_run and not args.allow_session_takeover:
        parser.error(
            "canlı çalıştırma (--dry-run olmadan) --allow-session-takeover ister: "
            "bu bağlantı Telegram Mini App oturumunu devralabilir; onay olmadan "
            "initData istemi yapılmaz, bağlantı açılmaz."
        )

    run_date = args.run_date if args.run_date is not None else _istanbul_today()

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

    if args.dry_run:
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

    if args.dry_run:
        return 0

    return asyncio.run(_run_live(
        args, config=config, guards=guards, plan=plan, run_date=run_date,
        run_records_dir=run_records_dir,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
