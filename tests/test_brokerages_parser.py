import json
import unittest
from pathlib import Path

from fintr.data.parsers import MissingFieldError, parse_brokerages


FIXTURE = Path(__file__).parent / "fixtures" / "market" / "brokerages_data.json"


class BrokeragesParserTests(unittest.TestCase):
    def test_parses_directory(self) -> None:
        event = json.loads(FIXTURE.read_text(encoding="utf-8"))
        directory = parse_brokerages(event)
        self.assertEqual(directory.brokers[0].code, "AAA")
        self.assertEqual(directory.as_of.isoformat(), "2026-09-22")

    def test_missing_code_is_explicit(self) -> None:
        event = json.loads(FIXTURE.read_text(encoding="utf-8"))
        del event["data"]["results"][0]["code"]
        with self.assertRaisesRegex(MissingFieldError, "code"):
            parse_brokerages(event)


if __name__ == "__main__":
    unittest.main()
