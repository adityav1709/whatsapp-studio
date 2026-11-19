"""The self test line: one quiet line on the Today tab, after the setup
checklist, inviting the owner to test the bot himself by texting the
shop number from another phone and asking the price of the first
priced service. The number is a wa.me link with the question
prefilled, so tapping it opens WhatsApp ready to send.

The line only shows when a test would actually get an answer: sending
live, a key set, not demo mode, a shop number on file, and at least
one published price. Otherwise it stays hidden, so the owner is never
invited to a test that gets silence.
"""
import http.client
import re
import tempfile
import threading
import unittest
from pathlib import Path

from test_llm import FixedLLM
from whatsapp_studio.app import Studio, make_server
from whatsapp_studio.bookings import BookingStore
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.missed import MissedStore
from whatsapp_studio.send import WhatsAppSender


def _profile(services=None, phone="919999999999"):
    return BusinessProfile(
        name="Glow & Grace Salon",
        category="salon",
        city="Gurgaon",
        phone=phone,
        hours={"daily": "10:00 AM - 8:00 PM"},
        services=services if services is not None else [
            Service("Haircut", 499, 30),
            Service("Facial", 1499, 60),
        ],
        address="Sector 50, Gurgaon",
    )


def _studio(tmp, demo_mode=False, dry_run=False, profile=None, llm=True, sender=True):
    return Studio(
        profile or _profile(),
        LeadStore(Path(tmp) / "leads.json"),
        bookings=BookingStore(),
        handoffs=HandoffStore(),
        missed=MissedStore(),
        llm=FixedLLM("We are open 10:00 AM to 8:00 PM.") if llm else None,
        sender=WhatsAppSender("tok", "pid") if sender else None,
        demo_mode=demo_mode,
        dry_run=dry_run,
    )


def _line_text(page):
    match = re.search(r"<p class='selftest'>(.*?)</p>", page, re.S)
    assert match is not None, "no self test line on the page"
    return re.sub(r"<[^>]+>", "", match.group(1))


class TestSelfTestLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = _studio(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_live_studio_names_first_priced_service_digits_and_price(self):
        line = self.studio.self_test_line()
        self.assertIn("Haircut", line)
        self.assertIn("+919999999999", line)
        self.assertIn("Rs 499", line)
        self.assertIn(
            "https://wa.me/919999999999?text=What%20is%20the%20price%20of%20a%20Haircut%3F",
            line,
        )

    def test_first_priced_service_wins_when_unpriced_listed_first(self):
        profile = _profile(services=[
            Service("Facial", None, 60),
            Service("Haircut", 499, 30),
        ])
        studio = _studio(self.tmp.name, profile=profile)
        line = studio.self_test_line()
        self.assertIn("Haircut", line)
        self.assertIn("Rs 499", line)
        self.assertNotIn("Facial", line)

    def test_no_priced_service_returns_empty(self):
        profile = _profile(services=[Service("Facial", None, 60)])
        studio = _studio(self.tmp.name, profile=profile)
        self.assertEqual(studio.self_test_line(), "")

    def test_no_services_returns_empty(self):
        profile = _profile(services=[])
        studio = _studio(self.tmp.name, profile=profile)
        self.assertEqual(studio.self_test_line(), "")

    def test_blank_phone_returns_empty(self):
        profile = _profile(phone="")
        studio = _studio(self.tmp.name, profile=profile)
        self.assertEqual(studio.self_test_line(), "")

    def test_phone_with_spaces_and_plus_yields_digits_only(self):
        profile = _profile(phone="+91 99999 99999")
        studio = _studio(self.tmp.name, profile=profile)
        line = studio.self_test_line()
        self.assertIn("+919999999999", line)
        self.assertIn("https://wa.me/919999999999?text=", line)

    def test_demo_mode_returns_empty(self):
        studio = _studio(self.tmp.name, demo_mode=True)
        self.assertEqual(studio.self_test_line(), "")

    def test_dry_run_returns_empty(self):
        studio = _studio(self.tmp.name, dry_run=True)
        self.assertEqual(studio.self_test_line(), "")

    def test_paused_sending_returns_empty(self):
        self.studio.disable_sending()
        self.assertEqual(self.studio.self_test_line(), "")

    def test_no_sender_returns_empty(self):
        studio = _studio(self.tmp.name, sender=False)
        self.assertEqual(studio.self_test_line(), "")

    def test_no_llm_key_returns_empty(self):
        studio = _studio(self.tmp.name, llm=False)
        self.assertEqual(studio.self_test_line(), "")

    def test_dashboard_shows_exact_line_on_today(self):
        text = _line_text(self.studio.dashboard_html("today"))
        self.assertEqual(
            text,
            "Try it yourself. Text +919999999999 on WhatsApp from another "
            "phone and ask: What is the price of a Haircut? You should get "
            "Rs 499 back in a few seconds, and your message will show on "
            "this page too. If nothing comes back, check the bot is on above.",
        )

    def test_dashboard_hides_line_when_it_would_get_silence(self):
        cases = {
            "demo": _studio(self.tmp.name, demo_mode=True),
            "dry run": _studio(self.tmp.name, dry_run=True),
            "no sender": _studio(self.tmp.name, sender=False),
            "no key": _studio(self.tmp.name, llm=False),
            "blank phone": _studio(self.tmp.name, profile=_profile(phone="")),
            "no priced service": _studio(
                self.tmp.name,
                profile=_profile(services=[Service("Facial", None, 60)]),
            ),
        }
        for name, studio in cases.items():
            with self.subTest(name):
                self.assertNotIn("Try it yourself", studio.dashboard_html("today"))
        paused = _studio(self.tmp.name)
        paused.disable_sending()
        self.assertNotIn("Try it yourself", paused.dashboard_html("today"))

    def test_line_only_on_the_today_tab(self):
        for tab in ("bookings", "customers", "replies", "conversation"):
            with self.subTest(tab=tab):
                self.assertNotIn(
                    "Try it yourself", self.studio.dashboard_html(tab)
                )

    def test_line_copy_has_no_dash(self):
        text = _line_text(self.studio.dashboard_html("today"))
        self.assertNotIn("-", text)
        self.assertNotIn("\u2013", text)
        self.assertNotIn("\u2014", text)

    def test_hyphenated_service_name_adds_no_dash_to_the_link(self):
        profile = _profile(services=[Service("Cut-Colour", 799, 45)])
        studio = _studio(self.tmp.name, profile=profile)
        line = studio.self_test_line()
        self.assertIn("Cut-Colour", line)
        link = re.search(r"href='([^']+)'", line).group(1)
        self.assertNotIn("-", link.split("?text=")[0])

    def test_setup_checklist_unchanged_with_line(self):
        page = self.studio.dashboard_html("today")
        self.assertIn("Setup", page)
        self.assertIn("Services and prices filled in: done.", page)
        self.assertIn("Try it yourself", page)


class TestSelfTestLive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = _studio(self.tmp.name)
        self.httpd = make_server(self.studio, port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _get(self, port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        return resp.status, body

    def test_live_page_carries_the_line(self):
        status, body = self._get(self.port)
        self.assertEqual(status, 200)
        self.assertIn("Try it yourself", body)
        self.assertIn("wa.me/919999999999", body)

    def test_demo_page_has_no_line(self):
        demo = _studio(self.tmp.name, demo_mode=True)
        httpd = make_server(demo, port=0)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = self._get(port)
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(status, 200)
        self.assertNotIn("Try it yourself", body)


if __name__ == "__main__":
    unittest.main()
