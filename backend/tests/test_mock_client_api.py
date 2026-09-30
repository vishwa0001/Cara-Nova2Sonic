"""Automated tests for FastAPI Mock Client Integration API."""

import time
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from mock_client_api import app


class TestMockClientAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health_check(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_agent_availability_default_success_sla(self):
        start = time.perf_counter()
        resp = self.client.get("/api/v1/agents/availability?callId=call-123&campaignId=cmp-456")
        duration_ms = (time.perf_counter() - start) * 1000

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["available"])
        self.assertIsNotNone(data["agentPhone"])
        self.assertEqual(data["agentId"], "agent-001")
        self.assertEqual(data["agentName"], "Sarah Jenkins")
        self.assertIn("T", data["checkedAt"])
        # SLA must be under 500ms
        self.assertLess(duration_ms, 500)

    def test_agent_availability_force_busy(self):
        resp = self.client.get("/api/v1/agents/availability?forceStatus=busy")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["available"])
        self.assertIsNone(data["agentPhone"])
        self.assertIsNone(data["agentId"])
        self.assertIsNone(data["agentName"])

    def test_agent_availability_force_available(self):
        resp = self.client.get("/api/v1/agents/availability?forceStatus=available")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["available"])
        self.assertIsNotNone(data["agentPhone"])

    def test_call_handoff_webhook_valid_payload(self):
        payload = {
            "callId": "call-test-999",
            "patientId": "PT-984321",
            "empi": "EMPI-984321",
            "patientName": "Jane Doe",
            "patientPhone": "+18145550123",
            "campaignId": "camp-wellness-01",
            "transferReason": "CONSENT_GRANTED",
            "callMode": "normal_coaching",
            "transferredAt": "2026-09-22T14:30:00Z",
            "assignedAgent": {
                "agentId": "agent-001",
                "agentName": "Sarah Jenkins",
                "agentPhone": "+15822671755",
                "checkedAt": "2026-09-22T14:30:00Z",
            },
            "callSummary": "Patient confirmed identity, answered questions on cost, and agreed to speak with nurse.",
            "transcript": [
                {"speaker": "system", "text": "Call connected", "timestamp": "2026-09-22T14:29:45Z"},
                {"speaker": "agent", "text": "Hi Jane, I'm Cara...", "timestamp": "2026-09-22T14:29:47Z"},
                {"speaker": "caller", "text": "Yes, I'd like to connect.", "timestamp": "2026-09-22T14:29:55Z"},
            ],
        }

        resp = self.client.post("/api/v1/webhooks/call-handoff", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["handoffId"].startswith("handoff-evt-"))
        self.assertIn("T", data["receivedAt"])

    def test_call_handoff_webhook_missing_mandatory_field(self):
        # Missing patientId and callId
        payload = {
            "patientName": "Jane Doe",
            "campaignId": "camp-wellness-01",
        }
        resp = self.client.post("/api/v1/webhooks/call-handoff", json=payload)
        self.assertEqual(resp.status_code, 422)  # Unprocessable Entity


if __name__ == "__main__":
    unittest.main()
