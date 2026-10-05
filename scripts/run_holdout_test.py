"""One-shot pre-registered holdout-test executor (April 2027 window).

Runs ``scripts/run_study.py --split test`` for each of the four frozen
H1-H4 specs, in both test scopes (primary: all 30 declared symbols;
secondary: holdout-only, E5), then ``scripts/evaluate_study_family.py`` on
the primary H1+H2 results only. See ``docs/prereg/README.md`` ("Holdout
testi çalıştırma") and ``docs/prereg/DECISIONS.md`` (2026-09-28) for the
protocol this implements.

Default mode is ``--dry-run``: it writes nothing and only reports which of
six pre-flight checks pass. ``--execute`` requires every dry-run check to
pass first, then atomically creates a lock file
(``data/interim/studies/test/LOCK``, ``O_EXCL``) before doing anything else,
so two concurrent invocations cannot both run the one-shot test. A lock or a
completed manifest from an earlier run always refuses a new execute -- this
test runs once.

No return, p-value or decision text is ever written to stdout here: only
file paths and check statuses. Every statistic goes to the JSON result
files ``run_study.py``/``evaluate_study_family.py`` write, never printed.

**Decided 2026-09-28 (docs/prereg/DECISIONS.md, "Holdout fiyat geçmişi kararı"): full price history is kept, for data-quality detection only.** Background of the former open question (see ``docs/prereg/README.md``
E3/E4 notes and ``docs/study_engine.md``'s "Custody scale jumps" /
"Ratio-regime" sections): ``fintr.features.price_join.join_price_history``
and the engine's adjustment-boundary/ratio-regime detection read a symbol's
**entire** saved price history, including bars before ``test_start`` --
this is existing, already-documented engine behaviour (train runs rely on
it too), not something this tool changes. Whether a holdout symbol's price
file should be trimmed to start at ``test_start`` before this one-shot test
runs, or kept at full history as train does, was not decided by Efe; this
tool runs it the same way ``run_study.py`` always has (full saved price
history) and flags the question in its report rather than picking an
answer.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_PLAN = PROJECT_ROOT / "configs" / "studies" / "prereg" / "test_plan_v1.json"
DEFAULT_PERIOD_ROOT = PROJECT_ROOT / "data" / "interim" / "period_analysis"
DEFAULT_PRICE_ROOT = PROJECT_ROOT / "data" / "interim" / "price_history"
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "interim" / "studies"
DEFAULT_TEST_DIR = PROJECT_ROOT / "data" / "interim" / "studies" / "test"

MANIFEST_SCHEMA = "fintr_holdout_test_manifest_v1"

_GIT_CANDIDATES = ("git", r"C:\Program Files\Git\cmd\git.exe")
_CAPTURE_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.json$")

from fintr.data.period_capture import PeriodCaptureError, load_period_capture
from fintr.data.price_history import PriceHistoryCaptureError, load_price_history_capture
from fintr.evaluation.symbol_guard import SymbolGuardError, load_active_guards
from fintr.research.capture_merge import CaptureMergeError, CaptureSource, merge_period_captures
from fintr.research.engine import tradeable_price_bars
from fintr.research.study_spec import StudySpecError, load_study_spec
from fintr.research.test_plan import TestPlanError, load_test_plan


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git_status(root: Path | None = None) -> tuple[str | None, bool, str]:
    """``(commit, is_clean, detail)``. ``root`` overrides the repo root
    (tests only); ``is_clean`` is ``False`` whenever either git call fails."""
    def run(*args: str) -> subprocess.CompletedProcess | None:
        for git in _GIT_CANDIDATES:
            try:
                return subprocess.run(
                    [git, *args], cwd=root or PROJECT_ROOT, capture_output=True, text=True,
                    timeout=15, check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
        return None

    commit_proc = run("rev-parse", "HEAD")
    commit = commit_proc.stdout.strip() if commit_proc is not None and commit_proc.returncode == 0 else None
    status_proc = run("status", "--porcelain")
    if status_proc is None or status_proc.returncode != 0:
        return commit, False, "could not run git status"
    dirty = status_proc.stdout.strip()
    if dirty:
        return commit, False, f"working tree not clean:\n{dirty}"
    if commit is None:
        return commit, False, "could not resolve HEAD commit"
    return commit, True, f"clean, commit {commit}"


def _period_files(period_root: Path, symbol: str) -> list[Path]:
    directory = period_root / symbol
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and _CAPTURE_NAME_RE.match(p.name))


def _price_files(price_root: Path, symbol: str) -> list[Path]:
    directory = price_root / symbol
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and _CAPTURE_NAME_RE.match(p.name))


class SymbolCheck:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.price_path: Path | None = None
        self.period_paths_used: list[Path] = []
        self.period_paths_pre_window_ignored: list[Path] = []
        self.check3_ok = False
        self.check3_detail = ""
        self.check4_ok = False
        self.check4_detail = ""
        self.check5_ok = False
        self.check5_missing_dates: list[str] = []
        self.check5_detail = ""


def _inspect_symbol(
    symbol: str, *, period_root: Path, price_root: Path, test_start: date, test_end: date, min_bars: int,
) -> SymbolCheck:
    result = SymbolCheck(symbol)

    price_candidates = _price_files(price_root, symbol)
    loaded_prices = []
    for path in price_candidates:
        try:
            loaded_prices.append((path, load_price_history_capture(path)))
        except (OSError, PriceHistoryCaptureError):
            continue
    if not loaded_prices:
        result.check4_detail = f"no price history file found under {price_root / symbol}"
        result.check3_detail = "skipped: no price history to determine tradeable dates"
        result.check5_detail = "skipped: no price history to determine tradeable dates"
        return result
    price_path, price = max(loaded_prices, key=lambda item: item[1].requested_end)
    result.price_path = price_path
    bars = tradeable_price_bars(price)

    after_window = [bar for bar in bars if bar.trade_date > test_end]
    result.check4_ok = len(after_window) >= min_bars
    result.check4_detail = (
        f"{len(after_window)} tradeable bar(s) after {test_end.isoformat()} "
        f"(need >= {min_bars}) in {price_path}"
    )

    window_tradeable_dates = {bar.trade_date for bar in bars if test_start <= bar.trade_date <= test_end}

    period_candidates = _period_files(period_root, symbol)
    loaded_periods = []
    for path in period_candidates:
        try:
            loaded_periods.append((path, load_period_capture(path)))
        except (OSError, PeriodCaptureError):
            continue
    qualifying = [(p, c) for p, c in loaded_periods if c.start >= test_start]
    result.period_paths_pre_window_ignored = [
        p for p, c in loaded_periods if c.start < test_start
    ]
    if not qualifying:
        result.check3_detail = (
            f"no period_analysis record with start >= {test_start.isoformat()} under "
            f"{period_root / symbol}"
            + (
                f" ({len(result.period_paths_pre_window_ignored)} pre-window record(s) found, "
                "not used)" if result.period_paths_pre_window_ignored else ""
            )
        )
        result.check5_detail = "skipped: check 3 (period records) failed"
        return result
    result.period_paths_used = [p for p, _c in qualifying]
    try:
        if len(qualifying) == 1:
            merged_capture = qualifying[0][1]
        else:
            sources = [
                CaptureSource(capture, label=str(path), file_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                for path, capture in qualifying
            ]
            merged = merge_period_captures(sources, tradeable_dates=[bar.trade_date for bar in bars])
            merged_capture = merged.capture
    except CaptureMergeError as exc:
        result.check3_detail = f"could not merge {len(qualifying)} period record(s) (E4): {exc}"
        return result
    result.check3_ok = True
    result.check3_detail = f"{len(qualifying)} period record(s) used: {[str(p) for p, _ in qualifying]}"

    missing = sorted(window_tradeable_dates - set(merged_capture.server_dates))
    result.check5_ok = not missing
    result.check5_missing_dates = [d.isoformat() for d in missing]
    result.check5_detail = "ok" if not missing else f"missing AKD server date(s): {result.check5_missing_dates}"
    return result


def _load_plan_and_specs(test_plan_path: Path) -> tuple[object | None, dict[str, object], list[str]]:
    """Returns ``(plan, {spec_id: StudySpec}, problems)``. A spec that fails
    to load or match its recorded hashes is left out of the dict and its
    reason appended to ``problems``."""
    problems: list[str] = []
    try:
        plan = load_test_plan(test_plan_path)
    except TestPlanError as exc:
        return None, {}, [f"invalid test plan {test_plan_path}: {exc}"]

    specs: dict[str, object] = {}
    for ref in plan.specs:
        spec_path = PROJECT_ROOT / ref.path
        try:
            raw_sha256 = hashlib.sha256(spec_path.read_bytes()).hexdigest()
        except OSError as exc:
            problems.append(f"{ref.spec_id}: could not read {spec_path} ({exc})")
            continue
        if raw_sha256 != ref.raw_sha256:
            problems.append(f"{ref.spec_id}: raw sha256 mismatch ({spec_path})")
            continue
        try:
            spec = load_study_spec(spec_path)
        except StudySpecError as exc:
            problems.append(f"{ref.spec_id}: invalid spec ({exc})")
            continue
        if spec.spec_sha256 != ref.canonical_sha256:
            problems.append(f"{ref.spec_id}: canonical sha256 mismatch ({spec_path})")
            continue
        if not spec.frozen:
            problems.append(f"{ref.spec_id}: spec is not frozen")
            continue
        if spec.train_test_cutoff != plan.test_start:
            problems.append(
                f"{ref.spec_id}: train_test_cutoff {spec.train_test_cutoff} != plan test_start {plan.test_start}"
            )
            continue
        specs[spec.spec_id] = spec
    return plan, specs, problems


def run_dry_run_checks(args: argparse.Namespace) -> tuple[list[dict], dict]:
    checks: list[dict] = []
    context: dict = {}

    plan, specs, plan_problems = _load_plan_and_specs(args.test_plan)
    checks.append({
        "id": 1, "name": "plan_and_spec_hashes",
        "ok": plan is not None and not plan_problems and len(specs) == len(plan.specs),
        "detail": "ok" if plan is not None and not plan_problems else "; ".join(plan_problems) or "unreadable plan",
    })
    context["plan"] = plan
    context["specs"] = specs

    commit, clean, detail = _git_status(args.repo_root)
    checks.append({"id": 2, "name": "git_working_tree_clean", "ok": clean, "detail": detail})
    context["commit"] = commit

    try:
        guards = load_active_guards()
        context["guards"] = guards
    except SymbolGuardError as exc:
        guards = None
        context["guards"] = None
        for check_id, name in ((3, "period_records_after_window_start"),
                                (4, "min_tradeable_bars_after_window_end"),
                                (5, "akd_dates_cover_tradeable_window_dates")):
            checks.append({"id": check_id, "name": name, "ok": False, "detail": f"could not load split guard: {exc}"})
        symbol_checks: dict[str, SymbolCheck] = {}
        context["symbol_checks"] = symbol_checks
    else:
        excluded = {s for s in guards.split.all_symbols() if guards.is_excluded(s)}
        all_symbols = sorted(set(guards.split.all_symbols()) - excluded)
        holdout_symbols = sorted(s for s in guards.split.holdout_symbols if s not in excluded)
        context["primary_symbols"] = all_symbols
        context["secondary_symbols"] = holdout_symbols

        if plan is None:
            symbol_checks = {}
            for check_id, name in ((3, "period_records_after_window_start"),
                                    (4, "min_tradeable_bars_after_window_end"),
                                    (5, "akd_dates_cover_tradeable_window_dates")):
                checks.append({
                    "id": check_id, "name": name, "ok": False,
                    "detail": "skipped: test plan did not load (see check 1)",
                })
        else:
            symbol_checks = {
                symbol: _inspect_symbol(
                    symbol, period_root=args.period_analysis_root, price_root=args.price_history_root,
                    test_start=plan.test_start, test_end=plan.test_end,
                    min_bars=plan.min_tradeable_bars_after_end,
                )
                for symbol in all_symbols
            }
            for check_id, name, attr in (
                (3, "period_records_after_window_start", "check3"),
                (4, "min_tradeable_bars_after_window_end", "check4"),
                (5, "akd_dates_cover_tradeable_window_dates", "check5"),
            ):
                failing = sorted(
                    symbol for symbol, sc in symbol_checks.items() if not getattr(sc, f"{attr}_ok")
                )
                checks.append({
                    "id": check_id, "name": name, "ok": not failing,
                    "detail": "ok" if not failing else "; ".join(
                        f"{s}={getattr(symbol_checks[s], f'{attr}_detail')}" for s in failing
                    ),
                })
        context["symbol_checks"] = symbol_checks

    lock_path = args.test_dir / "LOCK"
    manifest_path = args.test_dir / "manifest.json"
    status = _previous_output_status(lock_path, manifest_path)
    checks.append({
        "id": 6, "name": "no_lock_or_previous_output", "ok": status is None,
        "detail": "ok" if status is None else f"{_STATUS_DETAIL[status]} ({lock_path} / {manifest_path})",
    })
    context["lock_path"] = lock_path
    context["manifest_path"] = manifest_path
    return checks, context


def _previous_output_status(lock_path: Path, manifest_path: Path) -> str | None:
    """``None`` (clean, ok to run), ``"completed"`` (manifest.json reports
    ``completed: true`` -- never rerun) or ``"incomplete"`` (a lock and/or a
    manifest exist but the run never completed -- a half-finished run;
    Efe's decision is required, never auto-retried)."""
    lock_exists = lock_path.exists()
    manifest = None
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            manifest = {"completed": False}
    if not lock_exists and manifest is None:
        return None
    if manifest is not None and manifest.get("completed") is True:
        return "completed"
    return "incomplete"


_STATUS_DETAIL = {
    "completed": "önceki test zaten tamamlanmış (manifest.json completed=true); tekrar çalışmaz",
    "incomplete": "yarım kalmış çalışma, Efe kararı gerekir (LOCK ve/veya manifest mevcut, tamamlanmamış)",
}


def render_checks(checks: list[dict]) -> str:
    lines = []
    for check in checks:
        mark = "OK" if check["ok"] else "FAIL"
        lines.append(f"[{mark}] {check['id']}. {check['name']}: {check['detail']}")
    return "\n".join(lines) + "\n"


_MODULE_CACHE: dict[str, object] = {}


def _load_script_module(name: str):
    if name not in _MODULE_CACHE:
        spec = importlib.util.spec_from_file_location(f"holdout_runner_{name}", PROJECT_ROOT / "scripts" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULE_CACHE[name] = module
    return _MODULE_CACHE[name]


def _run_module_main(module, argv: list[str]) -> tuple[int, str]:
    """Runs ``module.main(argv)`` with its stdout swallowed (never leaking
    returns/p-values/decision text to our stdout); returns ``(code,
    stderr_text)``."""
    original_stdout = module.sys.stdout
    buffer = io.BytesIO()
    wrapped = io.TextIOWrapper(buffer, encoding="utf-8")
    module.sys.stdout = wrapped
    stderr_buffer = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr_buffer):
            code = module.main(argv)
        wrapped.flush()
    finally:
        module.sys.stdout = original_stdout
    return code, stderr_buffer.getvalue()


_SAVED_RE = re.compile(r"^Result saved: (.+)$", re.MULTILINE)


def _run_study_for(
    *, spec_path: Path, test_plan_path: Path, test_scope: str, cost_bps: Decimal,
    symbols: list[str], symbol_checks: dict[str, "SymbolCheck"], out_dir: Path,
) -> tuple[int, Path | None, str]:
    module = _load_script_module("run_study")
    argv = [
        "--spec", str(spec_path), "--test-plan", str(test_plan_path), "--test-scope", test_scope,
        "--split", "test", "--cost-bps", str(cost_bps), "--out-dir", str(out_dir), "--save",
        "--format", "json",
    ]
    for symbol in symbols:
        sc = symbol_checks[symbol]
        for period_path in sc.period_paths_used:
            argv += ["--data", f"{symbol}={period_path},{sc.price_path}"]
    code, stderr_text = _run_module_main(module, argv)
    match = _SAVED_RE.search(stderr_text)
    saved_path = Path(match.group(1)) if match else None
    return code, saved_path, stderr_text


def _run_family_for(result_paths: list[Path], out_path: Path) -> tuple[int, str]:
    module = _load_script_module("evaluate_study_family")
    argv = ["--out", str(out_path), "--format", "json"]
    for path in result_paths:
        argv += ["--result", str(path)]
    return _run_module_main(module, argv)


def execute(args: argparse.Namespace, checks: list[dict], context: dict) -> int:
    if not all(check["ok"] for check in checks):
        print("Execute refused: not every dry-run check passed (see above).", file=sys.stderr)
        return 1

    test_dir = args.test_dir
    test_dir.mkdir(parents=True, exist_ok=True)
    lock_path = context["lock_path"]
    manifest_path = context["manifest_path"]

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"created_at_utc": _now_iso(), "pid": os.getpid()}) + "\n")
    except FileExistsError:
        status = _previous_output_status(lock_path, manifest_path) or "incomplete"
        print(f"Execute refused: {_STATUS_DETAIL[status]}", file=sys.stderr)
        return 1

    plan = context["plan"]
    specs = context["specs"]
    symbol_checks = context["symbol_checks"]
    hypothesis_to_spec_path: dict[str, Path] = {}
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "started_at_utc": _now_iso(),
        "code_commit": context["commit"],
        "test_plan_path": str(args.test_plan),
        "test_plan_sha256": plan.plan_sha256,
        "split_file_sha256": context["guards"].split.split_sha256,
        "specs": {
            ref.spec_id: {
                "path": ref.path,
                "raw_sha256": ref.raw_sha256,
                "canonical_sha256": specs[ref.spec_id].spec_sha256,
                "bootstrap_seed": specs[ref.spec_id].bootstrap.seed,
                "block_bootstrap_seed": (
                    None if specs[ref.spec_id].block_bootstrap is None
                    else specs[ref.spec_id].block_bootstrap.seed
                ),
            }
            for ref in plan.specs
        },
        "python_version": platform.python_version(),
        "input_file_sha256": {},
        "results": {},
        "completed": False,
    }
    try:
        for symbol, sc in symbol_checks.items():
            for path in sc.period_paths_used:
                manifest["input_file_sha256"][f"{symbol}:period_capture:{path.name}"] = \
                    hashlib.sha256(path.read_bytes()).hexdigest()
            if sc.price_path is not None:
                manifest["input_file_sha256"][f"{symbol}:price_history"] = \
                    hashlib.sha256(sc.price_path.read_bytes()).hexdigest()

        for spec_id, spec in specs.items():
            spec_path = PROJECT_ROOT / next(
                ref.path for ref in plan.specs if ref.spec_id == spec_id
            )
            for scope, symbols in (
                ("primary", context["primary_symbols"]), ("secondary", context["secondary_symbols"]),
            ):
                code, saved_path, stderr_text = _run_study_for(
                    spec_path=spec_path, test_plan_path=args.test_plan, test_scope=scope,
                    cost_bps=plan.cost_bps_per_side, symbols=symbols, symbol_checks=symbol_checks,
                    out_dir=args.out_dir,
                )
                print(f"run_study {spec_id} scope={scope}: {'ok' if code == 0 else 'FAILED'}")
                if code != 0 or saved_path is None:
                    raise RuntimeError(f"run_study failed for {spec_id} scope={scope}: {stderr_text.strip()}")
                print(f"  saved: {saved_path}")
                manifest["results"][f"{spec_id}:{scope}"] = {
                    "path": str(saved_path), "sha256": hashlib.sha256(saved_path.read_bytes()).hexdigest(),
                }
                if scope == "primary" and spec.preregistration is not None:
                    hypothesis_to_spec_path[spec.preregistration.hypothesis_id] = saved_path

        primary_members = plan.primary.primary_family_members
        missing_members = [hid for hid in primary_members if hypothesis_to_spec_path.get(hid) is None]
        if missing_members:
            raise RuntimeError(f"missing primary result(s) for: {','.join(missing_members)}")
        primary_result_paths = [hypothesis_to_spec_path[hid] for hid in primary_members]
        family_out = test_dir / "family_decision_primary.json"
        code, stderr_text = _run_family_for(primary_result_paths, family_out)
        print(f"evaluate_study_family (primary H1+H2): {'ok' if code == 0 else 'FAILED'}")
        if code != 0:
            raise RuntimeError(f"evaluate_study_family failed: {stderr_text.strip()}")
        print(f"  saved: {family_out}")
        manifest["family_decision_path"] = str(family_out)
        manifest["family_decision_sha256"] = hashlib.sha256(family_out.read_bytes()).hexdigest()
        manifest["completed"] = True
        manifest["finished_at_utc"] = _now_iso()
    except Exception as exc:  # noqa: BLE001 -- always record, never re-raise past manifest write
        manifest["error"] = str(exc)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Execute failed: {exc}", file=sys.stderr)
        print(f"Partial manifest written: {manifest_path}", file=sys.stderr)
        return 1

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest written: {manifest_path}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "One-shot pre-registered holdout-test executor (docs/prereg/README.md, "
            "docs/prereg/DECISIONS.md 2026-09-28). Default: --dry-run (writes nothing)."
        ),
    )
    parser.add_argument("--test-plan", type=Path, default=DEFAULT_TEST_PLAN)
    parser.add_argument("--period-analysis-root", type=Path, default=DEFAULT_PERIOD_ROOT)
    parser.add_argument("--price-history-root", type=Path, default=DEFAULT_PRICE_ROOT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                         help="where run_study.py --save writes result JSON files")
    parser.add_argument("--test-dir", type=Path, default=DEFAULT_TEST_DIR,
                         help="where LOCK, manifest.json and the family decision are written")
    parser.add_argument("--repo-root", type=Path, default=None,
                         help="override the git repository root (tests only)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="default: checks only, writes nothing")
    mode.add_argument("--execute", action="store_true",
                       help="run the one-shot test; requires every dry-run check to pass first")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    checks, context = run_dry_run_checks(args)
    print(render_checks(checks))
    if not args.execute:
        return 0 if all(check["ok"] for check in checks) else 1
    return execute(args, checks, context)


if __name__ == "__main__":
    raise SystemExit(main())
