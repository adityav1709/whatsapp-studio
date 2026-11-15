"""Booking slots with real availability.

A booking is a service at a start time on a date, stored as JSON next to
the leads file. Slots come from the business's opening hours and the
service's duration, so a slot the bot offers is a slot that is actually
free — double-bookings are checked before anything is confirmed.

All times are India time (Asia/Kolkata, a fixed +5:30 offset — India has
no DST, so no tzdata is needed). The server may run UTC; the shop is in
India, so every date decision goes through india_now()/india_today().
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# India has no DST, so a fixed offset is exact — no zoneinfo, no tzdata.
IST = timezone(timedelta(hours=5, minutes=30))


def india_now():
    """Current time in India."""
    return datetime.now(IST)


def india_today():
    """Today's date in India."""
    return india_now().date()


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
# Relative times of day, used when no clock time is given: "aaj shaam".
_RELATIVE_TIMES = {"subah": 11 * 60, "dopahar": 15 * 60, "shaam": 18 * 60, "sham": 18 * 60}


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


def is_closed(hours_text):
    """True when the shop is shut that day ('closed' as the hours value)."""
    return (hours_text or "").strip().lower() == "closed"


def parse_day(text, today=None):
    """A date mentioned in a message, or None.

    Understands 'today', 'tomorrow' (and Hinglish 'aaj'/'kal'/'parso'),
    weekday names, and dates like '15 nov' or '15/11'.
    """
    today = today or india_today()
    lowered = text.lower()
    if re.search(r"\b(today|aaj)\b", lowered):
        return today
    if re.search(r"\b(tomorrow|kal)\b", lowered):
        return today + timedelta(days=1)
    if re.search(r"\bparso\b", lowered):
        return today + timedelta(days=2)
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
    '16:00', '11 baje', or a time of day ('subah', 'dopahar', 'shaam').
    A bare number like '499' is a price, not a time.
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
        # "11 baje" is morning, "4 baje" is afternoon, "12 baje" is noon.
        if hour == 12:
            return _parse_clock("12 pm")
        return _parse_clock(f"{hour} {'pm' if hour <= 7 else 'am'}")
    for word, minutes in _RELATIVE_TIMES.items():
        if re.search(rf"\b{word}\b", lowered):
            return minutes
    return None


def has_relative_time(text):
    """True when the time in a message is a time of day ('subah') rather
    than a clock reading — the caller may treat it as the next such time."""
    lowered = text.lower()
    if not re.search(r"\b(subah|dopahar|shaam|sham)\b", lowered):
        return False
    if re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", lowered):
        return False
    if re.search(r"\b\d{1,2}:\d{2}\b", lowered):
        return False
    if re.search(r"\b\d{1,2}\s*baje\b", lowered):
        return False
    return True


def validate_slot(day, start_min, service, hours_text):
    """True only when a booking at this start is one the shop can keep:
    inside opening hours, on the slot grid (multiples of the service
    duration from opening, matching free_slots), and finishing at or
    before closing. A closed day is never valid."""
    if is_closed(hours_text):
        return False
    span = parse_hours_range(hours_text)
    if span is None:
        return False
    open_min, close_min = span
    if start_min < open_min:
        return False
    if start_min + service.duration_min > close_min:
        return False
    return (start_min - open_min) % service.duration_min == 0


def _atomic_write(path, text):
    """Write a file so a crash mid-write cannot corrupt it: a uniquely
    named temp file in the same directory, then a rename. The server
    handles webhooks on threads, so the temp name must be unique per
    write — two webhooks at the same moment must never share one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class BookingStore:
    """Bookings persisted as JSON; path=None keeps them in memory only."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._bookings = []
        self._save_lock = threading.Lock()
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._bookings = data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                self._bookings = []

    def _save(self):
        if self.path is None:
            return
        with self._save_lock:
            _atomic_write(self.path, json.dumps(self._bookings, indent=2))

    def bookings_on(self, day):
        key = day.isoformat()
        return [b for b in self._bookings if b["date"] == key]

    def all_bookings(self):
        return sorted(
            self._bookings, key=lambda b: (b["date"], b["start_min"])
        )

    def overlapping(self, day, start_min, duration_min):
        """Bookings that clash with a slot starting here."""
        end = start_min + duration_min
        return [
            b for b in self.bookings_on(day)
            if start_min < b["start_min"] + b["duration_min"]
            and b["start_min"] < end
        ]

    def is_free(self, day, start_min, duration_min, capacity=1):
        """Free when fewer than `capacity` bookings overlap the slot.
        Capacity is per service (a two-chair salon takes two at once);
        the default of 1 is the old behaviour: any overlap refuses."""
        return len(self.overlapping(day, start_min, duration_min)) < capacity

    def free_slots(self, day, service, hours_text):
        """Start times (minutes) still free on a day, from opening hours."""
        if is_closed(hours_text):
            return []
        span = parse_hours_range(hours_text)
        if span is None:
            return []
        open_min, close_min = span
        slots = []
        start = open_min
        while start + service.duration_min <= close_min:
            if self.is_free(day, start, service.duration_min, service.capacity):
                slots.append(start)
            start += service.duration_min
        return slots

    def book(self, phone, name, service, day, start_min, price=None):
        """Confirm a booking if the slot is free; returns it, else None.

        The price is the list price unless the caller passes an agreed
        price (a negotiation the LLM settled); the caller validates it
        against the floor before calling."""
        if not self.is_free(day, start_min, service.duration_min, service.capacity):
            return None
        booking = {
            "phone": phone,
            "name": name or "",
            "service": service.name,
            "price": service.price if price is None else price,
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
        today = india_today().isoformat()
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

    def update_latest_name(self, phone, name):
        """Put a name on the customer's most recent upcoming booking."""
        today = india_today().isoformat()
        mine = [
            b for b in self._bookings
            if b["phone"] == phone and b["date"] >= today
        ]
        if not mine:
            return None
        latest = sorted(mine, key=lambda b: (b["date"], b["start_min"]))[-1]
        latest["name"] = name
        self._save()
        return latest
