"""Retry policy for calls that end without reaching the patient.

Every no-contact outcome (never connected, voicemail, no engagement,
identity not confirmed) and every call the patient hangs up mid-conversation
is redialed 24 hours later, up to 3 retries, with the reason recorded.
"""

import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "lambda"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import campaign_dialer
from gateway.nova_sonic_service import NovaSonicSession
from gateway.pipeline import CaraVoicePipeline
from gateway.tools import OutreachToolExecutor
from storage.outreach_records import (
    MAX_RETRIES,
    OutreachRecordService,
    format_disposition,
)

PATIENT_ID = "8d2f0c1e-retry-test"
CALL_ID = "call-retry-test"
FMT = "%Y-%m-%dT%H:%M:%SZ"


class _FakeTable:
    """Just enough of a DynamoDB table for OutreachRecordService."""

    def __init__(self, item):
        self.item = dict(item)

    def get_item(self, Key):
        return {"Item": dict(self.item)}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues):
        assignments = UpdateExpression.removeprefix("SET ").split(", ")
        for assignment in assignments:
            field, placeholder = assignment.split(" = ")
            self.item[field] = ExpressionAttributeValues[placeholder]


def _service(**item):
    table = _FakeTable({"patient_id": PATIENT_ID, **item})
    ddb = MagicMock()
    ddb.Table.return_value = table
    return OutreachRecordService(dynamodb_resource=ddb), table


def _hours_from_now(iso):
    delta = dt.datetime.strptime(iso, FMT).replace(tzinfo=dt.timezone.utc) - dt.datetime.now(dt.timezone.utc)
    return delta.total_seconds() / 3600


# --- schedule_retry -----------------------------------------------------------

@pytest.mark.parametrize("reason", [
    "NO_ANSWER", "BUSY", "UNAVAILABLE", "VOICEMAIL", "NO_ENGAGEMENT",
    "IDENTITY_UNCONFIRMED", "PATIENT_HANGUP",
])
def test_no_contact_outcome_is_retried_in_24h(reason):
    service, table = _service(call_status="DIALING", attempt_count=1)

    res = asyncio.run(service.schedule_retry(PATIENT_ID, CALL_ID, reason))

    assert res["call_status"] == "PENDING"
    assert table.item["call_status"] == "PENDING"
    assert table.item["last_failure_reason"] == reason
    assert table.item["attempt_count"] == 1
    assert table.item["last_call_id"] == CALL_ID
    assert _hours_from_now(table.item["scheduled_time"]) == pytest.approx(24, abs=0.01)
    assert "Retry 1 of 3" in table.item["call_summary"]


def test_three_retries_then_exhausted():
    service, table = _service(call_status="PENDING", attempt_count=0)

    for attempt in range(1, MAX_RETRIES + 2):
        # The dialer counts each attempt when it places the call
        table.item.update(call_status="DIALING", attempt_count=attempt)
        res = asyncio.run(service.schedule_retry(PATIENT_ID, CALL_ID, "NO_ANSWER"))
        if attempt <= MAX_RETRIES:
            assert res["call_status"] == "PENDING", f"attempt {attempt}"
            assert f"Retry {attempt} of 3" in table.item["call_summary"]
        else:
            assert res["call_status"] == "EXHAUSTED"

    assert table.item["attempt_count"] == 4
    assert res["scheduled_time"] == "N/A"
    assert table.item["call_summary"].endswith("No further retries after 4 attempts")


def test_outcome_already_written_during_call_does_not_count_twice():
    # terminate_call(VOICEMAIL) writes the status before teardown schedules the retry
    service, table = _service(call_status="VOICEMAIL", attempt_count=2)

    asyncio.run(service.schedule_retry(PATIENT_ID, CALL_ID, "VOICEMAIL"))

    assert table.item["attempt_count"] == 2
    assert table.item["call_status"] == "PENDING"


def test_attempt_that_never_reached_gateway_is_counted():
    service, table = _service(call_status="PENDING", attempt_count=1)

    asyncio.run(service.schedule_retry(PATIENT_ID, "", "GATEWAY_TIMEOUT", count_attempt=True))

    assert table.item["attempt_count"] == 2
    assert "last_call_id" not in table.item
    assert table.item["call_summary"].startswith("Call could not be placed")


def test_exhausted_disposition_names_last_reason():
    assert format_disposition("EXHAUSTED", reason="PATIENT_HANGUP") == (
        "EXHAUSTED - Max Attempts Reached (Last: PATIENT_HANGUP)"
    )
    assert format_disposition(
        "PENDING", reason="VOICEMAIL", rescheduled=True, retry_delay_desc="24h"
    ) == "VOICEMAIL_UNRESOLVED - Auto-Rescheduled (24h)"


# --- call teardown ------------------------------------------------------------

def _stop_pipeline(final_disposition):
    storage = MagicMock()
    storage.schedule_retry = AsyncMock()
    storage.mark_call_ended = AsyncMock()
    pipeline = SimpleNamespace(
        call_id=CALL_ID,
        media_server=None,
        task=SimpleNamespace(cancel=AsyncMock()),
        sip_signaling_server=None,
        nova_sonic=SimpleNamespace(session=SimpleNamespace(final_disposition=final_disposition)),
        patient=SimpleNamespace(patient_id=PATIENT_ID),
        storage=storage,
    )
    with patch.dict("gateway.pipeline.__dict__", {"sip_signaling_server": None}):
        asyncio.run(CaraVoicePipeline.stop(pipeline))
    return storage


@pytest.mark.parametrize("outcome", ["PATIENT_HANGUP", "NO_ENGAGEMENT", "VOICEMAIL", "IDENTITY_UNCONFIRMED"])
def test_teardown_retries_no_contact_outcomes(outcome):
    storage = _stop_pipeline(outcome)

    storage.schedule_retry.assert_awaited_once_with(
        patient_id=PATIENT_ID, call_id=CALL_ID, reason=outcome
    )
    storage.update_call_outcome.assert_not_called()


@pytest.mark.parametrize("outcome", ["COMPLETED", "REFUSED", "DECEASED", "WRONG_NUMBER", "TRANSFERRED"])
def test_teardown_keeps_final_outcomes(outcome):
    storage = _stop_pipeline(outcome)

    storage.schedule_retry.assert_not_called()
    storage.update_call_outcome.assert_called_once_with(
        patient_id=PATIENT_ID, call_id=CALL_ID, call_status=outcome
    )


def test_teardown_leaves_patient_requested_callback_alone():
    # reschedule_call already wrote RESCHEDULED (or EXHAUSTED) with the callback time
    storage = _stop_pipeline("RESCHEDULED")

    storage.schedule_retry.assert_not_called()
    storage.update_call_outcome.assert_not_called()


# --- in-call disposition --------------------------------------------------------

def _event(event_type, data):
    payload = json.dumps({"event": {event_type: data}}).encode("utf-8")
    return SimpleNamespace(value=SimpleNamespace(bytes_=payload))


class _FakeStream:
    def __init__(self, events):
        self._events = events
        self.input_stream = SimpleNamespace(send=AsyncMock(), close=AsyncMock())

    async def await_output(self):
        async def _gen():
            for e in self._events:
                yield e
        return None, _gen()


def _session_after(events):
    storage = MagicMock()
    storage.mark_call_ended = AsyncMock()
    executor = OutreachToolExecutor(
        storage_service=storage, connect_client=MagicMock(), contact_id=CALL_ID
    )
    executor.patient_id = PATIENT_ID
    session = NovaSonicSession(
        MagicMock(), contact_id=CALL_ID, tool_executor=executor,
        on_transcript=AsyncMock(), on_hangup=AsyncMock(),
    )
    session.is_active = True

    async def _main():
        session.stream = _FakeStream(events)
        await session._read_responses()
        await asyncio.sleep(0.3)

    asyncio.run(_main())
    return session


PATIENT_SPEAKS = [
    _event("textOutput", {"role": "ASSISTANT", "content": "Hi, is this Travis?"}),
    _event("contentEnd", {"stopReason": "END_TURN"}),
    _event("textOutput", {"role": "USER", "content": "Yes, speaking."}),
    _event("contentEnd", {"stopReason": "END_TURN"}),
]


def test_patient_hanging_up_mid_conversation_is_not_completed():
    session = _session_after(PATIENT_SPEAKS)

    assert session.final_disposition == "PATIENT_HANGUP"


def test_call_cara_closes_is_completed():
    session = _session_after(PATIENT_SPEAKS + [
        _event("textOutput", {"role": "ASSISTANT", "content": "Have a wonderful day. Goodbye."}),
        _event("contentEnd", {"stopReason": "END_TURN"}),
    ])

    assert session.final_disposition == "COMPLETED"


# --- dialer -------------------------------------------------------------------

def test_dialer_picks_up_due_patient_callbacks():
    table = MagicMock()
    table.query.side_effect = [
        {"Items": [{"patient_id": "new"}]},
        {"Items": [{"patient_id": "callback"}]},
    ]

    due = campaign_dialer.query_due_records(table, "2026-09-28T10:00:00Z", limit=50)

    assert [r["patient_id"] for r in due] == ["new", "callback"]
    assert [_queried_status(c) for c in table.query.call_args_list] == ["PENDING", "RESCHEDULED"]


def _queried_status(query_call):
    # KeyConditionExpression is Key("call_status").eq(status) & Key("scheduled_time").lte(now)
    status_condition = query_call.kwargs["KeyConditionExpression"].get_expression()["values"][0]
    return status_condition.get_expression()["values"][1]


def test_dialer_counts_attempt_when_gateway_unreachable(monkeypatch):
    storage = MagicMock()
    storage.schedule_retry = AsyncMock()
    monkeypatch.setattr(campaign_dialer, "OutreachRecordService", lambda *_: storage)
    monkeypatch.setattr(campaign_dialer, "_get_dynamodb_resource", MagicMock())
    monkeypatch.setattr(campaign_dialer, "resolve_ecs_gateway_url", lambda: "http://gw/calls/originate")
    monkeypatch.setattr(campaign_dialer, "query_due_records", lambda *a, **k: [{"patient_id": PATIENT_ID}])
    monkeypatch.setattr(campaign_dialer, "originate_single_call",
                        lambda *_: {"success": False, "error": "timed out"})
    monkeypatch.setattr(campaign_dialer.time, "sleep", lambda *_: None)

    campaign_dialer.lambda_handler({}, None)

    storage.schedule_retry.assert_awaited_once_with(
        patient_id=PATIENT_ID, call_id="", reason="GATEWAY_TIMEOUT",
        sip_code=None, count_attempt=True,
    )
