"""Booking slots with real availability.

A booking is a service at a start time on a date, stored as JSON next to
the leads file. Slots come from the business's opening hours and the
service's duration, so a slot the bot offers is a slot that is actually
free — double-bookings are checked before anything is confirmed.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_WEEKDAY_NAMES = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def format_time(minutes):
    """11:00 AM style; minutes since midnight."""
    hour, minute = divmod(minutes, 60)
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}"


def format_date(day):
    return day.strftime("%a, %d %b")


def _parse_clock(text):
    """Minutes since midnight for a clock reading like '10:00 AM'."""
    match = re.match(
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text.strip(), re.IGNORECASE
    )
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    marker = (match.group(3) or "").lower()
    if marker == "pm" and hour < 12:
        hour += 12
    if marker == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def parse_hours_range(text):
    """Opening hours like '10:00 AM - 8:00 PM' -> (open_min, close_min)."""
    parts = re.split(r"\s*-\s*", text.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    start, end = _parse_clock(parts[0]), _parse_clock(parts[1])
    if start is None or end is None or end <= start:
        return None
    return start, end


def parse_day(text, today=None):
    """A date mentioned in a message, or None.

    Understands 'today', 'tomorrow' (and Hinglish 'aaj'/'kal'),
    weekday names, and dates like '15 nov' or '15/11'.
    """
    today = today or date.today()
    lowered = text.lower()
    if re.search(r"\b(today|aaj)\b", lowered):
        return today
    if re.search(r"\b(tomorrow|kal)\b", lowered):
        return today + timedelta(days=1)
    match = re.search(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b",
        lowered,
    )
    if match:
        target = _WEEKDAY_NAMES[match.group(1)]
        delta = (target - today.weekday()) % 7
        return today + timedelta(days=delta)
    match = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s+([a-z]{3,9})\b", lowered)
    if match and match.group(2) in _MONTHS:
        day_num, month = int(match.group(1)), _MONTHS[match.group(2)]
        year = today.year
        try:
            parsed = date(year, month, day_num)
        except ValueError:
            return None
        if parsed < today:
            try:
                parsed = date(year + 1, month, day_num)
            except ValueError:
                return None
        return parsed
    match = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", lowered)
    if match:
        day_num, month = int(match.group(1)), int(match.group(2))
        year = int(match.group(3)) if match.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            parsed = date(year, month, day_num)
        except ValueError:
            return None
        if parsed < today and not match.group(3):
            try:
                parsed = date(year + 1, month, day_num)
            except ValueError:
                return None
        return parsed
    return None


def parse_time(text):
    """Minutes since midnight for a time in a message, or None.

    Only accepts times that are clearly times: '11am', '11:30 am',
    '16:00', '11 baje'. A bare number like '499' is a price, not a time.
    """
    lowered = text.lower()
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", lowered)
    if match:
        return _parse_clock(match.group(0))
    match = re.search(r"\b(\d{1,2}):(\d{2})\b", lowered)
    if match:
        return _parse_clock(match.group(0))
    match = re.search(r"\b(\d{1,2})\s*baje\b", lowered)
    if match:
        hour = int(match.group(1))
        if hour > 12:
            return None
        # "11 baje" is morning, "4 baje" is afternoon; 12 baje is noon.
        return _parse_clock(f"{hour} {'pm' if hour <= 7 else 'am'}")
    return None


class BookingStore:
    """Bookings persisted as JSON; path=None keeps them in memory only."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._bookings = []
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._bookings = data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                self._bookings = []

    def _save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._bookings, indent=2), encoding="utf-8")

    def bookings_on(self, day):
        key = day.isoformat()
        return [b for b in self._bookings if b["date"] == key]

    def all_bookings(self):
        return sorted(
            self._bookings, key=lambda b: (b["date"], b["start_min"])
        )

    def is_free(self, day, start_min, duration_min):
        end = start_min + duration_min
        for other in self.bookings_on(day):
            if start_min < other["start_min"] + other["duration_min"] and (
                other["start_min"] < end
            ):
                return False
        return True

    def free_slots(self, day, service, hours_text):
        """Start times (minutes) still free on a day, from opening hours."""
        span = parse_hours_range(hours_text)
        if span is None:
            return []
        open_min, close_min = span
        slots = []
        start = open_min
        while start + service.duration_min <= close_min:
            if self.is_free(day, start, service.duration_min):
                slots.append(start)
            start += service.duration_min
        return slots

    def book(self, phone, name, service, day, start_min):
        """Confirm a booking if the slot is free; returns it, else None."""
        if not self.is_free(day, start_min, service.duration_min):
            return None
        booking = {
            "phone": phone,
            "name": name or "",
            "service": service.name,
            "price": service.price,
            "date": day.isoformat(),
            "start_min": start_min,
            "duration_min": service.duration_min,
            "created_ts": int(time.time()),
        }
        self._bookings.append(booking)
        self._save()
        return booking

    def cancel_latest(self, phone):
        """Cancel the customer's most recent upcoming booking; returns it."""
        today = date.today().isoformat()
        mine = [
            b for b in self._bookings
            if b["phone"] == phone and b["date"] >= today
        ]
        if not mine:
            return None
        latest = sorted(mine, key=lambda b: (b["date"], b["start_min"]))[-1]
        self._bookings.remove(latest)
        self._save()
        return latest
