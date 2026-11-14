import base64
import hashlib
import hmac
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from whatsapp_studio.app import Studio, make_server
from whatsapp_studio.config import BusinessProfile
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.security import check_basic_auth, verify_webhook_signature

SECRET = "test-app-secret"
PASSWORD = "test-dashboard-password"


def _signature(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _basic(password):
    return "Basic " + base64.b64encode(f"owner:{password}".encode()).decode()


def _webhook_payload():
    return {
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


def _make_studio(tmp, **kwargs):
    profile = BusinessProfile(
        name="Glow & Grace Salon",
        category="salon",
        city="Gurgaon",
        phone="919999999999",
        hours={"daily": "10:00 AM - 8:00 PM"},
        services=[],
        address="Sector 50, Gurgaon",
    )
    return Studio(profile, LeadStore(Path(tmp) / "leads.json"), verify_token="test-token", **kwargs)


class _ServerCase(unittest.TestCase):
    studio_kwargs = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = _make_studio(self.tmp.name, **self.studio_kwargs)
        self.httpd = make_server(self.studio, port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return status, resp_headers, data

    def _post_webhook(self, headers=None):
        body = json.dumps(_webhook_payload()).encode()
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        return self._request("POST", "/webhook", body=body, headers=hdrs)


class TestSecurityFunctions(unittest.TestCase):
    def test_verify_valid_signature(self):
        body = b'{"hello": "world"}'
        self.assertTrue(verify_webhook_signature(SECRET, body, _signature(SECRET, body)))

    def test_verify_wrong_secret(self):
        body = b'{"hello": "world"}'
        self.assertFalse(verify_webhook_signature(SECRET, body, _signature("other-secret", body)))

    def test_verify_tampered_body(self):
        body = b'{"hello": "world"}'
        header = _signature(SECRET, b'{"hello": "someone else"}')
        self.assertFalse(verify_webhook_signature(SECRET, body, header))

    def test_verify_missing_or_malformed(self):
        body = b"{}"
        self.assertFalse(verify_webhook_signature(SECRET, body, None))
        self.assertFalse(verify_webhook_signature(SECRET, body, ""))
        self.assertFalse(verify_webhook_signature(SECRET, body, "not-a-signature"))
        self.assertFalse(verify_webhook_signature(SECRET, body, "sha256=xyz"))
        self.assertFalse(verify_webhook_signature("", body, _signature(SECRET, body)))

    def test_basic_auth_valid(self):
        self.assertTrue(check_basic_auth(PASSWORD, _basic(PASSWORD)))
        # The username is ignored; only the password is the gate.
        other_user = "Basic " + base64.b64encode(b"anyone:" + PASSWORD.encode()).decode()
        self.assertTrue(check_basic_auth(PASSWORD, other_user))

    def test_basic_auth_wrong_or_malformed(self):
        self.assertFalse(check_basic_auth(PASSWORD, _basic("wrong")))
        self.assertFalse(check_basic_auth(PASSWORD, None))
        self.assertFalse(check_basic_auth(PASSWORD, ""))
        self.assertFalse(check_basic_auth(PASSWORD, "Bearer abc"))
        self.assertFalse(check_basic_auth(PASSWORD, "Basic !!!not-base64!!!"))
        self.assertFalse(check_basic_auth(PASSWORD, "Basic " + base64.b64encode(b"nopassword").decode()))
        self.assertFalse(check_basic_auth("", _basic(PASSWORD)))


class TestWebhookSignature(_ServerCase):
    studio_kwargs = {"app_secret": SECRET, "dashboard_password": PASSWORD}

    def test_valid_signature_processed(self):
        body = json.dumps(_webhook_payload()).encode()
        status, _, data = self._request(
            "POST", "/webhook",
            body=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": _signature(SECRET, body)},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["processed"], 1)
        self.assertEqual(len(self.studio.outbox), 1)

    def test_bad_signature_rejected_and_not_processed(self):
        body = json.dumps(_webhook_payload()).encode()
        status, _, _ = self._request(
            "POST", "/webhook",
            body=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": _signature("wrong-secret", body)},
        )
        self.assertEqual(status, 401)
        self.assertEqual(len(self.studio.outbox), 0)
        self.assertIsNone(self.studio.leads.get("919810000001"))

    def test_missing_signature_rejected(self):
        status, _, _ = self._post_webhook()
        self.assertEqual(status, 401)
        self.assertEqual(len(self.studio.outbox), 0)

    def test_malformed_signature_rejected(self):
        body = json.dumps(_webhook_payload()).encode()
        status, _, _ = self._request(
            "POST", "/webhook",
            body=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=deadbeef"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(len(self.studio.outbox), 0)

    def test_webhook_get_verification_stays_open(self):
        # GET verification is Meta's own check; it needs no auth and no
        # signature, even with both gates configured.
        status, _, data = self._request(
            "GET", "/webhook?hub.mode=subscribe&hub.verify_token=test-token&hub.challenge=abc123"
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, b"abc123")

    def test_health_stays_open(self):
        status, _, data = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["status"], "ok")


class TestDashboardAuth(_ServerCase):
    studio_kwargs = {"app_secret": SECRET, "dashboard_password": PASSWORD}

    def test_dashboard_requires_auth(self):
        status, headers, _ = self._request("GET", "/")
        self.assertEqual(status, 401)
        self.assertIn("basic", headers.get("www-authenticate", "").lower())

    def test_dashboard_right_password(self):
        status, _, data = self._request("GET", "/", headers={"Authorization": _basic(PASSWORD)})
        self.assertEqual(status, 200)
        self.assertIn(b"Glow &amp; Grace Salon", data)

    def test_dashboard_wrong_password(self):
        status, _, _ = self._request("GET", "/", headers={"Authorization": _basic("wrong")})
        self.assertEqual(status, 401)

    def test_data_endpoints_require_auth(self):
        for path in ("/leads", "/bookings", "/handoffs"):
            status, _, _ = self._request("GET", path)
            self.assertEqual(status, 401, path)
            status, _, data = self._request("GET", path, headers={"Authorization": _basic(PASSWORD)})
            self.assertEqual(status, 200, path)
            self.assertIn(path.strip("/"), json.loads(data))

    def test_resolve_requires_auth(self):
        handoff = self.studio.handoffs.open("919810000001", "Rahul", "asked for a person", "talk to someone")
        body = f"handoff_id={handoff['id']}".encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        status, _, _ = self._request("POST", "/handoffs/resolve", body=body, headers=headers)
        self.assertEqual(status, 401)
        self.assertIsNotNone(self.studio.handoffs.open_for("919810000001"))
        auth = {"Authorization": _basic(PASSWORD)}
        status, _, _ = self._request("POST", "/handoffs/resolve", body=body, headers={**headers, **auth})
        self.assertEqual(status, 303)
        self.assertIsNone(self.studio.handoffs.open_for("919810000001"))

    def test_sending_toggle_requires_auth(self):
        status, _, _ = self._request("POST", "/sending/disable")
        self.assertEqual(status, 401)
        self.assertFalse(self.studio.sending_disabled)
        status, _, _ = self._request("POST", "/sending/disable", headers={"Authorization": _basic(PASSWORD)})
        self.assertEqual(status, 303)
        self.assertTrue(self.studio.sending_disabled)
        status, _, _ = self._request("POST", "/sending/enable", headers={"Authorization": _basic(PASSWORD)})
        self.assertEqual(status, 303)
        self.assertFalse(self.studio.sending_disabled)

    def test_unknown_path_requires_auth_too(self):
        status, _, _ = self._request("GET", "/nope")
        self.assertEqual(status, 401)
        status, _, _ = self._request("GET", "/nope", headers={"Authorization": _basic(PASSWORD)})
        self.assertEqual(status, 404)


class TestSecurityOffByDefault(_ServerCase):
    studio_kwargs = {}

    def test_webhook_without_secret_needs_no_signature(self):
        status, _, data = self._post_webhook()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["processed"], 1)

    def test_bogus_signature_ignored_without_secret(self):
        body = json.dumps(_webhook_payload()).encode()
        status, _, data = self._request(
            "POST", "/webhook",
            body=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=deadbeef"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["processed"], 1)

    def test_dashboard_without_password_needs_no_auth(self):
        status, _, _ = self._request("GET", "/")
        self.assertEqual(status, 200)
        status, _, _ = self._request("GET", "/leads")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
