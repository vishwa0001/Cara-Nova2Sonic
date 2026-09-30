from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cara_health_bot.builders import render_contact_flow
from cara_health_bot.config import load_config


class DirectHandoffFlowTests(unittest.TestCase):
    def setUp(self):
        self.flow_path = ROOT / "contact-flows" / "cara-health-bot-flow.json"
        self.cfg = load_config()

    def test_raw_flow_json_is_valid(self):
        content = self.flow_path.read_text(encoding="utf-8")
        data = json.loads(content)
        self.assertIn("Version", data)
        self.assertIn("StartAction", data)
        self.assertIn("Actions", data)

    def test_render_contact_flow_renders_without_unresolved_placeholders(self):
        rendered_json = render_contact_flow(
            cfg=self.cfg,
            assistant_id="test-assistant-id",
            assistant_arn="arn:aws:wisdom:us-east-1:123456789012:assistant/test",
            lex_alias_arn="arn:aws:lex:us-east-1:123456789012:bot-alias/test/alias",
            identity_lex_alias_arn="arn:aws:lex:us-east-1:123456789012:bot-alias/identity/alias",
            availability_lex_alias_arn="arn:aws:lex:us-east-1:123456789012:bot-alias/availability/alias",
            session_context_lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:context",
            human_transfer_queue_arn="arn:aws:connect:us-east-1:123456789012:instance/inst/queue/queue",
        )
        parsed = json.loads(rendered_json)
        self.assertIsInstance(parsed, dict)

    def test_recording_block_transitions_to_amd_and_callmode_compare(self):
        data = json.loads(self.flow_path.read_text(encoding="utf-8"))
        actions = {a["Identifier"]: a for a in data["Actions"]}

        rec_block = actions.get("20000000-0000-4000-8000-000000000001")
        self.assertIsNotNone(rec_block)
        self.assertEqual(rec_block["Type"], "UpdateContactRecordingBehavior")
        amd_action_id = rec_block["Transitions"]["NextAction"]
        self.assertEqual(amd_action_id, "f0000000-0000-4000-8000-000000000001")

        amd_block = actions.get(amd_action_id)
        self.assertEqual(amd_block["Type"], "CheckOutboundCallStatus")
        self.assertEqual(amd_block["Transitions"]["NextAction"], "e1000000-0000-4000-8000-000000000001")

        # Verify 2s fixed delay block and Loop blocks have been removed
        self.assertNotIn("f0000000-0000-4000-8000-000000000021", actions)
        self.assertNotIn("f0000000-0000-4000-8000-000000000020", actions)
        self.assertNotIn("f0000000-0000-4000-8000-000000000022", actions)

    def test_callmode_compare_block_routing(self):
        data = json.loads(self.flow_path.read_text(encoding="utf-8"))
        actions = {a["Identifier"]: a for a in data["Actions"]}

        callmode_block = actions.get("e1000000-0000-4000-8000-000000000001")
        self.assertIsNotNone(callmode_block)
        self.assertEqual(callmode_block["Type"], "Compare")
        self.assertEqual(callmode_block["Parameters"]["ComparisonValue"], "$.Attributes.callMode")

        # Verify DIRECT_HUMAN_HANDOFF condition routes to in-flow availability check
        conditions = callmode_block["Transitions"]["Conditions"]
        handoff_cond = next((c for c in conditions if c["Condition"]["Operands"] == ["DIRECT_HUMAN_HANDOFF"]), None)
        self.assertIsNotNone(handoff_cond)
        self.assertEqual(handoff_cond["NextAction"], "e1000000-0000-4000-8000-000000000010")

        # Verify Default / Missing transition points to original Identity Lex Bot
        self.assertEqual(callmode_block["Transitions"]["NextAction"], "10000000-0000-4000-8000-000000000001")

    def test_direct_call_availability_check_and_fallback_routing(self):
        data = json.loads(self.flow_path.read_text(encoding="utf-8"))
        actions = {a["Identifier"]: a for a in data["Actions"]}

        # 1. Availability check Lambda invocation node
        lambda_node = actions.get("e1000000-0000-4000-8000-000000000010")
        self.assertIsNotNone(lambda_node)
        self.assertEqual(lambda_node["Type"], "InvokeLambdaFunction")
        self.assertEqual(lambda_node["Parameters"]["LambdaInvocationAttributes"]["operation"], "checkAgentAvailability")
        self.assertEqual(lambda_node["Transitions"]["NextAction"], "e1000000-0000-4000-8000-000000000011")
        error_trans = next((e for e in lambda_node["Transitions"]["Errors"] if e["ErrorType"] == "NoMatchingError"), None)
        self.assertIsNotNone(error_trans)
        self.assertEqual(error_trans["NextAction"], "e1000000-0000-4000-8000-000000000020")

        # 2. Availability Compare node
        compare_node = actions.get("e1000000-0000-4000-8000-000000000011")
        self.assertIsNotNone(compare_node)
        self.assertEqual(compare_node["Type"], "Compare")
        self.assertEqual(compare_node["Parameters"]["ComparisonValue"], "$.External.available")
        true_cond = next((c for c in compare_node["Transitions"]["Conditions"] if c["Condition"]["Operands"] == ["true"]), None)
        self.assertIsNotNone(true_cond)
        self.assertEqual(true_cond["NextAction"], "e1000000-0000-4000-8000-000000000012")
        self.assertEqual(compare_node["Transitions"]["NextAction"], "e1000000-0000-4000-8000-000000000020")

        # 3. Available=true Attribute Update node (routes to phone check & transfer)
        avail_update = actions.get("e1000000-0000-4000-8000-000000000012")
        self.assertIsNotNone(avail_update)
        self.assertEqual(avail_update["Type"], "UpdateContactAttributes")
        self.assertEqual(avail_update["Parameters"]["Attributes"]["humanAgentPhoneNumber"], "$.External.agentPhone")
        self.assertEqual(avail_update["Transitions"]["NextAction"], "e1000000-0000-4000-8000-000000000002")

        # 4. Available=false Fallback node (resets callMode to NORMAL and routes to Normal Cara Lex Bot)
        fallback_update = actions.get("e1000000-0000-4000-8000-000000000020")
        self.assertIsNotNone(fallback_update)
        self.assertEqual(fallback_update["Type"], "UpdateContactAttributes")
        self.assertEqual(fallback_update["Parameters"]["Attributes"]["callMode"], "NORMAL")
        self.assertEqual(fallback_update["Transitions"]["NextAction"], "10000000-0000-4000-8000-000000000001")

    def test_mid_call_escalate_path_remains_isolated_and_untouched(self):
        data = json.loads(self.flow_path.read_text(encoding="utf-8"))
        actions = {a["Identifier"]: a for a in data["Actions"]}

        # Mid-call checkAgentAvailability node
        mid_lambda = actions.get("b0000000-0000-4000-8000-000000000010")
        self.assertIsNotNone(mid_lambda)
        self.assertEqual(mid_lambda["Transitions"]["NextAction"], "b0000000-0000-4000-8000-000000000011")

        # Mid-call Compare node
        mid_compare = actions.get("b0000000-0000-4000-8000-000000000011")
        self.assertIsNotNone(mid_compare)
        # Mid-call false branch still routes to callback announcement and scheduling
        self.assertEqual(mid_compare["Transitions"]["NextAction"], "b0000000-0000-4000-8000-000000000013")
        mid_prompt = actions.get("b0000000-0000-4000-8000-000000000013")
        self.assertIsNotNone(mid_prompt)
        self.assertEqual(mid_prompt["Transitions"]["NextAction"], "b0000000-0000-4000-8000-000000000014")
        mid_attr = actions.get("b0000000-0000-4000-8000-000000000014")
        self.assertIsNotNone(mid_attr)
        self.assertEqual(mid_attr["Transitions"]["NextAction"], "f2000000-0000-4000-8000-000000000003")

    def test_agent_phone_safeguard_compare_block(self):
        data = json.loads(self.flow_path.read_text(encoding="utf-8"))
        actions = {a["Identifier"]: a for a in data["Actions"]}

        agent_check_block = actions.get("e1000000-0000-4000-8000-000000000002")
        self.assertIsNotNone(agent_check_block)
        self.assertEqual(agent_check_block["Type"], "Compare")
        self.assertEqual(agent_check_block["Parameters"]["ComparisonValue"], "$.Attributes.humanAgentPhoneNumber")

        # TextStartsWith (+) condition routes to courtesy prompt
        conditions = agent_check_block["Transitions"]["Conditions"]
        plus_cond = next((c for c in conditions if c["Condition"]["Operator"] == "TextStartsWith" and c["Condition"]["Operands"] == ["+"]), None)
        self.assertIsNotNone(plus_cond)
        self.assertEqual(plus_cond["NextAction"], "e1000000-0000-4000-8000-000000000003")

        # Missing or invalid phone number routes to failure prompt via error transition and default fallback
        errors = agent_check_block["Transitions"]["Errors"]
        no_match = next((e for e in errors if e["ErrorType"] == "NoMatchingCondition"), None)
        self.assertIsNotNone(no_match)
        self.assertEqual(no_match["NextAction"], "e1000000-0000-4000-8000-000000000005")
        self.assertEqual(agent_check_block["Transitions"]["NextAction"], "e1000000-0000-4000-8000-000000000005")

    def test_transfer_to_phone_number_block_parameters(self):
        data = json.loads(self.flow_path.read_text(encoding="utf-8"))
        actions = {a["Identifier"]: a for a in data["Actions"]}

        transfer_block = actions.get("e1000000-0000-4000-8000-000000000004")
        self.assertIsNotNone(transfer_block)
        self.assertEqual(transfer_block["Type"], "TransferParticipantToThirdParty")
        self.assertEqual(transfer_block["Parameters"]["ThirdPartyPhoneNumber"], "$.Attributes.humanAgentPhoneNumber")
        self.assertEqual(transfer_block["Parameters"]["ThirdPartyConnectionTimeLimitSeconds"], "25")
        self.assertEqual(transfer_block["Parameters"]["ContinueFlowExecution"], "True")

        caller_id_block = actions.get("e1000000-0000-4000-8000-000000000006")
        self.assertIsNotNone(caller_id_block)
        self.assertEqual(caller_id_block["Type"], "UpdateContactAttributes")
        self.assertEqual(caller_id_block["Parameters"]["Attributes"]["CallerIdNumber"], "$.CustomerEndpoint.Address")

        # NextAction routes to post-transfer outcome evaluator
        self.assertEqual(transfer_block["Transitions"]["NextAction"], "f2000000-0000-4000-8000-000000000030")

        eval_block = actions.get("f2000000-0000-4000-8000-000000000030")
        self.assertIsNotNone(eval_block)
        self.assertEqual(eval_block["Type"], "InvokeLambdaFunction")
        self.assertEqual(eval_block["Parameters"]["LambdaInvocationAttributes"]["operation"], "evaluateTransferOutcome")
        self.assertEqual(eval_block["Transitions"]["NextAction"], "f2000000-0000-4000-8000-000000000031")
        self.assertEqual(eval_block["Transitions"]["Errors"][0]["NextAction"], "f2000000-0000-4000-8000-000000000000")

        compare_block = actions.get("f2000000-0000-4000-8000-000000000031")
        self.assertIsNotNone(compare_block)
        self.assertEqual(compare_block["Type"], "Compare")
        self.assertEqual(compare_block["Parameters"]["ComparisonValue"], "$.External.transferOutcome")
        self.assertEqual(compare_block["Transitions"]["Conditions"][0]["Condition"]["Operands"][0], "COMPLETED")
        self.assertEqual(compare_block["Transitions"]["Conditions"][0]["NextAction"], "77777777-7777-4777-8777-777777777777")
        self.assertEqual(compare_block["Transitions"]["NextAction"], "f2000000-0000-4000-8000-000000000000")

        # Error transitions point to callback offer gate block
        errors = {e["ErrorType"]: e["NextAction"] for e in transfer_block["Transitions"]["Errors"]}
        self.assertEqual(errors.get("ConnectionTimeLimitExceeded"), "f2000000-0000-4000-8000-000000000000")
        self.assertEqual(errors.get("CallFailed"), "f2000000-0000-4000-8000-000000000000")
        self.assertEqual(errors.get("NoMatchingError"), "f2000000-0000-4000-8000-000000000000")

    def test_direct_handoff_identity_turn_1_pass_through_nodes(self):
        data = json.loads(self.flow_path.read_text(encoding="utf-8"))
        actions = {a["Identifier"]: a for a in data["Actions"]}

        # Lex Node 1
        lex_turn1 = actions.get("a0000000-0000-4000-8000-000000000003")
        self.assertIsNotNone(lex_turn1)
        conditions = {c["Condition"]["Operands"][0]: c["NextAction"] for c in lex_turn1["Transitions"]["Conditions"]}

        # 1. IdentityConfirmed -> Pass-through Node 1 (a0000000-0000-4000-8000-000000000021) -> 90000000-0000-4000-8000-000000000005
        self.assertEqual(conditions["IdentityConfirmed"], "a0000000-0000-4000-8000-000000000021")
        pt1 = actions.get("a0000000-0000-4000-8000-000000000021")
        self.assertIsNotNone(pt1)
        self.assertEqual(pt1["Type"], "UpdateContactAttributes")
        self.assertEqual(pt1["Parameters"]["Attributes"]["identityTurn1Utterance"], "$.Lex.InputTranscript")
        self.assertEqual(pt1["Transitions"]["NextAction"], "90000000-0000-4000-8000-000000000005")
        self.assertEqual(pt1["Transitions"]["Errors"][0]["NextAction"], "90000000-0000-4000-8000-000000000005")

        # 2. IdentityNamedConfirmation -> Pass-through Node 2 (a0000000-0000-4000-8000-000000000022) -> f1000000-0000-4000-8000-000000000005
        self.assertEqual(conditions["IdentityNamedConfirmation"], "a0000000-0000-4000-8000-000000000022")
        pt2 = actions.get("a0000000-0000-4000-8000-000000000022")
        self.assertIsNotNone(pt2)
        self.assertEqual(pt2["Type"], "UpdateContactAttributes")
        self.assertEqual(pt2["Parameters"]["Attributes"]["identityTurn1Utterance"], "$.Lex.InputTranscript")
        self.assertEqual(pt2["Transitions"]["NextAction"], "f1000000-0000-4000-8000-000000000005")
        self.assertEqual(pt2["Transitions"]["Errors"][0]["NextAction"], "f1000000-0000-4000-8000-000000000005")

        # 3. IdentityAmbiguous / FallbackIntent / NextAction / Errors -> Pass-through Node 3 (a0000000-0000-4000-8000-000000000023) -> Turn 2 (a0000000-0000-4000-8000-000000000004)
        self.assertEqual(conditions["IdentityAmbiguous"], "a0000000-0000-4000-8000-000000000023")
        self.assertEqual(conditions["FallbackIntent"], "a0000000-0000-4000-8000-000000000023")
        self.assertEqual(lex_turn1["Transitions"]["NextAction"], "a0000000-0000-4000-8000-000000000023")
        for err in lex_turn1["Transitions"]["Errors"]:
            self.assertEqual(err["NextAction"], "a0000000-0000-4000-8000-000000000023")

        pt3 = actions.get("a0000000-0000-4000-8000-000000000023")
        self.assertIsNotNone(pt3)
        self.assertEqual(pt3["Type"], "UpdateContactAttributes")
        self.assertEqual(pt3["Parameters"]["Attributes"]["identityTurn1Utterance"], "$.Lex.InputTranscript")
        self.assertEqual(pt3["Transitions"]["NextAction"], "a0000000-0000-4000-8000-000000000004")
        self.assertEqual(pt3["Transitions"]["Errors"][0]["NextAction"], "a0000000-0000-4000-8000-000000000004")

    def test_direct_handoff_identity_turn_2_pass_through_nodes(self):
        data = json.loads(self.flow_path.read_text(encoding="utf-8"))
        actions = {a["Identifier"]: a for a in data["Actions"]}

        # Lex Node 2
        lex_turn2 = actions.get("a0000000-0000-4000-8000-000000000004")
        self.assertIsNotNone(lex_turn2)
        conditions = {c["Condition"]["Operands"][0]: c["NextAction"] for c in lex_turn2["Transitions"]["Conditions"]}

        # 1. IdentityConfirmed -> Pass-through Node 4 (a0000000-0000-4000-8000-000000000024) -> 90000000-0000-4000-8000-000000000005
        self.assertEqual(conditions["IdentityConfirmed"], "a0000000-0000-4000-8000-000000000024")
        pt4 = actions.get("a0000000-0000-4000-8000-000000000024")
        self.assertIsNotNone(pt4)
        self.assertEqual(pt4["Type"], "UpdateContactAttributes")
        self.assertEqual(pt4["Parameters"]["Attributes"]["identityTurn2Utterance"], "$.Lex.InputTranscript")
        self.assertEqual(pt4["Transitions"]["NextAction"], "90000000-0000-4000-8000-000000000005")
        self.assertEqual(pt4["Transitions"]["Errors"][0]["NextAction"], "90000000-0000-4000-8000-000000000005")

        # 2. IdentityNamedConfirmation -> Pass-through Node 5 (a0000000-0000-4000-8000-000000000025) -> f1000000-0000-4000-8000-000000000007
        self.assertEqual(conditions["IdentityNamedConfirmation"], "a0000000-0000-4000-8000-000000000025")
        pt5 = actions.get("a0000000-0000-4000-8000-000000000025")
        self.assertIsNotNone(pt5)
        self.assertEqual(pt5["Type"], "UpdateContactAttributes")
        self.assertEqual(pt5["Parameters"]["Attributes"]["identityTurn2Utterance"], "$.Lex.InputTranscript")
        self.assertEqual(pt5["Transitions"]["NextAction"], "f1000000-0000-4000-8000-000000000007")
        self.assertEqual(pt5["Transitions"]["Errors"][0]["NextAction"], "f1000000-0000-4000-8000-000000000007")

        # 3. IdentityAmbiguous / FallbackIntent / NoMatchingCondition -> Pass-through Node 6 (a0000000-0000-4000-8000-000000000026) -> Double-Check (10000000-0000-4000-8000-000000000007)
        self.assertEqual(conditions["IdentityAmbiguous"], "a0000000-0000-4000-8000-000000000026")
        self.assertEqual(conditions["FallbackIntent"], "a0000000-0000-4000-8000-000000000026")
        no_match = next((e for e in lex_turn2["Transitions"]["Errors"] if e["ErrorType"] == "NoMatchingCondition"), None)
        self.assertIsNotNone(no_match)
        self.assertEqual(no_match["NextAction"], "a0000000-0000-4000-8000-000000000026")

        pt6 = actions.get("a0000000-0000-4000-8000-000000000026")
        self.assertIsNotNone(pt6)
        self.assertEqual(pt6["Type"], "UpdateContactAttributes")
        self.assertEqual(pt6["Parameters"]["Attributes"]["identityTurn2Utterance"], "$.Lex.InputTranscript")
        self.assertEqual(pt6["Transitions"]["NextAction"], "10000000-0000-4000-8000-000000000007")
        self.assertEqual(pt6["Transitions"]["Errors"][0]["NextAction"], "10000000-0000-4000-8000-000000000007")


if __name__ == "__main__":
    unittest.main()

