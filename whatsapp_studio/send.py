"""Live sending: reply to customers on WhatsApp through Meta's Cloud API.

Same shape as notify.py: a small injectable sender, stdlib urllib POST,
credentials from the environment, off by default. The sender only sends
what it is given — the Studio decides who may be sent to, and the caller
records the outcome. No credentials live in this repo.
"""
from __future__ import annotations

import json
import os
import urllib.request

GRAPH_API_VERSION = "v21.0"
MAX_TEXT_CHARS = 4096


class WhatsAppSender:
    """Send a plain-text WhatsApp message through Meta's Cloud API."""

    def __init__(self, token, phone_number_id):
        self.token = token
        self.phone_number_id = phone_number_id

    def send(self, to, body):
        """Send one text message. Returns Meta's message id (wamid).

        A body over Meta's 4096-character cap is truncated, not rejected:
        the customer still gets the gist, and the caller records that it
        was cut. Any transport or API failure raises; the caller records
        it and never lets it break message handling.
        """
        if len(body) > MAX_TEXT_CHARS:
            body = body[:MAX_TEXT_CHARS]
        payload = json.dumps(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"body": body},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{self.phone_number_id}/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        messages = data.get("messages") or []
        if not messages or not messages[0].get("id"):
            raise ValueError("Meta send succeeded but returned no message id")
        return messages[0]["id"]


def sender_from_env():
    """A real sender only when BOTH env vars are set; otherwise None.

    A token without a phone number id cannot send anything, so one var
    alone keeps the server in demo mode.
    """
    token = os.environ.get("WHATSAPP_TOKEN")
    phone_number_id = os.environ.get("PHONE_NUMBER_ID")
    if token and phone_number_id:
        return WhatsAppSender(token, phone_number_id)
    return None
