"""Unit tests for DynamoDB Outreach Records & Agent Linkage Layer."""

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage.outreach_records import OutreachRecordService
from domain_rules import DEFAULT_MAX_RESCHEDULE_ATTEMPTS, CallDisposition


class TestDynamoDBStorage(unittest.TestCase):
    def setUp(self):
        self.service = OutreachRecordService(table_name="Test_OutreachRecords")

    def test_put_and_get_record(self):
        record = {
            "patient_id": "PT-TEST-001",
            "first_name": "Alice",
            "last_name": "Brown",
            "phone_number": "+18145550155",
            "campaign_id": "camp-oct-2026",
        }
        saved = self.service.put_record(record)
        self.assertEqual(saved["patient_id"], "PT-TEST-001")
        self.assertEqual(saved["reschedule_count"], 0)
        self.assertEqual(saved["call_status"], "PENDING")

        fetched = self.service.get_record("PT-TEST-001")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["first_name"], "Alice")

    def test_link_assigned_agent(self):
        patient_id = "PT-TEST-002"
        self.service.put_record({"patient_id": patient_id, "first_name": "Bob"})

        agent_data = {
            "agentId": "agent-777",
            "agentName": "Mark Stevens",
            "agentPhone": "+15822671755",
            "checkedAt": "2026-09-22T15:00:00Z",
        }
        assigned = self.service.link_assigned_agent(patient_id, agent_data)
        self.assertEqual(assigned["agentId"], "agent-777")
        self.assertEqual(assigned["agentName"], "Mark Stevens")

        rec = self.service.get_record(patient_id)
        self.assertEqual(rec["call_status"], CallDisposition.TRANSFERRED.value)
        self.assertEqual(rec["assigned_agent"]["agentPhone"], "+15822671755")

    def test_reschedule_increment_and_24h_default(self):
        patient_id = "PT-TEST-003"
        self.service.put_record({"patient_id": patient_id, "first_name": "Charlie"})

        # Attempt 1: No specific time requested -> default 24h offset
        res1 = self.service.update_reschedule(patient_id, requested_time=None)
        self.assertEqual(res1["reschedule_count"], 1)
        self.assertEqual(res1["call_status"], CallDisposition.RESCHEDULED.value)
        self.assertIsNotNone(res1["scheduled_time"])

        # Attempt 2: Specific time requested
        res2 = self.service.update_reschedule(patient_id, requested_time="2026-09-24T10:00:00Z")
        self.assertEqual(res2["reschedule_count"], 2)
        self.assertEqual(res2["call_status"], CallDisposition.RESCHEDULED.value)
        self.assertEqual(res2["scheduled_time"], "2026-09-24T10:00:00Z")

        # Attempt 3: Reaching MAX (3) -> EXHAUSTED
        res3 = self.service.update_reschedule(patient_id)
        self.assertEqual(res3["reschedule_count"], 3)
        self.assertEqual(res3["call_status"], "EXHAUSTED")
        self.assertIsNone(res3["scheduled_time"])


if __name__ == "__main__":
    unittest.main()
