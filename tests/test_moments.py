"""Moments: one quiet line in the hero on the Today tab, under the
money, for the bookings the bot made while the shop was closed, in
the most recent closed stretch.

A moment is defined by when the booking was made (created_ts, India
time), never by the appointment date: a booking made at 9 PM for
tomorrow morning is a moment, one made at noon for tomorrow is not.
The line is computed from the bookings on every render, so a
cancelled booking takes its moment with it, and it never appears in
demo mode or dry run. "While he was busy" is not built: the bot
knows the shop hours, not his day.
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

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _profile(hours=None):
    return BusinessProfile(
        name="Glow & Grace Salon",
        category="salon",
        city="Gurgaon",
        phone="919999999999",
        hours=hours or {"daily": "10:00 AM - 8:00 PM"},
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


def _at(day, hour=9, minute=0):
    """A pinned India time on a day, for rendering the dashboard."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def _book(studio, phone, name, service_name, day, start, created=None):
    service = next(s for s in _profile().services if s.name == service_name)
    booking = studio.bookings.book(phone, name, service, day, start)
    assert booking is not None, "slot was not free"
    if created is not None:
        booking["created_ts"] = created
    return booking


def _moments_text(page):
    """The moments line on the page, or None."""
    lines = re.findall(r"<p class='win'>(.*?)</p>", page, re.S)
    for line in lines:
        text = re.sub(r"<[^>]+>", "", line)
        if "While you were closed" in text or text.startswith("Closed today"):
            return text
    return None


class TestMoments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = _studio(self.tmp.name)
        self.today = india_today()
        self.yesterday = self.today - timedelta(days=1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_bookings_returns_empty(self):
        self.assertEqual(
            self.studio.bookings.moments(
                self.studio.profile.hours_today, _at(self.today, 9)
            ),
            [],
        )

    def test_booking_made_after_closing_last_night_is_a_moment(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.yesterday, 21, 0),
        )
        moments = self.studio.bookings.moments(
            self.studio.profile.hours_today, _at(self.today, 9)
        )
        self.assertEqual(len(moments), 1)
        self.assertEqual(moments[0]["reason"], "after closing")
        self.assertEqual(moments[0]["booking"]["name"], "Priya")

    def test_booking_made_before_opening_this_morning_is_a_moment(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.today, 8, 0),
        )
        moments = self.studio.bookings.moments(
            self.studio.profile.hours_today, _at(self.today, 11)
        )
        self.assertEqual(len(moments), 1)
        self.assertEqual(moments[0]["reason"], "before opening")

    def test_booking_made_during_hours_is_not_a_moment(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.today, 10, 30),
        )
        moments = self.studio.bookings.moments(
            self.studio.profile.hours_today, _at(self.today, 11)
        )
        self.assertEqual(moments, [])

    def test_booking_made_on_a_closed_day_is_a_moment(self):
        sunday = self.today - timedelta(days=(self.today.weekday() + 1) % 7 or 7)
        monday = sunday + timedelta(days=1)
        hours = {"daily": "10:00 AM - 8:00 PM", _DAYS[sunday.weekday()]: "closed"}
        studio = _studio(self.tmp.name, profile=_profile(hours))
        _book(
            studio, "919999900001", "Priya", "Haircut",
            monday, 660, created=_epoch(sunday, 15, 0),
        )
        moments = studio.bookings.moments(
            studio.profile.hours_today, _at(monday, 9)
        )
        self.assertEqual(len(moments), 1)
        self.assertEqual(moments[0]["reason"], "closed day")

    def test_booking_made_two_nights_ago_is_not_a_moment(self):
        two_days_ago = self.today - timedelta(days=2)
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(two_days_ago, 21, 0),
        )
        moments = self.studio.bookings.moments(
            self.studio.profile.hours_today, _at(self.today, 9)
        )
        self.assertEqual(moments, [])

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
        moments = self.studio.bookings.moments(
            self.studio.profile.hours_today, _at(self.today, 9)
        )
        self.assertEqual(moments, [])

    def test_midnight_boundary_uses_each_bookings_own_day(self):
        # Monday closes at 8 PM, Tuesday at 6 PM: 11:59 PM Monday is
        # after Monday's close, 12:01 AM Tuesday is before Tuesday's
        # open. Both are moments, with different reasons.
        monday = self.today - timedelta(days=self.today.weekday())
        tuesday = monday + timedelta(days=1)
        hours = {
            "mon": "10:00 AM - 8:00 PM",
            "tue": "10:00 AM - 6:00 PM",
        }
        studio = _studio(self.tmp.name, profile=_profile(hours))
        _book(
            studio, "919999900001", "Rahul", "Haircut",
            tuesday, 660, created=_epoch(monday, 23, 59),
        )
        _book(
            studio, "919999900002", "Priya", "Haircut",
            tuesday, 720, created=_epoch(tuesday, 0, 1),
        )
        moments = studio.bookings.moments(
            studio.profile.hours_today, _at(tuesday, 9)
        )
        self.assertEqual(len(moments), 2)
        self.assertEqual(
            [m["reason"] for m in moments],
            ["after closing", "before opening"],
        )

    def test_unparseable_hours_make_no_moment(self):
        studio = _studio(
            self.tmp.name, profile=_profile({"daily": "call us"})
        )
        _book(
            studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.yesterday, 21, 0),
        )
        moments = studio.bookings.moments(
            studio.profile.hours_today, _at(self.today, 9)
        )
        self.assertEqual(moments, [])

    def test_hours_changed_since_booking_can_misclassify(self):
        # The shop used to close at 8 PM and now closes at 9 PM. A
        # booking made at 8:30 PM last night no longer looks after
        # hours against the live profile. Accepted for v1: hours
        # change rarely, and moments are recent by nature.
        studio = _studio(
            self.tmp.name, profile=_profile({"daily": "10:00 AM - 9:00 PM"})
        )
        _book(
            studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.yesterday, 20, 30),
        )
        moments = studio.bookings.moments(
            studio.profile.hours_today, _at(self.today, 9)
        )
        self.assertEqual(moments, [])

    def test_cancelled_moment_disappears(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.yesterday, 21, 0),
        )
        self.studio.bookings.cancel_latest("919999900001")
        moments = self.studio.bookings.moments(
            self.studio.profile.hours_today, _at(self.today, 9)
        )
        self.assertEqual(moments, [])

    def test_moments_are_oldest_first(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.yesterday, 22, 0),
        )
        _book(
            self.studio, "919999900002", "Asha", "Facial",
            self.today, 720, created=_epoch(self.yesterday, 21, 0),
        )
        moments = self.studio.bookings.moments(
            self.studio.profile.hours_today, _at(self.today, 9)
        )
        self.assertEqual(
            [m["booking"]["name"] for m in moments], ["Asha", "Priya"]
        )


class TestMomentsDashboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = _studio(self.tmp.name)
        self.today = india_today()
        self.yesterday = self.today - timedelta(days=1)
        self.tomorrow = self.today + timedelta(days=1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dashboard_shows_one_moment_for_today(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.yesterday, 21, 0),
        )
        text = _moments_text(
            self.studio.dashboard_html("today", now=_at(self.today, 9))
        )
        self.assertEqual(
            text,
            "While you were closed, Priya booked a haircut at 11:00 AM "
            "for Rs 499.",
        )

    def test_dashboard_names_tomorrow_for_a_future_appointment(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.tomorrow, 660, created=_epoch(self.yesterday, 21, 0),
        )
        text = _moments_text(
            self.studio.dashboard_html("today", now=_at(self.today, 9))
        )
        self.assertEqual(
            text,
            "While you were closed, Priya booked a haircut for tomorrow "
            "at 11:00 AM for Rs 499.",
        )

    def test_dashboard_shows_count_and_total_for_several_moments(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.yesterday, 21, 0),
        )
        _book(
            self.studio, "919999900002", "Asha", "Facial",
            self.today, 720, created=_epoch(self.yesterday, 22, 0),
        )
        text = _moments_text(
            self.studio.dashboard_html("today", now=_at(self.today, 9))
        )
        self.assertEqual(
            text, "While you were closed, 2 people booked for Rs 1998."
        )

    def test_dashboard_hides_win_when_first_win_is_a_moment(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.today, 8, 0),
        )
        page = self.studio.dashboard_html("today", now=_at(self.today, 9))
        self.assertEqual(
            _moments_text(page),
            "While you were closed, Priya booked a haircut at 11:00 AM "
            "for Rs 499.",
        )
        self.assertNotIn("First booking", page)
        self.assertNotIn("first booking ever", page)

    def test_dashboard_shows_both_lines_for_different_bookings(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.yesterday, 21, 0),
        )
        _book(
            self.studio, "919999900002", "Asha", "Facial",
            self.today, 720, created=_epoch(self.today, 10, 30),
        )
        page = self.studio.dashboard_html("today", now=_at(self.today, 11))
        self.assertIn(
            "First booking today. Asha booked a facial at 12:00 PM "
            "for Rs 1499.",
            page,
        )
        self.assertEqual(
            _moments_text(page),
            "While you were closed, Priya booked a haircut at 11:00 AM "
            "for Rs 499.",
        )

    def test_dashboard_shows_closed_today_variant(self):
        hours = {
            "daily": "10:00 AM - 8:00 PM",
            _DAYS[self.today.weekday()]: "closed",
        }
        studio = _studio(self.tmp.name, profile=_profile(hours))
        _book(
            studio, "919999900001", "Priya", "Haircut",
            self.tomorrow, 660, created=_epoch(self.yesterday, 21, 0),
        )
        text = _moments_text(
            studio.dashboard_html("today", now=_at(self.today, 15))
        )
        self.assertEqual(
            text,
            "Closed today. Priya still booked a haircut for tomorrow "
            "at 11:00 AM for Rs 499.",
        )

    def test_dashboard_shows_closed_today_count_for_several(self):
        hours = {
            "daily": "10:00 AM - 8:00 PM",
            _DAYS[self.today.weekday()]: "closed",
        }
        studio = _studio(self.tmp.name, profile=_profile(hours))
        _book(
            studio, "919999900001", "Priya", "Haircut",
            self.tomorrow, 660, created=_epoch(self.yesterday, 21, 0),
        )
        _book(
            studio, "919999900002", "Asha", "Facial",
            self.tomorrow, 720, created=_epoch(self.yesterday, 22, 0),
        )
        text = _moments_text(
            studio.dashboard_html("today", now=_at(self.today, 15))
        )
        self.assertEqual(
            text, "Closed today. 2 people still booked for Rs 1998."
        )

    def test_dashboard_hides_moments_line_with_no_moments(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.today, 10, 30),
        )
        page = self.studio.dashboard_html("today", now=_at(self.today, 11))
        self.assertIsNone(_moments_text(page))
        self.assertNotIn("While you were closed", page)

    def test_dashboard_hides_moments_in_demo_mode(self):
        studio = _studio(self.tmp.name, demo_mode=True)
        # Noon, not 11am: the demo seed already holds a sample
        # booking at 11am tomorrow, and the slot check counts every
        # service at that time.
        _book(
            studio, "919999900001", "Priya", "Haircut",
            self.tomorrow, 720, created=_epoch(self.yesterday, 21, 0),
        )
        page = studio.dashboard_html("today", now=_at(self.today, 9))
        self.assertIsNone(_moments_text(page))
        self.assertNotIn("While you were closed", page)

    def test_dashboard_hides_moments_in_dry_run(self):
        studio = _studio(self.tmp.name, dry_run=True)
        _book(
            studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.yesterday, 21, 0),
        )
        page = studio.dashboard_html("today", now=_at(self.today, 9))
        self.assertIsNone(_moments_text(page))
        self.assertNotIn("While you were closed", page)

    def test_dashboard_shows_moments_when_sending_paused(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.yesterday, 21, 0),
        )
        self.studio.disable_sending()
        text = _moments_text(
            self.studio.dashboard_html("today", now=_at(self.today, 9))
        )
        self.assertEqual(
            text,
            "While you were closed, Priya booked a haircut at 11:00 AM "
            "for Rs 499.",
        )

    def test_dashboard_shows_moments_only_on_the_today_tab(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.yesterday, 21, 0),
        )
        for tab in ("bookings", "customers", "replies", "conversation"):
            page = self.studio.dashboard_html(tab, now=_at(self.today, 9))
            self.assertNotIn("While you were closed", page)

    def test_moments_without_name_or_price(self):
        profile = _profile()
        profile.services = [Service("Haircut", None, 30)]
        studio = _studio(self.tmp.name, profile=profile)
        service = profile.services[0]
        booking = studio.bookings.book(
            "919999900001", "", service, self.today, 660
        )
        booking["created_ts"] = _epoch(self.yesterday, 21, 0)
        text = _moments_text(
            studio.dashboard_html("today", now=_at(self.today, 9))
        )
        self.assertEqual(
            text,
            "While you were closed, someone booked a haircut at 11:00 AM.",
        )

    def test_moments_copy_has_no_dash(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.yesterday, 21, 0),
        )
        _book(
            self.studio, "919999900002", "Asha", "Facial",
            self.today, 720, created=_epoch(self.yesterday, 22, 0),
        )
        text = _moments_text(
            self.studio.dashboard_html("today", now=_at(self.today, 9))
        )
        self.assertNotIn("-", text)
        self.assertNotIn("\u2013", text)
        self.assertNotIn("\u2014", text)

    def test_glance_and_money_unchanged_with_moments(self):
        _book(
            self.studio, "919999900001", "Priya", "Haircut",
            self.today, 660, created=_epoch(self.yesterday, 21, 0),
        )
        page = self.studio.dashboard_html("today", now=_at(self.today, 9))
        self.assertIn("1 booked", page)
        self.assertIn("Rs 499", page)
        self.assertIn("booked today", page)


if __name__ == "__main__":
    unittest.main()
