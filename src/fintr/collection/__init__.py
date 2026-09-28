"""Daily incremental collection: offline planning only (ADR daily_miniapp_collection_adr.md).

See :mod:`fintr.collection.planner` for the pure window planner. No module
under this package opens a network connection or prompts for a secret --
that stays in ``scripts/collect_daily.py`` (dry-run only for now) and, in a
later PR, the run executor that reuses
``scripts.live_collector_smoke._collect_multi_symbol``.
"""

from __future__ import annotations
