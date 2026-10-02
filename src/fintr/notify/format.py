"""Pure formatting: ``fintr_scanner_observations_v1``/``_v2`` JSON -> Turkish
Telegram message text.

No I/O, no network, no new financial claim -- every line here only
restates a field already present in the scanner's saved observation
document (``docs/scanner.md``) or the fixed short rule descriptions passed
in by the caller (``configs/notify/rule_texts_v1.json``). This is an
observation notice: nothing in this module computes or emits a score,
rank, return, target, stop level, position size or a free-form buy/sell
recommendation.

Single, narrow exception (Efe's decision of 2026-10-02, AGENTS.md
"Guvenlik sinirlari", ``docs/prereg/DECISIONS.md``; personal use only):
when the caller passes ``directions`` (rule_id -> the frozen spec's
``predicted_direction``), each ``condition_met`` line may carry exactly
one of the two fixed phrases ``DIRECTION_LABELS_TR`` -- the frozen rule's
*hypothesised* direction, not a forecast. A line whose rule_id has no
exact ``positive``/``negative`` entry gets ``DIRECTION_UNKNOWN_TR``
instead (never a guess). Every message that contains a label also
contains ``DIRECTION_WARNING_TR`` exactly once. ``directions=None``
reproduces the pre-exception text byte for byte.
"""

from __future__ import annotations

from fintr.scanner.observe import NOTICE_TEXT_TR, VALIDATION_STATUS


OBSERVATIONS_SCHEMA_V1 = "fintr_scanner_observations_v1"
# v2: emitted only by ``scan_observations.py --daily-root`` (adds a
# per-symbol ``composition`` block and a per-scan ``data_status`` marker;
# see docs/scanner.md). Both are accepted here -- an older saved v1 file
# keeps rendering exactly as before (no composition block means no data
# status line below), and a v2 file additionally gets one.
OBSERVATIONS_SCHEMA_V2 = "fintr_scanner_observations_v2"
OBSERVATIONS_SCHEMA = OBSERVATIONS_SCHEMA_V1  # kept for existing callers of the default schema
SUPPORTED_OBSERVATIONS_SCHEMAS = (OBSERVATIONS_SCHEMA_V1, OBSERVATIONS_SCHEMA_V2)
TELEGRAM_MESSAGE_LIMIT = 4096

# Efe decision 2026-10-02 (see module docstring). These are the ONLY buy/sell
# wording this module may emit; tests/test_notify.py allows exactly these.
DIRECTION_LABELS_TR = {
    "positive": "beklenen yön: yukarı (al yönlü)",
    "negative": "beklenen yön: aşağı (sat yönlü)",
}
DIRECTION_UNKNOWN_TR = "yön bilinmiyor"
DIRECTION_WARNING_TR = (
    "Uyarı: yön etiketi doğrulanmamış ön kayıtlı hipotezdir "
    "(holdout testi 2027 Nisan); yatırım tavsiyesi değildir."
)
# Replaces the scanner's fixed notice (which says "al/sat sinyali değildir")
# only in a message that actually carries a direction label.
NOTICE_TEXT_DIRECTION_TR = (
    "Gözlem bildirimidir; kişisel kullanım; doğrulanmamış hipotez yönü; yatırım tavsiyesi, "
    "niyet veya manipülasyon iddiası değildir. Kurallar ön kayıtlı hipotezdir, "
    "holdout testi bekleniyor."
)

_SUPPRESSED_REASONS = ("ratio_regime", "custody_scale_jump")
_SUPPRESSED_LABELS_TR = {
    "ratio_regime": "oran rejimi",
    "custody_scale_jump": "takas ölçek sıçraması",
}
_FRESHNESS_FIELDS_TR = (
    ("last_akd_server_date", "AKD"),
    ("last_custody_date", "takas"),
    ("last_price_date", "fiyat"),
)


def has_positive_observations(document: dict) -> bool:
    """True if any observation in ``document`` has ``condition_met is True``."""
    for block in document.get("symbols", {}).values():
        for scan in block.get("scans", []):
            for obs in scan.get("observations", []):
                if obs.get("condition_met") is True:
                    return True
    return False


def _as_of_span(document: dict) -> str:
    as_of_values = sorted({
        scan["as_of"]
        for block in document.get("symbols", {}).values()
        for scan in block.get("scans", [])
    })
    if not as_of_values:
        return "bilinmiyor"
    if len(as_of_values) == 1:
        return as_of_values[0]
    return f"{as_of_values[0]}..{as_of_values[-1]}"


def _header_lines(document: dict) -> list[str]:
    return [
        "FinTR tarayıcı gözlem bildirimi",
        f"as_of: {_as_of_span(document)}",
        f"Kural dosyası: {document.get('rules_file', 'bilinmiyor')} "
        f"(sha256={document.get('rules_file_sha256', 'bilinmiyor')})",
    ]


def _feature_text(feature: dict) -> str:
    value = feature.get("value")
    text = f"{feature.get('name', '?')}={value if value is not None else 'yok'}"
    labels = feature.get("institution_labels") or {}
    resolved = sorted(
        f"{code} {label['title']}"
        for code, label in labels.items()
        if isinstance(label, dict) and label.get("status") == "resolved" and label.get("title")
    )
    if resolved:
        text += " (kurum: " + ", ".join(resolved) + ")"
    return text


def _direction_text(rule_id: str, directions: dict) -> str:
    """The fixed label for ``rule_id``, or the unknown marker. Only the exact
    strings ``positive``/``negative`` resolve; anything else (missing rule,
    ``None``, other value, non-string) is unknown -- never guessed."""
    direction = directions.get(rule_id)
    if isinstance(direction, str):
        label = DIRECTION_LABELS_TR.get(direction)
        if label is not None:
            return label
    return DIRECTION_UNKNOWN_TR


def _observation_line(
    symbol: str, rule_texts: dict, obs: dict, directions: dict | None = None,
) -> str | None:
    if obs.get("condition_met") is not True:
        return None
    rule_id = obs.get("rule_id", "?")
    description = rule_texts.get(rule_id, rule_id)
    features_text = "; ".join(_feature_text(f) for f in obs.get("features", []))
    line = f"{symbol} {rule_id}: {description}"
    if features_text:
        line += f" — {features_text}"
    if directions is not None:
        line += f" — {_direction_text(rule_id, directions)}"
    return line


def _freshness_lines(symbol: str, as_of: str, freshness: dict) -> list[str]:
    lines: list[str] = []
    if freshness.get("custody_flagged_stale_t_plus_2"):
        lines.append(
            f"{symbol} ({as_of}): takas verisi T+2 gerisinde (son takas="
            f"{freshness.get('last_custody_date') or 'yok'}, son AKD="
            f"{freshness.get('last_akd_server_date') or 'yok'})"
        )
    missing = [label for key, label in _FRESHNESS_FIELDS_TR if freshness.get(key) is None]
    if missing:
        lines.append(f"{symbol} ({as_of}): eksik veri ({', '.join(missing)})")
    return lines


def _data_status_line(symbol: str, composition: dict) -> str:
    """One line, per symbol, of composed-data counts only -- no value, claim
    or buy/sell wording (ADR §4: ``--daily-root`` output only).

    Counts are window-bounded when the observation file carries the
    ``window`` field (docs/scanner.md); an older v2 file without it falls
    back to the whole-period totals, labelled as such. A file with
    ``window: null`` had no rolling input, so no window count exists."""
    if "window" not in composition:
        gaps = composition.get("gaps", {}).get("count", 0)
        unsettled = composition.get("unsettled", {}).get("day_count", 0)
        revisions = composition.get("revisions", {}).get("day_count", 0)
        scope = "tüm dönem"
    else:
        window = composition["window"]
        if window is None:
            return f"{symbol} veri durumu: pencere yok (günlük kayıt yok)"
        gaps = composition.get("gaps", {}).get("in_window_count", 0)
        unsettled = composition.get("unsettled", {}).get("in_window_day_count", 0)
        revisions = composition.get("revisions", {}).get("in_window_day_count", 0)
        scope = f"{window['start']}..{window['end']}"
    return (
        f"{symbol} veri durumu ({scope}): boşluk={gaps}, kesinleşmemiş gün={unsettled}, "
        f"revize gün={revisions}"
    )


def _data_status_lines(document: dict) -> list[str]:
    lines: list[str] = []
    for symbol in sorted(document.get("symbols", {})):
        composition = document["symbols"][symbol].get("composition")
        if composition is not None:
            lines.append(_data_status_line(symbol, composition))
    return lines


def _suppressed_counts(document: dict) -> dict[str, int]:
    counts = {reason: 0 for reason in _SUPPRESSED_REASONS}
    for block in document.get("symbols", {}).values():
        for scan in block.get("scans", []):
            for obs in scan.get("observations", []):
                reason = obs.get("suppressed")
                if reason in counts:
                    counts[reason] += 1
    return counts


def _suppressed_line(document: dict) -> str:
    counts = _suppressed_counts(document)
    parts = [f"{_SUPPRESSED_LABELS_TR[reason]}={counts[reason]}" for reason in _SUPPRESSED_REASONS]
    return "Bastırılan gözlemler: " + ", ".join(parts)


def _footer_lines(document: dict, *, labelled: bool = False) -> list[str]:
    if labelled:
        warning = NOTICE_TEXT_DIRECTION_TR
    else:
        warning = document.get("warning") or NOTICE_TEXT_TR
    return [warning, f"Durum: {VALIDATION_STATUS}"]


def _has_label(text: str) -> bool:
    return any(label in text for label in DIRECTION_LABELS_TR.values())


def render_notification_text(
    document: dict, rule_texts: dict, directions: dict | None = None,
) -> str:
    """Pure: render the full, unsplit Turkish notification text for one
    ``fintr_scanner_observations_v1`` document.

    ``directions`` is ``None`` (no direction wording at all -- the original
    text) or a rule_id -> ``predicted_direction`` mapping (see module
    docstring); an empty mapping marks every observation "yön bilinmiyor"."""
    lines = list(_header_lines(document))
    lines.append("")

    observation_lines: list[str] = []
    freshness_lines: list[str] = []
    for symbol in sorted(document.get("symbols", {})):
        block = document["symbols"][symbol]
        for scan in block.get("scans", []):
            as_of = scan.get("as_of", "")
            for obs in scan.get("observations", []):
                line = _observation_line(symbol, rule_texts, obs, directions)
                if line is not None:
                    observation_lines.append(line)
            freshness_lines.extend(_freshness_lines(symbol, as_of, scan.get("freshness", {})))

    labelled = any(_has_label(line) for line in observation_lines)
    if observation_lines:
        if labelled:
            lines.append(DIRECTION_WARNING_TR)
            lines.append("")
        lines.extend(observation_lines)
    else:
        lines.append("Bu taramada condition_met=true gözlem yok.")
    lines.append("")

    if freshness_lines:
        lines.extend(sorted(set(freshness_lines)))
        lines.append("")

    data_status_lines = _data_status_lines(document)
    if data_status_lines:
        lines.extend(data_status_lines)
        lines.append("")

    lines.append(_suppressed_line(document))
    lines.append("")
    lines.extend(_footer_lines(document, labelled=labelled))
    return "\n".join(lines) + "\n"


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split ``text`` on line boundaries so no chunk exceeds ``limit``
    characters; a single line longer than ``limit`` is hard-split as a last
    resort (this module never produces one today)."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks


def render_notification(
    document: dict, rule_texts: dict, directions: dict | None = None,
) -> list[str]:
    """Pure: ``document`` -> one or more plain-text messages, each at most
    ``TELEGRAM_MESSAGE_LIMIT`` characters, split on line boundaries.

    Every message that contains a direction label carries
    ``DIRECTION_WARNING_TR`` exactly once: the full text has it once near
    the top (first message); a later chunk with a label gets it prepended,
    so the split limit is reduced by that line's length when labels exist."""
    text = render_notification_text(document, rule_texts, directions)
    if not _has_label(text):
        return split_message(text)
    reserve = len(DIRECTION_WARNING_TR) + 1
    chunks = split_message(text, TELEGRAM_MESSAGE_LIMIT - reserve)
    return [
        chunk if DIRECTION_WARNING_TR in chunk or not _has_label(chunk)
        else DIRECTION_WARNING_TR + "\n" + chunk
        for chunk in chunks
    ]
