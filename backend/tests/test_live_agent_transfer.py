"""Live transfer: when the patient agrees, the gateway dials the human agent
and joins the two calls. Before this, Cara said "please hold" and the line
went silent (call 1eeaa783) while the record was marked TRANSFERRED."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.gateway import nova_sonic_service
from backend.gateway.nova_sonic_service import NovaSonicSession
from backend.gateway.sip_transport import (
    BRIDGE_SSRC,
    CallRTPMediaSession,
    RTPPacket,
    SIPMessage,
    SIPRTPMediaServer,
    SIPSignalingServer,
)

PATIENT = "patient-leg"
AGENT_PHONE = "+12132960390"


def _media_session(call_id, remote):
    s = CallRTPMediaSession(contact_id=call_id, local_port=0, remote_addr=remote)
    s._is_active = True
    s.transport = MagicMock()
    s.transport.is_closing.return_value = False
    return s


def _server():
    media = SIPRTPMediaServer()
    media.call_sessions[PATIENT] = _media_session(PATIENT, ("1.1.1.1", 4000))
    sig = SIPSignalingServer(media_server=media)
    sig._is_running = True
    sig.transport = MagicMock()
    sig.active_dialogs[PATIENT] = {"state": "CONNECTED", "source": "+12819568434"}
    return sig, media


def _screen(verdict):
    return AsyncMock(return_value=verdict)


def _fake_originate(sig, media, answer):
    async def _originate(destination_phone, source_phone, patient_meta, answer_future, ring_timeout):
        agent_id = "agent-leg"
        media.call_sessions[agent_id] = _media_session(agent_id, ("2.2.2.2", 5000))
        sig.active_dialogs[agent_id] = {"state": "CONNECTED" if answer else "RINGING",
                                        "answer_future": answer_future}
        answer_future.set_result(answer)
        return agent_id
    return _originate


class BridgeTests(unittest.TestCase):
    def test_agent_answers_and_audio_flows_both_ways(self):
        sig, media = _server()

        async def main():
            with patch.object(sig, "originate_call", side_effect=_fake_originate(sig, media, True)),                     patch.object(sig, "_screen_agent_answer", new=_screen("HUMAN")):
                return await sig.dial_agent_and_bridge(PATIENT, AGENT_PHONE)

        self.assertTrue(asyncio.run(main()))
        patient, agent = media.call_sessions[PATIENT], media.call_sessions["agent-leg"]
        pkt = RTPPacket.pack(payload=b"\x7f" * 160, seq_num=1, timestamp=160, ssrc=1, payload_type=0)

        patient.handle_inbound_rtp(pkt, ("1.1.1.1", 4000))
        sent, addr = agent.transport.sendto.call_args[0]
        self.assertEqual(addr, ("2.2.2.2", 5000))
        self.assertEqual(RTPPacket.unpack(sent)[3], BRIDGE_SSRC)

        agent.handle_inbound_rtp(pkt, ("2.2.2.2", 5000))
        self.assertEqual(patient.transport.sendto.call_args[0][1], ("1.1.1.1", 4000))

    def test_cara_audio_is_muted_once_bridged(self):
        sig, media = _server()

        async def main():
            with patch.object(sig, "originate_call", side_effect=_fake_originate(sig, media, True)),                     patch.object(sig, "_screen_agent_answer", new=_screen("HUMAN")):
                await sig.dial_agent_and_bridge(PATIENT, AGENT_PHONE)

        asyncio.run(main())
        patient = media.call_sessions[PATIENT]
        patient.transport.sendto.reset_mock()
        media.send_rtp_packet(b"\xff" * 160, PATIENT, 5, 800)
        patient.transport.sendto.assert_not_called()

    def test_no_answer_leaves_patient_call_alone(self):
        sig, media = _server()

        async def main():
            with patch.object(sig, "originate_call", side_effect=_fake_originate(sig, media, False)):
                return await sig.dial_agent_and_bridge(PATIENT, AGENT_PHONE)

        self.assertFalse(asyncio.run(main()))
        self.assertIsNone(media.call_sessions[PATIENT].bridge_peer)
        self.assertIn(PATIENT, sig.active_dialogs)
        self.assertNotIn("agent-leg", sig.agent_legs)

    def test_voicemail_on_agent_side_is_not_bridged(self):
        sig, media = _server()

        async def main():
            with patch.object(sig, "originate_call", side_effect=_fake_originate(sig, media, True)),                     patch.object(sig, "_screen_agent_answer", new=_screen("MACHINE")),                     patch.object(sig, "send_bye", new=AsyncMock()) as send_bye:
                ok = await sig.dial_agent_and_bridge(PATIENT, AGENT_PHONE)
                return ok, send_bye

        ok, send_bye = asyncio.run(main())
        self.assertFalse(ok)
        self.assertIsNone(media.call_sessions[PATIENT].bridge_peer)
        send_bye.assert_awaited_once_with("agent-leg")

    def test_silent_agent_pickup_is_bridged(self):
        # Call 10316d32: the agent picked up and waited quietly; we hung up on them
        sig, media = _server()

        async def main():
            with patch.object(sig, "originate_call", side_effect=_fake_originate(sig, media, True)), \
                    patch.object(sig, "_screen_agent_answer", new=_screen("SILENCE")), \
                    patch.object(sig, "send_bye", new=AsyncMock()) as send_bye:
                ok = await sig.dial_agent_and_bridge(PATIENT, AGENT_PHONE)
                return ok, send_bye

        ok, send_bye = asyncio.run(main())
        self.assertTrue(ok)
        self.assertIsNotNone(media.call_sessions[PATIENT].bridge_peer)
        send_bye.assert_not_awaited()

    def test_rejected_invite_is_acked(self):
        sig, media = _server()
        sig.handle_inbound_sip(
            b"SIP/2.0 603 Decline\r\nVia: SIP/2.0/UDP 3.90.83.166:5060;branch=z9hG4bK-1\r\n"
            b"From: <sip:+12819568434@vc>;tag=a\r\nTo: <sip:+12132960390@vc>;tag=b\r\n"
            b"Call-ID: gone\r\nCSeq: 1 INVITE\r\nContent-Length: 0\r\n\r\n",
            ("3.80.16.27", 5060),
        )
        sent, addr = sig.transport.sendto.call_args[0]
        self.assertTrue(sent.startswith(b"ACK sip:+12132960390@vc SIP/2.0"))
        self.assertIn(b"CSeq: 1 ACK", sent)
        self.assertIn(b"branch=z9hG4bK-1", sent)

    def test_screening_hears_hello_over_rtp(self):
        import numpy as np
        from backend.gateway.sip_transport import ulaw_encode
        sig, media = _server()
        media.call_sessions["agent-leg"] = agent = _media_session("agent-leg", ("2.2.2.2", 5000))
        t = np.arange(int(8000 * 0.7)) / 8000
        voice = sum(np.sin(2 * np.pi * 140 * k * t) / k ** 0.5 for k in range(1, 21))
        hello = (voice / np.max(np.abs(voice)) * 5000).astype(np.int16).tobytes()
        audio = hello + bytes(4 * 8000)

        async def main():
            loop = asyncio.get_running_loop()
            screen = asyncio.create_task(sig._screen_agent_answer("agent-leg", loop.time() + 25))
            await asyncio.sleep(0)
            for n, i in enumerate(range(0, len(audio), 320)):
                pkt = RTPPacket.pack(payload=ulaw_encode(audio[i:i + 320]), seq_num=n,
                                     timestamp=n * 160, ssrc=7, payload_type=0)
                agent.handle_inbound_rtp(pkt, ("2.2.2.2", 5000))
                if screen.done():
                    break
            return await screen

        self.assertEqual(asyncio.run(main()), "HUMAN")

    def test_agent_leg_failure_does_not_touch_patient_record(self):
        sig, media = _server()
        sig.on_call_failed = MagicMock()
        sig.on_call_ended = AsyncMock()

        async def main():
            fut = asyncio.get_running_loop().create_future()
            dialog = {"answer_future": fut, "patient_id": ""}
            sig.active_dialogs["agent-leg"] = dialog
            await sig._finalize_call_failure("agent-leg", dialog, reason="BUSY", sip_code=486)
            return fut.result()

        self.assertFalse(asyncio.run(main()))
        sig.on_call_failed.assert_not_called()
        sig.on_call_ended.assert_not_awaited()

    def test_agent_hangup_ends_patient_call(self):
        sig, media = _server()
        sig.agent_legs["agent-leg"] = PATIENT
        sig.active_dialogs["agent-leg"] = {"state": "CONNECTED"}
        sig.on_call_ended = AsyncMock()
        bye = SIPMessage.parse(
            b"BYE sip:cara@1.2.3.4 SIP/2.0\r\nVia: SIP/2.0/UDP 2.2.2.2\r\nFrom: <sip:a>;tag=1\r\n"
            b"To: <sip:b>;tag=2\r\nCall-ID: agent-leg\r\nCSeq: 2 BYE\r\nContent-Length: 0\r\n\r\n"
        )
        self.assertEqual(bye.method, "BYE")

        async def main():
            with patch.object(sig, "send_bye", new=AsyncMock()) as send_bye:
                sig.handle_inbound_sip(
                    b"BYE sip:cara@1.2.3.4 SIP/2.0\r\nVia: SIP/2.0/UDP 2.2.2.2\r\nFrom: <sip:a>;tag=1\r\n"
                    b"To: <sip:b>;tag=2\r\nCall-ID: agent-leg\r\nCSeq: 2 BYE\r\nContent-Length: 0\r\n\r\n",
                    ("2.2.2.2", 5060),
                )
                await asyncio.sleep(0.05)
                return send_bye

        send_bye = asyncio.run(main())
        send_bye.assert_awaited_once_with(PATIENT)


class NovaTransferTests(unittest.TestCase):
    def setUp(self):
        synth = patch.object(nova_sonic_service, "_synthesize_forced_closing_helper",
                             AsyncMock(return_value=b"hold-audio"))
        self.synth = synth.start()
        self.addCleanup(synth.stop)

    def _session(self, answered):
        s = NovaSonicSession(MagicMock(), contact_id=PATIENT, on_hangup=AsyncMock(),
                             on_forced_audio=AsyncMock(), on_transfer=AsyncMock(return_value=answered),
                             tool_executor=MagicMock(reschedule_call=AsyncMock(return_value={"success": True})))
        s.is_active = True
        s.end = AsyncMock()
        s._initiate_hangup = AsyncMock()
        return s

    def test_answered_marks_transferred_and_closes_nova(self):
        s = self._session(True)
        s._send_tool_result = AsyncMock()
        asyncio.run(s._run_transfer("tool-1", AGENT_PHONE))
        self.assertEqual(s.final_disposition, "TRANSFERRED")
        s.end.assert_awaited_once()
        s._send_tool_result.assert_not_awaited()

    def test_no_answer_hands_the_call_back_to_cara(self):
        s = self._session(False)
        s._transfer_pending = True
        s._send_tool_result = AsyncMock()
        asyncio.run(s._run_transfer("tool-1", AGENT_PHONE))

        s._send_tool_result.assert_awaited_once()
        use_id, tool, result = s._send_tool_result.call_args[0]
        self.assertEqual((use_id, tool, result["status"]), ("tool-1", "evaluate_and_transfer_pes", "agent_no_answer"))
        self.assertFalse(s._transfer_pending)  # Cara hears the patient again
        self.assertNotEqual(s.final_disposition, "TRANSFERRED")
        s.end.assert_not_awaited()
        s._initiate_hangup.assert_not_awaited()
        s.tool_executor.reschedule_call.assert_not_awaited()  # Cara books it in conversation

    def test_transfer_tool_result_waits_for_the_agent(self):
        import json
        from types import SimpleNamespace
        s = self._session(False)
        s.tool_executor.evaluate_and_transfer_pes = AsyncMock(
            return_value={"success": True, "status": "transferring", "agent_phone": AGENT_PHONE})
        s._send_tool_result = AsyncMock()
        s._run_transfer = AsyncMock()
        payload = json.dumps({"event": {"toolUse": {
            "toolUseId": "tool-1", "toolName": "evaluate_and_transfer_pes", "content": "{}"}}}).encode()

        class _Stream:
            input_stream = SimpleNamespace(send=AsyncMock(), close=AsyncMock())

            async def await_output(self):
                async def gen():
                    yield SimpleNamespace(value=SimpleNamespace(bytes_=payload))
                return None, gen()

        async def main():
            s.stream = _Stream()
            await s._read_responses()
            await asyncio.sleep(0)

        asyncio.run(main())
        s._send_tool_result.assert_not_awaited()  # held until the agent answers or not
        s._run_transfer.assert_awaited_once_with("tool-1", AGENT_PHONE, spoken_text="")
        self.assertTrue(s._transfer_pending)

    def test_hold_line_played_when_nova_skipped_it(self):
        # Calls 9af65a8d / 60364398: tool called straight after "yes", patient heard silence
        s = self._session(False)
        s._send_tool_result = AsyncMock()
        asyncio.run(s._run_transfer("tool-1", AGENT_PHONE, spoken_text=""))
        self.assertIn("Please hold", self.synth.call_args[0][0])
        s.on_forced_audio.assert_awaited_once_with(b"hold-audio")
        s.on_transfer.assert_awaited_once_with(AGENT_PHONE)

    def test_hold_line_not_repeated_when_nova_said_it(self):
        s = self._session(True)
        asyncio.run(s._run_transfer("tool-1", AGENT_PHONE,
                                    spoken_text="Please hold while I connect you now."))
        self.synth.assert_not_awaited()
        s.on_forced_audio.assert_not_awaited()

    def test_transfer_still_dials_when_hold_line_fails(self):
        self.synth.side_effect = RuntimeError("polly down")
        s = self._session(True)
        asyncio.run(s._run_transfer("tool-1", AGENT_PHONE))
        s.on_transfer.assert_awaited_once_with(AGENT_PHONE)
        self.assertEqual(s.final_disposition, "TRANSFERRED")

    def test_direct_call_switches_to_normal_once_then_offers_callback(self):
        from backend.domain_rules import CallMode, PatientContext
        s = self._session(False)
        s.patient = PatientContext(patient_id="p1", first_name="Tim", last_name="Cook",
                                   practice_name="X", call_mode=CallMode.MODE_1_DIRECT_HANDOFF)
        s._send_tool_result = AsyncMock()
        asyncio.run(s._run_transfer("tool-1", AGENT_PHONE))
        self.assertEqual(s._send_tool_result.call_args[0][2]["status"], "switch_to_normal_call")
        # second missed transfer on the same call: normal callback branch
        asyncio.run(s._run_transfer("tool-2", AGENT_PHONE))
        self.assertEqual(s._send_tool_result.call_args[0][2]["status"], "agent_no_answer")

    def test_direct_call_already_in_normal_script_gets_plain_no_answer(self):
        # Call 10316d32: Cara ran the identity check on a direct call; "switch to
        # normal" then left her saying "please hold" and going silent
        from backend.domain_rules import CallMode, PatientContext
        s = self._session(False)
        s.patient = PatientContext(patient_id="p1", first_name="Tim", last_name="Cook",
                                   practice_name="X", call_mode=CallMode.MODE_1_DIRECT_HANDOFF)
        s._normal_script_spoken = True
        s._send_tool_result = AsyncMock()
        asyncio.run(s._run_transfer("tool-1", AGENT_PHONE))
        self.assertEqual(s._send_tool_result.call_args[0][2]["status"], "agent_no_answer")

    def test_normal_script_lines_are_noticed(self):
        self.assertTrue(any(m in "hi, this is cara. am i able to speak with tim cook?"
                            for m in nova_sonic_service._NORMAL_SCRIPT_MARKERS))
        direct_line = "hi tim, x has some important information to share with you. please hold while i connect you now."
        self.assertFalse(any(m in direct_line for m in nova_sonic_service._NORMAL_SCRIPT_MARKERS))

    def test_callback_closing_without_tool_is_booked_by_gateway(self):
        s = self._session(False)
        s._last_user_text = "you can call me anytime"

        async def main():
            await s._arm_closing_hangup("Got it, thanks — we’ll circle back soon. Have a good one, goodbye.")
            await asyncio.sleep(1.7)

        asyncio.run(main())
        self.assertEqual(s.final_disposition, "RESCHEDULED")
        s.tool_executor.reschedule_call.assert_awaited_once()
        self.assertEqual(s.tool_executor.reschedule_call.call_args[1]["preferred_time"], "unspecified")

    def test_callback_closing_not_booked_twice(self):
        s = self._session(False)

        async def main():
            await s._arm_closing_hangup("Perfect, I've got that noted - we'll aim to reach you then. Take care, goodbye.")
            s._reschedule_done = True  # Nova's own reschedule_call landed
            await asyncio.sleep(1.7)

        asyncio.run(main())
        s.tool_executor.reschedule_call.assert_not_awaited()

    def test_prompt_has_the_no_answer_script(self):
        from backend.domain_rules import PatientContext, generate_nova_sonic_system_prompt
        prompt = generate_nova_sonic_system_prompt(PatientContext(
            patient_id="p1", first_name="Tim", last_name="Cook", practice_name="X"))
        self.assertIn('status="agent_no_answer"', prompt)
        self.assertIn("Looks like our team is finishing up with a few others. "
                      "Let me schedule you for a callback.", prompt)

    def test_patient_audio_not_sent_to_nova_while_agent_rings(self):
        s = self._session(True)
        s.stream = MagicMock()
        s._send_event = AsyncMock()
        s._transfer_pending = True
        asyncio.run(s.push_caller_audio(b"\x00" * 320))
        s._send_event.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
