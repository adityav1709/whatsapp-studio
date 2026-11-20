"""Business profile configuration for WhatsApp Studio.

A profile is plain JSON so a non-technical business owner (or the
onboarding flow) can edit it without touching code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .bookings import format_time, india_today


def parse_setup_price(text):
    """A price typed on the setup form: blank means price on request,
    otherwise whole rupees from 1 to 100000. Returns (value, error):
    one of them is always None."""
    text = (text or "").strip()
    if not text:
        return None, None
    if not text.isdigit():
        return None, "Prices have to be whole rupees, like 499."
    value = int(text)
    if value < 1 or value > 100000:
        return None, "Prices have to be between Rs 1 and Rs 100000."
    return value, None


def parse_setup_duration(text):
    """A duration typed on the setup form: whole minutes from 5 to 480.
    Blank is refused: a service with no duration cannot be booked."""
    text = (text or "").strip()
    if not text:
        return None, "Every service needs a time in minutes."
    if not text.isdigit():
        return None, "Times have to be whole minutes, like 45."
    value = int(text)
    if value < 5 or value > 480:
        return None, "Times have to be between 5 and 480 minutes."
    return value, None


def parse_setup_day_hours(text):
    """One day of opening hours typed on the setup form: the word
    closed, or a clock range the shop already understands. Returns
    (value, error); the value is stored in one shape, "closed" or
    "10:00 AM - 8:00 PM", so the file stays readable."""
    from .bookings import is_closed, parse_hours_range

    text = (text or "").strip()
    if not text:
        return None, "Every day needs hours or the word closed."
    if is_closed(text):
        return "closed", None
    span = parse_hours_range(text)
    if span is None:
        return None, "Hours have to look like 10:00 AM - 8:00 PM, or closed."
    return "{} - {}".format(format_time(span[0]), format_time(span[1])), None


DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass
class Service:
    name: str
    # A service with no published price (the owner hasn't confirmed one
    # yet) has price None: the bot says the salon will confirm it, and
    # never quotes a number it made up.
    price: int | None = None  # INR
    duration_min: int = 30
    description: str = ""
    # The lowest price the owner will accept when the bot negotiates.
    # None means the price is fixed: the bot never discounts it.
    floor_price: int | None = None  # INR
    # How many customers can be served at once (chairs, doctors, trainers).
    # 1 is one provider; a two-chair salon sets 2.
    capacity: int = 1

    def to_dict(self):
        return {
            "name": self.name,
            "price": self.price,
            "duration_min": self.duration_min,
            "description": self.description,
            "floor_price": self.floor_price,
            "capacity": self.capacity,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


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
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, data):
        services = [Service.from_dict(s) for s in data.get("services", [])]
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

    def to_dict(self):
        return {
            "name": self.name,
            "category": self.category,
            "city": self.city,
            "phone": self.phone,
            "hours": dict(self.hours),
            "services": [service.to_dict() for service in self.services],
            "greeting": self.greeting,
            "address": self.address,
            "offers": self.offers,
        }

    def setup_hours_rows(self):
        """One hours string per day, Monday to Sunday, for the setup
        form. A single daily value fills all seven rows."""
        return [self.hours_today(weekday) for weekday in range(7)]

    def service_named(self, text):
        """Find a service whose name (or a distinctive word of it) appears in text.

        Full names win over single words: with "Hair Styling" and
        "Hair Spa" both on the menu, "hair spa ka price" is about the
        spa, not the styling — the shared word "hair" must not claim it
        just because styling is listed first.
        """
        lowered = text.lower()
        for service in self.services:
            if service.name.lower() in lowered:
                return service
        for service in self.services:
            for word in service.name.lower().split():
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


def validate_setup_services(profile, rows):
    """Rows from the setup form, each (name, price text, duration
    text). Returns (services, error, warnings): the new service list
    in profile order, the first problem in plain words, and warnings
    about prices worth a second look. Nothing is changed when the
    error is set. Names are the match: a row the shop does not have,
    or one missing, refuses the whole save."""
    seen = set()
    by_name = {}
    for name, price_text, duration_text in rows:
        name = (name or "").strip()
        if not name or name in seen:
            return None, "The page sent a service the shop does not have. Load it again and try once more.", []
        seen.add(name)
        price, error = parse_setup_price(price_text)
        if error:
            return None, "{}: {}".format(name, error), []
        duration, error = parse_setup_duration(duration_text)
        if error:
            return None, "{}: {}".format(name, error), []
        by_name[name] = (price, duration)
    if seen != {service.name for service in profile.services}:
        return None, "The page sent a service the shop does not have. Load it again and try once more.", []
    services = []
    warnings = []
    for service in profile.services:
        price, duration = by_name[service.name]
        if (
            service.price is not None
            and price is not None
            and (price > service.price * 3 or price * 3 < service.price)
        ):
            warnings.append(
                "{} changed from Rs {} to Rs {}. Check it is right.".format(
                    service.name, service.price, price
                )
            )
        if (
            service.floor_price is not None
            and price is not None
            and price < service.floor_price
        ):
            warnings.append(
                "{} is now Rs {}, below the lowest price the bot may agree (Rs {}).".format(
                    service.name, price, service.floor_price
                )
            )
        services.append(
            Service(
                name=service.name,
                price=price,
                duration_min=duration,
                description=service.description,
                floor_price=service.floor_price,
                capacity=service.capacity,
            )
        )
    return services, None, warnings


def validate_setup_hours(day_texts):
    """Seven day strings, Monday to Sunday. Returns (hours, error):
    one daily value when all seven agree, else one key per day."""
    if len(day_texts) != 7:
        return None, "Every day needs hours or the word closed."
    values = []
    for text, label in zip(day_texts, DAY_LABELS):
        value, error = parse_setup_day_hours(text)
        if error:
            return None, "{}: {}".format(label, error)
        values.append(value)
    if len(set(values)) == 1:
        return {"daily": values[0]}, None
    return dict(zip(DAY_KEYS, values)), None
