"""No-overwrite attempt records under ``data/interim/llm_notes/<as_of>/``.

Each run creates a fresh ``attempt-NNN`` directory (``os.mkdir`` fails if it
exists) and writes every file with ``O_EXCL``. ``record.json`` is written
last: a directory without it is an incomplete attempt and is ignored. An
attempt is "accepted" when its record says so; an accepted attempt makes
later runs for the same ``as_of`` a no-op, failed attempts do not.

LLM output is not deterministic: re-running the same packet can give a
different note. The attempt directory is the reproducibility record -- the
exact packet, the prompt/schema/config hashes, model id, CLI version, argv,
raw response and validation result.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re


RECORD_SCHEMA = "fintr_llm_note_record_v1"
DETERMINISM_NOTE = (
    "LLM çıktısı deterministik değildir: aynı paket ile yeniden çalıştırma farklı bir not "
    "üretebilir; tekrar üretilebilirlik kaydı bu dizindeki paket, prompt/şema/config "
    "sha256'ları, model, CLI sürümü ve ham yanıttır."
)
_ATTEMPT = re.compile(r"^attempt-(\d{3,})$")


def write_new(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def new_attempt_dir(root: Path, as_of: str) -> tuple[Path, int]:
    parent = Path(root) / as_of
    parent.mkdir(parents=True, exist_ok=True)
    existing = [int(m.group(1)) for p in parent.iterdir() if (m := _ATTEMPT.match(p.name))]
    number = max(existing, default=0) + 1
    while True:
        target = parent / f"attempt-{number:03d}"
        try:
            target.mkdir()
        except FileExistsError:
            number += 1
            continue
        return target, number


def _attempts(root: Path, as_of: str) -> list[tuple[int, Path, dict]]:
    parent = Path(root) / as_of
    found: list[tuple[int, Path, dict]] = []
    if not parent.is_dir():
        return found
    for entry in parent.iterdir():
        match = _ATTEMPT.match(entry.name)
        record_path = entry / "record.json"
        if not match or not record_path.is_file():
            continue
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("schema") == RECORD_SCHEMA:
            found.append((int(match.group(1)), entry, record))
    return sorted(found, key=lambda item: item[0])


def latest_accepted(root: Path, as_of: str) -> tuple[Path, dict] | None:
    accepted = [(path, rec) for _, path, rec in _attempts(root, as_of) if rec.get("status") == "accepted"]
    return accepted[-1] if accepted else None


def latest_attempt(root: Path, as_of: str) -> tuple[Path, dict] | None:
    attempts = _attempts(root, as_of)
    return (attempts[-1][1], attempts[-1][2]) if attempts else None
