"""Ellipsus visual system: fonts, classes, labels, CSS only motion.

The dashboard must stay script free (the security tests forbid a
<script> tag), so every effect here is plain CSS: a soft fade on
load, sections fading in as they enter where the browser supports
it, a CSS typewriter on the greeting, near invisible hovers. The
labels stay verbatim.
"""
import tempfile
import unittest
from pathlib import Path

from whatsapp_studio.app import Studio
from whatsapp_studio.bookings import BookingStore
from whatsapp_studio.config import BusinessProfile
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore

ROOT = Path(__file__).resolve().parents[1]


def make_studio(tmp, demo_mode=False):
    profile = BusinessProfile.from_json(ROOT / "demo" / "demo_business.json")
    return Studio(
        profile,
        LeadStore(Path(tmp) / "leads.json"),
        bookings=BookingStore(Path(tmp) / "bookings.json"),
        handoffs=HandoffStore(Path(tmp) / "handoffs.json"),
        outbox_path=Path(tmp) / "outbox.json",
        demo_mode=demo_mode,
    )


class TestVisualSystem(unittest.TestCase):
    def test_fonts_and_key_classes_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=True)
            html = studio.dashboard_html()
            self.assertIn("fonts.googleapis.com", html)
            self.assertIn("Fraunces", html)
            self.assertIn("Inter", html)
            for marker in (
                "class='hero'",
                "class='money'",
                "class='tabs'",
                "class='type'",
            ):
                self.assertIn(marker, html)
            self.assertIn("class='thread'", studio.dashboard_html("conversation"))
            self.assertIn("class='replybox'", studio.dashboard_html("replies"))
            self.assertIn("prefers-reduced-motion", html)
            self.assertIn("animation-timeline", html)
            self.assertNotIn("<script>", html)

    def test_labels_survive_the_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=True)
            for tab in ("today", "bookings", "customers", "replies", "conversation"):
                html = studio.dashboard_html(tab)
                self.assertIn("Bot is ON", html)
                self.assertIn("Demo", html)
            self.assertIn("People waiting for you", studio.dashboard_html("today"))
            replies = studio.dashboard_html("replies")
            self.assertIn("Reply to a customer", replies)
            self.assertIn("Record reply, sending is off", replies)
            self.assertIn("Drafts need an LLM key", replies)
            convo = studio.dashboard_html("conversation")
            self.assertIn("Conversation", convo)
            self.assertIn("class='msg in'", convo)
            self.assertIn("class='msg out'", convo)
