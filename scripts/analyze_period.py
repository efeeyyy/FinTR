"""Deterministic offline report for one saved period-analysis capture."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

from fintr.data.period_capture import PeriodCaptureError, load_period_capture
from fintr.inference.period_report import build_period_report


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LAGS = "0,1,2,3"
DEFAULT_DETAIL_LAG = 2
DEFAULT_TOP_N = 10


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze a saved period capture offline; no network, no model call.",
    )
    parser.add_argument("capture", type=Path, help="data/interim/period_analysis/<SYM>/<S>_<E>.json")
    parser.add_argument("--lags", type=_lags, default=_lags(DEFAULT_LAGS),
                        help=f"observation lags to test (default: {DEFAULT_LAGS})")
    parser.add_argument("--detail-lag", type=_non_negative_int, default=DEFAULT_DETAIL_LAG,
                        help=f"lag whose unmatched rows are listed (default: {DEFAULT_DETAIL_LAG})")
    parser.add_argument("--top", type=_positive_int, default=DEFAULT_TOP_N,
                        help=f"rows per ranked list (default: {DEFAULT_TOP_N})")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--save", action="store_true",
                        help="also write <capture>.report.json next to the capture; never overwrites")
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


def _run_metadata(args: argparse.Namespace, capture_bytes: bytes) -> dict[str, object]:
    try:
        display_path = args.capture.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        display_path = args.capture.name
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "input_file": display_path,
        "input_file_sha256": hashlib.sha256(capture_bytes).hexdigest(),
        "code_commit": _code_commit(),
        "python": platform.python_version(),
        "parameters": {
            "lags": list(args.lags), "detail_lag": args.detail_lag, "top": args.top,
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
        + (", ".join(diff["internally_inconsistent_codes"]) or "yok")
        + f"; günlük panel aralığı hizalı: {'evet' if diff['range_aligned_with_diff'] else 'hayır'}"
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
        f"{row['sign_matches']}/{row['comparable']} aynı işaret"
        for row in lag["summaries"]
    ]
    lines.append(
        f"  gecikme {lag['detail_lag']} için takas karşılığı henüz olmayan AKD günleri: "
        + (", ".join(lag["akd_dates_without_later_custody"]) or "yok")
    )
    days = report["provider_metadata"]["days"]
    if days:
        lines += ["", "Sağlayıcı retail_pct (yöntemi doğrulanmamış): " + ", ".join(
            f"{day['date'][5:]}={day['retail_pct'] if day['retail_pct'] is not None else 'null'}"
            for day in days
        )]
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        capture_bytes = args.capture.read_bytes()
        capture = load_period_capture(args.capture)
        report = build_period_report(
            capture, lags=args.lags, detail_lag=args.detail_lag, top_n=args.top,
        )
    except (OSError, PeriodCaptureError, ValueError) as exc:
        print(f"Analysis failed: {exc}", file=sys.stderr)
        return 1
    report["run"] = _run_metadata(args, capture_bytes)
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
