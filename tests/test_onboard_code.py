"""The gated WhatsApp code challenge at setup (v0.5b): the confirmation
page offers an optional code check after the download, only when the
sender is live and a code template is configured. The download never
depends on a code, in any mode, in any code state. No lockout, ever:
three wrong guesses void the code and offer a fresh one at once."""

import contextlib
import io
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from unittest import mock
from urllib.parse import parse_qs, urlencode

from test_app import FakeNotifier, make_studio
from whatsapp_studio.app import (
    _onboard_confirmation_html,
    make_server,
)
from whatsapp_studio.bookings import IST
from whatsapp_studio.config import validate_onboarding_profile


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


def _posted_fields(**overrides):
    return parse_qs(
        urlencode(_fields(**overrides), doseq=True), keep_blank_values=True
    )


def _profile():
    """The validated profile for the standard form, built by the real
    validator so the shape is honest."""
    profile, errors = validate_onboarding_profile(_posted_fields())
    assert not errors, errors
    return profile


class FakeMetaError(Exception):
    """A Meta send failure carrying an error body, the way
    urllib raises it: _meta_error reads the code from .read()."""

    def __init__(self, code):
        super().__init__("Meta send failed")
        self._body = json.dumps(
            {"error": {"code": code, "message": "Meta refused the send"}}
        ).encode("utf-8")

    def read(self):
        return self._body


class FakeCodeSender:
    """Records template sends, never touches a network. fail_with
    raises on every send_template call, mimicking a Meta refusal."""

    def __init__(self, fail_with=None):
        self.template_calls = []
        self.fail_with = fail_with

    def send_template(self, to, template_name, language, parameters):
        self.template_calls.append((to, template_name, language, list(parameters)))
        if self.fail_with is not None:
            raise self.fail_with
        return "wamid.code.{}".format(len(self.template_calls))


def _code_block(body):
    start = body.index("<h2>Check this number when you have a moment</h2>")
    end = body.index("<p class='foot'>")
    return re.sub(r"<[^>]+>", "", body[start:end])


class TestOnboardCodeHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ, {"CODE_TEMPLATE_NAME": "studio_number_check"}
        )
        self.env.start()
        self.sender = FakeCodeSender()
        self.notifier = FakeNotifier()
        self.studio = make_studio(self.tmp.name)
        self.studio.sender = self.sender
        self.studio.notifier = self.notifier
        self.httpd = make_server(self.studio, port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.stop()
        self.tmp.cleanup()

    def _url(self, path):
        return "http://127.0.0.1:{}{}".format(self.port, path)

    def _post_form(self, path, fields):
        req = urllib.request.Request(
            self._url(path),
            data=urlencode(fields, doseq=True).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode(), resp.headers

    def _get(self, path):
        try:
            with urllib.request.urlopen(self._url(path)) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def _send_code(self, **overrides):
        return self._post_form("/onboard/code/send", _fields(**overrides))

    def _verify_code(self, code, **overrides):
        fields = _fields(**overrides)
        fields["code"] = code
        return self._post_form("/onboard/code/verify", fields)

    def _live_code(self):
        return self.studio._onboard_codes["9899912779"]["code"]

    def _confirmation(self):
        return self._post_form("/onboard", _fields())[1]

    def test_code_block_only_when_sender_live_and_template_set(self):
        _, body, _ = self._post_form("/onboard", _fields())
        self.assertIn("Send me a code", body)
        self.assertIn("Check this number when you have a moment", body)
        self.assertIn(
            "This is optional. Your file is ready above, and you can do "
            "this now or later. It does not change your download.",
            body,
        )
        self.assertIn(
            "Stuck? Aditya will message you on WhatsApp and check it "
            "with you. Nothing goes live until you say so.",
            body,
        )
        self.assertIn("Download your shop file", body)

    def test_gated_off_page_is_the_session_31_page(self):
        expected = _onboard_confirmation_html(_profile(), _posted_fields())
        # No sender.
        self.studio.sender = None
        _, body, _ = self._post_form("/onboard", _fields())
        self.assertEqual(body, expected)
        self.assertNotIn("Send me a code", body)
        # No template name.
        self.studio.sender = self.sender
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODE_TEMPLATE_NAME", None)
            _, body, _ = self._post_form("/onboard", _fields())
        self.assertEqual(body, expected)
        # Demo mode.
        self.studio.demo_mode = True
        _, body, _ = self._post_form("/onboard", _fields())
        self.assertEqual(body, expected)

    def test_code_block_has_no_dash(self):
        _, body, _ = self._send_code()
        text = _code_block(body)
        for dash in ("-", "\u2010", "\u2011", "\u2012", "\u2013", "\u2014"):
            self.assertNotIn(dash, text)

    def test_send_calls_template_with_code_and_shop_name(self):
        _, body, _ = self._send_code()
        self.assertEqual(len(self.sender.template_calls), 1)
        to, template, language, parameters = self.sender.template_calls[0]
        self.assertEqual(to, "919899912779")
        self.assertEqual(template, "studio_number_check")
        self.assertEqual(language, "en_US")
        self.assertEqual(len(parameters), 2)
        self.assertRegex(parameters[0], r"^[0-9]{6}$")
        self.assertEqual(parameters[1], "Snips Salon")
        self.assertIn(
            "We sent a code to +91 9899912779 on WhatsApp. Type the code "
            "here to show this number is yours.",
            body,
        )
        self.assertIn("Your code", body)
        self.assertIn("Confirm my number", body)
        self.assertIn(
            "No code yet? Wait a minute, then tap Send the code again.", body
        )
        self.assertNotIn(parameters[0], body)
        self.assertIn("Download your shop file", body)

    def test_verify_right_code_shows_checked(self):
        self._send_code()
        _, body, _ = self._verify_code(self._live_code())
        self.assertIn(
            "Your number is checked. Aditya will message you on WhatsApp. "
            "When he does, send him your shop file.",
            body,
        )
        self.assertIn("Download your shop file", body)
        self.assertNotIn("Send me a code", body)

    def test_verify_wrong_code_shows_mismatch_and_keeps_download(self):
        self._send_code()
        _, body, _ = self._verify_code("000000")
        self.assertIn(
            "That code does not match. Check the message on WhatsApp "
            "and try again.",
            body,
        )
        self.assertIn("Download your shop file", body)
        self.assertIn("Your code", body)

    def test_expired_code_and_resend(self):
        self._send_code()
        old = self._live_code()
        record = self.studio._onboard_codes["9899912779"]
        record["expires_at"] = record["sent_at"] - timedelta(seconds=1)
        _, body, _ = self._verify_code(old)
        self.assertIn(
            "That code has run out. Tap Send the code again for a new one.",
            body,
        )
        self.assertIn("Send me a code", body)
        _, body, _ = self._send_code()
        self.assertEqual(len(self.sender.template_calls), 2)
        new = self._live_code()
        self.assertNotEqual(new, old)
        _, body, _ = self._verify_code(old)
        self.assertNotIn("Your number is checked", body)
        self.assertIn("That code does not match", body)

    def test_three_wrong_guesses_void_the_code_with_no_lockout(self):
        self._send_code()
        code = self._live_code()
        for _ in range(2):
            _, body, _ = self._verify_code("000000")
            self.assertIn("That code does not match", body)
        _, body, _ = self._verify_code("000000")
        self.assertIn(
            "That code did not match. Tap Send the code again for a new "
            "one, or leave it and Aditya will check it with you.",
            body,
        )
        self.assertNotIn("Wait 10 minutes", body)
        # The voided code no longer verifies, and the page still
        # offers the file, the fix, and the wa.me link.
        _, body, _ = self._verify_code(code)
        self.assertIn("That code has run out", body)
        self.assertIn("Download your shop file", body)
        self.assertIn("Change it", body)
        self.assertIn("https://wa.me/919899912779", body)
        # A resend works at once, inside the minute: a voided code
        # waives the cooldown, and the fresh code verifies.
        _, body, _ = self._send_code()
        self.assertEqual(len(self.sender.template_calls), 2)
        _, body, _ = self._verify_code(self._live_code())
        self.assertIn("Your number is checked", body)

    def test_resend_inside_the_minute_sends_nothing(self):
        self._send_code()
        _, body, _ = self._send_code()
        self.assertEqual(len(self.sender.template_calls), 1)
        self.assertIn(
            "No code yet? Wait a minute, then tap Send the code again.", body
        )
        self.assertIn("Your code", body)

    def test_send_cap_five_per_hour(self):
        self._send_code()
        for _ in range(4):
            record = self.studio._onboard_codes["9899912779"]
            record["sent_at"] -= timedelta(seconds=61)
            self._send_code()
        self.assertEqual(len(self.sender.template_calls), 5)
        record = self.studio._onboard_codes["9899912779"]
        record["sent_at"] -= timedelta(seconds=61)
        _, body, _ = self._send_code()
        self.assertEqual(len(self.sender.template_calls), 5)
        self.assertIn(
            "No code yet? Check the number above. If it is right, tap "
            "Send the code again. If it is wrong, tap Change it and fix it.",
            body,
        )

    def test_download_never_blocked(self):
        # No code, code sent, code expired, code voided, cap hit,
        # template unapproved, demo mode: the file always comes back.
        def _download():
            status, body, headers = self._post_form(
                "/onboard/download", _fields()
            )
            self.assertEqual(status, 200)
            self.assertIn("attachment", headers.get("Content-Disposition", ""))
            self.assertIn("Snips Salon", body)

        _download()
        self._send_code()
        _download()
        record = self.studio._onboard_codes["9899912779"]
        record["expires_at"] = record["sent_at"] - timedelta(seconds=1)
        _download()
        self._verify_code("000000")
        self._verify_code("000000")
        self._verify_code("000000")
        _download()
        for _ in range(4):
            record = self.studio._onboard_codes["9899912779"]
            record["sent_at"] -= timedelta(seconds=61)
            self._send_code()
        _download()
        self.studio.code_template_unapproved = True
        _download()
        self.studio.demo_mode = True
        _download()

    def test_restart_forgets_the_code(self):
        self._send_code()
        code = self._live_code()
        fresh = make_studio(self.tmp.name)
        fresh.sender = self.sender
        fresh.notifier = FakeNotifier()
        httpd = make_server(fresh, port=0)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            fields = _fields()
            fields["code"] = code
            req = urllib.request.Request(
                "http://127.0.0.1:{}/onboard/code/verify".format(port),
                data=urlencode(fields, doseq=True).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                body = resp.read().decode()
            self.assertIn("That code has run out", body)
            self.assertIn("Send me a code", body)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_template_unapproved_shows_once_then_hides(self):
        self.sender.fail_with = FakeMetaError(132000)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _, body, _ = self._send_code()
        self.assertEqual(len(self.sender.template_calls), 1)
        self.assertIn(
            "The number check is not ready yet. Aditya will message you "
            "on WhatsApp and check it with you.",
            body,
        )
        self.assertIn("Download your shop file", body)
        self.assertNotIn("Send me a code", body)
        self.assertIn("132000", buf.getvalue())
        self.assertNotIn("9899912779", buf.getvalue())
        _, body, _ = self._post_form("/onboard", _fields())
        self.assertNotIn("The number check is not ready yet", body)
        self.assertNotIn("Send me a code", body)

    def test_number_not_on_whatsapp_codes(self):
        for code in (131030, 131026):
            self.sender.fail_with = FakeMetaError(code)
            _, body, _ = self._send_code()
            self.assertIn(
                "This number is not on WhatsApp. Use the number your "
                "customers message, or set up WhatsApp on it first.",
                body,
            )
            self.assertIn(
                "Stuck? Aditya will message you on WhatsApp and check it "
                "with you. Nothing goes live until you say so.",
                body,
            )
            self.assertIn("Download your shop file", body)
            self.sender.fail_with = None

    def test_other_send_failure_uses_the_warm_line(self):
        self.sender.fail_with = FakeMetaError(131047)
        _, body, _ = self._send_code()
        self.assertIn(
            "No code yet? That happens sometimes. Your file is still here, "
            "and Aditya will message you on WhatsApp and check it with you.",
            body,
        )
        self.assertIn("Send me a code", body)
        self.sender.fail_with = RuntimeError("network down")
        _, body, _ = self._send_code()
        self.assertIn("That happens sometimes", body)

    def test_send_and_verify_keep_every_hidden_field(self):
        _, body, _ = self._send_code()
        for value in ("Snips Salon", "Haircut", "499", "9899912779"):
            self.assertIn("value='{}'".format(value), body)
        _, body, _ = self._verify_code("000000")
        for value in ("Snips Salon", "Haircut", "499", "9899912779"):
            self.assertIn("value='{}'".format(value), body)

    def test_tampered_fields_on_code_post_show_the_form(self):
        _, body, _ = self._post_form(
            "/onboard/code/send", _fields(phone="123", phone_confirm="123")
        )
        self.assertIn("does not look like an Indian mobile number", body)
        self.assertEqual(len(self.sender.template_calls), 0)

    def test_verified_state_survives_resubmit_and_clears_on_new_number(self):
        self._send_code()
        self._verify_code(self._live_code())
        _, body, _ = self._post_form("/onboard", _fields())
        self.assertIn("Your number is checked", body)
        _, body, _ = self._post_form(
            "/onboard", _fields(phone="9899912778", phone_confirm="9899912778")
        )
        self.assertNotIn("Your number is checked", body)
        self.assertIn("Send me a code", body)

    def test_verification_sends_no_second_telegram_ping(self):
        self._post_form("/onboard", _fields())
        deadline = 2.0
        import time

        while (
            len(self.notifier.onboard_calls) < 1 and deadline > 0
        ):
            time.sleep(0.05)
            deadline -= 0.05
        self.assertEqual(len(self.notifier.onboard_calls), 1)
        self._send_code()
        self._verify_code(self._live_code())
        self._post_form("/onboard", _fields())
        import time

        time.sleep(0.3)
        self.assertEqual(len(self.notifier.onboard_calls), 1)

    def test_direct_post_in_demo_mode_or_without_sender_sends_nothing(self):
        self.studio.demo_mode = True
        _, body, _ = self._send_code()
        self.assertEqual(len(self.sender.template_calls), 0)
        self.assertNotIn("Send me a code", body)
        self.studio.demo_mode = False
        self.studio.sender = None
        _, body, _ = self._send_code()
        self.assertEqual(len(self.sender.template_calls), 0)
        self.assertNotIn("Send me a code", body)

    def test_code_routes_have_no_get(self):
        status, _ = self._get("/onboard/code/send")
        self.assertEqual(status, 404)
        status, _ = self._get("/onboard/code/verify")
        self.assertEqual(status, 404)


class TestOnboardCodeUnit(unittest.TestCase):
    """The time rules on the Studio methods, with a pinned clock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ, {"CODE_TEMPLATE_NAME": "studio_number_check"}
        )
        self.env.start()
        self.sender = FakeCodeSender()
        self.studio = make_studio(self.tmp.name)
        self.studio.sender = self.sender
        self.studio.notifier = FakeNotifier()
        self.now = datetime(2026, 11, 28, 14, 0, tzinfo=IST)
        self.profile = _profile()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_code_send_due_cooldown_and_cap(self):
        phone = self.profile["phone"]
        self.assertEqual(self.studio.code_send_due(phone, self.now), (True, None))
        sent, kind = self.studio.send_onboard_code(self.profile, self.now)
        self.assertTrue(sent)
        self.assertIsNone(kind)
        self.assertEqual(
            self.studio.code_send_due(phone, self.now), (False, "cooldown")
        )
        later = self.now + timedelta(seconds=61)
        self.assertEqual(self.studio.code_send_due(phone, later), (True, None))
        for step in range(4):
            at = self.now + timedelta(seconds=61 * (step + 1))
            self.studio.send_onboard_code(self.profile, at)
        capped = self.now + timedelta(seconds=61 * 5)
        self.assertEqual(
            self.studio.code_send_due(phone, capped), (False, "cap")
        )
        after_hour = self.now + timedelta(hours=1, seconds=1)
        self.assertEqual(
            self.studio.code_send_due(phone, after_hour), (True, None)
        )

    def test_verify_expiry_and_wrong_count(self):
        phone = self.profile["phone"]
        self.assertEqual(
            self.studio.verify_onboard_code(phone, "123456", self.now), "expired"
        )
        self.studio.send_onboard_code(self.profile, self.now)
        code = self.studio._onboard_codes[phone]["code"]
        self.assertEqual(
            self.studio.verify_onboard_code(phone, "000000", self.now), "wrong"
        )
        self.assertEqual(
            self.studio.verify_onboard_code(phone, "000000", self.now), "wrong"
        )
        self.assertEqual(
            self.studio.verify_onboard_code(phone, "000000", self.now), "voided"
        )
        self.assertEqual(
            self.studio.verify_onboard_code(phone, code, self.now), "expired"
        )
        self.studio.send_onboard_code(self.profile, self.now)
        code = self.studio._onboard_codes[phone]["code"]
        late = self.now + timedelta(minutes=10, seconds=1)
        self.assertEqual(
            self.studio.verify_onboard_code(phone, code, late), "expired"
        )
        self.studio.send_onboard_code(self.profile, self.now)
        code = self.studio._onboard_codes[phone]["code"]
        self.assertEqual(
            self.studio.verify_onboard_code(phone, " " + code + " ", self.now),
            "verified",
        )
        self.assertEqual(self.studio.onboard_code_state(phone, self.now), "verified")

    def test_no_phone_number_and_no_code_in_log_lines(self):
        phone = self.profile["phone"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.studio.send_onboard_code(self.profile, self.now)
            code = self.studio._onboard_codes[phone]["code"]
            self.studio.verify_onboard_code(phone, "000000", self.now)
            self.studio.verify_onboard_code(phone, "000000", self.now)
            self.studio.verify_onboard_code(phone, "000000", self.now)
            self.studio.send_onboard_code(self.profile, self.now)
            self.studio.sender.fail_with = FakeMetaError(132000)
            self.studio.send_onboard_code(self.profile, self.now)
            self.studio.sender.fail_with = FakeMetaError(131026)
            self.studio.send_onboard_code(self.profile, self.now)
            self.studio.sender.fail_with = RuntimeError("boom")
            self.studio.send_onboard_code(self.profile, self.now)
        logged = buf.getvalue()
        self.assertNotIn(phone, logged)
        self.assertNotIn(code, logged)
        self.assertIn("132000", logged)


if __name__ == "__main__":
    unittest.main()
