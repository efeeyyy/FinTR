import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path

from fintr.data.parsers import MissingFieldError, parse_akd_snapshot


FIXTURE = Path(__file__).parent / "fixtures" / "market" / "akd_snapshot.json"


class AKDParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.event = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_preserves_positive_negative_and_zero_net(self) -> None:
        snapshot = parse_akd_snapshot(self.event)
        self.assertEqual([row.net_lots for row in snapshot.brokers], [100000, -75000, 0])
        self.assertEqual(snapshot.market_price, Decimal("123.5"))

    def test_accepts_empty_results(self) -> None:
        event = copy.deepcopy(self.event)
        event["data"]["results"] = []
        self.assertEqual(parse_akd_snapshot(event).brokers, ())

    def test_rejects_missing_broker_code(self) -> None:
        event = copy.deepcopy(self.event)
        del event["data"]["results"][0]["code"]
        with self.assertRaisesRegex(MissingFieldError, "code"):
            parse_akd_snapshot(event)

    def test_rejects_missing_date(self) -> None:
        event = copy.deepcopy(self.event)
        del event["date"]
        with self.assertRaisesRegex(MissingFieldError, "date"):
            parse_akd_snapshot(event)


if __name__ == "__main__":
    unittest.main()
