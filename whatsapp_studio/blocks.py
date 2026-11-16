"""Times the owner blocked by hand.

A block is a span on one day the owner is not available, lunch, leave,
a festival. The bot treats it like a time that is already taken: it
never offers it and never books over it. A block never deletes a
booking, the dashboard refuses to block over one.

One day at a time only, no recurring blocks. The same pattern as
bookings: tolerant load, save on every change, in-memory mode for tests.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import date
from pathlib import Path


def _atomic_write(path, text):
    """Write a file so a crash mid-write cannot corrupt it: a uniquely
    named temp file in the same directory, then a rename."""
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


class BlockStore:
    """Blocks persisted as JSON; path=None keeps them in memory only."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._blocks = []
        self._save_lock = threading.Lock()
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._blocks = data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                self._blocks = []

    def _save(self):
        if self.path is None:
            return
        with self._save_lock:
            _atomic_write(self.path, json.dumps(self._blocks, indent=2))

    def add(self, day, start_min, end_min, reason=""):
        """Record a block over [start_min, end_min) on a day."""
        block = {
            "id": max((b["id"] for b in self._blocks), default=0) + 1,
            "date": day.isoformat(),
            "start_min": start_min,
            "end_min": end_min,
            "reason": reason or "",
            "created_ts": int(time.time()),
        }
        self._blocks.append(block)
        self._save()
        return block

    def remove(self, block_id):
        """Drop a block; returns it, or None if there is no such one."""
        for block in self._blocks:
            if block["id"] == block_id:
                self._blocks.remove(block)
                self._save()
                return block
        return None

    def all_blocks(self):
        return sorted(
            self._blocks, key=lambda b: (b["date"], b["start_min"])
        )

    def blocks_on(self, day):
        key = day.isoformat() if isinstance(day, date) else day
        return [b for b in self.all_blocks() if b["date"] == key]

    def overlaps(self, day, start_min, end_min):
        """Blocks that clash with [start_min, end_min) on a day."""
        return [
            b for b in self.blocks_on(day)
            if start_min < b["end_min"] and b["start_min"] < end_min
        ]

    def is_blocked(self, day, start_min, duration_min):
        """True when a service slot starting here runs into a block."""
        return bool(
            self.overlaps(day, start_min, start_min + duration_min)
        )
