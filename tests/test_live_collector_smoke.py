"""Offline smoke acceptance tests: no authentication and no live connection."""

import importlib.util
import hashlib
import io
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fintr.collector.client import replay_frames
from fintr.collector.models import AttemptDiagnostic, CollectorRequest, DispatchResult, SendObservation
from fintr.data.models import (
    LiveAKDResponse,
    LiveCustodianResponse,
    LiveCustodianDiffResponse,
    LiveCustodyAnalysisResponse,
    LiveCustodyDistributionResponse,
    LiveCustodyStatsResponse,
    LiveBrokeragesResponse,
    SentAKDRequestProvenance,
    SentCustodianRequestProvenance,
    SentCustodianDiffRequestProvenance,
    SentCustodyAnalysisRequestProvenance,
    SentCustodyDistributionRequestProvenance,
    SentCustodyStatsRequestProvenance,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "live_collector_smoke", ROOT / "scripts" / "live_collector_smoke.py"
)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def fixture_frame(event_type: str) -> str:
    return (ROOT / "tests" / "fixtures" / "market" / f"{event_type}.json").read_text(
        encoding="utf-8"
    )


class LiveSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def full_evidence_case(self):
        frames = tuple(fixture_frame(name) for name in (
            "brokerages_data_live", "akd_data_partial", "custodian_data_live",
            "custody_distribution_data_live", "custody_stats_data_live",
        ))
        results = list(await replay_frames(frames))
        day = date(2026, 9, 22)
        provenance = (
            None,
            SentAKDRequestProvenance("AKFIS", day, day),
            SentCustodianRequestProvenance("AKFIS", day),
            SentCustodyDistributionRequestProvenance("AKFIS"),
            SentCustodyStatsRequestProvenance("AKFIS", True),
        )
        for index in range(1, len(results)):
            results[index] = replace(
                results[index],
                normalized=replace(results[index].normalized, request_provenance=provenance[index]),
                normalization_pending=False,
            )
        requests = (
            smoke.build_brokerages_request(),
            smoke.build_akd_request("AKFIS", day, day),
            smoke.build_custodian_request("AKFIS", day),
            smoke.build_custody_distribution_request("AKFIS"),
            smoke.build_custody_stats_request("AKFIS"),
        )
        return tuple(results), requests

    async def period_analysis_case(
        self, *, end: date = date(2026, 9, 23), diff_end: date | None = None,
        secret_source: str | None = None,
    ):
        brokerages = fixture_frame("brokerages_data_live")
        akd = fixture_frame("akd_data_partial")
        analysis = fixture_frame("custody_analysis_data_live")
        custody_diff = fixture_frame("custodian_diff_data_live").replace(
            '"end_date": "2026-09-22"',
            f'"end_date": "{(diff_end or end).isoformat()}"',
        )
        if secret_source is not None:
            custody_diff = custody_diff.replace(
                '"source": "pg"', f'"source": {json.dumps(secret_source)}',
            )
        requests = smoke._period_analysis_requests(
            "AKFIS", date(2026, 9, 14), end,
        )
        results = list(await replay_frames((brokerages, akd, analysis, custody_diff)))
        provenance = (
            None,
            SentAKDRequestProvenance("AKFIS", date(2026, 9, 14), end),
            SentCustodyAnalysisRequestProvenance(
                "AKFIS", date(2026, 9, 14), end,
            ),
            SentCustodianDiffRequestProvenance(
                "AKFIS", date(2026, 9, 14), end,
            ),
        )
        for index in range(1, len(results)):
            results[index] = replace(
                results[index],
                normalized=replace(
                    results[index].normalized,
                    request_provenance=provenance[index],
                ),
                normalization_pending=False,
            )
        return tuple(results), requests

    async def run_smoke(
        self, results, *, requests=None, argv=("--allow-session-takeover",),
        sent_request_types=(), extra_send_observations=(),
    ):
        parser = smoke._parser()
        args = parser.parse_args(argv)
        output = io.StringIO()
        with (
            patch.object(smoke, "WebSocketCollector") as collector,
            patch.object(smoke, "InteractiveSecretProvider") as provider,
            patch.object(
                smoke, "_requests",
                return_value=requests or (CollectorRequest("get_brokerages"),),
            ),
            redirect_stdout(output),
        ):
            async def collect_with_observations(_requests):
                observer = collector.call_args.kwargs["send_observer"]
                for request_type in sent_request_types:
                    observer(SendObservation(1, request_type, "sent"))
                for observation in extra_send_observations:
                    observer(observation)
                return results

            collector.return_value.collect = AsyncMock(side_effect=collect_with_observations)
            code = await smoke._run(args, parser)
            provider.return_value.get_init_data.assert_not_called()
            collector.return_value.collect.assert_awaited_once()
            config = collector.call_args.kwargs["config"]
        return code, output.getvalue(), config

    async def test_session_takeover_flag_is_required_before_secret_or_connection(self):
        parser = smoke._parser()
        args = parser.parse_args(())
        error = io.StringIO()
        with (
            patch.object(smoke, "WebSocketCollector") as collector,
            patch.object(smoke, "InteractiveSecretProvider") as provider,
            patch.object(smoke, "_requests") as requests,
            redirect_stderr(error),
        ):
            with self.assertRaises(SystemExit) as captured:
                await smoke._run(args, parser)

        self.assertEqual(captured.exception.code, 2)
        self.assertIn("allows only one active session", error.getvalue())
        self.assertIn("--allow-session-takeover", error.getvalue())
        provider.assert_not_called()
        collector.assert_not_called()
        requests.assert_not_called()

    async def test_takeover_warning_is_visible_when_flag_is_present(self):
        _, output, _ = await self.run_smoke(())
        self.assertIn("WARNING", output)
        self.assertIn("may close the active Telegram Mini App session", output)

    async def test_only_unknown_controls_fail_without_payload_output(self):
        results = await replay_frames((
            '{"type":"welcome","data":{"opaque":"DO_NOT_PRINT"}}',
            '{"type":"hb"}',
        ))
        code, output, _ = await self.run_smoke(results)
        self.assertEqual(code, 1)
        self.assertIn("Event: welcome (unknown)", output)
        self.assertIn("Event: hb (unknown)", output)
        self.assertNotIn("DO_NOT_PRINT", output)
        self.assertNotIn("Smoke completed", output)

    async def test_control_then_requested_known_response_succeeds(self):
        results = await replay_frames((
            '{"type":"welcome"}', '{"type":"hb"}',
            fixture_frame("brokerages_data_live"),
        ))
        code, output, _ = await self.run_smoke(results)
        self.assertEqual(code, 0)
        self.assertIn("Smoke completed: requested response received: brokerages_data", output)

    async def test_unrequested_known_response_fails(self):
        results = await replay_frames((fixture_frame("depth_update"),))
        code, output, _ = await self.run_smoke(results)
        self.assertEqual(code, 1)
        self.assertIn("Event: depth_update (known)", output)
        self.assertIn("no requested response", output)

    async def test_empty_results_fail(self):
        code, output, _ = await self.run_smoke(())
        self.assertEqual(code, 1)
        self.assertIn("Smoke failed", output)

    async def test_existing_offline_request_response_pairs(self):
        pairs = (
            ("get_brokerages", "brokerages_data_live"),
        )
        for request_type, event_type in pairs:
            with self.subTest(request_type=request_type):
                results = await replay_frames((fixture_frame(event_type),))
                code, _, _ = await self.run_smoke(
                    results, requests=(CollectorRequest(request_type),)
                )
                self.assertEqual(code, 0)

    async def test_one_of_multiple_requested_responses_is_sufficient(self):
        results = await replay_frames((fixture_frame("brokerages_data_live"),))
        code, _, _ = await self.run_smoke(results, requests=(
            CollectorRequest("get_brokerages"), CollectorRequest("get_akd"),
        ))
        self.assertEqual(code, 0)

    async def test_require_all_rejects_missing_response_even_when_all_sent(self):
        results = await replay_frames((fixture_frame("brokerages_data_live"),))
        code, output, _ = await self.run_smoke(
            results,
            requests=(CollectorRequest("get_brokerages"), CollectorRequest("get_akd")),
            argv=("--allow-session-takeover", "--require-all-responses"),
            sent_request_types=("get_brokerages", "get_akd"),
        )
        self.assertEqual(code, 1)
        self.assertIn("responses not received: akd_data", output)
        self.assertNotIn("Smoke completed", output)

    async def test_require_all_rejects_response_without_observed_send(self):
        results = await replay_frames((fixture_frame("brokerages_data_live"),))
        code, output, _ = await self.run_smoke(
            results,
            argv=("--allow-session-takeover", "--require-all-responses"),
        )
        self.assertEqual(code, 1)
        self.assertIn("requests not sent: get_brokerages", output)

    async def test_require_all_accepts_sent_and_normalized_response(self):
        results = await replay_frames((fixture_frame("brokerages_data_live"),))
        code, output, _ = await self.run_smoke(
            results,
            argv=("--allow-session-takeover", "--require-all-responses"),
            sent_request_types=("get_brokerages",),
        )
        self.assertEqual(code, 0)
        self.assertIn("Expected responses (all): brokerages_data", output)
        self.assertIn("Smoke completed: requested response received: brokerages_data", output)

    async def test_require_all_does_not_count_send_from_previous_attempt(self):
        results = await replay_frames((fixture_frame("brokerages_data_live"),))
        code, output, _ = await self.run_smoke(
            results,
            argv=("--allow-session-takeover", "--require-all-responses"),
            sent_request_types=("get_brokerages",),
            extra_send_observations=(
                SendObservation(2, "get_brokerages", "rate_limited"),
            ),
        )
        self.assertEqual(code, 1)
        self.assertIn("requests not sent: get_brokerages", output)

    async def test_evidence_flag_requires_full_strict_profile_before_collect(self):
        for argv in (
            ("--allow-session-takeover", "--validate-evidence-bundle"),
            ("--allow-session-takeover", "--require-all-responses", "--validate-evidence-bundle"),
        ):
            parser = smoke._parser()
            args = parser.parse_args(argv)
            with (
                self.subTest(argv=argv),
                patch.object(smoke, "WebSocketCollector") as collector,
                patch.object(smoke, "InteractiveSecretProvider") as provider,
                redirect_stderr(io.StringIO()),
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    await smoke._run(args, parser)
                collector.assert_not_called()
                provider.assert_not_called()

    async def test_evidence_flag_builds_in_memory_view_after_five_responses(self):
        results, requests = await self.full_evidence_case()
        code, output, _ = await self.run_smoke(
            results,
            requests=requests,
            argv=("--allow-session-takeover", "--require-all-responses", "--validate-evidence-bundle"),
            sent_request_types=tuple(request.request_type for request in requests),
        )
        self.assertEqual(code, 0)
        self.assertIn("In-memory evidence view: validated", output)
        self.assertIn("Smoke completed", output)
        self.assertNotIn("synthetic-akd-request", output)

    async def test_live_context_flag_requires_evidence_and_explicit_limits(self):
        for argv in (
            ("--allow-session-takeover", "--validate-live-context"),
            ("--allow-session-takeover", "--require-all-responses",
             "--validate-evidence-bundle", "--validate-live-context"),
            ("--allow-session-takeover", "--akd-context-rows", "1"),
        ):
            parser = smoke._parser()
            args = parser.parse_args(argv)
            with (
                self.subTest(argv=argv),
                patch.object(smoke, "WebSocketCollector") as collector,
                patch.object(smoke, "InteractiveSecretProvider") as provider,
                redirect_stderr(io.StringIO()),
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    await smoke._run(args, parser)
                collector.assert_not_called()
                provider.assert_not_called()

    async def test_live_context_flag_validates_without_printing_values_or_invoking_model(self):
        results, requests = await self.full_evidence_case()
        code, output, _ = await self.run_smoke(
            results,
            requests=requests,
            argv=(
                "--allow-session-takeover", "--require-all-responses",
                "--validate-evidence-bundle", "--validate-live-context",
                "--akd-context-rows", "1", "--custodian-context-rows", "1",
                "--distribution-context-rows", "1", "--stats-context-points", "1",
            ),
            sent_request_types=tuple(request.request_type for request in requests),
        )
        self.assertEqual(code, 0)
        self.assertIn("In-memory LLM context and prompt: validated", output)
        self.assertNotIn("synthetic-akd-request", output)
        self.assertNotIn("AKFIS", output.split("In-memory LLM context and prompt:")[1].split("Smoke completed:")[0])
        self.assertNotIn(smoke.ANALYSIS_CONTEXT_BEGIN, output)
        self.assertNotIn(smoke.ANALYSIS_CONTEXT_END, output)

    async def test_emit_analysis_context_requires_validated_live_context_before_collect(self):
        parser = smoke._parser()
        args = parser.parse_args((
            "--allow-session-takeover", "--emit-analysis-context",
        ))
        with (
            patch.object(smoke, "WebSocketCollector") as collector,
            patch.object(smoke, "InteractiveSecretProvider") as provider,
            redirect_stderr(io.StringIO()),
            redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            await smoke._run(args, parser)
        collector.assert_not_called()
        provider.assert_not_called()

    async def test_emit_analysis_context_prints_one_parseable_marked_packet(self):
        results, requests = await self.full_evidence_case()
        argv = (
            "--allow-session-takeover", "--require-all-responses",
            "--validate-evidence-bundle", "--validate-live-context",
            "--akd-context-rows", "1", "--custodian-context-rows", "1",
            "--distribution-context-rows", "1", "--stats-context-points", "1",
            "--emit-analysis-context",
        )
        code, output, _ = await self.run_smoke(
            results,
            requests=requests,
            argv=argv,
            sent_request_types=tuple(request.request_type for request in requests),
        )

        lines = output.splitlines()
        begin = lines.index(smoke.ANALYSIS_CONTEXT_BEGIN)
        end = lines.index(smoke.ANALYSIS_CONTEXT_END)
        packet = "\n".join(lines[begin + 1:end])
        context = json.loads(packet)
        self.assertEqual(code, 0)
        self.assertEqual(lines.count(smoke.ANALYSIS_CONTEXT_BEGIN), 1)
        self.assertEqual(lines.count(smoke.ANALYSIS_CONTEXT_END), 1)
        self.assertEqual(context["schema_version"], "live_evidence_context_v1")
        self.assertEqual(context["symbol"], "AKFIS")
        self.assertFalse(context["akd"]["truncated"])
        self.assertEqual(context["akd"]["included_rows"], context["akd"]["total_rows"])
        self.assertEqual(context["akd"]["rows"][0]["brokerage_code"], "TEST")
        self.assertEqual(
            context["akd"]["local_sent_request"],
            {"symbol": "AKFIS", "start": "2026-09-22", "end": "2026-09-22"},
        )
        self.assertEqual(context["custodian"]["server_date"], "2026-09-22")
        self.assertIn("percentage_as_received", context["custodian"]["rows"][0])
        self.assertEqual(context["custody_distribution"]["code_is_not_brokerage_code"], True)
        self.assertIn("provider_format", context["custody_stats"]["retail_percentage"])
        self.assertTrue(context["custody_stats"]["retail_percentage"]["truncated"])
        self.assertNotIn("synthetic-akd-request", packet)
        self.assertIn("Smoke completed", output)

    async def test_emit_analysis_context_rejects_sensitive_brokerage_label_without_packet(self):
        results, requests = await self.full_evidence_case()
        brokerages = results[0].normalized
        secret = "https://example.invalid/?initData=DO_NOT_PRINT"
        poisoned_brokerage = replace(
            brokerages,
            rows=(replace(
                brokerages.rows[0], code="TEST", title=secret,
            ),),
        )
        results = (replace(results[0], normalized=poisoned_brokerage), *results[1:])
        argv = (
            "--allow-session-takeover", "--require-all-responses",
            "--validate-evidence-bundle", "--validate-live-context",
            "--akd-context-rows", "1", "--custodian-context-rows", "1",
            "--distribution-context-rows", "1", "--stats-context-points", "1",
            "--emit-analysis-context",
        )
        code, output, _ = await self.run_smoke(
            results,
            requests=requests,
            argv=argv,
            sent_request_types=tuple(request.request_type for request in requests),
        )
        self.assertEqual(code, 1)
        self.assertIn("Smoke failed: safe analysis context export validation", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("DO_NOT_PRINT", output)
        self.assertNotIn(smoke.ANALYSIS_CONTEXT_BEGIN, output)
        self.assertNotIn(smoke.ANALYSIS_CONTEXT_END, output)
        self.assertNotIn("Smoke completed", output)

    async def test_live_context_failure_does_not_print_payload(self):
        results, requests = await self.full_evidence_case()
        with patch.object(smoke, "build_live_analysis_context", side_effect=ValueError("private-payload")):
            code, output, _ = await self.run_smoke(
                results,
                requests=requests,
                argv=(
                    "--allow-session-takeover", "--require-all-responses",
                    "--validate-evidence-bundle", "--validate-live-context",
                    "--akd-context-rows", "1", "--custodian-context-rows", "1",
                    "--distribution-context-rows", "1", "--stats-context-points", "1",
                ),
                sent_request_types=tuple(request.request_type for request in requests),
            )
        self.assertEqual(code, 1)
        self.assertIn("Smoke failed: in-memory LLM context validation", output)
        self.assertNotIn("private-payload", output)

    async def test_evidence_flag_rejects_date_mismatch_after_all_five_responses(self):
        results, requests = await self.full_evidence_case()
        results = results[:2] + (
            replace(results[2], normalized=replace(
                results[2].normalized,
                request_provenance=SentCustodianRequestProvenance("AKFIS", date(2026, 9, 23)),
            )),
        ) + results[3:]
        code, output, _ = await self.run_smoke(
            results,
            requests=requests,
            argv=("--allow-session-takeover", "--require-all-responses", "--validate-evidence-bundle"),
            sent_request_types=tuple(request.request_type for request in requests),
        )
        self.assertEqual(code, 1)
        self.assertIn("in-memory evidence view: custodian_date_mismatch", output)
        self.assertNotIn("Smoke completed", output)

    async def test_live_brokerages_smoke_requires_candidate_typed_response(self):
        result = (await replay_frames((fixture_frame("brokerages_data_live"),)))[0]
        self.assertIsInstance(result.normalized, LiveBrokeragesResponse)
        code, output, _ = await self.run_smoke((result,))
        self.assertEqual(code, 0)
        self.assertIn("Smoke completed: requested response received: brokerages_data", output)

    async def test_legacy_brokerages_fixture_does_not_satisfy_live_smoke(self):
        legacy = (await replay_frames((fixture_frame("brokerages_data"),)))[0]
        code, output, _ = await self.run_smoke((legacy,))
        self.assertEqual(code, 1)
        self.assertNotIn("Smoke completed", output)

    async def test_live_akd_event_is_expected_but_incomplete_schema_fails(self):
        results = await replay_frames((fixture_frame("akd_data_partial"),))
        code, output, _ = await self.run_smoke(
            results, requests=(CollectorRequest("get_akd"),)
        )
        self.assertEqual(smoke.REQUEST_RESPONSE_EVENTS["get_akd"], "akd_data")
        self.assertEqual(code, 1)
        self.assertIn("Event: akd_data (known; normalization pending", output)
        self.assertIn("safe local send context is unavailable", output)
        self.assertNotIn("synthetic-akd-request", output)
        self.assertNotIn("Smoke completed", output)

    async def test_akd_only_sends_only_exact_dated_request(self):
        parser = smoke._parser()
        args = parser.parse_args((
            "--akd-only", "--symbol", "GLRMK", "--start-date", "2026-09-22",
            "--end-date", "2026-09-22",
        ))
        requests = smoke._requests(args, parser)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].to_json(), (
            '{"type":"get_akd","data":{"company":"GLRMK",'
            '"start":"2026-09-22","end":"2026-09-22"}}'
        ))

    async def test_akd_only_requires_all_fields_and_no_custody_options(self):
        parser = smoke._parser()
        for argv in (
            ("--akd-only", "--symbol", "GLRMK"),
            ("--akd-only", "--symbol", "GLRMK", "--start-date", "2026-09-22",
             "--end-date", "2026-09-22", "--custody-date", "2026-09-22"),
        ):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    smoke._requests(parser.parse_args(argv), parser)

    async def test_akd_smoke_requires_bound_typed_response(self):
        raw = (await replay_frames((fixture_frame("akd_data_partial"),)))[0]
        request = CollectorRequest("get_akd")
        forged = replace(raw, normalization_pending=False)
        code, _, _ = await self.run_smoke((forged,), requests=(request,))
        self.assertEqual(code, 1)
        bound = replace(forged, normalized=replace(
            forged.normalized,
            request_provenance=SentAKDRequestProvenance(
                symbol="GLRMK", start=date(2026, 9, 22), end=date(2026, 9, 22)
            ),
        ))
        code, output, _ = await self.run_smoke((bound,), requests=(request,))
        self.assertEqual(code, 0)
        self.assertIn("local sent-request provenance only", output)

    async def test_legacy_akd_snapshot_does_not_satisfy_live_request(self):
        results = await replay_frames((fixture_frame("akd_snapshot"),))
        code, _, _ = await self.run_smoke(results, requests=(CollectorRequest("get_akd"),))
        self.assertEqual(code, 1)

    async def test_custodian_only_sends_one_dated_request(self):
        parser = smoke._parser()
        args = parser.parse_args((
            "--custodian-only", "--symbol", "AKFIS", "--custody-date", "2026-09-23",
        ))
        requests = smoke._requests(args, parser)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].to_json(),
                         '{"type":"get_custodian","data":{"company":"AKFIS","date":"2026-09-23"}}')

    async def test_custodian_only_rejects_missing_and_unrelated_options(self):
        parser = smoke._parser()
        examples = (
            ("--custodian-only", "--symbol", "AKFIS"),
            ("--custodian-only", "--custody-date", "2026-09-23"),
            ("--custodian-only", "--akd-only", "--symbol", "AKFIS", "--custody-date", "2026-09-23"),
            ("--custodian-only", "--symbol", "AKFIS", "--custody-date", "2026-09-23",
             "--start-date", "2026-09-22"),
            ("--custodian-only", "--symbol", "AKFIS", "--custody-date", "2026-09-23",
             "--include-custody-history"),
        )
        for argv in examples:
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    smoke._requests(parser.parse_args(argv), parser)

    async def test_custodian_smoke_requires_typed_local_provenance(self):
        request = CollectorRequest("get_custodian")
        raw = (await replay_frames((fixture_frame("custodian_data_live"),)))[0]
        self.assertIsInstance(raw.normalized, LiveCustodianResponse)
        code, _, _ = await self.run_smoke((raw,), requests=(request,))
        self.assertEqual(code, 1)
        forged = replace(raw, normalization_pending=False)
        code, _, _ = await self.run_smoke((forged,), requests=(request,))
        self.assertEqual(code, 1)
        bound = replace(forged, normalized=replace(
            forged.normalized,
            request_provenance=SentCustodianRequestProvenance(
                symbol="AKFIS", requested_date=date(2026, 9, 23)
            ),
        ))
        code, output, _ = await self.run_smoke((bound,), requests=(request,))
        self.assertEqual(code, 0)
        self.assertIn("requested date (local): 2026-09-23", output)
        self.assertIn("response date (server): 2026-09-22", output)

    async def test_legacy_custodian_fixture_does_not_count_as_live_success(self):
        request = CollectorRequest("get_custodian")
        legacy = (await replay_frames((fixture_frame("custodian_data"),)))[0]
        code, _, _ = await self.run_smoke((legacy,), requests=(request,))
        self.assertEqual(code, 1)

    async def test_custodian_diff_only_sends_one_dated_request(self):
        parser = smoke._parser()
        args = parser.parse_args((
            "--custodian-diff-only", "--symbol", "AKFIS",
            "--start-date", "2026-09-14", "--end-date", "2026-09-23",
        ))
        requests = smoke._requests(args, parser)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].to_json(),
                         '{"type":"get_custodian_diff","data":{"company":"AKFIS",'
                         '"start":"2026-09-14","end":"2026-09-23"}}')

    async def test_custodian_diff_only_rejects_missing_conflicting_options(self):
        parser = smoke._parser()
        examples = (
            ("--custodian-diff-only", "--symbol", "AKFIS"),
            ("--custodian-diff-only", "--start-date", "2026-09-14", "--end-date", "2026-09-23"),
            ("--custodian-diff-only", "--akd-only", "--symbol", "AKFIS",
             "--start-date", "2026-09-14", "--end-date", "2026-09-23"),
            ("--custodian-diff-only", "--custodian-only", "--symbol", "AKFIS",
             "--start-date", "2026-09-14", "--end-date", "2026-09-23"),
            ("--custodian-diff-only", "--symbol", "AKFIS", "--start-date", "2026-09-14",
             "--end-date", "2026-09-23", "--custody-date", "2026-09-23"),
            ("--custodian-diff-only", "--symbol", "AKFIS", "--start-date", "2026-09-14",
             "--end-date", "2026-09-23", "--include-custody-history"),
        )
        for argv in examples:
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    smoke._requests(parser.parse_args(argv), parser)

    async def test_custodian_diff_smoke_requires_live_typed_provenance(self):
        request = CollectorRequest("get_custodian_diff")
        raw = (await replay_frames((fixture_frame("custodian_diff_data_live"),)))[0]
        self.assertIsInstance(raw.normalized, LiveCustodianDiffResponse)
        code, _, _ = await self.run_smoke((raw,), requests=(request,))
        self.assertEqual(code, 1)
        forged = replace(raw, normalization_pending=False)
        code, _, _ = await self.run_smoke((forged,), requests=(request,))
        self.assertEqual(code, 1)
        bound = replace(forged, normalized=replace(
            forged.normalized,
            request_provenance=SentCustodianDiffRequestProvenance(
                symbol="AKFIS", requested_start=date(2026, 9, 14),
                requested_end=date(2026, 9, 23),
            ),
        ))
        code, output, _ = await self.run_smoke((bound,), requests=(request,))
        self.assertEqual(code, 0)
        self.assertIn("requested range (local): 2026-09-14..2026-09-23", output)
        self.assertIn("response range (server): 2026-09-14..2026-09-22", output)

    async def test_legacy_custodian_diff_is_not_live_smoke_success(self):
        request = CollectorRequest("get_custodian_diff")
        legacy = (await replay_frames((fixture_frame("custodian_diff_data"),)))[0]
        code, _, _ = await self.run_smoke((legacy,), requests=(request,))
        self.assertEqual(code, 1)

    async def test_custody_analysis_only_sends_one_bounded_request(self):
        parser = smoke._parser()
        args = parser.parse_args((
            "--custody-analysis-only", "--symbol", "AKFIS",
            "--from-date", "2026-09-14", "--to-date", "2026-09-23",
        ))
        requests = smoke._requests(args, parser)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].to_json(),
                         '{"type":"custody_analysis","data":{"symbol":"AKFIS",'
                         '"from_date":"2026-09-14","to_date":"2026-09-23"}}')

    async def test_custody_analysis_only_requires_fields_and_excludes_other_modes(self):
        parser = smoke._parser()
        examples = (
            ("--custody-analysis-only", "--symbol", "AKFIS"),
            ("--custody-analysis-only", "--from-date", "2026-09-14", "--to-date", "2026-09-23"),
            ("--custody-analysis-only", "--akd-only", "--symbol", "AKFIS",
             "--from-date", "2026-09-14", "--to-date", "2026-09-23"),
            ("--custody-analysis-only", "--custodian-only", "--symbol", "AKFIS",
             "--from-date", "2026-09-14", "--to-date", "2026-09-23"),
            ("--custody-analysis-only", "--custodian-diff-only", "--symbol", "AKFIS",
             "--from-date", "2026-09-14", "--to-date", "2026-09-23"),
            ("--custody-analysis-only", "--symbol", "AKFIS", "--from-date", "2026-09-14",
             "--to-date", "2026-09-23", "--custody-date", "2026-09-23"),
            ("--custody-analysis-only", "--symbol", "AKFIS", "--from-date", "2026-09-14",
             "--to-date", "2026-09-23", "--include-custody-history"),
            ("--custody-analysis-only", "--symbol", "AKFIS", "--from-date", "2026-09-23",
             "--to-date", "2026-09-14"),
        )
        for argv in examples:
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    smoke._requests(parser.parse_args(argv), parser)

    async def test_custody_analysis_over_90_days_requires_explicit_flag(self):
        parser = smoke._parser()
        base = ("--custody-analysis-only", "--symbol", "AKFIS",
                "--from-date", "2026-06-01", "--to-date", "2026-09-23")
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                smoke._requests(parser.parse_args(base), parser)
        requests = smoke._requests(parser.parse_args(base + ("--allow-large-range",)), parser)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].request_type, "custody_analysis")
        exactly_90 = ("--custody-analysis-only", "--symbol", "AKFIS",
                      "--from-date", "2026-06-25", "--to-date", "2026-09-23")
        self.assertEqual(len(smoke._requests(parser.parse_args(exactly_90), parser)), 1)

    async def test_custody_distribution_only_sends_one_symbol_request(self):
        parser = smoke._parser()
        args = parser.parse_args((
            "--custody-distribution-only", "--symbol", "AKFIS",
        ))
        requests = smoke._requests(args, parser)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].to_json(),
                         '{"type":"get_custody_distribution","data":{"code":"AKFIS"}}')

    async def test_custody_distribution_only_rejects_missing_and_conflicting_options(self):
        parser = smoke._parser()
        examples = (
            ("--custody-distribution-only",),
            ("--custody-distribution-only", "--symbol", "AKFIS", "--start-date", "2026-09-14"),
            ("--custody-distribution-only", "--symbol", "AKFIS", "--end-date", "2026-09-23"),
            ("--custody-distribution-only", "--symbol", "AKFIS", "--custody-date", "2026-09-23"),
            ("--custody-distribution-only", "--symbol", "AKFIS", "--include-custody-history"),
            ("--custody-distribution-only", "--symbol", "AKFIS", "--from-date", "2026-09-14"),
            ("--custody-distribution-only", "--symbol", "AKFIS", "--allow-large-range"),
            ("--custody-distribution-only", "--akd-only", "--symbol", "AKFIS",
             "--start-date", "2026-09-14", "--end-date", "2026-09-23"),
        )
        for argv in examples:
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    smoke._requests(parser.parse_args(argv), parser)

    async def test_custody_distribution_smoke_requires_live_typed_local_provenance(self):
        request = CollectorRequest("get_custody_distribution", {"code": "AKFIS"})
        raw = (await replay_frames((fixture_frame("custody_distribution_data_live"),)))[0]
        self.assertIsInstance(raw.normalized, LiveCustodyDistributionResponse)
        code, _, _ = await self.run_smoke((raw,), requests=(request,))
        self.assertEqual(code, 1)

        forged = replace(raw, normalization_pending=False)
        code, _, _ = await self.run_smoke((forged,), requests=(request,))
        self.assertEqual(code, 1)

        bound = replace(forged, normalized=replace(
            forged.normalized,
            request_provenance=SentCustodyDistributionRequestProvenance("AKFIS"),
        ))
        code, output, _ = await self.run_smoke((bound,), requests=(request,))
        self.assertEqual(code, 0)
        self.assertIn("local sent-request provenance only", output)
        self.assertIn("not echoed by server", output)

    async def test_third_level_distribution_smoke_requires_bound_typed_response(self):
        request = CollectorRequest("get_custody_distribution", {"code": "AKFIS"})
        event = json.loads(fixture_frame("custody_distribution_data_live"))
        wrapper = event["data"]["data"]
        nested = {
            "periods": wrapper.pop("periods"),
            "data": wrapper.pop("data"),
        }
        wrapper["data"] = nested
        raw = (await replay_frames((json.dumps(event),)))[0]
        self.assertIsInstance(raw.normalized, LiveCustodyDistributionResponse)
        code, _, _ = await self.run_smoke((raw,), requests=(request,))
        self.assertEqual(code, 1)

        bound = replace(raw, normalization_pending=False, normalized=replace(
            raw.normalized,
            request_provenance=SentCustodyDistributionRequestProvenance("AKFIS"),
        ))
        code, _, _ = await self.run_smoke((bound,), requests=(request,))
        self.assertEqual(code, 0)

    async def test_legacy_distribution_does_not_count_as_live_smoke_success(self):
        request = CollectorRequest("get_custody_distribution", {"code": "AKFIS"})
        legacy = (await replay_frames((fixture_frame("custody_distribution_data"),)))[0]
        code, _, _ = await self.run_smoke((legacy,), requests=(request,))
        self.assertEqual(code, 1)

    async def test_analysis_failure_prints_masked_field_and_type_categories(self):
        parser = smoke._parser()
        args = parser.parse_args((
            "--allow-session-takeover", "--custody-analysis-only", "--symbol", "AKFIS",
            "--from-date", "2026-09-14", "--to-date", "2026-09-23",
        ))
        output = io.StringIO()
        with (
            patch.object(smoke, "WebSocketCollector") as collector,
            patch.object(smoke, "InteractiveSecretProvider"),
            redirect_stdout(output),
        ):
            async def fail(_requests):
                collector.call_args.kwargs["diagnostic_observer"](AttemptDiagnostic(
                    1, "parse", "field_type", "custody_analysis_data",
                    "event.data.data.akd.[date][index].net_cost", "number", "string",
                ))
                raise smoke.CollectorConnectionError("safe failure")

            collector.return_value.collect = AsyncMock(side_effect=fail)
            code = await smoke._run(args, parser)
        self.assertEqual(code, 1)
        self.assertIn("field=event.data.data.akd.[date][index].net_cost", output.getvalue())
        self.assertIn("expected=number; actual=string", output.getvalue())
        self.assertNotIn("2026-09-22", output.getvalue())

    async def test_distribution_failure_prints_only_allowlisted_shape_summary(self):
        parser = smoke._parser()
        args = parser.parse_args((
            "--allow-session-takeover", "--custody-distribution-only",
            "--symbol", "AKFIS",
        ))
        output = io.StringIO()
        with (
            patch.object(smoke, "WebSocketCollector") as collector,
            patch.object(smoke, "InteractiveSecretProvider"),
            redirect_stdout(output),
        ):
            async def fail(_requests):
                collector.call_args.kwargs["diagnostic_observer"](AttemptDiagnostic(
                    1, "parse", "missing_field", "custody_distribution_data",
                    "event.data.data.periods", "present", "missing_or_null",
                    (
                        "event.data=object{request_id:string,timestamp:number,"
                        "processing_time_ms:number,data:object,periods:missing}",
                        "event.data.data=object{data:array,periods:null,source:string}",
                    ),
                ))
                raise smoke.CollectorConnectionError("safe failure")

            collector.return_value.collect = AsyncMock(side_effect=fail)
            code = await smoke._run(args, parser)
        rendered = output.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("field=event.data.data.periods", rendered)
        self.assertIn("periods:missing", rendered)
        self.assertIn("periods:null", rendered)
        self.assertNotIn("SECRET", rendered)

    async def test_custody_analysis_smoke_needs_bound_matching_live_response(self):
        request = CollectorRequest("custody_analysis")
        raw = (await replay_frames((fixture_frame("custody_analysis_data_live"),)))[0]
        self.assertIsInstance(raw.normalized, LiveCustodyAnalysisResponse)
        code, _, _ = await self.run_smoke((raw,), requests=(request,))
        self.assertEqual(code, 1)
        forged = replace(raw, normalization_pending=False)
        code, _, _ = await self.run_smoke((forged,), requests=(request,))
        self.assertEqual(code, 1)
        wrong_symbol = replace(forged, normalized=replace(
            forged.normalized,
            request_provenance=SentCustodyAnalysisRequestProvenance(
                symbol="ASELS", requested_from=date(2026, 9, 14),
                requested_to=date(2026, 9, 23),
            ),
        ))
        code, _, _ = await self.run_smoke((wrong_symbol,), requests=(request,))
        self.assertEqual(code, 1)
        bound = replace(forged, normalized=replace(
            forged.normalized,
            request_provenance=SentCustodyAnalysisRequestProvenance(
                symbol="AKFIS", requested_from=date(2026, 9, 14),
                requested_to=date(2026, 9, 23),
            ),
        ))
        code, output, _ = await self.run_smoke((bound,), requests=(request,))
        self.assertEqual(code, 0)
        self.assertIn("local requested range: 2026-09-14..2026-09-23", output)
        self.assertIn("actual_from: 2026-09-19", output)

    async def test_custody_stats_only_builds_one_exact_history_request(self):
        parser = smoke._parser()
        args = parser.parse_args(("--custody-stats-only", "--symbol", "akfis"))
        requests = smoke._requests(args, parser)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].to_json(), (
            '{"type":"get_custody_stats","data":{"symbol":"AKFIS",'
            '"history":true}}'
        ))

    async def test_custody_stats_only_rejects_missing_or_conflicting_options(self):
        parser = smoke._parser()
        examples = (
            ("--custody-stats-only",),
            ("--custody-stats-only", "--symbol", "AKFIS", "--start-date", "2030-01-02"),
            ("--custody-stats-only", "--symbol", "AKFIS", "--include-custody-history"),
            ("--custody-stats-only", "--custody-distribution-only", "--symbol", "AKFIS"),
            ("--custody-stats-only", "--symbol", "AKFIS", "--allow-large-range"),
        )
        for argv in examples:
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    smoke._requests(parser.parse_args(argv), parser)

    async def test_custody_stats_smoke_requires_live_typed_local_provenance(self):
        raw = (await replay_frames((fixture_frame("custody_stats_data_live"),)))[0]
        self.assertIsInstance(raw.normalized, LiveCustodyStatsResponse)
        request = smoke.build_custody_stats_request("AKFIS")
        code, _, _ = await self.run_smoke((raw,), requests=(request,))
        self.assertEqual(code, 1)
        forged = replace(raw, normalization_pending=False)
        code, _, _ = await self.run_smoke((forged,), requests=(request,))
        self.assertEqual(code, 1)
        bound = replace(forged, normalized=replace(
            forged.normalized,
            request_provenance=SentCustodyStatsRequestProvenance(
                symbol="AKFIS", history=True,
            ),
        ))
        code, output, _ = await self.run_smoke((bound,), requests=(request,))
        self.assertEqual(code, 0)
        self.assertIn("AKFIS/true", output)
        unrelated = replace(bound, normalized=replace(
            bound.normalized,
            request_provenance=SentCustodyStatsRequestProvenance(
                symbol="ASELS", history=True,
            ),
        ))
        code, _, _ = await self.run_smoke((unrelated,), requests=(request,))
        self.assertEqual(code, 1)

    async def test_legacy_custody_stats_fixture_is_not_live_smoke_success(self):
        legacy = (await replay_frames((fixture_frame("custody_stats_data"),)))[0]
        code, output, _ = await self.run_smoke(
            (legacy,), requests=(CollectorRequest("get_custody_stats"),)
        )
        self.assertEqual(code, 1)
        self.assertNotIn("Smoke completed", output)

    async def test_legacy_custody_history_is_not_live_smoke_success(self):
        request = CollectorRequest("custody_analysis")
        legacy = (await replay_frames((fixture_frame("custody_analysis_data"),)))[0]
        code, _, _ = await self.run_smoke((legacy,), requests=(request,))
        self.assertEqual(code, 1)

    async def test_pending_status_never_counts_as_normalization_success(self):
        result = DispatchResult("akd_data", True, object(), normalization_pending=True)
        code, _, _ = await self.run_smoke((result,), requests=(CollectorRequest("get_akd"),))
        self.assertEqual(code, 1)

    async def test_expected_label_without_normalization_fails(self):
        for known in (False, True):
            with self.subTest(known=known):
                code, _, _ = await self.run_smoke((
                    DispatchResult("brokerages_data", known, None),
                ))
                self.assertEqual(code, 1)

    async def test_frame_limit_alias_and_receive_timeout_are_preserved(self):
        for flag in ("--max-frames", "--max-events"):
            with self.subTest(flag=flag):
                _, output, config = await self.run_smoke(
                    (), argv=(
                        "--allow-session-takeover", flag, "2", "--timeout", "0.1"
                    )
                )
                self.assertEqual(config.max_events, 2)
                self.assertEqual(config.receive_timeout_seconds, 0.1)
                self.assertIn("2 total frames per attempt", output)

    def test_period_analysis_profile_builds_four_exact_range_requests(self):
        parser = smoke._parser()
        expected = (
            '{"type":"get_brokerages"}',
            '{"type":"get_akd","data":{"company":"AKFIS",'
            '"start":"2026-09-08","end":"2026-09-25"}}',
            '{"type":"custody_analysis","data":{"symbol":"AKFIS",'
            '"from_date":"2026-09-08","to_date":"2026-09-25"}}',
            '{"type":"get_custodian_diff","data":{"company":"AKFIS",'
            '"start":"2026-09-08","end":"2026-09-25"}}',
        )
        for flag in (
            "--emit-period-analysis-context",
            "--save-period-analysis-context",
        ):
            with self.subTest(flag=flag):
                args = parser.parse_args((
                    flag, "--symbol", "akfis", "--start-date", "2026-09-08",
                    "--end-date", "2026-09-25",
                ))
                requests = smoke._requests(args, parser)
                self.assertEqual(
                    tuple(request.request_type for request in requests),
                    smoke.PERIOD_ANALYSIS_REQUEST_TYPES,
                )
                self.assertEqual(
                    tuple(request.to_json() for request in requests), expected,
                )

    def test_period_analysis_profile_rejects_conflicting_options(self):
        parser = smoke._parser()
        examples = (
            (
                "--emit-period-analysis-context", "--symbol", "AKFIS",
            ),
            (
                "--emit-period-analysis-context", "--symbol", "AKFIS",
                "--start-date", "2026-09-08", "--end-date", "2026-09-25",
                "--custody-date", "2026-09-25",
            ),
            (
                "--emit-period-analysis-context", "--symbol", "AKFIS",
                "--start-date", "2026-09-08", "--end-date", "2026-09-25",
                "--custodian-diff-only",
            ),
        )
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                smoke._requests(parser.parse_args(examples[0]), parser)
            for argv in examples[1:]:
                with self.subTest(argv=argv), self.assertRaises(SystemExit):
                    smoke._requests(parser.parse_args(argv), parser)
            with self.assertRaises(SystemExit):
                parser.parse_args((
                    "--emit-period-analysis-context",
                    "--save-period-analysis-context",
                ))

    async def test_period_analysis_handoff_emits_complete_compact_safe_packet(self):
        results, requests = await self.period_analysis_case()
        code, output, _ = await self.run_smoke(
            results,
            requests=requests,
            argv=(
                "--allow-session-takeover", "--emit-period-analysis-context",
                "--symbol", "AKFIS", "--start-date", "2026-09-14",
                "--end-date", "2026-09-23",
            ),
            sent_request_types=smoke.PERIOD_ANALYSIS_REQUEST_TYPES,
        )
        self.assertEqual(code, 0)
        self.assertIn("Expected responses (all):", output)
        self.assertIn(smoke.PERIOD_ANALYSIS_CONTEXT_BEGIN, output)
        self.assertIn(smoke.PERIOD_ANALYSIS_CONTEXT_END, output)
        packet_text = output.split(smoke.PERIOD_ANALYSIS_CONTEXT_BEGIN + "\n", 1)[1]
        packet_text = packet_text.split("\n" + smoke.PERIOD_ANALYSIS_CONTEXT_END, 1)[0]
        self.assertNotIn("\n", packet_text)
        self.assertNotIn("synthetic-akd-request", output)
        self.assertNotIn("synthetic-analysis-request", output)
        self.assertNotIn("synthetic-diff-request", output)
        self.assertNotIn("request_id", output)
        packet = json.loads(packet_text)
        self.assertEqual(packet["schema"], "fintr_period_analysis_handoff_v2")
        directory = packet["brokerage_directory"]
        self.assertEqual(directory["source_event"], "brokerages_data")
        self.assertEqual(directory["row_count"], 1)
        self.assertEqual(directory["rows"], [{
            "code": "SYNTH-CODE-001",
            "title": "Synthetic Candidate Title",
            "short_title": "Synthetic",
        }])
        self.assertEqual(directory["flags"], {
            "undated": True,
            "exact_code_match_only": True,
            "identifies_ultimate_investor": False,
        })
        self.assertEqual(packet["requested_period"]["symbol"], "AKFIS")
        self.assertEqual(packet["get_akd_period"]["row_count"], 1)
        self.assertEqual(
            packet["get_akd_period"]["provenance"]["local_sent_request"]["origin"],
            "local_sent_request",
        )
        self.assertFalse(
            packet["get_akd_period"]["provenance"]["server_echo"]["period_echoed"]
        )
        daily = packet["custody_analysis_daily"]
        self.assertEqual(
            daily["provenance"]["server_echo"]["dates_in_provider_order"],
            ["2026-09-22", "2026-09-21", "2026-09-19"],
        )
        self.assertEqual(
            daily["provenance"]["server_echo"]["data_range"]["actual_from"],
            "2026-09-19",
        )
        self.assertEqual(
            daily["provenance"]["server_echo"]["data_range"]["requested_from"],
            "2026-09-14",
        )
        self.assertEqual(
            daily["provenance"]["server_echo"]["data_range"]["requested_to"],
            "2026-09-23",
        )
        self.assertEqual(
            daily["coverage"]["daily_akd"]["missing_from_family_dates"],
            ["2026-09-21"],
        )
        self.assertEqual(
            daily["coverage"]["daily_custody_balance"]["missing_from_family_dates"],
            ["2026-09-19"],
        )
        self.assertEqual(
            [day["date"] for day in daily["daily_akd_flow_in_provider_order"]],
            ["2026-09-22", "2026-09-19"],
        )
        self.assertEqual(
            [day["date"] for day in daily["daily_custody_balance_in_provider_order"]],
            ["2026-09-22", "2026-09-21"],
        )
        self.assertEqual(packet["get_custodian_diff_period"]["row_count"], 2)
        self.assertEqual(
            len(packet["get_custodian_diff_period"]["rows_in_provider_order"]), 2,
        )
        self.assertIn("22.840000000000001", packet_text)

    async def test_period_handoff_redacts_unknown_event_type_in_cli_output(self):
        results, requests = await self.period_analysis_case()
        unknown_result = (await replay_frames(
            ('{"type":"authorization:abc123"}',)
        ))[0]
        results = (*results, unknown_result)
        code, output, _ = await self.run_smoke(
            results,
            requests=requests,
            argv=(
                "--allow-session-takeover", "--emit-period-analysis-context",
                "--symbol", "AKFIS", "--start-date", "2026-09-14",
                "--end-date", "2026-09-23",
            ),
            sent_request_types=smoke.PERIOD_ANALYSIS_REQUEST_TYPES,
        )
        self.assertEqual(code, 0)
        self.assertNotIn("authorization:abc123", output)
        self.assertIn("Event: [UNKNOWN_EVENT_TYPE] (unknown)", output)
        for event_type in (
            "brokerages_data", "akd_data", "custody_analysis_data", "custodian_diff_data",
        ):
            self.assertIn(f"Event: {event_type} (known)", output)
        self.assertIn(smoke.PERIOD_ANALYSIS_CONTEXT_BEGIN, output)

    async def test_period_analysis_handoff_rejects_missing_or_mismatched_response(self):
        results, requests = await self.period_analysis_case()
        argv = (
            "--allow-session-takeover", "--emit-period-analysis-context",
            "--symbol", "AKFIS", "--start-date", "2026-09-14",
            "--end-date", "2026-09-23",
        )
        for name, candidate_results in (
            ("missing", results[:3]),
            (
                "mismatched server range",
                (await self.period_analysis_case(
                    diff_end=date(2026, 9, 22),
                ))[0],
            ),
        ):
            with self.subTest(case=name):
                code, output, _ = await self.run_smoke(
                    candidate_results,
                    requests=requests,
                    argv=argv,
                    sent_request_types=smoke.PERIOD_ANALYSIS_REQUEST_TYPES,
                )
                self.assertEqual(code, 1)
                self.assertNotIn(smoke.PERIOD_ANALYSIS_CONTEXT_BEGIN, output)
                self.assertNotIn(smoke.PERIOD_ANALYSIS_CONTEXT_END, output)
                if name == "mismatched server range":
                    self.assertIn("requested_range=2026-09-14..2026-09-23", output)
                    self.assertIn("server_reported_range=2026-09-14..2026-09-22", output)

    async def test_period_analysis_handoff_fails_closed_on_secret_like_source(self):
        results, requests = await self.period_analysis_case(
            secret_source="wss://secret.example/ws?auth_date=1700000000",
        )
        code, output, _ = await self.run_smoke(
            results,
            requests=requests,
            argv=(
                "--allow-session-takeover", "--emit-period-analysis-context",
                "--symbol", "AKFIS", "--start-date", "2026-09-14",
                "--end-date", "2026-09-23",
            ),
            sent_request_types=smoke.PERIOD_ANALYSIS_REQUEST_TYPES,
        )
        self.assertEqual(code, 1)
        self.assertIn("safe period-analysis export validation", output)
        self.assertNotIn("secret.example", output)
        self.assertNotIn(smoke.PERIOD_ANALYSIS_CONTEXT_BEGIN, output)

    async def test_period_analysis_context_saves_full_packet_without_json_stdout(self):
        results, requests = await self.period_analysis_case()
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            with patch.object(smoke, "PROJECT_ROOT", project_root):
                code, output, _ = await self.run_smoke(
                    results,
                    requests=requests,
                    argv=(
                        "--allow-session-takeover", "--save-period-analysis-context",
                        "--symbol", "AKFIS", "--start-date", "2026-09-14",
                        "--end-date", "2026-09-23",
                    ),
                    sent_request_types=smoke.PERIOD_ANALYSIS_REQUEST_TYPES,
                )
            target = (
                project_root / "data" / "interim" / "period_analysis" / "AKFIS"
                / "2026-09-14_2026-09-23.json"
            )
            self.assertEqual(code, 0, output)
            self.assertIn(
                "data/interim/period_analysis/AKFIS/2026-09-14_2026-09-23.json",
                output,
            )
            self.assertIn("Smoke completed: all period-analysis requests", output)
            self.assertNotIn(smoke.PERIOD_ANALYSIS_CONTEXT_BEGIN, output)
            self.assertNotIn(smoke.PERIOD_ANALYSIS_CONTEXT_END, output)
            self.assertNotIn("22.840000000000001", output)
            self.assertNotIn("request_id", output)
            saved_text = target.read_text(encoding="utf-8")
            self.assertNotIn("request_id", saved_text)
            self.assertNotIn("synthetic-akd-request", saved_text)
            artifact = json.loads(saved_text, parse_float=Decimal)
            metadata = artifact["capture_metadata"]
            packet = artifact["packet"]
            self.assertEqual(
                metadata["schema"], "fintr_period_analysis_capture_v1",
            )
            captured_at = datetime.fromisoformat(
                metadata["captured_at_utc"].replace("Z", "+00:00")
            )
            self.assertEqual(captured_at.utcoffset(), timezone.utc.utcoffset(captured_at))
            self.assertEqual(
                metadata["permission_provenance"]["basis"],
                "user_asserted_by_explicit_opt_in_cli_invocation",
            )
            self.assertEqual(
                metadata["permission_provenance"]["status"],
                "user_asserted_not_independently_verified",
            )
            self.assertFalse(
                metadata["permission_provenance"]["independently_verified"]
            )
            self.assertEqual(
                set(metadata["provider_sources"]),
                {"get_brokerages", "get_akd", "custody_analysis", "get_custodian_diff"},
            )
            self.assertEqual(
                metadata["provider_sources"]["get_brokerages"],
                "not_reported_by_normalized_schema",
            )
            self.assertEqual(
                metadata["provider_sources"]["custody_analysis"],
                "not_reported_by_normalized_schema",
            )
            self.assertEqual(
                metadata["provider_sources"]["get_akd"],
                packet["get_akd_period"]["provenance"]["server_echo"]["source"],
            )
            self.assertEqual(
                metadata["packet_sha256_canonical_json"],
                hashlib.sha256(
                    smoke._canonical_period_json(packet).encode("utf-8")
                ).hexdigest(),
            )
            self.assertEqual(packet["get_akd_period"]["row_count"], 1)
            self.assertEqual(
                packet["custody_analysis_daily"]["coverage"]["daily_akd"]
                ["missing_from_family_dates"],
                ["2026-09-21"],
            )
            self.assertEqual(
                packet["get_custodian_diff_period"]["row_count"], 2,
            )
            self.assertIn("22.840000000000001", saved_text)

    async def test_period_analysis_capture_refuses_overwrite(self):
        results, requests = await self.period_analysis_case()
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            with patch.object(smoke, "PROJECT_ROOT", project_root):
                code, output, _ = await self.run_smoke(
                    results,
                    requests=requests,
                    argv=(
                        "--allow-session-takeover", "--save-period-analysis-context",
                        "--symbol", "AKFIS", "--start-date", "2026-09-14",
                        "--end-date", "2026-09-23",
                    ),
                    sent_request_types=smoke.PERIOD_ANALYSIS_REQUEST_TYPES,
                )
                target = (
                    project_root / "data" / "interim" / "period_analysis" / "AKFIS"
                    / "2026-09-14_2026-09-23.json"
                )
                before = target.read_bytes()
                packet = smoke._period_analysis_packet(
                    results,
                    requests,
                    tuple(
                        SendObservation(1, request_type, "sent")
                        for request_type in smoke.PERIOD_ANALYSIS_REQUEST_TYPES
                    ),
                    symbol="AKFIS", start=date(2026, 9, 14), end=date(2026, 9, 23),
                )
                packet["get_akd_period"]["row_count"] = 999
                with self.assertRaises(smoke.PeriodAnalysisCaptureError) as error:
                    smoke._save_period_analysis_capture(packet)
                self.assertEqual(error.exception.reason, "capture_already_exists")
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(list(target.parent.iterdir()), [target])
            self.assertEqual(code, 0, output)
            self.assertIn("Smoke completed", output)

    async def test_period_analysis_save_validation_failure_leaves_no_file(self):
        results, requests = await self.period_analysis_case(
            secret_source="wss://secret.example/ws?auth_date=1700000000",
        )
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            with patch.object(smoke, "PROJECT_ROOT", project_root):
                code, output, _ = await self.run_smoke(
                    results,
                    requests=requests,
                    argv=(
                        "--allow-session-takeover", "--save-period-analysis-context",
                        "--symbol", "AKFIS", "--start-date", "2026-09-14",
                        "--end-date", "2026-09-23",
                    ),
                    sent_request_types=smoke.PERIOD_ANALYSIS_REQUEST_TYPES,
                )
            self.assertEqual(code, 1)
            self.assertIn("safe period-analysis export validation", output)
            self.assertNotIn("secret.example", output)
            self.assertNotIn(smoke.PERIOD_ANALYSIS_CONTEXT_BEGIN, output)
            self.assertEqual(list(project_root.iterdir()), [])

    async def test_dry_run_skips_session_takeover_secret_and_connector(self):
        parser = smoke._parser()
        args = parser.parse_args(("--dry-run",))
        output = io.StringIO()
        with (
            patch.object(smoke, "WebSocketCollector") as collector,
            patch.object(smoke, "InteractiveSecretProvider") as provider,
            redirect_stdout(output),
        ):
            code = await smoke._run(args, parser)
        rendered = output.getvalue()
        self.assertEqual(code, 0)
        collector.assert_not_called()
        provider.assert_not_called()
        self.assertNotIn("WARNING", rendered)
        self.assertIn("Planned requests, in order:", rendered)
        self.assertIn('get_brokerages: {"type":"get_brokerages"}', rendered)
        self.assertIn("Expected responses (at least one): brokerages_data", rendered)
        self.assertIn("Persistent storage: disabled", rendered)
        self.assertIn("Dry run: no connection opened", rendered)

    async def test_dry_run_prints_exact_period_request_payloads_in_order(self):
        parser = smoke._parser()
        args = parser.parse_args((
            "--dry-run", "--save-period-analysis-context",
            "--symbol", "akfis", "--start-date", "2026-09-08",
            "--end-date", "2026-09-29",
        ))
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(smoke, "PROJECT_ROOT", Path(temporary)),
                patch.object(smoke, "WebSocketCollector") as collector,
                patch.object(smoke, "InteractiveSecretProvider") as provider,
                redirect_stdout(output),
            ):
                code = await smoke._run(args, parser)
        rendered = output.getvalue()
        self.assertEqual(code, 0)
        collector.assert_not_called()
        provider.assert_not_called()
        lines = rendered.splitlines()
        self.assertEqual(
            [line.strip() for line in lines if line.strip().split(":", 1)[0] in (
                "get_brokerages", "get_akd", "custody_analysis", "get_custodian_diff",
            )],
            [
                'get_brokerages: {"type":"get_brokerages"}',
                'get_akd: {"type":"get_akd","data":{"company":"AKFIS",'
                '"start":"2026-09-08","end":"2026-09-29"}}',
                'custody_analysis: {"type":"custody_analysis","data":{"symbol":"AKFIS",'
                '"from_date":"2026-09-08","to_date":"2026-09-29"}}',
                'get_custodian_diff: {"type":"get_custodian_diff","data":{"company":"AKFIS",'
                '"start":"2026-09-08","end":"2026-09-29"}}',
            ],
        )
        self.assertIn(
            "Expected responses (all): akd_data, brokerages_data, "
            "custodian_diff_data, custody_analysis_data",
            rendered,
        )
        self.assertIn(
            "Persistence target: data/interim/period_analysis/AKFIS/"
            "2026-09-08_2026-09-29.json",
            rendered,
        )
        self.assertIn("Persistence target does not exist yet", rendered)
        self.assertIn("Dry run: no connection opened", rendered)

    async def test_dry_run_reports_existing_period_capture_target(self):
        parser = smoke._parser()
        args = parser.parse_args((
            "--dry-run", "--save-period-analysis-context",
            "--symbol", "AKFIS", "--start-date", "2026-09-08",
            "--end-date", "2026-09-29",
        ))
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            existing = (
                project_root / "data" / "interim" / "period_analysis" / "AKFIS"
                / "2026-09-08_2026-09-29.json"
            )
            existing.parent.mkdir(parents=True)
            existing.write_text("{}", encoding="utf-8")
            output = io.StringIO()
            with (
                patch.object(smoke, "PROJECT_ROOT", project_root),
                patch.object(smoke, "WebSocketCollector") as collector,
                patch.object(smoke, "InteractiveSecretProvider") as provider,
                redirect_stdout(output),
            ):
                code = await smoke._run(args, parser)
        rendered = output.getvalue()
        self.assertEqual(code, 0)
        collector.assert_not_called()
        provider.assert_not_called()
        self.assertIn(
            "Persistence target: data/interim/period_analysis/AKFIS/"
            "2026-09-08_2026-09-29.json",
            rendered,
        )
        self.assertIn(
            'Persistence target already exists: a real run would fail with '
            '"already exists" (no overwrite)',
            rendered,
        )

    async def test_dry_run_rejects_invalid_combo_like_a_real_run(self):
        parser = smoke._parser()
        args = parser.parse_args(("--dry-run", "--symbol", "AKFIS"))
        error = io.StringIO()
        with (
            patch.object(smoke, "WebSocketCollector") as collector,
            patch.object(smoke, "InteractiveSecretProvider") as provider,
            redirect_stderr(error),
        ):
            with self.assertRaises(SystemExit) as captured:
                await smoke._run(args, parser)
        self.assertEqual(captured.exception.code, 2)
        self.assertIn(
            "--symbol requires --start-date, --end-date, --custody-date",
            error.getvalue(),
        )
        collector.assert_not_called()
        provider.assert_not_called()

    async def test_max_message_mib_is_bounded_and_reaches_collector_config(self):
        parser = smoke._parser()
        for bad in ("0", "9"):
            args = parser.parse_args(("--dry-run", "--max-message-mib", bad))
            with redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit):
                await smoke._run(args, parser)
            self.assertIn("--max-message-mib must be between 1 and 8", error.getvalue())

        args = parser.parse_args((
            "--symbol", "AKFIS", "--start-date", "2026-07-01", "--end-date", "2026-09-29",
            "--save-period-analysis-context", "--allow-session-takeover",
            "--max-message-mib", "4",
        ))
        with (
            patch.object(smoke, "WebSocketCollector") as collector,
            patch.object(smoke, "InteractiveSecretProvider"),
            redirect_stdout(io.StringIO()) as output,
        ):
            collector.return_value.collect = AsyncMock(return_value=())
            await smoke._run(args, parser)
        self.assertEqual(collector.call_args.kwargs["config"].max_message_bytes, 4 * 1_048_576)
        self.assertIn("4 MiB per message", output.getvalue())

    def test_period_analysis_capture_rejects_unsafe_symbol_before_path_creation(self):
        packet = {
            "schema": "fintr_period_analysis_handoff_v1",
            "requested_period": {
                "symbol": "../AKFIS", "start_date": "2026-09-14",
                "end_date": "2026-09-23",
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            with patch.object(smoke, "PROJECT_ROOT", project_root):
                with self.assertRaises(smoke.PeriodAnalysisCaptureError) as error:
                    smoke._save_period_analysis_capture(packet)
            self.assertEqual(error.exception.reason, "invalid_packet_scope")
            self.assertEqual(list(project_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
