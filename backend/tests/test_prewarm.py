import datetime as dt
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "lambda") not in sys.path:
    sys.path.insert(0, str(ROOT / "lambda"))

from scripts.prewarm import (
    PrewarmResult,
    prewarm_bedrock_nova_lite,
    prewarm_lambda,
    prewarm_lex,
    prewarm_wisdom_and_bedrock,
    run_prewarm,
)
import campaign_intake
import campaign_dialer
import campaign_api


class TestPrewarmUnit(unittest.TestCase):
    def test_prewarm_result_formatting(self):
        res = PrewarmResult(
            component="Lambda (Worker 1)",
            target="cara-health-bot-agent-availability",
            status="SUCCESS",
            duration_ms=42.5,
            details="Status 200: ok",
        )
        s = str(res)
        self.assertIn("[OK]    ", s)
        self.assertIn("Lambda (Worker 1)", s)
        self.assertIn("42.5 ms", s)

    def test_prewarm_lambda_dry_run(self):
        mock_session = MagicMock()
        res = prewarm_lambda(
            session=mock_session,
            function_name="cara-health-bot-agent-availability",
            worker_idx=1,
            dry_run=True,
        )
        self.assertEqual(res.status, "SUCCESS")
        self.assertIn("Dry run simulated", res.details)
        mock_session.client.assert_not_called()

    def test_prewarm_lambda_mock_invoke(self):
        mock_session = MagicMock()
        mock_lambda = MagicMock()
        mock_session.client.return_value = mock_lambda
        mock_response = {
            "StatusCode": 200,
            "Payload": MagicMock(read=lambda: b'{"status": "ok", "prewarmed": true}'),
        }
        mock_lambda.invoke.return_value = mock_response

        res = prewarm_lambda(
            session=mock_session,
            function_name="cara-health-bot-agent-availability",
            worker_idx=1,
            dry_run=False,
        )
        self.assertEqual(res.status, "SUCCESS")
        mock_lambda.invoke.assert_called_once()
        call_kwargs = mock_lambda.invoke.call_args[1]
        self.assertEqual(call_kwargs["FunctionName"], "cara-health-bot-agent-availability")
        payload = json.loads(call_kwargs["Payload"])
        self.assertEqual(payload["operation"], "ping")

    def test_prewarm_lex_mock_recognize(self):
        mock_session = MagicMock()
        mock_lex = MagicMock()
        mock_session.client.return_value = mock_lex
        mock_lex.recognize_text.return_value = {
            "sessionState": {"intent": {"name": "FallbackIntent"}}
        }

        res = prewarm_lex(
            session=mock_session,
            component_name="Lex Identity Bot",
            bot_id="4M8I8HGPND",
            alias_id="KJZH9WFPLC",
            dry_run=False,
        )
        self.assertEqual(res.status, "SUCCESS")
        self.assertIn("FallbackIntent", res.details)
        mock_lex.recognize_text.assert_called_once()

    def test_prewarm_bedrock_nova_lite_mock(self):
        mock_session = MagicMock()
        mock_bedrock = MagicMock()
        mock_session.client.return_value = mock_bedrock
        mock_bedrock.converse.return_value = {"stopReason": "max_tokens"}

        res = prewarm_bedrock_nova_lite(
            session=mock_session,
            orchestration_model_id="us.amazon.nova-lite-v1:0",
            dry_run=False,
        )
        self.assertEqual(res.status, "SUCCESS")
        self.assertIn("Bedrock TTFT", res.details)
        mock_bedrock.converse.assert_called_once()

    def test_prewarm_wisdom_and_bedrock_alias(self):
        self.assertIs(prewarm_wisdom_and_bedrock, prewarm_bedrock_nova_lite)

    def test_run_prewarm_dry_run_all(self):
        results = run_prewarm(lead_time_seconds=60, concurrency=3, dry_run=True)
        self.assertEqual(len(results), 7)  # 3 lambdas + 3 lex + 1 bedrock nova lite
        for r in results:
            self.assertEqual(r.status, "SUCCESS")


class TestScheduleNameTruncation(unittest.TestCase):
    def test_schedule_name_truncation_long_campaign_id(self):
        # Long campaign ID (e.g. 50 characters, making base name > 64 chars)
        long_campaign_id = "camp-uuid-12345678-1234-5678-1234-567812345678-extra-long-identifier"
        
        start_name = campaign_intake._schedule_name(long_campaign_id, suffix="")
        prewarm_name = campaign_intake._schedule_name(long_campaign_id, suffix="-prewarm")

        # 1. Start name and prewarm name are DIFFERENT strings
        self.assertNotEqual(start_name, prewarm_name)
        # 2. Both are <= 64 chars
        self.assertLessEqual(len(start_name), 64)
        self.assertLessEqual(len(prewarm_name), 64)
        # 3. Prewarm name still ends with "-prewarm"
        self.assertTrue(prewarm_name.endswith("-prewarm"))
        self.assertEqual(prewarm_name[-8:], "-prewarm")

    def test_schedule_name_matches_campaign_api(self):
        long_id = "test-uuid-12345678-1234-5678-1234-567812345678-long-suffix-overflow"
        self.assertEqual(
            campaign_intake._schedule_name(long_id, ""),
            campaign_api._scheduler_name(long_id, "")
        )
        self.assertEqual(
            campaign_intake._schedule_name(long_id, "-prewarm"),
            campaign_api._scheduler_name(long_id, "-prewarm")
        )


class TestPrewarmDecouplingAndEdgeCases(unittest.TestCase):
    def test_prewarm_schedule_expression_utc_normal_60s(self):
        # 10 minutes in the future
        future_dt = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)
        call_time_iso = future_dt.isoformat()

        expr, is_immediate = campaign_intake._prewarm_schedule_expression_utc(call_time_iso, lead_time_seconds=60)
        self.assertFalse(is_immediate)
        expected_prewarm = (future_dt - dt.timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S")
        self.assertEqual(expr, f"at({expected_prewarm})")

    def test_prewarm_schedule_expression_utc_immediate_edge_case(self):
        # 10 seconds in the future (less than 60s lead time)
        near_future_dt = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10)
        call_time_iso = near_future_dt.isoformat()

        expr, is_immediate = campaign_intake._prewarm_schedule_expression_utc(call_time_iso, lead_time_seconds=60)
        self.assertTrue(is_immediate)
        self.assertTrue(expr.startswith("at("))

    @patch("campaign_dialer.run_prewarm")
    @patch("campaign_dialer._start_next_call")
    @patch("campaign_dialer._batches_table")
    @patch("campaign_dialer._patients_table")
    @patch("boto3.client")
    def test_campaign_dialer_prewarm_trigger_handler(self, mock_boto, mock_pat, mock_bat, mock_start_next, mock_prewarm):
        event = {"trigger": "prewarm", "campaignId": "camp-test-123"}
        res = campaign_dialer.handler(event, None)
        self.assertEqual(res, {"handled": "prewarm", "campaignId": "camp-test-123"})
        mock_start_next.assert_not_called()

    @patch("campaign_dialer.run_prewarm")
    @patch("campaign_dialer._start_next_call")
    @patch("campaign_dialer._batches_table")
    @patch("campaign_dialer._patients_table")
    @patch("boto3.client")
    def test_campaign_dialer_campaign_start_does_not_block_on_prewarm(self, mock_boto, mock_pat, mock_bat, mock_start_next, mock_prewarm):
        event = {"trigger": "campaign-start", "campaignId": "camp-test-456"}
        res = campaign_dialer.handler(event, None)
        self.assertEqual(res, {"handled": "campaign-start", "campaignId": "camp-test-456"})
        mock_start_next.assert_called_once()
        mock_prewarm.assert_not_called()


class TestAgentAvailabilityIdleRewarm(unittest.TestCase):
    @patch("campaign_dialer.boto3.client")
    def test_maybe_rewarm_triggers_after_5_minutes_idle(self, mock_boto):
        mock_lambda = MagicMock()
        mock_boto.return_value = mock_lambda

        # Reset module state to simulated 10 minutes ago
        campaign_dialer._LAST_AVAILABILITY_PING_TIME = time.time() - 600.0

        dispatched = campaign_dialer._maybe_rewarm_agent_availability()
        self.assertTrue(dispatched)
        mock_lambda.invoke.assert_called_once()
        call_kwargs = mock_lambda.invoke.call_args[1]
        self.assertEqual(call_kwargs["InvocationType"], "Event")
        payload = json.loads(call_kwargs["Payload"].decode("utf-8"))
        self.assertEqual(payload["operation"], "ping")

    @patch("campaign_dialer.boto3.client")
    def test_maybe_rewarm_skips_within_5_minutes(self, mock_boto):
        mock_lambda = MagicMock()
        mock_boto.return_value = mock_lambda

        # Set to 1 minute ago (< 300s threshold)
        campaign_dialer._LAST_AVAILABILITY_PING_TIME = time.time() - 60.0

        dispatched = campaign_dialer._maybe_rewarm_agent_availability()
        self.assertFalse(dispatched)
        mock_lambda.invoke.assert_not_called()


class TestWisdomSessionNoOrphans(unittest.TestCase):
    def test_wisdom_session_count_unchanged_after_10_runs(self):
        # Verify that running prewarm 10 times creates 0 Wisdom/QConnect sessions
        mock_session = MagicMock()
        mock_wisdom = MagicMock()
        mock_bedrock = MagicMock()

        def client_factory(service, **kwargs):
            if service == "bedrock-runtime":
                return mock_bedrock
            if service in {"wisdom", "qconnect"}:
                return mock_wisdom
            return MagicMock()

        mock_session.client.side_effect = client_factory
        mock_bedrock.converse.return_value = {"stopReason": "max_tokens"}

        session_store = []
        mock_wisdom.search_sessions.side_effect = lambda **kw: {"sessionSummaries": session_store}
        mock_wisdom.create_session.side_effect = lambda **kw: session_store.append({"sessionId": "orphaned"})

        initial_count = len(mock_wisdom.search_sessions(assistantId="test")["sessionSummaries"])

        for _ in range(10):
            res = prewarm_bedrock_nova_lite(session=mock_session, orchestration_model_id="us.amazon.nova-lite-v1:0")
            self.assertEqual(res.status, "SUCCESS")

        final_count = len(mock_wisdom.search_sessions(assistantId="test")["sessionSummaries"])
        self.assertEqual(initial_count, 0)
        self.assertEqual(final_count, 0)
        mock_wisdom.create_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
