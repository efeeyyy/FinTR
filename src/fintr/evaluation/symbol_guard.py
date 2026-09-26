"""Shared enforcement of the frozen BIST 30 split and the symbol exclusions.

Every CLI that analyses, studies or evaluates symbol data loads its guards
through ``load_active_guards``: the active split comes only from the
current-split pointer (``fintr.evaluation.symbol_split.resolve_active_split``)
and the exclusions only from ``configs/exclusions/symbols.json``
(``fintr.evaluation.symbol_exclusions.load_active_symbol_exclusions``). Both
fail closed. Collection tools (``scripts/fetch_price_history*.py``,
``scripts/live_collector_smoke.py``) load only the exclusions: collecting a
holdout symbol is allowed, collecting an excluded one is not.

``--no-split-guard`` (``bypass_split=True``) disables only the holdout
checks, and only for the run it is passed to; the split is still loaded
and reported, and the exclusions are never bypassed. It exists for
non-BIST 30 exploratory work, so every output of a bypassed run carries
``guard_bypassed: true``, the warning text, and the holdout symbols that
were processed only because of the bypass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from fintr.evaluation import symbol_exclusions as _exclusions
from fintr.evaluation import symbol_split as _split
from fintr.evaluation.symbol_exclusions import SymbolExclusions, SymbolExclusionsError
from fintr.evaluation.symbol_split import (
    NO_SPLIT_GUARD_WARNING,
    SymbolSplit,
    SymbolSplitError,
)


GUARD_REPORT_SCHEMA = "fintr_symbol_guard_report_v1"


class SymbolGuardError(ValueError):
    """The split or exclusions file could not be loaded as an active guard."""


def display_path(path: Path) -> str:
    """Repository-relative POSIX path when under the project root, else the
    absolute path (synthetic test files)."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(_split.PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved)


@dataclass(frozen=True, slots=True)
class ActiveGuards:
    split: SymbolSplit
    split_path: Path
    exclusions: SymbolExclusions
    exclusions_path: Path
    split_guard_bypassed: bool

    def is_excluded(self, symbol: str) -> bool:
        return self.exclusions.is_excluded(symbol)

    def exclusion_reason(self, symbol: str) -> str | None:
        entry = self.exclusions.entry_for(symbol)
        return None if entry is None else entry.reason

    def is_holdout(self, symbol: str) -> bool:
        return _split.is_holdout(self.split, symbol)

    def blocks_holdout(self, symbol: str) -> bool:
        """True when ``symbol`` is a holdout symbol and the guard is on."""
        return not self.split_guard_bypassed and self.is_holdout(symbol)

    def report(self, *, processed_symbols: Iterable[str] = ()) -> dict[str, Any]:
        """The block every enforcement script embeds in its output."""
        bypassed_holdout = sorted({
            symbol.strip().upper() for symbol in processed_symbols
            if self.split_guard_bypassed and self.is_holdout(symbol)
        })
        return {
            "schema": GUARD_REPORT_SCHEMA,
            "split_pointer": display_path(_split.DEFAULT_CURRENT_POINTER_PATH),
            "split_file": display_path(self.split_path),
            "split_file_sha256": self.split.split_sha256,
            "split_id": self.split.split_id,
            "exclusions_file": display_path(self.exclusions_path),
            "exclusions_file_sha256": self.exclusions.file_sha256,
            "excluded_symbols": sorted(entry.symbol for entry in self.exclusions.entries),
            "guard_bypassed": self.split_guard_bypassed,
            "bypass_warning": NO_SPLIT_GUARD_WARNING if self.split_guard_bypassed else None,
            "bypassed_holdout_symbols": bypassed_holdout,
        }


def load_active_exclusions() -> tuple[Path, SymbolExclusions]:
    try:
        return _exclusions.load_active_symbol_exclusions()
    except SymbolExclusionsError as exc:
        raise SymbolGuardError(f"invalid exclusions file ({exc})") from None


def load_active_guards(*, bypass_split: bool = False) -> ActiveGuards:
    exclusions_path, exclusions = load_active_exclusions()
    try:
        split_path, split = _split.resolve_active_split()
    except SymbolSplitError as exc:
        raise SymbolGuardError(f"invalid active split ({exc})") from None
    overlap = sorted(
        symbol for symbol in split.all_symbols() if exclusions.is_excluded(symbol)
    )
    if overlap:
        raise SymbolGuardError(
            f"invalid active split (split_contains_excluded_symbol: {','.join(overlap)})"
        )
    return ActiveGuards(
        split=split, split_path=split_path, exclusions=exclusions,
        exclusions_path=exclusions_path, split_guard_bypassed=bypass_split,
    )


def exclusions_report(path: Path, exclusions: SymbolExclusions) -> dict[str, Any]:
    """Provenance block for collection tools, which load no split."""
    return {
        "exclusions_file": display_path(path),
        "exclusions_file_sha256": exclusions.file_sha256,
        "excluded_symbols": sorted(entry.symbol for entry in exclusions.entries),
    }


def render_guard_lines(report: dict[str, Any]) -> list[str]:
    """Turkish text lines for a guard report; the bypass warning comes first
    so it cannot be missed."""
    lines: list[str] = []
    if report["guard_bypassed"]:
        holdout = ", ".join(report["bypassed_holdout_symbols"]) or "yok"
        lines.append(
            "!!! UYARI: --no-split-guard AKTİF -- BIST 30 holdout koruması bu "
            "çalışmada KAPALI (yalnız BIST 30 dışı keşif içindir). Koruma "
            f"kapalıyken işlenen holdout hisseleri: {holdout} !!!"
        )
    lines.append(
        f"Ayrım dosyası: {report['split_file']} (sha256={report['split_file_sha256']}, "
        f"split_id={report['split_id']}, işaretçi={report['split_pointer']})"
    )
    lines.append(
        f"Dışlama dosyası: {report['exclusions_file']} "
        f"(sha256={report['exclusions_file_sha256']}; dışlanan: "
        f"{', '.join(report['excluded_symbols']) or 'yok'})"
    )
    return lines
