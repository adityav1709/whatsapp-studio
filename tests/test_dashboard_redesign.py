"""Dashboard redesign, owner booking ping, and outbox persistence.

The redesign keeps every label the older tests pin ("Handoffs",
"Bookings today", "Resolve", "Sending: LIVE", "Stop sending",
"SEND FAILED", "Sending: PAUSED", "Resume sending", the escaped
business name, "4:00 PM") while changing how the page reads: a glance
strip first, people waiting as cards with tap-to-call links, plain
words instead of jargon. Time-dependent checks pin explicit times or
use "tomorrow", never the current hour.
"""
import json
import tempfile
import unittest
from html import escape
from pathlib import Path

from test_llm import C, FixedLLM, ScriptedLLM, T
from whatsapp_studio.app import Studio
from whatsapp_studio.bookings import BookingStore
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore


def make_profile():
    return BusinessProfile(
        name="Glow & Grace Salon",
        category="salon",
        city="Gurgaon",
        phone="919999999999",
        hours={"daily": "10:00 AM - 8:00 PM"},
        services=[Service(name="Haircut", price=499, duration_min=30),
                  Service(name="Facial", price=1499, duration_min=60)],
        address="Sector 50, Gurgaon",
    )


class FakeNotifier:
    """Records owner pings, never touches a network."""

    def __init__(self):
        self.calls = []
        self.booking_calls = []
        self.send_failed_calls = []

    def notify(self, business_name, handoff):
        self.calls.append((business_name, handoff))

    def notify_booking(self, business_name, booking):
        self.booking_calls.append((business_name, booking))

    def notify_send_failed(self, business_name, entry):
        self.send_failed_calls.append((business_name, entry))


class RaisingNotifier:
    """A notifier that always fails: the booking must still go through."""

    def notify(self, business_name, handoff):
        raise RuntimeError("telegram down")

    def notify_booking(self, business_name, booking):
        raise RuntimeError("telegram down")


class FakeSender:
    """Stand-in sender: records calls, never touches a network."""

    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    def send(self, to, body):
        self.calls.append((to, body))
        if self.fail_with is not None:
            raise self.fail_with
        return f"wamid.sent.{len(self.calls)}"


def make_studio(tmp, notifier=None, sender=None, outbox_path=None, llm=None):
    return Studio(
        make_profile(),
        LeadStore(Path(tmp) / "leads.json"),
        bookings=BookingStore(),
        handoffs=HandoffStore(),
        notifier=notifier if notifier is not None else FakeNotifier(),
        sender=sender,
        outbox_path=outbox_path,
        llm=llm if llm is not None else FixedLLM("Thanks for messaging! We will help you shortly."),
    )


def _hand_over_turns():
    return [
        T("hand_over", {"reason": "asked for a person"}),
        C("I've passed this to the team. They will reply here within 30 minutes."),
    ]


class TestDashboardRedesign(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notifier = FakeNotifier()
        self.studio = make_studio(self.tmp.name, notifier=self.notifier)

    def tearDown(self):
        self.tmp.cleanup()

    def test_viewport_meta_tag_present(self):
        html = self.studio.dashboard_html()
        self.assertIn("width=device-width, initial-scale=1", html)

    def test_glance_numbers_match_data(self):
        from whatsapp_studio.bookings import india_today

        self.studio.llm = ScriptedLLM(
            [
                C("Facial is Rs 1499 for 60 minutes."),
                *_hand_over_turns(),
            ]
        )
        self.studio.handle_message("919810000001", "facial ka price kya hai?", "Rahul")
        self.studio.handle_message(
            "919810000002", "can I talk to a person please", "Priya"
        )
        self.studio.bookings.book(
            "919810000003", "Asha", make_profile().services[0], india_today(), 16 * 60
        )
        html = self.studio.dashboard_html()
        self.assertIn("Today: 2 people messaged, 1 booked, 1 waiting for you.", html)
        self.assertIn("Rs 499", html)
        self.assertEqual(html.count("<span class='big'>1</span>"), 2)
        self.assertEqual(html.count("<span class='big'>2</span>"), 1)

    def test_all_clear_when_no_handoffs(self):
        html = self.studio.dashboard_html()
        self.assertIn("All clear", html)
        self.assertIn("nobody is waiting for you", html)

    def test_open_handoff_renders_card_with_tel_and_resolve(self):
        self.studio.llm = ScriptedLLM(_hand_over_turns())
        self.studio.handle_message(
            "919810000001", "can I talk to a person please", "Rahul"
        )
        self.studio.handle_message("919810000001", "my booking is wrong", "Rahul")
        # The waiting cards live on the Customers tab now, not on Today.
        html = self.studio.dashboard_html("customers")
        self.assertIn("People waiting for you", html)
        self.assertIn("Handoffs", html)
        self.assertIn("tel:+919810000001", html)
        self.assertIn("can I talk to a person please", html)
        self.assertIn("my booking is wrong", html)
        self.assertIn("Resolve", html)
        # The full thread lives on the Conversation tab.
        thread_html = self.studio.dashboard_html("conversation")
        self.assertIn("Conversation", thread_html)
        self.assertIn("class='msg in'", thread_html)
        self.assertIn("class='msg out'", thread_html)
        confirmation = escape(self.studio.outbox[0]["body"])
        self.assertLess(
            thread_html.index("can I talk to a person please"),
            thread_html.index(confirmation),
        )
        self.assertLess(
            thread_html.index(confirmation),
            thread_html.index("my booking is wrong"),
        )

    def test_no_unescaped_customer_text(self):
        self.studio.handle_message(
            "919810000001",
            "facial ka price kya hai? <img src=x onerror=alert(2)>",
            "<script>alert(1)</script>",
        )
        html = self.studio.dashboard_html()
        # No raw markup anywhere on the Today tab.
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        # The crafted name and message show on the Customers tab,
        # escaped, never as live markup.
        customers = self.studio.dashboard_html("customers")
        self.assertNotIn("<script>", customers)
        self.assertNotIn("<img", customers)
        self.assertIn("&lt;script&gt;", customers)
        self.assertIn("&lt;img", customers)

    def test_page_stays_small_row_caps(self):
        base = 1780000000
        for i in range(60):
            lead = self.studio.leads.record_message(
                f"91981000{i:04d}", f"hello {i}", None, f"Person {i}"
            )
            lead["last_seen"] = base + i
        self.studio.leads._save()
        html = self.studio.dashboard_html("customers")
        self.assertNotIn("919810000000", html)
        self.assertIn("919810000059", html)
        self.assertIn("Showing the latest 5.", html)

    def test_failed_send_is_loud_in_plain_words(self):
        sender = FakeSender(fail_with=RuntimeError("meta down"))
        studio = make_studio(self.tmp.name, sender=sender)
        studio.handle_message("919810000001", "facial ka price kya hai?", "Rahul")
        studio.wait_for_sends()
        html = studio.dashboard_html()
        self.assertIn("Sending: LIVE", html)
        self.assertIn("Stop sending", html)
        # The Today tab says it plainly and points at the phone.
        self.assertIn("did not get the reply", html)
        # The Replies tab carries the full failure, pinned label and
        # the error the send came back with.
        replies = studio.dashboard_html("replies")
        self.assertIn("SEND FAILED", replies)
        self.assertIn("did not reach the customer", replies)
        self.assertIn("meta down", replies)


class TestConversationThread(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notifier = FakeNotifier()
        self.studio = make_studio(self.tmp.name, notifier=self.notifier)

    def tearDown(self):
        self.tmp.cleanup()

    def _drive_thread(self):
        """A customer asks a price, gets an answer, asks for a person,
        gets the handoff confirmation, then writes again while waiting."""
        self.studio.llm = ScriptedLLM(
            [
                C("Facial is Rs 1499 for 60 minutes."),
                *_hand_over_turns(),
            ]
        )
        self.studio.handle_message(
            "919810000001", "facial ka price kya hai?", "Rahul"
        )
        self.studio.handle_message(
            "919810000001", "can I talk to a person please", "Rahul"
        )
        self.studio.handle_message("919810000001", "my booking is wrong", "Rahul")

    def test_thread_shows_full_history_in_order(self):
        self._drive_thread()
        html = self.studio.dashboard_html("conversation")
        price_reply = self.studio.outbox[0]["body"]
        confirmation = self.studio.outbox[1]["body"]
        texts = [
            escape(t)
            for t in [
                "facial ka price kya hai?",
                price_reply,
                "can I talk to a person please",
                confirmation,
                "my booking is wrong",
            ]
        ]
        positions = [html.index(t) for t in texts]
        self.assertEqual(positions, sorted(positions))

    def test_thread_marks_customer_and_business_sides_and_times(self):
        self._drive_thread()
        html = self.studio.dashboard_html("conversation")
        self.assertEqual(html.count("class='msg in'"), 3)
        self.assertEqual(html.count("class='msg out'"), 2)
        self.assertEqual(html.count("class='mtime'"), 5)

    def test_outbox_entries_carry_a_timestamp(self):
        import time

        before = int(time.time())
        self.studio.handle_message(
            "919810000001", "facial ka price kya hai?", "Rahul"
        )
        after = int(time.time())
        entry = self.studio.outbox[0]
        self.assertIn("ts", entry)
        self.assertIsInstance(entry["ts"], int)
        self.assertGreaterEqual(entry["ts"], before)
        self.assertLessEqual(entry["ts"], after)

    def test_voice_note_reply_carries_a_timestamp(self):
        entry = self.studio.handle_non_text_message(
            "919810000001", "audio", "Rahul"
        )
        self.assertIn("ts", entry)
        self.assertIsInstance(entry["ts"], int)

    def test_thread_without_timestamps_uses_handoff_time(self):
        self.studio.llm = ScriptedLLM(_hand_over_turns())
        self.studio.handle_message(
            "919810000001", "can I talk to a person please", "Rahul"
        )
        self.studio.outbox[0].pop("ts")
        thread = self.studio._conversation_thread("919810000001")
        self.assertEqual(
            [(m["side"], m["text"]) for m in thread],
            [
                ("in", "can I talk to a person please"),
                ("out", self.studio.outbox[0]["body"]),
            ],
        )

    def test_thread_without_timestamps_keeps_outbox_order(self):
        self.studio.llm = ScriptedLLM(_hand_over_turns())
        self.studio.handle_message(
            "919810000001", "can I talk to a person please", "Rahul"
        )
        self.studio.outbox[0].pop("ts")
        self.studio.outbox.append(
            {"to": "919810000001", "body": "first follow up", "intent": "price"}
        )
        self.studio.outbox.append(
            {"to": "919810000001", "body": "second follow up", "intent": "price"}
        )
        thread = self.studio._conversation_thread("919810000001")
        self.assertEqual(
            [m["text"] for m in thread if m["side"] == "out"],
            [
                self.studio.outbox[0]["body"],
                "first follow up",
                "second follow up",
            ],
        )


class TestSendFailedPing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notifier = FakeNotifier()

    def tearDown(self):
        self.tmp.cleanup()

    def test_failed_send_pings_owner_once(self):
        sender = FakeSender(fail_with=RuntimeError("meta down"))
        studio = make_studio(
            self.tmp.name, notifier=self.notifier, sender=sender
        )
        studio.handle_message("919810000001", "facial ka price kya hai?", "Rahul")
        studio.wait_for_sends()
        self.assertEqual(len(self.notifier.send_failed_calls), 1)
        business, entry = self.notifier.send_failed_calls[0]
        self.assertEqual(business, "Glow & Grace Salon")
        self.assertEqual(entry["to"], "919810000001")
        self.assertEqual(entry["send_error"], "meta down")

    def test_successful_send_does_not_ping(self):
        sender = FakeSender()
        studio = make_studio(
            self.tmp.name, notifier=self.notifier, sender=sender
        )
        studio.handle_message("919810000001", "facial ka price kya hai?", "Rahul")
        studio.wait_for_sends()
        self.assertEqual(len(self.notifier.send_failed_calls), 0)


class TestMoneyTomorrowWeek(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notifier = FakeNotifier()
        self.studio = make_studio(self.tmp.name, notifier=self.notifier)

    def tearDown(self):
        self.tmp.cleanup()

    def test_money_tomorrow_and_week_match_data(self):
        from datetime import timedelta

        from whatsapp_studio.bookings import india_today

        profile = make_profile()
        today = india_today()
        tomorrow = today + timedelta(days=1)
        haircut, facial = profile.services[0], profile.services[1]
        self.studio.bookings.book("919810000001", "Rahul", haircut, today, 10 * 60)
        self.studio.bookings.book("919810000002", "Priya", facial, today, 11 * 60)
        self.studio.bookings.book("919810000003", "Asha", facial, tomorrow, 10 * 60)
        html = self.studio.dashboard_html("bookings")
        self.assertIn("Rs 1998", html)
        self.assertIn("Facial", html)
        self.assertIn("Tomorrow", html)
        # Tomorrow's booking is in the week line only when tomorrow is
        # still in this calendar week (Mon-Sun).
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        in_week = monday <= tomorrow <= sunday
        week_count = 3 if in_week else 2
        week_rupees = 3497 if in_week else 1998
        self.assertIn(f"This week: {week_count} bookings, Rs {week_rupees}.", html)


class TestBookingPing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notifier = FakeNotifier()
        self.studio = make_studio(self.tmp.name, notifier=self.notifier)

    def tearDown(self):
        self.tmp.cleanup()

    def test_confirmed_booking_pings_owner_once(self):
        self.studio.llm = ScriptedLLM(
            [
                T(
                    "create_booking",
                    {"service": "Haircut", "day": "tomorrow", "time": "11am"},
                ),
                C("Done! Your Haircut is booked for tomorrow at 11:00 AM."),
            ]
        )
        reply = self.studio.handle_message(
            "919810000001", "book haircut tomorrow 11am", "Rahul"
        )["body"]
        self.assertIn("11:00 AM", reply)
        self.assertEqual(len(self.notifier.booking_calls), 1)
        business, booking = self.notifier.booking_calls[0]
        self.assertEqual(business, "Glow & Grace Salon")
        self.assertEqual(booking["service"], "Haircut")
        self.assertEqual(booking["price"], 499)
        self.assertEqual(booking["phone"], "919810000001")

    def test_double_booking_and_cancel_do_not_ping(self):
        self.studio.llm = ScriptedLLM(
            [
                T(
                    "create_booking",
                    {"service": "Haircut", "day": "tomorrow", "time": "11am"},
                ),
                C("Done! Your Haircut is booked for tomorrow at 11:00 AM."),
                T(
                    "create_booking",
                    {"service": "Haircut", "day": "tomorrow", "time": "11am"},
                ),
                C("Sorry, 11:00 AM tomorrow is already taken."),
                T("cancel_booking", {}),
                C("Your booking is cancelled."),
            ]
        )
        self.studio.handle_message("919810000001", "book haircut tomorrow 11am", "Rahul")
        self.assertEqual(len(self.notifier.booking_calls), 1)
        reply = self.studio.handle_message(
            "919810000002", "book haircut tomorrow 11am", "Asha"
        )["body"]
        self.assertIn("taken", reply.lower())
        self.assertEqual(len(self.notifier.booking_calls), 1)
        self.studio.handle_message("919810000001", "cancel my booking", "Rahul")
        self.assertEqual(len(self.notifier.booking_calls), 1)

    def test_booking_ping_failure_never_breaks_booking(self):
        studio = make_studio(
            self.tmp.name,
            notifier=RaisingNotifier(),
            llm=ScriptedLLM(
                [
                    T(
                        "create_booking",
                        {"service": "Haircut", "day": "tomorrow", "time": "11am"},
                    ),
                    C("Done! Your Haircut is booked for tomorrow at 11:00 AM."),
                ]
            ),
        )
        reply = studio.handle_message(
            "919810000001", "book haircut tomorrow 11am", "Rahul"
        )["body"]
        self.assertIn("11:00 AM", reply)
        self.assertEqual(len(studio.bookings.all_bookings()), 1)


class TestOutboxPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.outbox_path = Path(self.tmp.name) / "outbox.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_outbox_survives_restart(self):
        studio = make_studio(self.tmp.name, outbox_path=self.outbox_path)
        studio.handle_message("919810000001", "facial ka price kya hai?", "Rahul")
        self.assertEqual(len(studio.outbox), 1)
        self.assertTrue(self.outbox_path.exists())
        reopened = make_studio(self.tmp.name, outbox_path=self.outbox_path)
        self.assertEqual(len(reopened.outbox), 1)
        self.assertEqual(reopened.outbox[0]["body"], studio.outbox[0]["body"])
        self.assertEqual(reopened.outbox[0]["to"], "919810000001")

    def test_outbox_capped_at_100(self):
        studio = make_studio(self.tmp.name, outbox_path=self.outbox_path)
        studio.outbox = [
            {"to": f"91981000{i:04d}", "body": f"reply {i}", "intent": "price"}
            for i in range(120)
        ]
        studio._save_outbox()
        reopened = make_studio(self.tmp.name, outbox_path=self.outbox_path)
        self.assertEqual(len(reopened.outbox), 100)
        self.assertEqual(reopened.outbox[0]["body"], "reply 20")
        self.assertEqual(reopened.outbox[-1]["body"], "reply 119")

    def test_outbox_in_memory_without_path(self):
        studio = make_studio(self.tmp.name)
        studio.handle_message("919810000001", "facial ka price kya hai?", "Rahul")
        self.assertEqual(len(studio.outbox), 1)
        reopened = make_studio(self.tmp.name)
        self.assertEqual(len(reopened.outbox), 0)

    def test_corrupt_outbox_file_does_not_break_startup(self):
        self.outbox_path.write_text("not json at all", encoding="utf-8")
        studio = make_studio(self.tmp.name, outbox_path=self.outbox_path)
        self.assertEqual(studio.outbox, [])
        studio.handle_message("919810000001", "facial ka price kya hai?", "Rahul")
        self.assertEqual(len(studio.outbox), 1)


if __name__ == "__main__":
    unittest.main()
