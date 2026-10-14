import copy
from datetime import date
import json
from pathlib import Path
import unittest

from fintr.data.settlement_cycle import (
    DEFAULT_SETTLEMENT_CYCLE_PATH,
    SettlementCycleError,
    load_settlement_cycle,
    parse_settlement_cycle,
)

ROOT = Path(__file__).resolve().parents[1]

# Hypothetical fixture, not the real config: T+1 from 2030-01-02.
FIXTURE = {
    "schema": "fintr_settlement_cycle_v1",
    "description": "fixture",
    "source": "fixture",
    "periods": [
        {"effective_from": "2000-01-01", "settlement_lag_server_dates": 2},
        {"effective_from": "2030-01-02", "settlement_lag_server_dates": 1},
    ],
}


def _with(**changes):
    doc = copy.deepcopy(FIXTURE)
    doc.update(changes)
    return doc


class LookupTests(unittest.TestCase):
    def test_lag_switches_on_effective_date(self):
        cycle = parse_settlement_cycle(FIXTURE)
        self.assertEqual(cycle.lag_on(date(2000, 1, 1)), 2)
        self.assertEqual(cycle.lag_on(date(2030, 1, 1)), 2)
        self.assertEqual(cycle.lag_on(date(2030, 1, 2)), 1)
        self.assertEqual(cycle.lag_on(date(2031, 6, 1)), 1)

    def test_as_of_before_first_period_fails(self):
        with self.assertRaises(SettlementCycleError):
            parse_settlement_cycle(FIXTURE).lag_on(date(1999, 12, 31))

    def test_real_config_is_t_plus_2(self):
        cycle = load_settlement_cycle(ROOT / DEFAULT_SETTLEMENT_CYCLE_PATH)
        self.assertEqual(cycle.lag_on(date(2026, 10, 14)), 2)


class ValidationTests(unittest.TestCase):
    def test_rejects_bad_documents(self):
        first, second = FIXTURE["periods"]
        cases = {
            "not_object": [],
            "bad_schema": _with(schema="other"),
            "unknown_top_key": _with(extra=1),
            "missing_source": {k: v for k, v in FIXTURE.items() if k != "source"},
            "empty_periods": _with(periods=[]),
            "unsorted": _with(periods=[second, first]),
            "duplicate": _with(periods=[first, dict(first)]),
            "same_date_other_lag": _with(periods=[first, {**first, "settlement_lag_server_dates": 1}]),
            "zero_lag": _with(periods=[{**first, "settlement_lag_server_dates": 0}]),
            "negative_lag": _with(periods=[{**first, "settlement_lag_server_dates": -1}]),
            "bool_lag": _with(periods=[{**first, "settlement_lag_server_dates": True}]),
            "string_lag": _with(periods=[{**first, "settlement_lag_server_dates": "2"}]),
            "unknown_period_key": _with(periods=[{**first, "note": "x"}]),
            "missing_period_key": _with(periods=[{"effective_from": "2000-01-01"}]),
            "bad_date": _with(periods=[{**first, "effective_from": "2000-13-01"}]),
            "non_iso_date": _with(periods=[{**first, "effective_from": "20000101"}]),
            "period_not_object": _with(periods=[1]),
        }
        for name, doc in cases.items():
            with self.subTest(name):
                with self.assertRaises(SettlementCycleError):
                    parse_settlement_cycle(doc)

    def test_unreadable_file_fails(self):
        with self.assertRaises(SettlementCycleError):
            load_settlement_cycle(ROOT / "configs" / "market" / "missing.json")

    def test_real_config_is_valid_json(self):
        json.loads((ROOT / DEFAULT_SETTLEMENT_CYCLE_PATH).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
