"""Prompt templates for grounded BIST market-microstructure analysis."""

from __future__ import annotations

from dataclasses import dataclass

from fintr.inference.context import MarketAnalysisContext, render_market_context


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


def build_analysis_prompt(context: MarketAnalysisContext) -> AnalysisPrompt:
    """Build message content without invoking a tokenizer or model."""

    return AnalysisPrompt(
        system=MARKET_ANALYSIS_SYSTEM_PROMPT,
        user=MARKET_ANALYSIS_USER_TEMPLATE.format(
            context=render_market_context(context).rstrip()
        ),
    )
