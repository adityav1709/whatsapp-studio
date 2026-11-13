"""HTTP service: WhatsApp webhook, lead list, tiny dashboard.

Runs on the Python standard library only — no dependencies to install.

Demo mode: replies are generated and recorded in the outbox, NOT sent to
Meta. Sending for real needs WHATSAPP_TOKEN and PHONE_NUMBER_ID set and
the Meta WhatsApp Cloud API; see README.md.
"""
from __future__ import annotations

import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .intents import BOOKING, PRICE, detect_intent
from .replies import build_reply


class Studio:
    """Holds the business profile, lead store and outgoing replies."""

    def __init__(self, profile, lead_store, verify_token="studio-demo"):
        self.profile = profile
        self.leads = lead_store
        self.verify_token = verify_token
        # Replies sit here in demo mode instead of going to Meta, so the owner can
        # read exactly what would have been sent before anything reaches a customer.
        self.outbox = []
        self.live = bool(os.environ.get("WHATSAPP_TOKEN"))

    def handle_message(self, phone, text, sender_name=""):
        intent = detect_intent(text)
        service = None
        if intent in (PRICE, BOOKING):
            service = self.profile.service_named(text)
        reply = build_reply(self.profile, intent, text, service=service)
        self.leads.record_message(phone, text, intent, sender_name)
        entry = {"to": phone, "body": reply, "intent": intent}
        self.outbox.append(entry)
        return entry

    def handle_webhook_payload(self, payload):
        """Process a Meta WhatsApp Cloud API webhook payload. Returns count."""
        count = 0
        # Meta mixes status updates and non-text messages into the same payload
        # shape. Skip anything that isn't a text message rather than letting
        # one odd entry kill the whole batch.
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                contacts = {
                    c.get("wa_id"): (c.get("profile") or {}).get("name", "")
                    for c in value.get("contacts", []) or []
                }
                for msg in value.get("messages", []) or []:
                    if msg.get("type") != "text":
                        continue
                    phone = msg.get("from", "")
                    text = (msg.get("text") or {}).get("body", "")
                    if not phone:
                        continue
                    self.handle_message(phone, text, contacts.get(phone, ""))
                    count += 1
        return count

    def dashboard_html(self):
        leads = self.leads.all_leads()
        # Names and message text come from strangers on WhatsApp, so everything
        # gets escaped: unescaped, a crafted name would run script in the
        # owner's own dashboard.
        rows = []
        for lead in leads:
            last = lead["messages"][-1]["text"] if lead["messages"] else ""
            rows.append(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                    html.escape(lead.get("name") or "—"),
                    html.escape(lead["phone"]),
                    lead["message_count"],
                    html.escape(", ".join(lead["intents"])),
                    html.escape(last[:80]),
                )
            )
        body = "".join(rows) or '<tr><td colspan="5">No leads yet.</td></tr>'
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>{} — WhatsApp Studio</title></head><body>"
            "<h1>{}</h1><p>AI receptionist on WhatsApp. Replies generated: {}.</p>"
            "<table border='1' cellpadding='6'>"
            "<tr><th>Name</th><th>Phone</th><th>Messages</th><th>Intents</th><th>Last message</th></tr>"
            "{}"
            "</table></body></html>"
        ).format(
            html.escape(self.profile.name),
            html.escape(self.profile.name),
            len(self.outbox),
            body,
        )


def make_handler(studio):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep test/demo output clean
            pass

        def _send_json(self, status, obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_text(self, status, text, content_type="text/plain"):
            data = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send_json(200, {"status": "ok", "business": studio.profile.name})
            elif parsed.path == "/webhook":
                qs = parse_qs(parsed.query)
                mode = qs.get("hub.mode", [""])[0]
                token = qs.get("hub.verify_token", [""])[0]
                challenge = qs.get("hub.challenge", [""])[0]
                if mode == "subscribe" and token == studio.verify_token and challenge:
                    self._send_text(200, challenge)
                else:
                    self._send_json(403, {"error": "verification failed"})
            elif parsed.path == "/leads":
                self._send_json(200, {"leads": studio.leads.all_leads()})
            elif parsed.path == "/":
                self._send_text(200, studio.dashboard_html(), "text/html")
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path != "/webhook":
                self._send_json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid json"})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "invalid payload"})
                return
            count = studio.handle_webhook_payload(payload)
            self._send_json(200, {"received": True, "processed": count})

    return Handler


def make_server(studio, host="127.0.0.1", port=0):
    return ThreadingHTTPServer((host, port), make_handler(studio))
