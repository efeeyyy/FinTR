"""Synthetic, offline custody-to-brokerage association tests."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import unittest

from fintr.data import (
    BrokerageResolutionStatus,
    LiveBrokerageIndex,
    resolve_live_custodian_brokerages,
    resolve_live_custodian_diff_brokerages,
)
from fintr.data.parsers.brokerages_live import parse_live_brokerages_data
from fintr.data.parsers.custodian_diff_live import parse_live_custodian_diff
from fintr.data.parsers.custodian_live import parse_live_custodian_data


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "market"


def event(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"), parse_float=Decimal)


def directory(*rows: tuple[str, str, str]) -> LiveBrokerageIndex:
    payload = [
        {"code": code, "title": title, "short_title": short_title}
        for code, title, short_title in rows
    ]
    response = parse_live_brokerages_data({
        "type": "brokerages_data", "data": {"data": json.dumps(payload)},
    })
    return LiveBrokerageIndex.from_response(response)


class CustodianBrokerageAssociationTests(unittest.TestCase):
    def test_daily_exact_match_and_unknown_preserve_numbers_and_source(self) -> None:
        raw = event("custodian_data_live.json")
        before = deepcopy(raw)
        custody = parse_live_custodian_data(raw)
        index = directory(("SYN1", "Synthetic Custodian", "Synthetic"))

        resolved = resolve_live_custodian_brokerages(custody, index)

        self.assertIs(resolved.source, custody)
        self.assertIs(resolved.directory, index)
        self.assertEqual(
            [item.row.custodian for item in resolved.rows], ["SYN1", "SYN2"],
        )
        self.assertIs(resolved.rows[0].row, custody.results[0])
        self.assertIs(resolved.rows[0].resolution.status, BrokerageResolutionStatus.RESOLVED)
        self.assertEqual(resolved.rows[0].resolution.label.title, "Synthetic Custodian")
        self.assertIs(resolved.rows[1].resolution.status, BrokerageResolutionStatus.UNRESOLVED)
        self.assertIsNone(resolved.rows[1].resolution.label)
        self.assertEqual(resolved.rows[0].row.value, Decimal("123.4567890123456789"))
        self.assertEqual(custody.date.isoformat(), "2026-09-22")
        self.assertEqual(raw, before)
        self.assertNotIn("Synthetic Custodian", repr(resolved))
        self.assertNotIn("SYN1", repr(resolved))

    def test_diff_conflict_and_match_preserve_range_and_signed_values(self) -> None:
        raw = event("custodian_diff_data_live.json")
        before = deepcopy(raw)
        custody = parse_live_custodian_diff(raw)
        index = directory(
            ("SYN1", "First Synthetic", "First"),
            ("SYN1", "Second Synthetic", "Second"),
            ("SYN2", "Third Synthetic", "Third"),
        )

        resolved = resolve_live_custodian_diff_brokerages(custody, index)

        self.assertIs(resolved.source, custody)
        self.assertIs(resolved.directory, index)
        self.assertIs(resolved.rows[0].row, custody.results[0])
        self.assertIs(resolved.rows[0].resolution.status, BrokerageResolutionStatus.AMBIGUOUS)
        self.assertIsNone(resolved.rows[0].resolution.label)
        self.assertIs(resolved.rows[1].resolution.status, BrokerageResolutionStatus.RESOLVED)
        self.assertEqual(resolved.rows[1].resolution.label.short_title, "Third")
        self.assertEqual(resolved.rows[0].row.difference, Decimal("-24.625"))
        self.assertEqual(resolved.rows[0].row.first_value, Decimal("100.125"))
        self.assertEqual(resolved.rows[0].row.last_value, Decimal("75.5"))
        self.assertEqual(custody.start_date.isoformat(), "2026-09-14")
        self.assertEqual(custody.end_date.isoformat(), "2026-09-22")
        self.assertEqual(raw, before)
        self.assertNotIn("First Synthetic", repr(resolved))
        self.assertNotIn("SYN1", repr(resolved))

    def test_empty_rows_stay_empty_without_invented_labels(self) -> None:
        for name, parser, resolver in (
            ("custodian_data_live.json", parse_live_custodian_data, resolve_live_custodian_brokerages),
            ("custodian_diff_data_live.json", parse_live_custodian_diff, resolve_live_custodian_diff_brokerages),
        ):
            with self.subTest(name=name):
                raw = event(name)
                raw["data"]["data"]["results"] = []
                parsed = parser(raw)
                self.assertEqual(resolver(parsed, directory()).rows, ())

    def test_wrong_input_types_fail_without_payload_values(self) -> None:
        daily = parse_live_custodian_data(event("custodian_data_live.json"))
        diff = parse_live_custodian_diff(event("custodian_diff_data_live.json"))
        for resolver, response in (
            (resolve_live_custodian_brokerages, daily),
            (resolve_live_custodian_diff_brokerages, diff),
        ):
            for bad_response, bad_index in (("PRIVATE", directory()), (response, "PRIVATE")):
                with self.subTest(resolver=resolver.__name__, bad=type(bad_response).__name__):
                    with self.assertRaises(TypeError) as captured:
                        resolver(bad_response, bad_index)
                    self.assertNotIn("PRIVATE", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
