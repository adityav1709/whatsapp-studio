"""Owner notifications for handoffs, bookings, failed sends, and
onboarding submissions. Best-effort helpers on top of the dashboard,
never a reason for a handoff, a booking, or a form submit to fail.

The interface is an object with ``notify(business_name, handoff)``,
``notify_booking(business_name, booking)``,
``notify_send_failed(business_name, entry)`` and
``notify_onboard(profile, seen_before=False)``. There are four
implementations: Telegram (owner-supplied bot token and chat id from
the environment, off by default), WhatsApp (a Meta template message to
the owner's personal number, for handoffs only), a fan-out that sends
through each channel in turn, and a null one that does nothing. No
credentials live in this repo.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import date, datetime, timedelta

from .bookings import IST, format_date, format_time
from .config import DAY_KEYS, DAY_LABELS


def _waiting_since(opened_at, now):
    """When a handoff opened, in plain words for the morning ping:
    "last night at 10:20 PM", "today at 8:00 AM", "yesterday at
    3:00 PM", or "Tue at 10:20 PM". The age is the point of a held
    ping: without it the owner reads an overnight wait as fresh."""
    opened = datetime.fromtimestamp(opened_at, tz=IST)
    when = format_time(opened.hour * 60 + opened.minute)
    today = now.date()
    if opened.date() == today:
        return "today at {}".format(when)
    if opened.date() == today - timedelta(days=1):
        if opened.hour >= 18:
            return "last night at {}".format(when)
        return "yesterday at {}".format(when)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return "{} at {}".format(days[opened.weekday()], when)


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
        """Returns "sent" or "failed": the queue needs to know whether
        the ping went out, so a failed morning ping is not mistaken
        for a delivered one."""
        if self._send(
            f"Customer needs you — {business_name}\n"
            f"{handoff.get('name') or 'Customer'} ({handoff['phone']})\n"
            f"Reason: {handoff['reason']}\n"
            f"\"{handoff['trigger']}\""
        ):
            return "sent"
        return "failed"

    def _held_lines(self, handoffs, now):
        """One line per held handoff, oldest first: name, number,
        reason, and when it opened. The age is what lets the owner
        call the longest waiter first."""
        lines = []
        for handoff in sorted(handoffs, key=lambda h: h["opened_at"]):
            lines.append(
                "{}, {}, waiting since {}".format(
                    "{} ({})".format(
                        handoff.get("name") or "Customer", handoff["phone"]
                    ),
                    (handoff.get("reason") or "").strip() or "asked for a person",
                    _waiting_since(handoff["opened_at"], now),
                )
            )
            lines.append("\"{}\"".format(handoff.get("trigger") or ""))
        return lines

    def notify_overnight_summary(self, business_name, handoffs, now=None):
        """One morning message for every held handoff, oldest first.
        Returns "sent" or "failed". The WhatsApp template is one
        customer per message, so the single catch-up list lives here,
        on the free-form channel."""
        now = now or datetime.now(IST)
        headline = (
            "Customer waited overnight at {}"
            if len(handoffs) == 1
            else "Customers waited overnight at {}"
        ).format(business_name)
        text = "\n".join([headline] + self._held_lines(handoffs, now))
        return "sent" if self._send(text) else "failed"

    def notify_still_waiting(self, business_name, handoffs, now=None):
        """The one nudge: same list, different sentence, so it never
        reads as a resend glitch. Returns "sent" or "failed"."""
        now = now or datetime.now(IST)
        text = "\n".join(
            ["Still waiting, no reply yet at {}".format(business_name)]
            + self._held_lines(handoffs, now)
        )
        return "sent" if self._send(text) else "failed"

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
        # Nothing to send through, so the queue leaves the ping held
        # rather than dropping it as delivered.
        return "skipped"

    def notify_booking(self, business_name, booking):
        return None

    def notify_send_failed(self, business_name, entry):
        return None

    def notify_onboard(self, profile, seen_before=False):
        return None


def _digits(number):
    return "".join(c for c in (number or "") if c.isdigit())


def _shop_env_suffix(shop_id):
    """The per-shop suffix for an env var, `_SHOP`, with the shop id
    uppercased and anything that cannot appear in an env var turned
    into an underscore — the same rule as
    ``security.py::dashboard_password_env_name``."""
    if not shop_id:
        return ""
    safe = "".join(c if c.isalnum() else "_" for c in str(shop_id).upper())
    return "_" + safe


def _owner_whatsapp_env(shop_id):
    """(owner number, template name, language) for one shop: per-shop
    vars win over the base vars. The number is the owner's personal
    WhatsApp number, digits with an optional leading +, and the
    template is the name Meta approved for owner alerts. Unset means
    the channel was never configured."""
    suffix = _shop_env_suffix(shop_id)
    number = os.environ.get("OWNER_WHATSAPP_NUMBER" + suffix) or os.environ.get(
        "OWNER_WHATSAPP_NUMBER"
    )
    template = os.environ.get("OWNER_TEMPLATE_NAME" + suffix) or os.environ.get(
        "OWNER_TEMPLATE_NAME"
    )
    language = (
        os.environ.get("OWNER_TEMPLATE_LANGUAGE" + suffix)
        or os.environ.get("OWNER_TEMPLATE_LANGUAGE")
        or "en_US"
    )
    return number, template, language


def _code_template_env(shop_id):
    """(template name, language) for the onboarding number check: the
    template Meta approved for sending the setup code. Same shape as
    _owner_whatsapp_env, per-shop vars winning over the base vars. A
    prospect has no shop id yet, so the base CODE_TEMPLATE_NAME is
    the one that bites at onboarding; the suffix shape is kept so the
    helper works the same if the route ever runs under a shop scope.
    Unset means the check was never configured."""
    suffix = _shop_env_suffix(shop_id)
    template = os.environ.get("CODE_TEMPLATE_NAME" + suffix) or os.environ.get(
        "CODE_TEMPLATE_NAME"
    )
    language = (
        os.environ.get("CODE_TEMPLATE_LANGUAGE" + suffix)
        or os.environ.get("CODE_TEMPLATE_LANGUAGE")
        or "en_US"
    )
    return template, language


def code_challenge_status(sender, shop_id, demo_mode):
    """(ready, reason) for the onboarding number check: ready True when
    the code can be sent; reason the plain-words cause when it cannot,
    or None when the check was simply never configured — not an error,
    and not worth a boot log line. Mirrors whatsapp_channel_status:
    demo mode and a missing sender keep the check off, and a missing
    template means it was never set up."""
    template, _language = _code_template_env(shop_id)
    if not template:
        return False, None
    if demo_mode:
        return False, "Demo mode is on, so the number check is off."
    if sender is None:
        return (
            False,
            "Set WHATSAPP_TOKEN and PHONE_NUMBER_ID on Render so the number check can send.",
        )
    return True, None


def owner_whatsapp_configured(shop_id):
    """True when the owner has started setting up the WhatsApp ping —
    the number or the template name is set. The setup checklist shows
    the ping line only then: the ping is an optional upgrade, and a
    Telegram-only shop is a complete setup."""
    number, template, _language = _owner_whatsapp_env(shop_id)
    return bool(number or template)


def whatsapp_channel_status(sender, shop_id, shop_number, demo_mode):
    """(ready, reason) for the owner WhatsApp ping: ready True when the
    channel can be built; reason the plain-words cause when it cannot,
    or None when the channel was simply never configured — not an
    error, and not worth a boot log line."""
    number, template, _language = _owner_whatsapp_env(shop_id)
    if not number and not template:
        return False, None
    if demo_mode:
        return False, "Demo mode is on, so the owner WhatsApp ping is off."
    if sender is None:
        return (
            False,
            "Set WHATSAPP_TOKEN and PHONE_NUMBER_ID on Render so the owner WhatsApp ping can send.",
        )
    if not number:
        return (
            False,
            "Set OWNER_WHATSAPP_NUMBER on Render — your personal number, not the shop's number.",
        )
    digits = _digits(number)
    if not 7 <= len(digits) <= 15:
        return False, "OWNER_WHATSAPP_NUMBER is not a valid WhatsApp number."
    if not template:
        return (
            False,
            "Set OWNER_TEMPLATE_NAME on Render — the name of the template Meta approved for owner alerts.",
        )
    if shop_number and digits == _digits(shop_number):
        return (
            False,
            "The owner number is the shop's own number — set your personal number, not the shop's.",
        )
    return True, None


def _meta_error(exc):
    """(code, message) from a failed Meta send: the error body when the
    call reached Meta, else no code and the exception's own words."""
    if getattr(exc, "read", None) is not None:
        try:
            error = json.loads(exc.read().decode("utf-8")).get("error") or {}
            return error.get("code"), error.get("message") or str(exc)
        except Exception:
            pass
    return None, str(exc)


# Meta's words for the failures an owner can actually fix, keyed by
# the code in the error body. Anything else is reported by its code.
# 131026 is Meta's "message undeliverable": the number is not on
# WhatsApp, the recipient has not accepted the current terms, or the
# app is too old. The actionable reading is the first of the three,
# which is why the code challenge treats it as not on WhatsApp.
_META_ERROR_WORDS = {
    132000: "template not approved by Meta yet",
    131030: "template or recipient problem",
    131026: "message could not be delivered",
}


class WhatsAppNotifier:
    """Ping the owner on WhatsApp the moment a handoff opens, with a
    Meta template message — the only way to reach the owner outside
    the 24-hour window. Handoffs only: every message shape needs its
    own approved template, and the handoff is the urgent one; bookings
    and onboarding stay on Telegram."""

    def __init__(self, sender, owner_number, template_name, language="en_US", is_live=None):
        self.sender = sender
        self.owner_number = owner_number
        self.template_name = template_name
        self.language = language
        self._is_live = is_live
        # The last failed ping, {"code", "message", "at"}: the checklist
        # reports it, so a template still waiting for Meta approval
        # says so plainly instead of failing silently. In memory only.
        self.last_failure = None

    def failure_note(self):
        """One plain line about the last failed ping, or None when the
        last ping went out. Never carries Meta's raw error text."""
        if self.last_failure is None:
            return None
        code = self.last_failure.get("code")
        words = _META_ERROR_WORDS.get(code)
        if words:
            return "Last ping failed: {}.".format(words)
        if code is not None:
            return "Last ping failed (Meta error {}).".format(code)
        return "Last ping failed: {}.".format(
            _short(self.last_failure.get("message"), 120)
        )

    def notify(self, business_name, handoff):
        """Returns "sent", "skipped", or "failed": the queue treats the
        three differently, so they must not all look alike. Skipped
        means dry run or the kill switch, never a send; failed means
        Meta said no, and the failure is remembered for the
        checklist."""
        if self._is_live is not None and not self._is_live():
            # Dry run or the kill switch: record what would have gone
            # out, send nothing.
            print(
                "owner whatsapp ping skipped, sending is off: {} to {}".format(
                    business_name, self.owner_number
                )
            )
            return "skipped"
        parameters = [
            _short(business_name, 80),
            "{} ({})".format(
                _short(handoff.get("name") or "Customer", 60), handoff["phone"]
            ),
            _short(handoff.get("reason") or "", 120),
            _short(handoff.get("trigger") or "", 200),
        ]
        try:
            self.sender.send_template(
                self.owner_number, self.template_name, self.language, parameters
            )
        except Exception as exc:
            code, message = _meta_error(exc)
            self.last_failure = {
                "code": code,
                "message": message[:200],
                "at": int(time.time()),
            }
            detail = "{} {}".format(code, message) if code is not None else message
            print("owner whatsapp ping failed: {}".format(detail[:200]))
            return "failed"
        return "sent"

    def notify_booking(self, business_name, booking):
        return None

    def notify_send_failed(self, business_name, entry):
        return None

    def notify_onboard(self, profile, seen_before=False):
        return None


class FanoutNotifier:
    """Send every notification through each channel in turn, so one
    channel failing never stops the next: while the template is still
    waiting for Meta approval, Telegram still pings."""

    def __init__(self, notifiers):
        self.notifiers = [n for n in notifiers if n is not None]

    def _each(self, method, *args):
        for notifier in self.notifiers:
            try:
                getattr(notifier, method)(*args)
            except Exception:
                pass

    def notify(self, business_name, handoff):
        """Send through every channel; returns the combined status.
        "sent" when any channel sent, else "failed" when any failed,
        else "skipped". A notifier that answers nothing (an old test
        double, or a channel that predates statuses) counts as sent:
        the call was attempted and no failure was reported, so the
        queue must not hold the ping forever waiting on it."""
        statuses = []
        for notifier in self.notifiers:
            try:
                status = notifier.notify(business_name, handoff)
            except Exception:
                status = "failed"
            statuses.append(status if status is not None else "sent")
        if "sent" in statuses:
            return "sent"
        if "failed" in statuses:
            return "failed"
        return "skipped"

    def notify_booking(self, business_name, booking):
        self._each("notify_booking", business_name, booking)

    def notify_send_failed(self, business_name, entry):
        self._each("notify_send_failed", business_name, entry)

    def notify_onboard(self, profile, seen_before=False):
        self._each("notify_onboard", profile, seen_before)


def notifier_from_env(sender=None, shop_id=None, demo_mode=False, shop_number=None, is_live=None):
    """The notifier for one shop: Telegram, WhatsApp, both through a
    fan-out, or null when neither is configured. The no-argument call
    keeps working — existing tests construct a Studio without a
    notifier and rely on the env fallback."""
    channels = []
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        channels.append(TelegramNotifier(token, chat_id))
    ready, reason = whatsapp_channel_status(sender, shop_id, shop_number, demo_mode)
    if ready:
        number, template, language = _owner_whatsapp_env(shop_id)
        channels.append(
            WhatsAppNotifier(
                sender, _digits(number), template, language, is_live=is_live
            )
        )
    elif reason is not None:
        # A half-configured channel is worth one boot line; a channel
        # that was never configured is silence, not an error.
        print("owner whatsapp ping off: {}".format(reason))
    if not channels:
        return NullNotifier()
    if len(channels) == 1:
        return channels[0]
    return FanoutNotifier(channels)
