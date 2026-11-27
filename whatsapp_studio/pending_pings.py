"""Held owner pings: handoffs that opened after hours, waiting for
the shop to open.

One entry per held handoff, one JSON file, the same pattern as
handoffs: tolerant load, save on every change, in-memory mode for
tests. A held ping is a promise to a customer who was told they
will hear back by opening plus one hour, so the queue must survive
a restart — in memory would silently drop it on the deploy or
spin-down that will happen overnight.

An entry is {"handoff_id", "phone", "opened_at", "ping_sent_at",
"nudged"}. No due time is stored: the opening is recomputed from
the live profile at sweep time, so an hours edit never strands a
ping. An entry leaves the queue when its handoff resolves, when its
ping fails (the failure is visible on the checklist instead), or
when its nudge has gone out.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path


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


class PendingPingStore:
    """Held owner pings persisted as JSON; path=None keeps them in
    memory only."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._entries = []
        self._save_lock = threading.Lock()
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._entries = data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                self._entries = []

    def _save(self):
        if self.path is None:
            return
        with self._save_lock:
            _atomic_write(self.path, json.dumps(self._entries, indent=2))

    def hold(self, handoff_id, phone, opened_at):
        """Hold the ping for a handoff, or return the entry already
        held for it: one handoff, one held ping, ever."""
        existing = self.get(handoff_id)
        if existing is not None:
            return existing
        entry = {
            "handoff_id": handoff_id,
            "phone": phone,
            "opened_at": opened_at,
            "ping_sent_at": None,
            "nudged": False,
        }
        self._entries.append(entry)
        self._save()
        return entry

    def get(self, handoff_id):
        for entry in self._entries:
            if entry["handoff_id"] == handoff_id:
                return entry
        return None

    def all(self):
        """Held pings, oldest first: the one who waited longest is the
        one the owner calls first."""
        return sorted(self._entries, key=lambda e: e["opened_at"])

    def mark_sent(self, handoff_id, sent_at):
        """The morning ping went out; the entry stays until its nudge
        goes out or the handoff resolves."""
        entry = self.get(handoff_id)
        if entry is None:
            return None
        entry["ping_sent_at"] = sent_at
        self._save()
        return entry

    def mark_nudged(self, handoff_id):
        """The nudge went out (or its window passed after closing).
        Marked in the same write as the send, so a restart between
        the nudge and the drop cannot double-nudge."""
        entry = self.get(handoff_id)
        if entry is None:
            return None
        entry["nudged"] = True
        self._save()
        return entry

    def drop(self, handoff_id):
        """Forget a held ping: its handoff resolved, its ping failed,
        or its nudge is done."""
        entry = self.get(handoff_id)
        if entry is None:
            return None
        self._entries.remove(entry)
        self._save()
        return entry
