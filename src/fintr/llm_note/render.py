"""Telegram text for the analysis note, and the integrity check before it is shown.

Used by ``scripts/notify_observations.py --llm-note-dir``. The note is sent
as separate message(s) after the deterministic notification. One model call
was made per symbol, so each symbol has its own outcome: a verified note, or
its own status line ("alınamadı"/"doğrulamadan geçemedi" with the category),
plus one summary line with the counts. This module adds no financial wording
of its own: it prints the validated assessments and reasons, plus fixed
Turkish framing. The direction label (the same two fixed phrases
``fintr.notify.format`` uses, Efe's 2026-10-02 decision) is shown only when
the caller passes ``directions``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from fintr.llm_note.record import latest_accepted, latest_attempt, recorded_symbols, symbol_dir
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


@dataclass(slots=True)
class SymbolState:
    symbol: str
    kind: str                      # ok | rejected | failed | not_run
    category: str | None = None
    attempt_dir: Path | None = None
    record: dict | None = None
    note: dict | None = None
    packet: dict | None = None

    @property
    def status_line(self) -> str:
        if self.kind == "rejected":
            return f"{self.symbol}: analiz notu doğrulamadan geçemedi ({self.category})."
        if self.kind == "failed":
            return f"{self.symbol}: analiz notu alınamadı ({self.category})."
        return f"{self.symbol}: bu as_of için not yok (adım çalıştırılmadı)."


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


def symbol_state(root: Path, as_of: str, symbol: str) -> SymbolState:
    try:
        parent = symbol_dir(root, as_of, symbol)
    except ValueError:
        return SymbolState(symbol, "not_run")
    accepted = latest_accepted(parent)
    if accepted is not None:
        attempt_dir, record = accepted
        try:
            note, packet = load_verified_note(attempt_dir, record)
        except NoteIntegrityError:
            return SymbolState(symbol, "rejected", "record_integrity", attempt_dir, record)
        return SymbolState(symbol, "ok", None, attempt_dir, record, note, packet)
    latest = latest_attempt(parent)
    if latest is None:
        return SymbolState(symbol, "not_run")
    attempt_dir, record = latest
    category = record.get("category") or "bilinmiyor"
    kind = "rejected" if record.get("status") == "rejected" else "failed"
    return SymbolState(symbol, kind, category, attempt_dir, record)


def summary_line(states: list[SymbolState]) -> str:
    counts = {kind: sum(1 for s in states if s.kind == kind) for kind in ("ok", "rejected", "failed", "not_run")}
    text = (f"doğrulanan {counts['ok']}, doğrulamadan geçemeyen {counts['rejected']}, "
            f"alınamayan {counts['failed']}")
    if counts["not_run"]:
        text += f", çalıştırılmayan {counts['not_run']}"
    return text


def _direction_text(packet: dict, rule_id: str, directions: dict | None) -> str:
    if directions is None:
        return ""
    direction = packet.get("rules", {}).get(rule_id, {}).get("predicted_direction")
    label = fmt.DIRECTION_LABELS_TR.get(direction) if isinstance(direction, str) else None
    return f" ({label or fmt.DIRECTION_UNKNOWN_TR})"


def _blocks(state: SymbolState, directions: dict | None) -> list[str]:
    blocks: list[str] = []
    for item in state.note["assessments"]:
        lines = [
            f"{item['symbol']} {item['rule_id']}"
            f"{_direction_text(state.packet, item['rule_id'], directions)}"
            f" — değerlendirme: {item['assessment']}"
        ]
        lines += [f"• {reason['text']}" for reason in item["reasons"]]
        blocks.append("\n".join(lines))
    if state.note["data_caveats"]:
        blocks.append("\n".join([f"Veri notları ({state.symbol}):"] + [f"• {c}" for c in state.note["data_caveats"]]))
    return blocks


def _chunk(header: list[str], pieces_in: list[str], limit: int) -> list[str]:
    room = limit - len("\n".join(header)) - 2
    pieces: list[str] = []
    for text in pieces_in:
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


def render_note_messages(
    states: list[SymbolState], root: Path, as_of: str, *, directions: dict | None,
    limit: int = fmt.TELEGRAM_MESSAGE_LIMIT,
) -> list[str]:
    """The verified notes (title, warning, per-symbol assessments), the
    per-symbol status lines of the symbols without a note, the counts line
    and the fixed footer."""
    header = [TITLE, f"as_of: {as_of}"]
    if directions is not None:
        header.append(fmt.DIRECTION_WARNING_TR)
    blocks: list[str] = []
    for state in states:
        if state.kind == "ok":
            blocks.extend(_blocks(state, directions))
    lacking = [s.status_line for s in states if s.kind != "ok"]
    if lacking:
        blocks.append("\n".join(lacking))
    blocks.append(f"Özet: {summary_line(states)}.")
    footer = [VERIFIED_FOOTER, f"Kayıt: {(Path(root) / as_of).as_posix()}", f"Durum: {VALIDATION_STATUS}"]
    return _chunk(header, blocks + ["\n".join(footer)], limit)


def render_status_messages(states: list[SymbolState], *, limit: int = fmt.TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """No verified note at all: the counts and each symbol's own status line."""
    header = [f"{STATUS_PREFIX}: {summary_line(states)}."]
    return _chunk(header, [s.status_line for s in states], limit)


def note_messages(
    root: Path, as_of: str, *, directions: dict | None, has_observations: bool = True,
    expected_symbols: list[str] | None = None, limit: int = fmt.TELEGRAM_MESSAGE_LIMIT,
) -> list[str]:
    """The message(s) for the notifier. ``expected_symbols``: the symbols with
    a ``condition_met`` observation (default: those with records on disk)."""
    if not has_observations:
        return [f"{STATUS_PREFIX}: bu taramada gözlem yok; not üretilmedi.\n"]
    symbols = sorted(expected_symbols) if expected_symbols is not None else recorded_symbols(root, as_of)
    states = [symbol_state(root, as_of, symbol) for symbol in symbols]
    if not states or all(s.kind == "not_run" for s in states):
        return [f"{STATUS_PREFIX}: bu as_of için not yok (adım çalıştırılmadı).\n"]
    if any(s.kind == "ok" for s in states):
        return render_note_messages(states, root, as_of, directions=directions, limit=limit)
    return render_status_messages(states, limit=limit)
