"""The onboarding form: a prospect fills in her shop and downloads
a profile file. Nothing is written to the server and nothing is
stored: the form validates, shows the shop back, and hands her a
file to send to Aditya.

Time-dependent checks pin explicit times or use tomorrow, never the
current hour, so the suite passes whatever hour it runs in.
"""
import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from test_setup_form import make_profile, make_studio, write_profile
from whatsapp_studio.app import make_server
from whatsapp_studio.config import (
    BusinessProfile,
    Service,
    normalize_onboarding_phone,
    validate_onboarding_profile,
)
from whatsapp_studio.llm import build_system_prompt


def valid_fields(**overrides):
    fields = {
        "name": ["Snips Salon"],
        "category": ["salon"],
        "city": ["Gurgaon"],
        "address": ["B-3, Unitech Business Zone"],
        "owner_name": ["Ritu"],
        "phone": ["919899127791"],
        "phone_confirm": ["9899127791"],
        "open": ["10:00"],
        "close": ["20:00"],
        "service": ["Hair Spa", "Shampoo", "", "", "", "", "", "", "", ""],
        "price": ["", "199", "", "", "", "", "", "", "", ""],
        "duration": ["60", "30", "30", "30", "30", "30", "30", "30", "30", "30"],
        "varies": ["0"],
    }
    fields.update(overrides)
    return fields


class TestOnboardingPhone(unittest.TestCase):
    def test_normalises_indian_numbers(self):
        self.assertEqual(normalize_onboarding_phone("9899127791"), "9899127791")
        self.assertEqual(normalize_onboarding_phone("+91 98991 27791"), "9899127791")
        self.assertEqual(normalize_onboarding_phone("919899127791"), "9899127791")
        self.assertEqual(normalize_onboarding_phone("09899127791"), "9899127791")
        self.assertEqual(normalize_onboarding_phone("98991-27791"), "9899127791")

    def test_refuses_foreign_landline_and_short_numbers(self):
        for bad in ("", "98991", "1234567890", "022 12345678", "+1 415 555 0132",
                    "98991 2779a", "5899127791"):
            self.assertIsNone(normalize_onboarding_phone(bad), bad)

    def test_confirm_must_match_after_normalising(self):
        _, errors = validate_onboarding_profile(
            valid_fields(phone_confirm=["98991 27792"])
        )
        self.assertIn("phone_confirm", errors)
        self.assertIn("do not match", errors["phone_confirm"])
        profile, errors = validate_onboarding_profile(
            valid_fields(phone=["+91 98991 27791"], phone_confirm=["09899127791"])
        )
        self.assertEqual(errors, {})
        self.assertEqual(profile["phone"], "9899127791")


class TestOnboardingValidation(unittest.TestCase):
    def test_valid_submission_builds_the_profile(self):
        profile, errors = validate_onboarding_profile(valid_fields())
        self.assertEqual(errors, {})
        self.assertEqual(profile["name"], "Snips Salon")
        self.assertEqual(profile["category"], "salon")
        self.assertEqual(profile["city"], "Gurgaon")
        self.assertEqual(profile["phone"], "9899127791")
        self.assertEqual(profile["hours"], {"daily": "10:00 AM - 8:00 PM"})
        self.assertEqual(profile["greeting"], "Hello! Welcome to Snips Salon.")
        self.assertEqual(profile["owner_name"], "Ritu")
        self.assertEqual(len(profile["services"]), 2)
        self.assertIsNone(profile["services"][0]["price"])
        self.assertTrue(profile["services"][0]["price_varies"])
        self.assertEqual(profile["services"][0]["duration_min"], 60)
        self.assertEqual(profile["services"][1]["price"], 199)

    def test_profile_round_trips_through_the_dataclass(self):
        profile, _ = validate_onboarding_profile(valid_fields())
        back = BusinessProfile.from_dict(profile)
        self.assertEqual(back.to_dict()["services"], profile["services"])
        self.assertEqual(back.name, "Snips Salon")
        self.assertEqual(back.phone, "9899127791")

    def test_blank_rows_are_ignored_and_one_service_is_required(self):
        fields = valid_fields(
            service=["", "", "", "", "", "", "", "", "", ""],
            price=["", "", "", "", "", "", "", "", "", ""],
            varies=[],
        )
        _, errors = validate_onboarding_profile(fields)
        self.assertIn("services", errors)
        self.assertIn("at least one service", errors["services"])

    def test_duplicate_service_names_are_refused(self):
        _, errors = validate_onboarding_profile(
            valid_fields(service=["Haircut", "haircut", "", "", "", "", "", "", "", ""])
        )
        self.assertIn("service_1", errors)
        self.assertIn("twice", errors["service_1"])

    def test_price_without_a_name_is_an_error_naming_the_row(self):
        _, errors = validate_onboarding_profile(
            valid_fields(service=["", "Shampoo", "", "", "", "", "", "", "", ""])
        )
        self.assertIn("service_0", errors)
        self.assertIn("Service 1", errors["service_0"])

    def test_bad_price_names_the_service(self):
        _, errors = validate_onboarding_profile(
            valid_fields(price=["", "abc", "", "", "", "", "", "", "", ""])
        )
        self.assertIn("service_1", errors)
        self.assertIn("Shampoo", errors["service_1"])
        self.assertIn("whole rupees", errors["service_1"])

    def test_blank_duration_is_refused(self):
        _, errors = validate_onboarding_profile(
            valid_fields(duration=["", "30", "30", "30", "30", "30", "30", "30", "30", "30"])
        )
        self.assertIn("service_0", errors)
        self.assertIn("Hair Spa", errors["service_0"])

    def test_too_many_services_are_refused(self):
        fields = valid_fields(
            service=["Service {}".format(i) for i in range(21)],
            price=[""] * 21,
            duration=["30"] * 21,
            varies=[],
        )
        _, errors = validate_onboarding_profile(fields)
        self.assertIn("services", errors)

    def test_mismatched_row_counts_are_refused(self):
        _, errors = validate_onboarding_profile(
            valid_fields(price=["199"])
        )
        self.assertIn("services", errors)
        self.assertIn("half filled", errors["services"])

    def test_closed_all_week_is_refused(self):
        fields = valid_fields(
            closed=["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        )
        _, errors = validate_onboarding_profile(fields)
        self.assertIn("hours", errors)
        self.assertIn("closed every day", errors["hours"])

    def test_closed_days_become_closed_in_the_profile(self):
        profile, errors = validate_onboarding_profile(
            valid_fields(closed=["tue"])
        )
        self.assertEqual(errors, {})
        self.assertEqual(profile["hours"]["tue"], "closed")
        self.assertEqual(profile["hours"]["mon"], "10:00 AM - 8:00 PM")
        self.assertNotIn("daily", profile["hours"])

    def test_closing_before_opening_is_refused(self):
        _, errors = validate_onboarding_profile(
            valid_fields(open=["20:00"], close=["10:00"])
        )
        self.assertIn("hours", errors)
        self.assertIn("after the opening time", errors["hours"])

    def test_bad_hours_are_refused(self):
        _, errors = validate_onboarding_profile(valid_fields(open=["ten"]))
        self.assertIn("hours", errors)

    def test_missing_name_category_and_phone_are_each_named(self):
        _, errors = validate_onboarding_profile(
            valid_fields(name=[""], category=["hotel"], phone=[""], phone_confirm=[""])
        )
        self.assertIn("name", errors)
        self.assertIn("category", errors)
        self.assertIn("phone", errors)

    def test_long_values_are_refused(self):
        _, errors = validate_onboarding_profile(
            valid_fields(name=["x" * 101], address=["y" * 201])
        )
        self.assertIn("name", errors)
        self.assertIn("address", errors)

    def test_error_messages_have_no_dash(self):
        fields = valid_fields(
            name=[""],
            category=["hotel"],
            phone=["123"],
            open=["20:00"],
            close=["10:00"],
            service=["", "", "", "", "", "", "", "", "", ""],
            price=["", "", "", "", "", "", "", "", "", ""],
            varies=[],
        )
        _, errors = validate_onboarding_profile(fields)
        self.assertTrue(errors)
        for message in errors.values():
            self.assertNotIn("-", message)
            self.assertNotIn("\u2013", message)
            self.assertNotIn("\u2014", message)

    def test_price_varies_round_trips_in_the_service(self):
        service = Service("Bridal Makeup", None, 120, price_varies=True)
        back = Service.from_dict(service.to_dict())
        self.assertTrue(back.price_varies)
        self.assertIsNone(back.price)

    def test_varying_price_reaches_the_model_wording(self):
        profile = make_profile()
        profile.services = [Service("Bridal Makeup", None, 120, price_varies=True)]
        prompt = build_system_prompt(profile)
        self.assertIn("price varies, the shop confirms it", prompt)
        self.assertIn("never quote a number for it", prompt)


class TestOnboardingHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = write_profile(self.tmp.name)
        self.studio = make_studio(
            self.tmp.name, profile_path=self.path, dashboard_password="onboard-test-pw"
        )
        self.httpd = make_server(self.studio, port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _url(self, path):
        return "http://127.0.0.1:{}{}".format(self.port, path)

    def _auth(self):
        return "Basic " + base64.b64encode(b"owner:onboard-test-pw").decode()

    def _get(self, path, auth=True):
        req = urllib.request.Request(self._url(path))
        if auth:
            req.add_header("Authorization", self._auth())
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode()

    def _post(self, path, fields, auth=True):
        data = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
        req = urllib.request.Request(self._url(path), data=data, method="POST")
        if auth:
            req.add_header("Authorization", self._auth())
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read().decode(), resp.headers
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode(), err.headers

    def _post_raw(self, path, data, auth=True):
        req = urllib.request.Request(self._url(path), data=data, method="POST")
        if auth:
            req.add_header("Authorization", self._auth())
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode()

    def test_form_is_public_and_shows_ten_service_rows(self):
        status, body = self._get("/onboard", auth=False)
        self.assertEqual(status, 200)
        self.assertIn("Set up your shop", body)
        self.assertIn("action='/onboard'", body)
        self.assertEqual(body.count("name='service'"), 10)
        self.assertIn("facial ka price kya hai?", body)
        self.assertIn("Price varies", body)
        self.assertIn("Type it again", body)
        self.assertNotIn("Hello! Welcome to Glow & Grace.", body)
        self.assertNotIn("action='/setup/save'", body)
        self.assertNotIn("<script", body)

    def test_valid_submission_shows_the_confirmation(self):
        before = self.path.read_text(encoding="utf-8")
        status, body, _ = self._post("/onboard", valid_fields(), auth=False)
        self.assertEqual(status, 200)
        self.assertIn("Thanks, Ritu.", body)
        self.assertIn("Snips Salon", body)
        self.assertIn("Hair Spa: price varies, the salon confirms it", body)
        self.assertIn("Shampoo: Rs 199, 30 minutes", body)
        self.assertIn("Open 10:00 AM - 8:00 PM every day", body)
        self.assertIn("Hair Spa ka price kya hai?", body)
        self.assertIn("the price depends on the service", body)
        self.assertIn("Send this file to Aditya", body)
        self.assertIn("action='/onboard/download'", body)
        self.assertIn("Change something", body)
        self.assertIn("type='hidden' name='phone' value='919899127791'", body)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.studio.profile.name, "Glow & Grace Salon")

    def test_invalid_submission_keeps_what_she_typed(self):
        status, body, _ = self._post(
            "/onboard", valid_fields(phone=["123"]), auth=False
        )
        self.assertEqual(status, 200)
        self.assertIn("does not look like an Indian mobile number", body)
        self.assertIn("value='Snips Salon'", body)
        self.assertIn("Set up your shop", body)
        self.assertNotIn("Download your shop file", body)

    def test_script_in_the_shop_name_comes_back_escaped(self):
        status, body, _ = self._post(
            "/onboard",
            valid_fields(name=["<script>alert(1)</script>"]),
            auth=False,
        )
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>alert", body)

    def test_download_returns_the_profile_as_a_file(self):
        before = self.path.read_text(encoding="utf-8")
        status, body, headers = self._post(
            "/onboard/download", valid_fields(), auth=False
        )
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn("snips-salon.json", headers.get("Content-Disposition", ""))
        profile = json.loads(body)
        back = BusinessProfile.from_dict(profile)
        self.assertEqual(back.name, "Snips Salon")
        self.assertEqual(back.phone, "9899127791")
        self.assertEqual(len(back.services), 2)
        self.assertEqual(back.to_dict()["services"], profile["services"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.studio.profile.name, "Glow & Grace Salon")

    def test_download_with_bad_fields_shows_the_form_again(self):
        status, body, _ = self._post(
            "/onboard/download", valid_fields(phone=["123"]), auth=False
        )
        self.assertEqual(status, 200)
        self.assertIn("does not look like an Indian mobile number", body)

    def test_edit_round_trip_prefills_the_form(self):
        status, body, _ = self._post(
            "/onboard",
            dict(valid_fields(), _action=["edit"]),
            auth=False,
        )
        self.assertEqual(status, 200)
        self.assertIn("Set up your shop", body)
        self.assertIn("value='Snips Salon'", body)
        self.assertIn("value='Hair Spa'", body)
        self.assertNotIn("Download your shop file", body)

    def test_oversized_body_is_refused(self):
        data = urllib.parse.urlencode(
            {"name": "x" * 70000}, doseq=True
        ).encode("utf-8")
        status, _ = self._post_raw("/onboard", data, auth=False)
        self.assertEqual(status, 413)

    def test_oversized_setup_save_is_refused(self):
        data = urllib.parse.urlencode(
            {"service": "x" * 70000}, doseq=True
        ).encode("utf-8")
        status, _ = self._post_raw("/setup/save", data, auth=True)
        self.assertEqual(status, 413)

    def test_dashboard_footer_links_to_onboarding(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("href='/onboard'", body)
        self.assertIn("Set up your shop", body)
        self.assertNotIn("forms.gle", body)


if __name__ == "__main__":
    unittest.main()
