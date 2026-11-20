"""The owner setup form: prices, times and hours on the dashboard.

The shop menu lives in a JSON profile file, and a real owner cannot
fill in prices without editing it. The Setup tab lets the owner type
prices, durations and hours on her phone, behind the dashboard
password, and the bot quotes the new values at once. A save that fails
any check changes nothing: no half saved menu ever reaches a customer.

Time-dependent checks pin explicit times or use tomorrow, never the
current hour, so the suite passes whatever hour it runs in.
"""
import base64
import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

from test_llm import FixedLLM
from whatsapp_studio.app import Studio, make_server
from whatsapp_studio.bookings import BookingStore, india_today
from whatsapp_studio.config import (
    BusinessProfile,
    Service,
    parse_setup_day_hours,
    parse_setup_duration,
    parse_setup_price,
    validate_setup_hours,
    validate_setup_services,
)
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore

DAY_HOURS = "10:00 AM - 8:00 PM"


def make_profile():
    return BusinessProfile(
        name="Glow & Grace Salon",
        category="salon",
        city="Gurgaon",
        phone="919999999999",
        hours={"daily": DAY_HOURS},
        services=[
            Service("Haircut", 499, 30, floor_price=400),
            Service("Facial", None, 60),
        ],
        greeting="Hello! Welcome to Glow & Grace.",
        address="Sector 50, Gurgaon",
        offers="",
    )


def write_profile(tmp, profile=None, extra=None):
    """A profile file on disk, with an extra key the dataclass drops,
    like the Snips _draft note. Returns the path."""
    data = (profile or make_profile()).to_dict()
    if extra:
        data.update(extra)
    path = Path(tmp) / "profile.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def make_studio(tmp, profile_path=None, **kwargs):
    kwargs.setdefault("llm", FixedLLM("Thanks for messaging!"))
    return Studio(
        BusinessProfile.from_json(Path(tmp) / "profile.json"),
        LeadStore(Path(tmp) / "leads.json"),
        bookings=BookingStore(Path(tmp) / "bookings.json"),
        handoffs=HandoffStore(Path(tmp) / "handoffs.json"),
        outbox_path=Path(tmp) / "outbox.json",
        profile_path=profile_path,
        **kwargs,
    )


def valid_fields(**overrides):
    fields = {
        "service": ["Haircut", "Facial"],
        "price": ["499", "1499"],
        "duration": ["30", "60"],
    }
    for key in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        fields["hours_" + key] = [DAY_HOURS]
    fields.update(overrides)
    return fields


class TestSetupParsing(unittest.TestCase):
    def test_price_blank_means_on_request(self):
        self.assertEqual(parse_setup_price(""), (None, None))
        self.assertEqual(parse_setup_price("   "), (None, None))

    def test_price_whole_rupees_only(self):
        self.assertEqual(parse_setup_price("499"), (499, None))
        self.assertEqual(parse_setup_price("100000"), (100000, None))
        for bad in ("499.5", "abc", "0", "-5", "100001", "4 99"):
            _, error = parse_setup_price(bad)
            self.assertIsNotNone(error, bad)

    def test_duration_minutes_only(self):
        self.assertEqual(parse_setup_duration("45"), (45, None))
        for bad in ("", "abc", "4", "481", "45.5", "0"):
            _, error = parse_setup_duration(bad)
            self.assertIsNotNone(error, bad)

    def test_day_hours_closed_and_ranges(self):
        self.assertEqual(parse_setup_day_hours("closed"), ("closed", None))
        self.assertEqual(parse_setup_day_hours("CLOSED"), ("closed", None))
        self.assertEqual(parse_setup_day_hours(" closed "), ("closed", None))
        self.assertEqual(
            parse_setup_day_hours("10:00 AM - 8:00 PM"),
            ("10:00 AM - 8:00 PM", None),
        )
        self.assertEqual(
            parse_setup_day_hours("10:00-20:00"),
            ("10:00 AM - 8:00 PM", None),
        )
        for bad in ("", "nonsense", "8:00 PM - 10:00 AM", "10:00 AM"):
            _, error = parse_setup_day_hours(bad)
            self.assertIsNotNone(error, bad)

    def test_validate_hours_collapses_equal_days(self):
        hours, error = validate_setup_hours([DAY_HOURS] * 7)
        self.assertIsNone(error)
        self.assertEqual(hours, {"daily": DAY_HOURS})

    def test_validate_hours_keeps_days_that_differ(self):
        days = [DAY_HOURS] * 7
        days[1] = "closed"
        hours, error = validate_setup_hours(days)
        self.assertIsNone(error)
        self.assertEqual(hours["tue"], "closed")
        self.assertEqual(hours["mon"], DAY_HOURS)
        self.assertNotIn("daily", hours)

    def test_validate_hours_names_the_bad_day(self):
        days = [DAY_HOURS] * 7
        days[4] = "nonsense"
        _, error = validate_setup_hours(days)
        self.assertIn("Friday", error)

    def test_validate_services_keeps_order_and_hidden_fields(self):
        services, error, warnings = validate_setup_services(
            make_profile(),
            [("Haircut", "499", "30"), ("Facial", "1499", "60")],
        )
        self.assertIsNone(error)
        self.assertEqual(warnings, [])
        self.assertEqual([s.name for s in services], ["Haircut", "Facial"])
        self.assertEqual(services[0].floor_price, 400)
        self.assertEqual(services[0].price, 499)
        self.assertIsNone(services[1].floor_price)
        self.assertEqual(services[1].price, 1499)

    def test_validate_services_refuses_unknown_or_duplicate_names(self):
        _, error, _ = validate_setup_services(
            make_profile(),
            [("Haircut", "499", "30"), ("Haircut", "1499", "60")],
        )
        self.assertIsNotNone(error)
        _, error, _ = validate_setup_services(
            make_profile(),
            [("Haircut", "499", "30"), ("Shave", "99", "15")],
        )
        self.assertIsNotNone(error)
        _, error, _ = validate_setup_services(
            make_profile(),
            [("Haircut", "499", "30")],
        )
        self.assertIsNotNone(error)

    def test_validate_services_names_the_bad_row(self):
        _, error, _ = validate_setup_services(
            make_profile(),
            [("Haircut", "abc", "30"), ("Facial", "1499", "60")],
        )
        self.assertIn("Haircut", error)
        self.assertIn("whole rupees", error)

    def test_validate_services_warns_on_big_price_move_and_floor(self):
        _, _, warnings = validate_setup_services(
            make_profile(),
            [("Haircut", "49", "30"), ("Facial", "1499", "60")],
        )
        self.assertEqual(len(warnings), 2)
        self.assertIn("Haircut changed from Rs 499 to Rs 49", warnings[0])
        self.assertIn("below the lowest price", warnings[1])


class TestSetupRoundTrip(unittest.TestCase):
    def test_to_dict_and_back_keeps_every_field(self):
        profile = make_profile()
        back = BusinessProfile.from_dict(profile.to_dict())
        self.assertEqual(back.name, profile.name)
        self.assertEqual(back.category, profile.category)
        self.assertEqual(back.city, profile.city)
        self.assertEqual(back.phone, profile.phone)
        self.assertEqual(back.hours, profile.hours)
        self.assertEqual(back.greeting, profile.greeting)
        self.assertEqual(back.address, profile.address)
        self.assertEqual(back.offers, profile.offers)
        self.assertEqual(len(back.services), 2)
        self.assertEqual(back.services[0].price, 499)
        self.assertEqual(back.services[0].floor_price, 400)
        self.assertEqual(back.services[0].capacity, 1)
        self.assertIsNone(back.services[1].price)

    def test_from_json_survives_extra_keys_in_service_dicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = make_profile().to_dict()
            data["services"][0]["surprise"] = "kept out of the object"
            path = Path(tmp) / "profile.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            profile = BusinessProfile.from_json(path)
            self.assertEqual(profile.services[0].price, 499)


class TestApplySetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = write_profile(
            self.tmp.name, extra={"_draft": {"note": "confirm with the owner"}}
        )
        self.studio = make_studio(self.tmp.name, profile_path=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_updates_file_memory_and_backup(self):
        notice = self.studio.apply_setup(valid_fields(price=["549", "1499"]))
        self.assertTrue(notice.startswith("Saved."), notice)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["services"][0]["price"], 549)
        self.assertEqual(saved["services"][1]["price"], 1499)
        self.assertEqual(saved["hours"], {"daily": DAY_HOURS})
        self.assertEqual(saved["_draft"]["note"], "confirm with the owner")
        backup = json.loads(Path(str(self.path) + ".bak").read_text(encoding="utf-8"))
        self.assertIsNone(backup["services"][1]["price"])
        self.assertEqual(self.studio.profile.services[0].price, 549)
        self.assertEqual(self.studio.profile.services[1].price, 1499)
        result = self.studio._run_tool(
            "get_price", {"service": "Facial"}, "919810000001", "Priya", None, ""
        )
        self.assertEqual(result["price"], 1499)

    def test_save_with_blank_price_keeps_price_on_request(self):
        notice = self.studio.apply_setup(valid_fields(price=["499", ""]))
        self.assertTrue(notice.startswith("Saved."), notice)
        self.assertIsNone(self.studio.profile.services[1].price)

    def test_invalid_save_changes_nothing(self):
        before = self.path.read_text(encoding="utf-8")
        notice = self.studio.apply_setup(valid_fields(price=["abc", "1499"]))
        self.assertIn("Haircut", notice)
        self.assertIn("whole rupees", notice)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.studio.profile.services[0].price, 499)
        self.assertIsNone(self.studio.profile.services[1].price)
        self.assertFalse(Path(str(self.path) + ".bak").exists())

    def test_save_without_a_profile_path_refuses(self):
        studio = make_studio(self.tmp.name, profile_path=None)
        notice = studio.apply_setup(valid_fields())
        self.assertIn("no profile file", notice)
        self.assertEqual(studio.profile.services[0].price, 499)

    def test_save_to_an_unwritable_path_fails_cleanly(self):
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        studio = make_studio(
            self.tmp.name, profile_path=blocker / "profile.json"
        )
        notice = studio.apply_setup(valid_fields())
        self.assertIn("save failed", notice.lower())
        self.assertEqual(studio.profile.services[0].price, 499)

    def test_demo_mode_save_refuses(self):
        studio = make_studio(
            self.tmp.name, profile_path=self.path, demo_mode=True
        )
        before = self.path.read_text(encoding="utf-8")
        notice = studio.apply_setup(valid_fields())
        self.assertIn("demo", notice.lower())
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_closing_a_day_with_bookings_warns(self):
        tomorrow = india_today() + timedelta(days=1)
        self.studio.bookings.book(
            "919810000002", "Neha", make_profile().services[0],
            tomorrow, 15 * 60,
        )
        days = [DAY_HOURS] * 7
        days[tomorrow.weekday()] = "closed"
        fields = valid_fields()
        for key, value in zip(
            ("mon", "tue", "wed", "thu", "fri", "sat", "sun"), days
        ):
            fields["hours_" + key] = [value]
        notice = self.studio.apply_setup(fields)
        self.assertTrue(notice.startswith("Saved."), notice)
        self.assertIn("1 booking sits on a day you just closed", notice)

    def test_checklist_flips_once_every_price_is_filled(self):
        items = dict(
            (label, ok) for label, ok, _ in self.studio.setup_checklist()
        )
        self.assertFalse(items["Services and prices filled in"])
        self.studio.apply_setup(valid_fields())
        items = dict(
            (label, ok) for label, ok, _ in self.studio.setup_checklist()
        )
        self.assertTrue(items["Services and prices filled in"])

    def test_setup_copy_has_no_dash(self):
        notice = self.studio.apply_setup(valid_fields(price=["49", "1499"]))
        self.assertNotIn("-", notice)
        self.assertNotIn("\u2013", notice)
        self.assertNotIn("\u2014", notice)


class TestSetupFormHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = write_profile(self.tmp.name)
        self.studio = make_studio(
            self.tmp.name, profile_path=self.path, dashboard_password="secret"
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
        return "Basic " + base64.b64encode(b"owner:secret").decode()

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
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode()

    def test_setup_tab_shows_the_form(self):
        status, body = self._get("/?tab=setup")
        self.assertEqual(status, 200)
        self.assertIn("Setup", body)
        self.assertIn("action='/setup/save'", body)
        self.assertIn("Haircut", body)
        self.assertIn("Facial", body)
        self.assertIn("1 of 2 services still need a price", body)
        self.assertIn("Save prices and hours", body)
        self.assertIn("until the next deploy", body)

    def test_setup_tab_needs_auth(self):
        status, _ = self._get("/?tab=setup", auth=False)
        self.assertEqual(status, 401)

    def test_save_over_http_updates_profile_and_quotes_new_price(self):
        status, body = self._post("/setup/save", valid_fields(price=["549", "1499"]))
        self.assertEqual(status, 200)
        self.assertIn("Saved. The bot now quotes these prices.", body)
        self.assertEqual(self.studio.profile.services[0].price, 549)
        result = self.studio._run_tool(
            "get_price", {"service": "Haircut"}, "919810000001", "Priya", None, ""
        )
        self.assertEqual(result["price"], 549)

    def test_invalid_save_over_http_changes_nothing(self):
        before = self.path.read_text(encoding="utf-8")
        status, body = self._post(
            "/setup/save", valid_fields(duration=["30", "abc"])
        )
        self.assertEqual(status, 200)
        self.assertIn("Facial", body)
        self.assertIn("whole minutes", body)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_save_without_auth_is_refused(self):
        status, _ = self._post(
            "/setup/save", valid_fields(), auth=False
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.studio.profile.services[0].price, 499)

    def test_demo_mode_tab_shows_a_note_and_refuses_save(self):
        studio = make_studio(
            self.tmp.name, profile_path=self.path, demo_mode=True
        )
        page = studio.dashboard_html("setup")
        self.assertIn("demo shop", page)
        self.assertNotIn("Save prices and hours", page)
        before = self.path.read_text(encoding="utf-8")
        self.assertIn("demo", studio.apply_setup(valid_fields()).lower())
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_export_includes_the_profile(self):
        self.studio.apply_setup(valid_fields(price=["549", "1499"]))
        status, body = self._get("/export")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["profile"]["name"], "Glow & Grace Salon")
        self.assertEqual(payload["profile"]["services"][0]["price"], 549)


if __name__ == "__main__":
    unittest.main()
