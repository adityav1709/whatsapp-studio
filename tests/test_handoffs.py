import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock

from test_llm import C, FixedLLM, RaisingLLM, ScriptedLLM, T
from whatsapp_studio.app import Studio, handoff_confirmation, make_server
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.notify import TelegramNotifier


def make_profile():
    return BusinessProfile(
        name="Glow & Grace Salon",
        category="salon",
        city="Gurgaon",
        phone="919999999999",
        hours={"daily": "10:00 AM - 8:00 PM"},
        services=[Service(name="Haircut", price=499, duration_min=30)],
        address="Sector 50, Gurgaon",
    )


class FakeNotifier:
    """Stand-in for the owner notifier: records calls, never touches a network."""

    def __init__(self):
        self.calls = []

    def notify(self, business_name, handoff):
        self.calls.append((business_name, handoff))


def make_studio(tmp, llm=None):
    notifier = FakeNotifier()
    studio = Studio(
        make_profile(),
        LeadStore(Path(tmp) / "leads.json"),
        handoffs=HandoffStore(),
        notifier=notifier,
        llm=llm if llm is not None else ScriptedLLM([]),
    )
    return studio, notifier


def _hand_over_turns():
    return [
        T("hand_over", {"reason": "asked for a person"}),
        C("I've passed this to the team. They will reply here within 30 minutes."),
    ]


class TestHandoffFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio, self.notifier = make_studio(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_explicit_ask_opens_handoff(self):
        self.studio.llm = ScriptedLLM(_hand_over_turns())
        reply = self.studio.handle_message(
            "919810000001",
            "can I talk to a person please",
            "Rahul",
            now=datetime(2026, 11, 15, 11, 0),
        )
        self.assertIsNotNone(reply["body"])
        self.assertIn("passed", reply["body"])
        handoff = self.studio.handoffs.open_for("919810000001")
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff["reason"], "asked for a person")
        self.assertEqual(handoff["trigger"], "can I talk to a person please")
        self.assertEqual(handoff["status"], "open")
        self.assertEqual(len(self.notifier.calls), 1)

    def test_llm_failure_opens_handoff(self):
        # The model is down: the customer still hears back, the owner
        # is pinged, and the handoff says why it opened. Fixed
        # open-hours time: the confirmation wording depends on it,
        # and the suite must pass whatever hour it runs in.
        self.studio.llm = RaisingLLM()
        now = datetime(2026, 11, 15, 11, 0)
        reply = self.studio.handle_message("919810000001", "asdfgh", "Rahul", now=now)
        handoff = self.studio.handoffs.open_for("919810000001")
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff["reason"], "couldn't reply")
        self.assertIn("couldn't quite get that", reply["body"])

    def test_one_normal_message_does_not_open_handoff(self):
        self.studio.llm = FixedLLM("Hello! How can I help?")
        self.studio.handle_message("919810000001", "asdfgh", "Rahul")
        self.assertIsNone(self.studio.handoffs.open_for("919810000001"))
        self.assertEqual(len(self.studio.outbox), 1)

    def test_no_replies_while_open(self):
        self.studio.llm = ScriptedLLM(_hand_over_turns())
        self.studio.handle_message(
            "919810000001", "can I talk to a person please", "Rahul"
        )
        self.assertEqual(len(self.studio.outbox), 1)
        paused = self.studio.handle_message(
            "919810000001", "book haircut tomorrow 11am", "Rahul"
        )
        self.assertIsNone(paused["body"])
        self.assertTrue(paused["paused"])
        self.studio.handle_message("919810000001", "hello??", "Rahul")
        self.assertEqual(len(self.studio.outbox), 1)
        self.assertEqual(self.studio.bookings.all_bookings(), [])
        lead = self.studio.leads.get("919810000001")
        self.assertEqual(lead["message_count"], 3)

    def test_second_ask_does_not_duplicate(self):
        self.studio.llm = ScriptedLLM(_hand_over_turns())
        self.studio.handle_message(
            "919810000001", "can I talk to a person please", "Rahul"
        )
        self.studio.handle_message("919810000001", "anyone there?", "Rahul")
        handoffs = [
            h for h in self.studio.handoffs.all_handoffs() if h["status"] == "open"
        ]
        self.assertEqual(len(handoffs), 1)

    def test_resolve_resumes_replies(self):
        self.studio.llm = ScriptedLLM(
            _hand_over_turns() + [C("We are open 10:00 AM to 8:00 PM.")]
        )
        self.studio.handle_message(
            "919810000001", "can I talk to a person please", "Rahul"
        )
        handoff = self.studio.handoffs.open_for("919810000001")
        self.assertIsNotNone(self.studio.handoffs.resolve(handoff["id"]))
        reply = self.studio.handle_message(
            "919810000001", "what are your timings?", "Rahul"
        )
        self.assertIn("10:00 AM", reply["body"])
        self.assertEqual(len(self.studio.outbox), 2)

    def test_handoff_survives_restart(self):
        path = Path(self.tmp.name) / "handoffs.json"
        store = HandoffStore(path)
        store.open("919810000001", "Rahul", "asked for a person", "talk to someone")
        reopened = HandoffStore(path)
        handoff = reopened.open_for("919810000001")
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff["reason"], "asked for a person")

    def test_open_hours_confirmation(self):
        reply = handoff_confirmation(
            make_profile(), "asked for a person", now=datetime(2026, 11, 15, 11, 0)
        )
        self.assertIn("30 minutes", reply)
        self.assertIn("shared our chat", reply)
        self.assertNotIn("Meanwhile", reply)

    def test_after_hours_confirmation_is_honest(self):
        reply = handoff_confirmation(
            make_profile(), "asked for a person", now=datetime(2026, 11, 15, 22, 0)
        )
        self.assertIn("closed", reply.lower())
        self.assertIn("10:00 AM", reply)
        self.assertIn("11:00 AM", reply)
        self.assertNotIn("30 minutes", reply)
        self.assertNotIn("quick", reply.lower())


class TestTelegramNotifier(unittest.TestCase):
    def test_posts_to_telegram(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        handoff = {
            "phone": "919810000001",
            "name": "Rahul",
            "reason": "asked for a person",
            "trigger": "talk to someone",
        }
        with mock.patch("whatsapp_studio.notify.urllib.request.urlopen", fake_urlopen):
            TelegramNotifier("tok123", "chat456").notify("Glow & Grace Salon", handoff)
        self.assertIn("api.telegram.org/bottok123/sendMessage", captured["url"])
        self.assertEqual(captured["body"]["chat_id"], "chat456")
        self.assertIn("919810000001", captured["body"]["text"])
        self.assertIn("talk to someone", captured["body"]["text"])

    def test_failure_is_swallowed(self):
        def failing_urlopen(request, timeout=None):
            raise urllib.error.URLError("no network")

        handoff = {
            "phone": "919810000001",
            "name": "Rahul",
            "reason": "asked for a person",
            "trigger": "talk to someone",
        }
        with mock.patch(
            "whatsapp_studio.notify.urllib.request.urlopen", failing_urlopen
        ):
            TelegramNotifier("tok123", "chat456").notify("Glow & Grace Salon", handoff)


class TestHandoffHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio, self.notifier = make_studio(self.tmp.name)
        self.httpd = make_server(self.studio, port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path):
        with urllib.request.urlopen(self._url(path)) as resp:
            return resp.status, resp.read().decode()

    def test_handoffs_endpoint_and_dashboard(self):
        self.studio.llm = ScriptedLLM(_hand_over_turns())
        entry = self.studio.handle_message(
            "919810000001", "can I talk to a person please", "Rahul"
        )
        self.studio.handle_message("919810000001", "my booking is wrong", "Rahul")
        status, body = self._get("/handoffs")
        self.assertEqual(status, 200)
        handoffs = json.loads(body)["handoffs"]
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0]["id"], entry["handoff"])
        self.assertEqual(handoffs[0]["status"], "open")
        status, body = self._get("/?tab=customers")
        self.assertEqual(status, 200)
        self.assertIn("Handoffs", body)
        self.assertIn("919810000001", body)
        self.assertIn("can I talk to a person please", body)
        self.assertIn("my booking is wrong", body)
        self.assertIn("Resolve", body)

    def test_resolve_via_form(self):
        self.studio.llm = ScriptedLLM(_hand_over_turns())
        entry = self.studio.handle_message(
            "919810000001", "can I talk to a person please", "Rahul"
        )
        req = urllib.request.Request(
            self._url("/handoffs/resolve"),
            data=f"handoff_id={entry['handoff']}".encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
        status, body = self._get("/handoffs")
        self.assertEqual(json.loads(body)["handoffs"][0]["status"], "resolved")

    def test_resolve_unknown_returns_404(self):
        req = urllib.request.Request(
            self._url("/handoffs/resolve"),
            data=b"handoff_id=999",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
