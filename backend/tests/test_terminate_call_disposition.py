"""Regression tests for terminate_call disposition handling (call a347027d).

Nova Sonic sends tool arguments as a JSON string in the toolUse event's
`content` field. The gateway used to read only `input`/`parameters`, so
terminate_call always ran with no disposition and the call was first
written as COMPLETED.
"""

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.gateway.nova_sonic_service import NovaSonicSession, _parse_tool_input
from backend.gateway.tools import OutreachToolExecutor

CALL_ID = "a347027d-df1e-4dea-9573-637ce681892b"
PATIENT_ID = "TESTPT-0000"


def _event(event_type, data):
    payload = json.dumps({"event": {event_type: data}}).encode("utf-8")
    return SimpleNamespace(value=SimpleNamespace(bytes_=payload))


class _FakeStream:
    def __init__(self, events):
        self._events = events
        self.input_stream = SimpleNamespace(send=AsyncMock())

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
    session = NovaSonicSession(MagicMock(), contact_id=CALL_ID, tool_executor=executor)
    session.is_active = True
    return session, storage


def _run(session, events):
    session.stream = _FakeStream(events)
    asyncio.run(session._read_responses())


class TestParseToolInput(unittest.TestCase):
    def test_reads_json_string_from_content(self):
        data = {"toolName": "terminate_call", "content": '{"disposition": "WRONG_NUMBER"}'}
        self.assertEqual(_parse_tool_input(data), {"disposition": "WRONG_NUMBER"})

    def test_double_encoded_content(self):
        data = {"content": json.dumps('{"disposition": "REFUSED"}')}
        self.assertEqual(_parse_tool_input(data), {"disposition": "REFUSED"})

    def test_legacy_input_field(self):
        self.assertEqual(_parse_tool_input({"input": {"reason": "x"}}), {"reason": "x"})

    def test_missing_or_malformed_returns_empty(self):
        self.assertEqual(_parse_tool_input({}), {})
        self.assertEqual(_parse_tool_input({"content": ""}), {})
        self.assertEqual(_parse_tool_input({"content": "not json"}), {})
        self.assertEqual(_parse_tool_input({"content": "[1, 2]"}), {})


@patch.dict(os.environ, {"TRAFFIC_TYPE": "TEST"})
class TestTerminateCallDisposition(unittest.TestCase):
    def test_disposition_from_content_is_written_on_first_update(self):
        """The model calls terminate_call before speaking the wrong-number line."""
        session, storage = _make_session()
        _run(session, [
            _event("toolUse", {
                "toolUseId": "e811b57e",
                "toolName": "terminate_call",
                "content": json.dumps({"disposition": "WRONG_NUMBER"}),
            }),
        ])

        self.assertEqual(session.final_disposition, "WRONG_NUMBER")
        storage.update_call_outcome.assert_called_once_with(
            patient_id=PATIENT_ID, call_id=CALL_ID, call_status="WRONG_NUMBER",
        )

    def test_post_speech_inference_updates_outcome_without_call_id_error(self):
        """No disposition passed; the spoken wrong-number line corrects the outcome."""
        session, storage = _make_session()
        _run(session, [
            _event("toolUse", {"toolUseId": "t1", "toolName": "terminate_call", "content": "{}"}),
            _event("textOutput", {
                "role": "ASSISTANT",
                "content": "Thank you for telling me. I'll update our system so we "
                           "stop reaching out to this number for Alex.",
            }),
        ])

        self.assertEqual(session.final_disposition, "WRONG_NUMBER")
        self.assertEqual(
            [c.kwargs["call_status"] for c in storage.update_call_outcome.call_args_list],
            ["COMPLETED", "WRONG_NUMBER"],
        )
        self.assertEqual(storage.update_call_outcome.call_args.kwargs["call_id"], CALL_ID)


if __name__ == "__main__":
    unittest.main()
