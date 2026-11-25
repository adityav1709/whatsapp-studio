"""The mock preview: ?mock=1 shows every section with sample data.

Demo mode hides the win and moments lines, since sample bookings are
not real customers. A mock preview lifts that gate for one page view
so the owner can see the whole product at work, and the page says so.
The sample data itself is richer now: a regular, a booking made after
closing, and two people who left with nothing.
"""
import http.client
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlencode

from test_demo_mode import make_studio
from test_llm import FixedLLM
from whatsapp_studio.app import make_server
from whatsapp_studio.bookings import IST, india_today


def _afternoon_today():
    """3 PM today in India time: open hours, so a preview shows both
    the win line and the moments line."""
    now = datetime.now(IST)
    return now.replace(hour=15, minute=0, second=0, microsecond=0)


def _pin_created_times(studio, now):
    """Pin every seeded booking to a known made time, so the page
    renders the same whatever time the suite runs. The moment booking
    was made last night after closing; the rest were made this
    morning during open hours, earliest first."""
    today = now.date()
    yesterday = today - timedelta(days=1)
    made = {
        ("919810000001", "Haircut", today.isoformat()): (today, 11, 0),
        ("919810000002", "Facial", (today + timedelta(days=1)).isoformat()): (today, 11, 5),
        ("919810000004", "Hair Spa", today.isoformat()): (today, 11, 10),
        ("919810000001", "Haircut", (today - timedelta(days=10)).isoformat()): (today, 11, 15),
        ("919810000007", "Facial", (today + timedelta(days=1)).isoformat()): (yesterday, 20, 30),
    }
    for booking in studio.bookings.all_bookings():
        key = (booking["phone"], booking["service"], booking["date"])
        day, hour, minute = made[key]
        moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)
        booking["created_ts"] = int(moment.timestamp())
    studio.bookings._save()


class TestMockSeed(unittest.TestCase):
    def test_seed_extras_show_regulars_and_bad_experience(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=True)
            self.assertEqual(len(studio.bookings.all_bookings()), 5)
            self.assertEqual(len(studio.bookings.bookings_on(india_today())), 2)
            regulars = studio.bookings.regulars()
            self.assertEqual(len(regulars), 1)
            self.assertEqual(regulars[0]["name"], "Rahul")
            self.assertEqual(regulars[0]["count"], 2)
            self.assertEqual(regulars[0]["usual_service"], "Haircut")
            self.assertFalse(regulars[0]["quiet"])
            self.assertEqual(len(studio.missed.open_newest()), 2)
            self.assertEqual(studio.missed.open_today(), 2)
            html = studio.dashboard_html()
            self.assertIn("Rs 1498", html)
            self.assertEqual(len(studio.leads.all_leads()), 6)
            self.assertEqual(len(studio.outbox), 7)
            self.assertIn("Bad experience", html)
            self.assertIn("Karan", html)
            self.assertIn("Divya", html)
            self.assertIn("2 people left without booking today.", html)
            customers = studio.dashboard_html("customers")
            self.assertIn("Regulars", customers)
            self.assertIn("Rahul", customers)


class TestMockPreview(unittest.TestCase):
    def test_mock_off_hides_win_and_moments_in_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=True)
            _pin_created_times(studio, _afternoon_today())
            html = studio.dashboard_html(now=_afternoon_today())
            self.assertNotIn("First booking today", html)
            self.assertNotIn("While you were closed", html)
            self.assertNotIn("Mock preview", html)
            self.assertNotIn("mock=1", html)

    def test_mock_on_shows_win_and_moments_in_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=True)
            _pin_created_times(studio, _afternoon_today())
            html = studio.dashboard_html(now=_afternoon_today(), mock=True)
            self.assertIn(
                "First booking today. Rahul booked a haircut at 11:00 AM for Rs 499.",
                html,
            )
            self.assertIn(
                "While you were closed, Meera booked a facial for tomorrow at 11:00 AM for Rs 1499.",
                html,
            )
            self.assertIn("Mock preview. Every section is shown with sample data.", html)
            self.assertIn("/?tab=bookings&mock=1", html)
            self.assertIn("/?tab=replies&mock=1", html)

    def test_mock_still_hides_the_self_test_line(self):
        # The self test invites texting the shop number from another
        # phone. In a preview the number is fake and no reply could
        # come back, so the line stays off even with mock on.
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=True)
            html = studio.dashboard_html(now=_afternoon_today(), mock=True)
            self.assertNotIn("Try it yourself", html)

    def test_mock_copy_has_no_dash(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=True)
            _pin_created_times(studio, _afternoon_today())
            html = studio.dashboard_html(now=_afternoon_today(), mock=True)
            for line in (
                "First booking today. Rahul booked a haircut at 11:00 AM for Rs 499.",
                "While you were closed, Meera booked a facial for tomorrow at 11:00 AM for Rs 1499.",
                "Mock preview. Every section is shown with sample data.",
            ):
                self.assertIn(line, html)
                for bad in ("-", "\u2013", "\u2014"):
                    self.assertNotIn(bad, line)


class TestMockPreviewHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = make_studio(self.tmp.name, demo_mode=True, llm=FixedLLM(
            "Hi Amit, sorry about the delay. The team will call you about the refund today."
        ))
        _pin_created_times(self.studio, _afternoon_today())
        # The page renders with the same pinned afternoon, not the
        # live clock: after closing, last night's booking falls out
        # of the most recent closed stretch and the moments line
        # would vanish, failing the suite only at night.
        self.httpd = make_server(self.studio, port=0, now=_afternoon_today())
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}"
        ) as resp:
            return resp.status, resp.read().decode()

    def _post(self, path, fields=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = urlencode(fields or {})
        conn.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp = conn.getresponse()
        status, location = resp.status, resp.getheader("Location")
        resp.read()
        conn.close()
        return status, location

    def test_mock_page_over_http_and_draft_through_the_bot(self):
        status, html = self._get("/?mock=1")
        self.assertEqual(status, 200)
        self.assertIn("First booking today", html)
        self.assertIn("While you were closed", html)
        self.assertIn("Bad experience", html)
        self.assertIn("Mock preview", html)
        # The draft comes from the brain itself: one tap, and the
        # reply box on the Replies tab is prefilled. Nothing is sent.
        sent_before = len(self.studio.outbox)
        status, location = self._post("/handoffs/1/draft")
        self.assertEqual(status, 303)
        self.assertEqual(location, "/?tab=replies")
        self.assertEqual(len(self.studio.outbox), sent_before)
        _, replies = self._get("/?tab=replies")
        self.assertIn(
            "value='Hi Amit, sorry about the delay. The team will call you about the refund today.'",
            replies,
        )


if __name__ == "__main__":
    unittest.main()
