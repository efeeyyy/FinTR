"""Offline tests for explicit, bounded live-context pilot capture."""

from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import AsyncMock, patch

from fintr.collector.client import replay_frames
from fintr.collector.evidence import build_live_collection_evidence
from fintr.collector.models import SendObservation
from fintr.data.models import (
    SentAKDRequestProvenance,
    SentCustodianRequestProvenance,
    SentCustodyDistributionRequestProvenance,
    SentCustodyStatsRequestProvenance,
)
from fintr.evaluation import live_capture
from fintr.inference.live_context import LiveContextLimits, build_live_analysis_context


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "market"
DAY = date(2026, 9, 23)
LIMITS = LiveContextLimits(
    akd_rows=2,
    custodian_rows=2,
    distribution_rows=2,
    stats_points_per_series=2,
)
SMOKE_SPEC = importlib.util.spec_from_file_location(
    "live_collector_smoke", ROOT / "scripts" / "live_collector_smoke.py"
)
smoke = importlib.util.module_from_spec(SMOKE_SPEC)
SMOKE_SPEC.loader.exec_module(smoke)


async def _pilot_inputs(*, brokerage_title_override: str | None = None):
    frames = []
    for name in (
        "brokerages_data_live.json",
        "akd_data_partial.json",
        "custodian_data_live.json",
        "custody_distribution_data_live.json",
        "custody_stats_data_live.json",
    ):
        data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        if name == "brokerages_data_live.json" and brokerage_title_override is not None:
            data["data"]["data"] = json.dumps([{
                "code": "TEST",
                "title": brokerage_title_override,
                "short_title": "Synthetic",
            }])
        elif name == "custodian_data_live.json":
            data["data"]["data"]["date"] = DAY.isoformat()
        frames.append(json.dumps(data))
    results = tuple(await replay_frames(frames))
    provenance = {
        "akd_data": SentAKDRequestProvenance("AKFIS", DAY, DAY),
        "custodian_data": SentCustodianRequestProvenance("AKFIS", DAY),
        "custody_distribution_data": SentCustodyDistributionRequestProvenance("AKFIS"),
        "custody_stats_data": SentCustodyStatsRequestProvenance("AKFIS", True),
    }
    bound_results = tuple(
        replace(
            result,
            normalized=replace(
                result.normalized,
                request_provenance=provenance[result.event_type],
            ),
            normalization_pending=False,
        ) if result.event_type in provenance else result
        for result in results
    )
    evidence = build_live_collection_evidence(bound_results)
    generated_at = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)
    context = build_live_analysis_context(
        evidence, generated_at=generated_at, limits=LIMITS,
    )
    requests = (
        smoke.build_brokerages_request(),
        smoke.build_akd_request("AKFIS", DAY, DAY),
        smoke.build_custodian_request("AKFIS", DAY),
        smoke.build_custody_distribution_request("AKFIS"),
        smoke.build_custody_stats_request("AKFIS"),
    )
    return evidence, context, requests, bound_results


class LivePilotCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.evidence, self.context, self.requests, self.results = await _pilot_inputs()

    def _smoke_args(self, *, capture: bool, day: str = "2026-09-23"):
        argv = [
            "--allow-session-takeover",
            "--symbol", "AKFIS",
            "--start-date", day,
            "--end-date", day,
            "--custody-date", day,
            "--require-all-responses",
            "--validate-evidence-bundle",
            "--validate-live-context",
            "--akd-context-rows", "2",
            "--custodian-context-rows", "2",
            "--distribution-context-rows", "2",
            "--stats-context-points", "2",
        ]
        if capture:
            argv.append("--capture-pilot-context")
        return smoke._parser(), argv

    async def _run_smoke(self, *, capture: bool, day: str = "2026-09-23"):
        parser, argv = self._smoke_args(capture=capture, day=day)
        args = parser.parse_args(argv)
        output = io.StringIO()
        with (
            patch.object(smoke, "WebSocketCollector") as collector,
            patch.object(smoke, "InteractiveSecretProvider"),
            redirect_stdout(output),
        ):
            async def collect(requests):
                observer = collector.call_args.kwargs["send_observer"]
                for request in requests:
                    observer(SendObservation(1, request.request_type, "sent"))
                return self.results

            collector.return_value.collect = AsyncMock(side_effect=collect)
            status = await smoke._run(args, parser)
        return status, output.getvalue(), collector

    async def test_default_smoke_remains_non_persistent(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            live_capture, "DATA_PILOT_DIR", Path(temporary),
        ):
            status, output, _ = await self._run_smoke(capture=False)
            self.assertEqual(status, 0)
            self.assertIn("Persistent storage: disabled", output)
            self.assertFalse((Path(temporary) / live_capture.PILOT_CAPTURE_FILENAME).exists())

    async def test_opt_in_pilot_smoke_writes_only_after_context_validation(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            live_capture, "DATA_PILOT_DIR", Path(temporary),
        ):
            status, output, _ = await self._run_smoke(capture=True)
            target = Path(temporary) / live_capture.PILOT_CAPTURE_FILENAME
            self.assertEqual(status, 0)
            self.assertIn("In-memory LLM context and prompt: validated", output)
            self.assertIn("Pilot context captured:", output)
            self.assertIn("classification=pilot/development", output)
            self.assertTrue(target.is_file())

    async def test_retired_eval_flag_fails_before_collector_construction(self):
        parser = smoke._parser()
        args = parser.parse_args(("--allow-session-takeover", "--capture-eval-context"))
        error_output = io.StringIO()
        with patch.object(smoke, "WebSocketCollector") as collector:
            with (
                redirect_stdout(io.StringIO()),
                redirect_stderr(error_output),
                self.assertRaises(SystemExit) as error,
            ):
                await smoke._run(args, parser)
        self.assertEqual(error.exception.code, 2)
        self.assertIn("--capture-eval-context is retired", error_output.getvalue())
        self.assertIn("not held-out evaluation data", error_output.getvalue())
        collector.assert_not_called()

    async def test_cli_scope_guard_rejects_other_date_before_collector(self):
        parser, argv = self._smoke_args(capture=True, day="2026-09-22")
        args = parser.parse_args(argv)
        with patch.object(smoke, "WebSocketCollector") as collector:
            with (
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                await smoke._run(args, parser)
        self.assertEqual(error.exception.code, 2)
        collector.assert_not_called()

    async def test_capture_metadata_checksum_and_sensitive_data_exclusion(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            live_capture, "DATA_PILOT_DIR", Path(temporary),
        ):
            saved = live_capture.capture_live_pilot_context(
                self.evidence,
                self.context,
                self.requests,
                LIMITS,
                captured_at=datetime(2026, 9, 24, 12, 30, tzinfo=timezone.utc),
            )
            artifact = json.loads(saved.read_text(encoding="utf-8"))
            canonical_context = json.dumps(
                artifact["context"], ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.assertEqual(
                artifact["context_sha256"], hashlib.sha256(canonical_context).hexdigest(),
            )
            self.assertEqual(artifact["captured_at"], "2026-09-24T12:30:00+00:00")
            self.assertEqual(artifact["source"]["event_families"], list(live_capture.EVENT_FAMILIES))
            self.assertEqual(artifact["context_schema_version"], "live_evidence_context_v0")
            self.assertEqual(artifact["context_limits"]["akd_rows"], LIMITS.akd_rows)
            self.assertEqual(artifact["permission_provenance"]["asserted_on"], "2026-09-24")
            self.assertFalse(artifact["permission_provenance"]["independent_license_verification"])
            self.assertEqual(artifact["artifact_schema_version"], 2)
            self.assertEqual(artifact["classification"], "pilot/development")
            self.assertEqual(artifact["split"], "pilot")
            self.assertFalse(artifact["heldout_eligible"])
            self.assertFalse(artifact["training_allowed"])
            self.assertFalse(artifact["evaluation_allowed"])
            self.assertEqual(artifact["retention"], "manual_local_management")
            serialized = saved.read_text(encoding="utf-8").casefold()
            for excluded in (
                "initdata", "https://", "wss://", "cookie", "request_id",
                "synthetic-akd-request", "synthetic-custodian-request",
                "synthetic-distribution-request", "raw_model_output",
            ):
                self.assertNotIn(excluded, serialized)

    async def test_scope_guard_rejects_mismatched_request(self):
        wrong_requests = list(self.requests)
        wrong_requests[1] = replace(
            wrong_requests[1],
            data={"company": "OTHER", "start": DAY.isoformat(), "end": DAY.isoformat()},
        )
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            live_capture, "DATA_PILOT_DIR", Path(temporary),
        ):
            with self.assertRaisesRegex(
                live_capture.LivePilotCaptureError, "^pilot_scope_mismatch$",
            ):
                live_capture.capture_live_pilot_context(
                    self.evidence, self.context, wrong_requests, LIMITS,
                )
            self.assertEqual(list(Path(temporary).iterdir()), [])

    async def test_provider_url_initdata_text_is_rejected_without_writing(self):
        marker = "https://example.invalid/?initData=DO_NOT_STORE&query_id=DO_NOT_STORE"
        evidence, context, requests, _ = await _pilot_inputs(
            brokerage_title_override=marker,
        )
        self.assertIn(marker, context.to_json())
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            live_capture, "DATA_PILOT_DIR", Path(temporary),
        ):
            with self.assertRaisesRegex(
                live_capture.LivePilotCaptureError, "^sensitive_context_value$",
            ) as raised:
                live_capture.capture_live_pilot_context(
                    evidence, context, requests, LIMITS,
                )
            self.assertNotIn("DO_NOT_STORE", str(raised.exception))
            self.assertEqual(list(Path(temporary).iterdir()), [])

    async def test_existing_capture_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            live_capture, "DATA_PILOT_DIR", Path(temporary),
        ):
            saved = live_capture.capture_live_pilot_context(
                self.evidence, self.context, self.requests, LIMITS,
            )
            original = saved.read_bytes()
            with self.assertRaisesRegex(
                live_capture.LivePilotCaptureError, "^capture_already_exists$",
            ):
                live_capture.capture_live_pilot_context(
                    self.evidence, self.context, self.requests, LIMITS,
                )
            self.assertEqual(saved.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
