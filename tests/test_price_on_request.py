"""A service with no published price: the bot never quotes a number
nobody set. The get_price tool says the salon confirms the price, the
booking still works at no price, and the dashboard never shows "Rs 0"
or "Rs None" for it.

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
from test_llm import C, ScriptedLLM, T
from whatsapp_studio.app import Studio
from whatsapp_studio.bookings import BookingStore, india_now, india_today
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.leads import LeadStore

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


def _studio(tmp):
    return Studio(
        _profile(),
        LeadStore(Path(tmp) / "leads.json"),
        bookings=BookingStore(),
        notifier=FakeNotifier(),
        llm=ScriptedLLM([]),
    )


class TestPriceOnRequest(unittest.TestCase):
    def test_service_without_a_price_loads_as_none(self):
        service = Service("Hair Spa")
        self.assertIsNone(service.price)
        self.assertEqual(service.duration_min, 30)

    def test_get_price_tool_for_unpriced_service_names_no_number(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        studio = _studio(tmp.name)
        result = studio._run_tool(
            "get_price", {"service": "Hair Spa"}, "919810000001", "Priya",
            india_now(), "hair spa ka price kya hai?",
        )
        self.assertTrue(result["ok"])
        self.assertIsNone(result["price"])
        self.assertEqual(result["note"], "price confirmed at the shop")
        self.assertNotIn("Rs", json.dumps(result))
        self.assertNotIn("None", json.dumps(result))

    def test_get_price_tool_for_priced_service_quotes_it(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        studio = _studio(tmp.name)
        result = studio._run_tool(
            "get_price", {"service": "Haircut"}, "919810000001", "Priya",
            india_now(), "haircut price?",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["price"], 499)

    def test_list_prices_tool_mixes_priced_and_unpriced(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        studio = _studio(tmp.name)
        result = studio._run_tool(
            "list_prices", {}, "919810000001", "Priya",
            india_now(), "price list?",
        )
        self.assertTrue(result["ok"])
        by_name = {s["name"]: s for s in result["services"]}
        self.assertEqual(by_name["Haircut"]["price"], 499)
        self.assertIsNone(by_name["Hair Spa"]["price"])
        # The price field is a real null, never the string "None".
        self.assertNotIn('"None"', json.dumps(result))

    def test_create_booking_tool_books_unpriced_service_at_no_price(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        studio = _studio(tmp.name)
        result = studio._run_tool(
            "create_booking",
            {"service": "Hair Spa", "day": "tomorrow", "time": "11am"},
            "919810000001", "Priya", india_now(), "book hair spa tomorrow 11am",
        )
        self.assertTrue(result["ok"])
        self.assertIsNone(result["booking"]["price"])
        self.assertEqual(len(studio.bookings.all_bookings()), 1)

    def test_booking_an_unpriced_service_end_to_end(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        studio = _studio(tmp.name)
        studio.llm = ScriptedLLM(
            [
                T("get_price", {"service": "Hair Spa"}),
                C("The salon confirms it for Hair Spa. Shall I book it for you?"),
                T(
                    "create_booking",
                    {"service": "Hair Spa", "day": "tomorrow", "time": "11am"},
                ),
                C(
                    "Done! Your Hair Spa is booked for tomorrow at 11:00 AM, "
                    "price confirmed at the salon."
                ),
            ]
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
        html = studio.dashboard_html("bookings")
        self.assertIn("price to confirm", html)
        self.assertNotIn("Rs None", html)
        self.assertNotIn("Rs 0", html)

    def test_dashboard_week_line_without_priced_bookings(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        studio = _studio(tmp.name)
        studio.bookings.book(
            "919810000001", "Priya", Service("Hair Spa", None, 45),
            india_today(), 11 * 60,
        )
        html = studio.dashboard_html("bookings")
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
            llm=ScriptedLLM(
                [
                    T(
                        "create_booking",
                        {"service": "Shampoo", "day": "tomorrow", "time": "11am"},
                    ),
                    C(
                        "Done! Your Shampoo is booked for tomorrow at 11:00 AM "
                        "at Snips Salon, price confirmed at the salon."
                    ),
                ]
            ),
        )
        reply = studio.handle_message(
            "919810000002", "book shampoo tomorrow 11am", "Asha"
        )["body"]
        self.assertIn("booked", reply)
        self.assertIn("Snips Salon", reply)
        self.assertIn("price confirmed at the salon", reply)


if __name__ == "__main__":
    unittest.main()
