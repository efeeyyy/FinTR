"""Resumable multi-message delivery for notification layout v2 and the
analysis note.

``scripts/notify_observations.py`` sends the layout's messages in order, with
a short pause between them. If a send fails part-way, what *was* sent is
recorded in a receipt and the next run for the same ``as_of`` sends only the
messages that were not sent -- never a duplicate of one that was.

Receipt files (``fintr_notify_receipt_v2``), under ``<out-dir>/<as_of>/``, are
create-only (``O_EXCL``) and never rewritten:

* a *session head* (``receipt.json``; ``receipt-<utc-stamp>[-n].json`` for a
  ``--resend``) is written by the run that starts a send session, and
* each later run that resumes an incomplete session writes a continuation
  ``<head-stem>.part-NNN.json`` beside it.

Every file carries the full per-message state *after* that run
(``message_status``), so the newest file of a session is its current state;
state is read as the union of all of the session's files. A session is
complete when every message index has a Telegram ``message_id``. Resuming
requires the new run's messages to hash identically (same observations,
same code, same texts) to the session's; otherwise nothing is sent and the
caller must pass ``--resend`` for a fresh, full send.

No token or chat id is ever written here: a failure is recorded only as the
failing message index and the exception class name.

The analysis note (``--llm-note-dir``) uses the same mechanism with its own
file family (``note_receipt.json``, ``note_receipt-<stamp>.json``,
``note_receipt.part-NNN.json``; schema ``fintr_notify_note_receipt_v2``): see
``NOTE_RECEIPTS``. The two families never see each other's files (the glob
``receipt*.json`` does not match ``note_receipt*``). A note receipt of the
earlier single-shot schema (``fintr_notify_note_receipt_v1``) is not a
resumable session: it counts as a complete send, so it blocks a second send
without ``--resend`` and is never silently resent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Callable


RECEIPT_SCHEMA_V2 = "fintr_notify_receipt_v2"
NOTE_RECEIPT_SCHEMA_V2 = "fintr_notify_note_receipt_v2"
DELIVERY_SCHEMA = "fintr_notify_delivery_v1"
PRIMARY_RECEIPT = "receipt.json"
_PART_PATTERN = re.compile(r"\.part-(\d{3})\.json$")


@dataclass(frozen=True, slots=True)
class ReceiptFamily:
    """One family of receipt files in a receipt directory."""
    stem: str  # file name stem: "receipt" or "note_receipt"
    schema: str  # schema of a resumable (v2) head of this family

    @property
    def primary(self) -> str:
        return f"{self.stem}.json"

    @property
    def resend_pattern(self) -> re.Pattern:
        return re.compile(rf"^{re.escape(self.stem)}-(\d{{8}}T\d{{6}}Z)(?:-(\d+))?\.json$")


DETERMINISTIC_RECEIPTS = ReceiptFamily("receipt", RECEIPT_SCHEMA_V2)
NOTE_RECEIPTS = ReceiptFamily("note_receipt", NOTE_RECEIPT_SCHEMA_V2)


class DeliveryConfigError(ValueError):
    """The delivery config file is unreadable or malformed."""


class ResumeMismatch(RuntimeError):
    """An incomplete receipt session exists but its messages differ from the
    ones about to be sent; resuming would risk wrong or duplicate sends."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_delivery_config(path: Path) -> tuple[float, str]:
    """(``inter_message_delay_seconds``, file SHA-256)."""
    try:
        raw = Path(path).read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeliveryConfigError(f"unreadable_delivery_config ({path})") from exc
    if not isinstance(document, dict) or document.get("schema") != DELIVERY_SCHEMA:
        raise DeliveryConfigError(f"unsupported_delivery_config_schema ({path})")
    delay = document.get("inter_message_delay_seconds")
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay <= 60:
        raise DeliveryConfigError(f"inter_message_delay_seconds must be a number in 0..60 ({path})")
    return float(delay), hashlib.sha256(raw).hexdigest()


# --- session state ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Session:
    head: Path
    parts: tuple[Path, ...]
    observations_sha256: str
    message_sha256: tuple[str, ...]
    sent: dict[int, int] = field(default_factory=dict)  # 1-based index -> telegram message_id
    legacy: bool = False  # head is not a v2 receipt: treated as a complete, opaque send

    @property
    def complete(self) -> bool:
        return self.legacy or all(index in self.sent for index in range(1, len(self.message_sha256) + 1))

    @property
    def next_part(self) -> int:
        return len(self.parts) + 2


def _head_order(name: str, family: ReceiptFamily) -> tuple:
    """Creation order of session heads: the primary receipt, then resend
    receipts by UTC stamp and collision suffix (``-2`` is later than none)."""
    if name == family.primary:
        return (0, "", 0)
    match = family.resend_pattern.match(name)
    if match:
        return (1, match.group(1), int(match.group(2) or 1))
    return (2, name, 0)


def _heads(receipt_dir: Path, family: ReceiptFamily) -> list[Path]:
    """Session heads, oldest first (see ``_head_order``)."""
    if not receipt_dir.is_dir():
        return []
    names = [
        path.name for path in receipt_dir.glob(f"{family.stem}*.json")
        if not _PART_PATTERN.search(path.name)
    ]
    return [receipt_dir / name for name in sorted(names, key=lambda name: _head_order(name, family))]


def _read_json(path: Path) -> dict | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def find_latest_session(
    receipt_dir: Path, family: ReceiptFamily = DETERMINISTIC_RECEIPTS,
) -> Session | None:
    """The most recent send session of ``family`` in ``receipt_dir`` (None if
    there is none)."""
    heads = _heads(receipt_dir, family)
    if not heads:
        return None
    head = heads[-1]
    head_doc = _read_json(head)
    if head_doc is None or head_doc.get("schema") != family.schema:
        return Session(head, (), "", (), {}, legacy=True)
    parts = tuple(sorted(
        path for path in head.parent.glob(f"{head.stem}.part-*.json") if _PART_PATTERN.search(path.name)
    ))
    sent: dict[int, int] = {}
    for path in (head, *parts):
        document = _read_json(path) or {}
        for entry in document.get("message_status", []):
            if isinstance(entry, dict) and entry.get("status") == "sent" \
                    and isinstance(entry.get("index"), int) \
                    and isinstance(entry.get("telegram_message_id"), int):
                sent[entry["index"]] = entry["telegram_message_id"]
    return Session(
        head, parts, str(head_doc.get("observations_sha256", "")),
        tuple(str(value) for value in head_doc.get("message_sha256", [])), sent,
    )


@dataclass(frozen=True, slots=True)
class Plan:
    target: Path
    to_send: tuple[int, ...]  # 1-based indices still to send, in order
    already_sent: dict[int, int]
    resend: bool
    continues: str | None  # head file name when resuming, else None
    attempt: int


def _new_head_path(receipt_dir: Path, family: ReceiptFamily) -> Path:
    primary = receipt_dir / family.primary
    if not primary.exists():
        return primary
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = receipt_dir / f"{family.stem}-{stamp}.json"
    suffix = 2
    while candidate.exists():
        candidate = receipt_dir / f"{family.stem}-{stamp}-{suffix}.json"
        suffix += 1
    return candidate


def plan_delivery(
    receipt_dir: Path, messages: list[str], observations_sha256: str, *, resend: bool,
    family: ReceiptFamily = DETERMINISTIC_RECEIPTS,
) -> Plan:
    """Decide what this run sends and where its receipt goes.

    Raises ``FileExistsError`` when the latest session is complete and
    ``resend`` is false (the existing "receipt zaten var" rejection) and
    ``ResumeMismatch`` when an incomplete session cannot be safely resumed."""
    hashes = tuple(sha256_text(message) for message in messages)
    all_indices = tuple(range(1, len(messages) + 1))
    session = find_latest_session(receipt_dir, family)
    if resend:
        return Plan(_new_head_path(receipt_dir, family), all_indices, {}, True, None, 1)
    if session is None:
        return Plan(receipt_dir / family.primary, all_indices, {}, False, None, 1)
    if session.complete:
        raise FileExistsError(session.head)
    if session.observations_sha256 != observations_sha256 or session.message_sha256 != hashes:
        raise ResumeMismatch(session.head)
    pending = tuple(index for index in all_indices if index not in session.sent)
    part = session.head.parent / f"{session.head.stem}.part-{session.next_part:03d}.json"
    return Plan(part, pending, dict(session.sent), False, session.head.name, session.next_part)


# --- sending ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome:
    sent_now: dict[int, int]  # index -> telegram message_id, this run
    failed_index: int | None
    error: Exception | None


def execute(
    plan: Plan, messages: list[str], *, send: Callable[[str], int],
    sleep: Callable[[float], None], delay_seconds: float,
) -> Outcome:
    """Send ``plan.to_send`` in order, pausing ``delay_seconds`` between two
    consecutive sends; stop at the first failure. ``send`` returns the
    Telegram ``message_id`` or raises."""
    sent_now: dict[int, int] = {}
    for position, index in enumerate(plan.to_send):
        if position and delay_seconds > 0:
            sleep(delay_seconds)
        try:
            sent_now[index] = send(messages[index - 1])
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 -- recorded by class name only
            # KeyboardInterrupt too: Ctrl-C mid-send must still leave a receipt
            # of what already went out, or the next run would send it again.
            return Outcome(sent_now, index, exc)
    return Outcome(sent_now, None, None)


def _progress_fields(plan: Plan, messages: list[str], outcome: Outcome) -> dict:
    """The per-message state every receipt of every family carries."""
    sent_all = {**plan.already_sent, **outcome.sent_now}
    status = [
        {"index": index, "status": "sent", "telegram_message_id": sent_all[index]}
        if index in sent_all else {"index": index, "status": "not_sent"}
        for index in range(1, len(messages) + 1)
    ]
    return {
        "attempt": plan.attempt,
        "continues": plan.continues,
        "resend": plan.resend,
        "message_count": len(messages),
        "message_sha256": [sha256_text(message) for message in messages],
        "message_status": status,
        "telegram_message_ids": [outcome.sent_now[index] for index in sorted(outcome.sent_now)],
        "complete": all(index in sent_all for index in range(1, len(messages) + 1)),
        "failed_at_index": outcome.failed_index,
        "error_kind": type(outcome.error).__name__ if outcome.error is not None else None,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_receipt(
    plan: Plan, messages: list[str], outcome: Outcome, *, as_of: str, observations_path: str,
    observations_sha256: str, rules_file_sha256: str | None, layout: str,
    explanations_path: str | None, explanations_sha256: str | None,
    delivery_path: str, delivery_sha256: str, delay_seconds: float,
) -> dict:
    return {
        "schema": RECEIPT_SCHEMA_V2,
        "as_of": as_of,
        "sent_at_utc": _utc_now(),
        "layout": layout,
        "observations_path": observations_path,
        "observations_sha256": observations_sha256,
        "rules_file_sha256": rules_file_sha256,
        "rule_explanations": (
            {"path": explanations_path, "sha256": explanations_sha256} if explanations_sha256 else None
        ),
        "delivery_config": {
            "path": delivery_path, "sha256": delivery_sha256,
            "inter_message_delay_seconds": delay_seconds,
        },
        **_progress_fields(plan, messages, outcome),
    }


def build_note_receipt(
    plan: Plan, messages: list[str], outcome: Outcome, *, as_of: str, observations_path: str,
    observations_sha256: str, note_dir: str, delay_seconds: float,
) -> dict:
    """Receipt of the analysis note's send session (``NOTE_RECEIPTS``)."""
    return {
        "schema": NOTE_RECEIPT_SCHEMA_V2,
        "as_of": as_of,
        "sent_at_utc": _utc_now(),
        "observations_path": observations_path,
        "observations_sha256": observations_sha256,
        "note_dir": note_dir,
        "inter_message_delay_seconds": delay_seconds,
        **_progress_fields(plan, messages, outcome),
    }
