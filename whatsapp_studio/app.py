"""HTTP service: WhatsApp webhook, lead list, tiny dashboard.

Runs on the Python standard library only — no dependencies to install.

The brain is an OpenAI language model, reached through
whatsapp_studio/llm.py. The model talks to customers; the deterministic
code here is the hands it calls as tools (price lookup, availability,
booking). A key is required: without LLM_API_KEY or OPENAI_API_KEY the
bot records the message and replies to nobody, and the dashboard says so.

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
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .blocks import BlockStore
from .bookings import (
    IST,
    BookingStore,
    _atomic_write,
    format_date,
    format_time,
    has_relative_time,
    india_now,
    india_today,
    is_closed,
    parse_day,
    parse_hours_range,
    parse_time,
    validate_slot,
)
from .config import (
    DAY_KEYS,
    DAY_LABELS,
    ONBOARDING_DEFAULT_SERVICE_ROWS,
    ONBOARDING_SERVICE_ROWS,
    BusinessProfile,
    validate_onboarding_profile,
    validate_setup_hours,
    validate_setup_services,
)
from .db import JsonOutboxStore
from .demo_data import seed_demo_data
from .handoffs import HandoffStore, age_text
from .leads import DuplicateMessageError
from .llm import (
    LLM_TOOLS,
    MAX_LLM_CHARS,
    MAX_TOOL_ROUNDS,
    build_draft_prompt,
    build_system_prompt,
    openai_tool_calls,
)
from .missed import MissedStore, REFUSAL_ERRORS, plain_reason
from .notify import TelegramNotifier, notifier_from_env
from .security import check_basic_auth, verify_webhook_signature
from .send import MAX_TEXT_CHARS, sender_from_env


def _is_open_now(profile, now):
    hours_text = profile.hours_today(now.weekday())
    if is_closed(hours_text):
        return False
    span = parse_hours_range(hours_text)
    if span is None:
        return True
    open_min, close_min = span
    current = now.hour * 60 + now.minute
    return open_min <= current < close_min


def _ordinal(n):
    """1st, 2nd, 3rd, 4th, and the teens stay th: 11th, 12th, 13th."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def handoff_confirmation(profile, reason, now=None):
    """The one fixed message sent when a handoff opens and there is no
    model to write it: the model failed, so the owner steps in.

    During open hours it says who will reply and gives a real wait.
    After hours it says plainly that the shop is closed and exactly
    when they will hear back. Never a quick-reply promise."""
    now = now or india_now()
    if _is_open_now(profile, now):
        lead = (
            "I couldn't quite get that, so I've passed this to the team"
            if reason == "couldn't reply"
            else "I've passed this to the team"
        )
        return (
            f"{lead} at {profile.name}. They will reply here within "
            "30 minutes during working hours, and I've shared our chat "
            "with them so you don't need to explain again."
        )
    span = parse_hours_range(profile.hours_today(now.weekday()))
    if span is None:
        return (
            "We're closed right now. I've passed your message to the team at "
            f"{profile.name} and they'll reply here when we open. "
            "You don't need to message again."
        )
    open_min, _ = span
    return (
        f"We're closed right now, {profile.name} opens at "
        f"{format_time(open_min)}. I've passed your message to the team "
        f"and you'll hear back here by {format_time(open_min + 60)}. "
        "You don't need to message again."
    )


class Studio:
    """Holds the business profile, lead store and outgoing replies."""

    def __init__(self, profile, lead_store, verify_token="studio-demo", bookings=None, handoffs=None, blocks=None, missed=None, notifier=None, sender=None, dry_run=None, app_secret=None, dashboard_password=None, outbox_path=None, outbox_store=None, llm=None, demo_mode=None, profile_path=None):
        self.profile = profile
        # Where the profile lives on disk, when the server knows it.
        # The setup form saves through this path; without it, a save
        # has nowhere to go and is refused. run.py passes it in.
        self.profile_path = Path(profile_path) if profile_path else None
        # The profile swap on a setup save is one assignment under
        # this lock, so a message handled at the same moment sees the
        # old profile whole or the new one whole, never a mix.
        self._profile_lock = threading.Lock()
        self.leads = lead_store
        self.bookings = bookings if bookings is not None else BookingStore()
        self.handoffs = handoffs if handoffs is not None else HandoffStore()
        self.blocks = blocks if blocks is not None else BlockStore()
        self.missed = missed if missed is not None else MissedStore()
        # What this turn did, reset at the start of _llm_reply: the
        # booking refusals in order, whether a booking landed, and
        # whether an unanswered question was already recorded. A turn
        # that ends in a booking is good service, not a bad
        # experience, so its refusals are never recorded.
        self._turn_refusals = []
        self._turn_booked = False
        self._turn_unanswered = False
        # The negotiation switch, on by default: the owner flips it on
        # the dashboard and the bot stops haggling at once. In memory
        # like the sending kill switch, so a restart turns it back on.
        self.negotiation_on = True
        # One shot notice for the dashboard, set by a form handler and
        # shown once on the next page render, then cleared.
        self._notice = None
        self.notifier = notifier if notifier is not None else notifier_from_env()
        self.sender = sender if sender is not None else sender_from_env()
        # The brain. None means no key is set and the bot cannot reply;
        # run.py builds it from the environment, tests pass a fake.
        self.llm = llm
        # Demo showcase: off by default. With DEMO_MODE=1 the page
        # shows the bot on with sample customers, so a visitor sees
        # the product at work instead of an empty page. A real install
        # keeps saying plainly when it has no key or number.
        self.demo_mode = demo_mode if demo_mode is not None else os.environ.get("DEMO_MODE") == "1"
        # The handoff the hand_over tool opened during this reply, if
        # any, so the reply entry can carry its id.
        self._last_handoff = None
        # Drafts the owner asked the brain for, one per open handoff.
        # A draft is only a suggestion: it prefills the reply box and
        # is never sent. The failed set remembers a draft the model
        # could not produce, so the page says so instead of going quiet.
        self._drafts = {}
        self._draft_failed = set()
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
        # A Postgres install passes its own outbox store instead.
        self._outbox_store = (
            outbox_store
            if outbox_store is not None
            else JsonOutboxStore(outbox_path)
        )
        self._outbox_lock = threading.Lock()
        self.outbox = self._load_outbox()
        # Demo mode never runs against a real database: seeding sample
        # customers into it would put fake bookings on real slots and
        # silence the bot for fake phones. Refuse loudly instead.
        if self.demo_mode and any(
            getattr(s, "backend", "json") == "postgres"
            for s in (self.leads, self.bookings, self.handoffs,
                      self.blocks, self.missed)
        ):
            raise RuntimeError(
                "Demo mode refuses to run against a real database."
                " Unset DEMO_MODE or DATABASE_URL and restart."
            )
        # A dead empty page attracts nobody. In demo mode, and only
        # when the stores are empty, seed the sample day at boot so
        # the dashboard shows the bot at work. Real data is never
        # touched: the seeder refuses when anything is already there.
        if self.demo_mode:
            seed_demo_data(self)

    def _load_outbox(self):
        return self._outbox_store.load()

    def _save_outbox(self):
        if len(self.outbox) > 100:
            del self.outbox[:-100]
        with self._outbox_lock:
            self._outbox_store.save(self.outbox)

    def db_status(self):
        """True when saving works. The JSON stores always work; a
        Postgres store pings the database, so a dead database reads
        as down instead of an empty shop."""
        ping = getattr(self.leads, "ping", None)
        if ping is None:
            return True
        try:
            return bool(ping())
        except Exception:
            return False

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

    def setup_checklist(self):
        """Four things a real install needs before it can serve a shop:
        a brain that replies, a WhatsApp number, services with prices,
        and a way to reach the owner. Each item says what is missing
        and exactly which setting fixes it, so a fresh install can be
        checked in one glance instead of guessed at."""
        items = []
        if self.llm is None:
            items.append(
                ("Bot replies", False, "Set LLM_API_KEY on Render so the bot can reply.")
            )
        elif self.dry_run:
            items.append(
                ("Bot replies", False, "Dry run is on, so replies are recorded but not sent.")
            )
        elif self.sending_disabled:
            items.append(
                ("Bot replies", False, "Sending is paused. Tap Resume sending to switch the bot back on.")
            )
        else:
            items.append(("Bot replies", True, ""))
        if self.sender is None:
            items.append(
                ("WhatsApp number connected", False, "Set WHATSAPP_TOKEN and PHONE_NUMBER_ID on Render.")
            )
        else:
            items.append(("WhatsApp number connected", True, ""))
        if not self.profile.services:
            items.append(
                ("Services and prices filled in", False, "Add services in the business profile.")
            )
        elif any(service.price is None for service in self.profile.services):
            items.append(
                ("Services and prices filled in", False, "Add prices in the business profile so the bot can quote them.")
            )
        else:
            items.append(("Services and prices filled in", True, ""))
        if isinstance(self.notifier, TelegramNotifier):
            items.append(("Owner alerts on", True, ""))
        else:
            items.append(
                ("Owner alerts on", False, "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID on Render so the owner is pinged.")
            )
        return items

    def _newly_closed_with_bookings(self, new_hours):
        """Future bookings on days the new hours just closed: the owner
        has to call those customers, so the count belongs in the save
        notice. Read before the profile swap, against the old hours."""
        today = india_today()
        closed_days = set()
        for weekday in range(7):
            old = self.profile.hours_today(weekday)
            new = new_hours.get(DAY_KEYS[weekday], new_hours.get("daily"))
            if not is_closed(old) and is_closed(new):
                closed_days.add(weekday)
        if not closed_days:
            return 0
        count = 0
        for booking in self.bookings.all_bookings():
            try:
                day = date.fromisoformat(booking["date"])
            except (KeyError, ValueError):
                continue
            if day >= today and day.weekday() in closed_days:
                count += 1
        return count

    def apply_setup(self, fields):
        """Save the setup form: prices, durations and hours. Returns
        the notice for the dashboard. The order is the safety:
        validate everything, write the file, then swap the live
        profile. A failure anywhere before the swap leaves file and
        memory untouched, and the notice says so in plain words."""
        if self.demo_mode:
            return "The demo page cannot be changed. This is the public showcase."
        if self.profile_path is None:
            return "There is no profile file to save to. Ask Aditya to check the setup."
        names = fields.get("service") or []
        prices = fields.get("price") or []
        durations = fields.get("duration") or []
        if not (len(names) == len(prices) == len(durations)):
            return "The page sent a half filled form. Load it again and try once more."
        hours_texts = [(fields.get("hours_" + key) or [""])[0] for key in DAY_KEYS]
        services, error, warnings = validate_setup_services(
            self.profile, list(zip(names, prices, durations))
        )
        if error:
            return error
        hours, error = validate_setup_hours(hours_texts)
        if error:
            return error
        closed_count = self._newly_closed_with_bookings(hours)
        new_profile = BusinessProfile(
            name=self.profile.name,
            category=self.profile.category,
            city=self.profile.city,
            phone=self.profile.phone,
            hours=hours,
            services=services,
            greeting=self.profile.greeting,
            address=self.profile.address,
            offers=self.profile.offers,
        )
        try:
            current_text = self.profile_path.read_text(encoding="utf-8")
            current = json.loads(current_text)
        except (OSError, ValueError):
            return "The save failed. The profile file could not be read. Try again."
        merged = dict(current)
        merged["services"] = [service.to_dict() for service in services]
        merged["hours"] = hours
        try:
            # The previous version stays safe next to the file, so a
            # wrong save can be undone by Aditya.
            _atomic_write(Path(str(self.profile_path) + ".bak"), current_text)
            _atomic_write(self.profile_path, json.dumps(merged, indent=2))
        except OSError:
            return "The save failed. Nothing changed. Try again."
        with self._profile_lock:
            self.profile = BusinessProfile.from_dict(merged)
        notice = "Saved. The bot now quotes these prices."
        if warnings:
            notice += " " + " ".join(warnings)
        if closed_count:
            notice += " {} booking{} {} on a day you just closed. Call those customers.".format(
                closed_count,
                "s" if closed_count != 1 else "",
                "sits" if closed_count == 1 else "sit",
            )
        return notice

    def self_test_line(self):
        """One quiet line inviting the owner to test the bot himself:
        text the shop number from another phone and ask the price of
        the first priced service. Only when a test would actually get
        an answer: sending live, a key set, not demo mode, a shop
        number on file, and at least one published price. Otherwise
        empty, so the owner is never invited to a test that gets
        silence. The number is a wa.me link with the question
        prefilled, so tapping it opens WhatsApp ready to send."""
        if not self.live or self.llm is None or self.demo_mode:
            return ""
        digits = "".join(c for c in self.profile.phone if c.isdigit())
        if not digits:
            return ""
        priced = [s for s in self.profile.services if s.price is not None]
        if not priced:
            return ""
        service = priced[0]
        message = "What is the price of a {}?".format(service.name)
        link = "https://wa.me/{}?text={}".format(digits, quote(message))
        return (
            "<p class='selftest'>Try it yourself. Text "
            "<a href='{}'>+{}</a> on WhatsApp from another phone and ask: "
            "{} You should get Rs {} back in a few seconds, and your "
            "message will show on this page too. If nothing comes back, "
            "check the bot is on above.</p>"
        ).format(
            html.escape(link, quote=True),
            html.escape(digits),
            html.escape(message),
            service.price,
        )

    def saving_line(self):
        """One quiet line under Setup, on real installs only: whether
        bookings survive a restart. Plain words, no database jargon.
        Empty in demo mode, where the page is a showcase."""
        if self.demo_mode:
            return ""
        if getattr(self.leads, "backend", "json") == "postgres":
            text = "Your bookings are saved safely. They stay even when the app restarts."
        else:
            text = (
                "Your bookings are saved on this server for now. "
                "They can be lost when the app restarts. "
                "Ask Aditya about safe saving."
            )
        return "<p class='saving'>{}</p>".format(html.escape(text))

    def enable_sending(self):
        self.sending_disabled = False

    def enable_negotiation(self):
        """The owner lets the bot haggle again, within the floor price."""
        self.negotiation_on = True

    def disable_negotiation(self):
        """The owner stops all haggling: fixed prices only from now on."""
        self.negotiation_on = False

    def block_conflicts(self, day, start_min, end_min):
        """Bookings that clash with a block over [start_min, end_min).
        A block over one of these is refused, never silently applied."""
        return [
            b for b in self.bookings.bookings_on(day)
            if start_min < b["start_min"] + b["duration_min"]
            and b["start_min"] < end_min
        ]

    def reject_booking(self, phone, day, start_min, block_slot=False, inform=False):
        """The owner rejects a booked appointment. The booking is
        removed, so the slot opens up again at once. The time is
        blocked only when asked, so it stays closed for other
        customers. The customer is told only when asked: a short
        message that the booking could not be kept, sent on WhatsApp
        when live and recorded in the outbox when not. Returns a dict
        with the rejected booking and what was done, or None when
        there is no such booking, so a second reject of the same one
        is refused."""
        booking = self.bookings.reject(phone, day, start_min)
        if booking is None:
            return None
        blocked = False
        if block_slot:
            self.blocks.add(
                day,
                booking["start_min"],
                booking["start_min"] + booking["duration_min"],
                "Rejected {} booking".format(booking["service"]),
            )
            blocked = True
        informed = False
        if inform:
            when = format_date(date.fromisoformat(booking["date"]))
            message = (
                "Hi {}, this is {}. We are sorry, but we cannot keep "
                "your {} appointment on {} at {}. Please message us "
                "to pick another time."
            ).format(
                booking.get("name") or "there",
                self.profile.name,
                booking["service"],
                when,
                format_time(booking["start_min"]),
            )
            self._record_reply(
                booking["phone"], "", "booking_rejected", "", message
            )
            informed = True
        return {"booking": booking, "blocked": blocked, "informed": informed}

    def _free_slots(self, day, service, hours):
        """Free start times on a day, with blocked times taken out too.
        The bot reads this list at booking time, never a cached one, so
        a block the owner just made counts at once."""
        return [
            s for s in self.bookings.free_slots(day, service, hours)
            if not self.blocks.is_blocked(day, s, service.duration_min)
        ]

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
        """Queue the reply, and send it if live. The incoming message
        is already recorded by handle_message before the brain runs, so
        this only records the reply."""
        entry = {"to": phone, "body": reply, "intent": intent, "ts": int(time.time())}
        self.outbox.append(entry)
        self._dispatch(entry)
        self._save_outbox()
        return entry

    def handle_message(self, phone, text, sender_name="", now=None, wamid=None):
        # The message is recorded first, before the brain runs, and the
        # wamid claim is the dedup: a redelivery of a message already
        # recorded raises instead of being answered twice. Meta
        # redelivers when it gets no 200, so this is the turn the
        # duplicate is caught on.
        try:
            self.leads.record_message(phone, text, None, sender_name, wamid=wamid)
        except DuplicateMessageError:
            return {"to": phone, "body": None, "duplicate": True}
        # A handoff is total: while one is open the message is recorded for
        # the owner to read, but the bot sends nothing.
        if self.handoffs.open_for(phone) is not None:
            return {"to": phone, "body": None, "paused": True}
        # No key, no brain: the bot cannot reply. The lead is recorded
        # above and nothing is sent; the dashboard says a key is needed.
        if self.llm is None:
            return {"to": phone, "body": None, "no_llm": True}
        reply = self._llm_reply(phone, text, sender_name, now)
        if reply is None:
            # The model failed: hand over so the owner knows and the
            # customer hears back, instead of silence.
            handoff = self.handoffs.open(phone, sender_name, "couldn't reply", text)
            if not self._turn_unanswered and not self.demo_mode:
                self.missed.record_unanswered(
                    phone, sender_name, text, "model failed", handoff["id"]
                )
            confirmation = handoff_confirmation(self.profile, "couldn't reply", now)
            entry = {"to": phone, "body": confirmation, "intent": "handoff", "handoff": handoff["id"], "ts": int(time.time())}
            self.outbox.append(entry)
            self._dispatch(entry)
            self._save_outbox()
            try:
                self.notifier.notify(self.profile.name, handoff)
            except Exception:
                pass
            return entry
        entry = self._record_reply(phone, text, None, sender_name, reply, wamid)
        if self._last_handoff is not None:
            entry["handoff"] = self._last_handoff["id"]
            self._last_handoff = None
        # The turn ended with no booking: every refusal is a person
        # who left with nothing, so each is recorded for the owner to
        # call back. The store collapses two tries at the same slot
        # into one record. A turn that booked is not recorded.
        if self._turn_refusals and not self._turn_booked and not self.demo_mode:
            lead = self.leads.get(phone)
            name = sender_name or (lead or {}).get("name", "") or ""
            for refusal in self._turn_refusals:
                self.missed.record_refused(
                    phone,
                    name,
                    refusal["service"],
                    refusal["date"],
                    refusal["start_min"],
                    refusal["reason"],
                    refusal["alternatives"],
                    text,
                )
        return entry

    def _llm_history(self, phone, limit=10):
        """The recent thread as chat messages, so the model remembers
        what was said: the customer's messages and the business's
        replies, oldest first, last few only."""
        thread = self._conversation_thread(phone)[-limit:]
        return [
            {
                "role": "user" if m["side"] == "in" else "assistant",
                "content": m["text"],
            }
            for m in thread
        ]

    @staticmethod
    def _refusal_record(args, outcome, now):
        """The record for a refused booking tool call, built from what
        the model asked for and what the tool answered."""
        today = (now or india_now()).date()
        return {
            "service": (args.get("service") or "").strip(),
            "date": parse_day(args.get("day") or "", today) or today,
            "start_min": parse_time(args.get("time") or ""),
            "reason": outcome.get("error") or "",
            "alternatives": list(outcome.get("alternatives") or []),
        }

    def _llm_reply(self, phone, text, sender_name, now):
        """One model turn: system prompt, thread, the new message, then
        tool calls until the model answers in words. Returns the reply
        text, or None when the model could not produce one."""
        self._turn_refusals = []
        self._turn_booked = False
        self._turn_unanswered = False
        messages = [{"role": "system", "content": build_system_prompt(self.profile, now, self.negotiation_on)}]
        messages.extend(self._llm_history(phone))
        messages.append({"role": "user", "content": text})
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                result = self.llm.chat(messages, tools=LLM_TOOLS)
            except Exception:
                return None
            content = (result or {}).get("content")
            tool_calls = (result or {}).get("tool_calls") or []
            if tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": openai_tool_calls(tool_calls),
                    }
                )
                for i, call in enumerate(tool_calls):
                    call_id = call.get("id") or f"call-{i}"
                    tool_name = call.get("name") or ""
                    outcome = self._run_tool(
                        tool_name,
                        call.get("arguments") or {},
                        phone,
                        sender_name,
                        now,
                        text,
                    )
                    if tool_name in ("create_booking", "reschedule_booking"):
                        if outcome.get("ok"):
                            self._turn_booked = True
                            service = (outcome.get("booking") or {}).get(
                                "service"
                            ) or ""
                            if service:
                                self.missed.resolve_for_booking(phone, service)
                        elif outcome.get("error") in REFUSAL_ERRORS:
                            self._turn_refusals.append(
                                self._refusal_record(
                                    call.get("arguments") or {}, outcome, now
                                )
                            )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json.dumps(outcome),
                        }
                    )
                continue
            if content and content.strip():
                return content.strip()[:MAX_LLM_CHARS]
            return None
        return None

    def draft_reply(self, phone):
        """Ask the brain to draft a reply for the owner to review and
        send. Returns the draft text, or None when there is no key or
        the model could not draft one. The draft is never sent, and the
        model gets no tools here, so drafting cannot book, cancel, or
        change anything."""
        if self.llm is None:
            return None
        lead = self.leads.get(phone)
        if lead is None or not lead["messages"]:
            return None
        messages = [{"role": "system", "content": build_draft_prompt(self.profile)}]
        messages.extend(self._llm_history(phone))
        try:
            result = self.llm.chat(messages)
        except Exception:
            return None
        content = (result or {}).get("content")
        if not content or not content.strip():
            return None
        return content.strip()[:MAX_LLM_CHARS]

    def _run_tool(self, name, args, phone, sender_name, now, trigger_text):
        """Run one tool the model asked for. Everything real happens
        here, in code: prices come from the catalogue, slots are
        validated, bookings are recorded. Returns a plain dict the
        model reads; a refusal says why and what is free instead."""
        today = (now or india_now()).date()
        now_min = (now or india_now()).hour * 60 + (now or india_now()).minute
        lead = self.leads.get(phone)
        name = name or ""
        args = args or {}

        def _service():
            service = self.profile.service_named(args.get("service") or "")
            if service is None:
                return None, {
                    "ok": False,
                    "error": "no such service",
                    "services": [s.name for s in self.profile.services],
                }
            return service, None

        def _alternatives(day, service, start):
            return [format_time(s) for s in self._nearest_slots(day, service, start)]

        if name == "get_price":
            service, error = _service()
            if error is not None:
                return error
            return {
                "ok": True,
                "service": service.name,
                "price": service.price,
                "duration_min": service.duration_min,
                "note": (
                    "price confirmed at the shop"
                    if service.price is None
                    else ""
                ),
            }
        if name == "list_prices":
            return {
                "ok": True,
                "services": [
                    {
                        "name": s.name,
                        "price": s.price,
                        "duration_min": s.duration_min,
                    }
                    for s in self.profile.services
                ],
                "offers": self.profile.offers or "",
            }
        if name == "check_availability":
            service, error = _service()
            if error is not None:
                return error
            day = parse_day(args.get("day") or "", today) or today
            hours = self.profile.hours_today(day.weekday())
            if is_closed(hours):
                return {"ok": True, "closed": True, "date": day.isoformat()}
            slots = self._free_slots(day, service, hours)
            start = parse_time(args.get("time") or "") if args.get("time") else None
            if start is not None:
                slots = sorted(slots, key=lambda s: abs(s - start))[:4]
            else:
                slots = slots[:8]
            return {
                "ok": True,
                "service": service.name,
                "date": day.isoformat(),
                "slots": [format_time(s) for s in slots],
            }
        if name == "create_booking":
            service, error = _service()
            if error is not None:
                return error
            day = parse_day(args.get("day") or "", today) or today
            start = parse_time(args.get("time") or "")
            if start is None:
                return {"ok": False, "error": "could not understand the time"}
            # A time of day ("subah") with no day means the next such
            # time: if it has already passed today, it is tomorrow's.
            if (
                not args.get("day")
                and has_relative_time(args.get("time") or "")
                and day == today
                and start <= now_min
            ):
                day = today + timedelta(days=1)
            hours = self.profile.hours_today(day.weekday())
            if is_closed(hours):
                return {"ok": False, "error": "closed", "date": day.isoformat()}
            if day == today and start <= now_min:
                future = [
                    s
                    for s in self._free_slots(day, service, hours)
                    if s > now_min
                ]
                return {
                    "ok": False,
                    "error": "time passed",
                    "alternatives": [format_time(s) for s in future[:4]],
                }
            if not validate_slot(day, start, service, hours):
                return {
                    "ok": False,
                    "error": "invalid slot",
                    "hours": hours,
                    "alternatives": _alternatives(day, service, start),
                }
            # A time the owner blocked is not free, same as a time
            # already taken. The error never says blocked, so the bot
            # never repeats that word to a customer.
            if self.blocks.is_blocked(day, start, service.duration_min):
                return {
                    "ok": False,
                    "error": "not free",
                    "alternatives": _alternatives(day, service, start),
                }
            agreed = args.get("price")
            if service.price is None:
                price = None
            elif agreed is None:
                price = service.price
            else:
                try:
                    price = int(agreed)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "bad price"}
                if service.floor_price is not None:
                    if price < service.floor_price:
                        return {
                            "ok": False,
                            "error": "below the lowest price",
                            "lowest": service.floor_price,
                        }
                    if price > service.price:
                        return {
                            "ok": False,
                            "error": "above the list price",
                            "price": service.price,
                        }
                elif price != service.price:
                    return {
                        "ok": False,
                        "error": "fixed price",
                        "price": service.price,
                    }
            # The switch is off: the model may still send an agreed
            # price, but the code never books below the list price.
            if (
                not self.negotiation_on
                and agreed is not None
                and price is not None
                and price != service.price
            ):
                return {
                    "ok": False,
                    "error": "fixed price",
                    "price": service.price,
                }
            customer_name = (
                args.get("customer_name")
                or sender_name
                or (lead or {}).get("name", "")
                or ""
            )
            booking = self.bookings.book(
                phone, customer_name, service, day, start, price=price
            )
            if booking is None:
                mine = [
                    b
                    for b in self.bookings.overlapping(
                        day, start, service.duration_min
                    )
                    if b["phone"] == phone
                ]
                if mine:
                    return {
                        "ok": False,
                        "error": "already yours",
                        "alternatives": _alternatives(day, service, start),
                    }
                return {
                    "ok": False,
                    "error": "already taken",
                    "alternatives": _alternatives(day, service, start),
                }
            try:
                self.notifier.notify_booking(self.profile.name, booking)
            except Exception:
                pass
            return {
                "ok": True,
                "booking": {
                    "service": booking["service"],
                    "date": booking["date"],
                    "start": format_time(booking["start_min"]),
                    "price": booking["price"],
                    "duration_min": booking["duration_min"],
                },
                "customer_name_known": bool(customer_name),
            }
        if name == "cancel_booking":
            cancelled = self.bookings.cancel_latest(phone)
            if cancelled is None:
                return {"ok": False, "error": "no booking to cancel"}
            return {
                "ok": True,
                "booking": {
                    "service": cancelled["service"],
                    "date": cancelled["date"],
                    "start": format_time(cancelled["start_min"]),
                },
            }
        if name == "reschedule_booking":
            today_iso = india_today().isoformat()
            mine = [
                b
                for b in self.bookings.all_bookings()
                if b["phone"] == phone and b["date"] >= today_iso
            ]
            if not mine:
                return {"ok": False, "error": "no booking to move"}
            latest = sorted(mine, key=lambda b: (b["date"], b["start_min"]))[-1]
            service = self.profile.service_named(args.get("service") or "")
            if service is None:
                service = self.profile.service_named(latest["service"])
            if service is None:
                return {"ok": False, "error": "no such service"}
            day = parse_day(args.get("day") or "", today) or today
            start = parse_time(args.get("time") or "")
            if start is None:
                return {"ok": False, "error": "could not understand the time"}
            if (
                not args.get("day")
                and has_relative_time(args.get("time") or "")
                and day == today
                and start <= now_min
            ):
                day = today + timedelta(days=1)
            hours = self.profile.hours_today(day.weekday())
            if is_closed(hours):
                return {"ok": False, "error": "closed", "date": day.isoformat()}
            if day == today and start <= now_min:
                future = [
                    s
                    for s in self._free_slots(day, service, hours)
                    if s > now_min
                ]
                return {
                    "ok": False,
                    "error": "time passed",
                    "alternatives": [format_time(s) for s in future[:4]],
                }
            if not validate_slot(day, start, service, hours):
                return {
                    "ok": False,
                    "error": "invalid slot",
                    "hours": hours,
                    "alternatives": _alternatives(day, service, start),
                }
            if self.blocks.is_blocked(day, start, service.duration_min):
                return {
                    "ok": False,
                    "error": "not free",
                    "alternatives": _alternatives(day, service, start),
                }
            # The old booking is only cancelled once the new slot is
            # valid and free; a failed move never destroys a booking.
            # The store moves it as one step, so a second webhook
            # cannot take the slot in between.
            others = [
                b
                for b in self.bookings.overlapping(day, start, service.duration_min)
                if not (
                    b["phone"] == phone
                    and b["date"] == latest["date"]
                    and b["start_min"] == latest["start_min"]
                )
            ]
            if len(others) >= service.capacity:
                return {
                    "ok": False,
                    "error": "already taken",
                    "alternatives": _alternatives(day, service, start),
                }
            cancelled, booking = self.bookings.reschedule(
                phone,
                latest.get("name") or sender_name,
                service,
                day,
                start,
                price=latest.get("price")
                if service.name == latest["service"]
                else None,
            )
            if booking is None:
                return {"ok": False, "error": "already taken"}
            return {
                "ok": True,
                "cancelled": {
                    "service": cancelled["service"],
                    "date": cancelled["date"],
                    "start": format_time(cancelled["start_min"]),
                },
                "booking": {
                    "service": booking["service"],
                    "date": booking["date"],
                    "start": format_time(booking["start_min"]),
                    "price": booking["price"],
                    "duration_min": booking["duration_min"],
                },
            }
        if name == "hand_over":
            reason = (args.get("reason") or "asked for a person").strip()
            handoff = self.handoffs.open(phone, sender_name, reason, trigger_text)
            # Remember it so the reply entry carries the handoff id,
            # the way the LLM-failure path already does.
            self._last_handoff = handoff
            if reason.lower().startswith("cannot answer") and not self.demo_mode:
                self.missed.record_unanswered(
                    phone, sender_name, trigger_text, "cannot answer", handoff["id"]
                )
                self._turn_unanswered = True
            try:
                self.notifier.notify(self.profile.name, handoff)
            except Exception:
                pass
            return {
                "ok": True,
                "handoff": True,
                "note": "the owner will reply within 30 minutes during working hours",
            }
        if name == "set_customer_name":
            customer_name = (args.get("name") or "").strip()
            if not customer_name:
                return {"ok": False, "error": "no name given"}
            self.leads.set_name(phone, customer_name)
            self.bookings.update_latest_name(phone, customer_name)
            return {"ok": True, "name": customer_name}
        return {"ok": False, "error": f"unknown tool: {name}"}
    def _nearest_slots(self, day, service, start, limit=4):
        """Free slots nearest to a requested time, for offering alternatives."""
        free = self._free_slots(
            day, service, self.profile.hours_today(day.weekday())
        )
        return sorted(free, key=lambda s: abs(s - start))[:limit]

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
        # The message is recorded first, before anything else, and the
        # wamid claim is the dedup, the same as typed messages.
        try:
            self.leads.record_message(
                phone, f"[sent a {label}]", None, sender_name, wamid=wamid
            )
        except DuplicateMessageError:
            return None
        # While a handoff is open the bot stays quiet; the message is
        # still recorded for the owner to read.
        if self.handoffs.open_for(phone) is not None:
            return None
        reason = f"sent a {label}"
        handoff = self.handoffs.open(phone, sender_name, reason, f"sent a {label}")
        reply = (
            "I can only read typed messages right now — please type it "
            "and I will help right away."
        )
        entry = {"to": phone, "body": reply, "intent": "handoff", "handoff": handoff["id"], "ts": int(time.time())}
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
                    try:
                        if msg.get("type") != "text":
                            self.handle_non_text_message(
                                phone, msg.get("type", ""), contacts.get(phone, ""), wamid=wamid
                            )
                            count += 1
                            continue
                        text = (msg.get("text") or {}).get("body", "")
                        self.handle_message(phone, text, contacts.get(phone, ""), wamid=wamid)
                        count += 1
                    except DuplicateMessageError:
                        # A second delivery that slipped past the check
                        # above: already recorded, already answered or
                        # being answered. Skip it, do not answer twice.
                        continue
        return count

    def _conversation_thread(self, phone):
        """The full chat thread for one phone, oldest first: the
        customer's messages from the lead record plus every reply the
        business sent, bot or owner, from the outbox. A reply answers
        one customer message, so it goes right after it; an owner reply
        goes after every message that was there when the owner sent
        it. Replies recorded before timestamps were added carry no ts;
        those fall back to the handoff they belong to (the confirmation
        went out with the trigger, an owner reply when it was resolved),
        and anything else keeps the last timestamp seen."""
        lead = self.leads.get(phone)
        customers = []
        if lead is not None:
            for m in lead["messages"]:
                customers.append({"side": "in", "text": m["text"], "ts": m["ts"]})
        handoffs = {h["id"]: h for h in self.handoffs.all_handoffs()}
        replies = []
        last_ts = 0
        for entry in self.outbox:
            if entry.get("to") != phone:
                continue
            body = entry.get("body") or ""
            if not body:
                continue
            ts = entry.get("ts")
            if not isinstance(ts, (int, float)):
                handoff = handoffs.get(entry.get("handoff") or 0)
                if handoff is not None:
                    if entry.get("intent") == "handoff_reply":
                        ts = handoff.get("resolved_at") or handoff["opened_at"]
                    else:
                        ts = handoff["opened_at"]
                        if lead is not None:
                            for m in lead["messages"]:
                                if m["text"] == handoff["trigger"] and m["ts"] >= ts:
                                    ts = m["ts"]
                                    break
                else:
                    ts = last_ts
            if ts > last_ts:
                last_ts = ts
            replies.append(
                {
                    "side": "out",
                    "text": body,
                    "ts": ts,
                    "owner": entry.get("intent") == "handoff_reply",
                }
            )
        thread = []
        i = 0
        for reply in replies:
            while i < len(customers) and customers[i]["ts"] < reply["ts"]:
                thread.append(customers[i])
                i += 1
            if reply["owner"]:
                while i < len(customers) and customers[i]["ts"] <= reply["ts"]:
                    thread.append(customers[i])
                    i += 1
            elif i < len(customers) and customers[i]["ts"] <= reply["ts"]:
                thread.append(customers[i])
                i += 1
            thread.append(reply)
        thread.extend(customers[i:])
        return thread

    def dashboard_html(self, tab="today", now=None, mock=False):
        """The owner's page, in tabs. Server side, no JavaScript: a tab
        is a link to the same page with ?tab=, and the server renders
        only that tab's section. The header (shop name, the day, the
        bot switch, the negotiation switch) stays on every tab. Today
        is the default and stays a clean overview: the glance, the
        money, anything wrong, and the counts that say which tab to
        tap. The detail lives behind its own tab, never on Today.
        Bookings is the calendar of appointments and where the owner
        blocks a time. Customers is the people waiting and who messaged
        and who asked and did not book. Replies is where the owner
        writes a reply, drafts one on request, and sees the replies
        that failed. Conversation is the full WhatsApp style thread
        for each waiting person. With mock=True the win and moments
        lines show in demo mode too, so a preview shows every section
        with sample data; the page says so, and nothing about it
        reads as a real customer.

        The look follows the Ellipsus standard: paper and ink, charcoal
        and warm off white fields, one gold accent, Fraunces serif
        headlines at weight 400 with Inter for body and UI, pill
        buttons, flat cards, tall quiet spacing. The money stays huge
        up front, because that is what the owner cares about most.
        Motion is CSS only, the page carries no script: a soft fade
        on load, sections fading in as they enter where the browser
        supports it, the greeting typing itself out, near invisible
        hovers, and nothing moves for reduced motion. The labels the
        tests assert on are kept verbatim. No dash of any kind in the
        copy here."""
        from .bookings import format_date, format_time

        tabs = ("today", "bookings", "customers", "replies", "conversation", "setup")
        if tab not in tabs:
            tab = "today"

        now = now or india_now()
        today = now.date()
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
        missed_today = self.missed.open_today(today)
        if missed_today:
            sentence += " {} {} left without booking today.".format(
                missed_today, plural(missed_today, "person", "people")
            )

        # A block today shows in the glance, with the reason, so the
        # owner remembers why that time is gone. Only when a block
        # exists; with none the sentence stays exactly as pinned.
        today_blocks = self.blocks.blocks_on(today)
        if today_blocks:
            span = parse_hours_range(self.profile.hours_today(today.weekday()))
            if span and any(
                b["start_min"] <= span[0] and b["end_min"] >= span[1]
                for b in today_blocks
            ):
                sentence += " Closed today."
            else:
                total = sum(b["end_min"] - b["start_min"] for b in today_blocks)
                if total >= 60:
                    amount = "{} hour{}".format(
                        total // 60, "s" if total // 60 != 1 else ""
                    )
                else:
                    amount = "{} minutes".format(total)
                reason = (today_blocks[0].get("reason") or "").strip()
                if reason:
                    sentence += " {} blocked for {}.".format(amount, reason)
                else:
                    sentence += " {} blocked.".format(amount)

        money_html = ""
        if todays and rupees_today > 0:
            money_html = (
                "<p class='money'>Rs {}</p>"
                "<p class='moneylbl'>booked today</p>"
            ).format(rupees_today)

        def win_line(win):
            """One quiet line for the first booking the bot made today:
            who, what service, what time, how much, and which day when
            the appointment is not today. No dash characters anywhere."""
            booking = win["booking"]
            name = (booking.get("name") or "").strip()
            service = (booking.get("service") or "service").strip().lower()
            article = "an" if service[:1] in "aeiou" else "a"
            day = date.fromisoformat(booking["date"])
            if day == today:
                when = ""
            elif day == today + timedelta(days=1):
                when = " for tomorrow"
            else:
                when = " for {}".format(format_date(day))
            price = booking.get("price")
            rupees = " for Rs {}".format(price) if price else ""
            time = format_time(booking["start_min"])
            if name:
                detail = "{} booked {} {}{} at {}{}.".format(
                    name, article, service, when, time, rupees
                )
            else:
                detail = "{} {}{} at {}{}.".format(
                    article.capitalize(), service, when, time, rupees
                )
            if win["kind"] == "first ever":
                return (
                    "Your first booking ever. {} "
                    "The bot made that happen.".format(detail)
                )
            return "First booking today. {}".format(detail)

        def moments_line(moments, today, closed_today):
            """One quiet line for the bookings the bot made while the
            shop was closed: who, what service, what time, how much,
            and which day when the appointment is not today. No dash
            characters anywhere."""
            verb = "still booked" if closed_today else "booked"
            lead = (
                "Closed today. " if closed_today else "While you were closed, "
            )
            if len(moments) == 1:
                booking = moments[0]["booking"]
                name = (booking.get("name") or "").strip()
                service = (booking.get("service") or "service").strip().lower()
                article = "an" if service[:1] in "aeiou" else "a"
                day = date.fromisoformat(booking["date"])
                if day == today:
                    when = ""
                elif day == today + timedelta(days=1):
                    when = " for tomorrow"
                else:
                    when = " for {}".format(format_date(day))
                price = booking.get("price")
                rupees = " for Rs {}".format(price) if price else ""
                time = format_time(booking["start_min"])
                if name:
                    detail = "{} {} {} {}{} at {}{}".format(
                        name, verb, article, service, when, time, rupees
                    )
                else:
                    who = "Someone" if closed_today else "someone"
                    detail = "{} {} {} {}{} at {}{}".format(
                        who, verb, article, service, when, time, rupees
                    )
                return "{}{}.".format(lead, detail)
            total = sum((m["booking"].get("price") or 0) for m in moments)
            rupees = " for Rs {}".format(total) if total > 0 else ""
            return "{}{} people {}{}.".format(
                lead, len(moments), verb, rupees
            )

        # A greeting that knows what time it is. Small, human, and it
        # makes the page feel like it was opened today, not generated.
        if now.hour < 12:
            greeting = "Good morning."
        elif now.hour < 17:
            greeting = "Good afternoon."
        else:
            greeting = "Good evening."

        state = self.sending_state()
        if state == "live":
            bot_block = (
                "<p><span class='pill on'>Sending: LIVE</span> "
                "Bot is ON. Replies are being sent on WhatsApp.</p>"
                "<form method='post' action='/sending/disable'>"
                "<button type='submit' class='primary'>Stop sending</button></form>"
            )
            bot_class = "bot"
        elif self.demo_mode:
            # The showcase: the bot reads as on, with a clear Demo
            # label and a sample data note, so a visitor sees the
            # product alive and nobody mistakes samples for real
            # customers. Nothing here is sent to a real phone. A mock
            # preview says so on the page, so the extra sections it
            # shows are read as samples too.
            mock_note = (
                "<p class='quiet'>Mock preview. Every section is shown with sample data.</p>"
                if mock
                else ""
            )
            bot_block = (
                "<p><span class='pill on'>Bot is ON</span> "
                "<span class='pill idle'>Demo</span> "
                "Bot is ON. This is a demo with sample customers, so "
                "replies are shown here and nothing is sent to a real phone.</p>"
                "<p class='quiet'>Sample data. These people are not real customers.</p>"
            ) + mock_note
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
            # No WhatsApp number is connected, so the bot cannot send
            # anything. Say so plainly and say what to do next, instead
            # of leaving a dead "Bot is OFF" with nowhere to go.
            bot_block = (
                "<p><span class='pill idle'>Bot is OFF</span> "
                "No WhatsApp number is connected yet, so the bot is "
                "watching and drafting replies but nothing reaches a "
                "customer. Ask Aditya to connect your WhatsApp number "
                "and the bot will start answering for real.</p>"
            )
            bot_class = "bot loud"
        bot_section = "<section class='{}'>{}</section>".format(bot_class, bot_block)

        # The negotiation switch sits next to the bot control, where the
        # owner already looks. A real control: flipping it takes effect
        # at once, and the bot reads it on every message.
        if self.negotiation_on:
            neg_section = (
                "<section class='bot'><p><span class='pill on'>Negotiation: ON</span> "
                "Let the bot negotiate prices. It may come down to the "
                "lowest price you set for a service, never below it.</p>"
                "<form method='post' action='/negotiation/disable'>"
                "<button type='submit' class='primary'>Turn negotiation off</button></form></section>"
            )
        else:
            neg_section = (
                "<section class='bot'><p><span class='pill off'>Negotiation: OFF</span> "
                "Let the bot negotiate prices. It is off, so the bot "
                "quotes fixed prices only and never haggles.</p>"
                "<form method='post' action='/negotiation/enable'>"
                "<button type='submit' class='primary'>Turn negotiation on</button></form></section>"
            )

        # No key, no brain: say so plainly at the top, so the owner
        # never thinks the bot is answering when it cannot reply.
        # Demo mode hides this on purpose: the showcase runs on
        # sample data with the bot reading as on, and a real install
        # without a key still says so.
        key_notice = ""
        warmline = "The bot is answering your customers. You only step in when you want to."
        if self.demo_mode:
            warmline = "Take a look around. These are sample chats, so you can see how the bot talks to customers."
        elif self.llm is None:
            key_notice = (
                "<section class='alerts'><p class='alerttitle'>The bot needs an LLM key</p>"
                "<p class='alertline'>No key is set, so the bot cannot reply to customers. "
                "Set LLM_API_KEY on the server and restart.</p></section>"
            )
            warmline = "The bot cannot reply yet. It needs an LLM key first."

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

        avatar_colors = ["#292A2E", "#4a4a44", "#6b6558", "#8a7f6a"]

        def handoff_card(handoff):
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
            card += "<p class='meta'>{} · waiting {}</p>".format(
                html.escape(handoff["reason"]),
                html.escape(age_text(handoff["opened_at"])),
            )
            if urgent:
                card += "<p class='urgent'>Waiting a long time. Call them soon.</p>"
            card += "<p class='quote'>&ldquo;{}&rdquo;</p>".format(
                html.escape(handoff["trigger"])
            )
            # What they said most recently, if it is not the message
            # that opened the handoff. The owner sees it here without
            # opening the full thread.
            lead = self.leads.get(handoff["phone"])
            if lead is not None and lead["messages"]:
                latest = lead["messages"][-1]["text"]
                if latest and latest != handoff["trigger"]:
                    card += "<p class='latest'><span>Latest</span> &ldquo;{}&rdquo;</p>".format(
                        html.escape(latest)
                    )
            card += "<a class='call' href='tel:+{}'>Call {}</a>".format(
                digits, html.escape(display)
            )
            card += (
                "<form method='post' action='/handoffs/resolve'>"
                "<input type='hidden' name='handoff_id' value='{}'>"
                "<button type='submit' class='resolve'>Resolve</button></form>"
            ).format(handoff["id"])
            card += "</div>"
            return card

        def missed_card(record):
            """One person who left with nothing: name, what they
            wanted, the plain reason, when, what the bot already
            offered, and a call button as the main action."""
            digits = "".join(c for c in record["phone"] if c.isdigit())
            name = record.get("name")
            display = name or record["phone"]
            initial = (name or "?")[:1].upper()
            color = avatar_colors[
                sum(ord(c) for c in record["phone"]) % len(avatar_colors)
            ]
            card = "<div class='card'>"
            card += "<div class='whorow'>"
            card += "<span class='avatar' style='background:{}'>{}</span>".format(
                color, html.escape(initial)
            )
            card += "<div><div class='who'>{}</div>".format(html.escape(display))
            if name:
                card += "<div class='phone'>{}</div>".format(
                    html.escape(record["phone"])
                )
            card += "</div></div>"
            card += "<p class='meta'>{} · {}</p>".format(
                html.escape(plain_reason(record.get("reason") or "")),
                html.escape(age_text(record["created_ts"])),
            )
            if record["kind"] == "refused":
                want = (record.get("service") or "").strip()
                if record.get("date"):
                    want += " " + format_date(
                        date.fromisoformat(record["date"])
                    )
                if record.get("start_min") is not None:
                    want += " at " + format_time(record["start_min"])
                if want.strip():
                    card += "<p class='want'>{}</p>".format(
                        html.escape(want.strip())
                    )
                if record.get("trigger"):
                    card += "<p class='quote'>&ldquo;{}&rdquo;</p>".format(
                        html.escape(record["trigger"])
                    )
            else:
                card += "<p class='quote'>&ldquo;{}&rdquo;</p>".format(
                    html.escape(record.get("question") or "")
                )
            alternatives = record.get("alternatives") or []
            if alternatives:
                card += "<p class='meta'>Bot offered: {}.</p>".format(
                    html.escape(", ".join(alternatives))
                )
            if record["occurrences"] > 1:
                card += "<p class='meta'>Asked {} times.</p>".format(
                    record["occurrences"]
                )
            past = [
                b
                for b in self.bookings.all_bookings()
                if b["phone"] == record["phone"]
            ]
            if len(past) >= 2:
                card += "<p class='meta'>Regular, booked {} times before.</p>".format(
                    len(past)
                )
            card += "<a class='call' href='tel:+{}'>Call {}</a>".format(
                digits, html.escape(display)
            )
            card += (
                "<form method='post' action='/missed/resolve'>"
                "<input type='hidden' name='missed_id' value='{}'>"
                "<select name='how'>"
                "<option value='called'>Called them</option>"
                "<option value='booked'>They booked</option>"
                "<option value='answered'>Answered</option>"
                "<option value='dismissed'>Dismiss</option>"
                "</select>"
                "<button type='submit' class='resolve'>Mark handled</button></form>"
            ).format(record["id"])
            card += "</div>"
            return card

        # Bad experience: people who wanted something, got a no, and
        # left with nothing. Newest first, three cards on the page,
        # the rest counted. An unanswered question whose handoff is
        # still open lives under People waiting, not here as well.
        open_handoff_ids = {h["id"] for h in open_handoffs}
        visible_missed = [
            r
            for r in self.missed.open_newest()
            if not (
                r["kind"] == "unanswered"
                and r.get("handoff_id") in open_handoff_ids
            )
        ]
        missed_section = ""
        if visible_missed:
            shown = visible_missed[:3]
            extra = len(visible_missed) - len(shown)
            more = ""
            if extra:
                more = "<p class='quiet'>And {} more.</p>".format(extra)
            missed_section = (
                "<section class='missed'><p class='missedtitle'>Bad experience</p>"
                "{}"
                "{}"
                "</section>"
            ).format("".join(missed_card(r) for r in shown), more)

        def reply_box(handoff):
            """The owner's reply form, with a draft on request. A draft
            only prefills the box; nothing here sends by itself. With
            no key the draft button is replaced by a plain note saying
            so, and a failed draft says so instead of going quiet."""
            reply_label = "Send reply" if self.live else "Record reply, sending is off"
            draft = self._drafts.get(handoff["id"])
            value = (
                " value='{}'".format(html.escape(draft)) if draft else ""
            )
            box = (
                "<div class='replybox'>"
                "<p class='quiet'>Only if you want to step in. The bot is already talking to them.</p>"
                "<form method='post' action='/handoffs/{}/reply'>"
                "<input type='text' name='text' placeholder='Reply to {}…'{} required>"
                "<button type='submit' class='soft'>{}</button></form>"
            ).format(
                handoff["id"],
                html.escape(handoff.get("name") or "them"),
                value,
                reply_label,
            )
            if self.llm is None:
                box += (
                    "<p class='quiet'>Drafts need an LLM key. The bot cannot "
                    "reply without one. Set LLM_API_KEY on the server and restart.</p>"
                )
            elif handoff["id"] in self._draft_failed:
                box += (
                    "<p class='quiet'>Could not draft a reply right now. "
                    "Just write your own.</p>"
                )
            else:
                box += (
                    "<form method='post' action='/handoffs/{}/draft'>"
                    "<button type='submit' class='ghost'>Draft a reply</button></form>"
                ).format(handoff["id"])
            if not self.live:
                box += (
                    "<p class='quiet'>Sending is off. Your reply will be "
                    "recorded here, not sent on WhatsApp.</p>"
                )
            box += "</div>"
            return box

        def reply_card(handoff):
            name = handoff.get("name")
            display = name or handoff["phone"]
            card = "<div class='card'>"
            card += "<div class='who'>{}</div>".format(html.escape(display))
            if name:
                card += "<div class='phone'>{}</div>".format(
                    html.escape(handoff["phone"])
                )
            card += "<p class='meta'>{} · waiting {}</p>".format(
                html.escape(handoff["reason"]),
                html.escape(age_text(handoff["opened_at"])),
            )
            lead = self.leads.get(handoff["phone"])
            if lead is not None and lead["messages"]:
                latest = lead["messages"][-1]["text"]
                if latest:
                    card += "<p class='latest'><span>Latest</span> &ldquo;{}&rdquo;</p>".format(
                        html.escape(latest)
                    )
            card += reply_box(handoff)
            card += "</div>"
            return card

        def thread_html(phone):
            thread = self._conversation_thread(phone)
            if not thread:
                return ""
            bubbles = []
            for msg in thread:
                when = ""
                if msg["ts"]:
                    when = datetime.fromtimestamp(msg["ts"], tz=IST).strftime(
                        "%I:%M %p"
                    ).lstrip("0").lower()
                time_html = (
                    "<span class='mtime'>{}</span>".format(when) if when else ""
                )
                bubbles.append(
                    "<div class='msg {}'><p>{}</p>{}</div>".format(
                        msg["side"], html.escape(msg["text"]), time_html
                    )
                )
            return "<div class='thread'><p class='threadtitle'>Conversation</p>{}</div>".format(
                "".join(bubbles)
            )

        waiting_body = "".join(handoff_card(h) for h in open_handoffs) or (
            "<p class='allclear'>All clear, nobody is waiting for you.</p>"
        )

        def booking_rows(bookings, reject=False):
            rows = []
            for b in bookings:
                digits = "".join(c for c in b["phone"] if c.isdigit())
                price = (
                    "Rs {}".format(b["price"])
                    if b.get("price") is not None
                    else "price to confirm"
                )
                reject_html = ""
                if reject:
                    reject_html = (
                        " <form method='get' action='/bookings/reject'>"
                        "<input type='hidden' name='phone' value='{}'>"
                        "<input type='hidden' name='date' value='{}'>"
                        "<input type='hidden' name='start_min' value='{}'>"
                        "<button type='submit' class='inline'>Reject</button></form>"
                    ).format(
                        html.escape(b["phone"], quote=True),
                        html.escape(b["date"], quote=True),
                        b["start_min"],
                    )
                rows.append(
                    "<li><strong>{}</strong> · {} for {} · {} · "
                    "<a href='tel:+{}'>{}</a>{}</li>".format(
                        html.escape(format_time(b["start_min"])),
                        html.escape(b["service"]),
                        html.escape(b.get("name") or b["phone"]),
                        price,
                        digits,
                        html.escape(b["phone"]),
                        reject_html,
                    )
                )
            return rows

        book_body = "".join(booking_rows(todays, reject=True)) or "<li>No bookings today.</li>"
        tomorrow_body = "".join(booking_rows(tomorrows, reject=True)) or "<li>No bookings tomorrow.</li>"

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
        # at all. A warm lead list, kept to one line. Derived from the
        # words they used, not a routed intent: a message names a
        # service, or asks about a price or a booking in plain words.
        booked_phones = {b["phone"] for b in self.bookings.all_bookings()}
        asked = []
        for lead in leads:
            if datetime.fromtimestamp(lead["last_seen"], tz=IST).date() != today:
                continue
            asked_today = False
            for m in lead["messages"]:
                if datetime.fromtimestamp(m["ts"], tz=IST).date() != today:
                    continue
                text = (m.get("text") or "").lower()
                if self.profile.service_named(text) is not None:
                    asked_today = True
                    break
                if any(
                    word in text
                    for word in ("price", "rate", "cost", "book", "appointment", "slot", "charge")
                ):
                    asked_today = True
                    break
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

        # Regulars: the repeat customers, most bookings first, with a
        # note when one has gone quiet. Built from the bookings, so
        # there is nothing new to store; with no regulars yet the
        # section stays off the page entirely.
        regulars_html = ""
        regulars = self.bookings.regulars(today)
        if regulars:
            items = []
            for r in regulars:
                usual = r["usual_service"].lower()
                article = "an" if usual[:1] in "aeiou" else "a"
                line = "<strong>{}</strong>, {} booking, usually {} {}. Last visit {}.".format(
                    html.escape(r["name"] or r["phone"]),
                    _ordinal(r["count"]),
                    article,
                    html.escape(usual),
                    html.escape(format_date(r["last_date"])),
                )
                if r["quiet"]:
                    line += " Has not been back in a while."
                items.append("<li>{}</li>".format(line))
            regulars_html = (
                "<h2>Regulars</h2>"
                "<p class='quiet'>The people who keep coming back. "
                "A regular gone quiet is worth a call.</p>"
                "<ul class='list'>{}</ul>".format("".join(items))
            )

        # The full thread for each waiting person, WhatsApp style:
        # the customer on one side, the business on the other, in
        # order, with the time on each message.
        conversation_body = ""
        for handoff in open_handoffs:
            name = handoff.get("name") or handoff["phone"]
            conversation_body += "<h2>{}</h2>{}".format(
                html.escape(name), thread_html(handoff["phone"])
            )
        if not conversation_body:
            conversation_body = (
                "<p class='allclear'>Nobody is waiting. When someone asks "
                "for a person, their full chat shows up here.</p>"
            )

        # The place to write a reply, one card per waiting person,
        # kept apart from the conversation thread.
        replies_body = "".join(reply_card(h) for h in open_handoffs) or (
            "<p class='allclear'>Nobody is waiting. When someone asks "
            "for a person, you can reply to them here.</p>"
        )

        css = """
        :root{--ink:#292A2E;--paper:#F4F4F2;--sage:#E5E5E1;--gold:#E8C96A;
        --goldpale:#F5EBCB;--grey:#6b6b66;--serif:'Fraunces',Georgia,'Times New Roman',serif;
        --sans:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
        body{margin:0;font-family:var(--sans);font-size:18px;line-height:1.6;color:var(--ink);background:var(--paper)}
        .wrap{max-width:960px;margin:0 auto;padding:0 0 80px}
        header{padding:56px 24px 24px;animation:fadeUp .6s ease both}
        h1{font-family:var(--serif);font-size:56px;line-height:1.05;margin:0;font-weight:400;letter-spacing:-.01em;color:var(--ink)}
        .dateline{color:var(--grey);font-size:15px;margin:12px 0 0}
        .greeting{font-family:var(--serif);font-style:italic;font-size:28px;font-weight:400;margin:20px 0 0;color:var(--ink)}
        .greeting .type{display:inline-block;overflow:hidden;white-space:nowrap;vertical-align:bottom;max-width:0;animation:typeIn 1.1s steps(16) .35s forwards}
        .greeting .type::after{content:"";display:inline-block;width:2px;height:1em;background:var(--gold);margin-left:3px;vertical-align:-.1em;animation:caret 2.4s steps(1) .35s forwards}
        .warmline{font-size:18px;color:#4a4a44;margin:12px 0 0;line-height:1.6;max-width:700px}
        .tabs{display:flex;flex-wrap:wrap;gap:10px;padding:28px 24px 16px;border-bottom:1px solid var(--sage);animation:fadeUp .6s ease .08s both}
        .tabs a{padding:11px 20px;border-radius:999px;border:1px solid var(--ink);background:#fff;color:var(--ink);text-decoration:none;font-weight:500;font-size:15px;transition:background .18s ease}
        .tabs a.active{background:var(--ink);color:var(--paper)}
        .tabs a:hover{background:var(--sage)}
        .tabs a.active:hover{background:var(--ink)}
        .tabs a:active,.call:active,button:active{transform:translateY(1px)}
        .hero{margin:24px;padding:56px 32px;background:var(--ink);border-radius:20px;color:var(--paper);animation:fadeUp .6s ease .14s both}
        .hero .today{font-family:var(--serif);font-size:24px;line-height:1.4;color:var(--paper);margin:0 0 20px}
        .hero .money{font-family:var(--serif);font-size:88px;line-height:1;font-weight:400;letter-spacing:-.02em;color:var(--gold);margin:0}
        .hero .moneylbl{color:#C9C9C2;font-size:15px;margin:14px 0 0}
        .hero .win{color:var(--gold);font-size:17px;line-height:1.5;margin:20px 0 0}
        .nums{display:flex;gap:32px;margin:0 24px 32px}
        .num .big{display:block;font-family:var(--serif);font-size:40px;font-weight:400;color:var(--ink);line-height:1.1}
        .num .lbl{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--grey)}
        .bot{background:#fff;border:1px solid var(--sage);border-radius:20px;padding:28px 32px;margin:0 24px 32px;color:var(--ink);animation:fadeUp .6s ease .2s both}
        .bot p{margin:0 0 10px;color:var(--ink);max-width:700px}
        .bot p:last-child{margin-bottom:0}
        .bot.loud p{font-size:20px;font-weight:500}
        .pill{display:inline-block;padding:6px 14px;border-radius:999px;font-weight:500;font-size:14px}
        .pill.on{background:var(--gold);color:var(--ink)}
        .pill.off{background:var(--sage);color:var(--ink)}
        .pill.idle{background:transparent;border:1px solid #C9C9C2;color:var(--grey)}
        .alerts{background:var(--ink);border-radius:20px;padding:28px 32px;margin:0 24px 32px}
        .missed{background:#F6E7DE;border-radius:20px;padding:28px 32px;margin:0 24px 32px}
        .missedtitle{font-family:var(--serif);font-size:28px;font-weight:400;color:var(--ink);margin:0 0 8px}
        .missed .card{margin:16px 0}
        .missed .quiet{color:var(--grey);font-size:14px;margin:12px 0 0}
        .want{font-size:18px;font-weight:500;margin:8px 0}
        .alerttitle{font-family:var(--serif);font-size:28px;font-weight:400;color:var(--paper);margin:0 0 8px}
        .alertline{color:#E5E5E1;font-weight:500;font-size:17px;margin:10px 0;max-width:700px}
        .alertline a{color:var(--gold);font-weight:500}
        h2{font-family:var(--serif);font-size:34px;margin:56px 24px 16px;color:var(--ink);font-weight:400;letter-spacing:-.01em;line-height:1.1}
        h2::after{content:"";display:block;width:56px;height:3px;background:var(--gold);border-radius:3px;margin-top:10px;transform:rotate(-1deg)}
        .tag{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--grey);font-weight:500}
        .card{background:#fff;border:1px solid var(--sage);border-radius:18px;padding:28px;margin:16px 24px;color:var(--ink);transition:border-color .18s ease}
        .card:hover{border-color:#C9C9C2}
        .card.urgent{border-color:var(--gold)}
        .whorow{display:flex;align-items:center;gap:14px}
        .avatar{width:54px;height:54px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:26px;font-weight:500;color:#fff;flex-shrink:0}
        .who{font-family:var(--serif);font-size:24px;font-weight:400;color:var(--ink);line-height:1.2}
        .phone{color:var(--grey);font-size:14px;margin-top:2px}
        .meta{color:var(--grey);font-size:14px;margin:8px 0}
        .quote{font-family:var(--serif);font-style:italic;font-size:20px;line-height:1.5;color:var(--ink);margin:12px 0;max-width:700px}
        .latest{font-size:16px;color:#4a4a44;margin:8px 0;max-width:700px}
        .latest span{font-weight:500;text-transform:uppercase;font-size:12px;letter-spacing:.08em;color:var(--ink)}
        .urgent{color:var(--ink);font-weight:500;margin:8px 0;text-decoration:underline wavy var(--gold) 2px;text-underline-offset:4px}
        .call{display:block;text-align:center;background:var(--ink);color:var(--paper);padding:14px 20px;border-radius:999px;font-weight:500;font-size:17px;text-decoration:none;margin:18px 0 0;transition:background .18s ease}
        .call:hover{background:#3a3a3e}
        .thread{background:var(--sage);border-radius:18px;padding:16px;margin:16px 24px;display:flex;flex-direction:column;gap:8px;max-width:760px}
        .threadtitle{font-size:12px;text-transform:uppercase;letter-spacing:.08em;font-weight:500;color:var(--grey);margin:0 0 6px}
        .msg{max-width:82%;border-radius:16px;padding:12px 14px}
        .msg p{margin:0;font-size:16px;line-height:1.5;overflow-wrap:break-word;color:var(--ink)}
        .msg.in{align-self:flex-start;background:#fff;border-bottom-left-radius:4px}
        .msg.out{align-self:flex-end;background:var(--goldpale);border-bottom-right-radius:4px}
        .mtime{display:block;text-align:right;font-size:12px;color:var(--grey);margin-top:4px}
        .allclear{font-family:var(--serif);font-size:24px;font-weight:400;color:var(--ink);margin:0 24px}
        .quiet{color:var(--grey);font-size:14px}
        .insight{font-size:17px;color:var(--ink);margin:16px 24px;max-width:700px}
        .selftest{font-size:17px;color:var(--ink);margin:16px 24px;max-width:700px}
        .list{list-style:none;padding:0;margin:0 24px}
        .list li{background:#fff;border:1px solid var(--sage);border-radius:14px;padding:16px 18px;margin:10px 0;color:var(--ink)}
        .list li.failed{background:var(--goldpale)}
        .err{color:var(--ink);font-weight:500}
        .replybox{background:var(--paper);border-radius:14px;padding:16px;margin-top:16px}
        .replybox .quiet{margin:0 0 4px;color:var(--grey)}
        button{min-height:44px;padding:12px 24px;font-size:16px;font-weight:500;border-radius:999px;border:0;background:var(--ink);color:var(--paper);cursor:pointer;font-family:inherit;transition:background .18s ease,border-color .18s ease}
        button:hover{background:#3a3a3e}
        button.primary{width:100%;margin-top:12px;background:var(--gold);color:var(--ink)}
        button.primary:hover{background:#DDBB55}
        button.soft{width:100%;min-height:44px;background:var(--sage);color:var(--ink);font-size:15px;margin-top:8px}
        button.soft:hover{background:#DCDCD4}
        button.ghost{width:100%;min-height:44px;background:transparent;color:var(--ink);border:1px solid var(--ink);font-size:15px;margin-top:8px}
        button.ghost:hover{background:var(--paper)}
        button.resolve{width:100%;min-height:44px;background:var(--paper);color:var(--ink);font-size:15px;margin-top:14px;border:1px solid var(--sage)}
        button.resolve:hover{border-color:#C9C9C2}
        input[type=text],input[type=date],input[type=time],select{width:100%;box-sizing:border-box;min-height:46px;padding:11px 14px;font-size:16px;border:1px solid #D8D8D2;border-radius:14px;margin:8px 0;background:#fff;color:var(--ink)}
        label{display:block;font-size:15px;font-weight:500;margin:14px 0 0;color:var(--ink)}
        button.inline{width:auto;min-height:38px;padding:8px 18px;font-size:14px;margin-top:10px}
        a{color:var(--ink);text-decoration:underline;text-decoration-color:var(--gold);text-underline-offset:3px}
        a:hover{text-decoration-thickness:2px}
        .hero a,.alerts a{color:var(--gold)}
        .card a,.list a,.bot a{color:var(--ink)}
        .foot{margin:40px 24px 0;color:var(--grey);font-size:14px}
        .foot a{color:var(--ink)}
        @keyframes fadeUp{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
        @keyframes typeIn{to{max-width:24ch}}
        @keyframes caret{0%{opacity:1}40%{opacity:0}55%{opacity:1}90%{opacity:0}100%{opacity:0}}
        @supports (animation-timeline:view()){.card,.list li,.thread,h2,.nums,.alerts,.insight,.selftest,.allclear{animation:fadeUp .6s ease both;animation-timeline:view();animation-range:entry 0% entry 50%}}
        @media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}.greeting .type{max-width:none!important}.greeting .type::after{display:none}}
        @media (max-width:480px){h1{font-size:40px}header{padding:40px 20px 16px}.tabs{padding:20px 20px 12px;gap:8px}.tabs a{padding:9px 14px;font-size:14px}.hero{padding:40px 24px;margin:16px}.hero .money{font-size:64px}.hero .today{font-size:20px}.num .big{font-size:32px}.nums{gap:20px}.card{padding:22px}.bot{padding:22px 24px}h2{font-size:28px}}
        """

        # With no priced bookings this week the money part would read
        # "Rs 0", a number nobody set. Leave it off instead.
        week_line = "This week: {} bookings.".format(len(week_bookings))
        if week_rupees > 0:
            week_line = "This week: {} bookings, Rs {}.".format(
                len(week_bookings), week_rupees
            )

        tab_labels = [
            ("today", "Today"),
            ("bookings", "Bookings"),
            ("customers", "Customers"),
            ("replies", "Replies"),
            ("conversation", "Conversation"),
            ("setup", "Setup"),
        ]
        mock_qs = "&mock=1" if mock else ""
        nav = "<nav class='tabs'>" + "".join(
            "<a href='/?tab={}{}'{}>{}</a>".format(
                name, mock_qs, " class='active' aria-current='page'" if name == tab else "", label
            )
            for name, label in tab_labels
        ) + "</nav>"

        if tab == "today":
            # The win lives in the hero, under the money: one line about
            # the first booking the bot made today, or the shop's first
            # ever. The moments line sits next to it: the bookings made
            # while the shop was closed, in the most recent closed
            # stretch. Never in demo mode or dry run: a rehearsal is not
            # a win, and sample bookings are not real customers. Still
            # shows when sending is paused: the booking was real when
            # it was made. A mock preview lifts the demo gate so the
            # owner can see both lines with sample data.
            win_html = ""
            moments_html = ""
            if (not self.demo_mode or mock) and not self.dry_run:
                win = self.bookings.first_win(today)
                if win:
                    win_html = "<p class='win'>{}</p>".format(
                        html.escape(win_line(win))
                    )
                moments = self.bookings.moments(
                    self.profile.hours_today, now
                )
                if moments:
                    # When the first booking made today was itself made
                    # after hours, the moments line says everything the
                    # win line would say, plus the closed context.
                    if win and any(
                        win["booking"] is m["booking"] for m in moments
                    ):
                        win_html = ""
                    closed_today = is_closed(
                        self.profile.hours_today(today.weekday())
                    )
                    moments_html = "<p class='win'>{}</p>".format(
                        html.escape(moments_line(moments, today, closed_today))
                    )
            hero = (
                "<section class='hero'><p class='today'>{}</p>{}{}{}</section>"
            ).format(html.escape(sentence), money_html, win_html, moments_html)
            nums = (
                "<div class='nums'>"
                "<div class='num'><span class='big'>{}</span><span class='lbl'>waiting for you</span></div>"
                "<div class='num'><span class='big'>{}</span><span class='lbl'>bookings today</span></div>"
                "<div class='num'><span class='big'>{}</span><span class='lbl'>people messaged today</span></div>"
                "</div>"
            ).format(waiting, len(todays), messaged_today)
            # Today stays a clean overview. The counts say which tab to
            # tap; the booking and customer detail lives behind its own
            # tab and never on this page. The setup checklist is for a
            # real install only: in demo mode the page is a showcase,
            # so it stays hidden there.
            setup_html = ""
            if not self.demo_mode:
                setup_items = self.setup_checklist()
                lines = []
                for label, ok, hint in setup_items:
                    if ok:
                        lines.append("<li>{}: done.</li>".format(html.escape(label)))
                    else:
                        lines.append(
                            "<li>{}: not done. {}</li>".format(
                                html.escape(label), html.escape(hint)
                            )
                        )
                heading = (
                    "Setup complete"
                    if all(ok for _, ok, _ in setup_items)
                    else "Setup"
                )
                setup_html = (
                    "<section class='setup'><h2>{}</h2>"
                    "<ul class='list'>{}</ul>"
                    "<p><a href='/?tab=setup'>Open Setup</a> to fill in prices, times and hours.</p></section>"
                ).format(heading, "".join(lines))
            body = hero + nums + alerts_section + missed_section + setup_html + self.saving_line() + self.self_test_line()
        elif tab == "bookings":
            # A notice from the last block action, shown once. A
            # refusal names the booking in the way, so the owner knows
            # to cancel or move it first.
            notice_html = ""
            if self._notice:
                notice_html = (
                    "<section class='alerts'><p class='alertline'>{}</p></section>"
                ).format(html.escape(self._notice))
                self._notice = None
            block_items = []
            for block in self.blocks.all_blocks():
                if date.fromisoformat(block["date"]) < today:
                    continue
                reason = (block.get("reason") or "").strip()
                line = "{} · {} to {}".format(
                    format_date(date.fromisoformat(block["date"])),
                    format_time(block["start_min"]),
                    format_time(block["end_min"]),
                )
                if reason:
                    line += " · {}".format(html.escape(reason))
                block_items.append(
                    "<li>{} "
                    "<form method='post' action='/blocks/remove'>"
                    "<input type='hidden' name='block_id' value='{}'>"
                    "<button type='submit' class='inline'>Open this time again</button></form></li>".format(
                        line, block["id"]
                    )
                )
            blocks_body = "".join(block_items) or "<li>No blocked times.</li>"
            body = (
                "<section class='hero'>{}</section>".format(money_html)
                if money_html
                else ""
            ) + notice_html + (
                "<h2>Bookings today ({})</h2>"
                "<ul class='list'>{}</ul>"
                "<h2>Tomorrow ({})</h2>"
                "<ul class='list'>{}</ul>"
                "<p class='insight'>{}</p>"
                "<h2>Block a time</h2>"
                "<div class='card'><p class='quiet'>When you are out, the bot "
                "says that time is not available and offers the nearest "
                "free times instead.</p>"
                "<form method='post' action='/blocks/add'>"
                "<label>Day</label>"
                "<input type='date' name='day' value='{}' required>"
                "<label>From</label>"
                "<input type='time' name='start' required>"
                "<label>To</label>"
                "<input type='time' name='end' required>"
                "<label>Reason</label>"
                "<input type='text' name='reason' placeholder='Lunch' maxlength='80'>"
                "<button type='submit' class='primary'>Block this time</button></form></div>"
                "<h2>Blocked times</h2>"
                "<ul class='list'>{}</ul>"
            ).format(
                html.escape(format_date(today)),
                book_body,
                html.escape(format_date(tomorrow)),
                tomorrow_body,
                week_line,
                today.isoformat(),
                blocks_body,
            )
        elif tab == "customers":
            # The people waiting live here now, not on Today: Today is
            # a clean overview, and this is where people are.
            body = (
                "<h2>People waiting for you <span class='tag'>Handoffs</span></h2>"
                + waiting_body
                + "<p class='quiet' style='margin:14px 20px'>{} handled today.</p>".format(
                    len(resolved_today)
                )
                + most_html
                + nobook_html
                + "<h2>People who messaged</h2>"
                "<ul class='list'>{}</ul>".format(lead_body)
                + regulars_html
            )
        elif tab == "replies":
            body = (
                "<h2>Reply to a customer</h2>"
                "{}"
                "<h2>Replies the bot sent</h2>"
                "<ul class='list'>{}</ul>"
            ).format(replies_body, out_body)
        elif tab == "setup":
            # A notice from the last save, shown once.
            notice_html = ""
            if self._notice:
                notice_html = (
                    "<section class='alerts'><p class='alertline'>{}</p></section>"
                ).format(html.escape(self._notice))
                self._notice = None
            if self.demo_mode:
                body = notice_html + (
                    "<h2>Setup</h2>"
                    "<p class='quiet'>This is the demo shop, so its prices "
                    "and hours cannot be changed here. On a real install "
                    "this page is where the owner fills in prices, times "
                    "and hours.</p>"
                )
            else:
                unpriced = [
                    service for service in self.profile.services
                    if service.price is None
                ]
                # Services still missing a price come first, so the
                # unfinished work is what the owner sees first.
                ordered = sorted(
                    self.profile.services,
                    key=lambda s: (s.price is not None, s.name),
                )
                rows = []
                for service in ordered:
                    price = "" if service.price is None else str(service.price)
                    rows.append(
                        "<li><strong>{}</strong> "
                        "<input type='hidden' name='service' value='{}'>"
                        "<label>Price Rs <input type='text' name='price' "
                        "inputmode='numeric' value='{}'></label> "
                        "<label>Minutes <input type='text' name='duration' "
                        "inputmode='numeric' value='{}'></label></li>".format(
                            html.escape(service.name),
                            html.escape(service.name, quote=True),
                            html.escape(price, quote=True),
                            service.duration_min,
                        )
                    )
                hour_rows = []
                for key, label, value in zip(
                    DAY_KEYS, DAY_LABELS, self.profile.setup_hours_rows()
                ):
                    hour_rows.append(
                        "<li><label>{} "
                        "<input type='text' name='hours_{}' value='{}'></label></li>".format(
                            label,
                            key,
                            html.escape(value, quote=True),
                        )
                    )
                if unpriced:
                    count_line = (
                        "<p class='quiet'>{} of {} services still need a price. "
                        "A blank price means the shop confirms it.</p>"
                    ).format(len(unpriced), len(self.profile.services))
                else:
                    count_line = (
                        "<p class='quiet'>Every service has a price. "
                        "A blank price means the shop confirms it.</p>"
                    )
                body = notice_html + (
                    "<h2>Setup</h2>"
                    "{}"
                    "<form method='post' action='/setup/save'>"
                    "<h2>Prices and times</h2>"
                    "<ul class='list'>{}</ul>"
                    "<h2>Hours</h2>"
                    "<p class='quiet'>One line per day, like 10:00 AM - 8:00 PM, or closed.</p>"
                    "<ul class='list'>{}</ul>"
                    "<p class='quiet'>Prices saved here stay until the next deploy, "
                    "then the file goes back to what is in the repo. "
                    "Export your data to keep a copy.</p>"
                    "<button type='submit' class='primary'>Save prices and hours</button></form>"
                ).format(count_line, "".join(rows), "".join(hour_rows))
        else:
            body = conversation_body

        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<link rel='preconnect' href='https://fonts.googleapis.com'>"
            "<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
            "<link href='https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;1,9..144,400&family=Inter:wght@400;500&display=swap' rel='stylesheet'>"
            "<title>{} | WhatsApp Studio</title><style>{}</style></head><body>"
            "<div class='wrap'>"
            "<header><h1>{}</h1>"
            "<p class='dateline'>{}</p>"
            "<p class='greeting'><span class='type'>{}</span></p>"
            "<p class='warmline'>{}</p></header>"
            "{}"
            "{}"
            "{}"
            "{}"
            "{}"
            "<p class='foot'><a href='/export'>Export my data</a>. Download "
            "all leads, bookings and handoffs as one file.</p>"
            "<p class='foot'>New here? "
            "<a href='/onboard'>Set up your shop</a> and your WhatsApp starts answering customers.</p>"
            "</div></body></html>"
        ).format(
            html.escape(self.profile.name),
            css,
            html.escape(self.profile.name),
            html.escape(format_date(today)),
            html.escape(greeting),
            html.escape(warmline),
            nav,
            bot_section,
            neg_section,
            key_notice,
            body,
        )

ONBOARD_DURATIONS = (15, 30, 45, 60, 90, 120)
ONBOARD_MAX_BODY = 65536
ONBOARD_DAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
ONBOARD_CATEGORIES = (
    ("salon", "Salon"),
    ("clinic", "Clinic"),
    ("gym", "Gym"),
    ("other", "Other"),
)
# The fields the confirmation page carries back as hidden inputs, so
# the download and the edit both revalidate exactly what she typed.
ONBOARD_FIELD_KEYS = (
    "name", "category", "city", "address", "owner_name", "phone",
    "phone_confirm", "open", "close", "closed", "service", "price",
    "duration", "varies", "lowest",
)

_ONBOARD_CSS = """
:root{--ink:#292A2E;--paper:#F4F4F2;--sage:#E5E5E1;--gold:#E8C96A;
--goldpale:#F5EBCB;--grey:#6b6b66;--serif:'Fraunces',Georgia,'Times New Roman',serif;
--sans:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
body{margin:0;font-family:var(--sans);font-size:18px;line-height:1.6;color:var(--ink);background:var(--paper)}
.wrap{max-width:960px;margin:0 auto;padding:0 0 80px}
header{padding:56px 24px 24px}
h1{font-family:var(--serif);font-size:56px;line-height:1.05;margin:0;font-weight:400;letter-spacing:-.01em}
h2{font-family:var(--serif);font-size:34px;margin:56px 24px 16px;font-weight:400;letter-spacing:-.01em;line-height:1.1}
h2::after{content:"";display:block;width:56px;height:3px;background:var(--gold);border-radius:3px;margin-top:10px;transform:rotate(-1deg)}
.intro{font-size:18px;margin:12px 24px 0;max-width:700px}
.quiet{color:var(--grey);font-size:14px}
.thread{background:var(--sage);border-radius:18px;padding:16px;margin:16px 24px;display:flex;flex-direction:column;gap:8px;max-width:760px}
.threadtitle{font-size:12px;text-transform:uppercase;letter-spacing:.08em;font-weight:500;color:var(--grey);margin:0 0 6px}
.msg{max-width:82%;border-radius:16px;padding:12px 14px}
.msg p{margin:0;font-size:16px;line-height:1.5;color:var(--ink)}
.msg.in{align-self:flex-start;background:#fff;border-bottom-left-radius:4px}
.msg.out{align-self:flex-end;background:var(--goldpale);border-bottom-right-radius:4px}
.list{list-style:none;padding:0;margin:0 24px}
.list li{background:#fff;border:1px solid var(--sage);border-radius:14px;padding:16px 18px;margin:10px 0}
.card{background:#fff;border:1px solid var(--sage);border-radius:18px;padding:28px;margin:16px 24px}
.card p{margin:0 0 10px}
.card p:last-child{margin-bottom:0}
.err{color:var(--ink);font-weight:500}
.field{margin:0 24px}
label{display:block;font-size:15px;font-weight:500;margin:14px 0 0}
label.inline{display:inline-block;margin:8px 16px 0 0;font-weight:400}
[hidden]{display:none!important}
input[type=text],input[type=time],input[type=tel],select{width:100%;box-sizing:border-box;min-height:46px;padding:11px 14px;font-size:16px;border:1px solid #D8D8D2;border-radius:14px;margin:8px 0;background:#fff;color:var(--ink)}
input[type=radio],input[type=checkbox]{min-height:0;margin:0 6px 0 0}
button{min-height:44px;padding:12px 24px;font-size:16px;font-weight:500;border-radius:999px;border:0;background:var(--ink);color:var(--paper);cursor:pointer;font-family:inherit}
button.primary{width:100%;margin-top:12px;background:var(--gold);color:var(--ink)}
button.ghost{width:100%;margin-top:8px;background:transparent;color:var(--ink);border:1px solid var(--ink)}
a{color:var(--ink);text-decoration:underline;text-decoration-color:var(--gold);text-underline-offset:3px}
.foot{margin:40px 24px 0;color:var(--grey);font-size:14px}
@media (max-width:480px){h1{font-size:40px}header{padding:40px 20px 16px}h2{font-size:28px}}
"""


def _onboard_page(title, body):
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<link rel='preconnect' href='https://fonts.googleapis.com'>"
        "<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
        "<link href='https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;1,9..144,400&family=Inter:wght@400;500&display=swap' rel='stylesheet'>"
        "<title>{} | WhatsApp Studio</title><style>{}</style></head><body>"
        "<div class='wrap'>{}</div></body></html>"
    ).format(html.escape(title), _ONBOARD_CSS, body)


def _onboard_value(fields, key, default=""):
    return (fields.get(key) or [default])[0]


def _onboard_error(errors, key):
    if key in errors:
        return "<p class='err'>{}</p>".format(html.escape(errors[key]))
    return ""


def _onboard_hidden_fields(fields):
    parts = []
    for key in ONBOARD_FIELD_KEYS:
        for value in fields.get(key) or []:
            parts.append(
                "<input type='hidden' name='{}' value='{}'>".format(
                    html.escape(key, quote=True),
                    html.escape(value, quote=True),
                )
            )
    return "".join(parts)


def _onboard_slug(name):
    slug = []
    for ch in (name or "").lower():
        if ch.isalnum():
            slug.append(ch)
        elif slug and slug[-1] != "-":
            slug.append("-")
    return "".join(slug).strip("-")


def _onboard_hours_line(hours):
    """Plain words for the hours a profile holds: one daily value, or
    the closed days named next to the range."""
    ranges = {value for value in hours.values() if value != "closed"}
    span = next(iter(ranges)) if ranges else None
    if span is None:
        return "Closed every day"
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


def _onboard_place_word(category):
    return {"salon": "salon", "clinic": "clinic", "gym": "gym"}.get(
        category, "shop"
    )


def _onboard_sample_reply(profile):
    """The question a customer asks about the first service, and the
    answer the bot gives from the shop's own details. Priced, varying
    and unpriced services each get their own honest wording."""
    service = profile["services"][0]
    shop = profile["name"]
    place = _onboard_place_word(profile["category"])
    question = "{} ka price kya hai?".format(service["name"])
    if service["price"] is not None:
        answer = "{} at {} is Rs {}.".format(service["name"], shop, service["price"])
    elif service.get("price_varies"):
        answer = "{} at {}: the price depends on the service, so the {} confirms it.".format(
            service["name"], shop, place
        )
    else:
        answer = "{} at {}: the {} confirms the price.".format(
            service["name"], shop, place
        )
    return question, answer


def _onboard_form_html(fields, errors):
    """The onboarding form. Proof first, then the fields: only what
    she knows from memory at her counter. Every value she typed is
    kept when an error sends her back, and each error sits next to
    the box it belongs to."""
    category = _onboard_value(fields, "category", "salon").lower()
    radios = "".join(
        "<label class='inline'><input type='radio' name='category' value='{}'{}> {}</label>".format(
            value, " checked" if category == value else "", label
        )
        for value, label in ONBOARD_CATEGORIES
    )
    names = list(fields.get("service") or [])
    prices = list(fields.get("price") or [])
    durations = list(fields.get("duration") or [])
    varies = set(fields.get("varies") or [])
    lowest = list(fields.get("lowest") or [])
    rows = []
    # Two rows to start; on an error round trip keep every row she
    # sent, so a service she added is not lost with her typing.
    row_count = max(
        ONBOARDING_DEFAULT_SERVICE_ROWS,
        min(len(names), ONBOARDING_SERVICE_ROWS),
    )
    for index in range(row_count):
        name = names[index] if index < len(names) else ""
        price = prices[index] if index < len(prices) else ""
        lowest_value = lowest[index] if index < len(lowest) else ""
        try:
            duration = int(durations[index]) if index < len(durations) else 30
        except ValueError:
            duration = 30
        if duration not in ONBOARD_DURATIONS:
            duration = 30
        options = "".join(
            "<option value='{}'{}>{} minutes</option>".format(
                minutes, " selected" if minutes == duration else "", minutes
            )
            for minutes in ONBOARD_DURATIONS
        )
        rows.append(
            "<li><label>Service <input type='text' name='service' value='{}'></label> "
            "<label>Price Rs <input type='text' name='price' inputmode='numeric' value='{}'></label> "
            "<label>Minutes <select name='duration'>{}</select></label> "
            "<label class='inline'><input type='checkbox' name='varies' value='{}'{}> Price varies</label> "
            "<label class='inline lowest'{}>Lowest Rs <input type='text' name='lowest' inputmode='numeric' value='{}'></label>{}</li>".format(
                html.escape(name, quote=True),
                html.escape(price, quote=True),
                options,
                index,
                " checked" if str(index) in varies else "",
                " hidden" if str(index) not in varies else "",
                html.escape(lowest_value, quote=True),
                _onboard_error(errors, "service_{}".format(index)),
            )
        )
    # The button clones the last row and clears it, so the page needs
    # no row template. The lowest box only shows while its row's price
    # varies box is ticked. The script is a plain string, never passed
    # through the format call below, so its braces are safe.
    add_service = (
        "<button type='button' class='ghost' id='add-service'>Add service</button>"
        "<script>(function () {"
        "var list = document.querySelector('ul.list');"
        "var button = document.getElementById('add-service');"
        "if (!list || !button) { return; }"
        "function atCap() { return list.querySelectorAll('li').length >= "
        + str(ONBOARDING_SERVICE_ROWS)
        + "; }"
        "if (atCap()) { button.disabled = true; }"
        "button.addEventListener('click', function () {"
        "if (atCap()) { button.disabled = true; return; }"
        "var rows = list.querySelectorAll('li');"
        "var row = rows[rows.length - 1].cloneNode(true);"
        "var texts = row.querySelectorAll('input[type=text]');"
        "for (var i = 0; i < texts.length; i++) { texts[i].value = ''; }"
        "var varies = row.querySelector('input[type=checkbox]');"
        "varies.checked = false;"
        "varies.value = rows.length;"
        "var low = row.querySelector('.lowest');"
        "if (low) { low.hidden = true; }"
        "row.querySelector('select').value = '30';"
        "var errs = row.querySelectorAll('.err');"
        "for (var j = 0; j < errs.length; j++) { errs[j].remove(); }"
        "list.appendChild(row);"
        "if (atCap()) { button.disabled = true; }"
        "});"
        "function syncLowest(row) {"
        "var box = row.querySelector('input[type=checkbox]');"
        "var low = row.querySelector('.lowest');"
        "if (box && low) { low.hidden = !box.checked; }"
        "}"
        "list.addEventListener('change', function (event) {"
        "if (event.target && event.target.type === 'checkbox') {"
        "syncLowest(event.target.closest('li'));"
        "}"
        "});"
        "var all = list.querySelectorAll('li');"
        "for (var k = 0; k < all.length; k++) { syncLowest(all[k]); }"
        "})();</script>"
    )
    closed = set(fields.get("closed") or [])
    closed_boxes = "".join(
        "<label class='inline'><input type='checkbox' name='closed' value='{}'{}> {}</label>".format(
            key, " checked" if key in closed else "", short
        )
        for key, short in zip(DAY_KEYS, ONBOARD_DAY_SHORT)
    )
    open_value = _onboard_value(fields, "open", "10:00")
    close_value = _onboard_value(fields, "close", "20:00")
    if len(open_value) != 5 or open_value[2] != ":":
        open_value = "10:00"
    if len(close_value) != 5 or close_value[2] != ":":
        close_value = "20:00"
    return _onboard_page(
        "Set up your shop",
        "<header><h1>Set up your shop</h1>"
        "<p class='intro'>Your WhatsApp starts answering customers. "
        "It takes about 3 minutes. It is free while we test it with a "
        "few shops, and nothing goes live until you say so.</p></header>"
        "<div class='thread'><p class='threadtitle'>What your customers will see</p>"
        "<div class='msg in'><p>facial ka price kya hai?</p></div>"
        "<div class='msg out'><p>Facial at Glow &amp; Grace Salon is Rs 1499.</p></div></div>"
        "<form method='post' action='/onboard'>"
        "<h2>Your shop</h2>"
        "<div class='field'><label>Shop name "
        "<input type='text' name='name' maxlength='100' value='{}'></label>{}"
        "<p class='quiet'>The name your customers know.</p>"
        "<label>What kind of shop is it?</label>{}{}"
        "<label>City <input type='text' name='city' maxlength='100' value='{}'></label>{}"
        "<label>Address <input type='text' name='address' maxlength='200' value='{}'></label>{}"
        "<p class='quiet'>A landmark works, like near Nirvana Country, Sector 50.</p>"
        "<label>Your name <input type='text' name='owner_name' maxlength='100' value='{}'></label>{}"
        "<p class='quiet'>So we know who we are talking to.</p></div>"
        "<h2>Services and prices</h2>"
        "<div class='field'><p class='quiet'>One per row. Leave the price blank "
        "when you want to tell the customer yourself. Tick price varies "
        "when the price depends on the service, then set the lowest "
        "price you will accept.</p>{}</div>"
        "<ul class='list'>{}</ul>{}"
        "<h2>Hours</h2>"
        "<div class='field'><p class='quiet'>The same hours every day. "
        "If a day is different, you can change it later.</p>"
        "<label>Opens at <input type='time' name='open' value='{}'></label> "
        "<label>Closes at <input type='time' name='close' value='{}'></label>{}"
        "<label>Closed on</label>{}</div>"
        "<h2>How do we reach you</h2>"
        "<div class='field'><label>Your WhatsApp number "
        "<input type='tel' name='phone' inputmode='tel' value='{}'></label>{}"
        "<p class='quiet'>The number your customers already message. "
        "Only Aditya sees it, and nothing goes live until you send him "
        "your shop file.</p>"
        "<label>Type it again "
        "<input type='tel' name='phone_confirm' inputmode='tel' value='{}'></label>{}"
        "<p class='quiet'>A wrong number means your receptionist answers from nowhere.</p>"
        "<button type='submit' class='primary'>See my receptionist</button></div>"
        "</form>"
        "<p class='foot'><a href='/'>Back to the demo shop</a></p>".format(
            html.escape(_onboard_value(fields, "name"), quote=True),
            _onboard_error(errors, "name"),
            radios,
            _onboard_error(errors, "category"),
            html.escape(_onboard_value(fields, "city"), quote=True),
            _onboard_error(errors, "city"),
            html.escape(_onboard_value(fields, "address"), quote=True),
            _onboard_error(errors, "address"),
            html.escape(_onboard_value(fields, "owner_name"), quote=True),
            _onboard_error(errors, "owner_name"),
            _onboard_error(errors, "services"),
            "".join(rows),
            add_service,
            html.escape(open_value, quote=True),
            html.escape(close_value, quote=True),
            _onboard_error(errors, "hours"),
            closed_boxes,
            html.escape(_onboard_value(fields, "phone"), quote=True),
            _onboard_error(errors, "phone"),
            html.escape(_onboard_value(fields, "phone_confirm"), quote=True),
            _onboard_error(errors, "phone_confirm"),
        ),
    )


def _onboard_confirmation_html(profile, fields):
    """What she told us, as the bot will use it, a sample of her own
    bot answering a customer, and the honest next step: send the file
    to Aditya. Nothing here claims the bot is live."""
    owner_name = (fields.get("owner_name") or [""])[0].strip()
    title = "Thanks, {}.".format(owner_name) if owner_name else "Your shop file is ready."
    place = _onboard_place_word(profile["category"])
    service_lines = []
    for service in profile["services"]:
        if service["price"] is not None:
            line = "{}: Rs {}, {} minutes".format(
                service["name"], service["price"], service["duration_min"]
            )
        elif service.get("price_varies"):
            line = "{}: price varies, the {} confirms it".format(
                service["name"], place
            )
        else:
            line = "{}: price to confirm".format(service["name"])
        if service.get("floor_price") is not None:
            line += ", lowest Rs {}".format(service["floor_price"])
        service_lines.append("<li>{}</li>".format(html.escape(line)))
    question, answer = _onboard_sample_reply(profile)
    hidden = _onboard_hidden_fields(fields)
    return _onboard_page(
        title,
        "<header><h1>{}</h1>"
        "<p class='intro'>This is your shop, as your receptionist will "
        "know it. Check it over, then download the file and send it to "
        "Aditya.</p></header>"
        "<h2>What you told us</h2>"
        "<div class='card'><p><strong>{}</strong>, {}</p>"
        "<p>{}</p>"
        "{}"
        "<ul>{}</ul></div>"
        "<h2>What your customers will see</h2>"
        "<div class='thread'><div class='msg in'><p>{}</p></div>"
        "<div class='msg out'><p>{}</p></div></div>"
        "<h2>What happens next</h2>"
        "<div class='card'><p>Send this file to Aditya on WhatsApp and "
        "he will switch your receptionist on. Until then nothing "
        "changes for your customers. Your number works exactly as it "
        "does today.</p></div>"
        "<div class='field'><form method='post' action='/onboard/download'>{}"
        "<button type='submit' class='primary'>Download your shop file</button></form>"
        "<p class='quiet'>This file is your shop. Keep it safe and send it to Aditya.</p>"
        "<form method='post' action='/onboard'>{}"
        "<input type='hidden' name='_action' value='edit'>"
        "<button type='submit' class='ghost'>Change something</button></form></div>"
        "<p class='foot'><a href='/'>Back to the demo shop</a></p>".format(
            html.escape(title),
            html.escape(profile["name"]),
            html.escape(profile["category"]),
            html.escape(_onboard_hours_line(profile["hours"])),
            (
                "<p>{}</p>".format(html.escape(profile["address"]))
                if profile["address"]
                else ""
            ),
            "".join(service_lines),
            html.escape(question),
            html.escape(answer),
            hidden,
            hidden,
        ),
    )


def _reject_confirmation_html(booking):
    """The owner checks before a booking is rejected: who, what and
    when, plus two choices that stand alone. The slot stays open and
    the customer hears nothing unless the owner ticks the box."""
    when = format_date(date.fromisoformat(booking["date"]))
    who = booking.get("name") or booking["phone"]
    return _onboard_page(
        "Reject this booking",
        "<header><h1>Reject this booking?</h1>"
        "<p class='intro'>{} for {} on {} at {}. The booking is "
        "removed and the time opens up again for other customers.</p></header>"
        "<div class='card'><form method='post' action='/bookings/reject'>"
        "<input type='hidden' name='phone' value='{}'>"
        "<input type='hidden' name='date' value='{}'>"
        "<input type='hidden' name='start_min' value='{}'>"
        "<label class='inline'><input type='checkbox' name='block_slot' value='1'> "
        "Block this time on the calendar</label>"
        "<p class='quiet'>Tick this to keep the time closed, so nobody else can book it.</p>"
        "<label class='inline'><input type='checkbox' name='inform' value='1'> "
        "Tell the customer on WhatsApp</label>"
        "<p class='quiet'>Sends a short message that the booking could not be kept. "
        "When sending is off the message is recorded, not sent.</p>"
        "<button type='submit' class='primary'>Reject this booking</button></form></div>"
        "<p class='foot'><a href='/?tab=bookings'>Keep the booking</a></p>".format(
            html.escape(booking["service"]),
            html.escape(who),
            html.escape(when),
            html.escape(format_time(booking["start_min"])),
            html.escape(booking["phone"], quote=True),
            html.escape(booking["date"], quote=True),
            booking["start_min"],
        ),
    )


def make_handler(studio, router=None):
    """The HTTP handler. With a router, the webhook routes each change
    to its shop; everything else keeps showing the default shop."""
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

        def _read_body(self, limit=ONBOARD_MAX_BODY):
            """Read a form body, refusing anything over limit with a
            413. The webhook keeps its own read path: signature
            verification needs the raw body before anything is parsed.
            Returns None when the body was refused."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > limit:
                self.close_connection = True
                self._send_json(413, {"error": "body too large"})
                return None
            return self.rfile.read(length) if length else b""

        def _require_auth(self):
            """True when this request may proceed. When a dashboard
            password is set, every path except the webhook, /health
            and /onboard needs Basic Auth — Meta calls the webhook
            and cannot log in, health checks must stay open, and a
            prospect filling in the form has no password yet."""
            if not studio.dashboard_password:
                return True
            if check_basic_auth(studio.dashboard_password, self.headers.get("Authorization")):
                return True
            self._unauthorized()
            return False

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                # The uptime check. On the database backend it also says
                # whether saving works: a 503 when the database is down
                # tells Render and Meta that the shop cannot take
                # bookings right now, instead of quietly losing them.
                ok = studio.db_status()
                backend = getattr(studio.leads, "backend", "json")
                self._send_json(
                    200 if ok else 503,
                    {
                        "status": "ok" if ok else "degraded",
                        "business": studio.profile.name,
                        "live": studio.live,
                        "sending": studio.sending_state(),
                        "db": backend,
                        "db_ok": ok,
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
            elif parsed.path == "/onboard":
                # Public: a prospect has no dashboard password yet.
                # The form writes nothing to the server, so there is
                # nothing privileged behind it.
                self._send_text(200, _onboard_form_html({}, {}), "text/html")
            elif not self._require_auth():
                return
            elif parsed.path == "/leads":
                self._send_json(200, {"leads": studio.leads.all_leads()})
            elif parsed.path == "/bookings":
                self._send_json(200, {"bookings": studio.bookings.all_bookings()})
            elif parsed.path == "/bookings/reject":
                # The confirmation page before a reject: who, what and
                # when, plus the two choices. Nothing is removed here.
                qs = parse_qs(parsed.query)
                phone = qs.get("phone", [""])[0]
                date_text = qs.get("date", [""])[0]
                try:
                    day = date.fromisoformat(date_text)
                    start_min = int(qs.get("start_min", [""])[0])
                except ValueError:
                    day = None
                    start_min = None
                booking = None
                if day is not None and start_min is not None:
                    booking = next(
                        (
                            b for b in studio.bookings.bookings_on(day)
                            if b["phone"] == phone and b["start_min"] == start_min
                        ),
                        None,
                    )
                if booking is None:
                    self._send_json(404, {"error": "booking not found"})
                else:
                    self._send_text(
                        200, _reject_confirmation_html(booking), "text/html"
                    )
            elif parsed.path == "/handoffs":
                self._send_json(200, {"handoffs": studio.handoffs.all_handoffs()})
            elif parsed.path == "/regulars":
                self._send_json(
                    200,
                    {
                        "regulars": [
                            {**r, "last_date": r["last_date"].isoformat()}
                            for r in studio.bookings.regulars()
                        ]
                    },
                )
            elif parsed.path == "/missed":
                self._send_json(
                    200, {"missed": studio.missed.open_newest()}
                )
            elif parsed.path == "/export":
                # The owner's way out and the backup before a redeploy:
                # everything as one downloadable JSON file. Leads,
                # bookings, handoffs, blocks, missed customers and the
                # reply history, plus the counts and the export time,
                # so an export can be checked without opening it.
                payload = {
                    "exported_at": datetime.fromtimestamp(
                        time.time(), tz=IST
                    ).isoformat(),
                    "profile": studio.profile.to_dict(),
                    "leads": studio.leads.all_leads(),
                    "bookings": studio.bookings.all_bookings(),
                    "handoffs": studio.handoffs.all_handoffs(),
                    "blocks": studio.blocks.all_blocks(),
                    "missed": studio.missed.all_records(),
                    "outbox": studio.outbox,
                    "counts": {
                        "leads": len(studio.leads.all_leads()),
                        "bookings": len(studio.bookings.all_bookings()),
                        "handoffs": len(studio.handoffs.all_handoffs()),
                        "blocks": len(studio.blocks.all_blocks()),
                        "missed": len(studio.missed.all_records()),
                        "outbox": len(studio.outbox),
                    },
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
                qs = parse_qs(parsed.query)
                tab = qs.get("tab", ["today"])[0]
                mock = qs.get("mock", [""])[0] == "1"
                self._send_text(200, studio.dashboard_html(tab, mock=mock), "text/html")
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
                if router is not None:
                    count = router.handle_webhook_payload(payload)
                else:
                    count = studio.handle_webhook_payload(payload)
                self._send_json(200, {"received": True, "processed": count})
                return
            if parsed.path == "/onboard":
                # Public, like the GET: the form writes nothing to the
                # server, it only validates and shows the shop back.
                raw = self._read_body()
                if raw is None:
                    return
                fields = parse_qs(
                    raw.decode("utf-8", errors="replace"), keep_blank_values=True
                )
                if (fields.get("_action") or [""])[0] == "edit":
                    self._send_text(200, _onboard_form_html(fields, {}), "text/html")
                    return
                profile, errors = validate_onboarding_profile(fields)
                if errors:
                    self._send_text(
                        200, _onboard_form_html(fields, errors), "text/html"
                    )
                    return
                self._send_text(
                    200, _onboard_confirmation_html(profile, fields), "text/html"
                )
                return
            if parsed.path == "/onboard/download":
                # The confirmation page posts the same fields back; the
                # file is built from a fresh validation, never from
                # anything stored, because nothing is stored.
                raw = self._read_body()
                if raw is None:
                    return
                fields = parse_qs(
                    raw.decode("utf-8", errors="replace"), keep_blank_values=True
                )
                profile, errors = validate_onboarding_profile(fields)
                if errors:
                    self._send_text(
                        200, _onboard_form_html(fields, errors), "text/html"
                    )
                    return
                data = json.dumps(profile, indent=2).encode("utf-8")
                slug = _onboard_slug(profile["name"]) or "shop"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header(
                    "Content-Disposition",
                    'attachment; filename="{}.json"'.format(slug),
                )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
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
            if parsed.path == "/negotiation/enable":
                studio.enable_negotiation()
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/negotiation/disable":
                studio.disable_negotiation()
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/setup/save":
                # The owner saves prices, durations and hours. Blank
                # inputs must survive parsing, or a blank price reads
                # as a missing field instead of price on request.
                raw = self._read_body()
                if raw is None:
                    return
                fields = parse_qs(
                    raw.decode("utf-8", errors="replace"), keep_blank_values=True
                )
                studio._notice = studio.apply_setup(fields)
                self.send_response(303)
                self.send_header("Location", "/?tab=setup")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/blocks/add":
                # One day at a time, no recurring blocks. A block over
                # an existing booking is refused and the booking is
                # named, so the owner cancels or moves it first.
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                fields = parse_qs(raw.decode("utf-8", errors="replace"))
                day_text = (fields.get("day") or [""])[0].strip()
                start_text = (fields.get("start") or [""])[0].strip()
                end_text = (fields.get("end") or [""])[0].strip()
                reason = (fields.get("reason") or [""])[0].strip()
                try:
                    day = date.fromisoformat(day_text)
                except ValueError:
                    day = parse_day(day_text, india_today())
                start = parse_time(start_text) if start_text else None
                end = parse_time(end_text) if end_text else None
                today = india_today()
                now_min = india_now().hour * 60 + india_now().minute
                if day is None:
                    notice = "Pick a day to block."
                elif start is None or end is None:
                    notice = "Pick a start and an end time."
                elif start >= end:
                    notice = "The end time must be after the start time."
                elif day < today:
                    notice = "You cannot block a day that has already gone."
                elif day == today and end <= now_min:
                    notice = "That time has already gone."
                elif is_closed(studio.profile.hours_today(day.weekday())):
                    notice = "The shop is closed that day."
                else:
                    conflicts = studio.block_conflicts(day, start, end)
                    if conflicts:
                        first = conflicts[0]
                        notice = (
                            "That time already has a booking: {} for {} at {}. "
                            "Cancel it or move it first, then block the time."
                        ).format(
                            first.get("name") or first["phone"],
                            first["service"],
                            format_time(first["start_min"]),
                        )
                    else:
                        studio.blocks.add(day, start, end, reason)
                        when = "today" if day == today else format_date(day)
                        notice = "Blocked {} {} to {}".format(
                            when, format_time(start), format_time(end)
                        )
                        if reason:
                            notice += " for {}".format(reason)
                        notice += "."
                studio._notice = notice
                self.send_response(303)
                self.send_header("Location", "/?tab=bookings")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/blocks/remove":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                fields = parse_qs(raw.decode("utf-8", errors="replace"))
                try:
                    block_id = int((fields.get("block_id") or [""])[0])
                except ValueError:
                    block_id = None
                removed = (
                    studio.blocks.remove(block_id) if block_id is not None else None
                )
                if removed is None:
                    self._send_json(404, {"error": "block not found"})
                    return
                studio._notice = "Opened that time again."
                self.send_response(303)
                self.send_header("Location", "/?tab=bookings")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/bookings/reject":
                # The owner confirmed on the reject page: remove the
                # booking, block the time and tell the customer only
                # when asked, then say what happened on the Bookings
                # tab. A booking already rejected is not found here,
                # so it can never be rejected twice.
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                fields = parse_qs(raw.decode("utf-8", errors="replace"))
                phone = (fields.get("phone") or [""])[0]
                date_text = (fields.get("date") or [""])[0]
                try:
                    day = date.fromisoformat(date_text)
                    start_min = int((fields.get("start_min") or [""])[0])
                except ValueError:
                    day = None
                    start_min = None
                if day is None or start_min is None:
                    self._send_json(404, {"error": "booking not found"})
                    return
                result = studio.reject_booking(
                    phone,
                    day,
                    start_min,
                    block_slot="block_slot" in fields,
                    inform="inform" in fields,
                )
                if result is None:
                    self._send_json(404, {"error": "booking not found"})
                    return
                booking = result["booking"]
                who = booking.get("name") or booking["phone"]
                notice = (
                    "Rejected {}'s {} booking on {} at {}. "
                    "That time is open again."
                ).format(
                    who,
                    booking["service"],
                    format_date(day),
                    format_time(booking["start_min"]),
                )
                if result["blocked"]:
                    notice += " Blocked that time on the calendar."
                if result["informed"]:
                    if studio.live:
                        notice += " {} has been told on WhatsApp.".format(who)
                    else:
                        notice += (
                            " The message was recorded, not sent, "
                            "because sending is off."
                        )
                studio._notice = notice
                self.send_response(303)
                self.send_header("Location", "/?tab=bookings")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/missed/resolve":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                fields = parse_qs(raw.decode("utf-8", errors="replace"))
                try:
                    missed_id = int((fields.get("missed_id") or [""])[0])
                except ValueError:
                    missed_id = None
                how = (fields.get("how") or [""])[0]
                if (
                    missed_id is None
                    or studio.missed.resolve(missed_id, how) is None
                ):
                    self._send_json(404, {"error": "missed record not found"})
                    return
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
                studio._drafts.pop(handoff_id, None)
                studio._draft_failed.discard(handoff_id)
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path.startswith("/handoffs/") and parsed.path.endswith("/draft"):
                # The owner asked the brain for a draft. It only prefills
                # the reply box on the Replies tab; nothing is sent, and
                # the handoff stays open until the owner sends or resolves.
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
                draft = studio.draft_reply(handoff["phone"])
                if draft is None:
                    if studio.llm is not None:
                        studio._draft_failed.add(handoff_id)
                    studio._drafts.pop(handoff_id, None)
                else:
                    studio._drafts[handoff_id] = draft
                    studio._draft_failed.discard(handoff_id)
                self.send_response(303)
                self.send_header("Location", "/?tab=replies")
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
                    "ts": int(time.time()),
                }
                studio.outbox.append(entry)
                studio._dispatch(entry)
                studio._save_outbox()
                studio.handoffs.resolve(handoff_id)
                # The owner answered, so the question the bot could
                # not answer is answered too.
                studio.missed.resolve_for_handoff(handoff_id, "answered")
                studio._drafts.pop(handoff_id, None)
                studio._draft_failed.discard(handoff_id)
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_json(404, {"error": "not found"})

    return Handler


def make_server(studio, host="127.0.0.1", port=0, router=None):
    return ThreadingHTTPServer((host, port), make_handler(studio, router=router))
