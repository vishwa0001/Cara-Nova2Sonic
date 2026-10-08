"""A real person must never get Cara's voicemail message.

Calls a60362b6 and e284bb14: Cara opened a silent call, and the 10 s
"no user speech" timer, counted from her text rather than from when her ~7 s
opening finished playing, played the voicemail message to the patient. Nova
answered the patient at the same time (two Cara voices), and a separate 10 s
timer hung up partway through the 16 s message.
"""

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("PUBLIC_IP", "127.0.0.1")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np

from gateway import nova_sonic_service
from gateway.nova_sonic_service import NovaSonicSession

LOUD = np.full(160, 3000, dtype=np.int16).tobytes()
QUIET = np.zeros(160, dtype=np.int16).tobytes()
MESSAGE = b"\xff" * 1600  # 0.2 s of voicemail message


def _session():
    s = NovaSonicSession(
        contact_id="vm-1",
        tool_executor=MagicMock(terminate_call=AsyncMock()),
        on_forced_audio=AsyncMock(),
        on_hangup=AsyncMock(),
        on_purge_output=MagicMock(),
    )
    s.is_active = True
    s.stream = SimpleNamespace(input_stream=SimpleNamespace(send=AsyncMock(), close=AsyncMock()))
    return s


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, value in (
            ("VM_NO_REPLY_S", 0.2), ("CALLER_QUIET_S", 0.1), ("VM_FIRST_TURN_MAX_S", 2.0),
        ):
            p = patch.object(nova_sonic_service, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(nova_sonic_service, "_synthesize_forced_closing_helper", AsyncMock(return_value=MESSAGE))
        p.start()
        self.addCleanup(p.stop)


class NoReplyTimerTests(_Base):
    async def test_waits_for_the_first_turn_to_finish_playing(self):
        s = _session()
        s._handle_voicemail_detected = AsyncMock()
        task = asyncio.create_task(s._voicemail_watchdog())
        await asyncio.sleep(0.5)  # Cara's opening still playing
        s._handle_voicemail_detected.assert_not_awaited()
        await s._on_queue_drained()  # opening finished
        await asyncio.sleep(0.1)
        s._handle_voicemail_detected.assert_not_awaited()  # patient still has time
        await asyncio.wait_for(task, 1)
        s._handle_voicemail_detected.assert_awaited_once_with(cancellable=True)

    async def test_patient_reply_stops_the_timer(self):
        s = _session()
        s._handle_voicemail_detected = AsyncMock()
        task = asyncio.create_task(s._voicemail_watchdog(first_turn_seconds=0.0))
        s._user_spoke = True
        await asyncio.wait_for(task, 1)
        s._handle_voicemail_detected.assert_not_awaited()

    async def test_waits_while_the_patient_is_mid_sentence(self):
        s = _session()
        s._handle_voicemail_detected = AsyncMock()
        task = asyncio.create_task(s._voicemail_watchdog(first_turn_seconds=0.0))
        for _ in range(20):  # 0.4 s of speech, longer than the no-reply window
            await s.push_caller_audio(LOUD)
            await asyncio.sleep(0.02)
        s._handle_voicemail_detected.assert_not_awaited()
        s._user_spoke = True  # their transcript arrives
        await asyncio.wait_for(task, 1)
        s._handle_voicemail_detected.assert_not_awaited()

    async def test_silent_line_still_reaches_voicemail(self):
        s = _session()
        s._handle_voicemail_detected = AsyncMock()
        await s.push_caller_audio(QUIET)
        await asyncio.wait_for(s._voicemail_watchdog(first_turn_seconds=0.0), 1)
        s._handle_voicemail_detected.assert_awaited_once_with(cancellable=True)

    async def test_forced_opening_counts_from_the_end_of_its_audio(self):
        s = _session()
        s._handle_voicemail_detected = AsyncMock()
        s.note_forced_opening("Hi, this is Cara", 0.3)
        await asyncio.sleep(0.4)  # opening playing + part of the window
        s._handle_voicemail_detected.assert_not_awaited()
        await asyncio.wait_for(s._watchdog_task, 1)
        s._handle_voicemail_detected.assert_awaited_once_with(cancellable=True)


class VoicemailMessageTests(_Base):
    async def test_no_reply_message_is_cancelled_when_the_patient_speaks(self):
        s = _session()
        await s._handle_voicemail_detected(cancellable=True)
        s.on_purge_output.assert_called_once()  # Cara's own turn cut first
        s.on_forced_audio.assert_awaited_once_with(MESSAGE)
        s.tool_executor.terminate_call.assert_not_awaited()  # not recorded yet

        await s._cancel_voicemail("no tim cook is not here")
        self.assertEqual(s.on_purge_output.call_count, 2)  # message cut off
        self.assertFalse(s._voicemail_handled)
        self.assertEqual(s.final_disposition, "PATIENT_HANGUP")
        await asyncio.sleep(0.5)  # past the message length
        s.tool_executor.terminate_call.assert_not_awaited()
        s.on_hangup.assert_not_awaited()

    async def test_no_reply_message_is_recorded_after_it_plays(self):
        s = _session()
        await s._handle_voicemail_detected(cancellable=True)
        await asyncio.sleep(0.9)  # message 0.2 s + 0.3 s margin + hangup grace
        s.tool_executor.terminate_call.assert_awaited_once()
        s.on_hangup.assert_awaited_once()

    async def test_beep_or_greeting_message_plays_in_full_then_hangs_up(self):
        s = _session()
        with patch.object(s, "_terminal_trigger_watchdog", AsyncMock()) as safety:
            await s._handle_voicemail_detected()
            s.tool_executor.terminate_call.assert_awaited_once()  # recorded at once, as before
            timeout = safety.call_args.kwargs["timeout_seconds"]
            self.assertGreater(timeout, len(MESSAGE) / 8000)  # never cuts the message off
            await asyncio.sleep(0.9)
        s.on_hangup.assert_awaited_once()

    async def test_beep_during_a_no_reply_message_makes_it_final(self):
        s = _session()
        await s._handle_voicemail_detected(cancellable=True)
        await s._handle_voicemail_detected()  # beep heard
        self.assertFalse(s._voicemail_cancellable)


class _Event:
    def __init__(self, payload):
        self.value = SimpleNamespace(bytes_=json.dumps({"event": payload}).encode())


class _Stream:
    def __init__(self, events):
        self.input_stream = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        self._events = events

    async def await_output(self):
        async def _gen():
            for e in self._events:
                yield e
        return None, _gen()


class NovaMutedDuringVoicemailTests(unittest.IsolatedAsyncioTestCase):
    async def test_nova_audio_is_dropped_while_the_message_plays(self):
        s = _session()
        s.on_audio_out = AsyncMock()
        s._voicemail_handled = True
        audio = {"audioOutput": {"content": "AAAA" * 100}}
        s.stream = _Stream([_Event(audio)])
        await s._read_responses()
        s.on_audio_out.assert_not_awaited()

    async def test_patient_transcript_cancels_and_unmutes(self):
        s = _session()
        s.on_audio_out = AsyncMock()
        s.on_transcript = AsyncMock()
        s._voicemail_handled = True
        s._voicemail_cancellable = True
        s.stream = _Stream([
            _Event({"textOutput": {"role": "USER", "content": "no tim cook is not here"}}),
            _Event({"audioOutput": {"content": "AAAA" * 100}}),
        ])
        await s._read_responses()
        self.assertFalse(s._voicemail_handled)
        s.on_audio_out.assert_awaited_once()  # Cara's reply is heard


if __name__ == "__main__":
    unittest.main()
