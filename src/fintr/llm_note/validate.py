"""Fail-closed validation of a model-written note against its input packet.

``validate_note`` never repairs anything: the first failing category
rejects the whole note. Categories (stable slugs, shown to Efe as
"analiz notu doğrulamadan geçemedi (<kategori>)"):

``schema``              structure/type/length differs from the output schema
``assessment_value``    ``assessment`` not in ``ASSESSMENT_VALUES``
``coverage``            the (symbol, rule_id, as_of) set differs from the
                        packet's ``required_assessments`` (missing/extra/dup)
``evidence_path``       an ``evidence_paths`` entry does not resolve in the packet,
                        or covers more than ``MAX_EVIDENCE_LEAVES`` values
``forbidden_phrase``    wording forbidden by AGENTS.md (``forbidden.py``)
``number_word``         a spelled-out number or "yüzde <word>" (cannot be audited)
``number_not_in_input`` a number or ISO date in a reason is not under that
                        reason's own ``evidence_paths`` (sign-aware, see
                        ``numbers.py``), or one in ``data_caveats`` is absent
                        from the packet
``sensitive_content``   ``validate_context_contains_no_secrets`` rejected the note
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fintr.evaluation.live_capture import LivePilotCaptureError, validate_context_contains_no_secrets
from fintr.llm_note import numbers
from fintr.llm_note.forbidden import find_forbidden


ASSESSMENT_VALUES = ("destekliyor", "zayıflatıyor", "belirsiz")
MAX_ASSESSMENTS = 200
MAX_REASONS = 6
MAX_PATHS_PER_REASON = 6
MAX_CAVEATS = 8
MAX_TEXT_CHARS = 400
MAX_PATH_CHARS = 300
MAX_EVIDENCE_LEAVES = 600  # an evidence path may not point at "the whole packet"

_TOP_KEYS = {"assessments", "data_caveats"}
_ASSESSMENT_KEYS = {"symbol", "rule_id", "as_of", "assessment", "reasons"}
_REASON_KEYS = {"text", "evidence_paths"}


@dataclass(frozen=True, slots=True)
class Validation:
    ok: bool
    category: str | None = None
    details: tuple[str, ...] = field(default_factory=tuple)


def _fail(category: str, *details: str) -> Validation:
    return Validation(False, category, tuple(details[:10]))


def resolve_node(document: object, pointer: str) -> tuple[bool, object]:
    """(found, node) for the JSON Pointer ``pointer`` inside ``document``."""
    if not isinstance(pointer, str) or not pointer.startswith("/") or pointer == "/":
        return False, None
    node = document
    for raw in pointer[1:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if key not in node:
                return False, None
            node = node[key]
        elif isinstance(node, list):
            if not key.isdigit() or (len(key) > 1 and key[0] == "0") or int(key) >= len(node):
                return False, None
            node = node[int(key)]
        else:
            return False, None
    return True, node


def resolve_pointer(document: object, pointer: str) -> bool:
    """True if the JSON Pointer ``pointer`` resolves inside ``document``."""
    return resolve_node(document, pointer)[0]


def _leaf_count(node: object, cap: int) -> int:
    """Number of scalar leaves under ``node``, counting stops above ``cap``."""
    stack, count = [node], 0
    while stack and count <= cap:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        else:
            count += 1
    return count


def _structure(note: object) -> Validation | None:
    if not isinstance(note, dict) or set(note) != _TOP_KEYS:
        return _fail("schema", "top-level object must have exactly assessments, data_caveats")
    assessments, caveats = note["assessments"], note["data_caveats"]
    if not isinstance(assessments, list) or len(assessments) > MAX_ASSESSMENTS:
        return _fail("schema", "assessments must be a list")
    if not isinstance(caveats, list) or len(caveats) > MAX_CAVEATS or any(
        not isinstance(c, str) or not c.strip() or len(c) > MAX_TEXT_CHARS for c in caveats
    ):
        return _fail("schema", "data_caveats must be a short list of non-empty strings")
    for index, item in enumerate(assessments):
        where = f"assessments[{index}]"
        if not isinstance(item, dict) or set(item) != _ASSESSMENT_KEYS:
            return _fail("schema", f"{where}: wrong keys")
        for key in ("symbol", "rule_id", "as_of"):
            if not isinstance(item[key], str) or not item[key]:
                return _fail("schema", f"{where}.{key}: expected a non-empty string")
        if not isinstance(item["assessment"], str):
            return _fail("schema", f"{where}.assessment: expected a string")
        reasons = item["reasons"]
        if not isinstance(reasons, list) or not 1 <= len(reasons) <= MAX_REASONS:
            return _fail("schema", f"{where}.reasons: expected 1..{MAX_REASONS} items")
        for r_index, reason in enumerate(reasons):
            r_where = f"{where}.reasons[{r_index}]"
            if not isinstance(reason, dict) or set(reason) != _REASON_KEYS:
                return _fail("schema", f"{r_where}: wrong keys")
            text, paths = reason["text"], reason["evidence_paths"]
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
                return _fail("schema", f"{r_where}.text: expected a short non-empty string")
            if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_PATHS_PER_REASON or any(
                not isinstance(p, str) or len(p) > MAX_PATH_CHARS for p in paths
            ):
                return _fail("schema", f"{r_where}.evidence_paths: expected 1..{MAX_PATHS_PER_REASON} strings")
    return None


def _free_texts(note: dict) -> list[str]:
    texts = [reason["text"] for item in note["assessments"] for reason in item["reasons"]]
    return texts + list(note["data_caveats"])


def _packet_names(packet: dict) -> tuple[list[str], list[str]]:
    """(identifiers to mask before number extraction, phrases exempt from the
    forbidden-wording check). Both are drawn from the packet itself."""
    symbols = sorted(packet.get("symbols", {}))
    rule_ids = {*packet.get("rules", {})}
    for block in packet.get("symbols", {}).values():
        for scan in block.get("scans", []):
            rule_ids.update(obs["rule_id"] for obs in scan.get("observations", []) if obs.get("rule_id"))
    titles: list[str] = []
    short_titles: list[str] = []
    for block in packet.get("symbols", {}).values():
        for label in block.get("institutions", {}).values():
            if isinstance(label, dict):
                if label.get("title"):
                    titles.append(str(label["title"]))
                if label.get("short_title"):
                    short_titles.append(str(label["short_title"]))
    # Only full titles are exempt from the wording check: a short title can be
    # a single common word and must not whitelist that word everywhere.
    return symbols + sorted(rule_ids) + titles + short_titles, titles


def validate_note(note: object, packet: dict) -> Validation:
    """Validate ``note`` (the decoded model JSON) against ``packet``."""
    structural = _structure(note)
    if structural is not None:
        return structural

    for index, item in enumerate(note["assessments"]):
        if item["assessment"] not in ASSESSMENT_VALUES:
            return _fail("assessment_value", f"assessments[{index}].assessment")

    required = [
        (entry["symbol"], entry["rule_id"], entry["as_of"])
        for entry in packet.get("required_assessments", [])
    ]
    given = [(i["symbol"], i["rule_id"], i["as_of"]) for i in note["assessments"]]
    if sorted(given) != sorted(required) or len(set(given)) != len(given):
        missing = sorted(set(required) - set(given))
        extra = sorted(set(given) - set(required))
        return _fail("coverage", f"missing={missing}", f"extra={extra}",
                     f"duplicates={len(given) - len(set(given))}")

    for item in note["assessments"]:
        for reason in item["reasons"]:
            for path in reason["evidence_paths"]:
                found, node = resolve_node(packet, path)
                if not found:
                    return _fail("evidence_path", path)
                if _leaf_count(node, MAX_EVIDENCE_LEAVES) > MAX_EVIDENCE_LEAVES:
                    return _fail("evidence_path", f"too_broad:{path}")

    names, titles = _packet_names(packet)
    texts = _free_texts(note)
    for text in texts:
        found = find_forbidden(text, allowed_phrases=tuple(titles))
        if found:
            return _fail("forbidden_phrase", *found)

    for text in texts:
        words = numbers.find_number_words(numbers.mask_names(text, names))
        if words:
            return _fail("number_word", *words)

    # Reasons: every number/date must be under that reason's own evidence
    # paths. Data caveats cite no path: whole-packet check.
    path_sets: dict[str, tuple[set, set]] = {}

    def under_path(path: str) -> tuple[set, set]:
        if path not in path_sets:
            _, node = resolve_node(packet, path)
            values, dates = numbers.packet_number_set(node, mask=names)
            path_sets[path] = (values, dates | set(numbers.ISO_DATE.findall(path)))
        return path_sets[path]

    def audit(text: str, allowed_numbers: set, allowed_dates: set, where: str) -> Validation | None:
        dates, rest = numbers.extract_dates(text)
        for date_text in dates:
            if date_text not in allowed_dates:
                return _fail("number_not_in_input", f"date {date_text} ({where})")
        for token in numbers.extract_numbers(numbers.mask_names(rest, names)):
            if not token.candidates or not numbers.number_in_packet(token, allowed_numbers):
                return _fail("number_not_in_input", f"{token.raw} ({where})")
        return None

    for item in note["assessments"]:
        for reason in item["reasons"]:
            values: set = set()
            dates: set = set()
            for path in reason["evidence_paths"]:
                path_values, path_dates = under_path(path)
                values |= path_values
                dates |= path_dates
            failure = audit(reason["text"], values, dates, "not under the reason's evidence_paths")
            if failure is not None:
                return failure
    all_numbers, all_dates = numbers.packet_number_set(packet, mask=names)
    for caveat in note["data_caveats"]:
        failure = audit(caveat, all_numbers, all_dates, "data_caveats: not in the packet")
        if failure is not None:
            return failure

    try:
        validate_context_contains_no_secrets(note)
    except LivePilotCaptureError:
        return _fail("sensitive_content")
    return Validation(True)
