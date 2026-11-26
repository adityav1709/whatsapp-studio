"""Ellipsus visual system: fonts, classes, labels, CSS only motion.

The dashboard must stay script free (the security tests forbid a
<script> tag), so every effect here is plain CSS: sections below the
fold fade in as they enter where the browser supports it, a CSS
typewriter on the greeting (switched off on phones), near invisible
hovers. Above-fold content is visible on first paint: no load
animation, and nothing below the fold is gated on scroll except
cards, list rows and the thread. The labels stay verbatim.
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
            self.assertIn("People waiting for you", studio.dashboard_html("customers"))
            replies = studio.dashboard_html("replies")
            self.assertIn("Reply to a customer", replies)
            self.assertIn("Record reply, sending is off", replies)
            self.assertIn("No draft for now.", replies)
            convo = studio.dashboard_html("conversation")
            self.assertIn("Conversation", convo)
            self.assertIn("class='msg in'", convo)
            self.assertIn("class='msg out'", convo)


def _style(html):
    import re

    return re.search(r"<style>(.*?)</style>", html, re.S).group(1)


def _narrow_block(css):
    import re

    return re.search(r"@media \(max-width:480px\)\{(.*)\}\s*$", css, re.S).group(1)


class TestPhoneWidth(unittest.TestCase):
    """The dashboard at phone width: content visible on first paint,
    hero and stats readable in 342px, booking lines scannable, and
    the greeting shown at once instead of typed out."""

    def test_above_fold_has_no_load_animation(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=True)
            css = _style(studio.dashboard_html())
            import re

            for selector in ("header", ".tabs", ".hero", ".bot"):
                rule = re.search(
                    r"{}\{{[^}}]*\}}".format(re.escape(selector)), css
                ).group(0)
                self.assertNotIn("animation", rule, selector)

    def test_scroll_reveal_covers_only_cards_rows_and_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=True)
            html = studio.dashboard_html()
            css = _style(html)
            import re

            reveal = re.search(
                r"@supports \(animation-timeline:view\(\)\)\{(.*)\}",
                css,
            ).group(1)
            self.assertIn(".card,.list li,.thread{", reveal)
            for gated in ("h2", ".nums", ".alerts", ".insight", ".allclear"):
                self.assertNotIn(gated, reveal, gated)
            self.assertIn("animation-timeline", html)

    def test_phone_block_shrinks_hero_stats_and_greeting(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=True)
            narrow = _narrow_block(_style(studio.dashboard_html()))
            self.assertIn(".hero .money{font-size:48px}", narrow)
            self.assertIn(".hero .today{font-size:18px", narrow)
            self.assertIn(
                ".nums{display:grid;grid-template-columns:repeat(3,1fr)",
                narrow,
            )
            self.assertIn(".num .lbl{font-size:11px", narrow)
            self.assertIn(".greeting .type{animation:none;max-width:none}", narrow)
            self.assertIn(".greeting .type::after{display:none}", narrow)
            self.assertIn("button.inline{min-height:44px}", narrow)

    def test_booking_rows_split_into_three_scannable_lines(self):
        from whatsapp_studio.bookings import india_today

        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=True)
            studio.bookings.book(
                "919810000001",
                "Priya",
                studio.profile.services[1],
                india_today(),
                11 * 60,
            )
            html = studio.dashboard_html("bookings")
            import re

            rows = re.findall(r"<li>(.*?)</li>", html, re.S)
            booked = [row for row in rows if "tel:+" in row]
            self.assertTrue(booked)
            for row in booked:
                self.assertEqual(row.count("class='b-line'"), 3)
            self.assertIn("11:00 AM", html)
            self.assertIn("Facial", html)
            self.assertIn("Priya", html)
            self.assertIn("Rs 1499", html)
            self.assertIn("tel:+919810000001", html)
            self.assertIn("Reject", html)

    def test_heading_date_stays_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=True)
            html = studio.dashboard_html("bookings")
            self.assertIn("Bookings today", html)
            self.assertIn(
                "Bookings today <span class='nowrap'>(", html
            )
            self.assertIn("Tomorrow <span class='nowrap'>(", html)
            self.assertIn(".nowrap{white-space:nowrap}", _style(html))
