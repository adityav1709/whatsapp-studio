import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from test_llm import C, ScriptedLLM, T
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
        self.assertEqual(parse_time("12 baje"), 12 * 60)
        self.assertEqual(parse_time("aaj shaam"), 18 * 60)
        self.assertEqual(parse_time("subah"), 11 * 60)
        self.assertEqual(parse_time("dopahar"), 15 * 60)
        self.assertIsNone(parse_time("facial ka price kya hai"))
        self.assertIsNone(parse_time("Rs 499"))

    def test_parse_day(self):
        today = date(2026, 11, 14)  # a Saturday
        self.assertEqual(parse_day("today", today), today)
        self.assertEqual(parse_day("aaj", today), today)
        self.assertEqual(parse_day("tomorrow", today), today + timedelta(days=1))
        self.assertEqual(parse_day("kal", today), today + timedelta(days=1))
        self.assertEqual(parse_day("parso", today), today + timedelta(days=2))
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

    def test_corrupt_file_loads_empty(self):
        path = Path(self.tmp.name) / "bookings.json"
        path.write_text("not json at all", encoding="utf-8")
        reloaded = BookingStore(path)
        self.assertEqual(reloaded.all_bookings(), [])

    def test_write_is_always_valid_json(self):
        self.store.book("919810000001", "Rahul", self.haircut, self.day, 660)
        import json

        data = json.loads((Path(self.tmp.name) / "bookings.json").read_text())
        self.assertEqual(len(data), 1)


class TestValidateSlot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = BookingStore(Path(self.tmp.name) / "bookings.json")
        self.haircut = Service("Haircut", 499, 30)
        self.facial = Service("Facial", 1499, 60)
        self.day = date(2026, 11, 15)
        self.hours = "10:00 AM - 8:00 PM"

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_slot(self):
        from whatsapp_studio.bookings import validate_slot

        self.assertTrue(validate_slot(self.day, 660, self.haircut, self.hours))
        self.assertTrue(validate_slot(self.day, 660, self.facial, self.hours))

    def test_before_opening_refused(self):
        from whatsapp_studio.bookings import validate_slot

        self.assertFalse(validate_slot(self.day, 9 * 60, self.haircut, self.hours))
        self.assertFalse(validate_slot(self.day, 3 * 60, self.haircut, self.hours))

    def test_finishing_after_close_refused(self):
        from whatsapp_studio.bookings import validate_slot

        self.assertFalse(validate_slot(self.day, 20 * 60, self.facial, self.hours))
        self.assertTrue(validate_slot(self.day, 19 * 60, self.facial, self.hours))

    def test_off_grid_refused(self):
        from whatsapp_studio.bookings import validate_slot

        self.assertFalse(validate_slot(self.day, 11 * 60 + 15, self.haircut, self.hours))
        self.assertFalse(validate_slot(self.day, 11 * 60 + 30, self.facial, self.hours))

    def test_closed_day_refused(self):
        from whatsapp_studio.bookings import validate_slot

        self.assertFalse(validate_slot(self.day, 660, self.haircut, "closed"))
        self.assertEqual(self.store.free_slots(self.day, self.haircut, "closed"), [])

    def test_capacity_two_takes_two(self):
        two_chair = Service("Haircut", 499, 30, capacity=2)
        self.assertIsNotNone(
            self.store.book("919810000001", "Rahul", two_chair, self.day, 660)
        )
        self.assertIsNotNone(
            self.store.book("919810000002", "Priya", two_chair, self.day, 660)
        )
        self.assertIsNone(
            self.store.book("919810000003", "Asha", two_chair, self.day, 660)
        )

    def test_capacity_two_keeps_slot_listed_after_one(self):
        two_chair = Service("Haircut", 499, 30, capacity=2)
        self.store.book("919810000001", "Rahul", two_chair, self.day, 660)
        self.assertIn(
            660, self.store.free_slots(self.day, two_chair, self.hours)
        )
        self.store.book("919810000002", "Priya", two_chair, self.day, 660)
        self.assertNotIn(
            660, self.store.free_slots(self.day, two_chair, self.hours)
        )


class TestBookingFlow(unittest.TestCase):
    """Booking through the model: it calls the create_booking tool,
    the tool validates the slot in code, and the model's words are
    what the customer reads. The refusal tests script the model
    echoing the tool's refusal, so the assertion that matters is
    the booking count: the tool, not the words, decides."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        profile = _profile()
        self.studio = Studio(
            profile,
            LeadStore(Path(self.tmp.name) / "leads.json"),
            bookings=BookingStore(),
            llm=ScriptedLLM([]),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _script(self, *turns):
        self.studio.llm = ScriptedLLM(list(turns))

    def _book(self, service, day, time, reply_text):
        return [
            T("create_booking", {"service": service, "day": day, "time": time}),
            C(reply_text),
        ]

    def test_book_with_day_and_time(self):
        self._script(*self._book(
            "Haircut", "tomorrow", "11am",
            "Done! Your Haircut is booked for tomorrow at 11:00 AM.",
        ))
        reply = self.studio.handle_message(
            "919810000001", "book haircut tomorrow 11am", "Rahul"
        )["body"]
        self.assertIn("booked", reply)
        self.assertIn("Haircut", reply)
        self.assertIn("11:00 AM", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 1)

    def test_book_same_slot_twice_offers_alternatives(self):
        self._script(
            *self._book(
                "Haircut", "tomorrow", "11am",
                "Done! Your Haircut is booked for tomorrow at 11:00 AM.",
            ),
            *self._book(
                "Haircut", "tomorrow", "11am",
                "Sorry, 11:00 AM tomorrow is already taken. "
                "Free slots nearby: 10:30 AM, 11:30 AM.",
            ),
        )
        self.studio.handle_message("919810000001", "book haircut tomorrow 11am", "Rahul")
        reply = self.studio.handle_message(
            "919810000002", "book haircut tomorrow 11am", "Priya"
        )["body"]
        self.assertIn("already taken", reply)
        self.assertIn("10:30 AM", reply)
        self.assertIn("11:30 AM", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 1)

    def test_book_without_time_lists_slots(self):
        self._script(
            T("check_availability", {"service": "Haircut", "day": "tomorrow"}),
            C("Free slots tomorrow: 10:00 AM, 10:30 AM, 11:00 AM, 11:30 AM."),
        )
        reply = self.studio.handle_message(
            "919810000001", "book haircut tomorrow", "Rahul"
        )["body"]
        self.assertIn("Free slots", reply)
        self.assertIn("10:00 AM", reply)

    def test_book_without_service_asks_which(self):
        self._script(C("Which service would you like?"))
        reply = self.studio.handle_message("919810000001", "i want to book")["body"]
        self.assertIn("Which service", reply)

    def test_3am_booking_refused(self):
        self._script(*self._book(
            "Haircut", "tomorrow", "3am",
            "Sorry, 3:00 AM is outside our hours. We are open 10:00 AM to 8:00 PM.",
        ))
        reply = self.studio.handle_message(
            "919810000001", "book haircut tomorrow 3am", "Rahul"
        )["body"]
        self.assertIn("outside our hours", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 0)

    def test_9am_booking_refused(self):
        self._script(*self._book(
            "Haircut", "tomorrow", "9am",
            "Sorry, 9:00 AM is outside our hours. We open at 10:00 AM.",
        ))
        reply = self.studio.handle_message(
            "919810000001", "book haircut tomorrow 9am", "Rahul"
        )["body"]
        self.assertIn("outside our hours", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 0)

    def test_8pm_facial_refused(self):
        self._script(*self._book(
            "Facial", "tomorrow", "8pm",
            "Sorry, 8:00 PM is outside our hours for a Facial. "
            "We are open 10:00 AM to 8:00 PM.",
        ))
        reply = self.studio.handle_message(
            "919810000001", "book facial tomorrow 8pm", "Rahul"
        )["body"]
        self.assertIn("outside our hours", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 0)

    def test_off_grid_booking_refused(self):
        self._script(*self._book(
            "Haircut", "tomorrow", "11:15am",
            "Sorry, 11:15 AM is not a slot we offer. "
            "Free slots nearby: 11:00 AM, 11:30 AM.",
        ))
        reply = self.studio.handle_message(
            "919810000001", "book haircut tomorrow 11:15am", "Rahul"
        )["body"]
        self.assertIn("not a slot we offer", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 0)

    def test_12_baje_books_noon(self):
        self._script(*self._book(
            "Haircut", "tomorrow", "12 baje",
            "Done! Your Haircut is booked for tomorrow at 12:00 PM.",
        ))
        reply = self.studio.handle_message(
            "919810000001", "book haircut tomorrow 12 baje", "Rahul"
        )["body"]
        self.assertIn("booked", reply)
        self.assertIn("12:00 PM", reply)

    def test_rebooking_your_own_slot_says_so(self):
        self._script(
            *self._book(
                "Haircut", "tomorrow", "11am",
                "Done! Your Haircut is booked for tomorrow at 11:00 AM.",
            ),
            *self._book(
                "Haircut", "tomorrow", "11am",
                "You already have a booking at that time.",
            ),
        )
        self.studio.handle_message("919810000001", "book haircut tomorrow 11am", "Rahul")
        reply = self.studio.handle_message(
            "919810000001", "book haircut tomorrow 11am", "Rahul"
        )["body"]
        self.assertIn("already have a booking", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 1)

    def test_closed_day_says_closed(self):
        self.studio.profile.hours = {"sun": "closed", "daily": "10:00 AM - 8:00 PM"}
        self._script(
            T("check_availability", {"service": "Haircut", "day": "sunday"}),
            C("We are closed on Sunday."),
            *self._book(
                "Haircut", "sunday", "11am",
                "Sorry, we are closed on Sunday.",
            ),
        )
        reply = self.studio.handle_message(
            "919810000001", "book haircut sunday", "Rahul"
        )["body"]
        self.assertIn("closed", reply.lower())
        reply = self.studio.handle_message(
            "919810000001", "book haircut sunday 11am", "Rahul"
        )["body"]
        self.assertIn("closed", reply.lower())
        self.assertEqual(len(self.studio.bookings.all_bookings()), 0)

    def test_cancel_flow(self):
        self._script(
            *self._book(
                "Haircut", "tomorrow", "11am",
                "Done! Your Haircut is booked for tomorrow at 11:00 AM.",
            ),
            T("cancel_booking", {}),
            C("Your booking is cancelled."),
        )
        self.studio.handle_message("919810000001", "book haircut tomorrow 11am", "Rahul")
        reply = self.studio.handle_message(
            "919810000001", "cancel my booking"
        )["body"]
        self.assertIn("cancelled", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 0)

    def test_cancel_with_nothing_booked(self):
        self._script(
            T("cancel_booking", {}),
            C("I could not find a booking to cancel."),
        )
        reply = self.studio.handle_message(
            "919810000001", "cancel my booking"
        )["body"]
        self.assertIn("could not find", reply)

    def test_dashboard_lists_todays_bookings(self):
        from whatsapp_studio.bookings import india_today

        self.studio.bookings.book(
            "919810000001", "Rahul", _profile().services[0], india_today(), 16 * 60
        )
        html = self.studio.dashboard_html("bookings")
        self.assertIn("Bookings today", html)
        self.assertIn("Haircut", html)
        self.assertIn("4:00 PM", html)


if __name__ == "__main__":
    unittest.main()
