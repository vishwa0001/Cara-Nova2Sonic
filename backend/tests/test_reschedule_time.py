"""Callback times the patient asks for (call 10eee003: "uh you can call me
tomorrow by ten am" was saved as 09:00 UTC because "ten" wasn't read and
clock times were treated as UTC)."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from backend.gateway.reschedule_time import parse_reschedule_time

# 2026-09-30 08:44 UTC = 14:14 in Asia/Kolkata (UTC+5:30)
NOW = datetime(2026, 9, 30, 8, 44, 12, tzinfo=timezone.utc)


def _at(text, tz="Asia/Kolkata"):
    with patch.dict(os.environ, {"CALLBACK_TIMEZONE": tz}):
        return parse_reschedule_time(text, now=NOW)


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class RescheduleTimeTests(unittest.TestCase):
    def test_the_reported_call(self):
        # 10:00 IST on 1 Oct = 04:30 UTC
        self.assertEqual(_at("uh you can call me tomorrow by ten am"), _utc(2026, 10, 1, 4, 30))

    def test_relative_minutes_and_hours(self):
        cases = {
            "call me after 10 mins": timedelta(minutes=10),
            "in ten minutes": timedelta(minutes=10),
            "call me after an hour": timedelta(hours=1),
            "in two hours": timedelta(hours=2),
            "half an hour": timedelta(minutes=30),
            "in a couple of hours": timedelta(hours=2),
            "give me a few minutes": timedelta(minutes=5),
            "in fifteen minutes": timedelta(minutes=15),
        }
        for text, delta in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_at(text), NOW + delta)

    def test_unspecified_or_unclear_is_24_hours(self):
        for text in ["unspecified", "", "anytime", "you can call me anytime", "whenever",
                     "you can call me anything", "hmm not sure"]:
            with self.subTest(text=text):
                self.assertEqual(_at(text), NOW + timedelta(hours=24))

    def test_clock_times_are_local(self):
        self.assertEqual(_at("tomorrow at 3 pm"), _utc(2026, 10, 1, 9, 30))
        self.assertEqual(_at("tomorrow at 10:30 a.m."), _utc(2026, 10, 1, 5, 0))
        self.assertEqual(_at("tomorrow ten thirty am"), _utc(2026, 10, 1, 5, 0))
        self.assertEqual(_at("tomorrow"), _utc(2026, 10, 1, 3, 30))  # 9 am local

    def test_time_today_or_next_day_if_passed(self):
        self.assertEqual(_at("at 5 pm"), _utc(2026, 9, 30, 11, 30))    # later today
        self.assertEqual(_at("at 11 am"), _utc(2026, 10, 1, 5, 30))    # already past -> tomorrow
        self.assertEqual(_at("this evening"), _utc(2026, 9, 30, 12, 30))

    def test_days_and_weeks(self):
        self.assertEqual(_at("in 2 days"), NOW + timedelta(days=2))
        self.assertEqual(_at("next week"), NOW + timedelta(weeks=1))
        self.assertEqual(_at("friday at noon"), _utc(2026, 10, 2, 6, 30))

    def test_campaign_timezone_wins_over_default(self):
        with patch.dict(os.environ, {"CALLBACK_TIMEZONE": "Asia/Kolkata"}):
            got = parse_reschedule_time("tomorrow at 10 am", now=NOW, tz_name="America/Chicago")
        self.assertEqual(got, _utc(2026, 10, 1, 15, 0))

    def test_reschedule_call_uses_the_campaigns_timezone(self):
        import asyncio
        from unittest.mock import MagicMock
        from backend.domain_rules import PatientContext
        from backend.gateway.tools import OutreachToolExecutor
        storage = MagicMock()
        ex = OutreachToolExecutor(storage_service=storage, connect_client=MagicMock(), contact_id="c1",
                                  patient_context=PatientContext(patient_id="pat-1", first_name="T", last_name="C",
                                                                 practice_name="X", timezone="America/New_York"))
        with patch("backend.gateway.tools.parse_reschedule_time", wraps=parse_reschedule_time) as parse:
            asyncio.run(ex.reschedule_call(preferred_time="tomorrow at 10 am"))
        self.assertEqual(parse.call_args[1]["tz_name"], "America/New_York")

    def test_timezone_setting(self):
        self.assertEqual(_at("tomorrow at 10 am", tz="America/New_York"), _utc(2026, 10, 1, 14, 0))


if __name__ == "__main__":
    unittest.main()
