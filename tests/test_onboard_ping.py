"""The onboarding ping: a valid /onboard submit pings Aditya on Telegram
the moment a prospect is interested, once per phone number per day.

The ping is a lead, not a signup: the shop file is still only on her
phone, so the prospect is told on the form that Aditya will hear about
it, and the confirmation says he will message her. A failed ping never
breaks her confirmation page or download.
"""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from test_app import FakeNotifier, make_studio
from whatsapp_studio.app import make_server
from whatsapp_studio.config import DAY_KEYS, validate_onboarding_profile
from whatsapp_studio.notify import TelegramNotifier


def _fields(**overrides):
    fields = {
        "name": "Snips Salon",
        "category": "salon",
        "city": "Gurgaon",
        "address": "Sector 50",
        "owner_name": "Priya",
        "phone": "9899912779",
        "phone_confirm": "9899912779",
        "open": "10:00",
        "close": "20:00",
        "service": ["Haircut", "Hair Spa"],
        "price": ["499", ""],
        "duration": ["30", "60"],
    }
    fields.update(overrides)
    return fields


def _profile():
    """The validated profile for the standard form, built by the real
    validator so the shape is honest. The validator takes a parse_qs
    dict, so the fields go through the same encoding the form posts."""
    from urllib.parse import parse_qs

    profile, errors = validate_onboarding_profile(
        parse_qs(urlencode(_fields(), doseq=True), keep_blank_values=True)
    )
    assert not errors, errors
    return profile


class RaisingNotifier:
    """A notifier whose onboard ping always fails, to prove the form
    never breaks because of it."""

    def notify(self, business_name, handoff):
        return None

    def notify_booking(self, business_name, booking):
        return None

    def notify_send_failed(self, business_name, entry):
        return None

    def notify_onboard(self, profile, seen_before=False):
        raise RuntimeError("Telegram is down")


class TestOnboardPingHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notifier = FakeNotifier()
        self.studio = make_studio(self.tmp.name)
        self.studio.notifier = self.notifier
        self.httpd = make_server(self.studio, port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _url(self, path):
        return "http://127.0.0.1:{}{}".format(self.port, path)

    def _get(self, path):
        with urllib.request.urlopen(self._url(path)) as resp:
            return resp.status, resp.read().decode()

    def _post_form(self, path, fields):
        req = urllib.request.Request(
            self._url(path),
            data=urlencode(fields, doseq=True).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode(), resp.headers

    def _wait_for_calls(self, count, timeout=2.0):
        # The ping fires on a daemon thread, so the confirmation page
        # is back before the fake records the call: poll briefly.
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.notifier.onboard_calls) >= count:
                return True
            time.sleep(0.05)
        return len(self.notifier.onboard_calls) >= count

    def test_valid_submit_pings_once(self):
        status, body, _ = self._post_form("/onboard", _fields())
        self.assertEqual(status, 200)
        self.assertIn("Aditya knows you set up your shop", body)
        self.assertTrue(self._wait_for_calls(1))
        profile, seen_before = self.notifier.onboard_calls[0]
        self.assertEqual(profile["name"], "Snips Salon")
        self.assertEqual(profile["phone"], "9899912779")
        self.assertFalse(seen_before)

    def test_second_submit_same_day_does_not_ping(self):
        self._post_form("/onboard", _fields())
        self.assertTrue(self._wait_for_calls(1))
        self._post_form("/onboard", _fields())
        time.sleep(0.3)
        self.assertEqual(len(self.notifier.onboard_calls), 1)

    def test_next_day_submit_pings_again_marked_seen_before(self):
        with mock.patch(
            "whatsapp_studio.app.india_today", return_value=date(2026, 11, 24)
        ):
            self._post_form("/onboard", _fields())
        self.assertTrue(self._wait_for_calls(1))
        with mock.patch(
            "whatsapp_studio.app.india_today", return_value=date(2026, 11, 25)
        ):
            self._post_form("/onboard", _fields())
        self.assertTrue(self._wait_for_calls(2))
        _, seen_before = self.notifier.onboard_calls[1]
        self.assertTrue(seen_before)

    def test_invalid_submit_does_not_ping(self):
        status, body, _ = self._post_form(
            "/onboard", _fields(phone="123", phone_confirm="123")
        )
        self.assertEqual(status, 200)
        self.assertIn("does not look like an Indian mobile number", body)
        time.sleep(0.3)
        self.assertEqual(len(self.notifier.onboard_calls), 0)

    def test_edit_repost_does_not_ping(self):
        fields = _fields()
        fields["_action"] = "edit"
        status, body, _ = self._post_form("/onboard", fields)
        self.assertEqual(status, 200)
        self.assertIn("See my receptionist", body)
        time.sleep(0.3)
        self.assertEqual(len(self.notifier.onboard_calls), 0)

    def test_download_does_not_ping(self):
        status, body, headers = self._post_form("/onboard/download", _fields())
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn("Snips Salon", body)
        time.sleep(0.3)
        self.assertEqual(len(self.notifier.onboard_calls), 0)

    def test_failing_notifier_does_not_break_confirmation(self):
        self.studio.notifier = RaisingNotifier()
        status, body, _ = self._post_form("/onboard", _fields())
        self.assertEqual(status, 200)
        self.assertIn("Aditya knows you set up your shop", body)
        self.assertIn("Download your shop file", body)

    def test_form_tells_her_aditya_will_be_told(self):
        _, body = self._get("/onboard")
        self.assertIn("we will let him know you set up your shop", body)
        self.assertIn("No calls, no spam", body)

    def test_confirmation_says_aditya_will_message(self):
        _, body, _ = self._post_form("/onboard", _fields())
        self.assertIn("Aditya knows you set up your shop", body)
        self.assertIn("send him this file", body)
        self.assertIn("nothing changes for your customers", body)


class TestOnboardPingDedup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_due_then_not_due_then_due_next_day(self):
        studio = make_studio(self.tmp.name)
        day_one = date(2026, 11, 24)
        day_two = date(2026, 11, 25)
        self.assertEqual(
            studio.onboard_ping_due("9899912779", today=day_one), (True, False)
        )
        self.assertEqual(
            studio.onboard_ping_due("9899912779", today=day_one), (False, False)
        )
        self.assertEqual(
            studio.onboard_ping_due("9899912779", today=day_two), (True, True)
        )
        self.assertEqual(
            studio.onboard_ping_due("9899912778", today=day_two), (True, False)
        )


class TestNotifyOnboard(unittest.TestCase):
    def _captured_send(self):
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

        return captured, fake_urlopen

    def test_message_text(self):
        captured, fake_urlopen = self._captured_send()
        hours = {key: "10:00 - 20:00" for key in DAY_KEYS}
        hours["mon"] = "closed"
        profile = _profile()
        profile["hours"] = hours
        with mock.patch(
            "whatsapp_studio.notify.urllib.request.urlopen", fake_urlopen
        ):
            TelegramNotifier("tok123", "chat456").notify_onboard(profile)
        self.assertIn("api.telegram.org/bottok123/sendMessage", captured["url"])
        self.assertEqual(captured["body"]["chat_id"], "chat456")
        text = captured["body"]["text"]
        self.assertIn("New shop on WhatsApp Studio", text)
        self.assertIn("Snips Salon, salon, Gurgaon", text)
        self.assertIn("Priya, https://wa.me/919899912779", text)
        self.assertIn("Haircut Rs 499", text)
        self.assertIn("Hair Spa price to confirm", text)
        self.assertIn("Open 10:00 to 20:00, closed Monday", text)
        self.assertIn("nothing is live yet", text)
        self.assertNotIn(" - ", text)

    def test_seen_before_line(self):
        captured, fake_urlopen = self._captured_send()
        with mock.patch(
            "whatsapp_studio.notify.urllib.request.urlopen", fake_urlopen
        ):
            TelegramNotifier("tok123", "chat456").notify_onboard(
                _profile(), seen_before=True
            )
        self.assertIn("(submitted before)", captured["body"]["text"])

    def test_failure_is_swallowed(self):
        def failing_urlopen(request, timeout=None):
            raise urllib.error.URLError("no network")

        with mock.patch(
            "whatsapp_studio.notify.urllib.request.urlopen", failing_urlopen
        ):
            TelegramNotifier("tok123", "chat456").notify_onboard(_profile())


if __name__ == "__main__":
    unittest.main()
