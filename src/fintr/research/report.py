"""Assemble one study run's JSON result artifact and short text summary.

This is the only place that turns engine output into the final report; it
enforces the pre-registration (frozen) gate for test-period results and
builds the reproducibility block (code commit, spec hash, input file
hashes, seed, python version). Nothing here computes a new financial
figure -- every number is read from ``fintr.research.engine`` output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import platform
import subprocess
import sys
from pathlib import Path

from fintr.evaluation.symbol_guard import render_guard_lines
from fintr.research.engine import (
    EngineError,
    SymbolData,
    build_events,
    signal_dates_for_split,
    summarize_horizon,
    symbols_for_split,
)
from fintr.research.study_spec import StudySpec


RESULT_SCHEMA = "fintr_study_result_v1"

_GIT_CANDIDATES = ("git", r"C:\Program Files\Git\cmd\git.exe")


def resolve_code_commit(cwd: Path) -> str | None:
    """Best-effort commit hash; ``None`` (never fabricated) if git is
    unavailable under any known name, including when it is not on PATH."""
    for git in _GIT_CANDIDATES:
        try:
            completed = subprocess.run(
                [git, "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True,
                timeout=10, check=True,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        commit = completed.stdout.strip()
        if commit:
            return commit
    return None


def _decimal_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _bootstrap_payload(bootstrap) -> dict:
    return {
        "seed": bootstrap.seed,
        "resamples": bootstrap.resamples,
        "confidence_level": str(bootstrap.confidence_level),
        "lower": bootstrap.lower,
        "upper": bootstrap.upper,
        "mean_difference": bootstrap.mean_difference,
    }


def _horizon_payload(stats) -> dict:
    return {
        "horizon": stats.horizon,
        "event_count": stats.event_count,
        "mean_return": _decimal_str(stats.mean_return),
        "median_return": _decimal_str(stats.median_return),
        "hit_rate": _decimal_str(stats.hit_rate),
        "baseline_event_count": stats.baseline_event_count,
        "baseline_mean_return": _decimal_str(stats.baseline_mean_return),
        "baseline_median_return": _decimal_str(stats.baseline_median_return),
        "difference_mean": _decimal_str(stats.difference_mean),
        "bootstrap_difference_ci": _bootstrap_payload(stats.bootstrap),
        "below_min_events_warning": stats.below_min_events_warning,
        "dropped_insufficient_bars": stats.dropped_insufficient_bars,
        "dropped_adjustment_boundary": stats.dropped_adjustment_boundary,
        "dropped_custody_scale_jump": stats.dropped_custody_scale_jump,
        "condition_not_evaluable": stats.condition_not_evaluable,
        "adjusted_close_used_count": stats.adjusted_close_used_count,
    }


@dataclass(frozen=True, slots=True)
class RunParameters:
    cost_bps: Decimal
    volume_ratio_tolerance: Decimal
    adjustment_ratio_epsilon: Decimal
    custody_scale_jump_ratio: Decimal


def run_study(
    spec: StudySpec, symbol_data: dict[str, SymbolData], *, split: str, params: RunParameters,
    symbol_scope: str = "declared",
) -> dict:
    """Build the full result dict for one split ("train" or "test").

    Refuses ("test_requires_frozen_spec") to build test-period results for
    a spec whose ``frozen`` is ``false`` -- the pre-registration gate. Train
    results may always be built, including for an unfrozen spec, since
    iterating on a spec against the train period is exactly what
    pre-registration precedes.

    ``symbol_scope``: ``"declared"`` (default) uses ``symbols_for_split``;
    ``"holdout_only"`` (test split only) evaluates only the spec's
    ``symbol_holdout`` -- what ``scripts/run_study.py --split test`` uses
    under the frozen BIST 30 split guard, so a one-shot holdout evaluation
    never mixes in development symbols.
    """
    if split not in ("train", "test"):
        raise EngineError("invalid_split")
    if symbol_scope not in ("declared", "holdout_only"):
        raise EngineError("invalid_symbol_scope")
    if split == "test" and not spec.frozen:
        raise EngineError("test_requires_frozen_spec")

    if symbol_scope == "holdout_only":
        if split != "test":
            raise EngineError("holdout_only_requires_test_split")
        if not spec.symbol_holdout:
            raise EngineError("holdout_only_requires_symbol_holdout")
        required_symbols = spec.symbol_holdout
    else:
        required_symbols = symbols_for_split(spec, split)
    missing = [symbol for symbol in required_symbols if symbol not in symbol_data]
    if missing:
        raise EngineError(f"missing_symbol_data: {','.join(sorted(missing))}")

    per_symbol: dict[str, dict[int, tuple[list, list]]] = {}
    for symbol in required_symbols:
        data = symbol_data[symbol]
        signal_dates = signal_dates_for_split(data.capture, spec.train_test_cutoff, split)
        per_symbol[symbol] = {
            horizon: build_events(
                data, spec, horizon=horizon, cost_bps=params.cost_bps, signal_dates=signal_dates,
            )
            for horizon in spec.horizons_trading_days
        }

    overall_by_horizon = {}
    per_symbol_payload: dict[str, dict] = {}
    for horizon in spec.horizons_trading_days:
        all_conditioned = [event for symbol in required_symbols for event in per_symbol[symbol][horizon][0]]
        all_baseline = [event for symbol in required_symbols for event in per_symbol[symbol][horizon][1]]
        overall_by_horizon[horizon] = summarize_horizon(
            all_conditioned, all_baseline, horizon=horizon,
            min_event_warning_threshold=spec.min_event_warning_threshold,
            bootstrap_seed=spec.bootstrap.seed, bootstrap_resamples=spec.bootstrap.resamples,
            bootstrap_confidence_level=spec.bootstrap.confidence_level,
        )

    for symbol in required_symbols:
        per_symbol_payload[symbol] = {
            str(horizon): _horizon_payload(summarize_horizon(
                per_symbol[symbol][horizon][0], per_symbol[symbol][horizon][1], horizon=horizon,
                min_event_warning_threshold=spec.min_event_warning_threshold,
                bootstrap_seed=spec.bootstrap.seed, bootstrap_resamples=spec.bootstrap.resamples,
                bootstrap_confidence_level=spec.bootstrap.confidence_level,
            ))
            for horizon in spec.horizons_trading_days
        }

    return {
        "schema": RESULT_SCHEMA,
        "formula_version": "study_engine_v1",
        "spec_id": spec.spec_id,
        "spec_sha256": spec.spec_sha256,
        "spec_frozen": spec.frozen,
        "split": split,
        "symbol_scope": symbol_scope,
        "symbols": list(required_symbols),
        "symbol_holdout": list(spec.symbol_holdout),
        "train_test_cutoff": spec.train_test_cutoff.isoformat(),
        "entry_timing": spec.entry_timing,
        "adjustment_boundary_policy": spec.adjustment_boundary_policy,
        "parameters": {
            "transaction_cost_bps": str(params.cost_bps),
            "volume_ratio_tolerance": str(params.volume_ratio_tolerance),
            "adjustment_ratio_epsilon": str(params.adjustment_ratio_epsilon),
            "custody_scale_jump_ratio": str(params.custody_scale_jump_ratio),
        },
        "overall": {str(horizon): _horizon_payload(stats) for horizon, stats in overall_by_horizon.items()},
        "per_symbol": per_symbol_payload,
        "constraints": (
            "Bu rapor tarihsel istatistiklerdir; yatırım tavsiyesi veya al/sat "
            "sinyali değildir.",
            "Baseline aynı sembol evreninde tüm günleri kapsar; koşullu olay "
            "kümesinin bir üst kümesidir, ayrık bir tümleyen değildir.",
            "Bootstrap güven aralığı float ile hesaplanan istatistiksel bir "
            "tahmindir; kaynak finansal değer değildir.",
            "custody_change özelliği 'yabancı takas' gibi bir sınıflandırmayı "
            "doğrulamaz; yalnız spec'in verdiği kod kümesinin toplam lot "
            "değişimini ölçer.",
        ),
    }


def build_reproducibility_block(
    *, project_root: Path, spec: StudySpec, input_files: dict[str, str], seed: int,
) -> dict:
    return {
        "code_commit": resolve_code_commit(project_root),
        "spec_sha256": spec.spec_sha256,
        "input_file_sha256": input_files,
        "bootstrap_seed": seed,
        "python_version": platform.python_version(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def render_text(result: dict) -> str:
    """Short, Turkish, numbers-only-from-result summary."""
    lines = [
        f"Study: {result['spec_id']} ({result['spec_sha256'][:12]}...), split={result['split']}, "
        f"frozen={'evet' if result['spec_frozen'] else 'hayır'}",
        f"Semboller: {', '.join(result['symbols'])} (hold-out: {', '.join(result['symbol_holdout']) or 'yok'})",
        f"train/test sınırı: {result['train_test_cutoff']}; giriş: {result['entry_timing']}; "
        f"düzeltme sınırı politikası: {result['adjustment_boundary_policy']}; "
        f"işlem maliyeti: {result['parameters']['transaction_cost_bps']} bps",
    ]
    guard = result.get("split_guard")
    if guard is not None:
        # First, so a --no-split-guard warning cannot be missed.
        lines[:0] = render_guard_lines(guard)
    lines.append("")
    for horizon, stats in result["overall"].items():
        warn = " [UYARI: minimum olay eşiğinin altında]" if stats["below_min_events_warning"] else ""
        lines.append(
            f"Ufuk {horizon} gün: olay={stats['event_count']}, ortalama={stats['mean_return']}, "
            f"medyan={stats['median_return']}, hit_rate={stats['hit_rate']}, "
            f"baseline_ortalama={stats['baseline_mean_return']} (n={stats['baseline_event_count']}), "
            f"fark={stats['difference_mean']}, "
            f"bootstrap_CI=[{stats['bootstrap_difference_ci']['lower']}, "
            f"{stats['bootstrap_difference_ci']['upper']}]{warn}"
        )
        lines.append(
            f"  düşürülen: yetersiz_bar={stats['dropped_insufficient_bars']}, "
            f"düzeltme_sınırı={stats['dropped_adjustment_boundary']}, "
            f"takas_ölçek_sıçraması={stats['dropped_custody_scale_jump']}, "
            f"değerlendirilemeyen_koşul={stats['condition_not_evaluable']}, "
            f"adjusted_close_kullanımı={stats['adjusted_close_used_count']}"
        )
    lines += ["", "Sınırlar:"] + [f"  - {text}" for text in result["constraints"]]
    return "\n".join(lines) + "\n"
