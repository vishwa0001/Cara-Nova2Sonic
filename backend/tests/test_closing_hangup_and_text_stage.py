"""Regression tests for call 375604a0.

Nova said "Goodbye" but never called terminate_call, so the gateway never
hung up and the script restarted when the caller said hello again. Each
assistant line was also logged twice, because Nova sends every sentence
as a SPECULATIVE copy (before audio) and a FINAL copy (after audio).
"""

import asyncio
import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.gateway.nova_sonic_service import (
    NovaSonicSession,
    _is_closing_turn,
    _parse_generation_stage,
)
from backend.gateway.tools import OutreachToolExecutor

CALL_ID = "375604a0-06fc-48eb-b5a2-b0612cdeaa1e"
PATIENT_ID = "TESTPT-0000"

UNCONFIRMED_LINE = (
    "Thanks — I'll need to speak directly with Travis Head. "
    "I'll try reaching them another time."
)
SILENT_AUDIO = base64.b64encode(b"\x00" * 960).decode()


def _event(event_type, data):
    payload = json.dumps({"event": {event_type: data}}).encode("utf-8")
    return SimpleNamespace(value=SimpleNamespace(bytes_=payload))


def _text_start(stage):
    return _event("contentStart", {
        "type": "TEXT",
        "role": "ASSISTANT",
        "additionalModelFields": json.dumps({"generationStage": stage}),
    })


def _text(content, role="ASSISTANT"):
    return _event("textOutput", {"role": role, "content": content})


def _end(stop_reason):
    return _event("contentEnd", {"stopReason": stop_reason})


def _audio():
    return _event("audioOutput", {"content": SILENT_AUDIO})


class _FakeStream:
    def __init__(self, events):
        self._events = events
        self.input_stream = SimpleNamespace(send=AsyncMock(), close=AsyncMock())

    async def await_output(self):
        async def _gen():
            for e in self._events:
                yield e
        return None, _gen()


def _make_session():
    storage = MagicMock()
    storage.mark_call_ended = AsyncMock()
    executor = OutreachToolExecutor(
        storage_service=storage,
        connect_client=MagicMock(),
        contact_id=CALL_ID,
    )
    executor.patient_id = PATIENT_ID
    transcripts = []

    async def _on_transcript(role, text):
        transcripts.append((role, text))

    session = NovaSonicSession(
        MagicMock(),
        contact_id=CALL_ID,
        tool_executor=executor,
        on_transcript=_on_transcript,
        on_hangup=AsyncMock(),
    )
    session.is_active = True
    return session, storage, transcripts


def _run(session, events, after=None):
    async def _main():
        session.stream = _FakeStream(events)
        await session._read_responses()
        if after:
            await after()
        await asyncio.sleep(0.3)  # let hangup / DB tasks run

    asyncio.run(_main())


# Event order as seen in the gateway logs for call 375604a0 (07:25:42-07:25:49)
GOODBYE_TURN = [
    _text_start("SPECULATIVE"), _text(UNCONFIRMED_LINE), _end("PARTIAL_TURN"),
    _audio(), _end("PARTIAL_TURN"),
    _text_start("SPECULATIVE"), _text("Goodbye."), _end("PARTIAL_TURN"),
    _audio(), _end("END_TURN"),
]
FINAL_COPIES = [
    _text_start("FINAL"), _text(UNCONFIRMED_LINE), _end("END_TURN"),
    _text_start("FINAL"), _text("Goodbye."), _end("PARTIAL_TURN"),
]


class TestHelpers(unittest.TestCase):
    def test_parse_generation_stage(self):
        self.assertEqual(
            _parse_generation_stage({"additionalModelFields": '{"generationStage":"SPECULATIVE"}'}),
            "SPECULATIVE",
        )
        self.assertEqual(
            _parse_generation_stage({"additionalModelFields": {"generationStage": "final"}}),
            "FINAL",
        )
        self.assertIsNone(_parse_generation_stage({}))
        self.assertIsNone(_parse_generation_stage({"additionalModelFields": "not json"}))

    def test_closing_turn(self):
        self.assertTrue(_is_closing_turn("Appreciate your time. Goodbye."))
        self.assertFalse(_is_closing_turn("Am I able to speak with Travis Head?"))


@patch.dict(os.environ, {"TRAFFIC_TYPE": "TEST"})
class TestClosingHangup(unittest.TestCase):
    def test_goodbye_without_tool_call_hangs_up_after_audio_drains(self):
        session, storage, _ = _make_session()
        _run(session, GOODBYE_TURN)

        # Armed, but waits for the farewell audio to finish playing
        self.assertTrue(session._disconnect_after_speaking)
        session.on_hangup.assert_not_called()
        self.assertEqual(session.final_disposition, "IDENTITY_UNCONFIRMED")
        storage.update_call_outcome.assert_called_once_with(
            patient_id=PATIENT_ID, call_id=CALL_ID, call_status="IDENTITY_UNCONFIRMED",
        )

        async def _drain():
            await session._on_queue_drained()
            await asyncio.sleep(0.3)

        asyncio.run(_drain())
        session.on_hangup.assert_awaited_once()
        self.assertFalse(session.is_active)

    def test_wrong_number_goodbye_records_wrong_number(self):
        session, storage, _ = _make_session()
        _run(session, [
            _text_start("SPECULATIVE"),
            _text("Thank you for telling me — I’ll update our system so we stop "
                  "reaching out to this number for Travis."),
            _end("PARTIAL_TURN"), _audio(),
            _text_start("SPECULATIVE"), _text("Appreciate your time. Goodbye."),
            _end("PARTIAL_TURN"), _audio(), _end("END_TURN"),
        ])
        self.assertEqual(session.final_disposition, "WRONG_NUMBER")
        self.assertTrue(session._disconnect_after_speaking)

    def test_hangs_up_immediately_if_audio_already_drained(self):
        session, _, _ = _make_session()
        events = GOODBYE_TURN[:-1]  # everything but the final END_TURN

        async def _late_end_turn():
            await session._on_queue_drained()  # audio finished first
            session.stream = _FakeStream([_end("END_TURN")])
            await session._read_responses()

        _run(session, events, after=_late_end_turn)
        session.on_hangup.assert_awaited_once()

    def test_non_closing_turn_does_not_hang_up(self):
        session, _, _ = _make_session()
        _run(session, [
            _text_start("SPECULATIVE"), _text("Am I able to speak with Travis Head?"),
            _end("PARTIAL_TURN"), _audio(), _end("END_TURN"),
        ])
        self.assertFalse(session._disconnect_after_speaking)
        session.on_hangup.assert_not_called()

    def test_terminate_call_tool_takes_precedence(self):
        session, storage, _ = _make_session()
        _run(session, [
            _event("toolUse", {
                "toolUseId": "t1",
                "toolName": "terminate_call",
                "content": json.dumps({"disposition": "WRONG_NUMBER"}),
            }),
            *GOODBYE_TURN,
        ])
        self.assertFalse(session._closing_hangup_armed)
        self.assertTrue(session._disconnect_after_speaking)


@patch.dict(os.environ, {"TRAFFIC_TYPE": "TEST"})
class TestTranscriptNotDoubled(unittest.TestCase):
    def test_final_copies_are_not_logged_again(self):
        session, _, transcripts = _make_session()
        session._is_assistant_speaking = True
        _run(session, [
            _event("contentStart", {"type": "TEXT", "role": "USER",
                                    "additionalModelFields": '{"generationStage":"FINAL"}'}),
            _text("uh no i'm not travis", role="USER"), _end("PARTIAL_TURN"),
            *GOODBYE_TURN,
            *FINAL_COPIES,
        ])
        self.assertEqual(transcripts, [
            ("USER", "uh no i'm not travis"),
            ("ASSISTANT", UNCONFIRMED_LINE),
            ("ASSISTANT", "Goodbye."),
        ])

    def test_final_end_turn_does_not_reset_next_turn(self):
        """A late FINAL block's END_TURN must not count as a turn boundary."""
        session, _, _ = _make_session()
        _run(session, [
            _text_start("SPECULATIVE"), _text("Am I able to speak with Travis Head?"),
            _end("PARTIAL_TURN"), _audio(), _end("END_TURN"),
            _text_start("FINAL"), _text("Am I able to speak with Travis Head?"), _end("END_TURN"),
        ])
        self.assertEqual(session._last_completed_assistant_turn, "Am I able to speak with Travis Head?")


if __name__ == "__main__":
    unittest.main()
