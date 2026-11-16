"""The setup checklist: four things a real install needs, and the
dashboard says plainly which of them is missing and what fixes it.

The checklist is a follow up from the dashboard redesign plan: the
owner (and Aditya, before a pitch) should be able to check a fresh
install in one glance. It stays hidden in demo mode, where the page
is a showcase, not a real shop.
"""
import re
import tempfile
import unittest
from pathlib import Path

from test_llm import FixedLLM
from whatsapp_studio.app import Studio
from whatsapp_studio.bookings import BookingStore
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.notify import NullNotifier, TelegramNotifier
from whatsapp_studio.send import WhatsAppSender


def _profile(prices=True):
    return BusinessProfile(
        name="Glow & Grace Salon",
        category="salon",
        city="Gurgaon",
        phone="919999999999",
        hours={"daily": "10:00 AM - 8:00 PM"},
        services=[
            Service("Haircut", 499 if prices else None, 30),
            Service("Facial", 1499, 60),
        ],
        address="Sector 50, Gurgaon",
    )


def _studio(tmp, **kwargs):
    kwargs.setdefault("llm", FixedLLM("We are open 10:00 AM to 8:00 PM."))
    kwargs.setdefault("sender", WhatsAppSender("tok", "pid"))
    kwargs.setdefault("notifier", TelegramNotifier("tok", "chat"))
    return Studio(
        _profile(kwargs.pop("prices", True)),
        LeadStore(Path(tmp) / "leads.json"),
        bookings=BookingStore(),
        **kwargs,
    )


def _setup_text(page):
    match = re.search(r"<section class='setup'>(.*?)</section>", page, re.S)
    assert match is not None, "no setup section on the page"
    return re.sub(r"<[^>]+>", "", match.group(1))


class TestSetupChecklist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_done(self):
        items = dict(
            (label, ok) for label, ok, _ in _studio(self.tmp.name).setup_checklist()
        )
        self.assertEqual(
            items,
            {
                "Bot replies": True,
                "WhatsApp number connected": True,
                "Services and prices filled in": True,
                "Owner alerts on": True,
            },
        )
        page = _studio(self.tmp.name).dashboard_html("today")
        self.assertIn("Setup complete", page)
        self.assertIn("Bot replies: done.", page)

    def test_no_key_means_bot_cannot_reply(self):
        studio = _studio(self.tmp.name, llm=None)
        items = dict(
            (label, (ok, hint)) for label, ok, hint in studio.setup_checklist()
        )
        self.assertFalse(items["Bot replies"][0])
        self.assertIn("LLM_API_KEY", items["Bot replies"][1])
        self.assertIn("Setup", studio.dashboard_html("today"))
        self.assertNotIn("Setup complete", studio.dashboard_html("today"))

    def test_no_sender_means_number_not_connected(self):
        studio = _studio(self.tmp.name, sender=None)
        items = dict(
            (label, (ok, hint)) for label, ok, hint in studio.setup_checklist()
        )
        self.assertFalse(items["WhatsApp number connected"][0])
        self.assertIn("WHATSAPP_TOKEN", items["WhatsApp number connected"][1])

    def test_dry_run_and_paused_are_not_on(self):
        dry = _studio(self.tmp.name, dry_run=True)
        items = dict((label, ok) for label, ok, _ in dry.setup_checklist())
        self.assertFalse(items["Bot replies"])
        paused = _studio(self.tmp.name)
        paused.disable_sending()
        items = dict((label, ok) for label, ok, _ in paused.setup_checklist())
        self.assertFalse(items["Bot replies"])
        self.assertIn("Resume sending", paused.dashboard_html("today"))

    def test_price_missing_and_no_services(self):
        studio = _studio(self.tmp.name, prices=False)
        items = dict(
            (label, (ok, hint)) for label, ok, hint in studio.setup_checklist()
        )
        self.assertFalse(items["Services and prices filled in"][0])
        self.assertIn("prices", items["Services and prices filled in"][1])
        empty = Studio(
            BusinessProfile(
                name="Glow & Grace Salon",
                category="salon",
                city="Gurgaon",
                phone="919999999999",
                hours={"daily": "10:00 AM - 8:00 PM"},
                services=[],
                address="Sector 50, Gurgaon",
            ),
            LeadStore(Path(self.tmp.name) / "leads.json"),
            bookings=BookingStore(),
            llm=FixedLLM("hi"),
            sender=WhatsAppSender("tok", "pid"),
            notifier=TelegramNotifier("tok", "chat"),
        )
        items = dict((label, ok) for label, ok, _ in empty.setup_checklist())
        self.assertFalse(items["Services and prices filled in"])

    def test_no_telegram_means_alerts_off(self):
        studio = _studio(self.tmp.name, notifier=NullNotifier())
        items = dict(
            (label, (ok, hint)) for label, ok, hint in studio.setup_checklist()
        )
        self.assertFalse(items["Owner alerts on"][0])
        self.assertIn("TELEGRAM_CHAT_ID", items["Owner alerts on"][1])

    def test_demo_mode_hides_the_checklist(self):
        studio = _studio(self.tmp.name, demo_mode=True)
        self.assertNotIn("Setup", studio.dashboard_html("today"))

    def test_checklist_copy_has_no_dash(self):
        page = _studio(self.tmp.name, llm=None, sender=None, notifier=NullNotifier()).dashboard_html("today")
        text = _setup_text(page)
        self.assertNotIn("-", text)
        self.assertNotIn("\u2013", text)
        self.assertNotIn("\u2014", text)


if __name__ == "__main__":
    unittest.main()
