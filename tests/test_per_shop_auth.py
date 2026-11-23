"""Per-shop dashboard auth: one password per shop, master sees all.

Slice two of many-shops-on-one-install. The password picks the shop:
a shop password opens exactly its shop, the master password opens
the picker, or the shop named by ?shop=. A shop password with ?shop=
opens nothing, and /health carries no shop name. Tests use a
throwaway directory per case, never the real data/.
"""
import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from test_multishop import SHOP_A, SHOP_B, PromptEchoLLM, _payload, _profile
from whatsapp_studio.app import make_server
from whatsapp_studio.multishop import ShopRouter
from whatsapp_studio.security import (
    dashboard_password_env_name,
    load_dashboard_passwords,
    resolve_dashboard_scope,
)

MASTER = "master-secret"
GLOW_PASS = "glow-owner-pass"
SNIPS_PASS = "snips-owner-pass"


def _basic(password):
    return "Basic " + base64.b64encode(f"owner:{password}".encode()).decode()


class TestDashboardPasswordEnvNames(unittest.TestCase):
    def test_plain_shop_id(self):
        self.assertEqual(
            dashboard_password_env_name("snips"), "DASHBOARD_PASSWORD_SNIPS"
        )

    def test_hyphen_becomes_underscore(self):
        self.assertEqual(
            dashboard_password_env_name("glow-grace"),
            "DASHBOARD_PASSWORD_GLOW_GRACE",
        )


class TestLoadDashboardPasswords(unittest.TestCase):
    def test_loads_set_passwords_and_skips_unset_shops(self):
        env = {"DASHBOARD_PASSWORD_GLOW": GLOW_PASS}
        self.assertEqual(
            load_dashboard_passwords(["glow", "snips"], environ=env),
            {"glow": GLOW_PASS},
        )

    def test_two_shops_sharing_one_password_refuses_boot(self):
        env = {
            "DASHBOARD_PASSWORD_GLOW": "same-pass",
            "DASHBOARD_PASSWORD_SNIPS": "same-pass",
        }
        with self.assertRaises(ValueError):
            load_dashboard_passwords(["glow", "snips"], environ=env)

    def test_shop_password_equal_to_master_refuses_boot(self):
        env = {"DASHBOARD_PASSWORD_GLOW": MASTER}
        with self.assertRaises(ValueError):
            load_dashboard_passwords(
                ["glow"], environ=env, master_password=MASTER
            )


class TestResolveDashboardScope(unittest.TestCase):
    def setUp(self):
        self.passwords = {"glow": GLOW_PASS, "snips": SNIPS_PASS}
        self.shops = ["glow", "snips"]

    def resolve(self, password, shop=None):
        header = _basic(password) if password is not None else None
        return resolve_dashboard_scope(
            self.shops, self.passwords, MASTER, header, shop
        )

    def test_shop_password_opens_its_shop(self):
        self.assertEqual(self.resolve(GLOW_PASS), ("shop", "glow"))
        self.assertEqual(self.resolve(SNIPS_PASS), ("shop", "snips"))

    def test_master_opens_the_picker(self):
        self.assertEqual(self.resolve(MASTER), ("master", None))

    def test_master_with_shop_opens_that_shop(self):
        self.assertEqual(self.resolve(MASTER, "snips"), ("master", "snips"))

    def test_master_with_unknown_shop_opens_nothing(self):
        self.assertIsNone(self.resolve(MASTER, "nope"))

    def test_shop_password_with_shop_opens_nothing(self):
        self.assertIsNone(self.resolve(GLOW_PASS, "glow"))
        self.assertIsNone(self.resolve(GLOW_PASS, "snips"))

    def test_wrong_or_missing_password_opens_nothing(self):
        self.assertIsNone(self.resolve("wrong"))
        self.assertIsNone(self.resolve(None))
        self.assertIsNone(
            resolve_dashboard_scope(
                self.shops, self.passwords, MASTER, "Bearer nope"
            )
        )


class TestPerShopDashboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "profiles").mkdir()
        (self.base / "data").mkdir()
        profile_a = self.base / "profiles" / "glow.json"
        profile_b = self.base / "profiles" / "snips.json"
        profile_a.write_text(
            json.dumps(_profile("Glow and Grace Salon", 499, "919999999999").to_dict()),
            encoding="utf-8",
        )
        profile_b.write_text(
            json.dumps(_profile("Snips Salon", 799, "919899127791").to_dict()),
            encoding="utf-8",
        )
        registry = {
            SHOP_A: {"shop": "glow", "profile": "profiles/glow.json"},
            SHOP_B: {"shop": "snips", "profile": "profiles/snips.json"},
        }
        env = mock.patch.dict(os.environ, {"DASHBOARD_PASSWORD": MASTER})
        env.start()
        self.addCleanup(env.stop)
        self.router = ShopRouter(
            registry, str(profile_a), self.base, llm=PromptEchoLLM()
        )
        self.shop_a = self.router.studio_for(SHOP_A)
        self.shop_b = self.router.studio_for(SHOP_B)
        # One lead and one booking per shop, different people.
        self.router.handle_webhook_payload(
            _payload(SHOP_A, "919810000001", "haircut price?", "wamid-a")
        )
        self.router.handle_webhook_payload(
            _payload(SHOP_B, "919810000002", "haircut price?", "wamid-b")
        )
        tomorrow = date.today() + timedelta(days=1)
        self.shop_a.bookings.book(
            "919810000001", "Rahul", self.shop_a.profile.services[0],
            tomorrow, 660,
        )
        self.shop_b.bookings.book(
            "919810000002", "Asha", self.shop_b.profile.services[0],
            tomorrow, 660,
        )
        self.httpd = make_server(
            self.router.default_studio,
            port=0,
            router=self.router,
            dashboard_passwords={"glow": GLOW_PASS, "snips": SNIPS_PASS},
        )
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _request(self, method, path, body=None, headers=None, password=None):
        hdrs = dict(headers or {})
        if password is not None:
            hdrs["Authorization"] = _basic(password)
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
        location = resp.getheader("Location")
        conn.close()
        return status, location, data

    def _get(self, path, password=None):
        status, _, data = self._request("GET", path, password=password)
        return status, data.decode("utf-8", errors="replace")

    def test_shop_password_sees_only_its_shop(self):
        status, body = self._get("/", password=GLOW_PASS)
        self.assertEqual(status, 200)
        self.assertIn("Glow and Grace Salon", body)
        self.assertNotIn("Snips Salon", body)
        self.assertNotIn("shop=", body)
        status, body = self._get("/leads", password=GLOW_PASS)
        phones = [lead["phone"] for lead in json.loads(body)["leads"]]
        self.assertEqual(phones, ["919810000001"])
        status, body = self._get("/bookings", password=GLOW_PASS)
        names = [b["name"] for b in json.loads(body)["bookings"]]
        self.assertEqual(names, ["Rahul"])
        status, body = self._get("/export", password=GLOW_PASS)
        payload = json.loads(body)
        self.assertEqual(payload["profile"]["name"], "Glow and Grace Salon")
        self.assertEqual(
            [b["name"] for b in payload["bookings"]], ["Rahul"]
        )

    def test_other_shop_password_sees_only_its_shop(self):
        status, body = self._get("/", password=SNIPS_PASS)
        self.assertEqual(status, 200)
        self.assertIn("Snips Salon", body)
        self.assertNotIn("Glow and Grace Salon", body)
        status, body = self._get("/bookings", password=SNIPS_PASS)
        names = [b["name"] for b in json.loads(body)["bookings"]]
        self.assertEqual(names, ["Asha"])

    def test_shop_password_with_shop_param_is_refused(self):
        status, _ = self._get("/?shop=glow", password=GLOW_PASS)
        self.assertEqual(status, 401)
        status, _ = self._get("/?shop=snips", password=GLOW_PASS)
        self.assertEqual(status, 401)

    def test_no_or_wrong_password_is_refused_with_plain_words(self):
        status, body = self._get("/")
        self.assertEqual(status, 401)
        self.assertIn("That password did not work", body)
        self.assertIn("message Aditya on WhatsApp", body)
        status, _ = self._get("/", password="wrong")
        self.assertEqual(status, 401)

    def test_master_without_shop_gets_the_picker(self):
        status, body = self._get("/", password=MASTER)
        self.assertEqual(status, 200)
        self.assertIn("Pick a shop", body)
        self.assertIn("Glow and Grace Salon", body)
        self.assertIn("Snips Salon", body)
        self.assertIn("/?shop=glow", body)
        self.assertIn("/?shop=snips", body)
        status, body = self._get("/bookings", password=MASTER)
        self.assertEqual(status, 400)

    def test_master_with_shop_sees_that_shop_and_stays_in_it(self):
        status, body = self._get("/?shop=snips", password=MASTER)
        self.assertEqual(status, 200)
        self.assertIn("Snips Salon", body)
        self.assertNotIn("Glow and Grace Salon", body)
        self.assertIn("shop=snips", body)
        status, body = self._get("/bookings?shop=snips", password=MASTER)
        names = [b["name"] for b in json.loads(body)["bookings"]]
        self.assertEqual(names, ["Asha"])
        status, body = self._get("/?shop=nope", password=MASTER)
        self.assertEqual(status, 401)

    def test_master_post_keeps_the_shop_and_redirects_back(self):
        status, location, _ = self._request(
            "POST", "/sending/disable?shop=snips", body=b"", password=MASTER
        )
        self.assertEqual(status, 303)
        self.assertEqual(location, "/?shop=snips")
        self.assertTrue(self.shop_b.sending_disabled)
        self.assertFalse(self.shop_a.sending_disabled)

    def test_master_post_without_shop_is_refused(self):
        status, _, _ = self._request(
            "POST", "/sending/disable", body=b"", password=MASTER
        )
        self.assertEqual(status, 400)
        self.assertFalse(self.shop_a.sending_disabled)
        self.assertFalse(self.shop_b.sending_disabled)

    def test_shop_post_touches_only_its_shop(self):
        status, location, _ = self._request(
            "POST", "/sending/disable", body=b"", password=GLOW_PASS
        )
        self.assertEqual(status, 303)
        self.assertEqual(location, "/")
        self.assertTrue(self.shop_a.sending_disabled)
        self.assertFalse(self.shop_b.sending_disabled)

    def test_setup_save_under_one_shop_leaves_the_other_alone(self):
        import urllib.parse

        fields = {
            "service": ["Haircut"],
            "price": ["599"],
            "duration": ["30"],
        }
        for key in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
            fields["hours_" + key] = ["10:00 AM - 8:00 PM"]
        data = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
        status, location, _ = self._request(
            "POST", "/setup/save", body=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            password=GLOW_PASS,
        )
        self.assertEqual(status, 303)
        self.assertEqual(self.shop_a.profile.services[0].price, 599)
        self.assertEqual(self.shop_b.profile.services[0].price, 799)

    def test_health_carries_no_shop_name(self):
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["status"], "ok")
        self.assertNotIn("business", data)
        self.assertNotIn("live", data)
        self.assertNotIn("sending", data)


if __name__ == "__main__":
    unittest.main()
