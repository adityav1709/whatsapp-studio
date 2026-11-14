import unittest

from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.replies import build_reply


def make_profile():
    return BusinessProfile(
        name="Glow & Grace Salon",
        category="salon",
        city="Gurgaon",
        phone="919999999999",
        hours={"daily": "10:00 AM - 8:00 PM"},
        services=[],
        address="Sector 50, Gurgaon",
    )


class TestReplies(unittest.TestCase):
    def test_greeting_names_business(self):
        reply = build_reply(make_profile(), "greeting", "hi")
        self.assertIn("Glow & Grace Salon", reply)

    def test_hours_reply(self):
        reply = build_reply(make_profile(), "hours", "timings?")
        self.assertIn("10:00 AM - 8:00 PM", reply)

    def test_price_list_when_no_service(self):
        profile = make_profile()
        profile.services = [Service(name="Haircut", price=499), Service(name="Facial", price=1499)]
        reply = build_reply(profile, "price", "price list?")
        self.assertIn("Haircut: Rs 499", reply)
        self.assertIn("Facial: Rs 1499", reply)

    def test_price_reply_with_service(self):
        profile = make_profile()
        service = Service(name="Facial", price=1499, duration_min=60)
        reply = build_reply(profile, "price", "facial price?", service=service)
        self.assertIn("Rs 1499", reply)

    def test_booking_asks_for_time(self):
        reply = build_reply(make_profile(), "booking", "book karna hai")
        self.assertIn("book", reply.lower())

    def test_location_reply(self):
        reply = build_reply(make_profile(), "location", "kahan ho")
        self.assertIn("Sector 50", reply)

    def test_fallback_is_helpful(self):
        reply = build_reply(make_profile(), "fallback", "asdfgh")
        self.assertIn("prices", reply)

    def test_greeting_says_it_is_the_assistant(self):
        reply = build_reply(make_profile(), "greeting", "hi")
        self.assertIn("assistant", reply.lower())

    def test_greeting_does_not_repeat_assistant(self):
        profile = make_profile()
        profile.greeting = "Hello! I'm the assistant here."
        reply = build_reply(profile, "greeting", "hi")
        self.assertEqual(reply.lower().count("assistant"), 1)

    def test_welcome_back_greeting(self):
        reply = build_reply(
            make_profile(), "greeting", "hi", returning=True, customer_name="Rahul"
        )
        self.assertIn("Welcome back", reply)
        self.assertIn("Rahul", reply)

    def test_offer_reply_quotes_offers(self):
        profile = make_profile()
        profile.offers = "20% off on all facials this week"
        reply = build_reply(profile, "offer", "any offers?")
        self.assertIn("20% off on all facials", reply)

    def test_offer_reply_when_none(self):
        reply = build_reply(make_profile(), "offer", "any offers?")
        self.assertIn("no current offers", reply)

    def test_closed_day_handoff_does_not_promise_morning_reply(self):
        from datetime import datetime, timezone, timedelta

        from whatsapp_studio.replies import handoff_confirmation

        ist = timezone(timedelta(hours=5, minutes=30))
        profile = make_profile()
        profile.hours = {"sun": "closed", "daily": "10:00 AM - 8:00 PM"}
        # Sunday 15 Nov 2026, 11 PM IST — the shop is shut that day.
        reply = handoff_confirmation(
            profile, "asked for a person",
            datetime(2026, 11, 15, 23, 0, tzinfo=ist),
        )
        self.assertIn("closed", reply.lower())
        self.assertNotIn("11:00 AM", reply)


if __name__ == "__main__":
    unittest.main()
