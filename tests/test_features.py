import json
import unittest
from decimal import Decimal
from pathlib import Path

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


FIXTURES = Path(__file__).parent / "fixtures" / "market"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FeatureTests(unittest.TestCase):
    def test_depth_features(self) -> None:
        features = calculate_depth_features(parse_depth_update(load("depth_update.json")))
        self.assertEqual(features.total_bid_lots, 9_000_000_002_500)
        self.assertEqual(features.total_ask_lots, 7300)
        self.assertEqual(features.spread, Decimal("0.1"))

    def test_akd_features(self) -> None:
        features = calculate_akd_features(parse_akd_snapshot(load("akd_snapshot.json")))
        self.assertEqual(features.top_buyers[0].code, "AAA")
        self.assertEqual(features.top_sellers[0].code, "BBB")
        self.assertIsNotNone(features.broker_cost_vs_market_price[0].gap_pct)

    def test_custody_horizons_concentration_and_persistence(self) -> None:
        distribution = parse_custody_distribution(load("custody_distribution_data.json"))
        changes = calculate_horizon_changes(distribution)
        self.assertEqual(changes[0].change_1d, 500432)
        self.assertEqual(changes[1].change_90d, -2000000)

        snapshot = parse_custodian_snapshot(load("custodian_data.json"))
        self.assertIsNotNone(calculate_top5_custody_concentration(snapshot))

        history = parse_custody_history(load("custody_analysis_data.json"))
        persistence = {item.code: item for item in calculate_broker_persistence(history)}
        self.assertEqual(persistence["AAA"].persistence, Decimal("1"))
        self.assertEqual(persistence["BBB"].persistence, Decimal("1"))

    def test_stats_features(self) -> None:
        features = calculate_stats_features(parse_custody_stats(load("custody_stats_data.json")))
        self.assertEqual(features.retail_share_change, Decimal("-1.5"))
        self.assertEqual(features.institutional_share_change, Decimal("1.5"))
        self.assertEqual(features.investor_count_change, 1250)


if __name__ == "__main__":
    unittest.main()
