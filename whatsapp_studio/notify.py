"""Owner notifications for handoffs. Best-effort helpers on top of the
dashboard, never a reason for a handoff to fail.

The interface is just an object with ``notify(business_name, handoff)``,
so a WhatsApp notifier can plug in later without touching the app. Today
there are two implementations: Telegram (owner-supplied bot token and
chat id from the environment, off by default) and a null one that does
nothing. No credentials live in this repo.
"""
from __future__ import annotations

import json
import os
import urllib.request


class TelegramNotifier:
    """Ping the owner on Telegram the moment a handoff opens."""

    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id

    def notify(self, business_name, handoff):
        text = (
            f"Customer needs you — {business_name}\n"
            f"{handoff.get('name') or 'Customer'} ({handoff['phone']})\n"
            f"Reason: {handoff['reason']}\n"
            f"\"{handoff['trigger']}\""
        )
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
            # A failed ping must never break the handoff: the dashboard
            # still shows it, and the customer still gets their confirmation.
            pass


class NullNotifier:
    """No notification configured; handoffs queue on the dashboard."""

    def notify(self, business_name, handoff):
        return None


def notifier_from_env():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return TelegramNotifier(token, chat_id)
    return NullNotifier()
