"""Notification layout v3: the Telegram "signal chat" -- one short message a
day, one line per stock (Efe's decision of 2026-10-03, ``docs/notify.md``).

Pure: no I/O, no network, no clock. Takes a saved
``fintr_scanner_observations_v1``/``_v2`` document and returns the Turkish
plain-text message (normally exactly one; see ``render_layout_v3`` for the
fallback when it would exceed ``TELEGRAM_MESSAGE_LIMIT``).

Shape::

    📊 <GG.AA> sinyaller (doğrulanmamış hipotez, tavsiye değil)
    <SYM> ↑ al yönlü · H2+H4 · Opus: destekliyor
    <SYM> ↓ sat yönlü · H1 · Opus: —
    veri: 2 gün kesinleşmemiş          (only when the window has any)

* The direction comes only from the frozen rule's ``predicted_direction``
  (``directions``: rule_id -> ``positive``/``negative``). A rule without a
  resolved direction, or ``directions=None`` (``--no-direction-label``),
  writes no direction part -- never a guess.
* Rules of one stock with the same direction share one line (``H2+H4``);
  different directions get separate lines.
* ``Opus:`` is the evidence word (``destekliyor``/``zayıflatıyor``/
  ``belirsiz``) of the *accepted* analysis note for exactly that stock, rule
  and day (``assessments``), else ``—``. Merged rules whose words differ are
  shown per rule (``Opus: H2 destekliyor, H4 belirsiz``); the layout never
  invents or merges a word the note did not give.
* The data line counts, per kind, the largest number of unsettled days / gap
  days / revised days any scanned stock has inside its window (the saved
  ``composition`` counts, not a new figure); ``durum bilgisi yok`` when a
  stock has no window information at all.
* No condition met: the single line ``📊 <GG.AA> sinyal yok``.

No return, score, target, stop, position size, rule description or "what it
means" text is ever added here.
"""

from __future__ import annotations

from fintr.notify.format import TELEGRAM_MESSAGE_LIMIT
from fintr.notify.layout import _as_of_values, _data_signature, _hits, _parse_date


DIRECTION_SHORT_V3_TR = {"positive": "↑ al yönlü", "negative": "↓ sat yönlü"}
ASSESSMENT_WORDS = ("destekliyor", "zayıflatıyor", "belirsiz")
NO_ASSESSMENT = "—"
HEADER_SUFFIX_TR = "sinyaller (doğrulanmamış hipotez, tavsiye değil)"
_DIRECTION_ORDER = ("positive", "negative", None)


def _day_text(iso: str) -> str:
    parsed = _parse_date(iso)
    return parsed.strftime("%d.%m") if parsed is not None else str(iso)


def _date_range(document: dict, fallback_as_of: str | None) -> str:
    values = _as_of_values(document) or ([fallback_as_of] if fallback_as_of else [])
    if not values:
        return "tarih bilinmiyor"
    if len(values) == 1:
        return _day_text(values[0])
    return f"{_day_text(values[0])}–{_day_text(values[-1])}"


def _rule_direction(rule_id: str, directions: dict | None) -> str | None:
    direction = directions.get(rule_id) if directions is not None else None
    return direction if direction in DIRECTION_SHORT_V3_TR else None


def _assessment(assessments: dict | None, symbol: str, rule_id: str, as_of: str) -> str:
    word = (assessments or {}).get((symbol, rule_id, as_of))
    return word if word in ASSESSMENT_WORDS else NO_ASSESSMENT


def _opus_text(rule_ids: list[str], words: list[str]) -> str:
    if len(set(words)) == 1:
        return words[0]
    return ", ".join(f"{rule_id} {word}" for rule_id, word in zip(rule_ids, words))


def _rows(document: dict, hits: dict, directions: dict | None, assessments: dict | None) -> list[str]:
    multi_day = len(_as_of_values(document)) > 1
    rows: list[str] = []
    for (symbol, as_of), observations in hits.items():
        groups: dict[str | None, list[str]] = {}
        for obs in observations:
            rule_id = str(obs.get("rule_id", "?"))
            groups.setdefault(_rule_direction(rule_id, directions), []).append(rule_id)
        name = f"{symbol} ({_day_text(as_of)})" if multi_day else symbol
        for direction in _DIRECTION_ORDER:
            rule_ids = groups.get(direction)
            if not rule_ids:
                continue
            words = [_assessment(assessments, symbol, rule_id, as_of) for rule_id in rule_ids]
            parts = [name if direction is None else f"{name} {DIRECTION_SHORT_V3_TR[direction]}"]
            parts.append("+".join(rule_ids))
            parts.append(f"Opus: {_opus_text(rule_ids, words)}")
            rows.append(" · ".join(parts))
    return rows


def _data_line(document: dict) -> str | None:
    """One short line when any stock's window has unsettled days, gaps or
    revisions (or no window information); otherwise None."""
    unsettled = gaps = revisions = 0
    unknown = False
    for block in document.get("symbols", {}).values():
        signature = _data_signature(block.get("composition"))
        if signature[0] != "counts":
            unknown = True
            continue
        _kind, gap_count, unsettled_count, revision_count, _whole = signature
        gaps, unsettled, revisions = max(gaps, gap_count), max(unsettled, unsettled_count), max(revisions, revision_count)
    parts: list[str] = []
    if unsettled:
        parts.append(f"{unsettled} gün kesinleşmemiş")
    if gaps:
        parts.append(f"{gaps} gün boşluk")
    if revisions:
        parts.append(f"{revisions} gün revize")
    if unknown:
        parts.append("durum bilgisi yok")
    return "veri: " + ", ".join(parts) if parts else None


def render_layout_v3(
    document: dict, directions: dict | None = None, assessments: dict | None = None, *,
    as_of: str | None = None, limit: int = TELEGRAM_MESSAGE_LIMIT,
) -> list[str]:
    """Pure: ``document`` -> the day's signal message(s).

    ``directions``: ``None`` = no direction part; otherwise rule_id ->
    ``predicted_direction``. ``assessments``: (symbol, rule_id, as_of) ->
    evidence word of an accepted note; anything else prints ``—``. ``as_of``:
    date for the header when the document has no scan (the no-signal line).

    Normally one message. Only if the lines would exceed ``limit`` (more
    stocks than a chat message holds) the lines are continued in further
    messages, each repeating the header line."""
    day = _date_range(document, as_of)
    hits = _hits(document)
    if not hits:
        return [f"📊 {day} sinyal yok"]
    header = f"📊 {day} {HEADER_SUFFIX_TR}"
    lines = _rows(document, hits, directions, assessments)
    data_line = _data_line(document)
    if data_line is not None:
        lines.append(data_line)
    messages: list[str] = []
    current = [header]
    for line in lines:
        if len(current) > 1 and len("\n".join([*current, line])) > limit:
            messages.append("\n".join(current))
            current = [header]
        current.append(line)
    messages.append("\n".join(current))
    return messages
