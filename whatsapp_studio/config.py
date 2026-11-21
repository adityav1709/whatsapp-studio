"""Business profile configuration for WhatsApp Studio.

A profile is plain JSON so a non-technical business owner (or the
onboarding flow) can edit it without touching code.
"""
from __future__ import annotations

import json
import re
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
    # True when the price genuinely varies (bridal makeup, hair
    # extensions): the bot says the shop confirms it, and says it
    # varies, instead of sounding like it forgot the price.
    price_varies: bool = False

    def to_dict(self):
        return {
            "name": self.name,
            "price": self.price,
            "duration_min": self.duration_min,
            "description": self.description,
            "floor_price": self.floor_price,
            "capacity": self.capacity,
            "price_varies": self.price_varies,
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


ONBOARDING_CATEGORIES = ("salon", "clinic", "gym", "other")
ONBOARDING_DEFAULT_SERVICE_ROWS = 2
ONBOARDING_SERVICE_ROWS = 10
ONBOARDING_MAX_SERVICES = 20


def normalize_onboarding_phone(text):
    """Digits only Indian mobile for the onboarding form, or None.
    Strips spaces, dashes and a leading +91 / 91 / 0, then requires
    10 digits starting 6 to 9. Anything else is not a number the bot
    can be switched on for."""
    digits = re.sub(r"\D", "", text or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "6789":
        return None
    return digits


def validate_onboarding_profile(fields):
    """Fields from the onboarding form (a parse_qs dict). Returns
    (profile, errors): the profile in BusinessProfile.to_dict shape
    when every check passes, else None and errors keyed by field,
    each a plain sentence for the box it belongs to. The owner's
    name rides along as owner_name in the file, so Aditya knows who
    he is calling, and is dropped when the profile is loaded. Pure:
    nothing here touches the disk or the live profile."""
    errors = {}

    def one(key):
        return (fields.get(key) or [""])[0].strip()

    def too_long(value, limit):
        return len(value) > limit

    name = one("name")
    if not name:
        errors["name"] = "Tell us the name of your shop."
    elif too_long(name, 100):
        errors["name"] = "That name is longer than 100 letters. Use a shorter one."
    category = one("category").lower()
    if category not in ONBOARDING_CATEGORIES:
        errors["category"] = "Pick what kind of shop it is."
    city = one("city")
    if too_long(city, 100):
        errors["city"] = "That city is longer than 100 letters. Use a shorter one."
    address = one("address")
    if too_long(address, 200):
        errors["address"] = "That address is longer than 200 letters. Use a shorter one."
    owner_name = one("owner_name")
    if too_long(owner_name, 100):
        errors["owner_name"] = "That name is longer than 100 letters. Use a shorter one."
    phone = normalize_onboarding_phone(one("phone"))
    if phone is None:
        errors["phone"] = (
            "That does not look like an Indian mobile number. "
            "It should be 10 digits, like 98991 27791."
        )
    elif normalize_onboarding_phone(one("phone_confirm")) != phone:
        errors["phone_confirm"] = (
            "The two numbers do not match. Type the same number twice."
        )

    hours = {}
    open_text = one("open")
    close_text = one("close")
    clock = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
    if not clock.match(open_text) or not clock.match(close_text):
        errors["hours"] = "Tell us when the shop opens and closes."
    else:
        open_min = int(open_text[:2]) * 60 + int(open_text[3:])
        close_min = int(close_text[:2]) * 60 + int(close_text[3:])
        if close_min <= open_min:
            errors["hours"] = "The closing time has to be after the opening time."
        else:
            closed = set(fields.get("closed") or [])
            day_texts = [
                "closed" if key in closed else "{} - {}".format(open_text, close_text)
                for key in DAY_KEYS
            ]
            if all(text == "closed" for text in day_texts):
                errors["hours"] = (
                    "The shop is closed every day. Leave at least one day open."
                )
            else:
                hours, error = validate_setup_hours(day_texts)
                if error:
                    errors["hours"] = error

    names = fields.get("service") or []
    prices = fields.get("price") or []
    durations = fields.get("duration") or []
    # A lowest price only arrives when the form renders that box; an
    # older page without it is the same as every lowest left blank.
    lowest_texts = fields.get("lowest")
    if lowest_texts is None:
        lowest_texts = [""] * len(names)
    if not (len(names) == len(prices) == len(durations) == len(lowest_texts)):
        errors["services"] = (
            "The page sent a half filled form. Load it again and try once more."
        )
        services = []
    elif len(names) > ONBOARDING_MAX_SERVICES:
        errors["services"] = (
            "That is a lot of services. List the first 20 and add the rest later."
        )
        services = []
    else:
        varies = set()
        for raw in fields.get("varies") or []:
            try:
                varies.add(int(raw))
            except ValueError:
                pass
        services = []
        seen = set()
        for index, (name_text, price_text, duration_text, lowest_text) in enumerate(
            zip(names, prices, durations, lowest_texts)
        ):
            service_name = (name_text or "").strip()
            # A wholly blank row is not a service: the form starts
            # with two rows and the owner adds more. Duration always arrives
            # (the select pre-selects 30), so it never marks a row used.
            if (
                not service_name
                and not (price_text or "").strip()
                and not (lowest_text or "").strip()
                and index not in varies
            ):
                continue
            key = "service_{}".format(index)
            if not service_name:
                errors[key] = (
                    "Service {} needs a name. Fill it in or leave the "
                    "whole row blank.".format(index + 1)
                )
                continue
            if too_long(service_name, 100):
                errors[key] = (
                    "That service name is longer than 100 letters. "
                    "Use a shorter one."
                )
                continue
            if service_name.lower() in seen:
                errors[key] = "You listed {} twice. Keep it once.".format(service_name)
                continue
            seen.add(service_name.lower())
            price, error = parse_setup_price(price_text)
            if error:
                errors[key] = "{}: {}".format(service_name, error)
                continue
            duration, error = parse_setup_duration(duration_text)
            if error:
                errors[key] = "{}: {}".format(service_name, error)
                continue
            # The lowest price only counts when the price varies: that
            # is when the box shows, and when the bot may negotiate.
            # A fixed price never drops, so a lowest typed against one
            # is ignored. With no listed price it is the floor the bot
            # may negotiate down to; with one it cannot sit above it.
            floor = None
            if index in varies:
                floor, error = parse_setup_price(lowest_text)
                if error:
                    errors[key] = "{}: {}".format(service_name, error)
                    continue
                if floor is not None and price is not None and floor > price:
                    errors[key] = (
                        "{}: the lowest price cannot be above the listed "
                        "price.".format(service_name)
                    )
                    continue
            services.append(
                Service(
                    name=service_name,
                    price=price,
                    duration_min=duration,
                    floor_price=floor,
                    price_varies=index in varies,
                )
            )
        if not services and "services" not in errors:
            errors["services"] = (
                "List at least one service, so the bot has something "
                "to answer about."
            )

    if errors:
        return None, errors
    profile = BusinessProfile(
        name=name,
        category=category,
        city=city,
        phone=phone,
        hours=hours,
        services=services,
        greeting="Hello! Welcome to {}.".format(name),
        address=address,
        offers="",
    ).to_dict()
    if owner_name:
        profile["owner_name"] = owner_name
    return profile, {}
