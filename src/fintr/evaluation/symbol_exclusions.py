"""Symbol exclusion registry (``fintr_symbol_exclusions_v1``).

Records a user decision to keep specific symbols out of collection,
analysis, studies, evaluation and signals entirely -- independent of, and
enforced in addition to, the BIST30 holdout/development split
(``fintr.evaluation.symbol_split``). An exclusion's ``reason`` is always the
user's own assertion (see ``configs/exclusions/symbols.json``'s DSTKF
entry); this module never independently verifies or restates it as a
project finding.

Unlike the holdout guard (which has ``--no-split-guard`` for non-BIST30
exploratory work), an exclusion has **no override flag anywhere** in this
project. Every enforcement point either refuses or skips an excluded
symbol unconditionally.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
import hashlib
import json

from fintr.data.parsers.common import (
    MarketDataParseError,
    require_date,
    require_field,
    require_mapping,
    require_string,
)


EXCLUSIONS_SCHEMA = "fintr_symbol_exclusions_v1"
DEFAULT_EXCLUSIONS_PATH = Path("configs/exclusions/symbols.json")


class SymbolExclusionsError(ValueError):
    """A fixed reason a symbol-exclusions file is invalid or unreadable."""


@dataclass(frozen=True, slots=True)
class ExclusionEntry:
    symbol: str
    decided_by: str
    decided_on: date
    scope: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol or self.symbol != self.symbol.upper():
            raise SymbolExclusionsError("invalid_exclusion_symbol")
        if not isinstance(self.decided_by, str) or not self.decided_by:
            raise SymbolExclusionsError("invalid_exclusion_decided_by")
        if not isinstance(self.decided_on, date):
            raise SymbolExclusionsError("invalid_exclusion_decided_on")
        if not isinstance(self.scope, str) or not self.scope:
            raise SymbolExclusionsError("invalid_exclusion_scope")
        if not isinstance(self.reason, str) or not self.reason:
            raise SymbolExclusionsError("invalid_exclusion_reason")


@dataclass(frozen=True, slots=True)
class SymbolExclusions:
    entries: tuple[ExclusionEntry, ...]
    file_sha256: str | None
    raw_document: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, ExclusionEntry) for entry in self.entries
        ):
            raise SymbolExclusionsError("invalid_exclusion_entries")
        symbols = tuple(entry.symbol for entry in self.entries)
        if len(set(symbols)) != len(symbols):
            raise SymbolExclusionsError("duplicate_exclusion_symbol")

    def entry_for(self, symbol: str) -> ExclusionEntry | None:
        normalized = symbol.strip().upper()
        for entry in self.entries:
            if entry.symbol == normalized:
                return entry
        return None

    def is_excluded(self, symbol: str) -> bool:
        return self.entry_for(symbol) is not None


EMPTY_EXCLUSIONS = SymbolExclusions(
    entries=(), file_sha256=None, raw_document={"schema": EXCLUSIONS_SCHEMA, "symbols": {}},
)


def parse_symbol_exclusions(document: Any, *, file_sha256: str | None = None) -> SymbolExclusions:
    try:
        root = require_mapping(document, "exclusions")
        if root.get("schema") != EXCLUSIONS_SCHEMA:
            raise SymbolExclusionsError("unsupported_exclusions_schema")
        symbols_map = require_mapping(
            require_field(root, "symbols", "exclusions"), "exclusions.symbols",
        )
        entries: list[ExclusionEntry] = []
        for symbol, raw in symbols_map.items():
            if not isinstance(symbol, str) or not symbol:
                raise SymbolExclusionsError("invalid_exclusion_symbol_key")
            item_path = f"exclusions.symbols.{symbol}"
            item = require_mapping(raw, item_path)
            entries.append(
                ExclusionEntry(
                    symbol=symbol,
                    decided_by=require_string(
                        require_field(item, "decided_by", item_path), f"{item_path}.decided_by",
                    ),
                    decided_on=require_date(
                        require_field(item, "decided_on", item_path), f"{item_path}.decided_on",
                    ),
                    scope=require_string(
                        require_field(item, "scope", item_path), f"{item_path}.scope",
                    ),
                    reason=require_string(
                        require_field(item, "reason", item_path), f"{item_path}.reason",
                    ),
                )
            )
    except MarketDataParseError as exc:
        raise SymbolExclusionsError(f"invalid_exclusions_field: {exc.field_path or 'unknown'}") from None
    return SymbolExclusions(entries=tuple(entries), file_sha256=file_sha256, raw_document=document)


def load_symbol_exclusions(path: Path) -> SymbolExclusions:
    try:
        raw_bytes = Path(path).read_bytes()
        document = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SymbolExclusionsError("unreadable_exclusions") from None
    return parse_symbol_exclusions(document, file_sha256=hashlib.sha256(raw_bytes).hexdigest())


def load_default_symbol_exclusions(path: Path = DEFAULT_EXCLUSIONS_PATH) -> SymbolExclusions:
    """Empty (no exclusions) if ``path`` does not exist; parsed and
    validated otherwise. There is deliberately no flag anywhere that skips
    this file when it does exist."""
    if not Path(path).is_file():
        return EMPTY_EXCLUSIONS
    return load_symbol_exclusions(path)


def is_excluded(exclusions: SymbolExclusions, symbol: str) -> bool:
    return exclusions.is_excluded(symbol)


def exclusion_reason(exclusions: SymbolExclusions, symbol: str) -> str | None:
    entry = exclusions.entry_for(symbol)
    return None if entry is None else entry.reason
