"""The regulars view: repeat customers on the Customers tab, most
bookings first, with a note when one has gone quiet.

The follow up from the dashboard redesign plan: a shop lives on its
regulars, and a regular who has not been back in a month is the one
to call first. The view is computed from the bookings, so there is
nothing new to store.
"""
import json
import re
import tempfile
import threading
import unittest
import urllib.request
from datetime import timedelta
from pathlib import Path

from test_llm import FixedLLM
from whatsapp_studio.app import Studio, make_server
from whatsapp_studio.bookings import BookingStore, india_today
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.leads import LeadStore
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


def _studio(tmp):
    return Studio(
        _profile(),
        LeadStore(Path(tmp) / "leads.json"),
        bookings=BookingStore(),
        llm=FixedLLM("We are open 10:00 AM to 8:00 PM."),
        sender=WhatsAppSender("tok", "pid"),
    )


_slot = [600]


def _book(studio, phone, name, service_name, days_ago):
    service = next(s for s in _profile().services if s.name == service_name)
    # Each booking takes its own time on the day: the shop has one
    # chair, so two customers cannot share a slot even in a test.
    start = _slot[0]
    _slot[0] += 60
    return studio.bookings.book(
        phone, name, service, india_today() - timedelta(days=days_ago), start
    )


def _regulars_text(page):
    match = re.search(r"<h2>Regulars</h2>(.*?)</ul>", page, re.S)
    assert match is not None, "no Regulars section on the page"
    return re.sub(r"<[^>]+>", "", match.group(1))


class TestRegulars(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = _studio(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_booking_is_not_a_regular(self):
        _book(self.studio, "919810000001", "Rahul", "Haircut", 0)
        self.assertEqual(self.studio.bookings.regulars(), [])

    def test_two_bookings_make_a_regular(self):
        _book(self.studio, "919810000001", "Rahul", "Haircut", 10)
        _book(self.studio, "919810000001", "Rahul", "Facial", 0)
        regulars = self.studio.bookings.regulars()
        self.assertEqual(len(regulars), 1)
        self.assertEqual(regulars[0]["name"], "Rahul")
        self.assertEqual(regulars[0]["count"], 2)
        self.assertEqual(regulars[0]["quiet"], False)

    def test_usual_service_is_the_most_booked(self):
        _book(self.studio, "919810000001", "Rahul", "Haircut", 20)
        _book(self.studio, "919810000001", "Rahul", "Facial", 10)
        _book(self.studio, "919810000001", "Rahul", "Haircut", 0)
        self.assertEqual(
            self.studio.bookings.regulars()[0]["usual_service"], "Haircut"
        )

    def test_usual_service_tie_goes_to_the_most_recent(self):
        _book(self.studio, "919810000001", "Rahul", "Haircut", 30)
        _book(self.studio, "919810000001", "Rahul", "Facial", 20)
        _book(self.studio, "919810000001", "Rahul", "Haircut", 10)
        _book(self.studio, "919810000001", "Rahul", "Facial", 0)
        self.assertEqual(
            self.studio.bookings.regulars()[0]["usual_service"], "Facial"
        )

    def test_name_is_the_latest_known_name(self):
        _book(self.studio, "919810000001", "Rahul", "Haircut", 10)
        _book(self.studio, "919810000001", "", "Haircut", 0)
        self.assertEqual(self.studio.bookings.regulars()[0]["name"], "Rahul")
        _book(self.studio, "919810000002", "", "Haircut", 10)
        _book(self.studio, "919810000002", "Asha", "Haircut", 0)
        names = {r["phone"]: r["name"] for r in self.studio.bookings.regulars()}
        self.assertEqual(names["919810000002"], "Asha")

    def test_quiet_after_thirty_days(self):
        _book(self.studio, "919810000001", "Rahul", "Haircut", 40)
        _book(self.studio, "919810000001", "Rahul", "Haircut", 31)
        _book(self.studio, "919810000002", "Asha", "Haircut", 40)
        _book(self.studio, "919810000002", "Asha", "Haircut", 30)
        quiet = {r["name"]: r["quiet"] for r in self.studio.bookings.regulars()}
        self.assertEqual(quiet, {"Rahul": True, "Asha": False})

    def test_most_bookings_first(self):
        _book(self.studio, "919810000001", "Rahul", "Haircut", 10)
        _book(self.studio, "919810000001", "Rahul", "Haircut", 0)
        _book(self.studio, "919810000002", "Asha", "Haircut", 20)
        _book(self.studio, "919810000002", "Asha", "Facial", 10)
        _book(self.studio, "919810000002", "Asha", "Haircut", 0)
        names = [r["name"] for r in self.studio.bookings.regulars()]
        self.assertEqual(names, ["Asha", "Rahul"])

    def test_dashboard_shows_regulars(self):
        _book(self.studio, "919810000001", "Rahul", "Haircut", 10)
        _book(self.studio, "919810000001", "Rahul", "Facial", 5)
        _book(self.studio, "919810000001", "Rahul", "Haircut", 0)
        _book(self.studio, "919810000002", "Asha", "Haircut", 50)
        _book(self.studio, "919810000002", "Asha", "Haircut", 40)
        page = self.studio.dashboard_html("customers")
        self.assertIn("Regulars", page)
        text = _regulars_text(page)
        self.assertIn("Rahul, 3rd booking, usually a haircut", text)
        self.assertIn("Asha, 2nd booking, usually a haircut", text)
        self.assertIn("Has not been back in a while", text)
        self.assertIn("The people who keep coming back", text)

    def test_dashboard_hides_regulars_when_there_are_none(self):
        _book(self.studio, "919810000001", "Rahul", "Haircut", 0)
        page = self.studio.dashboard_html("customers")
        self.assertNotIn("Regulars", page)

    def test_regulars_copy_has_no_dash(self):
        _book(self.studio, "919810000001", "Rahul", "Haircut", 40)
        _book(self.studio, "919810000001", "Rahul", "Haircut", 31)
        page = self.studio.dashboard_html("customers")
        text = _regulars_text(page)
        self.assertNotIn("-", text)
        self.assertNotIn("\u2013", text)
        self.assertNotIn("\u2014", text)

    def test_regulars_endpoint(self):
        _book(self.studio, "919810000001", "Rahul", "Haircut", 10)
        _book(self.studio, "919810000001", "Rahul", "Haircut", 0)
        httpd = make_server(self.studio, port=0)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/regulars"
            ) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read())
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(len(data["regulars"]), 1)
        self.assertEqual(data["regulars"][0]["name"], "Rahul")
        self.assertEqual(data["regulars"][0]["count"], 2)
        self.assertEqual(
            data["regulars"][0]["last_date"], india_today().isoformat()
        )


if __name__ == "__main__":
    unittest.main()
