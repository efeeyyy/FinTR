"""Offline smoke acceptance tests: no authentication and no live connection."""

import importlib.util
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fintr.collector.client import replay_frames
from fintr.collector.models import CollectorRequest, DispatchResult


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
    async def run_smoke(self, results, *, requests=None, argv=("--allow-session-takeover",)):
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
            collector.return_value.collect = AsyncMock(return_value=results)
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
            fixture_frame("brokerages_data"),
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

    async def test_all_confirmed_request_response_pairs(self):
        pairs = (
            ("get_brokerages", "brokerages_data"),
            ("get_akd", "akd_snapshot"),
            ("get_custodian", "custodian_data"),
            ("get_custody_distribution", "custody_distribution_data"),
            ("get_custody_stats", "custody_stats_data"),
            ("custody_analysis", "custody_analysis_data"),
            ("get_custodian_diff", "custodian_diff_data"),
        )
        for request_type, event_type in pairs:
            with self.subTest(request_type=request_type):
                results = await replay_frames((fixture_frame(event_type),))
                code, _, _ = await self.run_smoke(
                    results, requests=(CollectorRequest(request_type),)
                )
                self.assertEqual(code, 0)

    async def test_one_of_multiple_requested_responses_is_sufficient(self):
        results = await replay_frames((fixture_frame("akd_snapshot"),))
        code, _, _ = await self.run_smoke(results, requests=(
            CollectorRequest("get_brokerages"), CollectorRequest("get_akd"),
        ))
        self.assertEqual(code, 0)

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


if __name__ == "__main__":
    unittest.main()
