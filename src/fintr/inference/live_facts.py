"""Allowlisted, code-rendered scope facts for one validated live context."""

from __future__ import annotations

from dataclasses import dataclass
import re

from fintr.inference.live_context import LiveAnalysisContext


@dataclass(frozen=True, slots=True)
class LiveScopeFact:
    fact_id: str
    text: str
    source_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LiveFactSelection:
    fact_ids: tuple[str, ...]
    rendered_lines: tuple[str, ...]


def build_live_scope_facts(context: LiveAnalysisContext) -> tuple[LiveScopeFact, ...]:
    """Build true statements only from verified context fields, not model prose."""
    if not isinstance(context, LiveAnalysisContext):
        raise TypeError("context must be LiveAnalysisContext")
    data = context.to_dict()
    akd = data["akd"]
    custodian = data["custodian"]
    distribution = data["custody_distribution"]
    stats = data["custody_stats"]
    facts: list[LiveScopeFact] = []
    facts.append(LiveScopeFact(
        "A",
        "AKD yanıtı mevcut. Sembol ve tarih aralığı sunucunun tekrar ettiği alanlar değil, yerel gönderilmiş istek bağlamıdır.",
        ("akd.source_event", "akd.local_sent_request"),
    ))
    if custodian["included_rows"] > 0 and (
        custodian["server_date"] == custodian["local_sent_request"]["date"]
    ):
        facts.append(LiveScopeFact(
            "B",
            "Günlük Takas yanıtındaki sunucu tarihi, yerel istek tarihiyle eşleşir; bu eşleşme diğer veri ailelerine taşınmaz.",
            ("custodian.server_date", "custodian.local_sent_request.date"),
        ))
    if distribution["included_rows"] > 0 and distribution["periods_from_server"]:
        facts.append(LiveScopeFact(
            "C",
            "Takas dağılımının dönemleri sunucu yanıtından gelir; sembol bağlantısı yerel isteğe dayanır.",
            ("custody_distribution.periods_from_server", "custody_distribution.local_sent_symbol"),
        ))
    series_names = ("retail_percentage", "institution_percentage", "investor_count")
    if all(stats[name]["included_points"] > 0 for name in series_names):
        facts.append(LiveScopeFact(
            "D",
            "Takas istatistiklerinin üç serisi bağlamda mevcuttur; sembol bağlantısı yerel isteğe dayanır.",
            ("custody_stats.local_sent_request", "custody_stats.retail_percentage",
             "custody_stats.institution_percentage", "custody_stats.investor_count"),
        ))
    if (akd["truncated"] or custodian["truncated"] or distribution["truncated"]
            or any(stats[name]["truncated"] for name in series_names)):
        facts.append(LiveScopeFact(
            "E",
            "LLM bağlamındaki satır veya seri noktaları sınırlandırılmıştır; dışarıda kalan veriler sıfır kabul edilmez.",
            ("akd.truncated", "custodian.truncated", "custody_distribution.truncated",
             "custody_stats.*.truncated"),
        ))
    if data["brokerage_labels"]["directory_rows"] > 0:
        facts.append(LiveScopeFact(
            "F",
            "Kurum etiketleri tarihsiz bir dizinden yalnız kesin kod eşleşmesiyle bağlanır; nihai yatırımcı kimliği değildir.",
            ("brokerage_labels.dated", "brokerage_labels.used_only_for_exact_code_matches"),
        ))
    return tuple(facts)


def select_live_scope_facts(
    model_text: str, facts: tuple[LiveScopeFact, ...], *, max_new_tokens: int,
    generated_tokens: int,
) -> LiveFactSelection:
    """Accept only three exact allowlisted IDs; model text is never rendered."""
    if not isinstance(model_text, str) or not isinstance(facts, tuple):
        raise TypeError("invalid_live_fact_selection_input")
    if (
        type(max_new_tokens) is not int
        or type(generated_tokens) is not int
        or max_new_tokens < 1
        or generated_tokens < 1
    ):
        raise ValueError("invalid_live_fact_token_count")
    if generated_tokens >= max_new_tokens:
        raise ValueError("generation_token_cap_reached")
    candidate = model_text.strip()
    if not re.fullmatch(r"[A-Z](?:\s*,\s*[A-Z]){2}", candidate):
        raise ValueError("invalid_fact_id_format")
    selected_ids = tuple(part.strip() for part in candidate.split(","))
    if len(set(selected_ids)) != 3:
        raise ValueError("duplicate_fact_id")
    by_id = {fact.fact_id: fact for fact in facts}
    if any(fact_id not in by_id for fact_id in selected_ids):
        raise ValueError("unknown_fact_id")
    mandatory_ids = {fact_id for fact_id in ("A", "E") if fact_id in by_id}
    if not mandatory_ids.issubset(selected_ids):
        raise ValueError("mandatory_fact_missing")
    return LiveFactSelection(
        fact_ids=selected_ids,
        rendered_lines=tuple(
            f"[{fact_id}] {by_id[fact_id].text} "
            f"[kaynak alanları: {', '.join(by_id[fact_id].source_fields)}]"
            for fact_id in selected_ids
        ),
    )
