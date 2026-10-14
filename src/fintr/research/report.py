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
from fintr.features.price_join import DEFAULT_RATIO_REGIME_TOLERANCE
from fintr.research.engine import (
    FORMULA_VERSION,
    EngineError,
    SymbolData,
    build_events,
    drop_ratio_regime_signal_dates,
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


def _block_bootstrap_payload(block) -> dict | None:
    if block is None:
        return None
    return {
        "method": block.method,
        "block_length_signal_dates": block.block_length_signal_dates,
        "resamples": block.resamples,
        "seed": block.seed,
        "status": block.status,
        "distinct_signal_dates": block.distinct_signal_dates,
        "distinct_conditioned_signal_dates": block.distinct_conditioned_signal_dates,
        "observed_difference": block.observed_difference,
        "predicted_direction": block.predicted_direction,
        "p_value_one_sided": block.p_value_one_sided,
        "percentile_05": block.percentile_05,
        "percentile_95": block.percentile_95,
        "undefined_resamples": block.undefined_resamples,
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
        "distinct_conditioned_signal_dates": stats.distinct_conditioned_signal_dates,
        "block_bootstrap_difference": _block_bootstrap_payload(stats.block_bootstrap),
    }


def build_hypothesis_test(spec: StudySpec, primary_stats) -> dict | None:
    """Per-spec pre-registered test summary at the primary horizon.

    Reads only numbers already computed by the engine. ``p_value_for_holm``
    is set only for a ``primary`` family member: the E1 one-sided p, or 1
    when the minimum-event rule fails (the family size never shrinks). A
    ``secondary_descriptive`` hypothesis never gets one -- it does not enter
    the Holm family (``fintr.research.family``).
    """
    prereg = spec.preregistration
    if prereg is None:
        return None
    block = primary_stats.block_bootstrap
    min_events_met = (
        primary_stats.event_count >= prereg.min_events_primary
        and primary_stats.distinct_conditioned_signal_dates >= prereg.min_distinct_signal_dates_primary
    )
    p_value = None if block is None else block.p_value_one_sided
    is_primary = prereg.family_role == "primary"
    p_for_holm = None
    if is_primary:
        p_for_holm = p_value if (min_events_met and p_value is not None) else 1.0
    bound = None
    if block is not None:
        bound = block.percentile_05 if prereg.predicted_direction == "negative" else block.percentile_95
    sesoi = float(prereg.sesoi_difference)
    sesoi_excluded = None
    if bound is not None:
        sesoi_excluded = bound > -sesoi if prereg.predicted_direction == "negative" else bound < sesoi
    return {
        "hypothesis_id": prereg.hypothesis_id,
        "family_id": prereg.family_id,
        "family_role": prereg.family_role,
        "enters_holm_family": is_primary,
        "primary_family_members": list(prereg.primary_family_members),
        "family_size": prereg.family_size,
        "multiple_comparison": prereg.multiple_comparison,
        "familywise_alpha": str(prereg.familywise_alpha),
        "predicted_direction": prereg.predicted_direction,
        "primary_horizon_trading_days": prereg.primary_horizon_trading_days,
        "difference_mean": _decimal_str(primary_stats.difference_mean),
        "event_count": primary_stats.event_count,
        "distinct_conditioned_signal_dates": primary_stats.distinct_conditioned_signal_dates,
        "min_events_primary": prereg.min_events_primary,
        "min_distinct_signal_dates_primary": prereg.min_distinct_signal_dates_primary,
        "min_events_met": min_events_met,
        "block_bootstrap_status": None if block is None else block.status,
        "p_value_one_sided": p_value,
        "p_value_for_holm": p_for_holm,
        "sesoi_difference": str(prereg.sesoi_difference),
        "one_sided_bound": bound,
        "one_sided_bound_percentile": "5" if prereg.predicted_direction == "negative" else "95",
        "sesoi_excluded_in_predicted_direction": sesoi_excluded,
    }


@dataclass(frozen=True, slots=True)
class RunParameters:
    cost_bps: Decimal
    volume_ratio_tolerance: Decimal
    adjustment_ratio_epsilon: Decimal
    custody_scale_jump_ratio: Decimal
    ratio_regime_tolerance: Decimal = DEFAULT_RATIO_REGIME_TOLERANCE


def _ratio_regime_payload(regime) -> dict:
    return {
        "start": regime.start.isoformat(),
        "end": regime.end.isoformat(),
        "k": regime.k,
        "day_count": regime.day_count,
        "label": regime.label,
    }


def run_study(
    spec: StudySpec, symbol_data: dict[str, SymbolData], *, split: str, params: RunParameters,
    symbol_scope: str = "declared", test_window_end: date | None = None,
    event_sink: dict | None = None,
) -> dict:
    """Build the full result dict for one split ("train" or "test").

    ``event_sink`` (opt-in, used only by the synthetic calibration harness
    ``scripts/calibrate_engine.py``): when a dict is given, the pooled
    ``(conditioned, baseline)`` event lists of every horizon are stored in it
    under the horizon; the returned result is identical with or without it.

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

    ``test_window_end`` (test split only, E3): signal dates after it are
    dropped (``fintr.research.engine.signal_dates_for_split``'s
    ``window_end``); ``docs/prereg/DECISIONS.md`` 2026-09-28's pre-registered
    test-window end, applied by ``scripts/run_study.py`` from the test plan.
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
    ratio_regime_quality: dict[str, dict] = {}
    for symbol in required_symbols:
        data = symbol_data[symbol]
        raw_signal_dates = signal_dates_for_split(
            data.capture, spec.train_test_cutoff, split, window_end=test_window_end,
        )
        signal_dates, dropped_regime_count = drop_ratio_regime_signal_dates(
            raw_signal_dates, data.ratio_regime_dates,
        )
        ratio_regime_quality[symbol] = {
            "regimes": [_ratio_regime_payload(regime) for regime in data.ratio_regimes],
            "dropped_signal_dates": dropped_regime_count,
        }
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
        if event_sink is not None:
            event_sink[horizon] = (all_conditioned, all_baseline)
        overall_by_horizon[horizon] = summarize_horizon(
            all_conditioned, all_baseline, horizon=horizon,
            min_event_warning_threshold=spec.min_event_warning_threshold,
            bootstrap_seed=spec.bootstrap.seed, bootstrap_resamples=spec.bootstrap.resamples,
            bootstrap_confidence_level=spec.bootstrap.confidence_level,
            block_bootstrap=spec.block_bootstrap,
            predicted_direction=(
                None if spec.preregistration is None else spec.preregistration.predicted_direction
            ),
        )

    # Per-symbol blocks keep only the i.i.d. diagnostic: E1 is defined on the
    # pooled date panel (same-day cross-symbol dependence), not per symbol.
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
        "formula_version": FORMULA_VERSION,
        "spec_id": spec.spec_id,
        "spec_sha256": spec.spec_sha256,
        "spec_frozen": spec.frozen,
        "split": split,
        "symbol_scope": symbol_scope,
        "test_window_end": None if test_window_end is None else test_window_end.isoformat(),
        "symbols": list(required_symbols),
        "symbol_holdout": list(spec.symbol_holdout),
        "train_test_cutoff": spec.train_test_cutoff.isoformat(),
        "entry_timing": spec.entry_timing,
        "adjustment_boundary_policy": spec.adjustment_boundary_policy,
        "parameters": {
            "transaction_cost_bps": str(params.cost_bps),
            "transaction_cost_basis": "per_side (round_trip = 2 x transaction_cost_bps)",
            "volume_ratio_tolerance": str(params.volume_ratio_tolerance),
            "adjustment_ratio_epsilon": str(params.adjustment_ratio_epsilon),
            "custody_scale_jump_ratio": str(params.custody_scale_jump_ratio),
            "ratio_regime_tolerance": str(params.ratio_regime_tolerance),
        },
        "ratio_regime_quality": ratio_regime_quality,
        "overall": {str(horizon): _horizon_payload(stats) for horizon, stats in overall_by_horizon.items()},
        "hypothesis_test": (
            None if spec.preregistration is None else build_hypothesis_test(
                spec, overall_by_horizon[spec.preregistration.primary_horizon_trading_days],
            )
        ),
        "per_symbol": per_symbol_payload,
        "constraints": (
            "Bu rapor tarihsel istatistiklerdir; yatırım tavsiyesi veya al/sat "
            "sinyali değildir.",
            "Baseline aynı sembol evreninde tüm günleri kapsar; koşullu olay "
            "kümesinin bir üst kümesidir, ayrık bir tümleyen değildir.",
            "Bootstrap güven aralığı float ile hesaplanan istatistiksel bir "
            "tahmindir; kaynak finansal değer değildir.",
            "i.i.d. bootstrap aralığı yalnız ikincil tanılamadır; birincil test "
            "sinyal tarihine göre hareketli blok bootstrap'tır (E1).",
            "İşlem maliyeti taraf başınadır: net = brüt − 2 × bps/10000 "
            "(gidiş-dönüş = 2×).",
            "custody_change özelliği 'yabancı takas' gibi bir sınıflandırmayı "
            "doğrulamaz; yalnız spec'in verdiği kod kümesinin toplam lot "
            "değişimini ölçer.",
            "ratio_regime içindeki sinyal günleri hem koşullu hem baseline "
            "kümesinden düşülür (docs/prereg/DECISIONS.md); getiriler "
            "Yahoo'nun kendi içinde tutarlı serisinden hesaplandığı için "
            "ayrıca etkilenmez, yalnız sinyal günü seçimi değişir.",
        ),
    }


def build_reproducibility_block(
    *, project_root: Path, spec: StudySpec, input_files: dict[str, str], seed: int,
) -> dict:
    block = spec.block_bootstrap
    return {
        "code_commit": resolve_code_commit(project_root),
        "spec_sha256": spec.spec_sha256,
        "input_file_sha256": input_files,
        "bootstrap_seed": seed,
        "block_bootstrap": None if block is None else {
            "seed": block.seed, "resamples": block.resamples,
            "block_length_signal_dates": block.block_length_signal_dates,
        },
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
        f"işlem maliyeti: {result['parameters']['transaction_cost_bps']} bps/taraf "
        f"(gidiş-dönüş 2×)",
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
        block = stats.get("block_bootstrap_difference")
        if block is not None:
            lines.append(
                f"  E1 blok bootstrap (L={block['block_length_signal_dates']}, B={block['resamples']}, "
                f"seed={block['seed']}): durum={block['status']}, "
                f"sinyal_tarihi={block['distinct_signal_dates']} "
                f"(koşullu {block['distinct_conditioned_signal_dates']}), "
                f"yön={block['predicted_direction']}, p_tek_yönlü={block['p_value_one_sided']}, "
                f"%5={block['percentile_05']}, %95={block['percentile_95']}"
            )
    test = result.get("hypothesis_test")
    if test is not None:
        lines += [
            "",
            f"Ön kayıt: {test['hypothesis_id']} rol={test['family_role']} "
            f"(Holm ailesine {'girer' if test['enters_holm_family'] else 'girmez'}; "
            f"aile={','.join(test['primary_family_members'])}, α={test['familywise_alpha']}), "
            f"asgari_olay_sağlandı={'evet' if test['min_events_met'] else 'hayır'}, "
            f"p_tek_yönlü={test['p_value_one_sided']}, p_holm_girdisi={test['p_value_for_holm']}",
        ]
    regime_quality = result.get("ratio_regime_quality") or {}
    if regime_quality:
        lines += [
            "",
            f"Rejim veri kalitesi (ratio_regime_tolerance="
            f"{result['parameters']['ratio_regime_tolerance']}):",
        ]
        for symbol, report in regime_quality.items():
            regimes = report["regimes"]
            regime_text = "; ".join(
                f"{r['start']}..{r['end']} k={r['k']} gün={r['day_count']}" for r in regimes
            ) or "yok"
            lines.append(
                f"  {symbol}: rejim={len(regimes)} ({regime_text}), "
                f"düşülen_sinyal_günü={report['dropped_signal_dates']}"
            )
    merges = result.get("capture_merge") or {}
    merged_lines = [
        f"  {symbol}: capture={report['capture_count']}, çakışan_tarih={len(report['overlap_dates'])}, "
        f"ek_yeri_boşluğu={'VAR' if report['any_seam_gap'] else 'yok'}"
        for symbol, report in merges.items()
        if report["capture_count"] > 1 or report["any_seam_gap"]
    ]
    if merged_lines:
        lines += ["", "E4 capture birleştirme (ayrıntı JSON'da capture_merge):"] + merged_lines
    lines += ["", "Sınırlar:"] + [f"  - {text}" for text in result["constraints"]]
    return "\n".join(lines) + "\n"
