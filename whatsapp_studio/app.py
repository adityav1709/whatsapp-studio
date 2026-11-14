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
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .bookings import BookingStore, parse_day, parse_time
from .handoffs import HandoffStore, age_text
from .intents import BOOKING, CANCEL, FALLBACK, HUMAN, PRICE, detect_intent
from .notify import notifier_from_env
from .replies import (
    booking_cancelled,
    booking_confirmed,
    booking_none_to_cancel,
    booking_taken,
    build_reply,
    handoff_confirmation,
    slots_available,
)
from .send import MAX_TEXT_CHARS, sender_from_env


class Studio:
    """Holds the business profile, lead store and outgoing replies."""

    def __init__(self, profile, lead_store, verify_token="studio-demo", bookings=None, handoffs=None, notifier=None, sender=None, dry_run=None):
        self.profile = profile
        self.leads = lead_store
        self.bookings = bookings if bookings is not None else BookingStore()
        self.handoffs = handoffs if handoffs is not None else HandoffStore()
        self.notifier = notifier if notifier is not None else notifier_from_env()
        self.sender = sender if sender is not None else sender_from_env()
        # Dry run forces record-only even when a token is set, so a pilot
        # can watch exactly what would be sent before anything leaves.
        self.dry_run = dry_run if dry_run is not None else os.environ.get("WHATSAPP_DRY_RUN") == "1"
        self.sending_disabled = False
        self._send_threads = []
        self.verify_token = verify_token
        # Replies sit here in demo mode instead of going to Meta, so the owner can
        # read exactly what would have been sent before anything reaches a customer.
        self.outbox = []

    @property
    def live(self):
        """True only when replies are actually being sent: a sender exists,
        dry-run is off, and the kill switch has not been hit. A token
        without a phone number id cannot send, so it is not live."""
        return self.sender is not None and not self.dry_run and not self.sending_disabled

    def sending_state(self):
        if self.sender is None:
            return "demo"
        if self.dry_run:
            return "dry-run"
        if self.sending_disabled:
            return "paused"
        return "live"

    def disable_sending(self):
        """Kill switch: stop all sending instantly. Replies keep being
        recorded, so the owner can see what would have gone out."""
        self.sending_disabled = True

    def enable_sending(self):
        self.sending_disabled = False

    def wait_for_sends(self, timeout=5):
        """Block until in-flight sends finish. For tests and the demo;
        the webhook never waits on this."""
        deadline = time.time() + timeout
        for thread in list(self._send_threads):
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        self._send_threads = [t for t in self._send_threads if t.is_alive()]

    def _valid_recipient(self, to):
        """The reply goes only to the phone the message came from: digits
        only, never the business's own number. Anything else is refused
        and recorded, never sent."""
        digits = to[1:] if to.startswith("+") else to
        if not digits or not digits.isdigit():
            return False
        business = "".join(c for c in (self.profile.phone or "") if c.isdigit())
        return not business or digits != business

    def _dispatch(self, entry):
        """Send a recorded reply, if live. Never raises, never blocks.

        The outbox is the audit trail: the entry is already recorded
        before this runs, and this only fills in what happened to it.
        """
        entry["send_status"] = "recorded"
        entry["wamid"] = None
        entry["send_error"] = None
        body = entry.get("body")
        if body is None or not self.live:
            return
        if not self._valid_recipient(entry["to"]):
            entry["send_status"] = "failed"
            entry["send_error"] = "invalid recipient"
            return
        if len(body) > MAX_TEXT_CHARS:
            body = body[:MAX_TEXT_CHARS]
            entry["body"] = body
            entry["truncated"] = True
        entry["send_status"] = "sending"
        thread = threading.Thread(target=self._send_entry, args=(entry,), daemon=True)
        thread.start()
        self._send_threads.append(thread)

    def _send_entry(self, entry):
        try:
            wamid = self.sender.send(entry["to"], entry["body"])
        except Exception as exc:
            entry["send_status"] = "failed"
            entry["send_error"] = str(exc) or exc.__class__.__name__
            return
        entry["send_status"] = "sent"
        entry["wamid"] = wamid

    def handle_message(self, phone, text, sender_name="", now=None, wamid=None):
        intent = detect_intent(text)
        # A handoff is total: while one is open the message is recorded for
        # the owner to read, but the bot sends nothing — not even a booking.
        if self.handoffs.open_for(phone) is not None:
            self.leads.record_message(phone, text, intent, sender_name, wamid=wamid)
            return {"to": phone, "body": None, "intent": intent, "paused": True}
        reason = None
        if intent == HUMAN:
            reason = "asked for a person"
        elif intent == FALLBACK and self._fallback_streak(phone) >= 2:
            reason = "couldn't help"
        if reason is not None:
            handoff = self.handoffs.open(phone, sender_name, reason, text)
            reply = handoff_confirmation(self.profile, reason, now)
            self.leads.record_message(phone, text, intent, sender_name, wamid=wamid)
            entry = {"to": phone, "body": reply, "intent": intent, "handoff": handoff["id"]}
            self.outbox.append(entry)
            self._dispatch(entry)
            try:
                self.notifier.notify(self.profile.name, handoff)
            except Exception:
                pass
            return entry
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
            reply = build_reply(self.profile, intent, text, service=service, now=now)
        self.leads.record_message(phone, text, intent, sender_name, wamid=wamid)
        entry = {"to": phone, "body": reply, "intent": intent}
        self.outbox.append(entry)
        self._dispatch(entry)
        return entry

    def _fallback_streak(self, phone):
        """Trailing fallback intents for this phone, counting the message in flight.

        Only messages since the last resolved handoff count: once the owner
        resolves one, a customer who is still confused starts a fresh streak.
        """
        count = 1
        lead = self.leads.get(phone)
        if lead is None:
            return count
        resolved_at = 0
        for handoff in self.handoffs.all_handoffs():
            if handoff["phone"] == phone and handoff["status"] == "resolved":
                resolved_at = max(resolved_at, handoff["resolved_at"] or 0)
        for message in reversed(lead["messages"]):
            if message["ts"] <= resolved_at:
                break
            if message["intent"] == FALLBACK:
                count += 1
            else:
                break
        return count

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
                    # Meta redelivers when it does not get a 200 fast. The
                    # dedup check runs here, synchronously, before any work:
                    # a message id already in the lead record is answered
                    # once, never twice. A payload with no id is processed
                    # normally.
                    wamid = msg.get("id")
                    if wamid and self.leads.has_wamid(phone, wamid):
                        continue
                    self.handle_message(phone, text, contacts.get(phone, ""), wamid=wamid)
                    count += 1
        return count

    def dashboard_html(self):
        from .bookings import format_date, format_time

        open_handoffs = [
            h for h in self.handoffs.all_handoffs() if h["status"] == "open"
        ]
        resolved_handoffs = [
            h for h in self.handoffs.all_handoffs() if h["status"] == "resolved"
        ]
        hand_rows = []
        for handoff in open_handoffs:
            lead = self.leads.get(handoff["phone"])
            since = []
            if lead is not None:
                since = [
                    m["text"]
                    for m in lead["messages"]
                    if m["ts"] >= handoff["opened_at"]
                ]
            urgent = (
                "URGENT — "
                if int(time.time()) - handoff["opened_at"] > 4 * 3600
                else ""
            )
            hand_rows.append(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}{}</td><td>{}</td>"
                "<td><form method='post' action='/handoffs/resolve'>"
                "<input type='hidden' name='handoff_id' value='{}'>"
                "<button type='submit'>Resolve</button></form></td></tr>".format(
                    html.escape(handoff.get("name") or "—"),
                    html.escape(handoff["phone"]),
                    html.escape(handoff["reason"]),
                    html.escape(handoff["trigger"][:80]),
                    urgent,
                    html.escape(age_text(handoff["opened_at"])),
                    "<br>".join(html.escape(m[:120]) for m in since[-10:]) or "—",
                    handoff["id"],
                )
            )
        hand_body = "".join(hand_rows) or (
            '<tr><td colspan="7">No open handoffs.</td></tr>'
        )
        resolved_rows = []
        for handoff in resolved_handoffs[:20]:
            resolved_rows.append(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                    html.escape(handoff.get("name") or "—"),
                    html.escape(handoff["phone"]),
                    html.escape(handoff["reason"]),
                    html.escape(handoff["trigger"][:80]),
                )
            )
        resolved_body = "".join(resolved_rows) or (
            '<tr><td colspan="4">None yet.</td></tr>'
        )
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
        state = self.sending_state()
        if state == "live":
            sending_line = (
                "<p><strong>Sending: LIVE</strong> — replies are being sent on WhatsApp.</p>"
                "<form method='post' action='/sending/disable'>"
                "<button type='submit'>Stop sending</button></form>"
            )
        elif state == "paused":
            sending_line = (
                "<p><strong>Sending: PAUSED</strong> — replies are recorded only.</p>"
                "<form method='post' action='/sending/enable'>"
                "<button type='submit'>Resume sending</button></form>"
            )
        elif state == "dry-run":
            sending_line = (
                "<p><strong>Sending: DRY RUN</strong> — replies are recorded only, "
                "nothing is sent.</p>"
            )
        else:
            sending_line = (
                "<p><strong>Sending: DEMO</strong> — no WhatsApp token configured, "
                "replies are recorded only.</p>"
            )
        out_rows = []
        for entry in reversed(self.outbox[-10:]):
            if entry.get("paused"):
                status = "paused"
            else:
                status = entry.get("send_status") or "recorded"
            detail = ""
            if entry.get("wamid"):
                detail = html.escape(entry["wamid"])
            elif entry.get("send_error"):
                detail = html.escape(entry["send_error"])
            if entry.get("truncated"):
                detail = (detail + " " if detail else "") + "truncated to 4096 chars"
            marker = "SEND FAILED — " if status == "failed" else ""
            out_rows.append(
                "<tr><td>{}</td><td>{}</td><td>{}{}</td><td>{}</td><td>{}</td></tr>".format(
                    html.escape(entry["to"]),
                    html.escape(entry.get("intent") or "—"),
                    marker,
                    html.escape(status),
                    html.escape((entry.get("body") or "")[:80]),
                    detail,
                )
            )
        out_body = "".join(out_rows) or '<tr><td colspan="5">No replies yet.</td></tr>'
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>{} — WhatsApp Studio</title></head><body>"
            "<h1>{}</h1><p>AI receptionist on WhatsApp. Replies generated: {}.</p>"
            "{}"
            "<h2>Recent replies</h2>"
            "<table border='1' cellpadding='6'>"
            "<tr><th>To</th><th>Intent</th><th>Send status</th><th>Reply</th><th>Detail</th></tr>"
            "{}"
            "</table>"
            "<h2>Handoffs (open)</h2>"
            "<table border='1' cellpadding='6'>"
            "<tr><th>Name</th><th>Phone</th><th>Reason</th><th>Trigger</th><th>Open for</th><th>Messages since</th><th></th></tr>"
            "{}"
            "</table>"
            "<h2>Handoffs (resolved)</h2>"
            "<table border='1' cellpadding='6'>"
            "<tr><th>Name</th><th>Phone</th><th>Reason</th><th>Trigger</th></tr>"
            "{}"
            "</table>"
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
            sending_line,
            out_body,
            hand_body,
            resolved_body,
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
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "business": studio.profile.name,
                        "live": studio.live,
                        "sending": studio.sending_state(),
                    },
                )
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
            elif parsed.path == "/handoffs":
                self._send_json(200, {"handoffs": studio.handoffs.all_handoffs()})
            elif parsed.path == "/":
                self._send_text(200, studio.dashboard_html(), "text/html")
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path == "/sending/disable":
                studio.disable_sending()
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/sending/enable":
                studio.enable_sending()
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/handoffs/resolve":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                fields = parse_qs(raw.decode("utf-8", errors="replace"))
                try:
                    handoff_id = int((fields.get("handoff_id") or [""])[0])
                except ValueError:
                    handoff_id = None
                if handoff_id is None or studio.handoffs.resolve(handoff_id) is None:
                    self._send_json(404, {"error": "handoff not found"})
                    return
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
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
