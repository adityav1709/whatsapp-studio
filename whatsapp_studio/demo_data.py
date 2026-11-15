"""Sample day for Demo mode: the showcase data the dashboard shows
when DEMO_MODE=1 and the stores are empty.

Every number is in the fake 9198100000xx range, so sample people can
never be mistaken for real customers. The same steps feed
scripts/seed_demo_data.py, which pushes the day through the webhook
on a fresh instance; the app seeds it straight into the stores at
boot instead, so the bot replies and bookings are there too.
"""
from __future__ import annotations

import time
from datetime import datetime, time as dtime, timedelta

from .bookings import IST, india_now

SAMPLE_PREFIX = "9198100000"

# One sample day at the salon. Each turn is a customer message, how
# many minutes before now it arrived, and the bot reply that followed
# (None when the bot stayed quiet, for example while a handoff was
# open). A handoff turn opens one and the confirmation goes out with
# it. A booking is (service, "today" or "tomorrow", start minutes
# after midnight).
EXCHANGES = [
    {
        "phone": "919810000001",
        "name": "Rahul",
        "turns": [
            {
                "text": "haircut ka price kya hai",
                "ago": 300,
                "reply": "Haircut is Rs 499 for 30 minutes. Want me to book a slot for you?",
            },
            {
                "text": "book haircut today 11am",
                "ago": 290,
                "reply": "Done! Your Haircut is booked for today at 11:00 AM. See you then!",
            },
        ],
        "booking": {"service": "Haircut", "day": "today", "start_min": 660},
    },
    {
        "phone": "919810000002",
        "name": "Priya",
        "turns": [
            {
                "text": "book facial tomorrow 4pm",
                "ago": 200,
                "reply": "Done! Your Facial is booked for tomorrow at 4:00 PM. See you then!",
            },
        ],
        "booking": {"service": "Facial", "day": "tomorrow", "start_min": 960},
    },
    {
        "phone": "919810000003",
        "name": "Amit",
        "turns": [
            {
                "text": "can I talk to a person please",
                "ago": 120,
                "reply": None,
                "handoff": {"reason": "asked for a person"},
            },
            {"text": "it is about a refund for last week", "ago": 100, "reply": None},
        ],
    },
    {
        "phone": "919810000004",
        "name": "Neha",
        "turns": [
            {
                "text": "book hair spa today 2:30pm",
                "ago": 90,
                "reply": "Done! Your Hair Spa is booked for today at 2:30 PM. See you then!",
            },
        ],
        "booking": {"service": "Hair Spa", "day": "today", "start_min": 870},
    },
    {
        "phone": "919810000005",
        "name": "Vikram",
        "turns": [
            {
                "text": "[sent a voice note]",
                "ago": 60,
                "reply": "I can only read typed messages right now. Please type it and I will help right away.",
                "handoff": {"reason": "sent a voice note"},
                "trigger": "sent a voice note",
                "kind": "audio",
            },
        ],
    },
    {
        "phone": "919810000006",
        "name": "Sana",
        "turns": [
            {
                "text": "facial ka price kya hai",
                "ago": 30,
                "reply": "Facial is Rs 1499 for 60 minutes. Want me to book a slot for you?",
            },
        ],
    },
]


def webhook_steps():
    """The same sample day as webhook posts: (phone, name, wamid,
    message type, body). The seed script posts these to a fresh
    instance; the audio step has no body, like a real voice note."""
    steps = []
    n = 0
    for exchange in EXCHANGES:
        for turn in exchange["turns"]:
            n += 1
            kind = turn.get("kind", "text")
            steps.append(
                (
                    exchange["phone"],
                    exchange["name"],
                    f"wamid.mock{n:03d}",
                    kind,
                    turn["text"] if kind == "text" else None,
                )
            )
    return steps


def _handoff_confirmation(profile_name):
    """What the customer hears when the bot hands over during open
    hours: who will reply, and that they need not explain again."""
    return (
        f"I've passed this to the team at {profile_name}. They will reply "
        "here within 30 minutes during working hours, and I've shared our "
        "chat with them so you don't need to explain again."
    )


def _sample_time(base, ago, day_start, previous):
    """Epoch time for a turn that arrived `ago` minutes before `base`.
    Anything that would land before today is pulled into today so the
    dashboard counts it today, and times stay in order even if the
    server boots just after midnight."""
    ts = base - ago * 60
    if ts < day_start:
        ts = day_start + 60
    if previous is not None and ts <= previous:
        ts = previous + 60
    if ts > base:
        ts = base
    return ts


def seed_demo_data(studio, now=None):
    """Fill empty stores with the sample day. Returns True when it
    seeded, False when any store already had data and nothing changed.
    Seeded replies are recorded, never sent: they carry send_status
    recorded, so the dashboard never claims a real phone got one."""
    if (
        studio.leads.all_leads()
        or studio.bookings.all_bookings()
        or studio.handoffs.all_handoffs()
        or studio.outbox
    ):
        return False
    now = now or india_now()
    base = int(now.timestamp())
    day_start = int(datetime.combine(now.date(), dtime(0, 0), tzinfo=IST).timestamp())
    today = now.date()
    previous = None
    for exchange in EXCHANGES:
        phone = exchange["phone"]
        name = exchange["name"]
        for turn in exchange["turns"]:
            ts = _sample_time(base, turn["ago"], day_start, previous)
            previous = ts
            studio.leads.record_message(phone, turn["text"], None, name, ts=ts)
            handoff = turn.get("handoff")
            handoff_id = None
            if handoff is not None:
                opened = studio.handoffs.open(
                    phone,
                    name,
                    handoff["reason"],
                    turn.get("trigger", turn["text"]),
                    opened_at=ts,
                )
                handoff_id = opened["id"]
            reply = turn.get("reply")
            if handoff is not None and reply is None:
                reply = _handoff_confirmation(studio.profile.name)
            if reply:
                entry = {
                    "to": phone,
                    "body": reply,
                    "intent": "handoff" if handoff is not None else None,
                    "ts": min(ts + 60, base),
                    "send_status": "recorded",
                    "wamid": None,
                    "send_error": None,
                }
                if handoff_id is not None:
                    entry["handoff"] = handoff_id
                studio.outbox.append(entry)
        booking = exchange.get("booking")
        if booking is not None:
            service = studio.profile.service_named(booking["service"])
            if service is not None:
                day = today if booking["day"] == "today" else today + timedelta(days=1)
                studio.bookings.book(phone, name, service, day, booking["start_min"])
    studio._save_outbox()
    return True
