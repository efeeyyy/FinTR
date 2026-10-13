"""Hash-chained, append-only manifest of the daily artifacts.

Chain: every entry carries ``seq`` (1, 2, ...), ``prev_hash`` (the previous
entry's ``entry_hash``; 64 zeros for the first) and ``entry_hash`` = SHA-256 of
the canonical JSON (sorted keys, no spaces, UTF-8) of the entry without
``entry_hash``. Editing, deleting or reordering a line breaks the chain from
that line on. Cutting lines off the END of the file cannot be seen from the
file alone: that needs an outside anchor (a git commit/tag of the manifest;
see ``docs/forward_log.md``).

Honesty limits: ``appended_at_utc`` is the local clock when the entry was
written, not an external timestamp, and a ``backfilled`` entry proves nothing
about what existed before it was appended.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re

SCHEMA = "fintr_forward_log_v1"
GENESIS_PREV_HASH = "0" * 64
# Daily artifact families, each laid out as <data_root>/<family>/<as_of>/...
ARTIFACT_FAMILIES = ("scanner_daily", "notify", "llm_notes")
_AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_REQUIRED_KEYS = ("schema", "seq", "as_of", "appended_at_utc", "files", "code_commit",
                  "revision", "backfilled", "prev_hash", "entry_hash")


class ForwardLogError(Exception):
    """Input or manifest problem; the message carries no data content."""


class ConflictError(ForwardLogError):
    """The as_of is already logged with different file hashes."""


class NoArtifactsError(ForwardLogError):
    """No daily artifact files exist for the as_of."""


def validate_as_of(as_of: str) -> str:
    if not isinstance(as_of, str) or not _AS_OF_RE.match(as_of):
        raise ForwardLogError(f"as_of must be YYYY-MM-DD, got {as_of!r}")
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise ForwardLogError(f"as_of is not a valid date: {as_of!r}") from exc
    return as_of


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_entry_hash(entry: dict) -> str:
    body = {key: value for key, value in entry.items() if key != "entry_hash"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_timestamp(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime(_STAMP_FORMAT)


def collect_artifacts(data_root: Path, as_of: str) -> list[dict]:
    """Every regular file under ``<family>/<as_of>/``, sorted by relative
    POSIX path: ``{path, sha256, size}``. Empty list if none exist."""
    validate_as_of(as_of)
    root = Path(data_root)
    found: list[dict] = []
    for family in ARTIFACT_FAMILIES:
        base = root / family / as_of
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                found.append({
                    "path": path.relative_to(root).as_posix(),
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                })
    return sorted(found, key=lambda item: item["path"])


def _fingerprint(files: list[dict]) -> list[tuple[str, str]]:
    return sorted((item["path"], item["sha256"]) for item in files)


def _shape_problems(entry: dict) -> list[str]:
    problems = [f"missing key {key!r}" for key in _REQUIRED_KEYS if key not in entry]
    if problems:
        return problems
    if entry["schema"] != SCHEMA:
        problems.append(f"unknown schema {entry['schema']!r}")
    try:
        validate_as_of(entry["as_of"])
    except ForwardLogError:
        problems.append("as_of is not a valid YYYY-MM-DD date")
    files = entry["files"]
    if not isinstance(files, list) or not files:
        problems.append("files must be a non-empty list")
    else:
        for item in files:
            if not (isinstance(item, dict) and isinstance(item.get("path"), str)
                    and isinstance(item.get("sha256"), str) and _SHA_RE.match(item["sha256"])
                    and isinstance(item.get("size"), int)):
                problems.append("a files item is not {path, sha256, size}")
                break
    return problems


def check_chain(entries: list[dict]) -> list[str]:
    """Problems found in already parsed entries (empty list = intact)."""
    problems: list[str] = []
    prev_hash = GENESIS_PREV_HASH
    prev_time: str | None = None
    seen_as_of: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"entry {index + 1}"
        if not isinstance(entry, dict):
            problems.append(f"{where}: not a JSON object")
            continue
        shape = _shape_problems(entry)
        if shape:
            problems += [f"{where}: {item}" for item in shape]
            if isinstance(entry.get("entry_hash"), str):
                prev_hash = entry["entry_hash"]
            continue
        if entry["seq"] != index + 1:
            problems.append(f"{where}: seq is {entry['seq']!r}, expected {index + 1}")
        if entry["prev_hash"] != prev_hash:
            problems.append(f"{where}: prev_hash does not match the previous entry")
        if entry["entry_hash"] != compute_entry_hash(entry):
            problems.append(f"{where}: entry_hash does not match the entry content")
        stamp = entry["appended_at_utc"]
        try:
            datetime.strptime(stamp, _STAMP_FORMAT)
        except (TypeError, ValueError):
            problems.append(f"{where}: appended_at_utc is not a UTC timestamp")
        else:
            if prev_time is not None and stamp < prev_time:
                problems.append(f"{where}: appended_at_utc is earlier than the previous entry")
            prev_time = stamp
        as_of = entry["as_of"]
        if as_of in seen_as_of:
            supersedes = entry.get("supersedes_seq")
            if entry["revision"] is not True:
                problems.append(f"{where}: as_of {as_of} was already logged and this is not a revision")
            elif not isinstance(supersedes, int) or not 1 <= supersedes <= index:
                problems.append(f"{where}: revision has no valid supersedes_seq")
        else:
            seen_as_of.add(as_of)
            if entry["revision"] is True:
                problems.append(f"{where}: revision of an as_of that was never logged")
        prev_hash = entry["entry_hash"]
    return problems


def load_manifest(path: Path) -> tuple[list[dict], list[str]]:
    """Parse the manifest; return ``(entries, problems)`` where problems
    covers unparseable lines and every chain break. A missing file is an
    empty log."""
    path = Path(path)
    if not path.exists():
        return [], []
    raw = path.read_bytes()
    problems: list[str] = []
    if raw and not raw.endswith(b"\n"):
        problems.append("manifest does not end with a newline (partial last line)")
    entries: list[dict] = []
    for number, line in enumerate(raw.decode("utf-8", errors="replace").split("\n"), start=1):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            problems.append(f"line {number}: not valid JSON")
    return entries, problems + check_chain(entries)


def verify_files(entries: list[dict], data_root: Path) -> list[str]:
    """Re-hash files on disk against the LATEST entry of each as_of (older
    revisions are superseded by design). Reports missing, changed and extra
    (not logged) files. ``entries`` must have passed ``check_chain``."""
    latest: dict[str, dict] = {}
    for entry in entries:
        latest[entry["as_of"]] = entry
    problems: list[str] = []
    for as_of in sorted(latest):
        logged = {item["path"]: item for item in latest[as_of]["files"]}
        on_disk = {item["path"]: item for item in collect_artifacts(data_root, as_of)}
        for rel in sorted(logged):
            if rel not in on_disk:
                problems.append(f"{as_of}: logged file is missing on disk: {rel}")
            elif on_disk[rel]["sha256"] != logged[rel]["sha256"]:
                problems.append(f"{as_of}: file changed since it was logged: {rel}")
        for rel in sorted(set(on_disk) - set(logged)):
            problems.append(f"{as_of}: file on disk is not in the log: {rel}")
    return problems


@dataclass(frozen=True)
class AppendResult:
    status: str  # "appended" | "noop"
    entry: dict


def _write_entry(manifest: Path, entry: dict) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    line = (canonical_json(entry) + "\n").encode("utf-8")
    with open(manifest, "ab") as handle:  # binary: no CRLF translation on Windows
        handle.write(line)


def append_as_of(manifest: Path, data_root: Path, as_of: str, *, revision: bool = False,
                 backfilled: bool = False, code_commit: str | None = None,
                 now: datetime | None = None) -> AppendResult:
    """Log the day's artifact hashes. Same as_of + identical hashes = no-op;
    different hashes = ``ConflictError`` unless ``revision`` (then a new
    entry marked as revision is appended; old lines are never touched)."""
    validate_as_of(as_of)
    manifest = Path(manifest)
    entries, problems = load_manifest(manifest)
    if problems:
        raise ForwardLogError(f"manifest is not intact ({len(problems)} problem(s)); "
                              "run `verify` -- nothing appended")
    files = collect_artifacts(data_root, as_of)
    if not files:
        raise NoArtifactsError(f"no daily artifact files found for as_of {as_of}")
    prior = [entry for entry in entries if entry["as_of"] == as_of]
    supersedes = None
    if prior:
        latest = prior[-1]
        if _fingerprint(latest["files"]) == _fingerprint(files):
            return AppendResult("noop", latest)
        if not revision:
            raise ConflictError(f"as_of {as_of} is already logged (seq {latest['seq']}) with "
                                "different file hashes; refusing (use --revision to append a "
                                "revision entry)")
        supersedes = latest["seq"]
    elif revision:
        raise ForwardLogError(f"--revision given but as_of {as_of} was never logged")
    entry = {
        "schema": SCHEMA,
        "seq": len(entries) + 1,
        "as_of": as_of,
        "appended_at_utc": utc_timestamp(now),
        "files": files,
        "code_commit": code_commit,
        "revision": bool(revision),
        "backfilled": bool(backfilled),
        "prev_hash": entries[-1]["entry_hash"] if entries else GENESIS_PREV_HASH,
    }
    if supersedes is not None:
        entry["supersedes_seq"] = supersedes
    entry["entry_hash"] = compute_entry_hash(entry)
    _write_entry(manifest, entry)
    return AppendResult("appended", entry)


def list_as_of_dates(data_root: Path, start: str, end: str) -> list[str]:
    """Dates that have a scanner observations file, ``start <= date <= end``."""
    validate_as_of(start)
    validate_as_of(end)
    base = Path(data_root) / "scanner_daily"
    if not base.is_dir():
        return []
    found = []
    for path in base.iterdir():
        try:
            validate_as_of(path.name)
        except ForwardLogError:
            continue
        if start <= path.name <= end and (path / "observations.json").is_file():
            found.append(path.name)
    return sorted(found)
