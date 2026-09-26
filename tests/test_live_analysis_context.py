"""One-collection live context from synthetic protocol fixtures only."""

from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fintr.collector.client import replay_frames
from fintr.collector.evidence import build_live_collection_evidence
from fintr.data.models import (
    SentAKDRequestProvenance,
    SentCustodianRequestProvenance,
    SentCustodyDistributionRequestProvenance,
    SentCustodyStatsRequestProvenance,
)
from fintr.inference.live_context import LiveContextLimits, build_live_analysis_context
from fintr.inference.prompts import build_live_analysis_prompt


FIXTURES = Path(__file__).parent / "fixtures" / "market"
DAY = date(2026, 9, 22)
LIMITS = LiveContextLimits(
    akd_rows=1, custodian_rows=1, distribution_rows=1, stats_points_per_series=1,
)


async def _evidence():
    brokerages = json.loads((FIXTURES / "brokerages_data_live.json").read_text(encoding="utf-8"))
    brokerages["data"]["data"] = json.dumps([
        {"code": "TEST", "title": "Synthetic Broker", "short_title": "Broker"},
    ])
    frames = (
        json.dumps(brokerages),
        *((FIXTURES / name).read_text(encoding="utf-8") for name in (
            "akd_data_partial.json",
            "custodian_data_live.json",
            "custody_distribution_data_live.json",
            "custody_stats_data_live.json",
        )),
    )
    with patch("fintr.collector.client.connect", side_effect=AssertionError("network forbidden")):
        results = tuple(await replay_frames(frames))
    provenance = {
        "akd_data": SentAKDRequestProvenance("AKFIS", DAY, DAY),
        "custodian_data": SentCustodianRequestProvenance("AKFIS", DAY),
        "custody_distribution_data": SentCustodyDistributionRequestProvenance("AKFIS"),
        "custody_stats_data": SentCustodyStatsRequestProvenance("AKFIS", True),
    }
    bound = tuple(
        replace(result, normalized=replace(
            result.normalized, request_provenance=provenance[result.event_type],
        ), normalization_pending=False) if result.event_type in provenance else result
        for result in results
    )
    return build_live_collection_evidence(bound)


class LiveAnalysisContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_distinct_scopes_values_and_omissions_are_visible(self):
        evidence = await _evidence()
        context = build_live_analysis_context(
            evidence, generated_at=datetime(2026, 9, 24, tzinfo=timezone.utc), limits=LIMITS,
        )
        data = context.to_dict()
        self.assertEqual(data["symbol"], "AKFIS")
        self.assertEqual(data["akd"]["local_sent_request"]["start"], DAY.isoformat())
        self.assertEqual(data["akd"]["server_as_of"], "12:34:56")
        self.assertEqual(data["akd"]["rows"][0]["net"]["percentage_as_received"], "0.25")
        self.assertEqual(data["akd"]["rows"][0]["brokerage_label"]["title"], "Synthetic Broker")
        self.assertEqual(data["custodian"]["server_date"], DAY.isoformat())
        self.assertTrue(data["custodian"]["truncated"])
        self.assertEqual(data["custody_distribution"]["periods_from_server"][0], "2025-06-30")
        self.assertTrue(data["custody_distribution"]["code_is_not_brokerage_code"])
        self.assertTrue(data["custody_stats"]["retail_percentage"]["truncated"])
        self.assertEqual(data["custody_stats"]["retail_percentage"]["points"][0]["value"],
                         "12.34567890123456789")
        self.assertEqual(data["custody_stats"]["retail_percentage"]["provider_format"][1],
                         {"key": "suffix", "value": "%"})
        data["akd"]["rows"].clear()
        self.assertEqual(len(context.to_dict()["akd"]["rows"]), 1)

    async def test_prompt_excludes_request_ids_and_control_frames(self):
        context = build_live_analysis_context(
            await _evidence(), generated_at=datetime.now(timezone.utc), limits=LIMITS,
        )
        prompt = build_live_analysis_prompt(context)
        self.assertIn("kimlik", prompt.system.lower())
        self.assertIn("AKD", prompt.user)
        self.assertGreaterEqual(len(prompt.facts), 3)
        for secret_or_control in (
            "synthetic-akd-request", "synthetic-distribution-request",
            "init_data", "welcome", "favorite_update", "request_id",
            "0.25", "2026-09-22", "Synthetic Broker", "AKFIS",
        ):
            self.assertNotIn(secret_or_control, prompt.user)

    async def test_invalid_limits_and_naive_analysis_time_fail(self):
        evidence = await _evidence()
        with self.assertRaisesRegex(ValueError, "^invalid_live_context_limit$"):
            LiveContextLimits(0, 1, 1, 1)
        with self.assertRaisesRegex(ValueError, "^generated_at_requires_timezone$"):
            build_live_analysis_context(
                evidence, generated_at=datetime(2026, 9, 24), limits=LIMITS,
            )


if __name__ == "__main__":
    unittest.main()
