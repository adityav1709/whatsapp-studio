"""Human handoffs: when a customer asks for a person, or the bot fails
them twice in a row, the conversation is handed to the business owner.

A handoff is a small record, one JSON file, the same pattern as bookings:
tolerant load, save on every change, in-memory mode for tests. While a
handoff is open the bot stays quiet for that customer; the owner marks
it resolved on the dashboard and the bot resumes from the next message.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path


def age_text(opened_at, now=None):
    """How long a handoff has been open, in plain words."""
    seconds = max(0, int((now or time.time()) - opened_at))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h {minutes % 60}m"
    return f"{hours // 24}d {hours % 24}h"


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


class HandoffStore:
    """Handoffs persisted as JSON; path=None keeps them in memory only."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._handoffs = []
        self._save_lock = threading.Lock()
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._handoffs = data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                self._handoffs = []

    def _save(self):
        if self.path is None:
            return
        with self._save_lock:
            _atomic_write(self.path, json.dumps(self._handoffs, indent=2))

    def open(self, phone, name, reason, trigger_message):
        """Open a handoff, or return the open one if there already is one."""
        existing = self.open_for(phone)
        if existing is not None:
            return existing
        handoff = {
            "id": max((h["id"] for h in self._handoffs), default=0) + 1,
            "phone": phone,
            "name": name or "",
            "reason": reason,
            "trigger": trigger_message,
            "opened_at": int(time.time()),
            "resolved_at": None,
            "status": "open",
        }
        self._handoffs.append(handoff)
        self._save()
        return handoff

    def open_for(self, phone):
        for handoff in self._handoffs:
            if handoff["phone"] == phone and handoff["status"] == "open":
                return handoff
        return None

    def resolve(self, handoff_id):
        """Close an open handoff; returns it, or None if there is no such one."""
        for handoff in self._handoffs:
            if handoff["id"] == handoff_id and handoff["status"] == "open":
                handoff["status"] = "resolved"
                handoff["resolved_at"] = int(time.time())
                self._save()
                return handoff
        return None

    def all_handoffs(self):
        return sorted(
            self._handoffs,
            key=lambda h: (h["status"] != "open", -h["opened_at"]),
        )
