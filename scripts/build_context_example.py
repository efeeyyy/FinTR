"""Render the v0 context example from sanitized fixtures without model inference."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from fintr.data.models import MarketSnapshot
from fintr.data.parsers import (
    parse_akd_snapshot,
    parse_custodian_snapshot,
    parse_custody_distribution,
    parse_custody_history,
    parse_custody_stats,
    parse_depth_update,
)
from fintr.features import (
    calculate_akd_features,
    calculate_broker_persistence,
    calculate_depth_features,
    calculate_horizon_changes,
    calculate_stats_features,
    calculate_top5_custody_concentration,
)
from fintr.inference.context import (
    MarketFeatureSet,
    build_market_context,
    render_market_context,
)
from fintr.inference.prompts import build_analysis_prompt


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "market"
ISTANBUL = timezone(timedelta(hours=3))


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def build_example_context():
    snapshot = MarketSnapshot(
        symbol="GLRMK",
        as_of=datetime(2026, 9, 22, 10, 16, tzinfo=ISTANBUL),
        depth=parse_depth_update(_load("depth_update.json")),
        akd=parse_akd_snapshot(_load("akd_snapshot.json")),
        custody=parse_custodian_snapshot(_load("custodian_data.json")),
        custody_distribution=parse_custody_distribution(
            _load("custody_distribution_data.json")
        ),
        custody_history=parse_custody_history(_load("custody_analysis_data.json")),
        custody_stats=parse_custody_stats(_load("custody_stats_data.json")),
    )
    features = MarketFeatureSet(
        depth=calculate_depth_features(snapshot.depth),
        akd=calculate_akd_features(snapshot.akd),
        custody_changes=calculate_horizon_changes(snapshot.custody_distribution),
        custody_top5_concentration=calculate_top5_custody_concentration(
            snapshot.custody
        ),
        custody_persistence=calculate_broker_persistence(snapshot.custody_history),
        stats=calculate_stats_features(snapshot.custody_stats),
    )
    return build_market_context(snapshot, features)


def render_document() -> str:
    context = build_example_context()
    rendered = render_market_context(context)
    prompt = build_analysis_prompt(context)
    return f"""# Market context example v0

Bu örnek yalnızca `tests/fixtures/market/` altındaki sanitize fixture'lardan
deterministik olarak üretilmiştir. Canlı bağlantı veya model çağrısı yapılmamıştır.

## Structured context

```json
{context.to_json()}
```

## Rendered text context

```text
{rendered.rstrip()}
```

## Analysis prompt

### System

```text
{prompt.system}
```

### User

```text
{prompt.user}
```
"""


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(render_document(), end="")
