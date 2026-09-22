import copy
import json
import unittest
from pathlib import Path

from fintr.data.parsers import FieldTypeError, parse_depth_update


FIXTURE = Path(__file__).parent / "fixtures" / "market" / "depth_update.json"


class DepthParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.event = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_parses_symbol_timestamp_zero_and_huge_lots(self) -> None:
        snapshot = parse_depth_update(self.event)
        self.assertEqual(snapshot.symbol, "GLRMK")
        self.assertEqual(snapshot.timestamp.isoformat(), "2026-09-22T10:15:30+03:00")
        self.assertEqual(snapshot.bids[0].lots, 9_000_000_000_000)
        self.assertEqual(snapshot.bids[2].lots, 0)

    def test_accepts_empty_sides(self) -> None:
        event = copy.deepcopy(self.event)
        event["data"]["bids"] = []
        event["data"]["asks"] = []
        snapshot = parse_depth_update(event)
        self.assertEqual(snapshot.bids, ())
        self.assertEqual(snapshot.asks, ())

    def test_rejects_numeric_string_without_coercion(self) -> None:
        event = copy.deepcopy(self.event)
        event["data"]["bids"][0]["lots"] = "9000000000000"
        with self.assertRaisesRegex(FieldTypeError, "expected integer"):
            parse_depth_update(event)


if __name__ == "__main__":
    unittest.main()
