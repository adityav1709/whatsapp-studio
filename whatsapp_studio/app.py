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
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .bookings import BookingStore, parse_day, parse_time
from .intents import BOOKING, CANCEL, PRICE, detect_intent
from .replies import (
    booking_cancelled,
    booking_confirmed,
    booking_none_to_cancel,
    booking_taken,
    build_reply,
    slots_available,
)


class Studio:
    """Holds the business profile, lead store and outgoing replies."""

    def __init__(self, profile, lead_store, verify_token="studio-demo", bookings=None):
        self.profile = profile
        self.leads = lead_store
        self.bookings = bookings if bookings is not None else BookingStore()
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
        if intent == BOOKING and service is not None:
            reply = self._handle_booking(phone, text, sender_name, service)
        elif intent == CANCEL:
            cancelled = self.bookings.cancel_latest(phone)
            reply = (
                booking_cancelled(self.profile, cancelled)
                if cancelled
                else booking_none_to_cancel(self.profile)
            )
        else:
            reply = build_reply(self.profile, intent, text, service=service)
        self.leads.record_message(phone, text, intent, sender_name)
        entry = {"to": phone, "body": reply, "intent": intent}
        self.outbox.append(entry)
        return entry

    def _handle_booking(self, phone, text, sender_name, service):
        """Book a slot, or show free slots, from a booking message."""
        today = date.today()
        day = parse_day(text, today) or today
        start = parse_time(text)
        if start is None:
            slots = self.bookings.free_slots(
                day, service, self.profile.hours_today(day.weekday())
            )
            return slots_available(self.profile, service, day, slots)
        if day == today and start <= datetime.now().hour * 60 + datetime.now().minute:
            slots = self.bookings.free_slots(
                day, service, self.profile.hours_today(day.weekday())
            )
            future = [
                s for s in slots
                if s > datetime.now().hour * 60 + datetime.now().minute
            ]
            if not future:
                return slots_available(self.profile, service, day, [])
            return (
                f"That time has already passed today. "
                + slots_available(self.profile, service, day, future)
            )
        booking = self.bookings.book(phone, sender_name, service, day, start)
        if booking is not None:
            return booking_confirmed(self.profile, booking)
        alternatives = [
            s
            for s in self.bookings.free_slots(
                day, service, self.profile.hours_today(day.weekday())
            )
            if abs(s - start) <= 120
        ]
        return booking_taken(self.profile, service, day, start, alternatives)

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
        from .bookings import format_date, format_time

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
        today = date.today()
        todays = self.bookings.bookings_on(today)
        book_rows = []
        for b in todays:
            book_rows.append(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>Rs {}</td></tr>".format(
                    html.escape(b.get("name") or "—"),
                    html.escape(b["phone"]),
                    html.escape(b["service"]),
                    html.escape(format_time(b["start_min"])),
                    b["price"],
                )
            )
        book_body = "".join(book_rows) or (
            '<tr><td colspan="5">No bookings today.</td></tr>'
        )
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>{} — WhatsApp Studio</title></head><body>"
            "<h1>{}</h1><p>AI receptionist on WhatsApp. Replies generated: {}.</p>"
            "<h2>Bookings today ({})</h2>"
            "<table border='1' cellpadding='6'>"
            "<tr><th>Name</th><th>Phone</th><th>Service</th><th>Time</th><th>Price</th></tr>"
            "{}"
            "</table>"
            "<h2>Leads</h2>"
            "<table border='1' cellpadding='6'>"
            "<tr><th>Name</th><th>Phone</th><th>Messages</th><th>Intents</th><th>Last message</th></tr>"
            "{}"
            "</table></body></html>"
        ).format(
            html.escape(self.profile.name),
            html.escape(self.profile.name),
            len(self.outbox),
            format_date(today),
            book_body,
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
            elif parsed.path == "/bookings":
                self._send_json(200, {"bookings": studio.bookings.all_bookings()})
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
