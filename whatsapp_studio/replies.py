"""Reply templates. Plain text on purpose: it needs no Meta template approval, unlike formatted messages."""
from __future__ import annotations

from .intents import BOOKING, GREETING, HOURS, HUMAN, LOCATION, PRICE, THANKS


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
            return (
                f"Sure, I can book {service.name} (Rs {service.price}) for you. "
                "Please reply with your preferred day and time, and your name."
            )
        offered = ", ".join(s.name for s in profile.services)
        return f"Sure, I can help you book. Which service would you like? {offered}" if offered else (
            "Sure, I can help you book. Which service would you like?"
        )
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
