"""Turn a patient's spoken callback request into an exact UTC time.

Examples (with CALLBACK_TIMEZONE as the local zone):
  "unspecified" / unclear              -> now + 24 hours
  "in 10 minutes", "after ten mins"    -> now + 10 minutes
  "in an hour", "half an hour"         -> now + 60 / 30 minutes
  "tomorrow by ten am"                 -> tomorrow 10:00 local
  "at 3 pm", "this evening"            -> today 15:00 / 18:00 local (tomorrow if already past)
  "Friday at noon", "next Monday"      -> that day, 12:00 / 09:00 local
  "in 2 days", "next week"             -> now + 2 days / 7 days

Spoken numbers come from speech recognition, so "ten", "two thirty" and
"10:30 a.m." all count. Clock times are read in the campaign's timezone
(chosen in the UI when scheduling), or CALLBACK_TIMEZONE for campaigns
uploaded before that was recorded.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger("reschedule_time")

DEFAULT_CALLBACK_TIMEZONE = "Asia/Kolkata"
DEFAULT_HOUR = 9  # a day with no time given ("tomorrow") means 9 am local

_UNSPECIFIED = {"", "unspecified", "none", "not specified", "anytime", "any time", "whenever"}

_WORD_NUMBERS = [
    ("forty five", 45), ("forty-five", 45), ("twenty five", 25), ("twenty-five", 25),
    ("fifteen", 15), ("twenty", 20), ("thirty", 30), ("forty", 40), ("fifty", 50),
    ("ninety", 90), ("eleven", 11), ("twelve", 12), ("ten", 10), ("one", 1), ("two", 2),
    ("three", 3), ("four", 4), ("five", 5), ("six", 6), ("seven", 7), ("eight", 8), ("nine", 9),
]

_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Parts of the day, as local hours
_PART_OF_DAY = [("morning", 9), ("noon", 12), ("afternoon", 14), ("evening", 18),
                ("tonight", 19), ("night", 19), ("midnight", 0)]


def callback_timezone(tz_name: Optional[str] = None) -> ZoneInfo:
    """The campaign's timezone if known, else CALLBACK_TIMEZONE."""
    name = tz_name or os.environ.get("CALLBACK_TIMEZONE", DEFAULT_CALLBACK_TIMEZONE)
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning(f"[RESCHEDULE_PARSE] Unknown CALLBACK_TIMEZONE {name!r}; using UTC")
        return ZoneInfo("UTC")


def _normalize(text: str) -> str:
    t = text.lower()
    t = t.replace("a.m.", "am").replace("p.m.", "pm").replace("o'clock", "").replace("o clock", "")
    t = re.sub(r"\b(uh+|um+|er|like|please|maybe)\b", " ", t)
    t = re.sub(r"\ban hour and a half\b|\bone and a half hours?\b", "90 minutes", t)
    t = re.sub(r"\bhalf (an )?hour\b", "30 minutes", t)
    t = re.sub(r"\ba quarter (of an )?hour\b", "15 minutes", t)
    t = re.sub(r"\b(a )?couple (of )?", "2 ", t)
    t = re.sub(r"\b(a )?few minutes\b", "5 minutes", t)
    t = re.sub(r"\b(a )?few hours\b", "3 hours", t)
    t = re.sub(r"\b(a )?few days\b", "3 days", t)
    for word, num in _WORD_NUMBERS:
        t = re.sub(rf"\b{word}\b", str(num), t)
    # "10 30 am" (spoken "ten thirty") -> "10:30 am"
    t = re.sub(r"\b(\d{1,2}) ([0-5]\d)\b(?=\s*(am|pm)?)", r"\1:\2", t)
    return re.sub(r"\s+", " ", t).strip()


def _relative(t: str) -> Optional[timedelta]:
    for pattern, unit in ((r"(\d+)\s*(?:min|mins|minute|minutes)\b", "minutes"),
                          (r"(\d+)\s*(?:hr|hrs|hour|hours)\b", "hours"),
                          (r"(\d+)\s*days?\b", "days"),
                          (r"(\d+)\s*weeks?\b", "weeks"),
                          (r"(\d+)\s*months?\b", "months")):
        m = re.search(pattern, t)
        if m:
            n = int(m.group(1))
            return timedelta(days=30 * n) if unit == "months" else timedelta(**{unit: n})
    for pattern, delta in ((r"\b(a|an)\s+minute\b", timedelta(minutes=1)),
                           (r"\b(a|an)\s+hour\b", timedelta(hours=1)),
                           (r"\b(a|next)\s+week\b", timedelta(weeks=1)),
                           (r"\b(a|next)\s+month\b", timedelta(days=30)),
                           (r"\ba\s+day\b", timedelta(days=1))):
        if re.search(pattern, t):
            return delta
    return None


def _clock_time(t: str) -> Optional[Tuple[int, int]]:
    """Hour and minute of a clock time like "10", "10:30", "3 pm", or a part of day."""
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t) or \
        re.search(r"\b(?:at|by|around|after|before|about)\s+(\d{1,2})(?::(\d{2}))?\b()", t)
    if m:
        hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if hour > 23 or minute > 59:
            return None
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        elif not meridiem and 1 <= hour <= 7:
            hour += 12  # "at 3" on a phone call means the afternoon
        return hour, minute
    for word, hour in _PART_OF_DAY:
        if re.search(rf"\b{word}\b", t):
            return hour, 0
    return None


def _day_offset(t: str, local_now: datetime) -> Optional[int]:
    if "day after tomorrow" in t:
        return 2
    if "tomorrow" in t:
        return 1
    if re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", t):
        return 0
    for i, name in enumerate(_DAYS):
        if name in t:
            ahead = (i - local_now.weekday()) % 7
            return ahead or 7
    return None


def parse_reschedule_time(
    preferred_time: Optional[str],
    now: Optional[datetime] = None,
    tz_name: Optional[str] = None,
) -> datetime:
    """UTC datetime for the patient's requested callback; +24h when unspecified or unclear."""
    if now is None:
        now = datetime.now(timezone.utc)
    fallback = now + timedelta(hours=24)
    raw = (preferred_time or "").strip()
    t = _normalize(raw)
    if t in _UNSPECIFIED or re.fullmatch(r"(you can )?(call( me)?|contact me)? ?(any ?time|whenever)", t):
        return fallback

    delta = _relative(t)
    if delta is not None:
        return now + delta

    tz = callback_timezone(tz_name)
    local_now = now.astimezone(tz)
    offset = _day_offset(t, local_now)
    clock = _clock_time(t)
    if offset is None and clock is None:
        logger.warning(f"[RESCHEDULE_PARSE] Could not parse {raw!r}; defaulting to +24 hours")
        return fallback

    hour, minute = clock if clock else (DEFAULT_HOUR, 0)
    day = local_now.date() + timedelta(days=offset or 0)
    local_target = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    if offset is None and local_target <= local_now:
        local_target += timedelta(days=1)  # "at 3 pm" said after 3 pm means tomorrow
    return local_target.astimezone(timezone.utc)
