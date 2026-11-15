import base64
import http.client
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from whatsapp_studio.app import Studio, make_server
from whatsapp_studio.bookings import BookingStore
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore


def make_studio(tmp):
    profile = BusinessProfile(
        name="Glow & Grace Salon",
        category="salon",
        city="Gurgaon",
        phone="919999999999",
        hours={"daily": "10:00 AM - 8:00 PM"},
        services=[],
        address="Sector 50, Gurgaon",
    )
    return Studio(profile, LeadStore(Path(tmp) / "leads.json"), verify_token="test-token")


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


class TestApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = make_studio(self.tmp.name)
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

    def test_health(self):
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_webhook_verification_ok(self):
        status, body = self._get(
            "/webhook?hub.mode=subscribe&hub.verify_token=test-token&hub.challenge=abc123"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, "abc123")

    def test_webhook_verification_bad_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/webhook?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=abc123")
        self.assertEqual(ctx.exception.code, 403)

    def test_webhook_message_flow(self):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "contacts": [{"profile": {"name": "Rahul"}, "wa_id": "919810000001"}],
                                "messages": [
                                    {
                                        "from": "919810000001",
                                        "id": "wamid.1",
                                        "timestamp": "1",
                                        "type": "text",
                                        "text": {"body": "what are your timings?"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ],
        }
        status, data = self._post_json("/webhook", payload)
        self.assertEqual(status, 200)
        self.assertEqual(data["processed"], 1)
        self.assertEqual(len(self.studio.outbox), 1)
        self.assertIn("10:00 AM", self.studio.outbox[0]["body"])
        status, body = self._get("/leads")
        leads = json.loads(body)["leads"]
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["phone"], "919810000001")

    def test_webhook_invalid_json(self):
        req = urllib.request.Request(
            self._url("/webhook"),
            data=b"not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)

    def test_dashboard(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        # Business name is HTML-escaped in the page (& -> &amp;).
        self.assertIn("Glow &amp; Grace Salon", body)


def _payload(phone, msg_type, msg_id, text=None):
    message = {"from": phone, "id": msg_id, "timestamp": "1", "type": msg_type}
    if text is not None:
        message["text"] = {"body": text}
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [
                                {"profile": {"name": "Rahul"}, "wa_id": phone}
                            ],
                            "messages": [message],
                        }
                    }
                ]
            }
        ],
    }


class TestNonTextMessages(unittest.TestCase):
    """A voice note or photo gets an honest answer, a handoff, and a ping —
    never silence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notifier = FakeNotifier()
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
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_voice_note_gets_reply_handoff_and_ping(self):
        count = self.studio.handle_webhook_payload(
            _payload("919810000001", "audio", "wamid.voice1")
        )
        self.assertEqual(count, 1)
        self.assertEqual(len(self.studio.outbox), 1)
        self.assertIn("typed messages", self.studio.outbox[0]["body"])
        handoff = self.studio.handoffs.open_for("919810000001")
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff["reason"], "sent a voice note")
        self.assertEqual(len(self.notifier.calls), 1)
        self.assertEqual(len(self.studio.leads.all_leads()), 1)

    def test_photo_gets_same_treatment(self):
        self.studio.handle_webhook_payload(
            _payload("919810000001", "image", "wamid.photo1")
        )
        handoff = self.studio.handoffs.open_for("919810000001")
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff["reason"], "sent a photo")

    def test_redelivered_voice_note_answered_once(self):
        payload = _payload("919810000001", "audio", "wamid.voice1")
        self.assertEqual(self.studio.handle_webhook_payload(payload), 1)
        self.assertEqual(self.studio.handle_webhook_payload(payload), 0)
        self.assertEqual(len(self.studio.outbox), 1)


class TestExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        profile = BusinessProfile(
            name="Glow & Grace Salon",
            category="salon",
            city="Gurgaon",
            phone="919999999999",
            hours={"daily": "10:00 AM - 8:00 PM"},
            services=[Service("Haircut", 499, 30)],
            address="Sector 50, Gurgaon",
        )
        self.studio = Studio(
            profile,
            LeadStore(Path(self.tmp.name) / "leads.json"),
            bookings=BookingStore(),
            handoffs=HandoffStore(),
            dashboard_password="secret",
        )
        self.studio.handle_message("919810000001", "hi", "Rahul")
        self.httpd = make_server(self.studio, port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def test_export_needs_auth(self):
        conn = self._conn()
        conn.request("GET", "/export")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 401)
        resp.read()
        conn.close()

    def test_export_returns_everything_as_download(self):
        auth = base64.b64encode(b"owner:secret").decode()
        conn = self._conn()
        conn.request("GET", "/export", headers={"Authorization": f"Basic {auth}"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn("attachment", resp.getheader("Content-Disposition"))
        data = json.loads(resp.read())
        conn.close()
        self.assertIn("leads", data)
        self.assertIn("bookings", data)
        self.assertIn("handoffs", data)
        self.assertEqual(len(data["leads"]), 1)
        self.assertEqual(data["leads"][0]["phone"], "919810000001")


class TestOwnerReply(unittest.TestCase):
    """The owner replies from the dashboard: the text reaches the
    customer, the handoff is resolved, and the reply is recorded."""

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

    def _post_reply(self, handoff_id, text):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = urlencode({"text": text})
        conn.request(
            "POST",
            f"/handoffs/{handoff_id}/reply",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp = conn.getresponse()
        status, location = resp.status, resp.getheader("Location")
        resp.read()
        conn.close()
        return status, location

    def test_reply_reaches_customer_and_resolves_handoff(self):
        status, location = self._post_reply(1, "Hi Rahul, this is the salon — how can I help?")
        self.assertEqual(status, 303)
        self.assertEqual(location, "/")
        self.studio.wait_for_sends()
        # One send is the handoff confirmation from setUp; the second is
        # the owner's reply.
        self.assertEqual(len(self.sender.calls), 2)
        to, body = self.sender.calls[1]
        self.assertEqual(to, "919810000001")
        self.assertIn("this is the salon", body)
        self.assertIsNone(self.studio.handoffs.open_for("919810000001"))
        self.assertTrue(
            any(e.get("handoff") == 1 and e["body"].startswith("Hi Rahul")
                for e in self.studio.outbox)
        )
        owner_entry = next(
            e for e in self.studio.outbox if e["body"].startswith("Hi Rahul")
        )
        self.assertIn("ts", owner_entry)
        self.assertIsInstance(owner_entry["ts"], int)

    def test_reply_to_missing_handoff_is_404(self):
        sent_before = len(self.sender.calls)
        status, _ = self._post_reply(99, "hello?")
        self.assertEqual(status, 404)
        self.assertEqual(len(self.sender.calls), sent_before)

    def test_reply_with_sending_off_is_recorded_not_sent(self):
        self.studio.sender = None  # demo mode: no sender configured
        html = self.studio.dashboard_html()
        self.assertIn("Record reply, sending is off", html)
        self.assertIn("Sending is off", html)
        # One send already happened: the handoff confirmation from setUp.
        sent_before = len(self.sender.calls)
        status, _ = self._post_reply(1, "Hi Rahul, this is the salon.")
        self.assertEqual(status, 303)
        self.assertEqual(len(self.sender.calls), sent_before)
        entry = next(e for e in self.studio.outbox if e.get("handoff") == 1 and e["body"].startswith("Hi Rahul"))
        self.assertEqual(entry["send_status"], "recorded")
        self.assertIsNone(self.studio.handoffs.open_for("919810000001"))


if __name__ == "__main__":
    unittest.main()
