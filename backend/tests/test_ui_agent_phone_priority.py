"""The campaign's UI "Human Agent Phone" must win over the availability
API's agentPhone and the FIXED_AGENT_PHONE fallback."""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lambda"))

from backend.domain_rules import PatientContext
from backend.gateway.tools import OutreachToolExecutor
import campaign_dialer


def _executor(ui_phone):
    patient = PatientContext(patient_id="p1", first_name="Tim", last_name="Cook",
                             practice_name="X", human_agent_phone=ui_phone)
    storage = MagicMock()
    return OutreachToolExecutor(api_base_url="http://mock", storage_service=storage,
                                connect_client=MagicMock(), patient_context=patient, contact_id="c1")


def _run(executor, api_body):
    resp = MagicMock(status_code=200)
    resp.json.return_value = api_body
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    client.post = AsyncMock()
    with patch("httpx.AsyncClient", return_value=client):
        return asyncio.run(executor.evaluate_and_transfer_pes())


class AgentPhonePriorityTests(unittest.TestCase):
    def test_ui_number_beats_api_number(self):
        res = _run(_executor("+13105550000"), {"available": True, "agentPhone": "+12132960390"})
        self.assertEqual(res["agent_phone"], "+13105550000")

    def test_api_number_used_when_ui_blank(self):
        res = _run(_executor(""), {"available": True, "agentPhone": "+12132960390"})
        self.assertEqual(res["agent_phone"], "+12132960390")

    def test_fixed_agent_phone_when_both_blank(self):
        with patch.dict(os.environ, {"FIXED_AGENT_PHONE": "+12132960390"}):
            res = _run(_executor(""), {"available": True})
        self.assertEqual(res["agent_phone"], "+12132960390")

    def test_dialer_forwards_ui_number(self):
        with patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"
            urlopen.return_value.__enter__.return_value.status = 200
            campaign_dialer.originate_single_call("http://gw/calls/originate",
                                                  {"phone_number": "+1", "human_agent_phone": "+13105550000"})
            body = urlopen.call_args[0][0].data
        self.assertIn(b'"human_agent_phone": "+13105550000"', body)


if __name__ == "__main__":
    unittest.main()
