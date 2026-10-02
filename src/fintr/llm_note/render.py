"""Telegram text for the analysis note, and the integrity check before it is shown.

Used by ``scripts/notify_observations.py --llm-note-dir``. The note is sent
as separate message(s) after the deterministic notification; with no
accepted note, a single status line is sent instead. This module adds no
financial wording of its own: it prints the validated assessment and the
validated reasons, plus fixed Turkish framing. The direction label (the
same two fixed phrases ``fintr.notify.format`` uses, Efe's 2026-10-02
decision) is shown only when the caller passes ``directions``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fintr.llm_note.record import latest_accepted, latest_attempt
from fintr.llm_note.validate import validate_note
from fintr.notify import format as fmt
from fintr.scanner.observe import VALIDATION_STATUS


TITLE = "Opus 5.5 analiz notu — doğrulanmamış hipotez değerlendirmesi, yatırım tavsiyesi değildir"
STATUS_PREFIX = "Opus 5.5 analiz notu"
VERIFIED_FOOTER = (
    "Rakamlar ve kanıt yolları girdi paketiyle kodla denetlendi; model çıktısı deterministik "
    "değildir; kişisel kullanım, doğrulanmamış hipotez değerlendirmesidir."
)


class NoteIntegrityError(ValueError):
    """The accepted note on disk no longer matches its record/packet."""


def load_verified_note(attempt_dir: Path, record: dict) -> tuple[dict, dict]:
    """(note, packet) of an accepted attempt, re-validated from the files.
    Raises ``NoteIntegrityError`` on any mismatch."""
    try:
        packet_bytes = (attempt_dir / "packet.json").read_bytes()
        note = json.loads((attempt_dir / "note.json").read_text(encoding="utf-8"))
        packet = json.loads(packet_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise NoteIntegrityError("files_unreadable") from None
    if hashlib.sha256(packet_bytes).hexdigest() != record.get("inputs", {}).get("packet_sha256"):
        raise NoteIntegrityError("packet_sha256_mismatch")
    if not validate_note(note, packet).ok:
        raise NoteIntegrityError("revalidation_failed")
    return note, packet


def status_line(root: Path, as_of: str, *, has_observations: bool = True) -> str:
    """The single status line used when no verified note is available."""
    if not has_observations:
        return f"{STATUS_PREFIX}: bu taramada gözlem yok; not üretilmedi."
    latest = latest_attempt(root, as_of)
    if latest is None:
        return f"{STATUS_PREFIX}: bu as_of için not yok (adım çalıştırılmadı)."
    _, record = latest
    status, category = record.get("status"), record.get("category") or "bilinmiyor"
    if status == "rejected":
        return f"{STATUS_PREFIX}: analiz notu doğrulamadan geçemedi ({category})."
    return f"{STATUS_PREFIX}: analiz notu alınamadı ({category})."


def _direction_text(packet: dict, rule_id: str, directions: dict | None) -> str:
    if directions is None:
        return ""
    direction = packet.get("rules", {}).get(rule_id, {}).get("predicted_direction")
    label = fmt.DIRECTION_LABELS_TR.get(direction) if isinstance(direction, str) else None
    return f" ({label or fmt.DIRECTION_UNKNOWN_TR})"


def _blocks(note: dict, packet: dict, directions: dict | None) -> list[str]:
    blocks: list[str] = []
    for item in note["assessments"]:
        lines = [
            f"{item['symbol']} {item['rule_id']}{_direction_text(packet, item['rule_id'], directions)}"
            f" — değerlendirme: {item['assessment']}"
        ]
        lines += [f"• {reason['text']}" for reason in item["reasons"]]
        blocks.append("\n".join(lines))
    if note["data_caveats"]:
        blocks.append("\n".join(["Veri notları:"] + [f"• {c}" for c in note["data_caveats"]]))
    return blocks


def render_note_messages(
    attempt_dir: Path, record: dict, note: dict, packet: dict, *, directions: dict | None,
    limit: int = fmt.TELEGRAM_MESSAGE_LIMIT,
) -> list[str]:
    header = [TITLE, f"as_of: {packet['as_of']}"]
    if directions is not None:
        header.append(fmt.DIRECTION_WARNING_TR)
    blocks = _blocks(note, packet, directions)
    omitted = record.get("inputs", {}).get("omitted") or []
    if omitted:
        blocks.append(
            "Kapsam dışı (girdi paketi boyut sınırı): "
            + ", ".join(sorted(entry["symbol"] for entry in omitted))
        )
    footer = [VERIFIED_FOOTER, f"Kayıt: {attempt_dir.as_posix()}", f"Durum: {VALIDATION_STATUS}"]
    room = limit - len("\n".join(header)) - 2
    pieces: list[str] = []
    for text in blocks + ["\n".join(footer)]:
        pieces.extend(fmt.split_message(text, room))
    messages: list[str] = []
    current: list[str] = []
    for piece in pieces:
        candidate = "\n".join(header + current + [piece])
        if current and len(candidate) > limit:
            messages.append("\n".join(header + current) + "\n")
            current = []
        current.append(piece)
    messages.append("\n".join(header + current) + "\n")
    return messages


def note_messages(
    root: Path, as_of: str, *, directions: dict | None, has_observations: bool = True,
) -> list[str]:
    """The message(s) for the notifier: the verified note, else one status line."""
    accepted = latest_accepted(root, as_of)
    if accepted is not None:
        attempt_dir, record = accepted
        try:
            note, packet = load_verified_note(attempt_dir, record)
        except NoteIntegrityError:
            return [f"{STATUS_PREFIX}: analiz notu doğrulamadan geçemedi (record_integrity).\n"]
        return render_note_messages(attempt_dir, record, note, packet, directions=directions)
    return [status_line(root, as_of, has_observations=has_observations) + "\n"]
