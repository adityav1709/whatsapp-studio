"""Owner notifications for handoffs, bookings, and failed sends. Best-effort
helpers on top of the dashboard, never a reason for a handoff or a booking
to fail.

The interface is an object with ``notify(business_name, handoff)``,
``notify_booking(business_name, booking)`` and
``notify_send_failed(business_name, entry)``, so a WhatsApp notifier can
plug in later without touching the app. Today there are two
implementations: Telegram (owner-supplied bot token and chat id from the
environment, off by default) and a null one that does nothing. No
credentials live in this repo.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import date

from .bookings import format_date, format_time


class TelegramNotifier:
    """Ping the owner on Telegram the moment a handoff opens, or a
    customer books."""

    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id

    def _send(self, text):
        payload = json.dumps({"chat_id": self.chat_id, "text": text}).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                resp.read()
        except Exception:
            # A failed ping must never break the handoff or the booking:
            # the dashboard still shows it, and the customer still gets
            # their confirmation.
            pass

    def notify(self, business_name, handoff):
        self._send(
            f"Customer needs you — {business_name}\n"
            f"{handoff.get('name') or 'Customer'} ({handoff['phone']})\n"
            f"Reason: {handoff['reason']}\n"
            f"\"{handoff['trigger']}\""
        )

    def notify_booking(self, business_name, booking):
        day = date.fromisoformat(booking["date"])
        self._send(
            f"New booking — {business_name}\n"
            f"{booking.get('name') or 'Customer'} ({booking['phone']})\n"
            f"{booking['service']} — {format_date(day)}, "
            f"{format_time(booking['start_min'])} · Rs {booking['price']}"
        )

    def notify_send_failed(self, business_name, entry):
        """A reply that never reached the customer is the worst failure a
        receptionist can have — the owner hears about it, not just the
        dashboard."""
        self._send(
            f"Message did not send — {business_name}\n"
            f"{entry.get('to')}\n"
            f"\"{(entry.get('body') or '')[:200]}\"\n"
            f"Error: {entry.get('send_error') or 'unknown'}"
        )


class NullNotifier:
    """No notification configured; handoffs and bookings queue on the
    dashboard."""

    def notify(self, business_name, handoff):
        return None

    def notify_booking(self, business_name, booking):
        return None

    def notify_send_failed(self, business_name, entry):
        return None


def notifier_from_env():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return TelegramNotifier(token, chat_id)
    return NullNotifier()
