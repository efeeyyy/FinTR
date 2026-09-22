"""Deterministic custody/takas features."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from fintr.data.models import CustodianSnapshot, CustodyDistributionSnapshot, CustodyHistory


@dataclass(frozen=True, slots=True)
class HorizonChanges:
    code: str
    change_1d: int
    change_7d: int
    change_30d: int
    change_90d: int


@dataclass(frozen=True, slots=True)
class BrokerPersistence:
    code: str
    direction: str | None
    persistence: Decimal | None
    nonzero_observations: int


def calculate_horizon_changes(
    snapshot: CustodyDistributionSnapshot,
) -> tuple[HorizonChanges, ...]:
    return tuple(
        HorizonChanges(
            code=row.code,
            change_1d=row.values[0] - row.values[1],
            change_7d=row.values[0] - row.values[2],
            change_30d=row.values[0] - row.values[3],
            change_90d=row.values[0] - row.values[4],
        )
        for row in snapshot.rows
    )


def calculate_top5_custody_concentration(snapshot: CustodianSnapshot) -> Decimal | None:
    total = sum(row.value for row in snapshot.rows)
    if total == 0:
        return None
    top5 = sum(sorted((row.value for row in snapshot.rows), reverse=True)[:5])
    return Decimal(top5) / Decimal(total)


def calculate_broker_persistence(history: CustodyHistory) -> tuple[BrokerPersistence, ...]:
    directions: dict[str, list[int]] = defaultdict(list)
    for point in history.points:
        if point.daily_net > 0:
            directions[point.broker_code].append(1)
        elif point.daily_net < 0:
            directions[point.broker_code].append(-1)
        else:
            directions.setdefault(point.broker_code, [])

    result: list[BrokerPersistence] = []
    for code in sorted(directions):
        signs = directions[code]
        if not signs:
            result.append(BrokerPersistence(code, None, None, 0))
            continue
        buy_days = signs.count(1)
        sell_days = signs.count(-1)
        dominant = max(buy_days, sell_days)
        direction = "buy" if buy_days > sell_days else "sell" if sell_days > buy_days else "mixed"
        result.append(
            BrokerPersistence(
                code=code,
                direction=direction,
                persistence=Decimal(dominant) / Decimal(len(signs)),
                nonzero_observations=len(signs),
            )
        )
    return tuple(result)
