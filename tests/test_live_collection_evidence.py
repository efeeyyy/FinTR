"""One-collection evidence view; all fixtures and requests are synthetic."""

from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fintr.collector.client import replay_frames
from fintr.collector.evidence import (
    LiveEvidenceError,
    build_live_collection_evidence,
)
from fintr.data.brokerage_index import BrokerageResolutionStatus
from fintr.data.models import (
    SentAKDRequestProvenance,
    SentCustodianRequestProvenance,
    SentCustodyDistributionRequestProvenance,
    SentCustodyStatsRequestProvenance,
)


FIXTURES = Path(__file__).parent / "fixtures" / "market"
DAY = date(2026, 9, 22)


def _brokerages_frame() -> str:
    frame = json.loads((FIXTURES / "brokerages_data_live.json").read_text(encoding="utf-8"))
    frame["data"]["data"] = json.dumps([
        {"code": "TEST", "title": "Synthetic AKD Broker", "short_title": "AKD Broker"},
        {"code": "SYN1", "title": "Synthetic Custodian", "short_title": "Custodian"},
    ])
    return json.dumps(frame)


async def _bound_results():
    frames = (
        '{"type":"welcome"}',
        _brokerages_frame(),
        *((FIXTURES / name).read_text(encoding="utf-8") for name in (
            "akd_data_partial.json",
            "custodian_data_live.json",
            "custody_distribution_data_live.json",
            "custody_stats_data_live.json",
        )),
    )
    with patch("fintr.collector.client.connect", side_effect=AssertionError("network forbidden")):
        results = list(await replay_frames(frames))
    provenance = {
        "akd_data": SentAKDRequestProvenance("AKFIS", DAY, DAY),
        "custodian_data": SentCustodianRequestProvenance("AKFIS", DAY),
        "custody_distribution_data": SentCustodyDistributionRequestProvenance("AKFIS"),
        "custody_stats_data": SentCustodyStatsRequestProvenance("AKFIS", True),
    }
    return tuple(
        replace(
            result,
            normalized=replace(result.normalized, request_provenance=provenance[result.event_type]),
            normalization_pending=False,
        ) if result.event_type in provenance else result
        for result in results
    )


class LiveCollectionEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_view_keeps_sources_scopes_and_unmatched_labels(self):
        results = await _bound_results()
        view = build_live_collection_evidence(results)

        self.assertEqual(view.symbol, "AKFIS")
        self.assertIs(view.brokerages, results[1].normalized)
        self.assertIs(view.akd.source, results[2].normalized)
        self.assertIs(view.custodian.source, results[3].normalized)
        self.assertIs(view.custody_distribution, results[4].normalized)
        self.assertIs(view.custody_stats, results[5].normalized)
        self.assertEqual(view.custodian.source.date, DAY)
        self.assertEqual(view.akd.source.request_provenance.start, DAY)
        self.assertEqual(len(view.custody_distribution.periods), 6)
        self.assertTrue(view.custody_stats.request_provenance.history)
        self.assertIs(view.akd.rows[0].resolution.status, BrokerageResolutionStatus.RESOLVED)
        self.assertIs(view.custodian.rows[0].resolution.status, BrokerageResolutionStatus.RESOLVED)
        self.assertIs(view.custodian.rows[1].resolution.status, BrokerageResolutionStatus.UNRESOLVED)
        self.assertEqual(view.custody_distribution.rows[0].code, "TEST_A")
        self.assertNotIn("Synthetic", repr(view))
        self.assertNotIn("123.456", repr(view))

    async def test_missing_duplicate_and_pending_responses_fail_closed(self):
        results = await _bound_results()
        for candidate, reason in (
            (results[:-1], "missing_response"),
            (results + (results[2],), "duplicate_response"),
            (results[:2] + (replace(results[2], normalization_pending=True),) + results[3:],
             "invalid_response"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(LiveEvidenceError, f"^{reason}$"):
                build_live_collection_evidence(candidate)

    async def test_missing_or_forged_local_provenance_fails_closed(self):
        results = await _bound_results()
        for provenance in (
            None,
            replace(results[2].normalized.request_provenance, origin="other"),
        ):
            changed = results[:2] + (
                replace(results[2], normalized=replace(
                    results[2].normalized, request_provenance=provenance,
                )),
            ) + results[3:]
            with self.assertRaisesRegex(LiveEvidenceError, "^missing_local_send_provenance$"):
                build_live_collection_evidence(changed)

    async def test_symbol_or_response_date_conflict_fails_closed(self):
        results = await _bound_results()
        other_symbol = results[:4] + (
            replace(results[4], normalized=replace(
                results[4].normalized,
                request_provenance=SentCustodyDistributionRequestProvenance("OTHER"),
            )),
        ) + results[5:]
        with self.assertRaisesRegex(LiveEvidenceError, "^symbol_mismatch$"):
            build_live_collection_evidence(other_symbol)

        other_date = results[:3] + (
            replace(results[3], normalized=replace(
                results[3].normalized,
                request_provenance=SentCustodianRequestProvenance("AKFIS", date(2026, 9, 23)),
            )),
        ) + results[4:]
        with self.assertRaisesRegex(LiveEvidenceError, "^custodian_date_mismatch$"):
            build_live_collection_evidence(other_date)

    async def test_akd_range_stays_distinct_from_daily_custody_date(self):
        results = await _bound_results()
        ranged = results[:2] + (
            replace(results[2], normalized=replace(
                results[2].normalized,
                request_provenance=SentAKDRequestProvenance("AKFIS", date(2026, 9, 14), DAY),
            )),
        ) + results[3:]
        view = build_live_collection_evidence(ranged)
        self.assertEqual(view.akd.source.request_provenance.start, date(2026, 9, 14))
        self.assertEqual(view.custodian.source.date, DAY)


if __name__ == "__main__":
    unittest.main()
