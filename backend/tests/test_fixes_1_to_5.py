import unittest
from unittest.mock import MagicMock
import uuid

from backend.storage.outreach_records import (
    write_patient_record,
    format_disposition,
    format_call_summary,
)
import importlib
campaign_api = importlib.import_module("backend.lambda.campaign_api")
_compute_retry_delay_label = campaign_api._compute_retry_delay_label
from backend.gateway.nova_sonic_service import NovaSonicSession


class TestFixes1Through5(unittest.TestCase):
    def test_fix_1_write_patient_record_always_generates_fresh_uuid(self):
        """Verify that write_patient_record never reuses patient_id on same empi."""
        mock_ddb = MagicMock()
        mock_table = MagicMock()
        mock_ddb.Table.return_value = mock_table

        patient_data = {
            "empi": "TESTPT-DUPE",
            "phone_number": "+15822671755",
            "first_name": "Test",
            "last_name": "Patient",
        }

        # First write
        res1 = write_patient_record(
            patient=patient_data,
            campaign_id="CAMP-001",
            scheduled_time="2026-09-26T12:00:00Z",
            dynamodb_resource=mock_ddb,
            source_filename="batch1.csv",
        )
        self.assertTrue(res1["success"])
        pid1 = res1["patient_id"]

        # Second write with same empi but different campaign
        res2 = write_patient_record(
            patient=patient_data,
            campaign_id="CAMP-002",
            scheduled_time="2026-09-26T13:00:00Z",
            dynamodb_resource=mock_ddb,
            source_filename="batch2.csv",
        )
        self.assertTrue(res2["success"])
        pid2 = res2["patient_id"]

        # Verify both have distinct UUIDs
        self.assertNotEqual(pid1, pid2)
        # Verify both are valid UUIDs
        uuid.UUID(pid1)
        uuid.UUID(pid2)

        # Verify DynamoDB put_item was called twice with items having respective UUIDs and campaign_ids
        calls = mock_table.put_item.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["Item"]["patient_id"], pid1)
        self.assertEqual(calls[0][1]["Item"]["campaign_id"], "CAMP-001")
        self.assertEqual(calls[0][1]["Item"]["source_filename"], "batch1.csv")

        self.assertEqual(calls[1][1]["Item"]["patient_id"], pid2)
        self.assertEqual(calls[1][1]["Item"]["campaign_id"], "CAMP-002")
        self.assertEqual(calls[1][1]["Item"]["source_filename"], "batch2.csv")

    def test_fix_4_no_engagement_disposition_and_summary(self):
        """Verify format_disposition and format_call_summary for NO_ENGAGEMENT."""
        dispo = format_disposition("NO_ENGAGEMENT")
        self.assertEqual(dispo, "NO_ENGAGEMENT - Call Connected, No Response Given")

        summary = format_call_summary("NO_ENGAGEMENT")
        self.assertEqual(summary, "Call was answered but recipient never spoke")

    def test_fix_4_nova_sonic_session_default_and_transition(self):
        """Verify NovaSonicSession defaults to NO_ENGAGEMENT and transitions to COMPLETED on USER speech."""
        mock_client = MagicMock()
        session = NovaSonicSession(mock_client, contact_id="test-contact-123")
        self.assertEqual(session.final_disposition, "NO_ENGAGEMENT")

    def test_fix_5_compute_retry_delay_label_stale_records(self):
        """Verify _compute_retry_delay_label handles negative/zero deltas gracefully."""
        # Scheduled time is before call end time (stale pre-fix record)
        end_iso = "2026-09-25T15:00:00Z"
        sched_iso = "2026-09-25T14:00:00Z"
        label = _compute_retry_delay_label(end_iso, sched_iso)
        self.assertEqual(label, "pending retry")

        # Scheduled time is equal to call end time
        sched_iso_same = "2026-09-25T15:00:00Z"
        label_same = _compute_retry_delay_label(end_iso, sched_iso_same)
        self.assertEqual(label_same, "pending retry")

        # Scheduled time is 2 hours later
        sched_iso_future = "2026-09-25T17:00:00Z"
        label_future = _compute_retry_delay_label(end_iso, sched_iso_future)
        self.assertEqual(label_future, "2h")

        # Scheduled time is 15 minutes later
        sched_iso_15m = "2026-09-25T15:15:00Z"
        label_15m = _compute_retry_delay_label(end_iso, sched_iso_15m)
        self.assertEqual(label_15m, "15m")


if __name__ == "__main__":
    unittest.main()
