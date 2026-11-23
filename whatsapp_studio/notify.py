"""Owner notifications for handoffs, bookings, failed sends, and
onboarding submissions. Best-effort helpers on top of the dashboard,
never a reason for a handoff, a booking, or a form submit to fail.

The interface is an object with ``notify(business_name, handoff)``,
``notify_booking(business_name, booking)``,
``notify_send_failed(business_name, entry)`` and
``notify_onboard(profile, seen_before=False)``, so a WhatsApp notifier
can plug in later without touching the app. Today there are two
implementations: Telegram (owner-supplied bot token and chat id from
the environment, off by default) and a null one that does nothing. No
credentials live in this repo.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import date

from .bookings import format_date, format_time
from .config import DAY_KEYS, DAY_LABELS


def _short(text, limit):
    return (text or "")[:limit]


def _service_summary(service):
    name = _short(service.get("name"), 60)
    if service.get("price") is not None:
        return "{} Rs {}".format(name, service["price"])
    if service.get("price_varies"):
        return "{} price varies".format(name)
    return "{} price to confirm".format(name)


def _hours_line(hours):
    """One line for the hours a profile holds, with "to" instead of a
    dash: the ping lands in Aditya's Telegram, where a dash reads as
    noise."""
    spans = {value for value in hours.values() if value != "closed"}
    if not spans:
        return "Closed every day"
    span = next(iter(spans)).replace(" - ", " to ")
    closed = [
        label
        for key, label in zip(DAY_KEYS, DAY_LABELS)
        if hours.get(key) == "closed"
    ]
    if not closed:
        return "Open {} every day".format(span)
    if len(closed) == 1:
        return "Open {}, closed {}".format(span, closed[0])
    return "Open {}, closed {} and {}".format(
        span, ", ".join(closed[:-1]), closed[-1]
    )


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
            return True
        except Exception:
            # A failed ping must never break the handoff or the booking:
            # the dashboard still shows it, and the customer still gets
            # their confirmation.
            return False

    def notify(self, business_name, handoff):
        self._send(
            f"Customer needs you — {business_name}\n"
            f"{handoff.get('name') or 'Customer'} ({handoff['phone']})\n"
            f"Reason: {handoff['reason']}\n"
            f"\"{handoff['trigger']}\""
        )

    def notify_booking(self, business_name, booking):
        day = date.fromisoformat(booking["date"])
        price = (
            "Rs {}".format(booking["price"])
            if booking.get("price") is not None
            else "price to confirm"
        )
        self._send(
            f"New booking — {business_name}\n"
            f"{booking.get('name') or 'Customer'} ({booking['phone']})\n"
            f"{booking['service']} — {format_date(day)}, "
            f"{format_time(booking['start_min'])} · {price}"
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

    def notify_onboard(self, profile, seen_before=False):
        """A prospect filled in the onboarding form: a lead, not a
        signup. The shop file is still only on her phone, so the last
        line says so plainly — nothing is live yet."""
        headline = "New shop on WhatsApp Studio"
        if seen_before:
            headline += " (submitted before)"
        services = profile.get("services") or []
        shown = [_service_summary(service) for service in services[:3]]
        if len(services) > 3:
            shown.append("and {} more".format(len(services) - 3))
        text = (
            "{}\n"
            "{}, {}, {}\n"
            "{}, https://wa.me/91{}\n"
            "Services: {}\n"
            "Hours: {}\n"
            "She filled in the form. The shop file is still with her, "
            "nothing is live yet."
        ).format(
            headline,
            _short(profile.get("name"), 80),
            _short(profile.get("category"), 40),
            _short(profile.get("city"), 80),
            _short(profile.get("owner_name") or "Owner", 80),
            profile.get("phone") or "",
            ", ".join(shown),
            _hours_line(profile.get("hours") or {}),
        )
        if not self._send(text):
            # The ping is best-effort and nothing is stored, so a failed
            # ping leaves no trace anywhere else: log it, so a missed
            # prospect is at least findable in the server logs.
            print("onboard ping failed for {}".format(_short(profile.get("name"), 80)))


class NullNotifier:
    """No notification configured; handoffs and bookings queue on the
    dashboard."""

    def notify(self, business_name, handoff):
        return None

    def notify_booking(self, business_name, booking):
        return None

    def notify_send_failed(self, business_name, entry):
        return None

    def notify_onboard(self, profile, seen_before=False):
        return None


def notifier_from_env():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return TelegramNotifier(token, chat_id)
    return NullNotifier()
