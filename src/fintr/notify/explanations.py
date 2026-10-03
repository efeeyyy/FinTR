"""Fixed rule explanation texts for notification layout v2.

``configs/notify/rule_explanations_v1.json`` (schema
``fintr_notify_rule_explanations_v1``) holds, per rule id, the fixed
"Kural" line and the fixed "Ne demek" sentence(s). Nothing is generated at
run time: the file is read as bytes, its SHA-256 is returned so the receipt
can pin the exact wording that was sent, and every text passes the
forbidden-wording guard (``fintr.notify.guard``) at load time. A rule
without a ``meaning`` simply has no "Ne demek" line -- nothing is invented.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from fintr.notify.guard import find_forbidden_words


RULE_EXPLANATIONS_SCHEMA = "fintr_notify_rule_explanations_v1"
MAX_TEXT_CHARS = 600


class RuleExplanationsError(ValueError):
    """The explanation file is unreadable, malformed or contains forbidden wording."""


@dataclass(frozen=True, slots=True)
class RuleExplanations:
    rule_lines: dict[str, str]
    meanings: dict[str, str]
    sha256: str


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuleExplanationsError(f"{where}: boş olmayan metin bekleniyor")
    if len(value) > MAX_TEXT_CHARS:
        raise RuleExplanationsError(f"{where}: metin {MAX_TEXT_CHARS} karakteri aşıyor")
    found = find_forbidden_words(value)
    if found:
        raise RuleExplanationsError(f"{where}: yasak ifade ({', '.join(found)})")
    return value.strip()


def load_rule_explanations(path: Path) -> RuleExplanations:
    try:
        raw = Path(path).read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuleExplanationsError(f"unreadable_rule_explanations_file ({path})") from exc
    if not isinstance(document, dict) or document.get("schema") != RULE_EXPLANATIONS_SCHEMA:
        raise RuleExplanationsError(f"unsupported_rule_explanations_schema ({path})")
    rules = document.get("rules")
    if not isinstance(rules, dict) or not rules:
        raise RuleExplanationsError(f"rule_explanations file has no rules ({path})")
    rule_lines: dict[str, str] = {}
    meanings: dict[str, str] = {}
    for rule_id, entry in rules.items():
        if not isinstance(entry, dict):
            raise RuleExplanationsError(f"{rule_id}: nesne bekleniyor")
        if "rule_line" in entry:
            rule_lines[str(rule_id)] = _text(entry["rule_line"], f"{rule_id}.rule_line")
        if "meaning" in entry:
            meanings[str(rule_id)] = _text(entry["meaning"], f"{rule_id}.meaning")
    return RuleExplanations(rule_lines, meanings, hashlib.sha256(raw).hexdigest())
