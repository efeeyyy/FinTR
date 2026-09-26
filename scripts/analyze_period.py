"""Deterministic offline report for one saved period-analysis capture."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile

from fintr.data.period_capture import PeriodCaptureError, load_period_capture
from fintr.data.price_history import PriceHistoryCaptureError, load_price_history_capture
from fintr.features.period import (
    DEFAULT_CUSTODY_FLOAT_TOLERANCE,
    DEFAULT_PROVISIONAL_LAST_CUSTODY_DAYS,
)
from fintr.features.price_join import (
    DEFAULT_ADJUSTMENT_RATIO_EPSILON,
    DEFAULT_RATIO_REGIME_TOLERANCE,
    DEFAULT_VOLUME_RATIO_TOLERANCE,
)
from fintr.inference.period_report import build_period_report


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LAGS = "0,1,2,3"
DEFAULT_DETAIL_LAG = 2
DEFAULT_TOP_N = 10
DEFAULT_BATCH_ROOT = Path("data/interim/period_analysis")
DEFAULT_PRICE_HISTORY_ROOT = Path("data/interim/price_history")

# <SYMBOL>/<YYYY-MM-DD>_<YYYY-MM-DD>.json only; *.report.json, temp files
# (e.g. the ".period-report-*.tmp" files _save() briefly creates) and any
# other name are ignored by discovery.
_CAPTURE_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.json$")
_PRICE_CAPTURE_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json$")


def _lags(value: str) -> tuple[int, ...]:
    try:
        lags = tuple(int(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated integers") from exc
    if not lags or any(lag < 0 for lag in lags):
        raise argparse.ArgumentTypeError("lags must be non-negative")
    return lags


def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected non-negative integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("expected non-negative integer")
    return number


def _positive_int(value: str) -> int:
    number = _non_negative_int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected positive integer")
    return number


def _non_negative_decimal(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("expected a decimal number") from exc
    if not number.is_finite() or number < 0:
        raise argparse.ArgumentTypeError("expected a non-negative finite decimal number")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze a saved period capture offline; no network, no model call.",
    )
    parser.add_argument("capture", type=Path, nargs="?", default=None,
                        help="data/interim/period_analysis/<SYM>/<S>_<E>.json; omit when using --all")
    parser.add_argument("--all", dest="all_root", type=Path, nargs="?",
                        const=DEFAULT_BATCH_ROOT, default=None, metavar="ROOT",
                        help="batch mode: process every capture found under ROOT "
                             f"(default: {DEFAULT_BATCH_ROOT}); mutually exclusive with capture")
    parser.add_argument("--lags", type=_lags, default=_lags(DEFAULT_LAGS),
                        help=f"observation lags to test (default: {DEFAULT_LAGS})")
    parser.add_argument("--detail-lag", type=_non_negative_int, default=DEFAULT_DETAIL_LAG,
                        help=f"lag whose unmatched rows are listed (default: {DEFAULT_DETAIL_LAG})")
    parser.add_argument("--top", type=_positive_int, default=DEFAULT_TOP_N,
                        help=f"rows per ranked list (default: {DEFAULT_TOP_N})")
    parser.add_argument(
        "--price-history", type=Path, default=None, metavar="PRICE_HISTORY_JSON",
        help="single-capture mode only: a saved fintr_price_history_capture_v1 "
             "file to join against the capture's server dates for the "
             "report's price_volume section. In --all mode, a matching file "
             "is auto-discovered instead; see --price-history-root.",
    )
    parser.add_argument(
        "--price-history-root", type=Path, default=DEFAULT_PRICE_HISTORY_ROOT, metavar="ROOT",
        help="--all mode only: root under which price-history captures are "
             f"auto-discovered (default: {DEFAULT_PRICE_HISTORY_ROOT})",
    )
    parser.add_argument(
        "--volume-ratio-tolerance", type=_non_negative_decimal,
        default=DEFAULT_VOLUME_RATIO_TOLERANCE,
        help="price_volume: allowed relative deviation of "
             f"implied_traded_lots/volume from 1 (default: {DEFAULT_VOLUME_RATIO_TOLERANCE})",
    )
    parser.add_argument(
        "--adjustment-ratio-epsilon", type=_non_negative_decimal,
        default=DEFAULT_ADJUSTMENT_RATIO_EPSILON,
        help="price_volume: relative adjusted_close/close change that flags "
             f"a corporate-action boundary date (default: {DEFAULT_ADJUSTMENT_RATIO_EPSILON})",
    )
    parser.add_argument(
        "--ratio-regime-tolerance", type=_non_negative_decimal,
        default=DEFAULT_RATIO_REGIME_TOLERANCE,
        help="price_volume: relative tolerance for volume_ratio matching a "
             "constant 1/k (probable_retroactive_price_adjustment hypothesis; "
             f"default: {DEFAULT_RATIO_REGIME_TOLERANCE})",
    )
    parser.add_argument(
        "--custody-float-tolerance", type=_non_negative_decimal,
        default=DEFAULT_CUSTODY_FLOAT_TOLERANCE,
        help="custodian_diff_check / lagged_akd_custody_match: absolute "
             "tolerance (lot_count units) that separates float-artifact "
             f"provider noise from a real inconsistency (default: "
             f"{DEFAULT_CUSTODY_FLOAT_TOLERANCE})",
    )
    parser.add_argument(
        "--provisional-last-custody-days", type=_non_negative_int,
        default=DEFAULT_PROVISIONAL_LAST_CUSTODY_DAYS,
        help="number of trailing server dates whose daily custody balance is "
             "policy-treated as provisional (observed to be revised between "
             f"two captures); excluded from lag matching counts (default: "
             f"{DEFAULT_PROVISIONAL_LAST_CUSTODY_DAYS})",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--save", action="store_true",
                        help="also write <capture>.report.json next to each capture; never "
                             "overwrites. In --all mode also (over)writes <root>/index.json.")
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
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.name


def _run_metadata(
    capture_path: Path, capture_bytes: bytes, *, lags: tuple[int, ...], detail_lag: int, top: int,
    volume_ratio_tolerance: Decimal, adjustment_ratio_epsilon: Decimal,
    ratio_regime_tolerance: Decimal, custody_float_tolerance: Decimal,
    provisional_last_custody_days: int,
) -> dict[str, object]:
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "input_file": _display_path(capture_path),
        "input_file_sha256": hashlib.sha256(capture_bytes).hexdigest(),
        "code_commit": _code_commit(),
        "python": platform.python_version(),
        "parameters": {
            "lags": list(lags), "detail_lag": detail_lag, "top": top,
            "volume_ratio_tolerance": str(volume_ratio_tolerance),
            "adjustment_ratio_epsilon": str(adjustment_ratio_epsilon),
            "ratio_regime_tolerance": str(ratio_regime_tolerance),
            "custody_float_tolerance": str(custody_float_tolerance),
            "provisional_last_custody_days": provisional_last_custody_days,
        },
    }


_DIRECTION_TR = {"positive": "pozitif", "negative": "negatif", "zero": "sıfır"}


def _shown(value: str | None) -> str:
    return "listelenmemiş" if value is None else value


def _coded(item: dict) -> str:
    """Code, plus its resolved short_title in parentheses; never a guessed name."""
    label = item.get("label")
    if isinstance(label, dict) and label.get("status") == "resolved" and label.get("short_title"):
        return f"{item['code']} ({label['short_title']})"
    return item["code"]


def render_text(report: dict) -> str:
    """Short Turkish summary; every number comes from the report."""
    scope = report["scope"]
    lines = [
        f"{scope['symbol']} {scope['requested_start']}..{scope['requested_end']} "
        f"({scope['server_date_count']} işlem gözlemi; birimler sağlayıcının verdiği gibi)",
        "",
        "AKD mutabakatı (günlük toplam = dönem neti): "
        + ", ".join(f"{k}={v}" for k, v in report["akd_reconciliation"]["status_counts"].items()),
    ]
    diff = report["custodian_diff_check"]
    lines.append(
        "Takas farkı iç tutarlılık hatası: "
        + (", ".join(_coded(row) for row in diff["internally_inconsistent_codes"]) or "yok")
        + f"; günlük panel aralığı hizalı: {'evet' if diff['range_aligned_with_diff'] else 'hayır'}"
        + f"; provizyonel (henüz kesinleşmemiş) son gün sayısı="
        f"{diff['provisional_last_custody_days']} ({', '.join(diff['provisional_dates']) or 'yok'})"
    )
    for item in diff["exceptions"]:
        lines.append(
            f"  - {_coded(item)}: ilk {item['first_value']} / panel "
            f"{_shown(item['daily_first_value'])} ({item['first_status']}); son "
            f"{item['last_value']} / panel {_shown(item['daily_last_value'])} "
            f"({item['last_status']})"
        )
    lines += ["", "Dönem AKD neti (en yüksek / en düşük):"]
    net = report["period_akd_net"]
    lines += [f"  + {_coded(row)}: {row['net']}" for row in net["top_positive"]]
    lines += [f"  - {_coded(row)}: {row['net']}" for row in net["top_negative"]]
    lines += ["", "Takas dönem farkı (en büyük artış / azalış):"]
    custody = report["custodian_diff"]
    lines += [f"  + {_coded(row)}: {row['difference']}" for row in custody["largest_increases"]]
    lines += [f"  - {_coded(row)}: {row['difference']}" for row in custody["largest_decreases"]]
    lines += ["", "Kurum akış dizileri (aynı işaretli ardışık günler):"]
    for item in report["broker_flow_runs"]["codes"]:
        runs = "; ".join(
            f"{run['start'][5:]}..{run['end'][5:]} {_DIRECTION_TR[run['direction']]} "
            f"{run['net_sum']}"
            for run in item["runs"]
        )
        lines.append(f"  {_coded(item)} toplam {item['listed_sum']} ({item['listed_days']} gün): {runs}")
    lag = report["lagged_akd_custody_match"]
    lines += ["", "AKD neti ile sonraki takas değişimi eşleşmesi:"]
    lines += [
        f"  gecikme {row['lag']}: {row['exact_matches']}/{row['comparable']} birebir, "
        f"{row['sign_matches']}/{row['comparable']} aynı işaret, "
        f"{row['float_artifact_matches']} ondalık gürültü toleransı içinde, "
        f"{row['excluded_provisional_count']} provizyonel gün nedeniyle hariç"
        for row in lag["summaries"]
    ]
    lines.append(
        f"  gecikme {lag['detail_lag']} için takas karşılığı henüz olmayan AKD günleri: "
        + (", ".join(lag["akd_dates_without_later_custody"]) or "yok")
    )
    lines.append(
        f"  provizyonel takas günleri (son {lag['provisional_last_custody_days']} sunucu "
        "tarihi, gecikme eşleşmesinden hariç): "
        + (", ".join(lag["provisional_custody_dates"]) or "yok")
    )
    proxy_summary = report["daily_market_proxy"]["check_summary"]
    lines += ["", "Günlük piyasa proxy kontrolleri (resmi fiyat/hacim serisi değildir): " + ", ".join(
        f"{name}={summary['passed_days']}/{summary['evaluated_days']} geçti"
        for name, summary in proxy_summary.items()
    )]
    days = report["provider_metadata"]["days"]
    if days:
        lines += ["", "Sağlayıcı retail_pct (yöntemi doğrulanmamış): " + ", ".join(
            f"{day['date'][5:]}={day['retail_pct'] if day['retail_pct'] is not None else 'null'}"
            for day in days
        )]
    field_coverage = report["provider_metadata"]["field_coverage"]
    if field_coverage:
        lines += ["", "Sağlayıcı metadata alan kapsamı (yöntem doğrulanmamış): " + ", ".join(
            f"{row['field']}={row['non_null_count']}/{row['day_count']}"
            + (" (hep null)" if row["all_null"] else "")
            for row in field_coverage
        )]
    price_volume = report["price_volume"]
    if price_volume["status"] == "joined":
        checks = price_volume["check_summary"]
        lines += ["", "Fiyat/hacim eşleşme kontrolleri (resmi olmayan kaynak): " + ", ".join(
            f"{name}={summary['passed_days']}/{summary['evaluated_days']} geçti"
            for name, summary in checks.items()
        ) + (
            f"; hariç tutulan placeholder gün={len(price_volume['excluded_placeholder_dates'])}"
            f", düzeltme sınırı gün={len(price_volume['adjustment_boundary_dates'])}"
            f", olası geçmişe dönük fiyat düzeltmesi (hipotez) gün="
            f"{price_volume['probable_retroactive_price_adjustment_days']}"
            f" ({len(price_volume['ratio_regimes'])} rejim)"
        )]
    elif price_volume["status"] == "load_failed":
        lines += ["", "Fiyat/hacim verisi: bulunan dosya yüklenemedi"]
    else:
        lines += ["", "Fiyat/hacim verisi: mevcut değil"]
    lines += ["", "Sınırlar:"] + [f"  - {text}" for text in report["constraints"]]
    return "\n".join(lines) + "\n"


def _save(capture_path: Path, payload: bytes) -> Path:
    target = capture_path.with_name(capture_path.stem + ".report.json")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".period-report-", suffix=".tmp",
            dir=target.parent, delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def discover_captures(root: Path) -> list[Path]:
    """<root>/<SYMBOL>/<YYYY-MM-DD>_<YYYY-MM-DD>.json files, sorted by symbol then date.

    Anything else under root (``*.report.json``, ``index.json``, temp files,
    stray non-matching names) is ignored, not reported as an error.
    """
    if not root.is_dir():
        return []
    found: list[Path] = []
    for symbol_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for entry in sorted(symbol_dir.iterdir()):
            if entry.is_file() and _CAPTURE_NAME_RE.match(entry.name):
                found.append(entry)
    return found


def discover_price_history(root: Path, symbol: str, start: date, end: date) -> Path | None:
    """Best covering saved price-history file for one capture's symbol/period.

    Looks under ``root/<symbol>/`` for ``<S>_<E>.json`` files (same name
    shape as a period capture) whose own ``[S, E]`` range covers (is a
    superset, inclusive, of) the capture's ``[start, end]``. Among covering
    files the one with the smallest total day span is chosen -- an exact
    ``[start, end]`` match always has the minimum possible span, so it wins
    automatically; ties break on the lexicographically smallest filename. No
    covering file under ``root/<symbol>/`` (including a missing directory)
    returns ``None``.
    """
    symbol_dir = root / symbol
    if not symbol_dir.is_dir():
        return None
    candidates: list[tuple[int, str, Path]] = []
    for entry in sorted(symbol_dir.iterdir()):
        if not entry.is_file():
            continue
        match = _PRICE_CAPTURE_NAME_RE.match(entry.name)
        if not match:
            continue
        file_start = date.fromisoformat(match.group(1))
        file_end = date.fromisoformat(match.group(2))
        if file_start > file_end or file_start > start or file_end < end:
            continue
        candidates.append(((file_end - file_start).days, entry.name, entry))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _process_capture(path: Path, args: argparse.Namespace) -> dict[str, object]:
    """Load, validate and (optionally) save the report for one capture.

    Never raises: a validation failure is returned as a ``failed`` entry so
    the caller can keep processing the rest of the batch.
    """
    entry: dict[str, object] = {
        "symbol": path.parent.name,
        "path": _display_path(path),
        "status": "failed",
        "error": None,
        "period_start": None,
        "period_end": None,
        "server_date_count": None,
        "schema_version": None,
        "akd_reconciliation_status_counts": None,
        "custodian_diff_exception_count": None,
        "lag_summaries": None,
        "brokerage_directory_present": None,
        "proxy_checks_passed": None,
        "price_history_found": False,
        "price_volume_checks_passed": None,
        "report": None,
    }
    try:
        capture_bytes = path.read_bytes()
        capture = load_period_capture(path)

        price_capture = None
        price_load_error: str | None = None
        price_path = discover_price_history(
            args.price_history_root, capture.symbol, capture.start, capture.end,
        )
        if price_path is not None:
            try:
                price_capture = load_price_history_capture(price_path)
            except PriceHistoryCaptureError as exc:
                price_load_error = str(exc)

        report = build_period_report(
            capture, lags=args.lags, detail_lag=args.detail_lag, top_n=args.top,
            price=price_capture,
            volume_ratio_tolerance=args.volume_ratio_tolerance,
            adjustment_ratio_epsilon=args.adjustment_ratio_epsilon,
            ratio_regime_tolerance=args.ratio_regime_tolerance,
            custody_float_tolerance=args.custody_float_tolerance,
            provisional_last_custody_days=args.provisional_last_custody_days,
        )
    except (OSError, PeriodCaptureError, ValueError) as exc:
        entry["error"] = str(exc)
        return entry

    if price_load_error is not None:
        report["price_volume"] = {"status": "load_failed", "error": price_load_error}

    entry.update({
        "status": "ok",
        "period_start": report["scope"]["requested_start"],
        "period_end": report["scope"]["requested_end"],
        "server_date_count": report["scope"]["server_date_count"],
        "schema_version": "v2" if capture.brokerage_directory.present else "v1",
        "akd_reconciliation_status_counts": report["akd_reconciliation"]["status_counts"],
        "custodian_diff_exception_count": len(report["custodian_diff_check"]["exceptions"]),
        "lag_summaries": report["lagged_akd_custody_match"]["summaries"],
        "brokerage_directory_present": capture.brokerage_directory.present,
        "proxy_checks_passed": all(
            summary["all_evaluated_days_passed"]
            for summary in report["daily_market_proxy"]["check_summary"].values()
        ),
        "price_history_found": report["price_volume"]["status"] == "joined",
        "price_volume_checks_passed": (
            all(
                summary["all_evaluated_days_passed"]
                for summary in report["price_volume"]["check_summary"].values()
            )
            if report["price_volume"]["status"] == "joined" else None
        ),
    })

    if not args.save:
        entry["report"] = "not_saved"
        return entry

    report["run"] = _run_metadata(
        path, capture_bytes, lags=args.lags, detail_lag=args.detail_lag, top=args.top,
        volume_ratio_tolerance=args.volume_ratio_tolerance,
        adjustment_ratio_epsilon=args.adjustment_ratio_epsilon,
        ratio_regime_tolerance=args.ratio_regime_tolerance,
        custody_float_tolerance=args.custody_float_tolerance,
        provisional_last_custody_days=args.provisional_last_custody_days,
    )
    payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        _save(path, payload)
        entry["report"] = "saved"
    except FileExistsError:
        entry["report"] = "skipped"
    except OSError:
        # The capture itself validated fine; only writing its report failed
        # (e.g. permissions), so "status" stays "ok" and only "report" flags it.
        entry["report"] = "failed"
        entry["error"] = "report_could_not_be_saved"
    return entry


_REPORT_STATUS_TR = {
    "saved": "kaydedildi", "skipped": "atlandı (mevcut)",
    "not_saved": "kaydedilmedi (--save yok)", "failed": "kaydedilemedi",
}


def build_batch_index(root: Path, entries: list[dict[str, object]], args: argparse.Namespace) -> dict:
    """Compact cross-capture index; every number comes from build_period_report."""
    ok = sum(1 for e in entries if e["status"] == "ok")
    failed = len(entries) - ok
    return {
        "schema": "fintr_period_batch_index_v1",
        "root": _display_path(root),
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "parameters": {
            "lags": list(args.lags), "detail_lag": args.detail_lag, "top": args.top,
            "price_history_root": _display_path(args.price_history_root),
            "volume_ratio_tolerance": str(args.volume_ratio_tolerance),
            "adjustment_ratio_epsilon": str(args.adjustment_ratio_epsilon),
            "ratio_regime_tolerance": str(args.ratio_regime_tolerance),
            "custody_float_tolerance": str(args.custody_float_tolerance),
            "provisional_last_custody_days": args.provisional_last_custody_days,
        },
        "captures": entries,
        "totals": {
            "captures": len(entries),
            "ok": ok,
            "failed": failed,
            "reports_saved": sum(1 for e in entries if e["report"] == "saved"),
            "reports_skipped": sum(1 for e in entries if e["report"] == "skipped"),
            "reports_not_saved": sum(1 for e in entries if e["report"] == "not_saved"),
            "reports_failed": sum(1 for e in entries if e["report"] == "failed"),
        },
    }


def render_index_text(index: dict) -> str:
    totals = index["totals"]
    lines = [
        f"Toplu dönem analizi: {index['root']} ({totals['captures']} kayıt)",
        f"Başarılı: {totals['ok']}, başarısız: {totals['failed']}; "
        f"rapor kaydedildi: {totals['reports_saved']}, "
        f"atlandı (mevcut): {totals['reports_skipped']}, "
        f"kaydedilmedi (--save yok): {totals['reports_not_saved']}, "
        f"kaydedilemedi: {totals['reports_failed']}",
        "",
    ]
    for entry in index["captures"]:
        if entry["status"] != "ok":
            lines.append(f"- {entry['symbol']} ({entry['path']}): HATA - {entry['error']}")
            continue
        akd = ", ".join(f"{k}={v}" for k, v in entry["akd_reconciliation_status_counts"].items())
        lag = "; ".join(
            f"L{row['lag']} {row['exact_matches']}/{row['comparable']} birebir, "
            f"{row['sign_matches']}/{row['comparable']} aynı işaret"
            for row in entry["lag_summaries"]
        )
        if entry["price_volume_checks_passed"] is None:
            price_checks_text = "n/a"
        elif entry["price_volume_checks_passed"]:
            price_checks_text = "evet"
        else:
            price_checks_text = "hayır"
        lines.append(
            f"- {entry['symbol']} {entry['period_start']}..{entry['period_end']} "
            f"({entry['schema_version']}, {entry['server_date_count']} gözlem, "
            f"kurum dizini={'var' if entry['brokerage_directory_present'] else 'yok'}): "
            f"AKD mutabakatı[{akd}], takas farkı istisna={entry['custodian_diff_exception_count']}, "
            f"gecikme[{lag}], "
            f"piyasa proxy kontrolleri geçti={'evet' if entry['proxy_checks_passed'] else 'hayır'}, "
            f"fiyat verisi bulundu={'evet' if entry['price_history_found'] else 'hayır'}, "
            f"fiyat/hacim kontrolleri geçti={price_checks_text}, "
            f"rapor={_REPORT_STATUS_TR[entry['report']]}"
        )
    return "\n".join(lines) + "\n"


def run_batch(args: argparse.Namespace) -> int:
    root: Path = args.all_root
    captures = discover_captures(root)
    entries = [_process_capture(path, args) for path in captures]
    index = build_batch_index(root, entries, args)
    payload = (json.dumps(index, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    if args.save:
        # Derived from the individual reports; unlike a per-capture report,
        # this index file is always (over)written to stay current.
        try:
            (root / "index.json").write_bytes(payload)
        except OSError as exc:
            print(f"Index could not be saved: {exc}", file=sys.stderr)

    if args.format == "json":
        sys.stdout.buffer.write(payload)
    else:
        sys.stdout.buffer.write(render_index_text(index).encode("utf-8"))
    totals = index["totals"]
    return 1 if (totals["failed"] or totals["reports_failed"]) else 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.all_root is not None:
        if args.capture is not None:
            print("Analysis failed: pass a capture path or --all, not both", file=sys.stderr)
            return 1
        if args.price_history is not None:
            print(
                "Analysis failed: --price-history is single-capture mode only; "
                "--all auto-discovers under --price-history-root",
                file=sys.stderr,
            )
            return 1
        return run_batch(args)
    if args.capture is None:
        _parser().error("the following arguments are required: capture (or use --all)")

    try:
        capture_bytes = args.capture.read_bytes()
        capture = load_period_capture(args.capture)
        price_capture = (
            None if args.price_history is None else load_price_history_capture(args.price_history)
        )
        report = build_period_report(
            capture, lags=args.lags, detail_lag=args.detail_lag, top_n=args.top,
            price=price_capture,
            volume_ratio_tolerance=args.volume_ratio_tolerance,
            adjustment_ratio_epsilon=args.adjustment_ratio_epsilon,
            ratio_regime_tolerance=args.ratio_regime_tolerance,
            custody_float_tolerance=args.custody_float_tolerance,
            provisional_last_custody_days=args.provisional_last_custody_days,
        )
    except (OSError, PeriodCaptureError, PriceHistoryCaptureError, ValueError) as exc:
        print(f"Analysis failed: {exc}", file=sys.stderr)
        return 1
    report["run"] = _run_metadata(
        args.capture, capture_bytes, lags=args.lags, detail_lag=args.detail_lag, top=args.top,
        volume_ratio_tolerance=args.volume_ratio_tolerance,
        adjustment_ratio_epsilon=args.adjustment_ratio_epsilon,
        ratio_regime_tolerance=args.ratio_regime_tolerance,
        custody_float_tolerance=args.custody_float_tolerance,
        provisional_last_custody_days=args.provisional_last_custody_days,
    )
    payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if args.save:
        try:
            saved = _save(args.capture, payload)
        except FileExistsError:
            print("Analysis failed: report already exists (no overwrite)", file=sys.stderr)
            return 1
        except OSError:
            print("Analysis failed: report could not be saved", file=sys.stderr)
            return 1
        print(f"Report saved: {saved.name}", file=sys.stderr)
    if args.format == "json":
        sys.stdout.buffer.write(payload)
    else:
        sys.stdout.buffer.write(render_text(report).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
