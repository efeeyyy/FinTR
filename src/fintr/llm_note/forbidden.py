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
import unicodedata


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
    ("kâr", r"(?<!\w)(?:kar|karlı\w*|karı|karın|profit\w*)(?!\w)"),
    # circumflex form with any tail (kârı, kârlılık); checked on the unfolded text
    # only -- folded to ASCII, ``kar\w*`` would match "karar"/"karşı"
    ("kâr", r"(?<!\w)kâr\w*"),
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
_TR_LOWER = str.maketrans({"İ": "i", "I": "ı"})
# Turkish letters with diacritics -> ASCII (the same text typed without them)
_TR_FOLD = str.maketrans("çğıöşüâîû", "cgiosuaiu")
# Cyrillic/Greek letters that look like Latin ones -> Latin (applied to lowercased text)
_HOMOGLYPHS = str.maketrans({
    **dict(zip("аеорсухкмтнвіѕј", "aeopcyxkmthbisj")),   # Cyrillic
    **dict(zip("αονικρτυχε", "aovikptuxe")),            # Greek
})
# Letters separated by spaces or dots ("k a z a n ç", "h.e.d.e.f"): joined before matching.
_SPACED_LETTERS = re.compile(r"(?<![a-z0-9])(?:[a-z][ \t._*\-]+){2,}[a-z](?![a-z0-9])")
# Longer stems are also looked for in the letters of the whole text with every
# separator removed ("ka zanç", "get iri"). Only stems of >= 6 letters: a short
# stem would show up across ordinary word boundaries.
_SQUASH_STEMS = (
    ("kazanç", "kazanc"), ("getiri", "getiri"), ("tavsiye", "tavsiye"), ("manipülasyon", "manipul"),
    ("pozisyon", "pozisyon"), ("garanti/mutlaka", "garanti"), ("fırsat", "firsat"), ("tahmin", "tahmin"),
)


def _compile(label: str, pattern: str) -> tuple[str, re.Pattern, re.Pattern | None]:
    # a pattern relying on the circumflex has no ASCII-folded twin
    folded = None if "â" in pattern else re.compile(pattern.translate(_TR_FOLD))
    return label, re.compile(pattern), folded


_COMPILED = tuple(_compile(label, pattern) for label, pattern in _PATTERNS)


def tr_lower(text: str) -> str:
    return text.translate(_TR_LOWER).lower()


def _clean(text: str) -> str:
    """Turkish-lowercased text in Unicode compatibility composition (``a`` +
    U+0302 -> ``â``, fullwidth and ligature forms -> plain), with invisible
    format characters (zero-width space/joiner, soft hyphen, BOM) and any
    remaining combining marks removed."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Mn"))
    return tr_lower(text)


def _fold(text: str) -> str:
    return text.translate(_HOMOGLYPHS).translate(_TR_FOLD)


def find_forbidden(text: str, *, allowed_phrases: tuple[str, ...] = ()) -> list[str]:
    """Labels of every forbidden stem found in ``text`` (empty = clean).

    The text is compared as written (Unicode-normalised, invisible characters
    removed), with Turkish diacritics and look-alike Cyrillic/Greek letters
    folded to ASCII, with letter-by-letter spelling joined, and (for long stems
    only) with all separators removed."""
    lowered = _clean(text)
    for phrase in sorted({*ALLOWED_PHRASES, *allowed_phrases}, key=len, reverse=True):
        if phrase:
            lowered = lowered.replace(_clean(phrase), " ")
    folded = _fold(lowered)
    joined = _SPACED_LETTERS.sub(lambda m: " " + re.sub(r"[^a-z]", "", m.group(0)) + " ", folded)
    found = {label for label, original, _ in _COMPILED if original.search(lowered)}
    for view in (folded, joined):
        found |= {label for label, _, ascii_pattern in _COMPILED
                  if ascii_pattern is not None and ascii_pattern.search(view)}
    letters = re.sub(r"[^a-z]", "", folded)
    found |= {label for label, stem in _SQUASH_STEMS if stem in letters}
    return sorted(found)
