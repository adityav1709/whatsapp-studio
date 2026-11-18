"""The first booking win callout: one quiet gold line in the hero on
the Today tab, under the money, for the first booking the bot made
today, with a bigger version once ever for the shop's first booking.

The follow up from the dashboard redesign plan. The win is defined by
when the booking was made (created_ts, India time), never by the
appointment date: a booking made today for tomorrow is today's win,
one made yesterday for today is not. The callout is computed from the
bookings on every render, so a cancelled booking takes its win with
it, and it never appears in demo mode or dry run.
"""
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from test_llm import FixedLLM
from whatsapp_studio.app import Studio
from whatsapp_studio.bookings import (
    BookingStore,
    IST,
    india_today,
)
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.missed import MissedStore
from whatsapp_studio.send import WhatsAppSender


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


def _studio(tmp, demo_mode=False, dry_run=False, profile=None):
    return Studio(
        profile or _profile(),
        LeadStore(Path(tmp) / "leads.json"),
        bookings=BookingStore(),
        handoffs=HandoffStore(),
        missed=MissedStore(),
        llm=FixedLLM("We are open 10:00 AM to 8:00 PM."),
        sender=WhatsAppSender("tok", "pid"),
        demo_mode=demo_mode,
        dry_run=dry_run,
    )


def _epoch(day, hour=11, minute=0):
    """Epoch seconds for a clock time on a day, in India time."""
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).timestamp())


def _book(studio, phone, name, service_name, day, start, created=None):
    service = next(s for s in _profile().services if s.name == service_name)
    booking = studio.bookings.book(phone, name, service, day, start)
    assert booking is not None, "slot was not free"
    if created is not None:
        booking["created_ts"] = created
    return booking


def _win_text(page):
    match = re.search(r"<p class='win'>(.*?)</p>", page, re.S)
    assert match is not None, "no win line on the page"
    return re.sub(r"<[^>]+>", "", match.group(1))


class TestFirstWin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = _studio(self.tmp.name)
        self.today = india_today()

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_bookings_returns_none(self):
        self.assertIsNone(self.studio.bookings.first_win())

    def test_first_booking_ever(self):
        booking = _book(
            self.studio, "919999900001", "Priya", "Haircut", self.today, 660
        )
        win = self.studio.bookings.first_win()
        self.assertEqual(win["booking"], booking)
        self.assertEqual(win["kind"], "first ever")

    def test_second_booking_today_is_first_today(self):
        yesterday = self.today - timedelta(days=1)
        _book(
            self.studio, "919999900001", "Rahul", "Haircut",
            yesterday, 660, created=_epoch(yesterday),
        )
        booking = _book(
            self.studio, "919999900002", "Priya", "Haircut", self.today, 660
        )
        win = self.studio.bookings.first_win()
        self.assertEqual(win["booking"], booking)
        self.assertEqual(win["kind"], "first today")

    def test_booking_made_yesterday_for_today_is_not_a_win(self):
        yesterday = self.today - timedelta(days=1)
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(yesterday),
        )
        self.assertIsNone(self.studio.bookings.first_win())

    def test_booking_made_today_for_tomorrow_is_a_win(self):
        tomorrow = self.today + timedelta(days=1)
        booking = _book(
            self.studio, "919999900001", "Priya", "Haircut", tomorrow, 660
        )
        win = self.studio.bookings.first_win()
        self.assertEqual(win["booking"], booking)
        self.assertEqual(win["kind"], "first ever")

    def test_earliest_created_wins_not_earliest_appointment(self):
        first = _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 720, created=_epoch(self.today, 9, 0),
        )
        _book(
            self.studio, "919999900002", "Asha", "Facial",
            self.today, 600, created=_epoch(self.today, 10, 0),
        )
        win = self.studio.bookings.first_win()
        self.assertEqual(win["booking"], first)

    def test_record_without_created_ts_is_ignored(self):
        self.studio.bookings._bookings.append(
            {
                "phone": "919999900009",
                "name": "Ghost",
                "service": "Haircut",
                "price": 499,
                "date": self.today.isoformat(),
                "start_min": 600,
                "duration_min": 30,
            }
        )
        self.assertIsNone(self.studio.bookings.first_win())
        booking = _book(
            self.studio, "919999900001", "Priya", "Haircut", self.today, 660
        )
        self.assertEqual(
            self.studio.bookings.first_win()["booking"], booking
        )

    def test_midnight_boundary_is_india_time(self):
        yesterday = self.today - timedelta(days=1)
        _book(
            self.studio, "919999900001", "Rahul", "Haircut",
            self.today, 600, created=_epoch(yesterday, 23, 59),
        )
        self.assertIsNone(self.studio.bookings.first_win())
        booking = _book(
            self.studio, "919999900002", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.today, 0, 1),
        )
        self.assertEqual(
            self.studio.bookings.first_win()["booking"], booking
        )

    def test_cancelled_first_booking_passes_the_win_on(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.today, 9, 0),
        )
        second = _book(
            self.studio, "919999900002", "Asha", "Facial",
            self.today, 600, created=_epoch(self.today, 10, 0),
        )
        self.studio.bookings.cancel_latest("919999900001")
        win = self.studio.bookings.first_win()
        self.assertEqual(win["booking"], second)
        # With the first booking gone, the second is now the earliest
        # booking in the whole store, so it is the first ever.
        self.assertEqual(win["kind"], "first ever")


class TestFirstWinDashboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = _studio(self.tmp.name)
        self.today = india_today()

    def tearDown(self):
        self.tmp.cleanup()

    def test_dashboard_shows_first_booking_ever(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut", self.today, 660
        )
        page = self.studio.dashboard_html("today")
        text = _win_text(page)
        self.assertEqual(
            text,
            "Your first booking ever. Priya booked a haircut at 11:00 AM "
            "for Rs 499. The bot made that happen.",
        )

    def test_dashboard_shows_first_booking_today(self):
        yesterday = self.today - timedelta(days=1)
        _book(
            self.studio, "919999900001", "Rahul", "Haircut",
            yesterday, 660, created=_epoch(yesterday),
        )
        _book(
            self.studio, "919999900002", "Priya", "Haircut", self.today, 660
        )
        text = _win_text(self.studio.dashboard_html("today"))
        self.assertEqual(
            text,
            "First booking today. Priya booked a haircut at 11:00 AM "
            "for Rs 499.",
        )

    def test_dashboard_names_tomorrow_for_a_future_appointment(self):
        tomorrow = self.today + timedelta(days=1)
        _book(
            self.studio, "919999900001", "Priya", "Haircut", tomorrow, 660
        )
        text = _win_text(self.studio.dashboard_html("today"))
        self.assertEqual(
            text,
            "Your first booking ever. Priya booked a haircut for "
            "tomorrow at 11:00 AM for Rs 499. The bot made that happen.",
        )

    def test_dashboard_hides_win_with_no_booking_made_today(self):
        yesterday = self.today - timedelta(days=1)
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(yesterday),
        )
        page = self.studio.dashboard_html("today")
        self.assertNotIn("class='win'", page)
        self.assertNotIn("First booking", page)

    def test_dashboard_hides_win_in_demo_mode(self):
        studio = _studio(self.tmp.name, demo_mode=True)
        page = studio.dashboard_html("today")
        self.assertNotIn("class='win'", page)
        self.assertNotIn("First booking", page)

    def test_dashboard_hides_win_in_dry_run(self):
        studio = _studio(self.tmp.name, dry_run=True)
        _book(
            studio, "919999900001", "Priya", "Haircut", self.today, 660
        )
        page = studio.dashboard_html("today")
        self.assertNotIn("class='win'", page)
        self.assertNotIn("First booking", page)

    def test_dashboard_shows_win_only_on_the_today_tab(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut", self.today, 660
        )
        for tab in ("bookings", "customers", "replies", "conversation"):
            self.assertNotIn(
                "class='win'", self.studio.dashboard_html(tab)
            )

    def test_win_without_name_or_price(self):
        profile = _profile()
        profile.services = [Service("Haircut", None, 30)]
        studio = _studio(self.tmp.name, profile=profile)
        service = profile.services[0]
        studio.bookings.book(
            "919999900001", "", service, self.today, 660
        )
        text = _win_text(studio.dashboard_html("today"))
        self.assertEqual(
            text,
            "Your first booking ever. A haircut at 11:00 AM. "
            "The bot made that happen.",
        )

    def test_win_copy_has_no_dash(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut", self.today, 660
        )
        text = _win_text(self.studio.dashboard_html("today"))
        self.assertNotIn("-", text)
        self.assertNotIn("\u2013", text)
        self.assertNotIn("\u2014", text)

    def test_glance_and_money_unchanged_with_win(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut", self.today, 660
        )
        page = self.studio.dashboard_html("today")
        self.assertIn("1 booked", page)
        self.assertIn("Rs 499", page)
        self.assertIn("booked today", page)


if __name__ == "__main__":
    unittest.main()
