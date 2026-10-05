"""Forbidden wording in model-written note text (fail-closed).

AGENTS.md "Güvenlik sınırları" (exception of 2026-10-02): the note may only
assess evidence; it must never produce a buy/sell order, target, stop,
position, return forecast, score, certainty claim or a manipulation/intent
claim. This is a *stem* list over Turkish-lowercased text with word
boundaries (so ``kesinleşmemiş``, a scanner term, is allowed while
``kesin`` is not). Full institution titles from the packet and the fixed
disclaimer phrases are masked first -- a broker whose title happens to
contain a listed word must not reject the note (short titles are not
masked: a single common word must not whitelist that word everywhere).
"""

from __future__ import annotations

import re


ALLOWED_PHRASES = (
    "yatırım tavsiyesi değildir",
    "doğrulanmamış hipotez",
)

# (label, regex over Turkish-lowercased text). ``(?<!\w)``/``(?!\w)`` are
# Unicode word boundaries.
_PATTERNS: tuple[tuple[str, str], ...] = (
    ("al", r"(?<!\w)al(?!\w)"),
    ("sat", r"(?<!\w)sat(?!\w)"),
    ("buy/sell", r"(?<!\w)(?:buy|sell)(?!\w)"),
    ("alın/satın", r"(?<!\w)(?:alın|satın|alınız|satınız)(?!\w)"),
    ("hedef", r"hedef\w*"),
    ("target", r"(?<!\w)target\w*"),
    ("stop", r"(?<!\w)stop\w*"),
    ("pozisyon", r"pozisyon\w*"),
    ("position", r"(?<!\w)position\w*"),
    # inflected buy/sell/hold advice ("almalı", "satılmalı", "alınabilir", "tutmalı")
    ("almalı/satmalı", r"(?<!\w)(?:al|sat)(?:ın|ıl)?(?:mal[ıi]|abilir)\w*"),
    ("tutmalı", r"(?<!\w)(?:elde\s+)?tut(?:mal|ulmal|abilir)\w*"),
    # forecast / certainty / loss wording
    ("yükselecek/düşecek", r"(?<!\w)(?:(?:yüksel|düş)ecek|(?:art|azal)acak)\w*"),
    ("beklenti", r"(?<!\w)(?:beklenti\w*|bekleniyor|beklenir|beklenmekte)(?!\w)"),
    ("garanti/mutlaka", r"(?<!\w)(?:garanti\w*|mutlaka)(?!\w)"),
    ("zarar", r"zarar\w*"),
    ("kesin", r"(?<!\w)kesin(?:lik\w*|likle)?(?!\w)"),
    ("kâr", r"(?<!\w)(?:kâr|kar|kârlı|karlı|profit)(?!\w)"),
    ("getiri", r"getiri\w*"),
    ("kazanç", r"kazan[çc]\w*"),
    ("return", r"(?<!\w)returns?(?!\w)"),
    ("tahmin", r"tahmin\w*"),
    ("öneri", r"öneri\w*"),
    ("tavsiye", r"tavsiye\w*"),
    ("puan/skor", r"(?<!\w)(?:puan\w*|skor\w*|score\w*|rank\w*)"),
    ("fırsat", r"fırsat\w*"),
    ("manipülasyon", r"manipül\w*|manipul\w*"),
    ("niyet/kasıt", r"(?<!\w)(?:niyet\w*|kasıt\w*|kasit\w*)"),
)
_COMPILED = tuple((label, re.compile(pattern)) for label, pattern in _PATTERNS)
_TR_LOWER = str.maketrans({"İ": "i", "I": "ı"})


def tr_lower(text: str) -> str:
    return text.translate(_TR_LOWER).lower()


def find_forbidden(text: str, *, allowed_phrases: tuple[str, ...] = ()) -> list[str]:
    """Labels of every forbidden stem found in ``text`` (empty = clean)."""
    lowered = tr_lower(text)
    for phrase in sorted({*ALLOWED_PHRASES, *allowed_phrases}, key=len, reverse=True):
        if phrase:
            lowered = lowered.replace(tr_lower(phrase), " ")
    return sorted({label for label, pattern in _COMPILED if pattern.search(lowered)})
