"""Forbidden-wording guard for notification text.

AGENTS.md "Guvenlik sinirlari": the notifier emits no score, rank, return,
target, stop, profit or free-form buy/sell wording; the only buy/sell words
allowed are the two fixed direction-label phrases (Efe's decision of
2026-10-02). ``tests/test_notify.py`` has always enforced this on rendered
text; the layout-v2 rule explanation file is also checked here at load
time (``fintr.notify.explanations``), so a bad edit to a fixed text fails
before anything is sent.

Matching is whole-word and case-insensitive, with allowed phrases removed
first. Python's ``str.lower`` is not locale aware ("YUKARI" -> "yukari"),
so allowed phrases are lowered the same way as the text before removal.
"""

from __future__ import annotations

import re


FORBIDDEN_WORDS = (
    "score", "rank", "return", "target", "al", "sat", "öneri", "kesin", "hedef", "stop", "kâr",
)


def find_forbidden_words(text: str, allowed_phrases: tuple[str, ...] = ()) -> list[str]:
    """Forbidden words found in ``text`` after removing ``allowed_phrases``
    (each removed wherever it occurs); empty when the text is clean."""
    lowered = text.lower()
    for phrase in allowed_phrases:
        lowered = lowered.replace(phrase.lower(), "")
    found: list[str] = []
    for word in FORBIDDEN_WORDS:
        if re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", lowered):
            found.append(word)
    return found
