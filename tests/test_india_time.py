"""All times are India time, not server time.

The server on Render runs UTC; the shop is in India. Around midnight IST
the two disagree by a whole day. Every test pins an explicit datetime.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from whatsapp_studio.app import Studio
from whatsapp_studio.bookings import IST, BookingStore, india_now, india_today
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.replies import handoff_confirmation


def _profile():
    return BusinessProfile(
        name="Glow & Grace Salon",
        category="salon",
        city="Gurgaon",
        phone="919999999999",
        hours={"daily": "10:00 AM - 8:00 PM"},
        services=[Service("Haircut", 499, 30)],
        address="Sector 50, Gurgaon",
    )


class TestIndiaTime(unittest.TestCase):
    def test_ist_is_fixed_plus_530(self):
        self.assertEqual(india_now().utcoffset(), timedelta(hours=5, minutes=30))
        self.assertEqual(india_today(), india_now().date())

    def test_booking_just_after_midnight_ist_is_the_right_day(self):
        # 00:30 IST on Mon 16 Nov is still 19:00 UTC on Sun 15 Nov — a
        # server-clock "tomorrow" would be the wrong day.
        tmp = tempfile.TemporaryDirectory()
        try:
            studio = Studio(
                _profile(),
                LeadStore(Path(tmp.name) / "leads.json"),
                bookings=BookingStore(),
            )
            now = datetime(2026, 11, 16, 0, 30, tzinfo=IST)
            reply = studio.handle_message(
                "919810000001", "book haircut tomorrow 11am", "Rahul", now=now
            )["body"]
            self.assertIn("booked", reply)
            booking = studio.bookings.all_bookings()[0]
            self.assertEqual(booking["date"], "2026-11-17")
        finally:
            tmp.cleanup()

    def test_booking_late_evening_ist_uses_ist_day(self):
        # 23:30 IST on Sun 15 Nov: "tomorrow" is Mon 16 Nov, not Tue.
        tmp = tempfile.TemporaryDirectory()
        try:
            studio = Studio(
                _profile(),
                LeadStore(Path(tmp.name) / "leads.json"),
                bookings=BookingStore(),
            )
            now = datetime(2026, 11, 15, 23, 30, tzinfo=IST)
            studio.handle_message(
                "919810000001", "book haircut tomorrow 11am", "Rahul", now=now
            )
            booking = studio.bookings.all_bookings()[0]
            self.assertEqual(booking["date"], "2026-11-16")
        finally:
            tmp.cleanup()

    def test_open_hours_check_at_11pm_ist_says_closed(self):
        now = datetime(2026, 11, 15, 23, 0, tzinfo=IST)
        reply = handoff_confirmation(_profile(), "asked for a person", now)
        self.assertIn("closed", reply.lower())
        self.assertNotIn("30 minutes", reply)

    def test_open_hours_check_at_11am_ist_promises_30_minutes(self):
        now = datetime(2026, 11, 15, 11, 0, tzinfo=IST)
        reply = handoff_confirmation(_profile(), "asked for a person", now)
        self.assertIn("30 minutes", reply)


if __name__ == "__main__":
    unittest.main()
