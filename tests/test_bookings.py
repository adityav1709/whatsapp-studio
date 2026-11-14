import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from whatsapp_studio.app import Studio
from whatsapp_studio.bookings import (
    BookingStore,
    format_time,
    parse_day,
    parse_hours_range,
    parse_time,
)
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.leads import LeadStore


def _profile():
    return BusinessProfile(
        name="Glow & Grace Salon",
        category="salon",
        city="Gurgaon",
        phone="919999999999",
        hours={"daily": "10:00 AM - 8:00 PM"},
        services=[
            Service("Haircut", 499, 30),
            Service("Facial", 1499, 60),
        ],
        address="Sector 50, Gurgaon",
    )


class TestParsing(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(parse_time("book haircut tomorrow 11am"), 11 * 60)
        self.assertEqual(parse_time("4:30 pm please"), 16 * 60 + 30)
        self.assertEqual(parse_time("16:00"), 16 * 60)
        self.assertEqual(parse_time("12pm"), 12 * 60)
        self.assertEqual(parse_time("12am"), 0)
        self.assertEqual(parse_time("kal 11 baje"), 11 * 60)
        self.assertEqual(parse_time("4 baje"), 16 * 60)
        self.assertIsNone(parse_time("facial ka price kya hai"))
        self.assertIsNone(parse_time("Rs 499"))

    def test_parse_day(self):
        today = date(2026, 11, 14)  # a Saturday
        self.assertEqual(parse_day("today", today), today)
        self.assertEqual(parse_day("aaj", today), today)
        self.assertEqual(parse_day("tomorrow", today), today + timedelta(days=1))
        self.assertEqual(parse_day("kal", today), today + timedelta(days=1))
        self.assertEqual(parse_day("book monday", today), date(2026, 11, 16))
        self.assertEqual(parse_day("saturday", today), today)
        self.assertEqual(parse_day("15 nov", today), date(2026, 11, 15))
        self.assertEqual(parse_day("15/11", today), date(2026, 11, 15))
        self.assertIsNone(parse_day("book haircut"))

    def test_parse_hours_range(self):
        self.assertEqual(parse_hours_range("10:00 AM - 8:00 PM"), (600, 1200))
        self.assertEqual(parse_hours_range("9:00 AM - 6:30 PM"), (540, 1110))
        self.assertIsNone(parse_hours_range("10 to 8"))

    def test_format_time(self):
        self.assertEqual(format_time(660), "11:00 AM")
        self.assertEqual(format_time(720), "12:00 PM")
        self.assertEqual(format_time(0), "12:00 AM")


class TestBookingStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = BookingStore(Path(self.tmp.name) / "bookings.json")
        self.haircut = Service("Haircut", 499, 30)
        self.day = date(2026, 11, 15)

    def tearDown(self):
        self.tmp.cleanup()

    def test_free_slots_from_hours(self):
        slots = self.store.free_slots(self.day, self.haircut, "10:00 AM - 8:00 PM")
        self.assertEqual(len(slots), 20)
        self.assertEqual(slots[0], 600)
        self.assertEqual(slots[-1], 1170)

    def test_book_then_slot_disappears(self):
        booked = self.store.book("919810000001", "Rahul", self.haircut, self.day, 660)
        self.assertIsNotNone(booked)
        slots = self.store.free_slots(self.day, self.haircut, "10:00 AM - 8:00 PM")
        self.assertNotIn(660, slots)
        self.assertEqual(len(slots), 19)

    def test_double_booking_rejected(self):
        self.store.book("919810000001", "Rahul", self.haircut, self.day, 660)
        clash = self.store.book("919810000002", "Priya", self.haircut, self.day, 660)
        self.assertIsNone(clash)

    def test_overlap_blocked_but_adjacent_ok(self):
        facial = Service("Facial", 1499, 60)
        self.store.book("919810000001", "Rahul", facial, self.day, 660)
        self.assertIsNone(
            self.store.book("919810000002", "Priya", self.haircut, self.day, 690)
        )
        self.assertIsNotNone(
            self.store.book("919810000002", "Priya", self.haircut, self.day, 720)
        )

    def test_cancel_latest(self):
        self.store.book("919810000001", "Rahul", self.haircut, self.day, 660)
        self.store.book("919810000001", "Rahul", self.haircut, self.day, 720)
        cancelled = self.store.cancel_latest("919810000001")
        self.assertEqual(cancelled["start_min"], 720)
        self.assertIsNotNone(self.store.book("919810000002", "Priya", self.haircut, self.day, 720))
        self.assertIsNone(self.store.cancel_latest("919810000099"))

    def test_persists_to_disk(self):
        self.store.book("919810000001", "Rahul", self.haircut, self.day, 660)
        reloaded = BookingStore(Path(self.tmp.name) / "bookings.json")
        self.assertEqual(len(reloaded.bookings_on(self.day)), 1)


class TestBookingFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        profile = _profile()
        self.studio = Studio(
            profile,
            LeadStore(Path(self.tmp.name) / "leads.json"),
            bookings=BookingStore(),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_book_with_day_and_time(self):
        reply = self.studio.handle_message(
            "919810000001", "book haircut tomorrow 11am", "Rahul"
        )["body"]
        self.assertIn("booked", reply)
        self.assertIn("Haircut", reply)
        self.assertIn("11:00 AM", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 1)

    def test_book_same_slot_twice_offers_alternatives(self):
        self.studio.handle_message("919810000001", "book haircut tomorrow 11am", "Rahul")
        reply = self.studio.handle_message(
            "919810000002", "book haircut tomorrow 11am", "Priya"
        )["body"]
        self.assertIn("already taken", reply)
        self.assertIn("10:30 AM", reply)
        self.assertIn("11:30 AM", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 1)

    def test_book_without_time_lists_slots(self):
        reply = self.studio.handle_message(
            "919810000001", "book haircut tomorrow", "Rahul"
        )["body"]
        self.assertIn("Free slots", reply)
        self.assertIn("10:00 AM", reply)

    def test_book_without_service_asks_which(self):
        reply = self.studio.handle_message("919810000001", "i want to book")["body"]
        self.assertIn("Which service", reply)

    def test_cancel_flow(self):
        self.studio.handle_message("919810000001", "book haircut tomorrow 11am", "Rahul")
        reply = self.studio.handle_message(
            "919810000001", "cancel my booking"
        )["body"]
        self.assertIn("cancelled", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 0)

    def test_cancel_with_nothing_booked(self):
        reply = self.studio.handle_message(
            "919810000001", "cancel my booking"
        )["body"]
        self.assertIn("could not find", reply)

    def test_dashboard_lists_todays_bookings(self):
        from datetime import date as _date

        self.studio.bookings.book(
            "919810000001", "Rahul", _profile().services[0], _date.today(), 16 * 60
        )
        html = self.studio.dashboard_html()
        self.assertIn("Bookings today", html)
        self.assertIn("Haircut", html)
        self.assertIn("4:00 PM", html)


if __name__ == "__main__":
    unittest.main()
