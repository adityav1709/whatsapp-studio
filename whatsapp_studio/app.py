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
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .bookings import (
    IST,
    BookingStore,
    has_relative_time,
    india_now,
    india_today,
    parse_day,
    parse_time,
    validate_slot,
)
from .handoffs import HandoffStore, age_text
from .intents import BOOKING, CANCEL, FALLBACK, HUMAN, PRICE, RESCHEDULE, detect_intent
from .notify import notifier_from_env
from .replies import (
    booking_already_yours,
    booking_ask_when,
    booking_cancelled,
    booking_confirmed,
    booking_invalid_slot,
    booking_none_to_cancel,
    booking_none_to_reschedule,
    booking_reschedule_ask,
    booking_taken,
    build_reply,
    handoff_confirmation,
    slots_available,
)
from .security import check_basic_auth, verify_webhook_signature
from .send import MAX_TEXT_CHARS, sender_from_env


def _atomic_write(path, text):
    """Write a file so a crash mid-write cannot corrupt it: a uniquely
    named temp file in the same directory, then a rename. The server
    handles webhooks on threads, so the temp name must be unique per
    write — two webhooks at the same moment must never share one."""
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
        self._outbox_lock = threading.Lock()
        self.outbox = self._load_outbox()
        # What the bot is waiting for from each phone: the question it
        # asked last ("which day and time?", "which service?", a slot
        # list). The next message from that phone is read in that
        # context, so a customer can book in two or three messages.
        # Transient by nature: a restart mid-conversation drops it.
        self._pending = {}

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
        with self._outbox_lock:
            _atomic_write(self._outbox_path, json.dumps(self.outbox, indent=2))

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
            # A failed send means a customer got silence after asking for
            # something — the owner hears about it, not just the dashboard.
            try:
                self.notifier.notify_send_failed(self.profile.name, entry)
            except Exception:
                pass
            return
        entry["send_status"] = "sent"
        entry["wamid"] = wamid

    def _record_reply(self, phone, text, intent, sender_name, reply, wamid=None):
        """Record the incoming message, queue the reply, and send it if live."""
        self.leads.record_message(phone, text, intent, sender_name, wamid=wamid)
        entry = {"to": phone, "body": reply, "intent": intent}
        self.outbox.append(entry)
        self._dispatch(entry)
        self._save_outbox()
        return entry

    def _resolve_pending(self, phone, text, sender_name, intent, now):
        """Read a message in the context of the question the bot last
        asked this phone. Returns the reply, or None when the message is
        not an answer to that question — then normal handling takes over.
        Only a plain answer counts: a price question, a greeting, or a
        new topic is answered on its own merits, not as a booking answer."""
        pending = self._pending.get(phone)
        if pending is None:
            return None
        kind = pending.get("kind")
        if kind == "booking_ask_service":
            if intent not in (FALLBACK, BOOKING):
                return None
            service = self.profile.service_named(text)
            if service is None:
                return None
            return self._handle_booking(phone, text, sender_name, service, now)
        if kind == "booking_ask_when":
            if intent not in (FALLBACK, BOOKING):
                return None
            service = self.profile.service_named(pending.get("service") or "")
            if service is None:
                return None
            return self._handle_booking(phone, text, sender_name, service, now)
        if kind == "slots_available":
            if intent not in (FALLBACK, BOOKING):
                return None
            service = self.profile.service_named(pending.get("service") or "")
            if service is None:
                return None
            # The message's own day wins over the pending day: "parso
            # 11am" after a slot list for tomorrow is the day after
            # tomorrow, not tomorrow.
            today = (now or india_now()).date()
            day = parse_day(text, today)
            if day is None:
                day = (
                    date.fromisoformat(pending["day"])
                    if pending.get("day")
                    else today
                )
            if text.strip().lower() in (
                "yes", "yeah", "yep", "ok", "okay", "first", "first one",
                "book it", "haan", "ha",
            ):
                slots = pending.get("slots") or []
                if not slots:
                    return None
                return self._confirm_booking(phone, sender_name, service, day, slots[0])
            start = parse_time(text)
            if start is None:
                return None
            return self._confirm_booking(phone, sender_name, service, day, start)
        if kind == "ask_name":
            if intent != FALLBACK:
                return None
            name = text.strip()
            words = name.split()
            if not (1 <= len(words) <= 3) or not all(w.isalpha() for w in words):
                return None
            if parse_day(text) is not None or parse_time(text) is not None:
                return None
            updated = self.bookings.update_latest_name(phone, name)
            self._pending.pop(phone, None)
            if updated is None:
                return None
            return f"Thanks, {name}! I've noted that down. See you then."
        return None

    def handle_message(self, phone, text, sender_name="", now=None, wamid=None):
        intent = detect_intent(text)
        # A handoff is total: while one is open the message is recorded for
        # the owner to read, but the bot sends nothing — not even a booking.
        if self.handoffs.open_for(phone) is not None:
            self._pending.pop(phone, None)
            self.leads.record_message(phone, text, intent, sender_name, wamid=wamid)
            return {"to": phone, "body": None, "intent": intent, "paused": True}
        # An explicit cancel, human request, or reschedule is always a
        # fresh intent — never an answer to the question the bot asked.
        # So is a fresh booking that names its own service: the message
        # is self-contained, and the pending question is abandoned.
        if intent not in (CANCEL, HUMAN, RESCHEDULE) and phone in self._pending:
            if intent == BOOKING and self.profile.service_named(text) is not None:
                self._pending.pop(phone, None)
            else:
                reply = self._resolve_pending(phone, text, sender_name, intent, now)
                if reply is not None:
                    return self._record_reply(phone, text, intent, sender_name, reply, wamid)
        reason = None
        if intent == HUMAN:
            reason = "asked for a person"
        elif intent == FALLBACK and self._fallback_streak(phone) >= 2:
            reason = "couldn't help"
        if reason is not None:
            self._pending.pop(phone, None)
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
            reply = self._handle_booking(phone, text, sender_name, service, now)
        elif intent == BOOKING:
            self._pending[phone] = {"kind": "booking_ask_service"}
            reply = build_reply(self.profile, intent, text, service=service, now=now)
        elif intent == CANCEL:
            self._pending.pop(phone, None)
            cancelled = self.bookings.cancel_latest(phone)
            reply = (
                booking_cancelled(self.profile, cancelled)
                if cancelled
                else booking_none_to_cancel(self.profile)
            )
        elif intent == RESCHEDULE:
            reply = self._handle_reschedule(phone, sender_name, now)
        else:
            lead = self.leads.get(phone)
            returning = lead is not None and lead["message_count"] > 0
            reply = build_reply(
                self.profile, intent, text, service=service, now=now,
                returning=returning,
                customer_name=(lead or {}).get("name", ""),
            )
        return self._record_reply(phone, text, intent, sender_name, reply, wamid)

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

    def _nearest_slots(self, day, service, start, limit=4):
        """Free slots nearest to a requested time, for offering alternatives."""
        free = self.bookings.free_slots(
            day, service, self.profile.hours_today(day.weekday())
        )
        return sorted(free, key=lambda s: abs(s - start))[:limit]

    def _confirm_booking(self, phone, sender_name, service, day, start):
        """Validate a slot, book it, and ping the owner. A slot the shop
        cannot keep is never confirmed — the bot says why and offers the
        nearest real ones instead."""
        hours_text = self.profile.hours_today(day.weekday())
        if not validate_slot(day, start, service, hours_text):
            alternatives = self._nearest_slots(day, service, start)
            self._pending[phone] = {
                "kind": "slots_available",
                "service": service.name,
                "day": day.isoformat(),
                "slots": alternatives,
            }
            return booking_invalid_slot(
                self.profile, service, day, start, hours_text, alternatives
            )
        booking = self.bookings.book(phone, sender_name, service, day, start)
        if booking is None:
            alternatives = self._nearest_slots(day, service, start)
            self._pending[phone] = {
                "kind": "slots_available",
                "service": service.name,
                "day": day.isoformat(),
                "slots": alternatives,
            }
            mine = [
                b for b in self.bookings.overlapping(
                    day, start, service.duration_min
                )
                if b["phone"] == phone
            ]
            if mine:
                return booking_already_yours(
                    self.profile, service, day, start, alternatives
                )
            return booking_taken(self.profile, service, day, start, alternatives)
        self._pending.pop(phone, None)
        # A confirmed booking pings the owner the same way a handoff
        # does: best-effort, never a reason for the booking to fail.
        try:
            self.notifier.notify_booking(self.profile.name, booking)
        except Exception:
            pass
        reply = booking_confirmed(self.profile, booking)
        lead = self.leads.get(phone)
        if not sender_name and not (lead and lead.get("name")):
            self._pending[phone] = {"kind": "ask_name"}
            reply += " May I have your name?"
        return reply

    def _handle_booking(self, phone, text, sender_name, service, now=None):
        """Book a slot, or show free slots, from a booking message."""
        now = now or india_now()
        today = now.date()
        now_min = now.hour * 60 + now.minute
        day = parse_day(text, today) or today
        start = parse_time(text)
        hours_text = self.profile.hours_today(day.weekday())
        if start is None:
            if parse_day(text, today) is None:
                # No day and no time: ask which day and time, and wait
                # for the answer instead of guessing.
                self._pending[phone] = {
                    "kind": "booking_ask_when",
                    "service": service.name,
                }
                return booking_ask_when(self.profile, service)
            slots = self.bookings.free_slots(day, service, hours_text)
            self._pending[phone] = {
                "kind": "slots_available",
                "service": service.name,
                "day": day.isoformat(),
                "slots": slots,
            }
            return slots_available(self.profile, service, day, slots)
        # A time of day ("subah") with no day means the next such time:
        # if it has already passed today, it is tomorrow's.
        if (
            parse_day(text, today) is None
            and has_relative_time(text)
            and day == today
            and start <= now_min
        ):
            day = today + timedelta(days=1)
            hours_text = self.profile.hours_today(day.weekday())
        if day == today and start <= now_min:
            slots = self.bookings.free_slots(day, service, hours_text)
            future = [s for s in slots if s > now_min]
            if not future:
                return slots_available(self.profile, service, day, [])
            self._pending[phone] = {
                "kind": "slots_available",
                "service": service.name,
                "day": day.isoformat(),
                "slots": future,
            }
            return (
                "That time has already passed today. "
                + slots_available(self.profile, service, day, future)
            )
        return self._confirm_booking(phone, sender_name, service, day, start)

    def _handle_reschedule(self, phone, sender_name, now):
        """Move a booking: cancel the latest one, then ask for the new
        day and time. Never silently destroy a booking the customer
        wanted to keep — if there is nothing to move, say so."""
        cancelled = self.bookings.cancel_latest(phone)
        if cancelled is None:
            return booking_none_to_reschedule(self.profile)
        self._pending.pop(phone, None)
        service = self.profile.service_named(cancelled["service"])
        if service is not None:
            self._pending[phone] = {
                "kind": "booking_ask_when",
                "service": service.name,
            }
        return booking_reschedule_ask(self.profile, cancelled)

    def handle_non_text_message(self, phone, msg_type, sender_name="", wamid=None):
        """A voice note, photo, or anything else that is not typed text
        gets an honest answer, a lead record, and a handoff — never
        silence. The bot reads typed messages, not voice notes, and says
        so instead of claiming it will listen later."""
        labels = {
            "audio": "voice note",
            "image": "photo",
            "video": "video",
            "location": "location",
            "sticker": "sticker",
            "document": "document",
        }
        label = labels.get(msg_type, "message")
        # While a handoff is open the bot stays quiet; the message is
        # still recorded for the owner to read.
        if self.handoffs.open_for(phone) is not None:
            self.leads.record_message(
                phone, f"[sent a {label}]", HUMAN, sender_name, wamid=wamid
            )
            return None
        reason = f"sent a {label}"
        handoff = self.handoffs.open(phone, sender_name, reason, f"sent a {label}")
        reply = (
            "I can only read typed messages right now — please type it "
            "and I will help right away."
        )
        self.leads.record_message(
            phone, f"[sent a {label}]", HUMAN, sender_name, wamid=wamid
        )
        entry = {"to": phone, "body": reply, "intent": HUMAN, "handoff": handoff["id"]}
        self.outbox.append(entry)
        self._dispatch(entry)
        self._save_outbox()
        try:
            self.notifier.notify(self.profile.name, handoff)
        except Exception:
            pass
        return entry

    def handle_webhook_payload(self, payload):
        """Process a Meta WhatsApp Cloud API webhook payload. Returns count."""
        count = 0
        # Meta mixes status updates and non-text messages into the same payload
        # shape. Skip status updates, but answer non-text messages honestly
        # rather than dropping them — real customers send voice notes.
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                contacts = {
                    c.get("wa_id"): (c.get("profile") or {}).get("name", "")
                    for c in value.get("contacts", []) or []
                }
                for msg in value.get("messages", []) or []:
                    phone = msg.get("from", "")
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
                    if msg.get("type") != "text":
                        self.handle_non_text_message(
                            phone, msg.get("type", ""), contacts.get(phone, ""), wamid=wamid
                        )
                        count += 1
                        continue
                    text = (msg.get("text") or {}).get("body", "")
                    self.handle_message(phone, text, contacts.get(phone, ""), wamid=wamid)
                    count += 1
        return count

    def dashboard_html(self):
        """The owner's page: who needs them, how today went, did anything
        go wrong. One column, phone first, warm words. The labels the
        tests assert on are kept verbatim. Warm cream, soft shadows,
        serif headings, no photo: this is a work tool, not a landing
        page. The bot does the talking; the owner only steps in when
        he wants to, so the reply box is a small quiet option and the
        call link leads. Order: glance, anything wrong, people waiting,
        bookings and money, what people asked about most, who asked and
        did not book, failed replies only, last few people, export.
        No dash of any kind in the copy here."""
        from .bookings import format_date, format_time

        today = india_today()
        open_handoffs = [
            h for h in self.handoffs.all_handoffs() if h["status"] == "open"
        ]
        resolved = [
            h for h in self.handoffs.all_handoffs() if h["status"] == "resolved"
        ]
        resolved_today = [
            h for h in resolved
            if h.get("resolved_at")
            and datetime.fromtimestamp(h["resolved_at"], tz=IST).date() == today
        ]
        leads = self.leads.all_leads()
        todays = self.bookings.bookings_on(today)
        # A booking with no published price contributes nothing here;
        # the money line below only appears when something was priced.
        rupees_today = sum(b.get("price") or 0 for b in todays)
        tomorrow = today + timedelta(days=1)
        tomorrows = self.bookings.bookings_on(tomorrow)
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        week_bookings = [
            b for b in self.bookings.all_bookings()
            if monday <= date.fromisoformat(b["date"]) <= sunday
        ]
        week_rupees = sum(b.get("price") or 0 for b in week_bookings)
        messaged_today = sum(
            1 for lead in leads
            if datetime.fromtimestamp(lead["last_seen"], tz=IST).date() == today
        )
        waiting = len(open_handoffs)

        def plural(n, one, many):
            return one if n == 1 else many

        if waiting == 0:
            sentence = (
                "All clear, nobody is waiting for you. Today: "
                f"{messaged_today} {plural(messaged_today, 'person', 'people')} messaged, "
                f"{len(todays)} booked."
            )
        else:
            sentence = (
                f"Today: {messaged_today} {plural(messaged_today, 'person', 'people')} messaged, "
                f"{len(todays)} booked, {waiting} waiting for you."
            )
        if todays and rupees_today > 0:
            sentence += f" Bookings today: Rs {rupees_today}."

        money_html = ""
        if todays and rupees_today > 0:
            money_html = (
                "<p class='money'>Rs {}<span>booked today</span></p>".format(
                    rupees_today
                )
            )

        state = self.sending_state()
        if state == "live":
            bot_block = (
                "<p><span class='pill on'>Sending: LIVE</span> "
                "Bot is ON. Replies are being sent on WhatsApp.</p>"
                "<form method='post' action='/sending/disable'>"
                "<button type='submit' class='primary'>Stop sending</button></form>"
            )
            bot_class = "bot"
        elif state == "paused":
            bot_block = (
                "<p><span class='pill off'>Sending: PAUSED</span> "
                "The bot is stopped. It will not reply to anyone.</p>"
                "<form method='post' action='/sending/enable'>"
                "<button type='submit' class='primary'>Resume sending</button></form>"
            )
            bot_class = "bot loud"
        elif state == "dry-run":
            bot_block = (
                "<p><span class='pill idle'>Bot is OFF</span> "
                "Replies are recorded only. Nothing is sent.</p>"
            )
            bot_class = "bot loud"
        else:
            bot_block = (
                "<p><span class='pill idle'>Bot is OFF</span> "
                "The bot is watching and drafting replies. Nothing is sent yet.</p>"
            )
            bot_class = "bot loud"
        bot_section = "<section class='{}'>{}</section>".format(bot_class, bot_block)

        entries = list(reversed(self.outbox))
        failed = [e for e in entries if (e.get("send_status") or "") == "failed"]

        # Anything wrong, first and loud: failed sends and handoffs open
        # more than 4 hours. The bot being off is the loud bot block.
        alert_lines = []
        for entry in failed:
            phone = entry.get("to", "")
            lead = self.leads.get(phone)
            name = (lead.get("name") if lead else None) or phone
            digits = "".join(c for c in phone if c.isdigit())
            alert_lines.append(
                "<p class='alertline'><strong>{}</strong> did not get the reply. "
                "<a href='tel:+{}'>Call {}</a></p>".format(
                    html.escape(name), digits, html.escape(name)
                )
            )
        for handoff in open_handoffs:
            if int(time.time()) - handoff["opened_at"] > 4 * 3600:
                digits = "".join(c for c in handoff["phone"] if c.isdigit())
                name = handoff.get("name") or handoff["phone"]
                alert_lines.append(
                    "<p class='alertline'><strong>{}</strong> has waited {}. "
                    "<a href='tel:+{}'>Call {}</a></p>".format(
                        html.escape(name),
                        html.escape(age_text(handoff["opened_at"])),
                        digits,
                        html.escape(name),
                    )
                )
        alerts_section = ""
        if alert_lines:
            alerts_section = (
                "<section class='alerts'><p class='alerttitle'>Needs you now</p>"
                "{}"
                "</section>"
            ).format("".join(alert_lines))

        avatar_colors = ["#1f6a44", "#c05f33", "#a67c2e", "#7a5a3a"]
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
            name = handoff.get("name")
            display = name or handoff["phone"]
            initial = (name or "?")[:1].upper()
            color = avatar_colors[
                sum(ord(c) for c in handoff["phone"]) % len(avatar_colors)
            ]
            card = "<div class='card{}'>".format(" urgent" if urgent else "")
            card += "<div class='whorow'>"
            card += "<span class='avatar' style='background:{}'>{}</span>".format(
                color, html.escape(initial)
            )
            card += "<div><div class='who'>{}</div>".format(html.escape(display))
            if name:
                card += "<div class='phone'>{}</div>".format(
                    html.escape(handoff["phone"])
                )
            card += "</div></div>"
            card += "<p class='said'>“{}”</p>".format(html.escape(handoff["trigger"]))
            card += "<p class='meta'>{} · waiting {}</p>".format(
                html.escape(handoff["reason"]),
                html.escape(age_text(handoff["opened_at"])),
            )
            if urgent:
                card += "<p class='urgent'>Waiting a long time. Call them soon.</p>"
            told = bot_replies.get(handoff["id"])
            if told:
                card += "<p class='told'>The bot told them: “{}”</p>".format(
                    html.escape(told[:200])
                )
            if since:
                card += "<p class='since'>Since then:</p><p class='said'>“{}”</p>".format(
                    "<br>".join(html.escape(m[:200]) for m in since[-5:])
                )
            card += "<a class='call' href='tel:+{}'>Call {}</a>".format(
                digits, html.escape(display)
            )
            reply_label = "Send reply" if self.live else "Record reply, sending is off"
            card += (
                "<div class='replybox'>"
                "<p class='quiet'>Only if you want to step in. The bot is already talking to them.</p>"
                "<form method='post' action='/handoffs/{}/reply'>"
                "<input type='text' name='text' placeholder='Reply to {}…' required>"
                "<button type='submit' class='soft'>{}</button></form>"
            ).format(
                handoff["id"],
                html.escape(handoff.get("name") or "them"),
                reply_label,
            )
            if not self.live:
                card += (
                    "<p class='quiet'>Sending is off. Your reply will be "
                    "recorded here, not sent on WhatsApp.</p>"
                )
            card += "</div>"
            card += (
                "<form method='post' action='/handoffs/resolve'>"
                "<input type='hidden' name='handoff_id' value='{}'>"
                "<button type='submit' class='resolve'>Resolve</button></form>"
            ).format(handoff["id"])
            card += "</div>"
            cards.append(card)
        waiting_body = "".join(cards) or (
            "<p class='allclear'>All clear, nobody is waiting for you.</p>"
        )

        def booking_rows(bookings):
            rows = []
            for b in bookings:
                digits = "".join(c for c in b["phone"] if c.isdigit())
                price = (
                    "Rs {}".format(b["price"])
                    if b.get("price") is not None
                    else "price to confirm"
                )
                rows.append(
                    "<li><strong>{}</strong> · {} for {} · {} · "
                    "<a href='tel:+{}'>{}</a></li>".format(
                        html.escape(format_time(b["start_min"])),
                        html.escape(b["service"]),
                        html.escape(b.get("name") or b["phone"]),
                        price,
                        digits,
                        html.escape(b["phone"]),
                    )
                )
            return rows

        book_body = "".join(booking_rows(todays)) or "<li>No bookings today.</li>"
        tomorrow_body = "".join(booking_rows(tomorrows)) or "<li>No bookings tomorrow.</li>"

        # What people asked about most today, from the messages the bot
        # answered. Count distinct people per service, not messages, so
        # one chatty customer cannot win it alone. If nothing is clear,
        # the line stays off.
        counts = {s.name: set() for s in self.profile.services}
        for lead in leads:
            for m in lead["messages"]:
                if datetime.fromtimestamp(m["ts"], tz=IST).date() != today:
                    continue
                text = (m.get("text") or "").lower()
                for service in self.profile.services:
                    if service.name.lower() in text:
                        counts[service.name].add(lead["phone"])
        best = max(counts.values(), key=len) if counts else set()
        leaders = [name for name, phones in counts.items() if len(phones) == len(best)]
        most_html = ""
        if best and len(leaders) == 1:
            most_html = "<p class='insight'>Today most people asked about {}.</p>".format(
                html.escape(leaders[0].lower())
            )

        # Who asked about a price or a booking today and has no booking
        # at all. A warm lead list, kept to one line.
        booked_phones = {b["phone"] for b in self.bookings.all_bookings()}
        asked = []
        for lead in leads:
            if datetime.fromtimestamp(lead["last_seen"], tz=IST).date() != today:
                continue
            asked_today = any(
                m.get("intent") in ("price", "booking")
                for m in lead["messages"]
                if datetime.fromtimestamp(m["ts"], tz=IST).date() == today
            )
            if asked_today and lead["phone"] not in booked_phones:
                asked.append(lead.get("name") or lead["phone"])
        nobook_html = ""
        if asked:
            shown = asked[:3]
            extra = len(asked) - len(shown)
            names = ", ".join(shown)
            if extra > 0:
                names += " and {} more".format(extra)
            nobook_html = (
                "<p class='insight'>{} {} asked about a price or a booking "
                "today and did not book: {}.</p>"
            ).format(
                len(asked), plural(len(asked), "person", "people"), html.escape(names)
            )

        # Replies the bot sent, cut to failures only. The owner only
        # cares when a reply failed.
        out_items = []
        for entry in failed[:10]:
            digits = "".join(c for c in entry["to"] if c.isdigit())
            phone_link = "<a href='tel:+{}'>{}</a>".format(
                digits, html.escape(entry["to"])
            )
            body = html.escape((entry.get("body") or "")[:120])
            inner = (
                "<strong>SEND FAILED</strong>. It did not reach the customer, "
                "please call them. {}<br>{}<br><span class='err'>{}</span>"
            ).format(phone_link, body, html.escape(entry.get("send_error") or ""))
            out_items.append("<li class='failed'>{}</li>".format(inner))
        out_body = "".join(out_items) or "<li>All replies went through.</li>"

        # People who messaged, cut to the last few at the very bottom.
        lead_items = []
        for lead in leads[:5]:
            last = lead["messages"][-1]["text"] if lead["messages"] else ""
            digits = "".join(c for c in lead["phone"] if c.isdigit())
            lead_items.append(
                "<li><strong>{}</strong> · <a href='tel:+{}'>{}</a><br>{}</li>".format(
                    html.escape(lead.get("name") or lead["phone"]),
                    digits,
                    html.escape(lead["phone"]),
                    html.escape(last[:120]),
                )
            )
        lead_body = "".join(lead_items) or "<li>No one has messaged yet.</li>"
        if len(leads) > 5:
            lead_body += "<li class='quiet'>Showing the latest 5.</li>"

        css = (
            "body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
            "Roboto,Helvetica,Arial,sans-serif;font-size:17px;line-height:1.6;"
            "color:#3a2f26;background:#faf5ec}"
            ".wrap{max-width:680px;margin:0 auto;padding:28px 20px 48px}"
            "h1{font-family:Georgia,'Times New Roman',serif;font-size:36px;"
            "line-height:1.15;margin:0;color:#2c2118}"
            "h2{font-family:Georgia,'Times New Roman',serif;font-size:24px;"
            "margin:36px 0 10px;color:#2c2118}"
            ".dateline{color:#96836c;font-size:15px;margin:8px 0 0}"
            ".warmline{font-family:Georgia,'Times New Roman',serif;font-style:italic;"
            "font-size:18px;color:#6b5a48;margin:12px 0 28px;line-height:1.5}"
            ".glance{background:#fffdf8;border-radius:16px;padding:22px;"
            "box-shadow:0 2px 16px rgba(90,60,25,.08);margin-bottom:20px}"
            ".today{font-size:19px;line-height:1.5;margin:0 0 16px}"
            ".nums{display:flex;gap:16px}.num{flex:1;text-align:center}"
            ".big{display:block;font-family:Georgia,'Times New Roman',serif;"
            "font-size:42px;font-weight:700;color:#1f6a44;line-height:1.1}"
            ".lbl{font-size:14px;color:#8a7a68}"
            ".money{font-family:Georgia,'Times New Roman',serif;font-size:34px;"
            "font-weight:700;color:#1f6a44;margin:18px 0 0;text-align:center;line-height:1.2}"
            ".money span{display:block;font-family:-apple-system,BlinkMacSystemFont,"
            "'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:14px;"
            "font-weight:400;color:#8a7a68}"
            ".bot{background:#fffdf8;border-radius:16px;padding:20px 22px;"
            "box-shadow:0 2px 16px rgba(90,60,25,.08);margin:0 0 20px}"
            ".bot p{margin:0 0 10px}.bot p:last-child{margin-bottom:0}"
            ".bot.loud{background:#fdf3ee}"
            ".bot.loud p{font-size:19px;font-weight:600;color:#a34a24}"
            ".pill{display:inline-block;padding:5px 14px;border-radius:999px;"
            "font-weight:700;font-size:14px}"
            ".pill.on{background:#d9ecdf;color:#1f6a44}"
            ".pill.off{background:#fbe4da;color:#a34a24}"
            ".pill.idle{background:#f0e8d8;color:#7a6a58}"
            ".alerts{background:#fdf3ee;border-radius:16px;padding:18px 20px;"
            "box-shadow:0 2px 16px rgba(90,60,25,.08);margin:0 0 20px}"
            ".alerttitle{font-family:Georgia,'Times New Roman',serif;font-size:19px;"
            "font-weight:700;color:#a34a24;margin:0 0 8px}"
            ".alertline{color:#a34a24;font-weight:600;font-size:17px;margin:10px 0}"
            ".alertline a{color:#a34a24;font-weight:700}"
            ".tag{font-size:13px;color:#a08a70;font-weight:400}"
            ".card{background:#fffdf8;border-radius:16px;padding:22px;margin:16px 0;"
            "box-shadow:0 2px 16px rgba(90,60,25,.08)}"
            ".card.urgent{background:#fdf6f0}"
            ".whorow{display:flex;align-items:center;gap:14px}"
            ".avatar{width:54px;height:54px;border-radius:50%;display:inline-flex;"
            "align-items:center;justify-content:center;font-family:Georgia,"
            "'Times New Roman',serif;font-size:26px;font-weight:700;color:#fff;flex-shrink:0}"
            ".who{font-family:Georgia,'Times New Roman',serif;font-size:23px;"
            "font-weight:700;color:#2c2118;line-height:1.2}"
            ".phone{color:#96836c;font-size:14px;margin-top:2px}"
            ".call{display:block;text-align:center;background:#1f6a44;color:#fff;"
            "padding:14px 20px;border-radius:12px;font-weight:700;font-size:17px;"
            "text-decoration:none;margin:18px 0 0}"
            ".said{font-family:Georgia,'Times New Roman',serif;font-style:italic;"
            "font-size:18px;line-height:1.5;margin:16px 0 8px}"
            ".meta{color:#96836c;font-size:14px;margin:6px 0}"
            ".told{background:#f6efe0;border-radius:10px;padding:12px 14px;"
            "font-size:15px;color:#5c4f42;margin:12px 0}"
            ".since{color:#96836c;font-size:14px;margin:16px 0 4px}"
            ".urgent{color:#a34a24;font-weight:700;margin:8px 0}"
            ".allclear{font-family:Georgia,'Times New Roman',serif;font-size:20px;"
            "color:#1f6a44;font-weight:700;margin:0}"
            ".quiet{color:#96836c;font-size:14px}"
            ".insight{font-size:16px;color:#5c4f42;margin:10px 0}"
            ".list{list-style:none;padding:0;margin:0}"
            ".list li{background:#fffdf8;border-radius:12px;padding:14px 16px;"
            "margin:10px 0;box-shadow:0 1px 8px rgba(90,60,25,.06)}"
            ".list li.failed{background:#fdf3ee}"
            ".err{color:#a34a24}"
            ".replybox{background:#f8f1e3;border-radius:12px;padding:14px 16px;margin-top:16px}"
            ".replybox .quiet{margin:0 0 4px}"
            "button{min-height:48px;padding:12px 22px;font-size:16px;font-weight:600;"
            "border-radius:12px;border:0;background:#1f6a44;color:#fff;cursor:pointer;"
            "font-family:inherit}"
            "button.primary{width:100%;margin-top:12px}"
            "button.soft{width:100%;min-height:44px;background:#e8dcc8;color:#5c4f42;"
            "font-size:15px;margin-top:8px}"
            "button.resolve{width:100%;min-height:46px;background:#f3ead9;color:#5c4f42;"
            "font-size:15px;margin-top:14px}"
            "input[type=text]{width:100%;box-sizing:border-box;min-height:46px;"
            "padding:11px 14px;font-size:16px;border:1px solid #e5d9c3;border-radius:12px;"
            "margin:8px 0;background:#fffdf8;color:#3a2f26}"
            "a{color:#1f6a44}"
            "@media (max-width:480px){.nums{flex-direction:column;gap:8px}.num{text-align:left}.big{font-size:36px}}"
        )
        # With no priced bookings this week the money part would read
        # "Rs 0", a number nobody set. Leave it off instead.
        week_line = "This week: {} bookings.".format(len(week_bookings))
        if week_rupees > 0:
            week_line = "This week: {} bookings, Rs {}.".format(
                len(week_bookings), week_rupees
            )
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>{} | WhatsApp Studio</title><style>{}</style></head><body>"
            "<div class='wrap'>"
            "<h1>{}</h1>"
            "<p class='dateline'>{}</p>"
            "<p class='warmline'>The bot is answering your customers. You only step in when you want to.</p>"
            "<section class='glance'><p class='today'>{}</p>"
            "<div class='nums'>"
            "<div class='num'><span class='big'>{}</span><span class='lbl'>waiting for you</span></div>"
            "<div class='num'><span class='big'>{}</span><span class='lbl'>bookings today</span></div>"
            "<div class='num'><span class='big'>{}</span><span class='lbl'>people messaged today</span></div>"
            "</div>{}</section>"
            "{}"
            "{}"
            "<h2>People waiting for you <span class='tag'>Handoffs</span></h2>"
            "{}"
            "<p class='quiet'>{} handled today.</p>"
            "<h2>Bookings today ({})</h2>"
            "<ul class='list'>{}</ul>"
            "<h2>Tomorrow ({})</h2>"
            "<ul class='list'>{}</ul>"
            "<p>{}</p>"
            "{}{}"
            "<h2>Replies the bot sent</h2>"
            "<ul class='list'>{}</ul>"
            "<h2>People who messaged</h2>"
            "<ul class='list'>{}</ul>"
            "<p class='quiet'><a href='/export'>Export my data</a>. Download "
            "all leads, bookings and handoffs as one file.</p>"
            "<p class='quiet'>New customers can sign up here: "
            "<a href='https://forms.gle/VbdEaEdXioxzUGGh8'>WhatsApp Studio signup</a></p>"
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
            money_html,
            bot_section,
            alerts_section,
            waiting_body,
            len(resolved_today),
            html.escape(format_date(today)),
            book_body,
            html.escape(format_date(tomorrow)),
            tomorrow_body,
            week_line,
            most_html,
            nobook_html,
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
            elif parsed.path == "/export":
                # The owner's way out and the backup before a redeploy:
                # everything as one downloadable JSON file.
                payload = {
                    "leads": studio.leads.all_leads(),
                    "bookings": studio.bookings.all_bookings(),
                    "handoffs": studio.handoffs.all_handoffs(),
                }
                data = json.dumps(payload, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header(
                    "Content-Disposition",
                    'attachment; filename="whatsapp-studio-export.json"',
                )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
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
            if parsed.path.startswith("/handoffs/") and parsed.path.endswith("/reply"):
                # The owner replies from the dashboard: the text goes to
                # the customer through the existing sender, is recorded in
                # the outbox, and the handoff is resolved — the owner
                # handled it by replying. With sending off, the reply is
                # recorded but not sent, and the page says so.
                try:
                    handoff_id = int(parsed.path.strip("/").split("/")[1])
                except (ValueError, IndexError):
                    self._send_json(404, {"error": "handoff not found"})
                    return
                handoff = next(
                    (
                        h for h in studio.handoffs.all_handoffs()
                        if h["id"] == handoff_id and h["status"] == "open"
                    ),
                    None,
                )
                if handoff is None:
                    self._send_json(404, {"error": "handoff not found"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                fields = parse_qs(raw.decode("utf-8", errors="replace"))
                text = (fields.get("text") or [""])[0].strip()
                if not text:
                    self._send_json(400, {"error": "empty reply"})
                    return
                entry = {
                    "to": handoff["phone"],
                    "body": text,
                    "intent": "handoff_reply",
                    "handoff": handoff_id,
                }
                studio.outbox.append(entry)
                studio._dispatch(entry)
                studio._save_outbox()
                studio.handoffs.resolve(handoff_id)
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_json(404, {"error": "not found"})

    return Handler


def make_server(studio, host="127.0.0.1", port=0):
    return ThreadingHTTPServer((host, port), make_handler(studio))
