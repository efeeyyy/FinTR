import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path

from fintr.data.parsers import (
    FieldTypeError,
    MissingFieldError,
    parse_custodian_diff,
    parse_custodian_snapshot,
    parse_custody_distribution,
    parse_custody_history,
    parse_custody_stats,
)


FIXTURES = Path(__file__).parent / "fixtures" / "market"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class CustodyParserTests(unittest.TestCase):
    def test_snapshot_preserves_percentage_zero_and_huge_value(self) -> None:
        snapshot = parse_custodian_snapshot(load("custodian_data.json"))
        self.assertEqual(snapshot.rows[0].percentage, Decimal("30.6"))
        self.assertEqual(snapshot.rows[1].value, 0)
        self.assertEqual(snapshot.rows[2].value, 21_474_836_480)

    def test_diff_preserves_negative_difference(self) -> None:
        snapshot = parse_custodian_diff(load("custodian_diff_data.json"))
        self.assertEqual(snapshot.rows[1].difference, -500000)
        self.assertEqual(snapshot.first_date.isoformat(), "2026-09-15")

    def test_distribution_requires_exactly_five_horizons(self) -> None:
        event = load("custody_distribution_data.json")
        event["data"]["data"][0]["values"] = [1, 2]
        with self.assertRaisesRegex(FieldTypeError, "exactly 5"):
            parse_custody_distribution(event)

    def test_distribution_parses_periods(self) -> None:
        snapshot = parse_custody_distribution(load("custody_distribution_data.json"))
        self.assertEqual(len(snapshot.periods), 5)
        self.assertEqual(snapshot.rows[1].values[0], 0)

    def test_history_preserves_dates_and_signed_net(self) -> None:
        history = parse_custody_history(load("custody_analysis_data.json"))
        self.assertEqual(len(history.dates), 3)
        self.assertTrue(any(point.daily_net < 0 for point in history.points))
        self.assertTrue(any(point.daily_net == 0 for point in history.points))

    def test_stats_preserves_percentages_and_investor_count(self) -> None:
        stats = parse_custody_stats(load("custody_stats_data.json"))
        self.assertEqual(stats.retail_percentage[0].value, Decimal("42.5"))
        self.assertEqual(stats.investor_count[-1].value, 121250)

    def test_snapshot_missing_date_is_explicit(self) -> None:
        event = load("custodian_data.json")
        del event["date"]
        with self.assertRaisesRegex(MissingFieldError, "date"):
            parse_custodian_snapshot(event)

    def test_history_missing_broker_code_is_explicit(self) -> None:
        event = load("custody_analysis_data.json")
        del event["data"]["custody"]["2026-09-20"][0]["broker_code"]
        with self.assertRaisesRegex(MissingFieldError, "broker_code"):
            parse_custody_history(event)


if __name__ == "__main__":
    unittest.main()
