"""Unit tests for Sequential vs. Parallel Dialing Modes in Campaign Dialer."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure backend path is available
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
LAMBDA_DIR = os.path.join(ROOT, "lambda")
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)

import campaign_dialer


class TestDialingMode(unittest.TestCase):

    def setUp(self):
        self.mock_table = MagicMock()
        self.mock_records = [
            {"patient_id": "p1", "phone_number": "+15550100001", "attempt_count": 0},
            {"patient_id": "p2", "phone_number": "+15550100002", "attempt_count": 0},
            {"patient_id": "p3", "phone_number": "+15550100003", "attempt_count": 0},
        ]

    @patch.dict(os.environ, {"DIALING_MODE": "SEQUENTIAL", "MAX_CONCURRENT_CALLS": "1"})
    @patch("campaign_dialer._get_dynamodb_resource")
    @patch("campaign_dialer.resolve_ecs_gateway_url", return_value="http://127.0.0.1:8000/calls/originate")
    @patch("campaign_dialer.originate_single_call", return_value={"success": True, "call_id": "call-1"})
    @patch("campaign_dialer.count_active_calls", return_value=0)
    @patch("campaign_dialer.query_due_records")
    def test_sequential_mode_when_idle_dials_only_one(
        self, mock_query, mock_count, mock_originate, mock_resolve, mock_ddb
    ):
        mock_ddb.return_value.Table.return_value = self.mock_table
        # In sequential mode, query_due_records must be called with limit=1
        mock_query.return_value = [self.mock_records[0]]

        res = campaign_dialer.lambda_handler({}, None)

        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(res["dialed_count"], 1)
        mock_query.assert_called_once()
        # Verify limit argument is 1
        _, kwargs = mock_query.call_args
        limit_arg = kwargs.get("limit") or mock_query.call_args[0][2]
        self.assertEqual(limit_arg, 1)
        mock_originate.assert_called_once()

    @patch.dict(os.environ, {"DIALING_MODE": "SEQUENTIAL", "MAX_CONCURRENT_CALLS": "1"})
    @patch("campaign_dialer._get_dynamodb_resource")
    @patch("campaign_dialer.resolve_ecs_gateway_url", return_value="http://127.0.0.1:8000/calls/originate")
    @patch("campaign_dialer.originate_single_call")
    @patch("campaign_dialer.count_active_calls", return_value=1)
    @patch("campaign_dialer.query_due_records")
    def test_sequential_mode_when_busy_skips_dialing(
        self, mock_query, mock_count, mock_originate, mock_resolve, mock_ddb
    ):
        mock_ddb.return_value.Table.return_value = self.mock_table

        res = campaign_dialer.lambda_handler({}, None)

        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(res["dialed_count"], 0)
        self.assertEqual(res["active_calls"], 1)
        self.assertEqual(res["mode"], "SEQUENTIAL")
        mock_query.assert_not_called()
        mock_originate.assert_not_called()

    @patch.dict(os.environ, {"DIALING_MODE": "PARALLEL", "MAX_CONCURRENT_CALLS": "5"})
    @patch("campaign_dialer._get_dynamodb_resource")
    @patch("campaign_dialer.resolve_ecs_gateway_url", return_value="http://127.0.0.1:8000/calls/originate")
    @patch("campaign_dialer.originate_single_call", return_value={"success": True, "call_id": "call-x"})
    @patch("campaign_dialer.count_active_calls", return_value=1)
    @patch("campaign_dialer.query_due_records")
    @patch("campaign_dialer.time.sleep", return_value=None)
    def test_parallel_mode_dials_multiple_up_to_available_slots(
        self, mock_sleep, mock_query, mock_count, mock_originate, mock_resolve, mock_ddb
    ):
        mock_ddb.return_value.Table.return_value = self.mock_table
        # 1 active call with max 5 slots means available_slots = 4
        mock_query.return_value = self.mock_records  # 3 records

        res = campaign_dialer.lambda_handler({}, None)

        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(res["dialed_count"], 3)
        mock_query.assert_called_once()
        limit_arg = mock_query.call_args[1].get("limit") or mock_query.call_args[0][2]
        self.assertEqual(limit_arg, 4)
        self.assertEqual(mock_originate.call_count, 3)


if __name__ == "__main__":
    unittest.main()
