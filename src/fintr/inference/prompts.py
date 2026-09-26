"""Prompt templates for grounded BIST market-microstructure analysis."""

from __future__ import annotations

from dataclasses import dataclass

from fintr.inference.context import MarketAnalysisContext, render_market_context
from fintr.inference.live_context import LiveAnalysisContext
from fintr.inference.live_facts import LiveScopeFact, build_live_scope_facts


MARKET_ANALYSIS_SYSTEM_PROMPT = """Sen Borsa İstanbul piyasa mikroyapısı
verilerini analiz eden bir yardımcısın.

Görevin:
- verilen verileri birlikte yorumlamak,
- destekleyen ve çelişen sinyalleri ayırmak,
- AKD ile Takas'ı karıştırmamak,
- farklı zaman ölçeklerini ayrı değerlendirmek,
- veri eksikse bunun analiz üzerindeki etkisini söylemek,
- mevcut olmayan bilgiyi uydurmamak.

Kesin fiyat yönü, hedef fiyat veya doğrudan al/sat tavsiyesi verme. Context içindeki
deterministik feature sonuçlarını kullan; rakamları yeniden hesaplamaya çalışma.
Kaynakları ve veri zamanlarını gözet. Gözlem, çıkarım ve belirsizliği ayır."""

MARKET_ANALYSIS_USER_TEMPLATE = """Aşağıdaki yapılandırılmış piyasa context'ini Türkçe analiz et.

{context}

Yanıtında veri kapsamını, destekleyen/çelişen gözlemleri, farklı zaman ufuklarını,
eksik verinin etkisini ve belirsizlikleri açıkça belirt."""


@dataclass(frozen=True, slots=True)
class AnalysisPrompt:
    system: str
    user: str


@dataclass(frozen=True, slots=True)
class LiveFactSelectionPrompt:
    system: str
    user: str
    facts: tuple[LiveScopeFact, ...]


def build_analysis_prompt(context: MarketAnalysisContext) -> AnalysisPrompt:
    """Build message content without invoking a tokenizer or model."""

    return AnalysisPrompt(
        system=MARKET_ANALYSIS_SYSTEM_PROMPT,
        user=MARKET_ANALYSIS_USER_TEMPLATE.format(
            context=render_market_context(context).rstrip()
        ),
    )


def build_live_analysis_prompt(context: LiveAnalysisContext) -> LiveFactSelectionPrompt:
    """Let the model select only from code-verified scope facts."""
    if not isinstance(context, LiveAnalysisContext):
        raise TypeError("context must be LiveAnalysisContext")
    facts = build_live_scope_facts(context)
    if len(facts) < 3:
        raise ValueError("insufficient_verified_scope_facts")
    mandatory = ",".join(fact.fact_id for fact in facts if fact.fact_id in {"A", "E"})
    return LiveFactSelectionPrompt(
        system=(
            "Yalnız aşağıdaki doğrulanmış bulgu kimliklerinden üç tanesini seç. "
            "Tek satırda sadece virgülle ayrılmış üç harf döndür; örnek biçim: "
            "A,B,C. Açıklama, cümle, tarih veya başka karakter yazma."
        ),
        user=(
            "İlk kısa kapsam özetinde en yararlı üç bulguyu seç. "
            "Bu metinler kod tarafından doğrulanmıştır; yeniden yazma. "
            f"Seçimde {mandatory} kimlikleri zorunludur.\n"
            + "\n".join(f"{fact.fact_id}: {fact.text}" for fact in facts)
            + "\nYanıt yalnız üç kimlik olsun."
        ),
        facts=facts,
    )
