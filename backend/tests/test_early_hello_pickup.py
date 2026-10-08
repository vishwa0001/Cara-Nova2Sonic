"""The patient's first "hello" must reach Cara, and a silent line must not stay silent.

Call aa078271: Cara was built only after the 200 OK, caller audio from the
first ~0.9 s was dropped, and Cara then waited ~7.5 s for a hello she never
heard. The gateway now builds Cara while the phone rings, keeps caller audio
from pickup, and opens the call itself if the caller stays quiet.
"""

import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

os.environ.setdefault("PUBLIC_IP", "127.0.0.1")  # no checkip lookup on import

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from domain_rules import CALL_CONNECTED_CUE, CallMode, PatientContext, generate_nova_sonic_system_prompt
from gateway import pipeline as pipeline_module
from gateway import sip_transport
from gateway.caller_speech import CallerSpeechDetector
from gateway.nova_sonic_service import VOICEMAIL_GREETING_PATTERNS, NovaSonicSession
from gateway.sip_transport import (
    CallRTPMediaSession,
    RTPPacket,
    SIPRTPMediaServer,
    SIPSignalingServer,
    ulaw_decode,
    ulaw_encode,
)

DISCLOSURE_WAV = ROOT / "assets" / "call_recording_disclosure_pcmu_8k.wav"


def _rtp(seq: int, level: int) -> bytes:
    pcm = np.full(160, level, dtype=np.int16).tobytes()
    return RTPPacket.pack(payload=ulaw_encode(pcm), seq_num=seq, timestamp=seq * 160, ssrc=1, payload_type=0)


def _decoded(level: int) -> bytes:
    return ulaw_decode(ulaw_encode(np.full(160, level, dtype=np.int16).tobytes()))


class _FakePipeline:
    def __init__(self):
        self.frames = []

    async def push_audio(self, pcm):
        self.frames.append(pcm)


class EarlyCallerAudioTests(unittest.IsolatedAsyncioTestCase):
    def _session(self):
        s = CallRTPMediaSession("call-1", 10000, ("127.0.0.1", 5000))
        s._is_active = True
        return s

    async def test_ringback_before_answer_is_never_kept(self):
        s = self._session()
        for seq in range(10):
            s.handle_inbound_rtp(_rtp(seq, 1000), ("127.0.0.1", 5000))
        self.assertEqual(len(s._inbound), 0)

    async def test_audio_after_answer_is_held_in_order_and_capped(self):
        s = self._session()
        s.answered = True
        total = sip_transport.EARLY_AUDIO_MAX_FRAMES + 10
        for seq in range(total):
            s.handle_inbound_rtp(_rtp(seq, seq * 10), ("127.0.0.1", 5000))
        self.assertEqual(len(s._inbound), sip_transport.EARLY_AUDIO_MAX_FRAMES)
        self.assertEqual(s.early_frames_dropped, 10)
        self.assertEqual(s._inbound[0], _decoded(10 * 10))  # oldest 10 dropped

    async def test_held_audio_reaches_the_pipeline_first_and_in_order(self):
        s = self._session()
        s.answered = True
        for seq in range(5):
            s.handle_inbound_rtp(_rtp(seq, seq * 100), ("127.0.0.1", 5000))
        pipeline = _FakePipeline()
        s.attach_pipeline(pipeline)
        for seq in range(5, 8):
            s.handle_inbound_rtp(_rtp(seq, seq * 100), ("127.0.0.1", 5000))
        await asyncio.sleep(0.05)
        self.assertEqual(pipeline.frames, [_decoded(i * 100) for i in range(8)])
        s.stop()


class PrepareWhileRingingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.media = SIPRTPMediaServer(host="127.0.0.1", port=10030)
        self.prepared = []

        async def _prepare(call_id, meta):
            self.prepared.append((call_id, meta))

        self.sip = SIPSignalingServer(
            host="127.0.0.1", port=15070, media_server=self.media,
            on_call_started=AsyncMock(), on_call_preparing=_prepare,
        )

    async def asyncTearDown(self):
        for call_id in list(self.sip.active_dialogs):
            task = self.sip.active_dialogs[call_id].get("ring_timeout_task")
            if task:
                task.cancel()
        for call_id in list(self.media.call_sessions):
            await self.media.close_call_session(call_id)

    async def test_patient_call_is_prepared_when_dialled(self):
        call_id = await self.sip.originate_call(
            "+15550001111", vc_host="127.0.0.1", patient_meta={"patient_id": "p-1", "first_name": "Tim"},
        )
        await asyncio.sleep(0)
        self.assertEqual(self.prepared, [(call_id, {"patient_id": "p-1", "first_name": "Tim"})])

    async def test_agent_leg_is_never_prepared(self):
        await self.sip.originate_call(
            "+15550002222", vc_host="127.0.0.1",
            patient_meta={"agent_leg_for": "x"}, answer_future=asyncio.get_running_loop().create_future(),
        )
        await asyncio.sleep(0)
        self.assertEqual(self.prepared, [])

    async def test_200_ok_lets_caller_audio_through(self):
        call_id = await self.sip.originate_call("+15550003333", vc_host="127.0.0.1", patient_meta={"patient_id": "p-3"})
        session = self.media.call_sessions[call_id]
        self.assertFalse(session.answered)
        ok = (
            "SIP/2.0 200 OK\r\n"
            "Via: SIP/2.0/UDP 127.0.0.1:5060\r\n"
            "From: <sip:+12819568434@127.0.0.1>;tag=a\r\n"
            "To: <sip:+15550003333@127.0.0.1>;tag=b\r\n"
            f"Call-ID: {call_id}\r\n"
            "CSeq: 1 INVITE\r\n"
            "Contact: <sip:+15550003333@127.0.0.1:5060>\r\n"
            "Content-Type: application/sdp\r\n\r\n"
            "v=0\r\nc=IN IP4 127.0.0.2\r\nm=audio 4000 RTP/AVP 0\r\n"
        ).encode()
        self.sip._is_running = True
        self.sip.handle_inbound_sip(ok, ("127.0.0.1", 5060))
        await asyncio.sleep(0)
        self.assertTrue(session.answered)
        self.sip.on_call_started.assert_awaited_once()


def _patient(mode=CallMode.MODE_2_NORMAL_COACHING):
    return PatientContext(
        patient_id="ceb19108", first_name="Tim", last_name="Cook",
        practice_name="Tia Sahu", phone_number="+15550000000", call_mode=mode,
    )


def _pipeline(answered=True, mode=CallMode.MODE_2_NORMAL_COACHING):
    with patch.object(pipeline_module, "OutreachRecordService", MagicMock()), \
            patch("gateway.tools.get_connect_client", MagicMock()):
        p = pipeline_module.CaraVoicePipeline(
            patient_context=_patient(mode), call_id="call-x",
            media_server=SIPRTPMediaServer(host="127.0.0.1", port=10040),
            sip_signaling_server=MagicMock(send_bye=AsyncMock()),
            answered=answered,
        )
    p.task = SimpleNamespace(queue_frame=AsyncMock(), cancel=AsyncMock())
    p._play_opening_disclosure = AsyncMock()
    return p


class _HoldingRunner:
    """Stands in for the Pipecat runner: runs until cancelled."""
    def __init__(self):
        self.stop = asyncio.Event()

    async def run(self, task):
        await self.stop.wait()


class PipelineAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_ringing_feeds_silence_then_held_audio_before_disclosure(self):
        p = _pipeline(answered=False)
        runner = p.runner = _HoldingRunner()
        p._open_if_caller_silent = AsyncMock()
        run = asyncio.create_task(p.run())
        await asyncio.sleep(0.25)
        sent = [c.args[0].audio for c in p.task.queue_frame.await_args_list]
        self.assertTrue(sent and all(a == pipeline_module.RINGING_SILENCE_FRAME for a in sent))
        p._play_opening_disclosure.assert_not_awaited()

        await p.push_audio(b"\x01\x00" * 160)  # caller audio arriving at pickup
        await p.push_audio(b"\x02\x00" * 160)
        p.mark_answered()
        await asyncio.sleep(0.05)
        sent = [c.args[0].audio for c in p.task.queue_frame.await_args_list]
        caller = [a for a in sent if a != pipeline_module.RINGING_SILENCE_FRAME]
        self.assertEqual(caller, [b"\x01\x00" * 160, b"\x02\x00" * 160])
        p._play_opening_disclosure.assert_awaited_once()
        runner.stop.set()
        await run

    async def test_unanswered_call_is_torn_down_without_an_outcome(self):
        p = _pipeline(answered=False)
        p.runner = _HoldingRunner()
        run = asyncio.create_task(p.run())
        await asyncio.sleep(0.05)
        p.runner.stop.set()
        await p.stop()
        await asyncio.wait_for(run, 1)
        p._play_opening_disclosure.assert_not_awaited()
        p.storage.schedule_retry.assert_not_called()
        p.storage.update_call_outcome.assert_not_called()


class _FakeSpeech:
    def __init__(self):
        self.speaking = False
        self.last_speech_at = None

    def quiet_for(self, now, since):
        start = since if self.last_speech_at is None else max(since, self.last_speech_at)
        return max(0.0, now - start)


class _FakeNova:
    def __init__(self):
        self.waiting = True
        self.send_opening_cue = AsyncMock()
        self.note_forced_opening = MagicMock()

    def awaiting_opening(self):
        return self.waiting


class OpeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, value in (
            ("OPENING_SILENCE_S", 0.1), ("OPENING_AFTER_SPEECH_S", 0.25),
            ("OPENING_CAP_S", 0.4), ("OPENING_CAP_PAUSE_S", 0.05), ("OPENING_POLL_S", 0.01),
        ):
            p = patch.object(pipeline_module, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _pipeline(self, mode=CallMode.MODE_2_NORMAL_COACHING):
        p = _pipeline(mode=mode)
        p.nova_sonic.session = self.nova = _FakeNova()
        p.caller_speech = self.speech = _FakeSpeech()
        p._on_forced_audio = AsyncMock()
        done = asyncio.get_running_loop().create_future()
        done.set_result(b"opening-audio")
        p._opening_audio_task = done
        return p

    async def test_silent_caller_hears_cara_open_the_call(self):
        p = self._pipeline()
        await asyncio.wait_for(p._open_if_caller_silent(time.monotonic()), 1)
        p._on_forced_audio.assert_awaited_once_with(b"opening-audio")
        self.nova.note_forced_opening.assert_called_once()
        self.nova.send_opening_cue.assert_not_awaited()

    async def test_caller_who_spoke_is_answered_by_nova_not_the_opening(self):
        p = self._pipeline()
        self.speech.last_speech_at = time.monotonic()
        task = asyncio.create_task(p._open_if_caller_silent(time.monotonic()))
        await asyncio.sleep(0.05)
        self.nova.waiting = False  # Nova transcribed the hello and is replying
        await asyncio.wait_for(task, 1)
        p._on_forced_audio.assert_not_awaited()

    async def test_cap_opens_at_the_first_pause_when_noise_keeps_counting_as_speech(self):
        p = self._pipeline()
        start = time.monotonic()
        self.speech.speaking = True
        self.speech.last_speech_at = start
        task = asyncio.create_task(p._open_if_caller_silent(start))
        await asyncio.sleep(0.5)
        p._on_forced_audio.assert_not_awaited()  # never talks over the caller
        self.speech.speaking = False
        self.speech.last_speech_at = time.monotonic()
        await asyncio.wait_for(task, 1)
        p._on_forced_audio.assert_awaited_once()

    async def test_nova_cue_used_when_enabled(self):
        p = self._pipeline()
        with patch.object(pipeline_module, "OPENING_CUE_ENABLED", True):
            await asyncio.wait_for(p._open_if_caller_silent(time.monotonic()), 1)
        self.nova.send_opening_cue.assert_awaited_once_with(CALL_CONNECTED_CUE)
        p._on_forced_audio.assert_not_awaited()

    async def test_direct_handoff_call_keeps_waiting_for_the_caller(self):
        p = self._pipeline(mode=CallMode.MODE_1_DIRECT_HANDOFF)
        await asyncio.wait_for(p._open_if_caller_silent(time.monotonic()), 1)
        p._on_forced_audio.assert_not_awaited()

    async def test_direct_handoff_call_gets_no_cue(self):
        # Call 60364398: the cue made Nova read the normal opening in direct mode
        p = self._pipeline(mode=CallMode.MODE_1_DIRECT_HANDOFF)
        with patch.object(pipeline_module, "OPENING_CUE_ENABLED", True):
            await asyncio.wait_for(p._open_if_caller_silent(time.monotonic()), 1)
        self.nova.send_opening_cue.assert_not_awaited()
        p._on_forced_audio.assert_not_awaited()

    async def test_no_opening_once_voicemail_or_cara_has_spoken(self):
        p = self._pipeline()
        self.nova.waiting = False
        await asyncio.wait_for(p._open_if_caller_silent(time.monotonic()), 1)
        p._on_forced_audio.assert_not_awaited()


def _frames(pcm: np.ndarray):
    for i in range(0, len(pcm) - 159, 160):
        yield pcm[i:i + 160].astype(np.int16).tobytes()


class CallerSpeechDetectorTests(unittest.TestCase):
    def _feed(self, detector, pcm):
        t = 0.0
        for frame in _frames(pcm):
            detector.feed(frame, t)
            t += 0.02

    def _speech(self):
        pcmu = DISCLOSURE_WAV.read_bytes()[44:]
        return np.frombuffer(ulaw_decode(pcmu), dtype=np.int16)

    def test_real_speech_is_heard(self):
        d = CallerSpeechDetector()
        self._feed(d, self._speech())
        self.assertTrue(d.heard_speech)

    def test_silence_and_steady_noise_are_not_speech(self):
        rng = np.random.default_rng(1)
        for pcm in (np.zeros(8000 * 2), rng.normal(0, 400, 8000 * 3)):
            d = CallerSpeechDetector()
            self._feed(d, pcm)
            self.assertFalse(d.heard_speech)

    def test_short_click_is_not_speech(self):
        d = CallerSpeechDetector()
        pcm = np.zeros(8000)
        pcm[4000:4400] = 8000  # 50 ms bump
        self._feed(d, pcm)
        self.assertFalse(d.heard_speech)

    def test_muted_during_disclosure(self):
        d = CallerSpeechDetector()
        d.set_muted(True)
        self._feed(d, self._speech())
        self.assertFalse(d.heard_speech)

    def test_loudness_fallback_without_the_model(self):
        d = CallerSpeechDetector()
        d._vad = None
        self._feed(d, self._speech())
        self.assertTrue(d.heard_speech)
        self.assertEqual(d.engine, "rms")


class NovaOpeningStateTests(unittest.IsolatedAsyncioTestCase):
    def _session(self):
        s = NovaSonicSession(MagicMock(), contact_id="c-1", tool_executor=MagicMock())
        s.is_active = True
        s.stream = SimpleNamespace(input_stream=SimpleNamespace(send=AsyncMock()))
        return s

    async def test_awaiting_opening_until_someone_speaks(self):
        s = self._session()
        self.assertTrue(s.awaiting_opening())
        s._last_user_text = "hello"
        self.assertFalse(s.awaiting_opening())

    async def test_cue_is_a_user_text_turn(self):
        s = self._session()
        await s.send_opening_cue(CALL_CONNECTED_CUE)
        events = [json.loads(c.args[0].value.bytes_)["event"] for c in s.stream.input_stream.send.await_args_list]
        self.assertEqual(events[0]["contentStart"]["role"], "USER")
        self.assertEqual(events[0]["contentStart"]["type"], "TEXT")
        self.assertEqual(events[1]["textInput"]["content"], CALL_CONNECTED_CUE)
        self.assertIn("contentEnd", events[2])
        self.assertFalse(s.awaiting_opening())

    async def test_forced_opening_arms_the_voicemail_watchdog(self):
        s = self._session()
        s.note_forced_opening("Hi, this is Cara", 7.0)
        self.assertFalse(s.awaiting_opening())
        self.assertIsNotNone(s._watchdog_task)
        s._watchdog_task.cancel()

    def test_carrier_pin_prompt_counts_as_voicemail(self):
        text = "please enter your four digit pin"
        self.assertTrue(any(p in text for p in VOICEMAIL_GREETING_PATTERNS))

    def test_prompt_explains_the_cue(self):
        self.assertIn(CALL_CONNECTED_CUE, generate_nova_sonic_system_prompt(_patient()))

    def test_direct_prompt_has_no_cue_text(self):
        # Call 10316d32: this text made Nova say the normal opening on a direct call
        prompt = generate_nova_sonic_system_prompt(_patient(mode=CallMode.MODE_1_DIRECT_HANDOFF))
        self.assertNotIn(CALL_CONNECTED_CUE, prompt)
        self.assertIn("[DIRECT TO HUMAN AGENT CALL]", prompt)


if __name__ == "__main__":
    unittest.main()
