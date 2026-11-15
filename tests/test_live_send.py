import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from test_llm import FixedLLM
from whatsapp_studio.app import Studio, make_server
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.send import (
    GRAPH_API_VERSION,
    MAX_TEXT_CHARS,
    WhatsAppSender,
    sender_from_env,
)


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
    """Stand-in for the owner notifier: records calls, never touches a network."""

    def __init__(self):
        self.calls = []

    def notify(self, business_name, handoff):
        self.calls.append((business_name, handoff))


class FakeSender:
    """Stand-in for the WhatsApp sender: records calls, never touches a network."""

    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    def send(self, to, body):
        self.calls.append((to, body))
        if self.fail_with is not None:
            raise self.fail_with
        return f"wamid.sent.{len(self.calls)}"


def make_studio(tmp, sender=None, dry_run=False):
    return Studio(
        make_profile(),
        LeadStore(Path(tmp) / "leads.json"),
        handoffs=HandoffStore(),
        notifier=FakeNotifier(),
        sender=sender,
        dry_run=dry_run,
        llm=FixedLLM("Facial is Rs 1499 for 60 minutes."),
    )


def webhook_payload(phone, text, wamid=None):
    msg = {
        "from": phone,
        "text": {"body": text},
        "timestamp": "1790532000",
        "type": "text",
    }
    if wamid is not None:
        msg["id"] = wamid
    return {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "contacts": [
                                {"profile": {"name": "Rahul"}, "wa_id": phone}
                            ],
                            "messages": [msg],
                            "messaging_product": "whatsapp",
                        },
                    }
                ]
            }
        ]
    }


class TestSenderFromEnv(unittest.TestCase):
    def test_needs_both_vars(self):
        with mock.patch.dict(
            os.environ,
            {"WHATSAPP_TOKEN": "tok", "PHONE_NUMBER_ID": "123"},
        ):
            sender = sender_from_env()
        self.assertIsInstance(sender, WhatsAppSender)

    def test_token_alone_is_not_live(self):
        with mock.patch.dict(os.environ, {"WHATSAPP_TOKEN": "tok"}):
            os.environ.pop("PHONE_NUMBER_ID", None)
            self.assertIsNone(sender_from_env())

    def test_phone_id_alone_is_not_live(self):
        with mock.patch.dict(os.environ, {"PHONE_NUMBER_ID": "123"}):
            os.environ.pop("WHATSAPP_TOKEN", None)
            self.assertIsNone(sender_from_env())

    def test_neither_is_not_live(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("WHATSAPP_TOKEN", None)
            os.environ.pop("PHONE_NUMBER_ID", None)
            self.assertIsNone(sender_from_env())


class TestWhatsAppSender(unittest.TestCase):
    def _fake_response(self, payload):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        return FakeResponse()

    def test_posts_to_pinned_version_and_returns_wamid(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["auth"] = request.headers.get("Authorization")
            return self._fake_response({"messages": [{"id": "wamid.123"}]})

        sender = WhatsAppSender("tok123", "555")
        with mock.patch(
            "whatsapp_studio.send.urllib.request.urlopen", fake_urlopen
        ):
            wamid = sender.send("919810000001", "hello")
        self.assertEqual(wamid, "wamid.123")
        self.assertIn(f"graph.facebook.com/{GRAPH_API_VERSION}/555/messages", captured["url"])
        self.assertEqual(captured["body"]["to"], "919810000001")
        self.assertEqual(captured["body"]["text"]["body"], "hello")
        self.assertEqual(captured["auth"], "Bearer tok123")

    def test_truncates_overlong_body(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return self._fake_response({"messages": [{"id": "wamid.123"}]})

        sender = WhatsAppSender("tok123", "555")
        with mock.patch(
            "whatsapp_studio.send.urllib.request.urlopen", fake_urlopen
        ):
            sender.send("919810000001", "x" * 5000)
        self.assertEqual(len(captured["body"]["text"]["body"]), MAX_TEXT_CHARS)

    def test_missing_message_id_raises(self):
        def fake_urlopen(request, timeout=None):
            return self._fake_response({"messages": []})

        sender = WhatsAppSender("tok123", "555")
        with mock.patch(
            "whatsapp_studio.send.urllib.request.urlopen", fake_urlopen
        ):
            with self.assertRaises(ValueError):
                sender.send("919810000001", "hello")


class TestLiveSend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_live_mode_sends_reply(self):
        sender = FakeSender()
        studio = make_studio(self.tmp.name, sender=sender)
        self.assertTrue(studio.live)
        entry = studio.handle_message(
            "919810000001", "hi, facial ka price kya hai?", "Rahul"
        )
        studio.wait_for_sends()
        self.assertEqual(len(sender.calls), 1)
        to, body = sender.calls[0]
        self.assertEqual(to, "919810000001")
        self.assertIn("1499", body)
        self.assertEqual(entry["send_status"], "sent")
        self.assertEqual(entry["wamid"], "wamid.sent.1")
        self.assertIsNone(entry["send_error"])

    def test_demo_mode_never_calls_sender(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("WHATSAPP_TOKEN", None)
            os.environ.pop("PHONE_NUMBER_ID", None)
            studio = Studio(
                make_profile(),
                LeadStore(Path(self.tmp.name) / "leads.json"),
                handoffs=HandoffStore(),
                notifier=FakeNotifier(),
                llm=FixedLLM("Facial is Rs 1499 for 60 minutes."),
            )
        self.assertIsNone(studio.sender)
        self.assertFalse(studio.live)
        self.assertEqual(studio.sending_state(), "demo")
        entry = studio.handle_message(
            "919810000001", "hi, facial ka price kya hai?", "Rahul"
        )
        self.assertEqual(entry["send_status"], "recorded")
        self.assertIsNone(entry["wamid"])

    def test_dry_run_records_but_does_not_call(self):
        sender = FakeSender()
        studio = make_studio(self.tmp.name, sender=sender, dry_run=True)
        self.assertFalse(studio.live)
        self.assertEqual(studio.sending_state(), "dry-run")
        entry = studio.handle_message(
            "919810000001", "hi, facial ka price kya hai?", "Rahul"
        )
        studio.wait_for_sends()
        self.assertEqual(sender.calls, [])
        self.assertEqual(entry["send_status"], "recorded")

    def test_failed_send_is_recorded_not_raised(self):
        sender = FakeSender(fail_with=urllib.error.URLError("no network"))
        studio = make_studio(self.tmp.name, sender=sender)
        entry = studio.handle_message(
            "919810000001", "hi, facial ka price kya hai?", "Rahul"
        )
        studio.wait_for_sends()
        self.assertEqual(entry["send_status"], "failed")
        self.assertIn("no network", entry["send_error"])
        self.assertIsNone(entry["wamid"])

    def test_wrong_number_never_sent(self):
        sender = FakeSender()
        studio = make_studio(self.tmp.name, sender=sender)
        entry = studio.handle_message(
            "919999999999", "hi, facial ka price kya hai?", "Owner"
        )
        studio.wait_for_sends()
        self.assertEqual(sender.calls, [])
        self.assertEqual(entry["send_status"], "failed")
        self.assertIn("invalid recipient", entry["send_error"])
        entry = studio.handle_message("not-a-number", "hi", "X")
        studio.wait_for_sends()
        self.assertEqual(sender.calls, [])
        self.assertEqual(entry["send_status"], "failed")

    def test_dispatch_truncates_and_records(self):
        sender = FakeSender()
        studio = make_studio(self.tmp.name, sender=sender)
        entry = {"to": "919810000001", "body": "y" * 5000, "intent": "price"}
        studio.outbox.append(entry)
        studio._dispatch(entry)
        studio.wait_for_sends()
        self.assertTrue(entry.get("truncated"))
        self.assertEqual(len(entry["body"]), MAX_TEXT_CHARS)
        self.assertEqual(len(sender.calls[0][1]), MAX_TEXT_CHARS)
        self.assertEqual(entry["send_status"], "sent")

    def test_kill_switch_stops_and_resumes(self):
        sender = FakeSender()
        studio = make_studio(self.tmp.name, sender=sender)
        studio.disable_sending()
        self.assertFalse(studio.live)
        self.assertEqual(studio.sending_state(), "paused")
        entry = studio.handle_message(
            "919810000001", "hi, facial ka price kya hai?", "Rahul"
        )
        studio.wait_for_sends()
        self.assertEqual(sender.calls, [])
        self.assertEqual(entry["send_status"], "recorded")
        studio.enable_sending()
        self.assertTrue(studio.live)
        entry = studio.handle_message(
            "919810000001", "hi, facial ka price kya hai?", "Rahul"
        )
        studio.wait_for_sends()
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(entry["send_status"], "sent")


class TestDedup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sender = FakeSender()
        self.studio = make_studio(self.tmp.name, sender=self.sender)

    def tearDown(self):
        self.tmp.cleanup()

    def test_repeated_wamid_processed_once(self):
        payload = webhook_payload(
            "919810000001", "hi, facial ka price kya hai?", wamid="wamid.dup.1"
        )
        self.assertEqual(self.studio.handle_webhook_payload(payload), 1)
        self.studio.wait_for_sends()
        self.assertEqual(self.studio.handle_webhook_payload(payload), 0)
        self.studio.wait_for_sends()
        self.assertEqual(len(self.studio.outbox), 1)
        self.assertEqual(len(self.sender.calls), 1)

    def test_two_identical_messages_in_one_payload(self):
        payload = webhook_payload(
            "919810000001", "hi, facial ka price kya hai?", wamid="wamid.dup.2"
        )
        payload["entry"][0]["changes"][0]["value"]["messages"].append(
            dict(payload["entry"][0]["changes"][0]["value"]["messages"][0])
        )
        self.assertEqual(self.studio.handle_webhook_payload(payload), 1)
        self.studio.wait_for_sends()
        self.assertEqual(len(self.studio.outbox), 1)
        self.assertEqual(len(self.sender.calls), 1)

    def test_payload_without_id_processes_normally(self):
        payload = webhook_payload("919810000001", "hi, facial ka price kya hai?")
        self.assertEqual(self.studio.handle_webhook_payload(payload), 1)
        self.assertEqual(self.studio.handle_webhook_payload(payload), 1)
        self.assertEqual(len(self.studio.outbox), 2)

    def test_dedup_survives_restart(self):
        payload = webhook_payload(
            "919810000001", "hi, facial ka price kya hai?", wamid="wamid.dup.3"
        )
        self.assertEqual(self.studio.handle_webhook_payload(payload), 1)
        reopened = make_studio(self.tmp.name, sender=self.sender)
        self.assertEqual(reopened.handle_webhook_payload(payload), 0)
        self.assertEqual(len(reopened.outbox), 0)


class TestLiveSendHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sender = FakeSender()
        self.studio = make_studio(self.tmp.name, sender=self.sender)
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

    def _post_json(self, path, obj):
        req = urllib.request.Request(
            self._url(path),
            data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())

    def _post_form(self, path):
        req = urllib.request.Request(
            self._url(path),
            data=b"",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            return resp.status

    def test_health_reports_live_flag_without_token(self):
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["live"])
        self.assertEqual(data["sending"], "live")
        self.assertNotIn("tok", body)

    def test_health_reports_demo_when_no_sender(self):
        demo = make_studio(self.tmp.name, sender=None, dry_run=False)
        httpd = make_server(demo, port=0)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as resp:
                data = json.loads(resp.read())
            self.assertFalse(data["live"])
            self.assertEqual(data["sending"], "demo")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_raising_sender_keeps_webhook_200(self):
        self.sender.fail_with = RuntimeError("meta down")
        payload = webhook_payload(
            "919810000001", "hi, facial ka price kya hai?", wamid="wamid.fail.1"
        )
        status, body = self._post_json("/webhook", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["processed"], 1)
        self.studio.wait_for_sends()
        entry = self.studio.outbox[0]
        self.assertEqual(entry["send_status"], "failed")
        self.assertIn("meta down", entry["send_error"])

    def test_kill_switch_endpoints(self):
        self.assertEqual(self._post_form("/sending/disable"), 200)
        self.assertFalse(self.studio.live)
        self.assertEqual(self.studio.sending_state(), "paused")
        entry = self.studio.handle_message(
            "919810000001", "hi, facial ka price kya hai?", "Rahul"
        )
        self.studio.wait_for_sends()
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(entry["send_status"], "recorded")
        self.assertEqual(self._post_form("/sending/enable"), 200)
        self.assertTrue(self.studio.live)
        entry = self.studio.handle_message(
            "919810000001", "hi, facial ka price kya hai?", "Rahul"
        )
        self.studio.wait_for_sends()
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(entry["send_status"], "sent")

    def test_dashboard_shows_send_failure_and_live_state(self):
        self.sender.fail_with = RuntimeError("meta down")
        self.studio.handle_message(
            "919810000001", "hi, facial ka price kya hai?", "Rahul"
        )
        self.studio.wait_for_sends()
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("Sending: LIVE", body)
        self.assertIn("Stop sending", body)
        self.assertIn("did not get the reply", body)
        status, body = self._get("/?tab=replies")
        self.assertEqual(status, 200)
        self.assertIn("SEND FAILED", body)
        self.assertIn("meta down", body)
        self._post_form("/sending/disable")
        status, body = self._get("/")
        self.assertIn("Sending: PAUSED", body)
        self.assertIn("Resume sending", body)


if __name__ == "__main__":
    unittest.main()
