"""The Replies tab: where the owner writes a reply, drafts one on request.

The bot auto replies otherwise; a draft is only generated when the owner
asks for one, and a draft is never sent by itself. With no LLM key the
page says so plainly instead of offering a draft.
"""
import http.client
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from test_app import FakeNotifier, FakeSender
from test_llm import C, FixedLLM, RaisingLLM, ScriptedLLM, T
from whatsapp_studio.app import Studio, make_server
from whatsapp_studio.bookings import BookingStore
from whatsapp_studio.config import BusinessProfile
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore


class TestRepliesTab(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notifier = FakeNotifier()
        self.sender = FakeSender()
        profile = BusinessProfile(
            name="Glow & Grace Salon",
            category="salon",
            city="Gurgaon",
            phone="919999999999",
            hours={"daily": "10:00 AM - 8:00 PM"},
            services=[],
            address="Sector 50, Gurgaon",
        )
        self.studio = Studio(
            profile,
            LeadStore(Path(self.tmp.name) / "leads.json"),
            bookings=BookingStore(),
            handoffs=HandoffStore(),
            notifier=self.notifier,
            sender=self.sender,
            llm=ScriptedLLM(
                [
                    T("hand_over", {"reason": "asked for a person"}),
                    C("I've passed this to the team. They will reply here within 30 minutes."),
                ]
            ),
        )
        self.studio.handle_message(
            "919810000001", "can I talk to a person please", "Rahul"
        )
        self.httpd = make_server(self.studio, port=0)
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

    def test_reply_form_lives_on_replies_tab_not_today(self):
        today = self.studio.dashboard_html()
        self.assertNotIn("Send reply", today)
        self.assertNotIn("Draft a reply", today)
        replies = self.studio.dashboard_html("replies")
        self.assertIn("Reply to a customer", replies)
        self.assertIn("Send reply", replies)
        self.assertIn("Draft a reply", replies)
        self.assertIn("can I talk to a person please", replies)
        # The failures list still has its place on the same tab.
        self.assertIn("Replies the bot sent", replies)
        self.assertIn("All replies went through.", replies)
        # And the Conversation tab is only the thread, no reply form.
        conversation = self.studio.dashboard_html("conversation")
        self.assertNotIn("Send reply", conversation)
        self.assertNotIn("Draft a reply", conversation)

    def test_draft_prefills_reply_box_and_sends_nothing(self):
        self.studio.llm = FixedLLM("Hi Rahul, sure, let me check a slot for you.")
        sent_before = len(self.sender.calls)
        outbox_before = len(self.studio.outbox)
        status, location = self._post("/handoffs/1/draft")
        self.assertEqual(status, 303)
        self.assertEqual(location, "/?tab=replies")
        # The draft went nowhere: no new send, no new outbox entry,
        # and the handoff is still open.
        self.assertEqual(len(self.sender.calls), sent_before)
        self.assertEqual(len(self.studio.outbox), outbox_before)
        self.assertIsNotNone(self.studio.handoffs.open_for("919810000001"))
        # The draft only prefills the reply box on the Replies tab.
        _, replies = self._get("/?tab=replies")
        self.assertIn(
            "value='Hi Rahul, sure, let me check a slot for you.'", replies
        )
        # Drafting asked the brain in words only: no tools, so it could
        # not book, cancel, or change anything.
        call = self.studio.llm.calls[-1]
        self.assertIsNone(call["tools"])
        self.assertIn("draft", call["messages"][0]["content"].lower())

    def test_draft_with_no_key_says_so_plainly(self):
        self.studio.llm = None
        replies = self.studio.dashboard_html("replies")
        self.assertIn("Drafts need an LLM key", replies)
        self.assertNotIn("Draft a reply", replies)
        sent_before = len(self.sender.calls)
        status, location = self._post("/handoffs/1/draft")
        self.assertEqual(status, 303)
        self.assertEqual(location, "/?tab=replies")
        self.assertNotIn(1, self.studio._drafts)
        self.assertEqual(len(self.sender.calls), sent_before)
        self.assertIsNotNone(self.studio.handoffs.open_for("919810000001"))

    def test_draft_failure_says_so_and_sends_nothing(self):
        self.studio.llm = RaisingLLM()
        sent_before = len(self.sender.calls)
        status, location = self._post("/handoffs/1/draft")
        self.assertEqual(status, 303)
        self.assertEqual(location, "/?tab=replies")
        _, replies = self._get("/?tab=replies")
        self.assertIn("Could not draft a reply right now.", replies)
        self.assertEqual(len(self.sender.calls), sent_before)
        self.assertIsNotNone(self.studio.handoffs.open_for("919810000001"))

    def test_sending_the_draft_clears_it(self):
        self.studio.llm = FixedLLM("Hi Rahul, sure, let me check a slot for you.")
        self._post("/handoffs/1/draft")
        _, replies = self._get("/?tab=replies")
        self.assertIn("value='Hi Rahul, sure, let me check a slot for you.'", replies)
        status, location = self._post(
            "/handoffs/1/reply",
            {"text": "Hi Rahul, sure, let me check a slot for you."},
        )
        self.assertEqual(status, 303)
        self.assertNotIn(1, self.studio._drafts)
        _, replies = self._get("/?tab=replies")
        self.assertIn("Nobody is waiting.", replies)
        self.assertNotIn("value='Hi Rahul, sure, let me check a slot for you.'", replies)

    def test_draft_for_missing_handoff_is_404(self):
        status, _ = self._post("/handoffs/99/draft")
        self.assertEqual(status, 404)

    def test_draft_reply_returns_none_without_key_or_messages(self):
        no_key = Studio(
            self.studio.profile,
            LeadStore(Path(self.tmp.name) / "leads2.json"),
            llm=None,
        )
        self.assertIsNone(no_key.draft_reply("919810000001"))
        with_key = Studio(
            self.studio.profile,
            LeadStore(Path(self.tmp.name) / "leads3.json"),
            llm=FixedLLM("hello"),
        )
        self.assertIsNone(with_key.draft_reply("919810000001"))


if __name__ == "__main__":
    unittest.main()
