"""Call 8e29df6f: "day after tomorrow" was booked as tomorrow (Nova passed
"after tomorrow"), clock times were read in UTC because the image had no
timezone data, and "you can talk to me" was not taken as a representative
offer. Call 7c016576: a direct call played "please hold" twice."""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from backend.domain_rules import CallMode, PatientContext, generate_nova_sonic_system_prompt
from backend.gateway import nova_sonic_service
from backend.gateway.nova_sonic_service import NovaSonicSession
from backend.gateway.reschedule_time import names_a_time, parse_reschedule_time
from backend.gateway.tools import OutreachToolExecutor

IST = "Asia/Calcutta"


def _patient(mode=CallMode.MODE_2_NORMAL_COACHING):
    return PatientContext(patient_id="af4b6c65", first_name="Tim", last_name="Cook",
                          practice_name="Tia Sahu", call_mode=mode, timezone=IST)


class TimezoneTests(unittest.TestCase):
    def test_campaign_timezone_is_available(self):
        # The gateway image needs tzdata; without it this raised and callbacks used UTC
        ZoneInfo(IST)

    def test_tomorrow_is_nine_am_in_the_campaign_timezone(self):
        now = datetime(2026, 10, 8, 12, 49, 51, tzinfo=timezone.utc)
        self.assertEqual(parse_reschedule_time("tomorrow", now=now, tz_name=IST),
                         datetime(2026, 10, 9, 3, 30, tzinfo=timezone.utc))
        self.assertEqual(parse_reschedule_time("day after tomorrow", now=now, tz_name=IST),
                         datetime(2026, 10, 10, 3, 30, tzinfo=timezone.utc))


class CallerWordsTests(unittest.TestCase):
    def test_names_a_time(self):
        for text in ("you can call me anytime by day after tomorrow", "at 3 pm", "in ten minutes", "friday"):
            self.assertTrue(names_a_time(text), text)
        for text in ("", "unspecified", "anytime", "yes sure", "okay"):
            self.assertFalse(names_a_time(text), text)

    def _executor(self):
        storage = MagicMock()
        storage.update_reschedule.return_value = {"reschedule_count": 1, "call_status": "RESCHEDULED"}
        storage.mark_call_ended = AsyncMock()
        return OutreachToolExecutor(api_base_url="http://mock", storage_service=storage,
                                    connect_client=MagicMock(), patient_context=_patient(),
                                    contact_id="8e29df6f"), storage

    def _booked_day(self, **kwargs):
        executor, storage = self._executor()
        asyncio.run(executor.reschedule_call(reason="specialist did not answer transfer", **kwargs))
        booked = datetime.strptime(storage.update_reschedule.call_args[0][1], "%Y-%m-%dT%H:%M:%SZ")
        return booked.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(IST)).date()

    def test_callers_words_beat_a_shortened_tool_input(self):
        today = datetime.now(ZoneInfo(IST)).date()
        day = self._booked_day(preferred_time="after tomorrow",
                               caller_words="you can call me anytime by day after tomorrow")
        self.assertEqual(day, today + timedelta(days=2))

    def test_tool_input_used_when_caller_named_no_time(self):
        today = datetime.now(ZoneInfo(IST)).date()
        self.assertEqual(self._booked_day(preferred_time="tomorrow", caller_words="okay sure"),
                         today + timedelta(days=1))

    def test_session_hands_the_whole_caller_turn_to_reschedule(self):
        s = NovaSonicSession(MagicMock(), contact_id="8e29df6f", on_hangup=AsyncMock(),
                             tool_executor=MagicMock(reschedule_call=AsyncMock(return_value={"success": True})))
        s.is_active = True
        s._send_tool_result = AsyncMock()
        s._initiate_hangup = AsyncMock()
        events = [
            {"textOutput": {"role": "ASSISTANT", "content": "What time works best for the team to call you back?"}},
            {"textOutput": {"role": "USER", "content": "you can call me anytime by"}},
            {"textOutput": {"role": "USER", "content": "day after tomorrow"}},
            {"toolUse": {"toolUseId": "t1", "toolName": "reschedule_call",
                         "content": json.dumps({"preferred_time": "after tomorrow"})}},
        ]

        class _Stream:
            input_stream = SimpleNamespace(send=AsyncMock(), close=AsyncMock())

            async def await_output(self):
                async def gen():
                    for e in events:
                        yield SimpleNamespace(value=SimpleNamespace(bytes_=json.dumps({"event": e}).encode()))
                return None, gen()

        async def main():
            s.stream = _Stream()
            await s._read_responses()

        asyncio.run(main())
        kwargs = s.tool_executor.reschedule_call.call_args[1]
        self.assertEqual(kwargs["caller_words"], "you can call me anytime by day after tomorrow")
        self.assertEqual(kwargs["preferred_time"], "after tomorrow")


class DirectHoldLineTests(unittest.TestCase):
    def setUp(self):
        synth = patch.object(nova_sonic_service, "_synthesize_forced_closing_helper",
                             AsyncMock(return_value=b"hold-audio"))
        self.synth = synth.start()
        self.addCleanup(synth.stop)

    def _session(self, mode):
        s = NovaSonicSession(MagicMock(), contact_id="7c016576", on_hangup=AsyncMock(),
                             on_forced_audio=AsyncMock(), on_transfer=AsyncMock(return_value=True))
        s.patient = _patient(mode)
        s.is_active = True
        s.end = AsyncMock()
        return s

    def test_direct_call_does_not_add_a_second_hold_line(self):
        s = self._session(CallMode.MODE_1_DIRECT_HANDOFF)
        asyncio.run(s._run_transfer("t1", "+13105550000", spoken_text=""))
        self.synth.assert_not_awaited()
        s.on_transfer.assert_awaited_once()

    def test_direct_call_already_in_normal_script_still_gets_it(self):
        s = self._session(CallMode.MODE_1_DIRECT_HANDOFF)
        s._normal_script_spoken = True
        asyncio.run(s._run_transfer("t1", "+13105550000", spoken_text=""))
        self.synth.assert_awaited_once()


class RepresentativePromptTests(unittest.TestCase):
    def test_offer_to_speak_for_patient_is_accepted_first_time(self):
        prompt = generate_nova_sonic_system_prompt(_patient())
        section2 = prompt.split("[SECTION 2")[1].split("[SECTION 3")[0]
        self.assertIn('"you can talk to me"', section2)
        self.assertIn("do NOT ask whether Tim is available", section2)
        self.assertIn("it never means Tim is available", section2)
        # The handover check only follows the caller going to get the patient
        self.assertLess(section2.index("they will get Tim"), section2.index("Hi, is this Tim?"))

    def test_callback_prompt_asks_for_every_word(self):
        self.assertIn("'day after tomorrow' not 'after tomorrow'", generate_nova_sonic_system_prompt(_patient()))


if __name__ == "__main__":
    unittest.main()
