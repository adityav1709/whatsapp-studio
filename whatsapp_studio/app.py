"""HTTP service: WhatsApp webhook, lead list, tiny dashboard.

Runs on the Python standard library only — no dependencies to install.

Demo mode: replies are generated and recorded in the outbox, NOT sent to
Meta. Sending for real needs WHATSAPP_TOKEN and PHONE_NUMBER_ID set and
the Meta WhatsApp Cloud API; see README.md.

Security gates (both off by default, both from the environment):
WHATSAPP_APP_SECRET verifies the X-Hub-Signature-256 header on POST
/webhook; DASHBOARD_PASSWORD puts Basic Auth on the dashboard and the
data endpoints. Set both before the server is reachable from the internet.
"""
from __future__ import annotations

import html
import json
import os
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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
from .security import check_basic_auth, verify_webhook_signature
from .send import MAX_TEXT_CHARS, sender_from_env


class Studio:
    """Holds the business profile, lead store and outgoing replies."""

    def __init__(self, profile, lead_store, verify_token="studio-demo", bookings=None, handoffs=None, notifier=None, sender=None, dry_run=None, app_secret=None, dashboard_password=None, outbox_path=None):
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
        # Security gates, off by default: no secret, no verification; no
        # password, no auth. Both come from the environment at runtime.
        self.app_secret = app_secret if app_secret is not None else (os.environ.get("WHATSAPP_APP_SECRET") or None)
        self.dashboard_password = dashboard_password if dashboard_password is not None else (os.environ.get("DASHBOARD_PASSWORD") or None)
        # Replies sit here in demo mode instead of going to Meta, so the owner can
        # read exactly what would have been sent before anything reaches a customer.
        # With a path, the reply history survives a restart the same way
        # leads and bookings do; without one it stays in memory only.
        self._outbox_path = Path(outbox_path) if outbox_path else None
        self.outbox = self._load_outbox()

    def _load_outbox(self):
        if self._outbox_path is None or not self._outbox_path.exists():
            return []
        try:
            data = json.loads(self._outbox_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(data, list):
            return []
        return data[-100:]

    def _save_outbox(self):
        if self._outbox_path is None:
            return
        if len(self.outbox) > 100:
            del self.outbox[:-100]
        self._outbox_path.parent.mkdir(parents=True, exist_ok=True)
        self._outbox_path.write_text(json.dumps(self.outbox, indent=2), encoding="utf-8")

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
            self._save_outbox()
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
        self._save_outbox()
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
            # A confirmed booking pings the owner the same way a handoff
            # does: best-effort, never a reason for the booking to fail.
            try:
                self.notifier.notify_booking(self.profile.name, booking)
            except Exception:
                pass
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
        """The owner's page: who needs them, how today went, did anything
        go wrong. One column, phone first, plain words. The labels the
        tests assert on are kept verbatim."""
        from .bookings import format_date, format_time

        today = date.today()
        open_handoffs = [
            h for h in self.handoffs.all_handoffs() if h["status"] == "open"
        ]
        resolved = [
            h for h in self.handoffs.all_handoffs() if h["status"] == "resolved"
        ]
        resolved_today = [
            h for h in resolved
            if h.get("resolved_at")
            and datetime.fromtimestamp(h["resolved_at"]).date() == today
        ]
        leads = self.leads.all_leads()
        todays = self.bookings.bookings_on(today)
        rupees_today = sum(b["price"] for b in todays)
        messaged_today = sum(
            1 for lead in leads
            if datetime.fromtimestamp(lead["last_seen"]).date() == today
        )
        waiting = len(open_handoffs)

        def plural(n, one, many):
            return one if n == 1 else many

        if waiting == 0:
            sentence = (
                "All clear — nobody is waiting for you. Today: "
                f"{messaged_today} {plural(messaged_today, 'person', 'people')} messaged, "
                f"{len(todays)} booked."
            )
        else:
            sentence = (
                f"Today: {messaged_today} {plural(messaged_today, 'person', 'people')} messaged, "
                f"{len(todays)} booked, {waiting} waiting for you."
            )
        if todays:
            sentence += f" Bookings today: Rs {rupees_today}."

        state = self.sending_state()
        if state == "live":
            bot_block = (
                "<p><span class='pill on'>Sending: LIVE</span> "
                "Bot is ON — replies are being sent on WhatsApp.</p>"
                "<form method='post' action='/sending/disable'>"
                "<button type='submit' class='big'>Stop sending</button></form>"
            )
        elif state == "paused":
            bot_block = (
                "<p><span class='pill off'>Sending: PAUSED</span> "
                "The bot is stopped — it will not reply to anyone.</p>"
                "<form method='post' action='/sending/enable'>"
                "<button type='submit' class='big'>Resume sending</button></form>"
            )
        elif state == "dry-run":
            bot_block = (
                "<p><span class='pill idle'>Bot is OFF</span> "
                "Replies are recorded only — nothing is sent.</p>"
            )
        else:
            bot_block = (
                "<p><span class='pill idle'>Bot is OFF</span> "
                "The bot is watching and drafting replies — nothing is sent yet.</p>"
            )

        bot_replies = {}
        for entry in self.outbox:
            hid = entry.get("handoff")
            if hid is not None:
                bot_replies[hid] = entry.get("body") or ""
        cards = []
        for handoff in open_handoffs:
            lead = self.leads.get(handoff["phone"])
            since = []
            if lead is not None:
                since = [
                    m["text"]
                    for m in lead["messages"]
                    if m["ts"] >= handoff["opened_at"]
                ]
            if since and since[0] == handoff["trigger"]:
                since = since[1:]
            digits = "".join(c for c in handoff["phone"] if c.isdigit())
            urgent = int(time.time()) - handoff["opened_at"] > 4 * 3600
            card = "<div class='card{}'>".format(" urgent" if urgent else "")
            card += "<div class='who'>{}</div>".format(
                html.escape(handoff.get("name") or "—")
            )
            card += "<a class='call' href='tel:+{}'>{}</a>".format(
                digits, html.escape(handoff["phone"])
            )
            card += "<p class='said'>“{}”</p>".format(html.escape(handoff["trigger"]))
            card += "<p class='meta'>{} · waiting {}</p>".format(
                html.escape(handoff["reason"]),
                html.escape(age_text(handoff["opened_at"])),
            )
            if urgent:
                card += "<p class='urgent'>Waiting a long time — call them soon.</p>"
            told = bot_replies.get(handoff["id"])
            if told:
                card += "<p class='bot'>The bot told them: “{}”</p>".format(
                    html.escape(told[:200])
                )
            if since:
                card += "<p class='since'>Since then:</p><p class='said'>“{}”</p>".format(
                    "<br>".join(html.escape(m[:200]) for m in since[-5:])
                )
            card += (
                "<form method='post' action='/handoffs/resolve'>"
                "<input type='hidden' name='handoff_id' value='{}'>"
                "<button type='submit' class='big'>Resolve</button></form>"
            ).format(handoff["id"])
            card += "</div>"
            cards.append(card)
        waiting_body = "".join(cards) or (
            "<p class='allclear'>All clear — nobody is waiting for you.</p>"
        )

        book_items = []
        for b in todays:
            digits = "".join(c for c in b["phone"] if c.isdigit())
            book_items.append(
                "<li><strong>{}</strong> — {} for {} — Rs {} · "
                "<a href='tel:+{}'>{}</a></li>".format(
                    html.escape(format_time(b["start_min"])),
                    html.escape(b["service"]),
                    html.escape(b.get("name") or "—"),
                    b["price"],
                    digits,
                    html.escape(b["phone"]),
                )
            )
        book_body = "".join(book_items) or "<li>No bookings today.</li>"

        entries = list(reversed(self.outbox))
        failed = [e for e in entries if (e.get("send_status") or "") == "failed"]
        rest = [e for e in entries if (e.get("send_status") or "") != "failed"]
        out_items = []
        for entry in (failed + rest)[:10]:
            status = entry.get("send_status") or "recorded"
            digits = "".join(c for c in entry["to"] if c.isdigit())
            phone_link = "<a href='tel:+{}'>{}</a>".format(
                digits, html.escape(entry["to"])
            )
            body = html.escape((entry.get("body") or "")[:120])
            if status == "failed":
                inner = (
                    "<strong>SEND FAILED</strong> — did not reach the customer, "
                    "call them. {}<br>{}<br><span class='err'>{}</span>"
                ).format(phone_link, body, html.escape(entry.get("send_error") or ""))
                out_items.append("<li class='failed'>{}</li>".format(inner))
            else:
                label = {"sent": "Sent", "sending": "Sending…"}.get(status, "Not sent")
                inner = "<strong>{}</strong> {}<br>{}".format(label, phone_link, body)
                if entry.get("truncated"):
                    inner += " <span class='quiet'>cut short — too long to send</span>"
                out_items.append("<li>{}</li>".format(inner))
        out_body = "".join(out_items) or "<li>No replies yet.</li>"

        lead_items = []
        for lead in leads[:50]:
            last = lead["messages"][-1]["text"] if lead["messages"] else ""
            digits = "".join(c for c in lead["phone"] if c.isdigit())
            lead_items.append(
                "<li><strong>{}</strong> · <a href='tel:+{}'>{}</a><br>{}</li>".format(
                    html.escape(lead.get("name") or "—"),
                    digits,
                    html.escape(lead["phone"]),
                    html.escape(last[:120]),
                )
            )
        lead_body = "".join(lead_items) or "<li>No one has messaged yet.</li>"
        if len(leads) > 50:
            lead_body += "<li class='quiet'>Showing the latest 50.</li>"

        css = (
            "body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
            "Roboto,Helvetica,Arial,sans-serif;font-size:16px;line-height:1.5;"
            "color:#1a1a1a;background:#f7f7f5}"
            ".wrap{max-width:640px;margin:0 auto;padding:16px}"
            "h1{font-size:28px;margin:8px 0 0}h2{font-size:20px;margin:28px 0 8px}"
            ".sub{color:#555;margin:4px 0 16px}"
            ".glance{background:#fff;border:1px solid #e3e3e0;border-radius:12px;padding:16px}"
            ".today{font-size:18px;margin:0 0 12px}"
            ".nums{display:flex;gap:12px}.num{flex:1;text-align:center}"
            ".big{display:block;font-size:36px;font-weight:700;color:#128c7e}"
            ".lbl{font-size:13px;color:#555}"
            ".pill{display:inline-block;padding:4px 12px;border-radius:999px;"
            "font-weight:700;font-size:14px}"
            ".pill.on{background:#dcf8c6;color:#075e54}"
            ".pill.off{background:#fde8e8;color:#a50e0e}"
            ".pill.idle{background:#eee;color:#555}"
            ".tag{font-size:12px;color:#888;font-weight:400}"
            ".card{background:#fff;border:1px solid #e3e3e0;border-left:4px solid #25d366;"
            "border-radius:12px;padding:16px;margin:12px 0}"
            ".card.urgent{border-left-color:#d93025}"
            ".who{font-size:20px;font-weight:700}"
            ".call{display:inline-block;margin:8px 0;font-size:17px;color:#128c7e;font-weight:600}"
            ".said{font-size:17px;margin:8px 0}"
            ".meta{color:#555;font-size:14px;margin:4px 0}"
            ".bot{color:#555;font-size:14px;margin:4px 0}"
            ".since{color:#555;font-size:14px;margin:12px 0 0}"
            ".urgent{color:#a50e0e;font-weight:700;margin:4px 0}"
            ".allclear{font-size:18px;color:#075e54;font-weight:700}"
            ".quiet{color:#777;font-size:14px}"
            ".list{list-style:none;padding:0;margin:0}"
            ".list li{background:#fff;border:1px solid #e3e3e0;border-radius:10px;"
            "padding:12px 14px;margin:8px 0}"
            ".list li.failed{border-left:4px solid #d93025}"
            ".err{color:#a50e0e}"
            "button{min-height:44px;padding:10px 20px;font-size:16px;border-radius:10px;"
            "border:1px solid #128c7e;background:#128c7e;color:#fff;cursor:pointer}"
            "button.big{width:100%;margin-top:8px}"
            "a{color:#128c7e}"
            "@media (max-width:480px){.nums{flex-direction:column;gap:4px}.num{text-align:left}}"
        )
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>{} — WhatsApp Studio</title><style>{}</style></head><body>"
            "<div class='wrap'>"
            "<h1>{}</h1><p class='sub'>Your WhatsApp receptionist · {}</p>"
            "<section class='glance'><p class='today'>{}</p>"
            "<div class='nums'>"
            "<div class='num'><span class='big'>{}</span><span class='lbl'>waiting for you</span></div>"
            "<div class='num'><span class='big'>{}</span><span class='lbl'>bookings today</span></div>"
            "<div class='num'><span class='big'>{}</span><span class='lbl'>people messaged today</span></div>"
            "</div></section>"
            "<section class='bot'>{}</section>"
            "<h2>People waiting for you <span class='tag'>Handoffs</span></h2>"
            "{}"
            "<p class='quiet'>{} handled today.</p>"
            "<h2>Bookings today ({})</h2>"
            "<ul class='list'>{}</ul>"
            "<h2>Replies the bot sent</h2>"
            "<ul class='list'>{}</ul>"
            "<h2>People who messaged</h2>"
            "<ul class='list'>{}</ul>"
            "</div></body></html>"
        ).format(
            html.escape(self.profile.name),
            css,
            html.escape(self.profile.name),
            html.escape(format_date(today)),
            html.escape(sentence),
            waiting,
            len(todays),
            messaged_today,
            bot_block,
            waiting_body,
            len(resolved_today),
            html.escape(format_date(today)),
            book_body,
            out_body,
            lead_body,
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

        def _unauthorized(self):
            data = b"authentication required"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="WhatsApp Studio"')
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _require_auth(self):
            """True when this request may proceed. When a dashboard
            password is set, every path except the webhook and /health
            needs Basic Auth — Meta calls the webhook and cannot log in,
            and health checks must stay open."""
            if not studio.dashboard_password:
                return True
            if check_basic_auth(studio.dashboard_password, self.headers.get("Authorization")):
                return True
            self._unauthorized()
            return False

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
            elif not self._require_auth():
                return
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
            if parsed.path == "/webhook":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                # The signature is checked over the raw body before
                # anything is parsed: a request that fails it is never
                # processed. No secret configured means no verification,
                # exactly as before.
                if studio.app_secret and not verify_webhook_signature(
                    studio.app_secret, raw, self.headers.get("X-Hub-Signature-256")
                ):
                    self._send_json(401, {"error": "invalid signature"})
                    return
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
                return
            if not self._require_auth():
                return
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
            self._send_json(404, {"error": "not found"})

    return Handler


def make_server(studio, host="127.0.0.1", port=0):
    return ThreadingHTTPServer((host, port), make_handler(studio))
