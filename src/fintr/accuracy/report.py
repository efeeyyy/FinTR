"""Output document (``fintr_accuracy_v1``) and Turkish markdown for the tracker.

Serialization only: every number comes from ``fintr.accuracy.core`` (counts,
Wilson intervals, base rates) or from the saved inputs' hashes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import platform

from fintr.accuracy.core import (
    COST_BPS_PER_SIDE,
    FORMULAS,
    PERIOD_POST_TRAIN,
    PERIOD_TRAIN,
    PERIODS,
    TEST_WINDOW_START,
    TRAIN_WINDOW_END,
    TrackResult,
    period_counts,
    summarize,
)
from fintr.scanner.report import resolve_code_commit

ACCURACY_SCHEMA = "fintr_accuracy_v1"

WARNING_TR = (
    "Tanımlayıcı izleme; yatırım tavsiyesi, al/sat sinyali, niyet veya manipülasyon iddiası "
    "değildir. Kurallar ön kayıtlı hipotezdir; holdout testi bekleniyor. Küçük ve bağımlı "
    "örneklem: güven aralığı olayları bağımsız varsayar (örtüşen vadeler, aynı gün hisseler arası "
    "bağımlılık), bu yüzden gerçek belirsizlik daha geniştir."
)

K4_NOTICE = (
    "K4 yalnız 20 geliştirme hissesi için Efe'nin 2026-10-16 kararıyla kaldırıldı "
    "(docs/prereg/DECISIONS.md, TRIAL_LOG T-31); holdout sembolleri izlenmez. 2026-10-16'dan "
    "itibaren 30 sembollü birincil holdout testi geliştirme sembolleri bakımından kör değildir "
    "(E5, yalnız holdout, kör kalır). Kural, eşik ve spec'ler dondurulmuştur; bir kural bu "
    "sayılar görüldükten sonra değişirse o kuralın holdout penceresi değişiklik tarihinde yeniden başlar."
)

LOOK_AHEAD_NOTES = (
    "reconstructed tahminler yalnız t ve öncesi tarihli satırları, takasta yalnız t'ye kadar "
    "kesinleşmiş (T+lag) günleri ve t'ye kadarki fiyat barlarını görür; t sonrası verinin "
    "değişmesi bu tahmini değiştiremez (test edildi).",
    "Artık ileriye bakış: yeniden kurulan günlerde t veya öncesi tarihli AKD/metadata satırları, "
    "t'den SONRA alınmış yakalamalardan (taban yakalama 2026-09-26'da, günlük yakalamalar sonra) "
    "gelen nihai/revize değerler olabilir; o günkü canlı tarayıcı bu değerleri henüz görmemiş "
    "olabilirdi. Kayıtlı tahminle yeniden kurulumun uyuşması reconstruction_check'te sayılır.",
    "Fiyat getirisi (sonuç) bilerek ileriye bakar: giriş t+1 açılışı, çıkış vade sonu kapanışı.",
    "Bildirim zamanı: günlük toplama pencere sonu R-1'dir; bir günün sinyali çoğu zaman t+1 "
    "akşamı üretilir, yani getiri tanımındaki t+1 açılış girişi bildirimi alan biri için "
    "gerçekleşebilir olmayabilir. Bu izleme dondurulmuş çalışma tanımını ölçer, işlem getirisini değil.",
)


def build_document(
    *, project_root: Path, run_date: str, parameters: dict, rules_file: str,
    rules_file_sha256: str, rules: tuple, split_guard: dict, input_files: dict,
    prediction_provenance: dict, look_ahead_exposure: dict, result: TrackResult,
) -> dict:
    rule_ids = [rule.rule_id for rule in rules]
    return {
        "schema": ACCURACY_SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "run_date": run_date,
        "code_commit": resolve_code_commit(project_root),
        "python_version": platform.python_version(),
        "decision": {
            "k4_lifted_for_development_symbols_only": True,
            "holdout_symbols_monitored": False,
            "decided_by": "Efe", "decided_on": "2026-10-16",
            "records": ["docs/prereg/DECISIONS.md", "docs/prereg/TRIAL_LOG.md#T-31"],
            "telegram_output": False,
        },
        "warning": WARNING_TR,
        "k4_notice": K4_NOTICE,
        "parameters": parameters,
        "rules": {
            "rules_file": rules_file, "rules_file_sha256": rules_file_sha256,
            "rules": [
                {
                    "rule_id": rule.rule_id, "spec_path": rule.spec_path,
                    "spec_raw_sha256": rule.spec_sha256,
                    "spec_canonical_sha256": rule.spec.spec_sha256,
                    "predicted_direction": rule.spec.preregistration.predicted_direction,
                    "family_role": rule.family_role,
                }
                for rule in rules
            ],
        },
        "split_guard": split_guard,
        "input_files": input_files,
        "prediction_provenance": prediction_provenance,
        "look_ahead": {"notes": list(LOOK_AHEAD_NOTES), "exposure": look_ahead_exposure},
        "formulas": FORMULAS,
        "counts": period_counts(result),
        "evaluation_counts": result.evaluation_counts,
        "skipped_days": result.skipped_days,
        "reconstruction_check": result.reconstruction_check,
        "summary": summarize(result, rule_ids),
        "rows": result.rows,
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _stat_row(label: str, stat: dict) -> str:
    d, c = stat["directional"], stat["cost_aware"]
    interval = (
        "—" if d["wilson95_low"] is None
        else f"{_pct(d['wilson95_low'])}..{_pct(d['wilson95_high'])}"
    )
    diff = "—" if d["difference"] is None else f"{d['difference'] * 100:+.1f} puan"
    return (
        f"| {label} | {stat['signals']} | {stat['resolved']} | {stat['pending']} | "
        f"{d['hits']} | {stat['zero_return']} | {_pct(d['hit_rate'])} | {interval} | "
        f"{_pct(d['base_rate'])} | {diff} | {_pct(c['hit_rate'])} (taban {_pct(c['base_rate'])}) |"
    )


def render_markdown(document: dict) -> str:
    params = document["parameters"]
    lines = [
        f"# İsabet izleme — {document['run_date']}",
        "",
        document["warning"],
        "",
        document["k4_notice"],
        "",
        f"- Pencere: {params['window_start']} .. {params['window_end']} "
        f"({params['window_day_count']} işlem günü), {len(params['symbols'])} geliştirme hissesi.",
        f"- Dönemler: `{PERIOD_TRAIN}` = sinyal günü <= {TRAIN_WINDOW_END.isoformat()} "
        f"(örneklem içi: kurallar bu veriyle kuruldu); `{PERIOD_POST_TRAIN}` = >= "
        f"{(TRAIN_WINDOW_END + timedelta(days=1)).isoformat()}. "
        "İki dönem asla tek sayıda toplanmaz.",
        f"- Vade: birincil {', '.join(str(h) for h in params['primary_horizons'])} işlem günü; "
        f"ikincil {params['secondary_horizon']} işlem günü (motor tanımı: giriş t+1 açılışı, çıkış "
        "`vade` işlem günü sonra kapanış). Maliyetli isabet: |brüt getiri| > "
        f"2x{COST_BPS_PER_SIDE} bp, tahmin yönünde. Sıfır getiri isabet sayılmaz.",
        f"- Kod: `{document['code_commit']}`; kural dosyası hash: `{document['rules']['rules_file_sha256']}`.",
        "",
        "## Sayımlar (sinyal / sonuçlanan / bekleyen)",
        "",
        "| Dönem | Vade | Sinyal | Sonuçlanan | Bekleyen | Düşen |",
        "|---|---|---|---|---|---|",
    ]
    for period in PERIODS:
        for horizon, c in document["counts"][period].items():
            lines.append(
                f"| {period} | {horizon} | {c['signals']} | {c['resolved']} | {c['pending']} | {c['dropped']} |"
            )
    check = document["reconstruction_check"]
    lines += [
        "",
        f"Kayıtlı tahminle yeniden kurulum karşılaştırması: {check['compared']} karşılaştırma, "
        f"{check['agree']} uyuşan, {check['disagree']} uyuşmayan.",
        "",
    ]
    for period in PERIODS:
        for horizon, block in document["summary"][period].items():
            base = block["baseline"]
            lines += [
                f"## {period} — vade {horizon} ({'birincil' if block['horizon_role'] == 'primary' else 'ikincil'})",
                "",
                f"Taban (koşulsuz, aynı hisse ve günler): {base['resolved']} sonuçlanan; yukarı "
                f"{_pct(base['up_fraction'])}, aşağı {_pct(base['down_fraction'])}, sıfır "
                f"{_pct(base['zero_fraction'])}.",
                "",
                "| Grup | Sinyal | Sonuçlanan | Bekleyen | İsabet | Sıfır | İsabet oranı | Wilson %95 | Taban | Fark | Maliyetli |",
                "|---|---|---|---|---|---|---|---|---|---|---|",
            ]
            for rule_id, stat in block["by_rule"].items():
                lines.append(_stat_row(rule_id, stat))
            for direction, stat in block["by_direction"].items():
                lines.append(_stat_row(f"yön: {direction}", stat))
            lines.append(_stat_row("toplam (kurallar örtüşür)", block["overall"]))
            lines.append("")
    lines += [
        "## Sınırlar",
        "",
        *(f"- {note}" for note in document["look_ahead"]["notes"]),
        "- Aynı hisse-gün birden çok kuralda görünebilir (H1/H3 ve H2/H4 koşulları örtüşür); "
        "`toplam` satırı bağımsız gözlem sayısı değildir.",
        f"- `in_test_window` satır alanı sinyal günü >= {TEST_WINDOW_START.isoformat()} olanı işaretler "
        "(birincil holdout penceresi; geliştirme sembolleri için artık kör değil).",
        "",
    ]
    return "\n".join(lines)
