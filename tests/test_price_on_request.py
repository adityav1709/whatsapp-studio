"""A service with no published price: the bot never quotes a number
nobody set. It says the salon confirms the price, the booking still
works, and the dashboard never shows "Rs 0" or "Rs None" for it.

This is what makes a pilot profile for a real business honest: Snips
Salon publishes no prices anywhere public, so its profile leaves every
price blank until the owner confirms one.
"""
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import run as run_module
from whatsapp_studio.app import Studio
from whatsapp_studio.bookings import BookingStore, india_today
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.intents import PRICE
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.replies import (
    booking_ask_when,
    booking_confirmed,
    build_reply,
)

BASE = Path(__file__).resolve().parent.parent


def _profile():
    return BusinessProfile(
        name="Glow & Grace Salon",
        category="salon",
        city="Gurgaon",
        phone="919999999999",
        hours={"daily": "10:00 AM - 8:00 PM"},
        services=[
            Service("Haircut", 499, 30),
            Service("Hair Spa", None, 30),
        ],
        address="Sector 50, Gurgaon",
    )


class FakeNotifier:
    def __init__(self):
        self.booking_calls = []

    def notify(self, business_name, handoff):
        pass

    def notify_booking(self, business_name, booking):
        self.booking_calls.append((business_name, booking))


class TestPriceOnRequest(unittest.TestCase):
    def test_service_without_a_price_loads_as_none(self):
        service = Service("Hair Spa")
        self.assertIsNone(service.price)
        self.assertEqual(service.duration_min, 30)

    def test_price_question_for_unpriced_service_names_no_number(self):
        profile = _profile()
        reply = build_reply(profile, PRICE, "hair spa ka price kya hai?",
                            service=profile.services[1])
        self.assertIn("confirms it", reply)
        self.assertNotIn("Rs", reply)
        self.assertNotIn("None", reply)

    def test_price_question_for_priced_service_still_quotes_it(self):
        profile = _profile()
        reply = build_reply(profile, PRICE, "haircut price?",
                            service=profile.services[0])
        self.assertIn("Rs 499", reply)

    def test_price_list_mixes_priced_and_unpriced(self):
        profile = _profile()
        reply = build_reply(profile, PRICE, "price list?")
        self.assertIn("- Haircut: Rs 499", reply)
        self.assertIn("- Hair Spa: price confirmed at the salon", reply)
        self.assertNotIn("None", reply)

    def test_booking_ask_when_names_no_price_for_unpriced_service(self):
        reply = booking_ask_when(_profile(), Service("Hair Spa", None, 45))
        self.assertIn("price confirmed at the salon", reply)
        self.assertNotIn("Rs", reply)

    def test_booking_confirmed_with_no_price(self):
        booking = {
            "service": "Hair Spa",
            "price": None,
            "date": (india_today() + timedelta(days=1)).isoformat(),
            "start_min": 11 * 60,
            "duration_min": 45,
        }
        reply = booking_confirmed(_profile(), booking)
        self.assertIn("booked", reply)
        self.assertIn("price confirmed at the salon", reply)
        self.assertNotIn("Rs None", reply)

    def test_booking_an_unpriced_service_end_to_end(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        studio = Studio(
            _profile(),
            LeadStore(Path(tmp.name) / "leads.json"),
            bookings=BookingStore(),
            notifier=FakeNotifier(),
        )
        reply = studio.handle_message(
            "919810000001", "hair spa ka price kya hai?"
        )["body"]
        self.assertIn("confirms it", reply)
        reply = studio.handle_message(
            "919810000001", "book hair spa tomorrow 11am", "Priya"
        )["body"]
        self.assertIn("booked", reply)
        self.assertIn("price confirmed at the salon", reply)
        booking = studio.bookings.all_bookings()[0]
        self.assertIsNone(booking["price"])
        html = studio.dashboard_html()
        self.assertIn("price to confirm", html)
        self.assertNotIn("Rs None", html)
        self.assertNotIn("Rs 0", html)

    def test_dashboard_week_line_without_priced_bookings(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        studio = Studio(
            _profile(),
            LeadStore(Path(tmp.name) / "leads.json"),
            bookings=BookingStore(),
            notifier=FakeNotifier(),
        )
        studio.bookings.book(
            "919810000001", "Priya", Service("Hair Spa", None, 45),
            india_today(), 11 * 60,
        )
        html = studio.dashboard_html()
        self.assertIn("This week: 1 bookings.", html)
        self.assertNotIn("Rs 0", html)


class TestSnipsPilotProfile(unittest.TestCase):
    def test_snips_profile_loads_and_only_carries_verified_facts(self):
        path = BASE / "profiles" / "snips-salon-gurgaon.json"
        with mock.patch.dict(os.environ, {"PROFILE_PATH": str(path)}):
            profile = run_module.load_profile()
        self.assertEqual(profile.name, "Snips Salon")
        self.assertEqual(profile.phone, "919899127791")
        self.assertEqual(profile.city, "Gurgaon")
        self.assertIn("Sector 50", profile.address)
        self.assertEqual(profile.hours_today(), "10:00 AM - 8:00 PM")
        names = [s.name for s in profile.services]
        self.assertIn("Hair Spa", names)
        self.assertIn("HD Makeup", names)
        # No published prices anywhere public: every price waits for
        # the owner, and the file says so in its draft note.
        self.assertTrue(all(s.price is None for s in profile.services))
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["_draft"]["status"].split(" — ")[0], "draft")

    def test_full_service_name_beats_a_shared_word(self):
        # "Hair Styling" is listed before "Hair Spa" and shares the word
        # "hair" — a price question about the spa must still find the spa.
        profile = BusinessProfile.from_json(
            BASE / "profiles" / "snips-salon-gurgaon.json"
        )
        self.assertEqual(
            profile.service_named("hair spa ka price kya hai?").name, "Hair Spa"
        )
        self.assertEqual(
            profile.service_named("hair styling").name, "Hair Styling"
        )

    def test_snips_customer_books_a_service_with_no_price(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        profile = BusinessProfile.from_json(
            BASE / "profiles" / "snips-salon-gurgaon.json"
        )
        studio = Studio(
            profile,
            LeadStore(Path(tmp.name) / "leads.json"),
            bookings=BookingStore(),
            notifier=FakeNotifier(),
        )
        reply = studio.handle_message(
            "919810000002", "book shampoo tomorrow 11am", "Asha"
        )["body"]
        self.assertIn("booked", reply)
        self.assertIn("Snips Salon", reply)
        self.assertIn("price confirmed at the salon", reply)


if __name__ == "__main__":
    unittest.main()
