"""Lead capture: every new phone number becomes a lead, stored as JSON.

A JSON file, not a database: a salon sees dozens of leads a day, and a single
file keeps deployment trivial. A real database is a problem for later, when
volume says so.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
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


class LeadStore:
    def __init__(self, path):
        self.path = Path(path)
        self._leads = {}
        self._save_lock = threading.Lock()
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._leads = data if isinstance(data, dict) else {}
            except (json.JSONDecodeError, OSError):
                # A corrupt file (killed mid-write, bad disk) must not take the
                # service down. Lose the old leads, keep receiving new ones.
                self._leads = {}

    def _save(self):
        with self._save_lock:
            _atomic_write(self.path, json.dumps(self._leads, indent=2))

    def record_message(self, phone, text, intent, sender_name="", wamid=None):
        now = int(time.time())
        lead = self._leads.get(phone)
        if lead is None:
            lead = {
                "phone": phone,
                "name": sender_name or "",
                "first_seen": now,
                "last_seen": now,
                "message_count": 0,
                "intents": [],
                "messages": [],
            }
            self._leads[phone] = lead
        lead["last_seen"] = now
        lead["message_count"] += 1
        if sender_name and not lead["name"]:
            lead["name"] = sender_name
        if intent and intent not in lead["intents"]:
            lead["intents"].append(intent)
        # The wamid is Meta's id for the incoming message. It is what
        # lets the webhook skip a redelivery instead of answering it twice.
        lead["messages"].append({"ts": now, "text": text, "intent": intent, "wamid": wamid})
        lead["messages"] = lead["messages"][-50:]
        # Save on every message, not in batches. A lost lead is lost business;
        # disk writes are cheap at this volume.
        self._save()
        return lead

    def has_wamid(self, phone, wamid):
        """True if this phone already sent a message with this Meta id."""
        if not wamid:
            return False
        lead = self._leads.get(phone)
        if lead is None:
            return False
        return any(m.get("wamid") == wamid for m in lead["messages"])

    def all_leads(self):
        return sorted(self._leads.values(), key=lambda l: l["last_seen"], reverse=True)

    def get(self, phone):
        return self._leads.get(phone)
