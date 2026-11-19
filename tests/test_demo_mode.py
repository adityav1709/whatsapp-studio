import tempfile
import unittest
from pathlib import Path

from whatsapp_studio.app import Studio
from whatsapp_studio.bookings import BookingStore, india_today
from whatsapp_studio.config import BusinessProfile
from whatsapp_studio.demo_data import SAMPLE_PREFIX, seed_demo_data
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore

ROOT = Path(__file__).resolve().parents[1]


def make_studio(tmp, demo_mode=False, llm=None):
    profile = BusinessProfile.from_json(ROOT / "demo" / "demo_business.json")
    return Studio(
        profile,
        LeadStore(Path(tmp) / "leads.json"),
        bookings=BookingStore(Path(tmp) / "bookings.json"),
        handoffs=HandoffStore(Path(tmp) / "handoffs.json"),
        outbox_path=Path(tmp) / "outbox.json",
        llm=llm,
        demo_mode=demo_mode,
    )


class TestDemoMode(unittest.TestCase):
    def test_demo_mode_shows_bot_on_and_sample_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=True)
            html = studio.dashboard_html()
            self.assertIn("Bot is ON", html)
            self.assertIn("Demo", html)
            self.assertIn("Sample data", html)
            self.assertNotIn("Bot is OFF", html)
            self.assertNotIn("needs an LLM key", html)
            self.assertNotIn("No WhatsApp number is connected", html)
            self.assertEqual(len(studio.leads.all_leads()), 6)
            self.assertEqual(
                len([h for h in studio.handoffs.all_handoffs() if h["status"] == "open"]),
                2,
            )
            self.assertEqual(len(studio.bookings.bookings_on(india_today())), 2)
            self.assertIn("Rs 1498", html)
            self.assertEqual(len(studio.outbox), 7)
            self.assertTrue(
                all(e.get("send_status") == "recorded" for e in studio.outbox)
            )
            # The mock extras ride along with the sample day: one
            # regular, one booking made after closing, two people who
            # left with nothing. Today stays exactly as before.
            self.assertEqual(len(studio.bookings.all_bookings()), 5)
            self.assertEqual(len(studio.bookings.regulars()), 1)
            self.assertEqual(len(studio.missed.open_newest()), 2)

    def test_demo_mode_thread_is_a_full_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=True)
            thread = studio._conversation_thread("919810000003")
            self.assertEqual(
                [m["side"] for m in thread], ["in", "out", "in"]
            )
            self.assertEqual(thread[0]["text"], "can I talk to a person please")
            self.assertIn("passed this to the team", thread[1]["text"])
            self.assertEqual(thread[2]["text"], "it is about a refund for last week")
            html = studio.dashboard_html(tab="conversation")
            self.assertIn("can I talk to a person please", html)
            self.assertIn("passed this to the team", html)

    def test_every_sample_number_is_in_the_fake_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=True)
            phones = {lead["phone"] for lead in studio.leads.all_leads()}
            phones |= {entry["to"] for entry in studio.outbox}
            phones |= {b["phone"] for b in studio.bookings.all_bookings()}
            self.assertTrue(phones)
            for phone in phones:
                self.assertTrue(
                    phone.startswith(SAMPLE_PREFIX),
                    f"{phone} is not a fake sample number",
                )

    def test_demo_mode_off_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=False)
            html = studio.dashboard_html()
            self.assertIn("needs an LLM key", html)
            self.assertIn("Bot is OFF", html)
            self.assertNotIn("Demo", html)
            self.assertEqual(studio.leads.all_leads(), [])
            self.assertEqual(studio.bookings.all_bookings(), [])
            self.assertEqual(studio.handoffs.all_handoffs(), [])
            self.assertEqual(studio.outbox, [])

    def test_no_seed_when_data_already_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = make_studio(tmp, demo_mode=False)
            first.leads.record_message("919999999999", "hello", None, "Real")
            second = make_studio(tmp, demo_mode=True)
            self.assertEqual(len(second.leads.all_leads()), 1)
            self.assertEqual(second.leads.all_leads()[0]["phone"], "919999999999")
            self.assertEqual(second.outbox, [])
            self.assertEqual(second.bookings.all_bookings(), [])

    def test_seeding_twice_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = make_studio(tmp, demo_mode=True)
            second = make_studio(tmp, demo_mode=True)
            self.assertEqual(len(second.leads.all_leads()), 6)
            self.assertEqual(
                len([h for h in second.handoffs.all_handoffs() if h["status"] == "open"]),
                2,
            )
            self.assertEqual(len(second.bookings.all_bookings()), 5)
            self.assertEqual(len(second.outbox), 7)
            self.assertIs(first.demo_mode, True)
            self.assertIs(second.demo_mode, True)

    def test_seed_function_reports_whether_it_seeded(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = make_studio(tmp, demo_mode=False)
            self.assertTrue(seed_demo_data(studio))
            self.assertFalse(seed_demo_data(studio))


if __name__ == "__main__":
    unittest.main()
