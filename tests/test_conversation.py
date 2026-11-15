"""The bot holds a conversation, reschedules, and remembers names.

There is no rule based state machine any more: the model holds the
conversation and calls a tool when something real has to happen
(book, cancel, reschedule, hand over). These tests script the model
with canned turns and assert the tools ran and the bookings are real.
Times are pinned explicitly (the timezone lesson from v0.3, never
rely on the current hour).
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from test_llm import C, ScriptedLLM, T
from whatsapp_studio.app import Studio
from whatsapp_studio.bookings import IST, BookingStore, india_today
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


class FakeNotifier:
    def __init__(self):
        self.calls = []
        self.booking_calls = []

    def notify(self, business_name, handoff):
        self.calls.append((business_name, handoff))

    def notify_booking(self, business_name, booking):
        self.booking_calls.append((business_name, booking))


class TestConversation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = Studio(
            _profile(),
            LeadStore(Path(self.tmp.name) / "leads.json"),
            bookings=BookingStore(),
            notifier=FakeNotifier(),
            llm=ScriptedLLM([]),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _script(self, *turns):
        self.studio.llm = ScriptedLLM(list(turns))

    def test_book_in_two_messages(self):
        self._script(
            C("Which day and time would you like for your Facial?"),
            T("create_booking", {"service": "Facial", "day": "tomorrow", "time": "11am"}),
            C("Done! Your Facial is booked for tomorrow at 11:00 AM."),
        )
        reply = self.studio.handle_message(
            "919810000001", "book facial", "Rahul"
        )["body"]
        self.assertIn("Which day and time", reply)
        reply = self.studio.handle_message(
            "919810000001", "tomorrow 11am", "Rahul"
        )["body"]
        self.assertIn("booked", reply)
        self.assertIn("11:00 AM", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 1)
        # A cooperating customer is not handed to the owner as a failure.
        self.assertEqual(len(self.studio.handoffs.all_handoffs()), 0)

    def test_slot_list_then_bare_time_books_it(self):
        self._script(
            T("check_availability", {"service": "Haircut", "day": "tomorrow"}),
            C("Free slots tomorrow: 10:00 AM, 10:30 AM, 11:00 AM, 11:30 AM."),
            T("create_booking", {"service": "Haircut", "day": "tomorrow", "time": "11am"}),
            C("Done! Your Haircut is booked for tomorrow at 11:00 AM."),
        )
        reply = self.studio.handle_message(
            "919810000001", "book haircut tomorrow", "Rahul"
        )["body"]
        self.assertIn("Free slots", reply)
        reply = self.studio.handle_message("919810000001", "11am", "Rahul")["body"]
        self.assertIn("booked", reply)
        self.assertIn("11:00 AM", reply)
        booking = self.studio.bookings.all_bookings()[0]
        self.assertEqual(booking["date"], (india_today() + timedelta(days=1)).isoformat())

    def test_which_service_then_service_name_asks_when(self):
        self._script(
            C("Which service would you like?"),
            C("Which day and time would you like for your Haircut?"),
        )
        reply = self.studio.handle_message("919810000001", "i want to book")["body"]
        self.assertIn("Which service", reply)
        reply = self.studio.handle_message("919810000001", "haircut", "Rahul")["body"]
        self.assertIn("Which day and time", reply)

    def test_yes_after_slot_list_books_first_slot(self):
        self._script(
            T("check_availability", {"service": "Haircut", "day": "tomorrow"}),
            C("Free slots tomorrow: 10:00 AM, 10:30 AM, 11:00 AM."),
            T("create_booking", {"service": "Haircut", "day": "tomorrow", "time": "10:00 AM"}),
            C("Done! Your Haircut is booked for tomorrow at 10:00 AM."),
        )
        self.studio.handle_message("919810000001", "book haircut tomorrow", "Rahul")
        reply = self.studio.handle_message("919810000001", "yes", "Rahul")["body"]
        self.assertIn("booked", reply)
        self.assertIn("10:00 AM", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 1)

    def test_fresh_booking_while_waiting_uses_its_own_service_and_day(self):
        # A slot list for haircut tomorrow is pending; a new message
        # naming its own service and day starts over, it does not
        # answer the old question with the old service and day. The
        # tool, not a state machine, resolves it: the message's own
        # service and day win.
        self._script(
            T("check_availability", {"service": "Haircut", "day": "tomorrow"}),
            C("Free slots tomorrow: 10:00 AM, 10:30 AM, 11:00 AM."),
            T("create_booking", {"service": "Facial", "day": "parso", "time": "11 baje"}),
            C("Done! Your Facial is booked for the day after tomorrow at 11:00 AM."),
        )
        self.studio.handle_message("919810000001", "book haircut tomorrow", "Rahul")
        reply = self.studio.handle_message(
            "919810000001", "book facial parso 11 baje", "Rahul"
        )["body"]
        self.assertIn("booked", reply)
        self.assertIn("Facial", reply)
        booking = self.studio.bookings.all_bookings()[0]
        self.assertEqual(booking["service"], "Facial")
        self.assertEqual(
            booking["date"], (india_today() + timedelta(days=2)).isoformat()
        )

    def test_bare_time_with_own_day_after_slot_list(self):
        # "parso 11am" after a slot list for tomorrow books the day
        # after tomorrow, not tomorrow.
        self._script(
            T("check_availability", {"service": "Haircut", "day": "tomorrow"}),
            C("Free slots tomorrow: 10:00 AM, 10:30 AM, 11:00 AM."),
            T("create_booking", {"service": "Haircut", "day": "parso", "time": "11am"}),
            C("Done! Your Haircut is booked for the day after tomorrow at 11:00 AM."),
        )
        self.studio.handle_message("919810000001", "book haircut tomorrow", "Rahul")
        reply = self.studio.handle_message(
            "919810000001", "parso 11am", "Rahul"
        )["body"]
        self.assertIn("booked", reply)
        booking = self.studio.bookings.all_bookings()[0]
        self.assertEqual(
            booking["date"], (india_today() + timedelta(days=2)).isoformat()
        )

    def test_pending_never_overrides_cancel_or_human(self):
        self._script(
            T("create_booking", {"service": "Haircut", "day": "tomorrow", "time": "11am"}),
            C("Done! Your Haircut is booked for tomorrow at 11:00 AM."),
            C("Which day and time would you like for your Facial?"),
            T("cancel_booking", {}),
            C("Your booking is cancelled."),
            C("Which day and time would you like for your Facial?"),
            T("hand_over", {"reason": "asked for a person"}),
            C("I've passed this to the team. They will reply here within 30 minutes."),
        )
        self.studio.handle_message("919810000001", "book haircut tomorrow 11am", "Rahul")
        self.studio.handle_message("919810000001", "book facial", "Rahul")
        reply = self.studio.handle_message(
            "919810000001", "cancel my booking"
        )["body"]
        self.assertIn("cancelled", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 0)
        self.studio.handle_message("919810000001", "book facial", "Rahul")
        self.studio.handle_message(
            "919810000001", "can I talk to a person please", "Rahul"
        )
        self.assertIsNotNone(self.studio.handoffs.open_for("919810000001"))

    def test_parso_books_day_after_tomorrow(self):
        now = datetime(2026, 11, 15, 10, 0, tzinfo=IST)  # a Sunday, 10 AM IST
        self._script(
            T("create_booking", {"service": "Haircut", "day": "parso", "time": "11 baje"}),
            C("Done! Your Haircut is booked for 2026-11-17 at 11:00 AM."),
        )
        reply = self.studio.handle_message(
            "919810000001", "book haircut parso 11 baje", "Rahul", now=now
        )["body"]
        self.assertIn("booked", reply)
        self.assertIn("11:00 AM", reply)
        booking = self.studio.bookings.all_bookings()[0]
        self.assertEqual(booking["date"], "2026-11-17")

    def test_aaj_shaam_books_today_evening(self):
        now = datetime(2026, 11, 15, 10, 0, tzinfo=IST)
        self._script(
            T("create_booking", {"service": "Facial", "day": "aaj", "time": "shaam"}),
            C("Done! Your Facial is booked for today at 6:00 PM."),
        )
        reply = self.studio.handle_message(
            "919810000001", "book facial aaj shaam", "Rahul", now=now
        )["body"]
        self.assertIn("booked", reply)
        self.assertIn("6:00 PM", reply)
        booking = self.studio.bookings.all_bookings()[0]
        self.assertEqual(booking["date"], "2026-11-15")

    def test_subah_with_no_day_is_tomorrow_once_it_has_passed(self):
        # 2 PM IST: this morning's 11 AM is gone, so "subah" is tomorrow's.
        now = datetime(2026, 11, 15, 14, 0, tzinfo=IST)
        self._script(
            T("create_booking", {"service": "Haircut", "time": "subah"}),
            C("Done! Your Haircut is booked for tomorrow at 11:00 AM."),
        )
        reply = self.studio.handle_message(
            "919810000001", "book haircut subah", "Rahul", now=now
        )["body"]
        self.assertIn("booked", reply)
        booking = self.studio.bookings.all_bookings()[0]
        self.assertEqual(booking["date"], "2026-11-16")
        self.assertEqual(booking["start_min"], 11 * 60)

    def test_subah_with_no_day_is_today_when_still_ahead(self):
        now = datetime(2026, 11, 15, 9, 0, tzinfo=IST)
        self._script(
            T("create_booking", {"service": "Haircut", "time": "subah"}),
            C("Done! Your Haircut is booked for today at 11:00 AM."),
        )
        reply = self.studio.handle_message(
            "919810000001", "book haircut subah", "Rahul", now=now
        )["body"]
        self.assertIn("booked", reply)
        booking = self.studio.bookings.all_bookings()[0]
        self.assertEqual(booking["date"], "2026-11-15")
        self.assertEqual(booking["start_min"], 11 * 60)


class TestReschedule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = Studio(
            _profile(),
            LeadStore(Path(self.tmp.name) / "leads.json"),
            bookings=BookingStore(),
            notifier=FakeNotifier(),
            llm=ScriptedLLM([]),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _script(self, *turns):
        self.studio.llm = ScriptedLLM(list(turns))

    def test_reschedule_moves_the_booking(self):
        # The model asks for the new time first; nothing is cancelled
        # until the customer gives it. Then the tool moves the booking.
        self._script(
            T("create_booking", {"service": "Haircut", "day": "tomorrow", "time": "11am"}),
            C("Done! Your Haircut is booked for tomorrow at 11:00 AM."),
            C("Which day and time would you like to move it to?"),
            T("reschedule_booking", {"service": "Haircut", "day": "tomorrow", "time": "4pm"}),
            C("Done! Your Haircut is booked for tomorrow at 4:00 PM instead."),
        )
        self.studio.handle_message(
            "919810000001", "book haircut tomorrow 11am", "Rahul"
        )
        reply = self.studio.handle_message(
            "919810000001", "reschedule my booking"
        )["body"]
        self.assertIn("Which day and time", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 1)
        reply = self.studio.handle_message(
            "919810000001", "tomorrow 4pm", "Rahul"
        )["body"]
        self.assertIn("booked", reply)
        self.assertIn("4:00 PM", reply)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 1)

    def test_reschedule_with_no_booking_says_so(self):
        self._script(
            T("reschedule_booking", {"service": "Haircut", "day": "tomorrow", "time": "4pm"}),
            C("I could not find a booking to move."),
        )
        reply = self.studio.handle_message(
            "919810000001", "reschedule my booking"
        )["body"]
        self.assertIn("could not find a booking to move", reply)


class TestRepeatCustomersAndNames(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = Studio(
            _profile(),
            LeadStore(Path(self.tmp.name) / "leads.json"),
            bookings=BookingStore(),
            notifier=FakeNotifier(),
            llm=ScriptedLLM([]),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _script(self, *turns):
        self.studio.llm = ScriptedLLM(list(turns))

    def test_second_greeting_welcomes_back(self):
        # The model sees the thread, so it knows Rahul has been here
        # before and can welcome him back by name.
        self._script(
            C("Hello Rahul! How can I help you today?"),
            C("Welcome back, Rahul! Good to see you again."),
        )
        first = self.studio.handle_message("919810000001", "hi", "Rahul")["body"]
        self.assertNotIn("Welcome back", first)
        second = self.studio.handle_message("919810000001", "hi", "Rahul")["body"]
        self.assertIn("Welcome back", second)
        self.assertIn("Rahul", second)

    def test_booking_without_a_name_asks_for_it(self):
        self._script(
            T("create_booking", {"service": "Haircut", "day": "tomorrow", "time": "11am"}),
            C("Done! Your Haircut is booked for tomorrow at 11:00 AM. May I have your name?"),
            T("set_customer_name", {"name": "Rahul"}),
            C("Thanks, Rahul! Your booking is all set."),
        )
        reply = self.studio.handle_message(
            "919810000001", "book haircut tomorrow 11am", ""
        )["body"]
        self.assertIn("booked", reply)
        self.assertIn("May I have your name", reply)
        reply = self.studio.handle_message("919810000001", "Rahul", "")["body"]
        self.assertIn("Thanks, Rahul", reply)
        booking = self.studio.bookings.all_bookings()[0]
        self.assertEqual(booking["name"], "Rahul")

    def test_booking_with_a_name_does_not_ask(self):
        self._script(
            T("create_booking", {"service": "Haircut", "day": "tomorrow", "time": "11am"}),
            C("Done! Your Haircut is booked for tomorrow at 11:00 AM."),
        )
        reply = self.studio.handle_message(
            "919810000001", "book haircut tomorrow 11am", "Rahul"
        )["body"]
        self.assertNotIn("May I have your name", reply)


if __name__ == "__main__":
    unittest.main()
