"""Offline preflight for the observed live-contract family, without a socket."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fintr.collector.client import replay_frames
from fintr.data import (
    BrokerageResolutionStatus as Status,
    LiveBrokerageIndex,
    resolve_live_akd_brokerages,
    resolve_live_custodian_brokerages,
    resolve_live_custodian_diff_brokerages,
    resolve_live_custody_analysis_brokerages,
)
from fintr.data.models import (
    BrokerageDirectory,
    LiveAKDResponse,
    LiveBrokeragesResponse,
    LiveCustodianDiffResponse,
    LiveCustodianResponse,
    LiveCustodyAnalysisResponse,
    LiveCustodyDistributionResponse,
    LiveCustodyStatsResponse,
)


FIXTURES = Path(__file__).parent / "fixtures" / "market"
LIVE_FIXTURES = (
    ("akd_data", "akd_data_partial.json", LiveAKDResponse),
    ("custodian_data", "custodian_data_live.json", LiveCustodianResponse),
    ("custodian_diff_data", "custodian_diff_data_live.json", LiveCustodianDiffResponse),
    ("custody_analysis_data", "custody_analysis_data_live.json", LiveCustodyAnalysisResponse),
    ("custody_distribution_data", "custody_distribution_data_live.json", LiveCustodyDistributionResponse),
    ("custody_stats_data", "custody_stats_data_live.json", LiveCustodyStatsResponse),
)


def brokerage_frame(*rows: tuple[str, str, str]) -> str:
    fixture = json.loads((FIXTURES / "brokerages_data_live.json").read_text(encoding="utf-8"))
    fixture["data"]["data"] = json.dumps([
        {"code": code, "title": title, "short_title": short_title}
        for code, title, short_title in rows
    ])
    return json.dumps(fixture)


class OfflineLivePreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_observed_live_families_replay_then_resolve_without_network(self) -> None:
        frames = (
            '{"type":"welcome"}',
            brokerage_frame(
                ("TEST", "Synthetic AKD Broker", "AKD Broker"),
                ("SYN1", "Synthetic Custodian", "Custodian"),
            ),
            *((FIXTURES / filename).read_text(encoding="utf-8")
              for _, filename, _ in LIVE_FIXTURES),
        )
        with patch("fintr.collector.client.connect", side_effect=AssertionError("network forbidden")):
            results = await replay_frames(frames)

        self.assertEqual(len(results), 2 + len(LIVE_FIXTURES))
        self.assertFalse(results[0].known)
        self.assertEqual(results[0].event_type, "welcome")
        self.assertIsInstance(results[1].normalized, LiveBrokeragesResponse)
        self.assertFalse(results[1].normalization_pending)
        for result, (event_type, _, model) in zip(results[2:], LIVE_FIXTURES, strict=True):
            with self.subTest(event_type=event_type):
                self.assertEqual(result.event_type, event_type)
                self.assertTrue(result.known)
                self.assertTrue(result.normalization_pending)
                self.assertIsInstance(result.normalized, model)
                self.assertIsNone(result.normalized.request_provenance)

        index = LiveBrokerageIndex.from_response(results[1].normalized)
        by_type = {result.event_type: result.normalized for result in results[2:]}
        akd = resolve_live_akd_brokerages(by_type["akd_data"], index)
        daily = resolve_live_custodian_brokerages(by_type["custodian_data"], index)
        diff = resolve_live_custodian_diff_brokerages(by_type["custodian_diff_data"], index)
        analysis = resolve_live_custody_analysis_brokerages(
            by_type["custody_analysis_data"], index,
        )

        self.assertIs(akd.rows[0].resolution.status, Status.RESOLVED)
        self.assertEqual(akd.rows[0].resolution.label.short_title, "AKD Broker")
        self.assertEqual(
            [row.resolution.status for row in daily.rows],
            [Status.RESOLVED, Status.UNRESOLVED],
        )
        self.assertEqual(
            [row.resolution.status for row in diff.rows],
            [Status.RESOLVED, Status.UNRESOLVED],
        )
        self.assertIs(analysis.akd[0].rows[0].resolution.status, Status.RESOLVED)
        self.assertIs(analysis.custody[0].rows[0].resolution.status, Status.RESOLVED)
        self.assertEqual(analysis.akd[1].rows, ())
        self.assertEqual(analysis.custody[1].rows, ())
        self.assertIs(akd.source, by_type["akd_data"])
        self.assertIs(daily.source, by_type["custodian_data"])
        self.assertIs(diff.source, by_type["custodian_diff_data"])
        self.assertIs(analysis.source, by_type["custody_analysis_data"])

        distribution = by_type["custody_distribution_data"]
        self.assertEqual([row.code for row in distribution.rows], ["TEST_A", "TEST_B"])
        self.assertEqual(len(distribution.periods), 6)
        self.assertIsNone(distribution.rows[1].values[-1])
        stats = by_type["custody_stats_data"]
        self.assertTrue(stats.cached)
        self.assertIsNone(stats.request_id)

    async def test_legacy_directory_cannot_be_used_as_live_name_evidence(self) -> None:
        frame = (FIXTURES / "brokerages_data.json").read_text(encoding="utf-8")
        result, = await replay_frames((frame,))
        self.assertIsInstance(result.normalized, BrokerageDirectory)
        with self.assertRaises(TypeError):
            LiveBrokerageIndex.from_response(result.normalized)

    async def test_conflicting_live_labels_fail_closed_across_all_views(self) -> None:
        frames = (
            brokerage_frame(
                ("SYN1", "First Synthetic", "First"),
                ("SYN1", "Second Synthetic", "Second"),
            ),
            *((FIXTURES / filename).read_text(encoding="utf-8")
              for filename in (
                  "custodian_data_live.json",
                  "custodian_diff_data_live.json",
                  "custody_analysis_data_live.json",
              )),
        )
        results = await replay_frames(frames)
        index = LiveBrokerageIndex.from_response(results[0].normalized)
        daily = resolve_live_custodian_brokerages(results[1].normalized, index)
        diff = resolve_live_custodian_diff_brokerages(results[2].normalized, index)
        analysis = resolve_live_custody_analysis_brokerages(results[3].normalized, index)
        for row in (
            daily.rows[0], diff.rows[0], analysis.akd[0].rows[0],
            analysis.custody[0].rows[0],
        ):
            self.assertIs(row.resolution.status, Status.AMBIGUOUS)
            self.assertIsNone(row.resolution.label)


if __name__ == "__main__":
    unittest.main()
