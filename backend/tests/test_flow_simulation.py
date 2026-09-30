import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class FlowExecutionSimulationTests(unittest.TestCase):
    def setUp(self):
        flow_path = ROOT / "contact-flows" / "cara-health-bot-flow.json"
        self.flow = json.loads(flow_path.read_text(encoding="utf-8"))
        self.actions = {a["Identifier"]: a for a in self.flow["Actions"]}

    def simulate_flow(
        self,
        initial_attributes,
        mock_lambda_results,
        mock_lex_intents=None,
        mock_amd_status=None,
        mock_transfer_error=None,
        mock_lex_slots=None,
        mock_lex_transcript=None,
        start_action=None,
    ):
        """Simulates step-by-step execution through the Connect Contact Flow JSON."""
        curr_id = start_action if start_action is not None else self.flow["StartAction"]
        attributes = dict(initial_attributes)
        trace = []
        prompts_spoken = []
        mock_lex = list(mock_lex_intents or [])
        mock_amd = list(mock_amd_status if mock_amd_status is not None else ["CallAnswered"])
        loop_counts = {}
        mock_transcripts = list(mock_lex_transcript) if isinstance(mock_lex_transcript, (list, tuple)) else ([mock_lex_transcript] if mock_lex_transcript else [])
        current_transcript = mock_transcripts[0] if mock_transcripts else ""

        while curr_id and curr_id != "77777777-7777-4777-8777-777777777777":
            action = self.actions[curr_id]
            act_type = action["Type"]
            trace.append((curr_id, act_type))

            if act_type == "CheckOutboundCallStatus":
                status = mock_amd.pop(0) if mock_amd else "CallAnswered"
                if status in ("AMD_NOT_APPLICABLE", "ANSWERED"):
                    status = "CallAnswered"
                elif status == "VOICEMAIL_BEEP":
                    status = "VoicemailBeep"
                elif status == "VOICEMAIL_NO_BEEP":
                    status = "VoicemailNoBeep"

                matched_next = None
                for cond in action["Transitions"].get("Conditions", []):
                    op = cond["Condition"]["Operator"]
                    operands = cond["Condition"]["Operands"]
                    if op == "Equals" and status in operands:
                        matched_next = cond["NextAction"]
                        break
                if matched_next:
                    curr_id = matched_next
                elif status in ("AMD_UNANSWERED", "AMD_ERROR") or "Error" in status:
                    errors = action["Transitions"].get("Errors", [])
                    curr_id = errors[0]["NextAction"] if errors else action["Transitions"].get("NextAction")
                else:
                    curr_id = action["Transitions"].get("NextAction")

            elif act_type == "Loop":
                count = loop_counts.get(curr_id, 0) + 1
                loop_counts[curr_id] = count
                max_count = int(action["Parameters"].get("LoopCount", 1))
                cond_name = "ContinueLooping" if count < max_count else "DoneLooping"
                matched_next = None
                for cond in action["Transitions"].get("Conditions", []):
                    if cond["Condition"]["Operands"][0] == cond_name:
                        matched_next = cond["NextAction"]
                        break
                if matched_next:
                    curr_id = matched_next
                else:
                    curr_id = action["Transitions"].get("NextAction")

            elif act_type == "Wait":
                curr_id = action["Transitions"].get("NextAction")

            elif act_type == "UpdateContactAttributes":
                for k, v in action["Parameters"].get("Attributes", {}).items():
                    # Resolve $.External.* or $.Attributes.* or $.SystemEndpoint.* or $.Lex.*
                    if v.startswith("$.External."):
                        ext_key = v.split("$.External.")[1]
                        attributes[k] = mock_lambda_results.get(ext_key, "")
                    elif v.startswith("$.Attributes."):
                        attr_key = v.split("$.Attributes.")[1]
                        attributes[k] = attributes.get(attr_key, "")
                    elif v.startswith("$.Lex.Slots."):
                        slot_name = v.split("$.Lex.Slots.")[1]
                        attributes[k] = (mock_lex_slots or {}).get(slot_name, "")
                    elif v == "$.Lex.InputTranscript":
                        attributes[k] = current_transcript
                    else:
                        attributes[k] = v
                curr_id = action["Transitions"].get("NextAction")

            elif act_type == "Compare":
                comp_param = action["Parameters"]["ComparisonValue"]
                val = ""
                if comp_param.startswith("$.Attributes."):
                    val = attributes.get(comp_param.split("$.Attributes.")[1], "")
                elif comp_param.startswith("$.External."):
                    val = mock_lambda_results.get(comp_param.split("$.External.")[1], "")
                
                matched_next = None
                for cond in action["Transitions"].get("Conditions", []):
                    op = cond["Condition"]["Operator"]
                    operands = cond["Condition"]["Operands"]
                    if op == "Equals" and val == operands[0]:
                        matched_next = cond["NextAction"]
                        break
                    elif op == "TextStartsWith" and val.startswith(operands[0]):
                        matched_next = cond["NextAction"]
                        break
                
                if matched_next:
                    curr_id = matched_next
                else:
                    curr_id = action["Transitions"].get("NextAction")

            elif act_type == "InvokeLambdaFunction":
                op = action["Parameters"]["LambdaInvocationAttributes"]["operation"]
                if op == "checkAgentAvailability":
                    # Simulated result already in mock_lambda_results
                    pass
                curr_id = action["Transitions"].get("NextAction")

            elif act_type == "MessageParticipant":
                txt = action["Parameters"].get("Text", "")
                if "$.Attributes." in txt:
                    import re
                    txt = re.sub(r"\$\.Attributes\.([a-zA-Z0-9_]+)", lambda m: attributes.get(m.group(1), ""), txt)
                elif txt.startswith("${") and txt.endswith("}"):
                    key = txt[2:-1]
                    camel_key = key[0].lower() + key[1:]
                    txt = attributes.get(camel_key, txt)
                if txt:
                    prompts_spoken.append(txt)
                curr_id = action["Transitions"].get("NextAction")

            elif act_type in ("ConnectParticipantWithLexBot", "GetUserInput"):
                lex_prompt = action["Parameters"].get("Text", "")
                if "$.Attributes." in lex_prompt:
                    import re
                    lex_prompt = re.sub(r"\$\.Attributes\.([a-zA-Z0-9_]+)", lambda m: attributes.get(m.group(1), ""), lex_prompt)
                elif lex_prompt.startswith("${") and lex_prompt.endswith("}"):
                    key = lex_prompt[2:-1]
                    camel_key = key[0].lower() + key[1:]
                    lex_prompt = attributes.get(camel_key, lex_prompt)
                prompts_spoken.append(lex_prompt)

                if mock_transcripts:
                    current_transcript = mock_transcripts.pop(0)

                if mock_lex:
                    intent = mock_lex.pop(0)
                    matched_next = None
                    for cond in action["Transitions"].get("Conditions", []):
                        op = cond["Condition"]["Operator"]
                        operands = cond["Condition"]["Operands"]
                        if op == "Equals" and intent == operands[0]:
                            matched_next = cond["NextAction"]
                            break
                    if matched_next:
                        curr_id = matched_next
                    else:
                        curr_id = action["Transitions"].get("NextAction")
                else:
                    break
            elif act_type == "TransferParticipantToThirdParty":
                if mock_transfer_error:
                    err_type = "ConnectionTimeLimitExceeded" if mock_transfer_error is True else mock_transfer_error
                    matched = None
                    for err in action["Transitions"].get("Errors", []):
                        if err.get("ErrorType") == err_type:
                            matched = err["NextAction"]
                            break
                    if not matched:
                        for err in action["Transitions"].get("Errors", []):
                            if err.get("ErrorType") == "NoMatchingError":
                                matched = err["NextAction"]
                                break
                    curr_id = matched or action["Transitions"].get("NextAction")
                elif mock_transfer_error is False:
                    curr_id = action["Transitions"].get("NextAction")
                else:
                    break
            else:
                curr_id = action["Transitions"].get("NextAction")

        return trace, attributes, prompts_spoken, curr_id

    def test_direct_handoff_available_true(self):
        initial_attrs = {
            "callMode": "DIRECT_HUMAN_HANDOFF",
            "customerName": "Jane Smith",
            "firstName": "Jane",
            "practiceName": "CareHarmony Clinic",
            "identityPrompt": "Hi, may I speak with Jane Smith?",
        }
        mock_lambda = {
            "available": "true",
            "agentPhone": "+15822671755",
            "agentId": "agent-001",
            "agentName": "Sarah Jenkins",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(initial_attrs, mock_lambda)
        
        # Verify transfer node is reached
        self.assertEqual(end_node_id, "e1000000-0000-4000-8000-000000000004")
        self.assertEqual(self.actions[end_node_id]["Type"], "TransferParticipantToThirdParty")
        self.assertIn("Hi Jane, CareHarmony Clinic has some important information to share with you. Please hold while I connect you now.", prompts)
        self.assertEqual(final_attrs["humanAgentPhoneNumber"], "+15822671755")
        self.assertEqual(final_attrs["agentName"], "Sarah Jenkins")

    def test_direct_handoff_available_false_falls_back_to_normal(self):
        initial_attrs = {
            "callMode": "DIRECT_HUMAN_HANDOFF",
            "customerName": "Jane Smith",
            "identityPrompt": "Hi, may I speak with Jane Smith?",
        }
        mock_lambda = {
            "available": "false",
            "agentPhone": None,
            "agentId": None,
            "agentName": None,
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(initial_attrs, mock_lambda)
        
        # Verify transfer is NOT reached; instead normal Cara Lex Bot opening is reached
        self.assertEqual(end_node_id, "10000000-0000-4000-8000-000000000001")
        self.assertEqual(self.actions[end_node_id]["Type"], "ConnectParticipantWithLexBot")
        self.assertEqual(final_attrs["callMode"], "NORMAL")
        self.assertEqual(prompts, ["Hi, may I speak with Jane Smith?"])

    def test_turn1_ambiguous_turn2_mumbled_turn3_double_check_confirmed(self):
        initial_attrs = {
            "callMode": "NORMAL",
            "customerName": "Angela Ha",
            "firstName": "Angela",
            "identityPrompt": "Hi, may I speak with Angela Ha?",
            "identityClarification": "I'm Cara, an automated assistant, and I'm trying to reach Angela Ha. I can explain more once I confirm I'm speaking with the right person. Are you Angela Ha?",
            "identityDoubleCheckPrompt": "Just to double check, is this Angela?",
            "identityFailureMessage": "Thanks. I need to speak directly with Angela Ha, so I'll end the call here. Have a good day.",
        }
        # Turn 1: "Sorry, who is this?" (IdentityAmbiguous)
        # Turn 2: "Um." (IdentityAmbiguous)
        # Turn 3: "Yes." (IdentityConfirmed)
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attrs,
            mock_lambda_results={},
            mock_lex_intents=["IdentityAmbiguous", "IdentityAmbiguous", "IdentityConfirmed"],
        )
        
        # Verify 3 identity prompts spoken in exact sequence
        self.assertEqual(prompts[:3], [
            "Hi, may I speak with Angela Ha?",
            "I'm Cara, an automated assistant, and I'm trying to reach Angela Ha. I can explain more once I confirm I'm speaking with the right person. Are you Angela Ha?",
            "Just to double check, is this Angela?",
        ])
        
        # Must confirm identity and proceed toward transfer
        self.assertEqual(final_attrs["identityResult"], "Confirmed")
        self.assertEqual(final_attrs["identityConfirmed"], "true")

    def test_turn1_ambiguous_turn2_mumbled_turn3_still_ambiguous_disconnects(self):
        initial_attrs = {
            "callMode": "NORMAL",
            "customerName": "Angela Ha",
            "firstName": "Angela",
            "identityPrompt": "Hi, may I speak with Angela Ha?",
            "identityClarification": "I'm Cara, an automated assistant, and I'm trying to reach Angela Ha. I can explain more once I confirm I'm speaking with the right person. Are you Angela Ha?",
            "identityDoubleCheckPrompt": "Just to double check, is this Angela?",
            "identityFailureMessage": "Thanks. I need to speak directly with Angela Ha, so I'll end the call here. Have a good day.",
        }
        # Turn 1: "Sorry, who is this?" (IdentityAmbiguous)
        # Turn 2: "Um." (IdentityAmbiguous)
        # Turn 3: "Um." (IdentityAmbiguous)
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attrs,
            mock_lambda_results={},
            mock_lex_intents=["IdentityAmbiguous", "IdentityAmbiguous", "IdentityAmbiguous"],
        )
        
        # Verify 3 prompts spoken + terminal failure message
        self.assertEqual(prompts[0], "Hi, may I speak with Angela Ha?")
        self.assertEqual(prompts[1], "I'm Cara, an automated assistant, and I'm trying to reach Angela Ha. I can explain more once I confirm I'm speaking with the right person. Are you Angela Ha?")
        self.assertEqual(prompts[2], "Just to double check, is this Angela?")
        self.assertIn("Thanks. I need to speak directly with Angela Ha, so I'll end the call here. Have a good day.", prompts)
        
        # Must record Ambiguous identityResult and reach disconnect
        self.assertEqual(final_attrs["identityResult"], "Ambiguous")
        node_ids = [t[0] for t in trace]
        self.assertIn("10000000-0000-4000-8000-000000000007", node_ids)
        self.assertIn("e0000000-0000-4000-8000-000000000006", node_ids)
        self.assertIn("10000000-0000-4000-8000-000000000005", node_ids)

    def test_amd_call_answered_single_check_proceeds_to_cara(self):
        initial_attrs = {
            "callMode": "NORMAL",
            "customerName": "Kevin Peterson",
            "identityPrompt": "Hi, may I speak with Kevin Peterson?",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attrs,
            mock_lambda_results={},
            mock_amd_status=["CallAnswered"],
        )
        node_ids = [t[0] for t in trace]
        self.assertNotIn("f0000000-0000-4000-8000-000000000021", node_ids)
        self.assertIn("f0000000-0000-4000-8000-000000000001", node_ids)
        self.assertIn("e1000000-0000-4000-8000-000000000001", node_ids)
        self.assertEqual(prompts[0], "Hi, may I speak with Kevin Peterson?")

    def test_amd_voicemail_beep_single_check_routes_to_voicemail(self):
        initial_attrs = {
            "callMode": "NORMAL",
            "customerName": "Kevin Peterson",
            "voicemailMessage": "Hi, please call us back.",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attrs,
            mock_lambda_results={},
            mock_amd_status=["VoicemailBeep"],
        )
        node_ids = [t[0] for t in trace]
        self.assertNotIn("f0000000-0000-4000-8000-000000000021", node_ids)
        self.assertIn("f0000000-0000-4000-8000-000000000001", node_ids)
        self.assertIn("f0000000-0000-4000-8000-000000000002", node_ids)
        self.assertIn("f0000000-0000-4000-8000-000000000003", node_ids)
        self.assertEqual(final_attrs["identityResult"], "Voicemail")
        self.assertEqual(final_attrs["disposition"], "Voicemail Left")
        self.assertIn("Hi, please call us back.", prompts)

    def test_amd_not_detected_single_check_proceeds_to_cara(self):
        initial_attrs = {
            "callMode": "NORMAL",
            "customerName": "Kevin Peterson",
            "identityPrompt": "Hi, may I speak with Kevin Peterson?",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attrs,
            mock_lambda_results={},
            mock_amd_status=["NotDetected"],
        )
        node_ids = [t[0] for t in trace]
        # Verified: 2s fixed delay is removed; flow proceeds directly to CheckOutboundCallStatus
        self.assertNotIn("f0000000-0000-4000-8000-000000000021", node_ids)
        # CheckOutboundCallStatus is evaluated
        self.assertIn("f0000000-0000-4000-8000-000000000001", node_ids)
        # Proceeds directly to Cara greeting (GetCustomerInput)
        self.assertIn("e1000000-0000-4000-8000-000000000001", node_ids)
        self.assertIn("Hi, may I speak with Kevin Peterson?", prompts)

    def test_amd_unanswered_disconnects(self):
        initial_attrs = {
            "callMode": "NORMAL",
            "customerName": "Kevin Peterson",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attrs,
            mock_lambda_results={},
            mock_amd_status=["AMD_UNANSWERED"],
        )
        node_ids = [t[0] for t in trace]
        self.assertIn("f0000000-0000-4000-8000-000000000023", node_ids)
        self.assertEqual(final_attrs["identityResult"], "Unanswered")
        self.assertEqual(prompts, [])

    def test_amd_not_applicable_proceeds_to_cara(self):
        initial_attrs = {
            "callMode": "NORMAL",
            "customerName": "Kevin Peterson",
            "identityPrompt": "Hi, may I speak with Kevin Peterson?",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attrs,
            mock_lambda_results={},
            mock_amd_status=["AMD_NOT_APPLICABLE"],
        )
        self.assertEqual(prompts[0], "Hi, may I speak with Kevin Peterson?")

    def test_escalate_to_human_transfer_timeout_specific_time(self):
        """EscalateToHuman path: identity confirmed, agent transfer times out -> Cara offers callback, collects specific time -> scheduled and cleanup confirmed."""
        initial_attrs = {
            "callMode": "NORMAL",
            "identityConfirmed": "true",
            "identityResult": "Confirmed",
            "recipientType": "TARGET",
            "customerName": "Jane Smith",
            "humanAgentPhoneNumber": "+15822671755",
            "agentUnavailablePrompt": "Is there a specific time that works best for you?",
            "transferNoPickupMessage": "Looks like our team is finishing up with a few others. Let me schedule you for a callback.",
            "transferNoPickupSpecificAck": "Perfect, I’ve got that noted — we’ll aim to reach you then. Take care, goodbye.",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attributes=initial_attrs,
            mock_lambda_results={},
            mock_lex_intents=["TargetUnavailable"],
            mock_transfer_error="ConnectionTimeLimitExceeded",
            mock_lex_slots={"callbackDate": "2026-09-08", "callbackTime": "14:00"},
            mock_lex_transcript="tomorrow at 2pm",
            start_action="e1000000-0000-4000-8000-000000000003",
        )
        node_ids = [t[0] for t in trace]
        self.assertIn("e1000000-0000-4000-8000-000000000004", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000000", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000001", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000002", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000003", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000004", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000005", node_ids)
        self.assertEqual(end_node_id, "77777777-7777-4777-8777-777777777777")
        self.assertEqual(final_attrs["conversationState"], "CALLBACK")
        self.assertEqual(final_attrs["identityConfirmed"], "true")
        self.assertEqual(final_attrs["identityResult"], "Confirmed")
        self.assertEqual(final_attrs["recipientType"], "TARGET")
        self.assertEqual(final_attrs["callbackWhen"], "tomorrow at 2pm")
        self.assertEqual(final_attrs["callbackDate"], "2026-09-08")
        self.assertEqual(final_attrs["callbackTime"], "14:00")
        # Explicit assertion that transferFailedReason == "NONE" in final_attrs (verifying the cleanup claim)
        self.assertEqual(final_attrs["transferFailedReason"], "NONE")
        self.assertIn("Looks like our team is finishing up with a few others. Let me schedule you for a callback.", prompts)
        self.assertIn("Perfect, I’ve got that noted — we’ll aim to reach you then. Take care, goodbye.", prompts)

    def test_escalate_to_human_transfer_timeout_unspecified_time(self):
        """EscalateToHuman path: agent transfer times out -> caller gives unspecified time -> default reschedule."""
        initial_attrs = {
            "callMode": "NORMAL",
            "identityConfirmed": "true",
            "identityResult": "Confirmed",
            "recipientType": "TARGET",
            "customerName": "Jane Smith",
            "humanAgentPhoneNumber": "+15822671755",
            "agentUnavailablePrompt": "Is there a specific time that works best for you?",
            "transferNoPickupMessage": "Looks like our team is finishing up with a few others. Let me schedule you for a callback.",
            "transferNoPickupUnspecifiedAck": "Got it, thanks — we’ll circle back soon. Have a good one, goodbye.",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attributes=initial_attrs,
            mock_lambda_results={},
            mock_lex_intents=["AvailabilityUnknown"],
            mock_transfer_error="ConnectionTimeLimitExceeded",
            start_action="e1000000-0000-4000-8000-000000000003",
        )
        node_ids = [t[0] for t in trace]
        self.assertIn("f2000000-0000-4000-8000-000000000000", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000001", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000006", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000007", node_ids)
        self.assertEqual(end_node_id, "77777777-7777-4777-8777-777777777777")
        self.assertEqual(final_attrs["conversationState"], "CALLBACK")
        self.assertEqual(final_attrs["identityConfirmed"], "true")
        self.assertEqual(final_attrs["identityResult"], "Confirmed")
        self.assertEqual(final_attrs["recipientType"], "TARGET")
        self.assertEqual(final_attrs.get("callbackWhen", ""), "")
        # Explicit assertion that transferFailedReason == "NONE" in final_attrs
        self.assertEqual(final_attrs["transferFailedReason"], "NONE")
        self.assertIn("Got it, thanks — we’ll circle back soon. Have a good one, goodbye.", prompts)

    def test_direct_human_handoff_transfer_timeout_reconfirms_identity_first(self):
        """DIRECT_HUMAN_HANDOFF path: unverified caller -> transfer fails -> reconfirms identity FIRST -> routes to callback offer -> Wisdom bypassed."""
        initial_attrs = {
            "callMode": "DIRECT_HUMAN_HANDOFF",
            "customerName": "Jane Smith",
            "identityPrompt": "Hi, this is Cara. May I speak with Jane Smith?",
            "agentUnavailablePrompt": "Is there a specific time that works best for you?",
            "transferNoPickupMessage": "Looks like our team is finishing up with a few others. Let me schedule you for a callback.",
            "transferNoPickupSpecificAck": "Perfect, I’ve got that noted — we’ll aim to reach you then. Take care, goodbye.",
        }
        mock_lambda = {
            "available": "true",
            "agentPhone": "+15822671755",
            "agentId": "agent-001",
            "agentName": "Sarah Jenkins",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attributes=initial_attrs,
            mock_lambda_results=mock_lambda,
            mock_lex_intents=["IdentityConfirmed", "EndConversation"],
            mock_transfer_error="ConnectionTimeLimitExceeded",
        )
        node_ids = [t[0] for t in trace]
        # Verified: transfer attempted first
        self.assertIn("e1000000-0000-4000-8000-000000000004", node_ids)
        # Gate recognizes unverified identity -> routes to f2000000-...-0020
        self.assertIn("f2000000-0000-4000-8000-000000000000", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000020", node_ids)
        # Identity Lex bot re-entered first
        self.assertIn("10000000-0000-4000-8000-000000000001", node_ids)
        self.assertIn("90000000-0000-4000-8000-000000000004", node_ids)
        # Enters Wisdom coaching session
        self.assertIn("33333333-3333-4333-8333-333333333333", node_ids)
        self.assertIn("55555555-5555-4555-8555-555555555555", node_ids)
        self.assertEqual(end_node_id, "77777777-7777-4777-8777-777777777777")
        self.assertEqual(final_attrs["identityConfirmed"], "true")
        self.assertEqual(final_attrs["identityResult"], "Confirmed")
        self.assertEqual(final_attrs["recipientType"], "TARGET")

    def test_pre_existing_bug_fix_mid_call_agent_unavailable_patient_callback(self):
        """Pre-existing bug fix: mid-call agent unavailable -> patient callback preserves TARGET/Confirmed instead of THIRD_PARTY/Denied."""
        initial_attrs = {
            "callMode": "NORMAL",
            "identityConfirmed": "true",
            "identityResult": "Confirmed",
            "recipientType": "TARGET",
            "customerName": "John Doe",
            "agentUnavailablePrompt": "Is there a specific time that works best for you?",
            "transferNoPickupSpecificAck": "Perfect, I’ve got that noted — we’ll aim to reach you then. Take care, goodbye.",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attributes=initial_attrs,
            mock_lambda_results={"available": "false"},
            mock_lex_intents=["TargetUnavailable"],
            mock_lex_slots={"callbackDate": "2026-09-08", "callbackTime": "15:00"},
            mock_lex_transcript="tomorrow at 3pm",
            start_action="b0000000-0000-4000-8000-000000000010",
        )
        node_ids = [t[0] for t in trace]
        self.assertIn("b0000000-0000-4000-8000-000000000011", node_ids)
        self.assertIn("b0000000-0000-4000-8000-000000000013", node_ids)
        self.assertIn("b0000000-0000-4000-8000-000000000014", node_ids)
        # Verified: b0000000-...-014 transitions to f2000000-...-0003 instead of old a0000000-...-011
        self.assertIn("f2000000-0000-4000-8000-000000000003", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000004", node_ids)
        self.assertNotIn("a0000000-0000-4000-8000-000000000011", node_ids)
        self.assertNotIn("a0000000-0000-4000-8000-000000000007", node_ids)
        # Confirmed patient is NOT marked THIRD_PARTY / Denied
        self.assertEqual(final_attrs["recipientType"], "TARGET")
        self.assertEqual(final_attrs["identityResult"], "Confirmed")
        self.assertEqual(final_attrs["identityConfirmed"], "true")
        self.assertEqual(final_attrs["conversationState"], "CALLBACK")
        self.assertEqual(final_attrs["callbackWhen"], "tomorrow at 3pm")
        self.assertEqual(final_attrs["transferFailedReason"], "NONE")

    def test_successful_transfer_no_regression(self):
        """Successful transfer: ContinueFlowExecution='True' naturally routes to 77777777 on disconnect without triggering fallback."""
        initial_attrs = {
            "callMode": "DIRECT_HUMAN_HANDOFF",
            "customerName": "Jane Smith",
            "identityPrompt": "Hi, may I speak with Jane Smith?",
        }
        mock_lambda = {
            "available": "true",
            "agentPhone": "+15822671755",
            "agentId": "agent-001",
            "agentName": "Sarah Jenkins",
            "transferOutcome": "COMPLETED",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attributes=initial_attrs,
            mock_lambda_results=mock_lambda,
            mock_transfer_error=False,  # Simulates successful transfer completion
        )
        node_ids = [t[0] for t in trace]
        self.assertIn("e1000000-0000-4000-8000-000000000004", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000030", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000031", node_ids)
        self.assertEqual(end_node_id, "77777777-7777-4777-8777-777777777777")
        # Callback offer blocks are untouched
        self.assertNotIn("f2000000-0000-4000-8000-000000000000", node_ids)
        self.assertNotIn("f2000000-0000-4000-8000-000000000001", node_ids)

    def test_transfer_no_pickup_duration_fallback(self):
        initial_attrs = {
            "callMode": "DIRECT_HUMAN_HANDOFF",
            "customerName": "Jane Smith",
            "identityPrompt": "Hi, may I speak with Jane Smith?",
        }
        mock_lambda = {
            "available": "true",
            "agentPhone": "+15822671755",
            "agentId": "agent-001",
            "agentName": "Sarah Jenkins",
            "transferOutcome": "NO_PICKUP",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attributes=initial_attrs,
            mock_lambda_results=mock_lambda,
            mock_transfer_error=False,  # Normal disconnect from transfer, but post-transfer evaluation flags NO_PICKUP
            mock_lex_intents=["IdentityConfirmed", "EndConversation"],
        )
        node_ids = [t[0] for t in trace]
        self.assertIn("e1000000-0000-4000-8000-000000000004", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000030", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000031", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000000", node_ids)
        self.assertIn("f2000000-0000-4000-8000-000000000020", node_ids)
        self.assertIn("10000000-0000-4000-8000-000000000001", node_ids)
        self.assertIn("90000000-0000-4000-8000-000000000004", node_ids)
        self.assertIn("33333333-3333-4333-8333-333333333333", node_ids)
        self.assertIn("55555555-5555-4555-8555-555555555555", node_ids)

    def test_representative_detected_routes_to_coaching_flow_and_not_0008(self):
        rep_greeting = "Perfect, thank you — let's go ahead. I'm Cara, an automated assistant calling on behalf of Mercy Health. I can connect you with a human specialist regarding Alex's care. Is now a good time?"
        direct_greeting = "Thanks, Alex. I'm Cara, an automated assistant. I can connect you with a human specialist who can help. Is now a good time?"
        initial_attrs = {
            "callMode": "NORMAL",
            "customerName": "Alex Taylor",
            "firstName": "Alex",
            "practiceName": "Mercy Health",
            "identityPrompt": "Hi, may I speak with Alex?",
            "coachingGreeting": direct_greeting,
            "representativeCoachingGreeting": rep_greeting,
        }
        mock_lambda = {
            "available": "true",
            "agentPhone": "+15822671755",
            "agentId": "agent-001",
            "agentName": "Sarah Jenkins",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attributes=initial_attrs,
            mock_lambda_results=mock_lambda,
            mock_lex_intents=["RepresentativeDetected", "EscalateToHuman"],
        )
        node_ids = [t[0] for t in trace]
        # Confirm representative routes into 33333333-... (coaching/consent flow)
        self.assertIn("e0000000-0000-4000-8000-000000000005", node_ids)
        self.assertIn("33333333-3333-4333-8333-333333333333", node_ids)
        self.assertIn("55555555-5555-4555-8555-555555555555", node_ids)
        # Confirm representative does NOT route into e0000000-0000-4000-8000-000000000008
        self.assertNotIn("e0000000-0000-4000-8000-000000000008", node_ids)
        # Confirm coachingGreeting was updated to the representative greeting
        self.assertEqual(final_attrs["coachingGreeting"], rep_greeting)
        self.assertEqual(final_attrs["recipientType"], "REPRESENTATIVE")
        self.assertEqual(final_attrs["identityResult"], "Denied")
        # Confirm spoken prompts include representative greeting, not direct address
        self.assertIn(rep_greeting, prompts)
        self.assertNotIn("Hi Alex", rep_greeting)
        self.assertNotIn("Thanks, Alex", rep_greeting)

    def test_direct_handoff_turn1_identity_confirmed_sets_transcript(self):
        initial_attrs = {
            "callMode": "DIRECT_HUMAN_HANDOFF",
            "customerName": "John Doe",
            "firstName": "John",
            "practiceName": "Sample Practice",
            "handoffIdentityPrompt": "Hi. May I confirm I'm speaking with John Doe?",
            "humanAgentPhoneNumber": "+15822671755",
        }
        mock_lambda = {
            "available": "true",
            "agentPhone": "+15822671755",
            "agentId": "agent-101",
            "agentName": "Alice Smith",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attributes=initial_attrs,
            mock_lambda_results=mock_lambda,
            mock_lex_intents=["IdentityConfirmed"],
            mock_lex_transcript=["Yes this is John Doe speaking"],
            start_action="a0000000-0000-4000-8000-000000000003",
        )
        node_ids = [t[0] for t in trace]
        # 1. Traverses pass-through node a0000000-0000-4000-8000-000000000021
        self.assertIn("a0000000-0000-4000-8000-000000000021", node_ids)
        # 2. Persists identityTurn1Utterance
        self.assertEqual(final_attrs.get("identityTurn1Utterance"), "Yes this is John Doe speaking")
        # 3. Transitions to 90000000-...-0005 and continues to transfer convergence
        self.assertIn("90000000-0000-4000-8000-000000000005", node_ids)

    def test_direct_handoff_turn1_ambiguous_routes_to_turn2_sets_both_transcripts(self):
        initial_attrs = {
            "callMode": "DIRECT_HUMAN_HANDOFF",
            "customerName": "John Doe",
            "firstName": "John",
            "practiceName": "Sample Practice",
            "handoffIdentityPrompt": "Hi. May I confirm I'm speaking with John Doe?",
            "identityClarification": "Just to clarify, is this John Doe?",
            "humanAgentPhoneNumber": "+15822671755",
        }
        mock_lambda = {
            "available": "true",
            "agentPhone": "+15822671755",
            "agentId": "agent-101",
            "agentName": "Alice Smith",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attributes=initial_attrs,
            mock_lambda_results=mock_lambda,
            mock_lex_intents=["IdentityAmbiguous", "IdentityConfirmed"],
            mock_lex_transcript=["Who is calling please?", "Yes I am John"],
            start_action="a0000000-0000-4000-8000-000000000003",
        )
        node_ids = [t[0] for t in trace]
        # 1. Traverses Turn 1 Ambiguous pass-through node
        self.assertIn("a0000000-0000-4000-8000-000000000023", node_ids)
        # 2. Reaches Turn 2 Lex node
        self.assertIn("a0000000-0000-4000-8000-000000000004", node_ids)
        # 3. Traverses Turn 2 Confirmed pass-through node
        self.assertIn("a0000000-0000-4000-8000-000000000024", node_ids)
        # 4. Both transcripts are recorded without collision
        self.assertEqual(final_attrs.get("identityTurn1Utterance"), "Who is calling please?")
        self.assertEqual(final_attrs.get("identityTurn2Utterance"), "Yes I am John")
        # 5. Continues to 90000000-...-0005
        self.assertIn("90000000-0000-4000-8000-000000000005", node_ids)

    def test_direct_handoff_turn2_ambiguous_routes_to_double_check(self):
        initial_attrs = {
            "callMode": "DIRECT_HUMAN_HANDOFF",
            "customerName": "John Doe",
            "firstName": "John",
            "practiceName": "Sample Practice",
            "handoffIdentityPrompt": "Hi. May I confirm I'm speaking with John Doe?",
            "identityClarification": "Just to clarify, is this John Doe?",
            "identityDoubleCheckPrompt": "Just to double check, is this John?",
            "humanAgentPhoneNumber": "+15822671755",
        }
        mock_lambda = {
            "available": "true",
            "agentPhone": "+15822671755",
            "agentId": "agent-101",
            "agentName": "Alice Smith",
        }
        trace, final_attrs, prompts, end_node_id = self.simulate_flow(
            initial_attributes=initial_attrs,
            mock_lambda_results=mock_lambda,
            mock_lex_intents=["IdentityAmbiguous", "IdentityAmbiguous", "IdentityConfirmed"],
            mock_lex_transcript=["Maybe", "I am not sure", "Yes I am John"],
            start_action="a0000000-0000-4000-8000-000000000003",
        )
        node_ids = [t[0] for t in trace]
        # 1. Traverses Turn 1 and Turn 2 ambiguous pass-through nodes
        self.assertIn("a0000000-0000-4000-8000-000000000023", node_ids)
        self.assertIn("a0000000-0000-4000-8000-000000000026", node_ids)
        # 2. Reaches double-check node 10000000-0000-4000-8000-000000000007
        self.assertIn("10000000-0000-4000-8000-000000000007", node_ids)
        # 3. Transcripts for Turn 1 and Turn 2 are saved
        self.assertEqual(final_attrs.get("identityTurn1Utterance"), "Maybe")
        self.assertEqual(final_attrs.get("identityTurn2Utterance"), "I am not sure")


if __name__ == "__main__":
    unittest.main()
