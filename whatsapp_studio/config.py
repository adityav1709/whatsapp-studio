"""Business profile configuration for WhatsApp Studio.

A profile is plain JSON so a non-technical business owner (or the
onboarding flow) can edit it without touching code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .bookings import india_today


@dataclass
class Service:
    name: str
    price: int  # INR
    duration_min: int = 30
    description: str = ""
    # How many customers can be served at once (chairs, doctors, trainers).
    # 1 is one provider; a two-chair salon sets 2.
    capacity: int = 1


@dataclass
class BusinessProfile:
    name: str
    category: str  # e.g. "salon", "dental clinic", "gym"
    city: str
    phone: str  # business WhatsApp number, digits only
    hours: dict = field(default_factory=dict)  # {"mon": "10:00-20:00", ...} or {"daily": ...}
    services: list = field(default_factory=list)
    greeting: str = ""
    address: str = ""
    offers: str = ""  # e.g. "20% off on all facials this week"

    @classmethod
    def from_json(cls, path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        services = [Service(**s) for s in data.get("services", [])]
        return cls(
            name=data["name"],
            category=data.get("category", "business"),
            city=data.get("city", ""),
            phone=data.get("phone", ""),
            hours=data.get("hours", {}),
            services=services,
            greeting=data.get("greeting", ""),
            address=data.get("address", ""),
            offers=data.get("offers", ""),
        )

    def service_named(self, text):
        """Find a service whose name (or a distinctive word of it) appears in text."""
        lowered = text.lower()
        for service in self.services:
            name = service.name.lower()
            if name in lowered:
                return service
            for word in name.split():
                # Short words match everywhere ("cut" is in half of all messages),
                # so only words long enough to mean something count on their own.
                if len(word) >= 4 and word in lowered:
                    return service
        return None

    def hours_today(self, weekday=None):
        """Opening hours for a weekday (0=Mon .. 6=Sun); defaults to today
        in India. A day's value may be "closed"."""
        if weekday is None:
            weekday = india_today().weekday()
        keys = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        day = keys[weekday]
        if day in self.hours:
            return self.hours[day]
        return self.hours.get("daily", "10:00 AM - 8:00 PM")
