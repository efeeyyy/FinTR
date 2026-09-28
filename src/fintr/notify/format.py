"""Pure formatting: ``fintr_scanner_observations_v1`` JSON -> Turkish
Telegram message text.

No I/O, no network, no new financial claim -- every line here only
restates a field already present in the scanner's saved observation
document (``docs/scanner.md``) or the fixed short rule descriptions passed
in by the caller (``configs/notify/rule_texts_v1.json``). This is an
observation notice, not a signal: nothing in this module computes or
emits a score, rank, return, target or buy/sell recommendation.
"""

from __future__ import annotations

from fintr.scanner.observe import NOTICE_TEXT_TR, VALIDATION_STATUS


OBSERVATIONS_SCHEMA = "fintr_scanner_observations_v1"
TELEGRAM_MESSAGE_LIMIT = 4096

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


def _observation_line(symbol: str, rule_texts: dict, obs: dict) -> str | None:
    if obs.get("condition_met") is not True:
        return None
    rule_id = obs.get("rule_id", "?")
    description = rule_texts.get(rule_id, rule_id)
    features_text = "; ".join(_feature_text(f) for f in obs.get("features", []))
    line = f"{symbol} {rule_id}: {description}"
    if features_text:
        line += f" — {features_text}"
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


def _footer_lines(document: dict) -> list[str]:
    warning = document.get("warning") or NOTICE_TEXT_TR
    return [warning, f"Durum: {VALIDATION_STATUS}"]


def render_notification_text(document: dict, rule_texts: dict) -> str:
    """Pure: render the full, unsplit Turkish notification text for one
    ``fintr_scanner_observations_v1`` document."""
    lines = list(_header_lines(document))
    lines.append("")

    observation_lines: list[str] = []
    freshness_lines: list[str] = []
    for symbol in sorted(document.get("symbols", {})):
        block = document["symbols"][symbol]
        for scan in block.get("scans", []):
            as_of = scan.get("as_of", "")
            for obs in scan.get("observations", []):
                line = _observation_line(symbol, rule_texts, obs)
                if line is not None:
                    observation_lines.append(line)
            freshness_lines.extend(_freshness_lines(symbol, as_of, scan.get("freshness", {})))

    if observation_lines:
        lines.extend(observation_lines)
    else:
        lines.append("Bu taramada condition_met=true gözlem yok.")
    lines.append("")

    if freshness_lines:
        lines.extend(sorted(set(freshness_lines)))
        lines.append("")

    lines.append(_suppressed_line(document))
    lines.append("")
    lines.extend(_footer_lines(document))
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


def render_notification(document: dict, rule_texts: dict) -> list[str]:
    """Pure: ``document`` -> one or more plain-text messages, each at most
    ``TELEGRAM_MESSAGE_LIMIT`` characters, split on line boundaries."""
    return split_message(render_notification_text(document, rule_texts))
