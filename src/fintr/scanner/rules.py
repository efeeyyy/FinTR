"""Loader for the scanner's frozen rule set (``fintr_scanner_rules_v1``).

Reuses ``fintr.research.study_spec.load_study_spec`` to parse each rule's
frozen H1-H4 spec file; a rule adds no threshold or parameter of its own --
its condition is exactly ``spec.condition`` (see ``docs/scanner.md``). This
loader is a pre-registration gate, symmetric to the one
``fintr.research.report.run_study`` applies to a ``test`` split: it refuses
to load a rule whose spec is not ``frozen: true``, or whose recorded
raw-file / canonical spec hashes (``docs/prereg/FREEZE.md``) do not match
the spec file actually on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from fintr.research.study_spec import StudySpec, StudySpecError, load_study_spec


RULES_SCHEMA = "fintr_scanner_rules_v1"


class ScannerRulesError(ValueError):
    """The scanner rule file, or one rule's frozen spec, failed validation."""


@dataclass(frozen=True, slots=True)
class ScannerRule:
    rule_id: str
    spec_path: str  # repo-relative, as recorded in the rules file
    spec_sha256: str  # raw file bytes, matches docs/prereg/FREEZE.md
    spec: StudySpec

    @property
    def family_role(self) -> str:
        # Enforced non-None by load_scanner_rules: every rule's spec must
        # carry a preregistration block.
        return self.spec.preregistration.family_role


def _require_str(mapping: dict, key: str, path: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ScannerRulesError(f"{path}.{key}: expected a non-empty string")
    return value


def load_scanner_rules(path: Path, *, project_root: Path) -> tuple[ScannerRule, ...]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ScannerRulesError("unreadable_rules_file") from None
    if not isinstance(document, dict) or document.get("schema") != RULES_SCHEMA:
        raise ScannerRulesError("unsupported_rules_schema")
    raw_rules = document.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ScannerRulesError("rules: expected a non-empty list")

    rules: list[ScannerRule] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_rules):
        entry_path = f"rules[{index}]"
        if not isinstance(raw, dict):
            raise ScannerRulesError(f"{entry_path}: expected an object")
        rule_id = _require_str(raw, "rule_id", entry_path)
        if rule_id in seen_ids:
            raise ScannerRulesError(f"duplicate_rule_id: {rule_id}")
        seen_ids.add(rule_id)
        spec_path_str = _require_str(raw, "spec_path", entry_path)
        raw_sha256 = _require_str(raw, "raw_sha256", entry_path)
        canonical_sha256 = _require_str(raw, "canonical_sha256", entry_path)

        spec_path = Path(project_root) / spec_path_str
        try:
            spec_bytes = spec_path.read_bytes()
        except OSError:
            raise ScannerRulesError(
                f"{rule_id}: unreadable_spec_file ({spec_path_str})"
            ) from None
        actual_raw_sha256 = hashlib.sha256(spec_bytes).hexdigest()
        if actual_raw_sha256 != raw_sha256:
            raise ScannerRulesError(
                f"{rule_id}: raw_sha256 mismatch (spec file on disk does not match "
                f"docs/prereg/FREEZE.md; expected {raw_sha256}, got {actual_raw_sha256})"
            )
        try:
            spec = load_study_spec(spec_path)
        except StudySpecError as exc:
            raise ScannerRulesError(f"{rule_id}: invalid_spec ({exc})") from None
        if not spec.frozen:
            raise ScannerRulesError(f"{rule_id}: spec is not frozen (frozen: false)")
        if spec.spec_sha256 != canonical_sha256:
            raise ScannerRulesError(
                f"{rule_id}: canonical_sha256 mismatch (expected {canonical_sha256}, "
                f"got {spec.spec_sha256})"
            )
        if spec.preregistration is None:
            raise ScannerRulesError(
                f"{rule_id}: spec has no preregistration block (family_role required)"
            )
        rules.append(ScannerRule(
            rule_id=rule_id, spec_path=spec_path_str, spec_sha256=actual_raw_sha256, spec=spec,
        ))
    return tuple(rules)
