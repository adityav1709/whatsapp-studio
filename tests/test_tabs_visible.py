"""Tabs stay visible on a phone.

Aditya opened the live site on his phone and the tabs were not on
the page: the tab bar was a horizontally scrolling pill row, so
Replies and Conversation slid off the right edge with no cue. The
bar now wraps, so all five tabs are on the page at once, on every
tab page, with the current one marked.
"""
import re
import tempfile
import unittest
from pathlib import Path

from whatsapp_studio.app import Studio
from whatsapp_studio.bookings import BookingStore
from whatsapp_studio.config import BusinessProfile
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore

ROOT = Path(__file__).resolve().parents[1]

TABS = (
    ("today", "Today"),
    ("bookings", "Bookings"),
    ("customers", "Customers"),
    ("replies", "Replies"),
    ("conversation", "Conversation"),
)


def make_studio(tmp):
    profile = BusinessProfile.from_json(ROOT / "demo" / "demo_business.json")
    return Studio(
        profile,
        LeadStore(Path(tmp) / "leads.json"),
        bookings=BookingStore(Path(tmp) / "bookings.json"),
        handoffs=HandoffStore(Path(tmp) / "handoffs.json"),
        outbox_path=Path(tmp) / "outbox.json",
        demo_mode=True,
    )


def style(html):
    return re.search(r"<style>(.*?)</style>", html, re.S).group(1)


class TestTabsVisible(unittest.TestCase):
    def test_all_five_tabs_on_every_tab_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp)
            for tab, _ in TABS:
                html = studio.dashboard_html(tab)
                for name, label in TABS:
                    self.assertIn("href='/?tab={}'".format(name), html)
                    self.assertIn(">{}</a>".format(label), html)

    def test_one_active_tab_matching_the_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp)
            for tab, _ in TABS:
                html = studio.dashboard_html(tab)
                active = re.findall(r"class='active'[^>]*>([^<]+)</a>", html)
                self.assertEqual(len(active), 1, tab)
                self.assertEqual(active[0], dict(TABS)[tab])
                self.assertEqual(html.count("aria-current='page'"), 1)

    def test_tab_bar_wraps_instead_of_scrolling(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp)
            css = style(studio.dashboard_html())
            tabs_rule = re.search(r"\.tabs\{[^}]*\}", css).group(0)
            self.assertIn("flex-wrap:wrap", tabs_rule)
            self.assertNotIn("overflow-x", css)
            tab_link_rule = re.search(r"\.tabs a\{[^}]*\}", css).group(0)
            self.assertNotIn("flex-shrink", tab_link_rule)
            narrow = re.search(
                r"@media \(max-width:480px\)\{(.*)\}\s*$", css, re.S
            ).group(1)
            self.assertIn(".tabs a{padding:9px 14px", narrow)
