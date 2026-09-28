"""CLI for the offline, lookahead-safe event-study engine.

No network access and no model call: reads saved period captures and price
histories with the existing hash-verifying loaders, evaluates one
pre-registered study spec (``fintr_study_spec_v1``), and writes a JSON
result artifact plus a short text summary. This tool only reports
historical-outcome statistics; it never emits a buy/sell recommendation.
See ``docs/study_engine.md``.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from fintr.data.period_capture import PeriodCaptureError, load_period_capture
from fintr.data.price_history import PriceHistoryCaptureError, load_price_history_capture
from fintr.features.price_join import (
    DEFAULT_ADJUSTMENT_RATIO_EPSILON,
    DEFAULT_RATIO_REGIME_TOLERANCE,
    DEFAULT_VOLUME_RATIO_TOLERANCE,
)
from fintr.evaluation.symbol_guard import SymbolGuardError, load_active_guards
from fintr.evaluation.symbol_split import NO_SPLIT_GUARD_WARNING
from fintr.research.capture_merge import CaptureMergeError, CaptureSource, merge_period_captures
from fintr.research.engine import EngineError, load_symbol_data, tradeable_price_bars
from fintr.research.report import RunParameters, build_reproducibility_block, render_text, run_study
from fintr.research.study_spec import StudySpecError, load_study_spec
from fintr.research.test_plan import ALLOWED_TEST_SCOPES, TestPlanError, load_test_plan


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = Path("data/interim/studies")
DEFAULT_CUSTODY_SCALE_JUMP_RATIO = Decimal("3")


def _decimal(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("expected a decimal number") from exc
    if not number.is_finite():
        raise argparse.ArgumentTypeError("expected a finite decimal number")
    return number


def _non_negative_decimal(value: str) -> Decimal:
    number = _decimal(value)
    if number < 0:
        raise argparse.ArgumentTypeError("expected a non-negative decimal number")
    return number


def _ratio_above_one(value: str) -> Decimal:
    number = _decimal(value)
    if number <= 1:
        raise argparse.ArgumentTypeError("expected a decimal number greater than 1")
    return number


def _data_mapping(value: str) -> tuple[str, Path, Path]:
    if "=" not in value or "," not in value.split("=", 1)[1]:
        raise argparse.ArgumentTypeError(
            "expected SYMBOL=PERIOD_CAPTURE_PATH,PRICE_HISTORY_PATH"
        )
    symbol, rest = value.split("=", 1)
    period_path, price_path = rest.split(",", 1)
    symbol = symbol.strip()
    if not symbol:
        raise argparse.ArgumentTypeError("expected a non-empty symbol before '='")
    return symbol, Path(period_path.strip()), Path(price_path.strip())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one pre-registered study spec offline; no network, no model call.",
    )
    parser.add_argument("--spec", type=Path, required=True, help="fintr_study_spec_v1 JSON file")
    parser.add_argument(
        "--data", type=_data_mapping, action="append", required=True, dest="data",
        metavar="SYMBOL=PERIOD_CAPTURE_PATH,PRICE_HISTORY_PATH",
        help="repeatable; at least one entry per symbol the spec needs (declared symbols "
             "plus symbol_holdout, if --split test). Repeat a symbol with several period "
             "captures (same price history path) to merge them (E4: later captured_at_utc "
             "wins a whole date; seams/gaps are reported under capture_merge)",
    )
    parser.add_argument("--split", choices=("train", "test"), default="train",
                        help="test refuses to run unless the spec's frozen flag is true")
    parser.add_argument(
        "--test-plan", type=Path, default=None, metavar="TEST_PLAN_JSON",
        help=(
            "fintr_test_plan_v1 JSON (e.g. configs/studies/prereg/test_plan_v1.json); "
            "required with --split test (no default). The --spec file must match one "
            "of the plan's declared specs byte-for-byte (raw and canonical sha256)"
        ),
    )
    parser.add_argument(
        "--test-scope", choices=ALLOWED_TEST_SCOPES, default=None,
        help=(
            "required with --split test (no default). 'primary': every symbol the "
            "spec declared (--data must match exactly, symbol_scope=declared) -- the "
            "confirmatory scope (docs/prereg/DECISIONS.md 2026-09-28). "
            "'secondary': only the spec's symbol_holdout (symbol_scope=holdout_only), "
            "a robustness check, never confirmatory on its own"
        ),
    )
    parser.add_argument("--cost-bps", type=_non_negative_decimal, default=Decimal(0),
                        help="transaction cost assumption in basis points PER SIDE, charged at "
                             "entry and at exit: net = gross - 2 * bps / 10000 (round-trip = "
                             "2 x this value; default: 0)")
    parser.add_argument("--volume-ratio-tolerance", type=_non_negative_decimal,
                        default=DEFAULT_VOLUME_RATIO_TOLERANCE,
                        help=f"default: {DEFAULT_VOLUME_RATIO_TOLERANCE}")
    parser.add_argument("--adjustment-ratio-epsilon", type=_non_negative_decimal,
                        default=DEFAULT_ADJUSTMENT_RATIO_EPSILON,
                        help=f"default: {DEFAULT_ADJUSTMENT_RATIO_EPSILON}")
    parser.add_argument("--custody-scale-jump-ratio", type=_ratio_above_one,
                        default=DEFAULT_CUSTODY_SCALE_JUMP_RATIO,
                        help=f"default: {DEFAULT_CUSTODY_SCALE_JUMP_RATIO}")
    parser.add_argument(
        "--ratio-regime-tolerance", type=_non_negative_decimal,
        default=DEFAULT_RATIO_REGIME_TOLERANCE,
        help=(
            "tolerance for price_join's probable_retroactive_price_adjustment "
            "ratio-regime detection (integer k >= 2); signal dates inside a "
            "detected regime are dropped from both the conditioned and "
            "baseline sets for that symbol (docs/prereg/DECISIONS.md); "
            f"default: {DEFAULT_RATIO_REGIME_TOLERANCE}"
        ),
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help=f"default: {DEFAULT_OUT_DIR}")
    parser.add_argument("--save", action="store_true",
                        help="write the JSON result under --out-dir; never overwrites")
    parser.add_argument(
        "--no-split-guard", action="store_true",
        help=(
            "Disable the frozen BIST 30 holdout-split checks (see "
            "docs/evaluation_protocol.md). Only for non-BIST 30 exploratory work; "
            "the result's split_guard block and the text output say so loudly. "
            "Symbol exclusions (configs/exclusions/symbols.json) have no override."
        ),
    )
    return parser


def _save(out_dir: Path, spec_id: str, split: str, payload: bytes, *, test_scope: str | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    label = split if test_scope is None else f"{split}_{test_scope}"
    for attempt in range(1000):
        suffix = "" if attempt == 0 else f"-{attempt}"
        target = out_dir / f"{spec_id}_{label}{suffix}.json"
        if target.exists():
            continue
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".study-result-", suffix=".tmp", dir=out_dir, delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, target)
        except FileExistsError:
            continue
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return target
    raise RuntimeError("could_not_allocate_result_filename")


def check_study_symbols(spec, data_symbols, *, split: str, guards, test_scope: str | None = None) -> str | None:
    """First reason the requested run violates the exclusions or the frozen
    holdout split, or ``None``. Exclusions are checked unconditionally;
    holdout rules are skipped only under ``--no-split-guard``.

    train: no holdout symbol in the spec's train symbols or in ``--data``.
    test: the spec must be frozen, must declare a non-empty ``symbol_holdout``
    that is a subset of the split's holdout symbols. ``test_scope``
    (``--test-scope``) then decides: ``"primary"`` requires ``--data`` to be
    exactly the spec's full declared symbol set (development + holdout, the
    engine's ``symbol_scope="declared"``); ``"secondary"`` requires ``--data``
    to be exactly the spec's ``symbol_holdout`` (``symbol_scope=
    "holdout_only"``). ``test_scope=None`` (only reachable when the split
    guard itself is bypassed) applies no scope-specific check.
    """
    declared = spec.all_declared_symbols()
    excluded = sorted(
        {s for s in data_symbols if guards.is_excluded(s)}
        | {s for s in declared if guards.is_excluded(s)}
    )
    if excluded:
        reasons = "; ".join(f"{s}: {guards.exclusion_reason(s)}" for s in excluded)
        return f"excluded symbol(s) {','.join(excluded)} ({reasons})"
    if guards.split_guard_bypassed:
        return None
    if split == "train":
        in_spec = sorted(s for s in spec.symbols if guards.is_holdout(s))
        if in_spec:
            return f"spec train symbols include holdout symbols ({','.join(in_spec)})"
        in_data = sorted(s for s in data_symbols if guards.is_holdout(s))
        if in_data:
            return f"holdout symbols not allowed in --split train ({','.join(in_data)})"
        return None
    if not spec.frozen:
        return "--split test requires a frozen spec (spec frozen=false)"
    if not spec.symbol_holdout:
        return "--split test requires the spec to declare symbol_holdout"
    not_holdout_spec = sorted(s for s in spec.symbol_holdout if not guards.is_holdout(s))
    if not_holdout_spec:
        return (
            "spec symbol_holdout lists symbols that are not holdout symbols of the "
            f"frozen split ({','.join(not_holdout_spec)})"
        )
    if test_scope is None:
        return None
    if test_scope == "primary":
        required = set(declared)
        given = set(data_symbols)
        missing = sorted(required - given)
        extra = sorted(given - required)
        if missing or extra:
            return (
                "--test-scope primary requires --data to be exactly the spec's "
                f"declared symbols (missing: {','.join(missing) or 'yok'}; "
                f"extra: {','.join(extra) or 'yok'})"
            )
        split_all = {s for s in guards.split.all_symbols() if not guards.is_excluded(s)}
        if required != split_all:
            mismatch = sorted(required ^ split_all)
            return (
                "--test-scope primary requires the spec's declared symbols to equal "
                f"the active split's symbols minus exclusions (mismatch: {','.join(mismatch)})"
            )
        return None
    if test_scope == "secondary":
        required = set(spec.symbol_holdout)
        given = set(data_symbols)
        missing = sorted(required - given)
        extra = sorted(given - required)
        if missing or extra:
            return (
                "--test-scope secondary requires --data to be exactly the spec's "
                f"symbol_holdout (missing: {','.join(missing) or 'yok'}; "
                f"extra: {','.join(extra) or 'yok'})"
            )
        return None
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.split == "test" and (args.test_plan is None or args.test_scope is None):
        print(
            "Study failed: --split test requires --test-plan and --test-scope "
            "(no default)",
            file=sys.stderr,
        )
        return 1

    try:
        spec = load_study_spec(args.spec)
    except StudySpecError as exc:
        print(f"Study failed: invalid spec ({exc})", file=sys.stderr)
        return 1

    data_symbols = sorted({symbol.strip().upper() for symbol, _, _ in args.data})
    try:
        guards = load_active_guards(bypass_split=args.no_split_guard)
    except SymbolGuardError as exc:
        print(f"Study failed: {exc}", file=sys.stderr)
        return 1
    test_scope = args.test_scope if args.split == "test" and not guards.split_guard_bypassed else None
    guard_problem = check_study_symbols(
        spec, data_symbols, split=args.split, guards=guards, test_scope=test_scope,
    )
    if guard_problem is not None:
        print(
            f"Study failed: {guard_problem}; split={guards.report()['split_file']} "
            f"sha256={guards.split.split_sha256}",
            file=sys.stderr,
        )
        return 1
    if guards.split_guard_bypassed:
        print(NO_SPLIT_GUARD_WARNING, file=sys.stderr)
    split_guard = guards.report(processed_symbols=data_symbols)
    # Under the guard a test run evaluates only the spec's symbol_holdout,
    # unless --test-scope primary asked for every declared symbol.
    symbol_scope = "declared" if test_scope in (None, "primary") else "holdout_only"

    test_plan_path: str | None = None
    test_plan_sha256: str | None = None
    test_window_end = None
    if test_scope is not None:
        try:
            plan = load_test_plan(args.test_plan)
        except TestPlanError as exc:
            print(f"Study failed: invalid test plan ({exc})", file=sys.stderr)
            return 1
        spec_ref = plan.spec_by_id(spec.spec_id)
        if spec_ref is None:
            print(
                f"Study failed: spec {spec.spec_id} is not declared in test plan {args.test_plan}",
                file=sys.stderr,
            )
            return 1
        raw_spec_sha256 = hashlib.sha256(args.spec.read_bytes()).hexdigest()
        if raw_spec_sha256 != spec_ref.raw_sha256 or spec.spec_sha256 != spec_ref.canonical_sha256:
            print(
                f"Study failed: --spec {args.spec} does not match the hashes recorded for "
                f"{spec.spec_id} in test plan {args.test_plan} (raw={raw_spec_sha256}, "
                f"canonical={spec.spec_sha256})",
                file=sys.stderr,
            )
            return 1
        if not spec.frozen:
            print("Study failed: test plan requires a frozen spec (spec frozen=false)", file=sys.stderr)
            return 1
        if plan.test_start != spec.train_test_cutoff:
            print(
                f"Study failed: test plan test_start ({plan.test_start.isoformat()}) does not "
                f"match spec train_test_cutoff ({spec.train_test_cutoff.isoformat()})",
                file=sys.stderr,
            )
            return 1
        plan.scope(test_scope)  # validates the scope block exists and is well-formed
        test_plan_path = str(args.test_plan)
        test_plan_sha256 = plan.plan_sha256
        test_window_end = plan.test_end

    grouped: dict[str, list[tuple[Path, Path]]] = {}
    for symbol, period_path, price_path in args.data:
        grouped.setdefault(symbol, []).append((period_path, price_path))

    symbol_data = {}
    input_file_hashes: dict[str, str] = {}
    capture_merge: dict[str, dict] = {}
    for symbol, entries in grouped.items():
        price_paths = {price_path for _, price_path in entries}
        if len(price_paths) != 1:
            print(
                f"Study failed: {symbol} has conflicting price history paths "
                f"({', '.join(sorted(str(p) for p in price_paths))})",
                file=sys.stderr,
            )
            return 1
        price_path = entries[0][1]
        try:
            price_bytes = price_path.read_bytes()
            price = load_price_history_capture(price_path)
            sources = []
            for index, (period_path, _) in enumerate(entries):
                period_bytes = period_path.read_bytes()
                period_sha256 = hashlib.sha256(period_bytes).hexdigest()
                key = f"{symbol}:period_capture" + ("" if len(entries) == 1 else f":{index}")
                input_file_hashes[key] = period_sha256
                sources.append(CaptureSource(
                    load_period_capture(period_path), label=str(period_path),
                    file_sha256=period_sha256,
                ))
            merged = merge_period_captures(
                sources, tradeable_dates=[bar.trade_date for bar in tradeable_price_bars(price)],
            )
            symbol_data[symbol] = load_symbol_data(
                symbol, merged.capture, price,
                adjustment_ratio_epsilon=args.adjustment_ratio_epsilon,
                custody_scale_jump_ratio=args.custody_scale_jump_ratio,
                ratio_regime_tolerance=args.ratio_regime_tolerance,
            )
        except (OSError, PeriodCaptureError, PriceHistoryCaptureError, EngineError,
                CaptureMergeError) as exc:
            print(f"Study failed: could not load data for {symbol} ({exc})", file=sys.stderr)
            return 1
        input_file_hashes[f"{symbol}:price_history"] = hashlib.sha256(price_bytes).hexdigest()
        capture_merge[symbol] = merged.report

    params = RunParameters(
        cost_bps=args.cost_bps, volume_ratio_tolerance=args.volume_ratio_tolerance,
        adjustment_ratio_epsilon=args.adjustment_ratio_epsilon,
        custody_scale_jump_ratio=args.custody_scale_jump_ratio,
        ratio_regime_tolerance=args.ratio_regime_tolerance,
    )
    try:
        result = run_study(
            spec, symbol_data, split=args.split, params=params, symbol_scope=symbol_scope,
            test_window_end=test_window_end,
        )
    except EngineError as exc:
        print(f"Study failed: {exc}", file=sys.stderr)
        return 1

    result["split_guard"] = split_guard
    result["capture_merge"] = capture_merge
    result["test_plan_path"] = test_plan_path
    result["test_plan_sha256"] = test_plan_sha256
    result["test_scope"] = test_scope
    result["reproducibility"] = build_reproducibility_block(
        project_root=PROJECT_ROOT, spec=spec, input_files=input_file_hashes,
        seed=spec.bootstrap.seed,
    )
    payload = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    if args.save:
        saved = _save(args.out_dir, spec.spec_id, args.split, payload, test_scope=test_scope)
        print(f"Result saved: {saved}", file=sys.stderr)

    if args.format == "json":
        sys.stdout.buffer.write(payload)
    else:
        sys.stdout.buffer.write(render_text(result).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
