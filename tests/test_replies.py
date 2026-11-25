"""The Replies tab: where the owner writes a reply, drafts one on request.

The bot auto replies otherwise; a draft is only generated when the owner
asks for one, and a draft is never sent by itself. With no LLM key the
page says so plainly instead of offering a draft.
"""
import contextlib
import http.client
import io
import re
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
        self.assertIn("No draft for now.", replies)
        self.assertIn("ask Aditya to switch it on", replies)
        self.assertNotIn("Draft a reply", replies)
        self.assertNotIn("OpenAI API error", replies)
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
        self.assertIn("The draft did not come through.", replies)
        self.assertIn("Draft a reply", replies)
        self.assertEqual(len(self.sender.calls), sent_before)
        self.assertIsNotNone(self.studio.handoffs.open_for("919810000001"))

    def _failure_notes(self, html):
        notes = re.findall(r"<p class='quiet'>(.*?)</p>", html)
        return [n for n in notes if "draft" in n.lower()]

    def test_provider_error_says_try_again(self):
        # 401 is fixable by Aditya, 500 is not, but the owner sees the
        # same plain line for both: try again, and ask Aditya if it
        # keeps failing. The code lives in the log, not on the page.
        for error in (
            "OpenAI API error: HTTP 401",
            "OpenAI API error: HTTP 500",
            "Gemini API error: HTTP 429",
        ):
            self.studio.llm = RaisingLLM(RuntimeError(error))
            self._post("/handoffs/1/draft")
            _, replies = self._get("/?tab=replies")
            self.assertIn("The draft did not come through.", replies)
            self.assertIn("Draft a reply", replies)
            self.assertNotIn("API error", replies)
            self.studio._draft_failures.clear()

    def test_draft_failure_log_carries_the_provider_error(self):
        self.studio.llm = RaisingLLM(RuntimeError("OpenAI API error: HTTP 401"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertIsNone(self.studio.draft_reply("919810000001", 1))
        self.assertIn("kind=provider_error", buf.getvalue())
        self.assertIn("HTTP 401", buf.getvalue())
        self.assertEqual(self.studio._draft_failures[1]["kind"], "provider_error")

    def test_empty_reply_has_its_own_line(self):
        for content in ("", "   "):
            self.studio.llm = FixedLLM(content)
            self._post("/handoffs/1/draft")
            _, replies = self._get("/?tab=replies")
            self.assertIn("No draft this time.", replies)
            self.assertIn("Draft a reply", replies)
            self.assertNotIn(1, self.studio._drafts)
            self.studio._draft_failures.clear()

    def test_no_messages_failure_is_recorded(self):
        with_key = Studio(
            self.studio.profile,
            LeadStore(Path(self.tmp.name) / "leads3.json"),
            llm=FixedLLM("hello"),
        )
        self.assertIsNone(with_key.draft_reply("919810000001", 7))
        self.assertEqual(with_key._draft_failures[7]["kind"], "no_messages")

    def test_no_key_failure_is_recorded(self):
        no_key = Studio(
            self.studio.profile,
            LeadStore(Path(self.tmp.name) / "leads2.json"),
            llm=None,
        )
        self.assertIsNone(no_key.draft_reply("919810000001", 7))
        self.assertEqual(no_key._draft_failures[7]["kind"], "no_key")

    def test_second_failure_stops_offering_retry(self):
        self.studio.llm = RaisingLLM()
        self._post("/handoffs/1/draft")
        _, replies = self._get("/?tab=replies")
        self.assertIn("The draft did not come through.", replies)
        self.assertIn("Draft a reply", replies)
        self._post("/handoffs/1/draft")
        _, replies = self._get("/?tab=replies")
        self.assertIn("The AI is not answering.", replies)
        self.assertIn("let Aditya know drafts are not working", replies)
        self.assertNotIn("Draft a reply", replies)

    def test_different_kind_restarts_the_retry_count(self):
        self.studio.llm = FixedLLM("")
        self._post("/handoffs/1/draft")
        _, replies = self._get("/?tab=replies")
        self.assertIn("No draft this time.", replies)
        self.studio.llm = RaisingLLM()
        self._post("/handoffs/1/draft")
        _, replies = self._get("/?tab=replies")
        self.assertIn("The draft did not come through.", replies)
        self.assertIn("Draft a reply", replies)

    def test_success_clears_the_failure(self):
        self.studio.llm = RaisingLLM()
        self._post("/handoffs/1/draft")
        self.assertIn(1, self.studio._draft_failures)
        self.studio.llm = FixedLLM("Hi Rahul, let me check.")
        self._post("/handoffs/1/draft")
        _, replies = self._get("/?tab=replies")
        self.assertNotIn(1, self.studio._draft_failures)
        self.assertIn("value='Hi Rahul, let me check.'", replies)
        self.assertNotIn("The draft did not come through.", replies)

    def test_sending_a_reply_clears_the_failure(self):
        self.studio.llm = RaisingLLM()
        self._post("/handoffs/1/draft")
        self.assertIn(1, self.studio._draft_failures)
        self._post("/handoffs/1/reply", {"text": "Hi Rahul, sure."})
        self.assertNotIn(1, self.studio._draft_failures)

    def test_resolving_clears_the_failure(self):
        self.studio.llm = RaisingLLM()
        self._post("/handoffs/1/draft")
        self.assertIn(1, self.studio._draft_failures)
        status, _ = self._post("/handoffs/resolve", {"handoff_id": "1"})
        self.assertEqual(status, 303)
        self.assertNotIn(1, self.studio._draft_failures)

    def test_hostile_error_never_reaches_the_page(self):
        self.studio.llm = RaisingLLM(RuntimeError("boom <script>alert(1)</script>"))
        self._post("/handoffs/1/draft")
        _, replies = self._get("/?tab=replies")
        self.assertIn("The draft did not come through.", replies)
        self.assertNotIn("<script>", replies)
        self.assertNotIn("&lt;script&gt;", replies)
        self.assertNotIn("alert(1)", replies)

    def test_long_provider_error_is_truncated_in_the_record(self):
        self.studio.llm = RaisingLLM(RuntimeError("x" * 500))
        self.studio.draft_reply("919810000001", 1)
        self.assertLessEqual(len(self.studio._draft_failures[1]["detail"]), 120)

    def test_failure_notes_have_no_dash(self):
        # No key.
        self.studio.llm = None
        pages = [self.studio.dashboard_html("replies")]
        # Provider error, then a second one: the escalated line.
        self.studio.llm = RaisingLLM()
        self._post("/handoffs/1/draft")
        _, page = self._get("/?tab=replies")
        pages.append(page)
        self._post("/handoffs/1/draft")
        _, page = self._get("/?tab=replies")
        pages.append(page)
        # Empty reply.
        self.studio._draft_failures.clear()
        self.studio.llm = FixedLLM("")
        self._post("/handoffs/1/draft")
        _, page = self._get("/?tab=replies")
        pages.append(page)
        # Nothing to draft from: empty the lead's messages and record it.
        self.studio._draft_failures.clear()
        self.studio.leads.get("919810000001")["messages"] = []
        self.studio._draft_failures[1] = {
            "kind": "no_messages",
            "detail": "",
            "tries": 1,
        }
        pages.append(self.studio.dashboard_html("replies"))
        for page in pages:
            notes = self._failure_notes(page)
            self.assertTrue(notes, "expected a draft failure note on the page")
            for note in notes:
                self.assertNotIn("-", note)
                self.assertNotIn("\u2013", note)
                self.assertNotIn("\u2014", note)

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
