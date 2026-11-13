import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from whatsapp_studio.app import Studio, make_server
from whatsapp_studio.config import BusinessProfile
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


if __name__ == "__main__":
    unittest.main()
