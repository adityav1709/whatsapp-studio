"""Missed customers: bookings the bot had to refuse and questions it
could not answer, so the owner can call them back.

A missed record is a person who wanted something, got a no, and left
with nothing. It is not a booking and not a handoff: a refusal does
not pause the bot, and an unanswered question may already be a
handoff. One store holds both kinds, the same shape as bookings and
handoffs: a JSON list, tolerant load, atomic write, in memory mode
for tests.

A refusal the bot fixed in the same turn never lands here. A later
booking for the same phone and service closes the open refused
records for that service as booked, so the owner never chases a
customer who already got what they wanted.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from .bookings import IST, india_today

# Tool errors that count as a refusal worth recording. "Already
# yours" is a win, not a loss, and "bad price" is an internal error
# the customer never saw, so neither is here.
REFUSAL_ERRORS = frozenset(
    {
        "closed",
        "time passed",
        "invalid slot",
        "not free",
        "already taken",
        "below the lowest price",
        "above the list price",
        "fixed price",
        "could not understand the time",
    }
)

# Plain words for the dashboard, one per reason code. The owner
# reads these, never the code.
REASON_TEXT = {
    "closed": "Shop is closed that day.",
    "time passed": "That time had already passed.",
    "invalid slot": "That time does not fit our slots.",
    "not free": "That slot was already taken.",
    "already taken": "That slot was already taken.",
    "below the lowest price": "Price was below the lowest price we accept.",
    "above the list price": "Price was above the list price.",
    "fixed price": "Price is fixed, no discount on this service.",
    "could not understand the time": "Bot did not understand the time.",
    "cannot answer": "Bot did not know the answer.",
    "model failed": "The bot could not reply.",
}

RESOLVED_HOW = ("booked", "called", "dismissed", "answered")

# Demo phones are sample data, never real customers.
_DEMO_PREFIX = "9198100000"

_MAX_TEXT = 280
_RETENTION_DAYS = 30


def plain_reason(reason):
    """Plain words for a reason code, for the dashboard."""
    return REASON_TEXT.get(reason or "", reason or "")


def _atomic_write(path, text):
    """Write a file so a crash mid-write cannot corrupt it: a uniquely
    named temp file in the same directory, then a rename. The server
    handles webhooks on threads, so the temp name must be unique per
    write."""
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


class MissedStore:
    """Missed customers persisted as JSON; path=None keeps them in
    memory only."""

    backend = "json"

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._records = []
        self._save_lock = threading.Lock()
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._records = data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                self._records = []

    def _save(self):
        if self.path is None:
            return
        with self._save_lock:
            _atomic_write(self.path, json.dumps(self._records, indent=2))

    def _prune(self):
        """Drop resolved records older than the retention window.
        Open records are never dropped: dropping one would hide a
        missed customer."""
        cutoff = int(time.time()) - _RETENTION_DAYS * 86400
        kept = [
            r
            for r in self._records
            if r["status"] != "resolved"
            or (r.get("resolved_at") or 0) > cutoff
        ]
        if len(kept) != len(self._records):
            self._records = kept

    def _write(self):
        self._prune()
        self._save()

    def _next_id(self):
        return max((r["id"] for r in self._records), default=0) + 1

    def _base(self, kind, phone, name, now_ts):
        return {
            "id": self._next_id(),
            "kind": kind,
            "phone": phone,
            "name": name or "",
            "created_ts": now_ts,
            "last_seen_ts": now_ts,
            "occurrences": 1,
            "status": "open",
            "resolved_at": None,
            "resolved_how": None,
        }

    def record_refused(self, phone, name, service, day, start_min, reason, alternatives, trigger, now_ts=None, sample=False):
        """Record a refused booking, or bump the open record when the
        same person asked for the same slot again. Returns the record,
        or None for a demo phone, which is never recorded. The seeder
        passes sample=True so the showcase can show the section with
        fake numbers; live traffic never passes it."""
        if (phone or "").startswith(_DEMO_PREFIX) and not sample:
            return None
        now_ts = now_ts if now_ts is not None else int(time.time())
        date_key = day.isoformat() if day is not None else None
        for record in self._records:
            if (
                record["status"] == "open"
                and record["kind"] == "refused"
                and record["phone"] == phone
                and record.get("service") == service
                and record.get("date") == date_key
                and record.get("start_min") == start_min
                and record.get("reason") == reason
            ):
                record["occurrences"] += 1
                record["last_seen_ts"] = now_ts
                record["trigger"] = (trigger or "")[:_MAX_TEXT]
                record["alternatives"] = list(alternatives or [])
                self._write()
                return record
        record = self._base("refused", phone, name, now_ts)
        record.update(
            {
                "service": service or "",
                "date": date_key,
                "start_min": start_min,
                "reason": reason,
                "alternatives": list(alternatives or []),
                "trigger": (trigger or "")[:_MAX_TEXT],
            }
        )
        self._records.append(record)
        self._write()
        return record

    def record_unanswered(self, phone, name, question, reason, handoff_id=None, now_ts=None, sample=False):
        """Record a question the bot could not answer, or bump the
        open record when the same person asked it again. Returns the
        record, or None for a demo phone. The seeder passes sample=True
        so the showcase can show the section with fake numbers; live
        traffic never passes it."""
        if (phone or "").startswith(_DEMO_PREFIX) and not sample:
            return None
        now_ts = now_ts if now_ts is not None else int(time.time())
        for record in self._records:
            if (
                record["status"] == "open"
                and record["kind"] == "unanswered"
                and record["phone"] == phone
                and record.get("question") == (question or "")[:_MAX_TEXT]
            ):
                record["occurrences"] += 1
                record["last_seen_ts"] = now_ts
                if handoff_id is not None:
                    record["handoff_id"] = handoff_id
                self._write()
                return record
        record = self._base("unanswered", phone, name, now_ts)
        record.update(
            {
                "question": (question or "")[:_MAX_TEXT],
                "reason": reason,
                "handoff_id": handoff_id,
            }
        )
        self._records.append(record)
        self._write()
        return record

    def resolve(self, record_id, how):
        """Close an open record; returns it, or None if there is no
        such open record or the reason is not one of the four."""
        if how not in RESOLVED_HOW:
            return None
        for record in self._records:
            if record["id"] == record_id and record["status"] == "open":
                record["status"] = "resolved"
                record["resolved_at"] = int(time.time())
                record["resolved_how"] = how
                self._write()
                return record
        return None

    def resolve_for_booking(self, phone, service):
        """A booking landed for this phone and service: every open
        refused record for them closes as booked."""
        changed = False
        for record in self._records:
            if (
                record["status"] == "open"
                and record["kind"] == "refused"
                and record["phone"] == phone
                and record.get("service") == service
            ):
                record["status"] = "resolved"
                record["resolved_at"] = int(time.time())
                record["resolved_how"] = "booked"
                changed = True
        if changed:
            self._write()

    def resolve_for_handoff(self, handoff_id, how):
        """The owner answered the handoff a question belongs to: the
        linked unanswered records close the same way."""
        if how not in RESOLVED_HOW or handoff_id is None:
            return
        changed = False
        for record in self._records:
            if (
                record["status"] == "open"
                and record["kind"] == "unanswered"
                and record.get("handoff_id") == handoff_id
            ):
                record["status"] = "resolved"
                record["resolved_at"] = int(time.time())
                record["resolved_how"] = how
                changed = True
        if changed:
            self._write()

    def all_records(self):
        return sorted(self._records, key=lambda r: -r["created_ts"])

    def open_newest(self, limit=20):
        """Open records only, newest first."""
        return [
            r for r in self.all_records() if r["status"] == "open"
        ][:limit]

    def open_today(self, today=None):
        """Open records created today, for the glance line."""
        if today is None:
            today = india_today()
        count = 0
        for record in self._records:
            if record["status"] != "open":
                continue
            if (
                datetime.fromtimestamp(record["created_ts"], tz=IST).date()
                == today
            ):
                count += 1
        return count
