"""Regression tests for call 5c32159a.

The caller said "unfortunately Travis passed away last month" twice and
"Travis is no more" once. Nova's ASR heard "unfortunately i was very
busy last month" and "that is no more", Cara repeated the same question
three times, took "that is correct" as Travis being available, greeted
the next voice as Travis, and every assistant line carried the prompt's
quotation marks. The call ended as RESCHEDULED with no sign that a death
had been mentioned.
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from domain_rules import (
    PatientContext,
    SCRIPT_DEFINITIONS,
    generate_nova_sonic_system_prompt,
)
from gateway.nova_sonic_service import (
    NovaSonicSession,
    _has_death_cue,
    _strip_script_quotes,
)
from gateway.pipeline import CaraVoicePipeline
from gateway.tools import OutreachToolExecutor, infer_disposition_from_speech
from storage.outreach_records import (
    DIALABLE_STATUSES,
    NEEDS_REVIEW_STATUS,
    OutreachRecordService,
    format_disposition,
)

CALL_ID = "5c32159a-53e2-414e-aeb1-acc5cedbfc52"
PATIENT_ID = "3f8a995b-b2fc-4df1-a848-55a03e98f958"

PATIENT = PatientContext(
    patient_id=PATIENT_ID,
    first_name="Travis",
    last_name="Head",
    practice_name="Sample Practice Group",
)


def _event(event_type, data):
    payload = json.dumps({"event": {event_type: data}}).encode("utf-8")
    return SimpleNamespace(value=SimpleNamespace(bytes_=payload))


def _text(content, role="ASSISTANT"):
    return _event("textOutput", {"role": role, "content": content})


class _FakeStream:
    def __init__(self, events):
        self._events = events
        self.input_stream = SimpleNamespace(send=AsyncMock(), close=AsyncMock())

    async def await_output(self):
        async def _gen():
            for e in self._events:
                yield e
        return None, _gen()


def _run_session(events):
    storage = MagicMock()
    storage.mark_call_ended = AsyncMock()
    executor = OutreachToolExecutor(
        storage_service=storage,
        connect_client=MagicMock(),
        contact_id=CALL_ID,
    )
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

    async def _main():
        session.stream = _FakeStream(events)
        await session._read_responses()

    asyncio.run(_main())
    return session, transcripts


class TestPrompt(unittest.TestCase):
    def setUp(self):
        self.prompt = generate_nova_sonic_system_prompt(PATIENT)

    def test_either_or_question_replaced_with_yes_no(self):
        self.assertNotIn("still reachable at this number", self.prompt)
        self.assertIn("is Travis available to talk right now?", self.prompt)

    def test_deceased_needs_confirmation_first(self):
        repeat = "I’m sorry — could you say that again for me?"
        self.assertIn(repeat, self.prompt)
        self.assertIn(
            "stated clearly, or confirmed after repeating per HEARING MISTAKES",
            self.prompt,
        )
        self.assertIn("no more", self.prompt)  # listed as a death phrase
        self.assertIn("is a do-not-call request, not a death", self.prompt)

    def test_clear_death_statement_skips_confirmation(self):
        # "he passed away last week" style statements go straight to the
        # deceased branch instead of asking the caller to repeat themselves.
        self.assertIn(
            "do NOT ask them to repeat it. Treat it as confirmed and go "
            "straight to the deceased branch",
            self.prompt,
        )

    def test_ambiguous_death_mention_still_asks_to_repeat(self):
        # A bare word, fragment, or hedged mention is exactly the kind of
        # thing that caused call 5c32159a's mishearing, so it still asks —
        # using the generic, reusable "say that again" line, not a
        # death-specific leading question.
        self.assertIn(
            "a bare word or fragment with no full context, mumbled, "
            "hedged, or dropped into an otherwise unrelated sentence",
            self.prompt,
        )
        self.assertIn(
            "This is the one general-purpose line for \"please say that "
            "again\"; use it anywhere in the call instead of guessing.",
            self.prompt,
        )

    def test_identity_confirmation_mumble_keeps_its_own_question(self):
        # The generic "say that again" line must not swallow SECTION 1's
        # identity double-check: a mumbled answer to the opening greeting
        # still gets the specific "Just to double check, is this Travis?"
        # question, not the new general-purpose repeat line.
        self.assertIn(
            "EXCEPTION: this does not apply to SECTION 1's identity check.",
            self.prompt,
        )
        self.assertIn(
            'you MUST still use SECTION 1\'s own double-check question, '
            '"Just to double check, is this Travis?", not this line',
            self.prompt,
        )
        self.assertIn('Say this exact sentence: "Just to double check, is this Travis?"', self.prompt)

    def test_rephrase_then_close_instead_of_repeating(self):
        self.assertIn("Never say the same sentence twice in a row", self.prompt)
        self.assertIn(
            "Sorry, I didn’t quite catch that. Is Travis available to talk right now?",
            self.prompt,
        )

    def test_off_script_input_gets_deflection_not_a_restart(self):
        # A joke request, small talk, or other unscripted remark (e.g. "can
        # you hear me", "tell me a joke") must get a brief deflection and
        # then resume the call where it left off — not a replay of the
        # opening greeting or an earlier section.
        self.assertIn(
            "I’m so sorry — I can’t help with that.",
            self.prompt,
        )
        self.assertIn(
            "say this exact sentence once: \"I’m so sorry — I can’t help with that.\" "
            "then immediately continue the call exactly where it left off",
            self.prompt,
        )
        self.assertIn("Do NOT restart the opening greeting", self.prompt)

    def test_identity_rechecked_after_handover(self):
        self.assertIn('"Hi, is this Travis?"', self.prompt)
        self.assertIn("do NOT assume it is Travis", self.prompt)

    def test_new_lines_do_not_trigger_gateway_markers(self):
        # A question must never be mistaken for a closing line or a
        # terminal disposition by the gateway's substring matching.
        for line in (
            SCRIPT_DEFINITIONS.other_person_rephrase,
            SCRIPT_DEFINITIONS.other_person_handover_check,
            SCRIPT_DEFINITIONS.clarification_repeat_request,
            SCRIPT_DEFINITIONS.off_script_deflection,
        ):
            rendered = PATIENT.render_template(line)
            self.assertNotIn("goodbye", rendered.lower())
            self.assertNotIn("just to double check", rendered.lower())
            self.assertIsNone(infer_disposition_from_speech(rendered))


class TestDeathCue(unittest.TestCase):
    def test_death_phrases(self):
        for text in (
            "that is no more",
            "no more",
            "travis is no more",
            "unfortunately travis passed away last month",
            "he passed",
            "she died last year",
            "he is deceased",
            "he's no longer with us",
        ):
            self.assertTrue(_has_death_cue(text), text)

    def test_not_death_phrases(self):
        for text in (
            "unfortunately i was very busy last month",
            "that is correct",
            "i am not travis",
            "no more calls please",
            "no more phone calls",
            "this is a deadline",
            "",
        ):
            self.assertFalse(_has_death_cue(text), text)


class TestSessionTranscripts(unittest.TestCase):
    def test_quotes_stripped_and_death_cue_recorded(self):
        session, transcripts = _run_session([
            _text('"No problem — is Travis available to talk right now?"'),
            _text("unfortunately i was very busy last month", role="USER"),
            _text("that is no more", role="USER"),
            _text("no more", role="USER"),
        ])
        self.assertEqual(
            transcripts[0],
            ("ASSISTANT", "No problem — is Travis available to talk right now?"),
        )
        self.assertEqual(session.death_cue_text, "that is no more")

    def test_no_death_cue_on_ordinary_call(self):
        session, _ = _run_session([
            _text("yes this is travis", role="USER"),
        ])
        self.assertIsNone(session.death_cue_text)

    def test_strip_script_quotes(self):
        self.assertEqual(_strip_script_quotes('"Hi, this is Cara'), "Hi, this is Cara")
        self.assertEqual(_strip_script_quotes("“Is now a good time?”"), "Is now a good time?")
        self.assertEqual(_strip_script_quotes(None), "")


def _stop_pipeline(final_disposition, death_cue_text):
    storage = MagicMock()
    storage.schedule_retry = AsyncMock()
    storage.mark_call_ended = AsyncMock()
    storage.flag_possible_death = AsyncMock()
    pipeline = SimpleNamespace(
        call_id=CALL_ID,
        media_server=None,
        task=SimpleNamespace(cancel=AsyncMock()),
        sip_signaling_server=None,
        nova_sonic=SimpleNamespace(session=SimpleNamespace(
            final_disposition=final_disposition,
            death_cue_text=death_cue_text,
        )),
        patient=PATIENT,
        storage=storage,
    )
    with patch.dict("gateway.pipeline.__dict__", {"sip_signaling_server": None}):
        asyncio.run(CaraVoicePipeline.stop(pipeline))
    return storage


class TestTeardownReviewFlag(unittest.TestCase):
    def test_flagged_when_death_heard_but_call_rescheduled(self):
        storage = _stop_pipeline("RESCHEDULED", "that is no more")
        storage.flag_possible_death.assert_awaited_once_with(PATIENT_ID, "that is no more")

    def test_flagged_when_death_heard_and_retry_queued(self):
        storage = _stop_pipeline("IDENTITY_UNCONFIRMED", "no more")
        storage.schedule_retry.assert_awaited_once()
        storage.flag_possible_death.assert_awaited_once_with(PATIENT_ID, "no more")

    def test_not_flagged_when_recorded_as_deceased(self):
        storage = _stop_pipeline("DECEASED", "he passed away")
        storage.flag_possible_death.assert_not_called()

    def test_not_flagged_without_death_cue(self):
        storage = _stop_pipeline("RESCHEDULED", None)
        storage.flag_possible_death.assert_not_called()


class _FakeTable:
    def __init__(self, item):
        self.item = dict(item)

    def get_item(self, Key):
        return {"Item": dict(self.item)}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues):
        for assignment in UpdateExpression.removeprefix("SET ").split(", "):
            field, placeholder = assignment.split(" = ")
            self.item[field] = ExpressionAttributeValues[placeholder]


class TestFlagPossibleDeath(unittest.TestCase):
    def _service(self, **item):
        table = _FakeTable({"patient_id": PATIENT_ID, **item})
        ddb = MagicMock()
        ddb.Table.return_value = table
        return OutreachRecordService(dynamodb_resource=ddb), table

    def test_appends_note_to_existing_summary(self):
        service, table = self._service(
            call_status="RESCHEDULED", call_summary="Callback requested by patient"
        )
        asyncio.run(service.flag_possible_death(PATIENT_ID, "that is no more"))
        self.assertEqual(
            table.item["call_summary"],
            'Callback requested by patient. Needs review: caller may have said '
            'the patient passed away (heard: "that is no more")',
        )
        self.assertTrue(table.item["needs_review"])
        self.assertEqual(table.item["review_reason"], "POSSIBLE_DECEASED")

    def test_pauses_patient_requested_callback(self):
        service, table = self._service(
            call_status="RESCHEDULED", scheduled_time="2026-09-29T09:49:38Z"
        )
        asyncio.run(service.flag_possible_death(PATIENT_ID, "that is no more"))
        self.assertEqual(table.item["call_status"], "NEEDS_REVIEW")
        self.assertEqual(table.item["status_before_review"], "RESCHEDULED")

    def test_pauses_automatic_retry(self):
        service, table = self._service(call_status="PENDING", attempt_count=1)
        asyncio.run(service.flag_possible_death(PATIENT_ID, "no more"))
        self.assertEqual(table.item["call_status"], "NEEDS_REVIEW")
        self.assertEqual(table.item["status_before_review"], "PENDING")

    def test_final_status_left_alone(self):
        for status in ("EXHAUSTED", "COMPLETED", "REFUSED"):
            service, table = self._service(call_status=status)
            asyncio.run(service.flag_possible_death(PATIENT_ID, "he passed"))
            self.assertEqual(table.item["call_status"], status)
            self.assertNotIn("status_before_review", table.item)
            self.assertTrue(table.item["needs_review"])

    def test_note_alone_when_no_summary(self):
        service, table = self._service()
        asyncio.run(service.flag_possible_death(PATIENT_ID, "he passed"))
        self.assertTrue(table.item["call_summary"].startswith("Needs review:"))

    def test_paused_status_is_not_dialable(self):
        sys.path.insert(0, str(ROOT / "lambda"))
        import campaign_dialer

        self.assertEqual(tuple(campaign_dialer.DIALABLE_STATUSES), DIALABLE_STATUSES)
        self.assertNotIn(NEEDS_REVIEW_STATUS, campaign_dialer.DIALABLE_STATUSES)
        self.assertEqual(
            format_disposition(NEEDS_REVIEW_STATUS),
            "NEEDS_REVIEW - Possible Deceased, Calls Paused",
        )


if __name__ == "__main__":
    unittest.main()
