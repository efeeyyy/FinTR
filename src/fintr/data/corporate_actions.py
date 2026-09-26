"""Strict loader for a symbol's user-maintained corporate-action registry.

Schema ``fintr_corporate_actions_v1``. A corporate-action fact (bonus issue,
rights issue, split, reverse split, ...) is never fetched by this project's
code: kap.org.tr and every other network source are out of reach for this
module by design. The user reads the primary source themselves and enters
the fact through ``scripts/register_corporate_action.py``; this module only
validates structure and integrity (multiplier sign, date ordering, no
overlapping/duplicate actions) and never invents a missing field. See
``docs/corporate_actions.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any

from fintr.data.parsers.common import (
    MarketDataParseError,
    require_date,
    require_field,
    require_list,
    require_mapping,
    require_string,
)


REGISTRY_SCHEMA = "fintr_corporate_actions_v1"

# The only lot-multiplier events this registry models. "other" covers a real
# event that does not fit the named categories; it still requires the same
# fields (a multiplier and both effective dates), so it is never a silent
# escape hatch for skipping provenance.
ACTION_TYPES = ("bonus_issue", "rights_issue", "split", "reverse_split", "other")

# The only accepted value: every entry is a user assertion from a primary
# source, never a value this project derived or verified independently.
VERIFICATION_USER_ASSERTED = "user_asserted_from_primary_source"

ENTERED_BY_USER = "user"


class CorporateActionsError(ValueError):
    """A corporate-action registry file failed structural or integrity validation."""


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """One user-entered corporate-action fact; every field is required except
    ``source_url``, ``source_published_date``, ``source_file_sha256`` and
    ``notes``, which may be ``None``/empty when the user has no such
    reference for a given action."""

    action_id: str
    type: str
    lot_multiplier: Decimal  # new lots per old lot, e.g. Decimal("6") for a 1-for-5 bonus issue
    ex_date: date  # first trading date in post-action units (affects AKD and price)
    custody_effective_date: date  # first custody date in post-action units (required, no default)
    source_url: str | None
    source_published_date: date | None
    source_file_sha256: str | None  # sha256 of a local file the user downloaded, or None
    entered_by: str
    entered_at_utc: str
    verification: str
    notes: str


@dataclass(frozen=True, slots=True)
class CorporateActionRegistry:
    """A validated registry; ``actions`` is ascending by ``ex_date`` and
    contains no duplicate/overlapping action (see ``parse_corporate_action_registry``)."""

    symbol: str
    actions: tuple[CorporateAction, ...]


def _optional_string(source: Any, key: str, path: str) -> str | None:
    value = source.get(key)
    if value is None:
        return None
    return require_string(value, f"{path}.{key}")


def _optional_date(source: Any, key: str, path: str) -> date | None:
    value = source.get(key)
    if value is None:
        return None
    return require_date(value, f"{path}.{key}")


def _notes_field(source: Any, path: str) -> str:
    """Unlike the other optional string fields, an empty note ("" or
    missing/null) is a normal, valid value -- not a required field that
    happens to be empty -- so this does not go through ``require_string``."""
    value = source.get("notes")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CorporateActionsError(f"{path}.notes: must be a string")
    return value


def _require_decimal_string(value: Any, path: str) -> Decimal:
    """``lot_multiplier`` is stored as a quoted Decimal STRING (e.g. "6"),
    never a bare JSON number, so an exact multiplier never round-trips
    through a float."""
    text = require_string(value, path)
    try:
        result = Decimal(text)
    except InvalidOperation:
        raise CorporateActionsError(f"{path}: invalid decimal string {text!r}") from None
    if not result.is_finite():
        raise CorporateActionsError(f"{path}: number must be finite")
    return result


def parse_corporate_action(raw: Any, path: str) -> CorporateAction:
    entry = require_mapping(raw, path)
    action_id = require_string(require_field(entry, "action_id", path), f"{path}.action_id")
    action_type = require_string(require_field(entry, "type", path), f"{path}.type")
    if action_type not in ACTION_TYPES:
        raise CorporateActionsError(
            f"{path}.type: must be one of {ACTION_TYPES}, got {action_type!r}"
        )
    lot_multiplier = _require_decimal_string(
        require_field(entry, "lot_multiplier", path), f"{path}.lot_multiplier",
    )
    if lot_multiplier <= 0:
        raise CorporateActionsError(
            f"{path}.lot_multiplier: must be a positive Decimal, got {lot_multiplier}"
        )
    ex_date = require_date(require_field(entry, "ex_date", path), f"{path}.ex_date")
    custody_effective_date = require_date(
        require_field(entry, "custody_effective_date", path), f"{path}.custody_effective_date",
    )
    if custody_effective_date < ex_date:
        raise CorporateActionsError(
            f"{path}: custody_effective_date ({custody_effective_date.isoformat()}) is "
            f"before ex_date ({ex_date.isoformat()})"
        )
    entered_by = require_string(require_field(entry, "entered_by", path), f"{path}.entered_by")
    if entered_by != ENTERED_BY_USER:
        raise CorporateActionsError(f"{path}.entered_by: must be {ENTERED_BY_USER!r}, got {entered_by!r}")
    verification = require_string(require_field(entry, "verification", path), f"{path}.verification")
    if verification != VERIFICATION_USER_ASSERTED:
        raise CorporateActionsError(
            f"{path}.verification: must be {VERIFICATION_USER_ASSERTED!r}, got {verification!r}"
        )
    entered_at_utc = require_string(
        require_field(entry, "entered_at_utc", path), f"{path}.entered_at_utc",
    )
    return CorporateAction(
        action_id=action_id,
        type=action_type,
        lot_multiplier=lot_multiplier,
        ex_date=ex_date,
        custody_effective_date=custody_effective_date,
        source_url=_optional_string(entry, "source_url", path),
        source_published_date=_optional_date(entry, "source_published_date", path),
        source_file_sha256=_optional_string(entry, "source_file_sha256", path),
        entered_by=entered_by,
        entered_at_utc=entered_at_utc,
        verification=verification,
        notes=_notes_field(entry, path),
    )


def parse_corporate_action_registry(document: Any) -> CorporateActionRegistry:
    """Validate a decoded registry document.

    Rejects: an unsupported schema, a non-positive ``lot_multiplier``, a
    ``custody_effective_date`` before its ``ex_date``, a duplicate
    ``action_id`` or ``ex_date``, and two actions whose windows overlap
    (one action's ``custody_effective_date`` falls after the next action's
    ``ex_date``, which would make the multiplier applicable to a given date
    ambiguous).
    """
    try:
        root = require_mapping(document, "registry")
        schema = root.get("schema")
        if schema != REGISTRY_SCHEMA:
            raise CorporateActionsError(
                f"registry.schema: expected {REGISTRY_SCHEMA!r}, got {schema!r}"
            )
        symbol = require_string(require_field(root, "symbol", "registry"), "registry.symbol")
        raw_actions = require_list(require_field(root, "actions", "registry"), "registry.actions")
        actions = [
            parse_corporate_action(raw, f"registry.actions[{index}]")
            for index, raw in enumerate(raw_actions)
        ]
    except MarketDataParseError as exc:
        raise CorporateActionsError(f"invalid_registry_field: {exc.field_path or 'unknown'}") from None

    seen_ids: set[str] = set()
    for action in actions:
        if action.action_id in seen_ids:
            raise CorporateActionsError(f"duplicate_action_id: {action.action_id}")
        seen_ids.add(action.action_id)

    ordered = tuple(sorted(actions, key=lambda action: action.ex_date))
    seen_ex_dates: set[date] = set()
    for action in ordered:
        if action.ex_date in seen_ex_dates:
            raise CorporateActionsError(f"duplicate_ex_date: {action.ex_date.isoformat()}")
        seen_ex_dates.add(action.ex_date)
    for previous, current in zip(ordered, ordered[1:]):
        if previous.custody_effective_date > current.ex_date:
            raise CorporateActionsError(
                "overlapping_actions: "
                f"{previous.action_id} (custody_effective_date "
                f"{previous.custody_effective_date.isoformat()}) overlaps "
                f"{current.action_id} (ex_date {current.ex_date.isoformat()})"
            )

    return CorporateActionRegistry(symbol=symbol, actions=ordered)


def load_corporate_action_registry(path: Path) -> CorporateActionRegistry:
    """Read and validate one saved ``configs/corporate_actions/<SYMBOL>.json`` file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        document = json.loads(text, parse_float=Decimal)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorporateActionsError(f"unreadable_registry: {exc}") from None
    return parse_corporate_action_registry(document)


def action_to_json(action: CorporateAction) -> dict[str, Any]:
    return {
        "action_id": action.action_id,
        "type": action.type,
        "lot_multiplier": format(action.lot_multiplier, "f"),
        "ex_date": action.ex_date.isoformat(),
        "custody_effective_date": action.custody_effective_date.isoformat(),
        "source_url": action.source_url,
        "source_published_date": (
            action.source_published_date.isoformat()
            if action.source_published_date is not None else None
        ),
        "source_file_sha256": action.source_file_sha256,
        "entered_by": action.entered_by,
        "entered_at_utc": action.entered_at_utc,
        "verification": action.verification,
        "notes": action.notes,
    }


def registry_to_json(registry: CorporateActionRegistry) -> dict[str, Any]:
    return {
        "schema": REGISTRY_SCHEMA,
        "symbol": registry.symbol,
        "actions": [action_to_json(action) for action in registry.actions],
    }
