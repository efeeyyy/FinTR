"""Frozen BIST30 symbol split (``fintr_symbol_split_v1``): holdout vs. development.

A symbol split freezes which BIST30 symbols are held out for a one-shot
frozen evaluation (``holdout_symbols``) versus available for iterative
analysis, rule development, prompt/model selection and threshold tuning
(``development_symbols``). Held-out symbols may still be *collected* (see
``scripts/fetch_price_history*.py`` and ``scripts/live_collector_smoke.py``,
which never consult this module); what this guard prevents is their use for
*analysis* -- ``scripts/run_study.py``, ``scripts/analyze_period.py``,
``scripts/custody_flow.py`` and the eval-manifest leakage guard.

The active split is not a fixed filename and there is no CLI flag that
names one: it is resolved only through the current-split pointer file
(``fintr_current_split_v1``, ``configs/splits/current.json`` under the
repository root), because a split can be superseded (see
``configs/splits/bist30_2026_holdout_v1.json``'s ``superseded_by`` /
``frozen: false`` after DSTKF was excluded and the pool redrawn as v2).
``resolve_active_split`` fails closed: a missing or broken pointer, a
missing split file, or a split that is unfrozen or carries
``superseded_by`` is an error, never a silently disabled guard. A
historical split can still be parsed for audit with
``load_symbol_split(..., require_active=False)``.

This module never verifies a source universe's or the exclusions file's
recorded ``sha256`` against the actual file on disk -- those are recorded
provenance of what the split was drawn from, not re-checked here (the split
file's own bytes are what gets hashed and reported as "the split file
applied", via ``split_sha256``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import hashlib
import json
import re

from fintr.data.parsers.common import (
    MarketDataParseError,
    require_field,
    require_list,
    require_mapping,
    require_string,
)


SPLIT_SCHEMA = "fintr_symbol_split_v1"
CURRENT_POINTER_SCHEMA = "fintr_current_split_v1"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
# Absolute, so the guard does not depend on the working directory. Tests
# patch this module attribute with a synthetic pointer; there is
# deliberately no CLI flag or environment variable that changes it.
DEFAULT_CURRENT_POINTER_PATH = PROJECT_ROOT / "configs" / "splits" / "current.json"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SymbolSplitError(ValueError):
    """A fixed reason a symbol split (or its current-split pointer) is
    invalid, unreadable, or not usable as the active guard."""


@dataclass(frozen=True, slots=True)
class SourceRef:
    """One recorded ``{path, sha256}`` provenance entry; never re-verified
    against the referenced file (see module docstring)."""

    path: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise SymbolSplitError("invalid_source_ref_path")
        if not isinstance(self.sha256, str) or not _SHA256_RE.match(self.sha256):
            raise SymbolSplitError("invalid_source_ref_sha256")


def _string_tuple(raw: Any, path: str) -> tuple[str, ...]:
    items = require_list(raw, path)
    return tuple(require_string(item, f"{path}[{index}]") for index, item in enumerate(items))


def _parse_source_ref(raw: Any, path: str) -> SourceRef:
    item = require_mapping(raw, path)
    return SourceRef(
        path=require_string(require_field(item, "path", path), f"{path}.path"),
        sha256=require_string(require_field(item, "sha256", path), f"{path}.sha256"),
    )


@dataclass(frozen=True, slots=True)
class SymbolSplit:
    split_id: str
    seed: int
    frozen: bool
    holdout_symbols: tuple[str, ...]
    development_symbols: tuple[str, ...]
    source_universes: tuple[SourceRef, ...]
    exclusions_file: SourceRef | None
    pool_size: int
    superseded_by: str | None
    split_sha256: str | None
    raw_document: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.split_id, str) or not self.split_id:
            raise SymbolSplitError("invalid_split_id")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise SymbolSplitError("invalid_seed")
        if type(self.frozen) is not bool:
            raise SymbolSplitError("invalid_frozen")
        for name, symbols in (
            ("holdout_symbols", self.holdout_symbols),
            ("development_symbols", self.development_symbols),
        ):
            if not isinstance(symbols, tuple) or not symbols:
                raise SymbolSplitError(f"invalid_{name}")
            if any(not isinstance(s, str) or not s for s in symbols):
                raise SymbolSplitError(f"invalid_{name}")
            if len(set(symbols)) != len(symbols):
                raise SymbolSplitError(f"duplicate_{name}")
            if any(s != s.upper() for s in symbols):
                raise SymbolSplitError(f"{name}_not_uppercase")
            if tuple(sorted(symbols)) != symbols:
                raise SymbolSplitError(f"{name}_not_sorted")
        if set(self.holdout_symbols) & set(self.development_symbols):
            raise SymbolSplitError("holdout_development_overlap")
        if not isinstance(self.source_universes, tuple) or not self.source_universes or any(
            not isinstance(item, SourceRef) for item in self.source_universes
        ):
            raise SymbolSplitError("invalid_source_universes")
        if self.exclusions_file is not None and not isinstance(self.exclusions_file, SourceRef):
            raise SymbolSplitError("invalid_exclusions_file")
        if isinstance(self.pool_size, bool) or not isinstance(self.pool_size, int) or self.pool_size < 1:
            raise SymbolSplitError("invalid_pool_size")
        if self.pool_size != len(self.holdout_symbols) + len(self.development_symbols):
            raise SymbolSplitError("pool_size_mismatch")
        if self.superseded_by is not None and (
            not isinstance(self.superseded_by, str) or not self.superseded_by
        ):
            raise SymbolSplitError("invalid_superseded_by")
        if self.split_sha256 is not None and not _SHA256_RE.match(self.split_sha256):
            raise SymbolSplitError("invalid_split_sha256")

    def all_symbols(self) -> tuple[str, ...]:
        """Every symbol this split ever names (holdout + development)."""
        return tuple(sorted(set(self.holdout_symbols) | set(self.development_symbols)))


def parse_symbol_split(document: Any, *, split_sha256: str | None = None) -> SymbolSplit:
    """Structural validation only: schema, types, disjointness, sort order,
    pool-size consistency. Does **not** check ``frozen``/``superseded_by``
    against the active-guard policy -- see ``require_split_active``."""
    try:
        root = require_mapping(document, "split")
        if root.get("schema") != SPLIT_SCHEMA:
            raise SymbolSplitError("unsupported_split_schema")
        source_universes = tuple(
            _parse_source_ref(raw, f"split.source_universes[{index}]")
            for index, raw in enumerate(
                require_list(
                    require_field(root, "source_universes", "split"), "split.source_universes",
                )
            )
        )
        exclusions_file_raw = root.get("exclusions_file")
        exclusions_file = (
            None if exclusions_file_raw is None
            else _parse_source_ref(exclusions_file_raw, "split.exclusions_file")
        )
        frozen = require_field(root, "frozen", "split")
        if type(frozen) is not bool:
            raise SymbolSplitError("invalid_frozen")
        seed = require_field(root, "seed", "split")
        pool_size = require_field(root, "pool_size", "split")
        superseded_by = root.get("superseded_by")
        return SymbolSplit(
            split_id=require_string(require_field(root, "split_id", "split"), "split.split_id"),
            seed=seed,
            frozen=frozen,
            holdout_symbols=_string_tuple(
                require_field(root, "holdout_symbols", "split"), "split.holdout_symbols",
            ),
            development_symbols=_string_tuple(
                require_field(root, "development_symbols", "split"), "split.development_symbols",
            ),
            source_universes=source_universes,
            exclusions_file=exclusions_file,
            pool_size=pool_size,
            superseded_by=superseded_by,
            split_sha256=split_sha256,
            raw_document=document,
        )
    except MarketDataParseError as exc:
        raise SymbolSplitError(f"invalid_split_field: {exc.field_path or 'unknown'}") from None


def require_split_active(split: SymbolSplit) -> None:
    """Raise unless ``split`` may be used as the active enforcement gate:
    not superseded, and frozen. A historical split (kept for audit, e.g.
    ``bist30_2026_holdout_v1.json`` after v2 superseded it) still parses
    fine; it just can never gate a script's enforcement."""
    # Presence of the key is enough (even ``null``): a split that was ever
    # marked as superseded is never active again.
    if split.superseded_by is not None or "superseded_by" in split.raw_document:
        raise SymbolSplitError("split_superseded")
    if not split.frozen:
        raise SymbolSplitError("split_not_frozen")


def load_symbol_split(path: Path, *, require_active: bool = True) -> SymbolSplit:
    """Read, parse and (by default) require-active one split file.

    ``split_sha256`` is the SHA-256 of the exact file bytes read -- this is
    what every enforcement script reports as "which split file + its
    sha256 was applied".
    """
    try:
        raw_bytes = Path(path).read_bytes()
        document = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SymbolSplitError("unreadable_split") from None
    split = parse_symbol_split(document, split_sha256=hashlib.sha256(raw_bytes).hexdigest())
    if require_active:
        require_split_active(split)
    return split


def load_current_split_pointer(path: Path = DEFAULT_CURRENT_POINTER_PATH) -> Path:
    """Parse a ``fintr_current_split_v1`` pointer file and return the
    (unresolved, as-written) path of the split it names."""
    try:
        raw_bytes = Path(path).read_bytes()
        document = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SymbolSplitError("unreadable_current_split_pointer") from None
    try:
        root = require_mapping(document, "current_split_pointer")
        if root.get("schema") != CURRENT_POINTER_SCHEMA:
            raise SymbolSplitError("unsupported_current_split_pointer_schema")
        current = require_string(
            require_field(root, "current", "current_split_pointer"),
            "current_split_pointer.current",
        )
    except MarketDataParseError as exc:
        raise SymbolSplitError(
            f"invalid_current_split_pointer_field: {exc.field_path or 'unknown'}"
        ) from None
    return Path(current)


def resolve_active_split(pointer_path: Path | None = None) -> tuple[Path, SymbolSplit]:
    """Resolve, load and require-active the split named by the pointer.

    ``pointer_path`` defaults to ``DEFAULT_CURRENT_POINTER_PATH`` (read at
    call time). A relative ``current`` is resolved against the repository
    root the pointer lives in, i.e. ``pointer_path.parents[2]`` for the
    ``<root>/configs/splits/current.json`` layout. Fails closed: every
    problem raises ``SymbolSplitError``; nothing returns "no split".
    """
    pointer = Path(DEFAULT_CURRENT_POINTER_PATH if pointer_path is None else pointer_path)
    if not pointer.is_file():
        raise SymbolSplitError("current_split_pointer_missing")
    target = load_current_split_pointer(pointer)
    if not target.is_absolute():
        try:
            root = pointer.resolve().parents[2]
        except IndexError:
            raise SymbolSplitError("current_split_pointer_outside_repository_layout") from None
        target = root / target
    if not target.is_file():
        raise SymbolSplitError("current_split_file_missing")
    return target, load_symbol_split(target, require_active=True)


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def is_holdout(split: SymbolSplit, symbol: str) -> bool:
    return _normalize_symbol(symbol) in split.holdout_symbols


def is_development_symbol(split: SymbolSplit, symbol: str) -> bool:
    return _normalize_symbol(symbol) in split.development_symbols


NO_SPLIT_GUARD_WARNING = (
    "WARNING: --no-split-guard is set -- the frozen BIST 30 holdout-symbol "
    "guard is DISABLED for this run. This flag exists only for non-BIST 30 "
    "exploratory work; holdout symbols processed under it are listed in the "
    "output. Symbol exclusions still apply (they have no override)."
)
