"""Date-effective equity settlement lag (in server dates) from configuration.

Fail closed: any malformed, unsorted, duplicate or unknown-key content, and
any ``as_of`` before the first period, raises ``SettlementCycleError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path

SETTLEMENT_CYCLE_SCHEMA = "fintr_settlement_cycle_v1"
DEFAULT_SETTLEMENT_CYCLE_PATH = Path("configs/market/settlement_cycle.json")

_TOP_KEYS = {"schema", "description", "source", "periods"}
_PERIOD_KEYS = {"effective_from", "settlement_lag_server_dates"}


class SettlementCycleError(ValueError):
    """The settlement cycle config is invalid or has no period for the date."""


@dataclass(frozen=True, slots=True)
class SettlementPeriod:
    effective_from: date
    settlement_lag_server_dates: int


@dataclass(frozen=True, slots=True)
class SettlementCycle:
    periods: tuple[SettlementPeriod, ...]

    def lag_on(self, as_of: date) -> int:
        lag: int | None = None
        for period in self.periods:
            if period.effective_from <= as_of:
                lag = period.settlement_lag_server_dates
            else:
                break
        if lag is None:
            raise SettlementCycleError(f"no_settlement_period_for: {as_of.isoformat()}")
        return lag


def parse_settlement_cycle(document: object) -> SettlementCycle:
    if not isinstance(document, dict) or document.get("schema") != SETTLEMENT_CYCLE_SCHEMA:
        raise SettlementCycleError("unsupported_settlement_cycle_schema")
    unknown = set(document) - _TOP_KEYS
    if unknown:
        raise SettlementCycleError(f"unknown_keys: {sorted(unknown)}")
    for key in ("description", "source"):
        if not isinstance(document.get(key), str) or not document[key]:
            raise SettlementCycleError(f"{key}: expected a non-empty string")
    raw_periods = document.get("periods")
    if not isinstance(raw_periods, list) or not raw_periods:
        raise SettlementCycleError("periods: expected a non-empty list")
    periods: list[SettlementPeriod] = []
    for index, raw in enumerate(raw_periods):
        path = f"periods[{index}]"
        if not isinstance(raw, dict):
            raise SettlementCycleError(f"{path}: expected an object")
        if set(raw) != _PERIOD_KEYS:
            raise SettlementCycleError(f"{path}: keys must be exactly {sorted(_PERIOD_KEYS)}")
        text = raw["effective_from"]
        try:
            if not isinstance(text, str) or len(text) != 10:
                raise ValueError
            effective_from = date.fromisoformat(text)
        except ValueError:
            raise SettlementCycleError(f"{path}.effective_from: expected YYYY-MM-DD") from None
        lag = raw["settlement_lag_server_dates"]
        if isinstance(lag, bool) or not isinstance(lag, int) or lag <= 0:
            raise SettlementCycleError(f"{path}.settlement_lag_server_dates: expected a positive integer")
        if periods and effective_from <= periods[-1].effective_from:
            raise SettlementCycleError(f"{path}.effective_from: must be strictly increasing")
        periods.append(SettlementPeriod(effective_from, lag))
    return SettlementCycle(tuple(periods))


def load_settlement_cycle(path: Path) -> SettlementCycle:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SettlementCycleError("unreadable_settlement_cycle_file") from None
    return parse_settlement_cycle(document)
