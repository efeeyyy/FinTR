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
from fintr.features.price_join import DEFAULT_ADJUSTMENT_RATIO_EPSILON, DEFAULT_VOLUME_RATIO_TOLERANCE
from fintr.research.engine import EngineError, load_symbol_data
from fintr.research.report import RunParameters, build_reproducibility_block, render_text, run_study
from fintr.research.study_spec import StudySpecError, load_study_spec


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
        help="repeatable; one entry per symbol the spec needs (declared symbols plus "
             "symbol_holdout, if --split test)",
    )
    parser.add_argument("--split", choices=("train", "test"), default="train",
                        help="test refuses to run unless the spec's frozen flag is true")
    parser.add_argument("--cost-bps", type=_non_negative_decimal, default=Decimal(0),
                        help="round-trip transaction cost assumption in basis points (default: 0)")
    parser.add_argument("--volume-ratio-tolerance", type=_non_negative_decimal,
                        default=DEFAULT_VOLUME_RATIO_TOLERANCE,
                        help=f"default: {DEFAULT_VOLUME_RATIO_TOLERANCE}")
    parser.add_argument("--adjustment-ratio-epsilon", type=_non_negative_decimal,
                        default=DEFAULT_ADJUSTMENT_RATIO_EPSILON,
                        help=f"default: {DEFAULT_ADJUSTMENT_RATIO_EPSILON}")
    parser.add_argument("--custody-scale-jump-ratio", type=_ratio_above_one,
                        default=DEFAULT_CUSTODY_SCALE_JUMP_RATIO,
                        help=f"default: {DEFAULT_CUSTODY_SCALE_JUMP_RATIO}")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help=f"default: {DEFAULT_OUT_DIR}")
    parser.add_argument("--save", action="store_true",
                        help="write the JSON result under --out-dir; never overwrites")
    return parser


def _save(out_dir: Path, spec_id: str, split: str, payload: bytes) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    for attempt in range(1000):
        suffix = "" if attempt == 0 else f"-{attempt}"
        target = out_dir / f"{spec_id}_{split}{suffix}.json"
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        spec = load_study_spec(args.spec)
    except StudySpecError as exc:
        print(f"Study failed: invalid spec ({exc})", file=sys.stderr)
        return 1

    symbol_data = {}
    input_file_hashes: dict[str, str] = {}
    for symbol, period_path, price_path in args.data:
        try:
            period_bytes = period_path.read_bytes()
            price_bytes = price_path.read_bytes()
            capture = load_period_capture(period_path)
            price = load_price_history_capture(price_path)
            symbol_data[symbol] = load_symbol_data(
                symbol, capture, price,
                adjustment_ratio_epsilon=args.adjustment_ratio_epsilon,
                custody_scale_jump_ratio=args.custody_scale_jump_ratio,
            )
        except (OSError, PeriodCaptureError, PriceHistoryCaptureError, EngineError) as exc:
            print(f"Study failed: could not load data for {symbol} ({exc})", file=sys.stderr)
            return 1
        input_file_hashes[f"{symbol}:period_capture"] = hashlib.sha256(period_bytes).hexdigest()
        input_file_hashes[f"{symbol}:price_history"] = hashlib.sha256(price_bytes).hexdigest()

    params = RunParameters(
        cost_bps=args.cost_bps, volume_ratio_tolerance=args.volume_ratio_tolerance,
        adjustment_ratio_epsilon=args.adjustment_ratio_epsilon,
        custody_scale_jump_ratio=args.custody_scale_jump_ratio,
    )
    try:
        result = run_study(spec, symbol_data, split=args.split, params=params)
    except EngineError as exc:
        print(f"Study failed: {exc}", file=sys.stderr)
        return 1

    result["reproducibility"] = build_reproducibility_block(
        project_root=PROJECT_ROOT, spec=spec, input_files=input_file_hashes,
        seed=spec.bootstrap.seed,
    )
    payload = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    if args.save:
        saved = _save(args.out_dir, spec.spec_id, args.split, payload)
        print(f"Result saved: {saved}", file=sys.stderr)

    if args.format == "json":
        sys.stdout.buffer.write(payload)
    else:
        sys.stdout.buffer.write(render_text(result).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
