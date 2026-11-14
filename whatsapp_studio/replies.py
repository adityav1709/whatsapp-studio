"""Reply templates. Plain text on purpose: it needs no Meta template approval, unlike formatted messages."""
from __future__ import annotations

from datetime import date as _date

from .bookings import format_date, format_time
from .intents import BOOKING, CANCEL, GREETING, HOURS, HUMAN, LOCATION, PRICE, THANKS


def booking_confirmed(profile, booking):
    return (
        f"Done! Your {booking['service']} at {profile.name} is booked for "
        f"{format_date(_date.fromisoformat(booking['date']))} "
        f"at {format_time(booking['start_min'])} "
        f"({booking['duration_min']} min, Rs {booking['price']}). "
        "We will see you then! Reply 'cancel my booking' if your plans change."
    )


def booking_taken(profile, service, day, start_min, alternatives):
    lines = [
        f"Sorry, {format_time(start_min)} on {format_date(day)} is already taken "
        f"for {service.name}.",
    ]
    if alternatives:
        lines.append(
            "The nearest free slots that day are: "
            + ", ".join(format_time(s) for s in alternatives[:4])
            + ". Reply with one of these times to book it."
        )
    else:
        lines.append("That day is fully booked — would another day work?")
    return " ".join(lines)


def slots_available(profile, service, day, slots):
    if not slots:
        return (
            f"Sorry, {service.name} is fully booked on {format_date(day)}. "
            "Would another day work?"
        )
    return (
        f"Free slots for {service.name} on {format_date(day)}: "
        + ", ".join(format_time(s) for s in slots[:8])
        + ". Reply with a time to book it."
    )


def booking_cancelled(profile, booking):
    return (
        f"Done — your {booking['service']} on "
        f"{format_date(_date.fromisoformat(booking['date']))} "
        f"at {format_time(booking['start_min'])} is cancelled. "
        "Hope to see you another day!"
    )


def booking_none_to_cancel(profile):
    return "I could not find an upcoming booking for you to cancel."


def booking_ask_when(profile, service):
    return (
        f"Great, {service.name} (Rs {service.price}, {service.duration_min} min). "
        "Which day and time works for you? For example, 'tomorrow 11am'."
    )


def build_reply(profile, intent, text, service=None):
    name = profile.name
    if intent == GREETING:
        services = ", ".join(s.name for s in profile.services[:4])
        base = profile.greeting or f"Hello! Welcome to {name}."
        extra = f" We offer {services}." if services else ""
        return (
            f"{base}{extra} How can I help you today? "
            "You can ask about prices, timings, or booking an appointment."
        )
    if intent == HOURS:
        return (
            f"{name} is open {profile.hours_today()}. "
            "Walk-ins are welcome, or I can book a slot for you."
        )
    if intent == PRICE:
        if service is not None:
            return (
                f"{service.name} at {name} is Rs {service.price} "
                f"({service.duration_min} min). Want me to book a slot for you?"
            )
        lines = [f"{name} price list:"]
        for s in profile.services:
            lines.append(f"- {s.name}: Rs {s.price}")
        lines.append("Reply with the service name to book, or ask me anything else.")
        return "\n".join(lines)
    if intent == BOOKING:
        if service is not None:
            return booking_ask_when(profile, service)
        offered = ", ".join(s.name for s in profile.services)
        return f"Sure, I can help you book. Which service would you like? {offered}" if offered else (
            "Sure, I can help you book. Which service would you like?"
        )
    if intent == CANCEL:
        return booking_none_to_cancel(profile)
    if intent == LOCATION:
        address = profile.address or f"{name}, {profile.city}"
        return f"You can find us here: {address}. Would you like me to book a visit?"
    if intent == HUMAN:
        return (
            "Of course, I will connect you with a person from the team. "
            "They usually reply within a few hours during working hours. "
            "Meanwhile, can I help with prices, timings, or booking?"
        )
    if intent == THANKS:
        return "You are most welcome! Anything else I can help with?"
    return (
        f"Thanks for messaging {name}! I can help with prices, timings, "
        "booking an appointment, or directions. What would you like to know?"
    )
