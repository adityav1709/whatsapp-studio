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


if __name__ == "__main__":
    unittest.main()
