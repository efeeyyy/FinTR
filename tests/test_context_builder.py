import json
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fintr.data.models import AKDBroker, CustodianRow, DepthLevel, MarketSnapshot
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
    ContextBuildError,
    ContextLimits,
    MarketFeatureSet,
    MarketSummary,
    RecentTradesSummary,
    build_market_context,
    render_market_context,
)
from fintr.inference.prompts import build_analysis_prompt


FIXTURES = Path(__file__).parent / "fixtures" / "market"
ISTANBUL = timezone(timedelta(hours=3))
ANALYSIS_TIME = datetime(2026, 9, 22, 10, 16, tzinfo=ISTANBUL)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="GLRMK",
        as_of=ANALYSIS_TIME,
        depth=parse_depth_update(load("depth_update.json")),
        akd=parse_akd_snapshot(load("akd_snapshot.json")),
        custody=parse_custodian_snapshot(load("custodian_data.json")),
        custody_distribution=parse_custody_distribution(
            load("custody_distribution_data.json")
        ),
        custody_history=parse_custody_history(load("custody_analysis_data.json")),
        custody_stats=parse_custody_stats(load("custody_stats_data.json")),
    )


def make_features(snapshot: MarketSnapshot) -> MarketFeatureSet:
    return MarketFeatureSet(
        depth=(
            None
            if snapshot.depth is None
            else calculate_depth_features(snapshot.depth)
        ),
        akd=(None if snapshot.akd is None else calculate_akd_features(snapshot.akd)),
        custody_changes=(
            ()
            if snapshot.custody_distribution is None
            else calculate_horizon_changes(snapshot.custody_distribution)
        ),
        custody_top5_concentration=(
            None
            if snapshot.custody is None
            else calculate_top5_custody_concentration(snapshot.custody)
        ),
        custody_persistence=(
            ()
            if snapshot.custody_history is None
            else calculate_broker_persistence(snapshot.custody_history)
        ),
        stats=(
            None
            if snapshot.custody_stats is None
            else calculate_stats_features(snapshot.custody_stats)
        ),
    )


def build_full_context(snapshot: MarketSnapshot | None = None):
    selected = make_snapshot() if snapshot is None else snapshot
    return build_market_context(
        selected,
        make_features(selected),
        market=MarketSummary(
            source="get_chart_data",
            as_of=ANALYSIS_TIME,
            last_price=Decimal("123.50"),
            volume=9_000_000_000_000,
        ),
        recent_trades=RecentTradesSummary(
            source="trade_flow_summary",
            as_of=ANALYSIS_TIME,
            trade_count=42,
            total_lots=250_000,
        ),
    )


class ContextBuilderTests(unittest.TestCase):
    def test_all_supported_data_is_available_with_provenance(self) -> None:
        context = build_full_context()
        self.assertEqual(context.schema_version, "market_context_v0")
        self.assertTrue(all(context.data_availability.values()))
        self.assertEqual(context.missing_for_stronger_analysis, ())
        self.assertEqual(context.depth["source"], "depth_update")
        self.assertEqual(context.akd["source"], "akd_snapshot")
        self.assertEqual(context.custody["snapshot"]["source"], "custodian_data")
        self.assertEqual(
            context.custody["history"]["source"], "custody_analysis_data"
        )
        self.assertEqual(context.custody_stats["source"], "custody_stats_data")

    def test_missing_custody_snapshot_is_not_zero(self) -> None:
        snapshot = replace(make_snapshot(), custody=None)
        context = build_market_context(snapshot, make_features(snapshot))
        self.assertFalse(context.data_availability["custody_snapshot"])
        self.assertIn(
            "current_custody_snapshot", context.missing_for_stronger_analysis
        )
        self.assertFalse(context.custody["snapshot"]["date"]["available"])
        self.assertIsNone(context.custody["snapshot"]["date"]["value"])
        self.assertEqual(context.custody["snapshot"]["top_holders"], [])

    def test_missing_akd_is_explicit(self) -> None:
        snapshot = replace(make_snapshot(), akd=None)
        context = build_market_context(snapshot, make_features(snapshot))
        self.assertFalse(context.data_availability["akd"])
        self.assertFalse(context.akd["market_price"]["available"])
        self.assertIsNone(context.akd["market_price"]["value"])

    def test_real_zero_differs_from_missing_null(self) -> None:
        snapshot = make_snapshot()
        zero_snapshot = replace(
            snapshot,
            akd=replace(snapshot.akd, market_price=Decimal("0")),
        )
        zero_context = build_market_context(
            zero_snapshot, make_features(zero_snapshot)
        )
        self.assertEqual(
            zero_context.akd["market_price"], {"value": "0", "available": True}
        )

        missing_snapshot = replace(
            snapshot,
            akd=replace(snapshot.akd, market_price=None),
        )
        missing_context = build_market_context(
            missing_snapshot, make_features(missing_snapshot)
        )
        self.assertEqual(
            missing_context.akd["market_price"],
            {"value": None, "available": False},
        )

    def test_freshness_keeps_different_dates_separate(self) -> None:
        snapshot = make_snapshot()
        shifted = replace(
            snapshot,
            custody=replace(snapshot.custody, date=date(2026, 9, 19)),
        )
        context = build_market_context(shifted, make_features(shifted))
        self.assertEqual(
            context.data_freshness["akd_as_of"]["value"], "2026-09-22"
        )
        self.assertEqual(
            context.data_freshness["custody_date"]["value"], "2026-09-19"
        )
        self.assertNotEqual(
            context.data_freshness["akd_as_of"]["value"],
            context.data_freshness["custody_date"]["value"],
        )

    def test_top_n_limits_large_collections(self) -> None:
        snapshot = make_snapshot()
        levels = tuple(
            DepthLevel(Decimal(200 - index), 10_000 + index)
            for index in range(15)
        )
        brokers = tuple(
            AKDBroker(
                code=f"B{index:02d}",
                buy_lots=1000 + index,
                sell_lots=10,
                net_lots=990 + index,
                average_cost=Decimal("123.45"),
            )
            for index in range(12)
        )
        holders = tuple(
            CustodianRow(
                code=f"H{index:02d}",
                value=10_000_000_000 + index,
                percentage=Decimal("1.25"),
            )
            for index in range(14)
        )
        expanded = replace(
            snapshot,
            depth=replace(snapshot.depth, bids=levels, asks=levels),
            akd=replace(snapshot.akd, brokers=brokers),
            custody=replace(snapshot.custody, rows=holders),
        )
        context = build_market_context(
            expanded,
            make_features(expanded),
            limits=ContextLimits(
                depth_levels=3,
                akd_brokers=4,
                custody_holders=6,
            ),
        )
        self.assertEqual(len(context.depth["raw_summary"]["bids"]), 3)
        self.assertEqual(len(context.depth["raw_summary"]["asks"]), 3)
        self.assertEqual(len(context.akd["top_buyers"]), 4)
        self.assertEqual(len(context.custody["snapshot"]["top_holders"]), 6)

    def test_negative_net_large_integer_and_decimal_are_preserved(self) -> None:
        context = build_full_context()
        self.assertEqual(context.akd["top_sellers"][0]["net_lots"], -75_000)
        self.assertEqual(
            context.depth["features"]["total_bid_lots"]["value"],
            9_000_000_002_500,
        )
        self.assertEqual(context.market["last_price"]["value"], "123.50")
        self.assertEqual(
            context.depth["raw_summary"]["bids"][0]["price"], "123.4"
        )

    def test_renderer_is_byte_stable_and_does_not_mutate_context(self) -> None:
        context = build_full_context()
        before = context.to_dict()
        first = render_market_context(context).encode("utf-8")
        second = render_market_context(context).encode("utf-8")
        self.assertEqual(first, second)
        self.assertEqual(context.to_dict(), before)
        self.assertIn("[VERİ ZAMANLARI]", first.decode("utf-8"))

    def test_prompt_uses_renderer_without_model_call(self) -> None:
        context = build_full_context()
        prompt = build_analysis_prompt(context)
        self.assertIn("AKD ile Takas'ı karıştırmamak", prompt.system)
        self.assertIn(render_market_context(context).rstrip(), prompt.user)
        self.assertIn("Kesin fiyat yönü", prompt.system)

    def test_symbol_mismatch_is_rejected(self) -> None:
        snapshot = make_snapshot()
        mismatched = replace(
            snapshot,
            depth=replace(snapshot.depth, symbol="OTHER"),
        )
        with self.assertRaisesRegex(ContextBuildError, "symbol mismatch"):
            build_market_context(mismatched, make_features(mismatched))


if __name__ == "__main__":
    unittest.main()
