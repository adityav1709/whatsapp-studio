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

    def _post(self, payload):
        """POST one message payload and return Meta's message id.

        Raises on any transport or API failure, so the caller can tell
        a dead token from an unapproved template from the error body.
        """
        request = urllib.request.Request(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{self.phone_number_id}/messages",
            data=json.dumps(payload).encode("utf-8"),
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

    def send(self, to, body):
        """Send one text message. Returns Meta's message id (wamid).

        A body over Meta's 4096-character cap is truncated, not rejected:
        the customer still gets the gist, and the caller records that it
        was cut. Any transport or API failure raises; the caller records
        it and never lets it break message handling.
        """
        if len(body) > MAX_TEXT_CHARS:
            body = body[:MAX_TEXT_CHARS]
        return self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"body": body},
            }
        )

    def send_template(self, to, template_name, language, parameters):
        """Send one template message. Returns Meta's message id (wamid).

        parameters are plain strings, one per {{1}}, {{2}}... slot in
        the template body, in order. Template messages are the only
        way to reach someone outside the 24-hour window, which is what
        the owner ping needs — but the template must already be
        approved by Meta, or this raises with a 132000 in the error
        body. The caller truncates each parameter first: Meta caps a
        parameter at 1024 chars and rejects the message past it.
        """
        return self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": language},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [
                                {"type": "text", "text": text}
                                for text in parameters
                            ],
                        }
                    ],
                },
            }
        )


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
