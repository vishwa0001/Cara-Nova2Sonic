"""The UI's campaign-level "Direct to Human Agent" toggle must survive
upload -> intake -> DynamoDB -> campaign list/records."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lambda"))

import campaign_api
import campaign_intake

CSV = (
    "empi,first_name,last_name,gender,phone_number,practice_name,practice_callback_number\n"
    "P1,Jane,Doe,Female,8145551001,Practice X,8005550100\n"
)


class PresignedUrlTests(unittest.TestCase):
    def _presign(self, body):
        s3 = MagicMock()
        s3.generate_presigned_url.return_value = "https://example/upload"
        with patch.object(campaign_api, "_get_s3_client", return_value=s3):
            res = campaign_api.handle_presigned_url(body)
        return res, s3

    def test_toggle_on_is_signed_into_upload_metadata(self):
        res, s3 = self._presign({"filename": "p.csv", "directAgentEnabled": True,
                                 "humanAgentPhoneNumber": "(814) 555-1234"})
        self.assertEqual(res["statusCode"], 200)
        meta = s3.generate_presigned_url.call_args[1]["Params"]["Metadata"]
        self.assertEqual(meta["direct-agent"], "yes")
        self.assertEqual(meta["human-agent-phone"], "+18145551234")
        body = json.loads(res["body"])
        self.assertTrue(body["directAgentEnabled"])
        self.assertEqual(body["uploadHeaders"]["x-amz-meta-direct-agent"], "yes")

    def test_toggle_off_by_default(self):
        res, s3 = self._presign({"filename": "p.csv"})
        self.assertEqual(res["statusCode"], 200)
        meta = s3.generate_presigned_url.call_args[1]["Params"]["Metadata"]
        self.assertEqual(meta["direct-agent"], "no")
        self.assertNotIn("human-agent-phone", meta)

    def test_campaign_timezone_is_signed_into_metadata(self):
        res, s3 = self._presign({"filename": "p.csv", "timezone": "America/New_York"})
        meta = s3.generate_presigned_url.call_args[1]["Params"]["Metadata"]
        self.assertEqual(meta["timezone"], "America/New_York")
        res, s3 = self._presign({"filename": "p.csv", "timezone": "Not/AZone"})
        self.assertNotIn("timezone", s3.generate_presigned_url.call_args[1]["Params"]["Metadata"])

    def test_filename_signed_as_browser_encodes_it(self):
        # Phones save downloads as "list (5)-1.csv"; the UI sends the header as
        # encodeURIComponent(name), so the signed value must match that.
        res, s3 = self._presign({"filename": "sample_customer_calling_list (5)-1.csv"})
        meta = s3.generate_presigned_url.call_args[1]["Params"]["Metadata"]
        self.assertEqual(meta["original-filename"], "sample_customer_calling_list%20(5)-1.csv")
        res, s3 = self._presign({"filename": "liste_é!~*'.csv"})
        meta = s3.generate_presigned_url.call_args[1]["Params"]["Metadata"]
        self.assertEqual(meta["original-filename"], "liste_%C3%A9!~*'.csv")

    def test_toggle_on_without_phone_is_rejected(self):
        res, s3 = self._presign({"filename": "p.csv", "directAgentEnabled": True})
        self.assertEqual(res["statusCode"], 400)
        s3.generate_presigned_url.assert_not_called()


class IntakeTests(unittest.TestCase):
    def _items(self, **kwargs):
        ddb = MagicMock()
        table = ddb.Table.return_value
        campaign_intake.process_csv_content(CSV, "CAMP-1", "2030-01-01T00:00:00Z",
                                            dynamodb_resource=ddb, **kwargs)
        return [c[1]["Item"] for c in table.put_item.call_args_list]

    def test_direct_agent_sets_call_mode_and_phone(self):
        (item,) = self._items(direct_agent=True, human_agent_phone="+18145551234")
        self.assertEqual(item["call_mode"], "MODE_1_DIRECT_HANDOFF")
        self.assertEqual(item["human_agent_phone"], "+18145551234")

    def test_campaign_timezone_stamped_on_rows(self):
        (item,) = self._items(campaign_timezone="America/New_York")
        self.assertEqual(item["timezone"], "America/New_York")

    def test_normal_campaign_unchanged(self):
        (item,) = self._items()
        self.assertEqual(item["call_mode"], "MODE_2_NORMAL_COACHING")
        self.assertNotIn("human_agent_phone", item)


class IntakeFilenameTests(unittest.TestCase):
    def test_signed_filename_is_decoded(self):
        s3 = MagicMock()
        s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=CSV.encode())),
            "Metadata": {"original-filename": "sample_customer_calling_list%20(5)-1.csv"},
        }
        event = {"Records": [{"s3": {"bucket": {"name": "b"},
                                     "object": {"key": "campaigns/CAMP-1/patients.csv"}}}]}
        with patch.object(campaign_intake.boto3, "client", return_value=s3),                 patch.object(campaign_intake, "process_csv_content", return_value={}) as proc:
            campaign_intake.lambda_handler(event, None)
        self.assertEqual(proc.call_args[1]["source_filename"], "sample_customer_calling_list (5)-1.csv")


class ReadBackTests(unittest.TestCase):
    def test_call_mode_fields(self):
        self.assertEqual(
            campaign_api._call_mode_fields({"call_mode": "MODE_1_DIRECT_HANDOFF", "human_agent_phone": "+1814"}),
            {"callMode": "DIRECT_HUMAN_HANDOFF", "directAgentEnabled": True, "humanAgentPhoneNumber": "+1814"},
        )
        self.assertFalse(campaign_api._call_mode_fields({})["directAgentEnabled"])


if __name__ == "__main__":
    unittest.main()
