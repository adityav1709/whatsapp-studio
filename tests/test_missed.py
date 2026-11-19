"""Bad experience records: bookings the bot had to refuse and
questions it could not answer, so the owner can call them back.

The follow up from the dashboard redesign plan: a record of why a
booking was refused, and a record of questions the bot could not
answer, so they can appear under bad experience. A refusal the bot
fixed in the same turn is good service, not a bad experience, and
never lands here.
"""
import http.client
import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from test_llm import C, FixedLLM, RaisingLLM, ScriptedLLM, T
from whatsapp_studio.app import Studio, make_server
from whatsapp_studio.bookings import (
    BookingStore,
    IST,
    india_today,
)
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.missed import (
    MissedStore,
    REFUSAL_ERRORS,
    plain_reason,
)
from whatsapp_studio.send import WhatsAppSender

# Test phones must not start with the demo prefix 9198100000, or
# the store refuses them as sample data.
PRIYA = "919999900001"
RAHUL = "919999900002"
ASHA = "919999900003"
MEERA = "919999900004"


def _profile(closed=False):
    hours = {"daily": "10:00 AM - 8:00 PM"}
    if closed:
        hours = {"daily": "closed"}
    return BusinessProfile(
        name="Glow & Grace Salon",
        category="salon",
        city="Gurgaon",
        phone="919999999999",
        hours=hours,
        services=[
            Service("Haircut", 499, 30, floor_price=400),
            Service("Facial", 1499, 60),
        ],
        address="Sector 50, Gurgaon",
    )


def _studio(tmp, llm=None, profile=None, demo_mode=False):
    return Studio(
        profile or _profile(),
        LeadStore(Path(tmp) / "leads.json"),
        bookings=BookingStore(),
        handoffs=HandoffStore(),
        missed=MissedStore(),
        llm=llm if llm is not None else ScriptedLLM([]),
        sender=WhatsAppSender("tok", "pid"),
        demo_mode=demo_mode,
    )


def _tomorrow():
    return india_today() + timedelta(days=1)


def _book_script(service, day, time, reply_text, price=None):
    args = {"service": service, "day": day, "time": time}
    if price is not None:
        args["price"] = price
    return [T("create_booking", args), C(reply_text)]


def _post(port, path, fields=None):
    """POST without following redirects, so a 303 stays a 303."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = urllib.parse.urlencode(fields or {})
    conn.request(
        "POST",
        path,
        body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = conn.getresponse()
    status = resp.status
    resp.read()
    conn.close()
    return status


def _missed_text(page):
    match = re.search(r"Bad experience(.*?)</section>", page, re.S)
    assert match is not None, "no Bad experience section on the page"
    return re.sub(r"<[^>]+>", "", match.group(1))


class TestMissedStore(unittest.TestCase):
    def test_roundtrip_and_corrupt_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missed.json"
            store = MissedStore(path)
            store.record_refused(
                PRIYA, "Priya", "Haircut", _tomorrow(), 660,
                "already taken", ["10:30 AM"], "book haircut tomorrow 11am",
            )
            reloaded = MissedStore(path)
            self.assertEqual(len(reloaded.all_records()), 1)
            self.assertEqual(reloaded.all_records()[0]["service"], "Haircut")
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(MissedStore(path).all_records(), [])

    def test_same_slot_twice_is_one_record(self):
        store = MissedStore()
        day = _tomorrow()
        store.record_refused(
            PRIYA, "Priya", "Haircut", day, 660,
            "already taken", ["10:30 AM"], "book haircut tomorrow 11am",
        )
        store.record_refused(
            PRIYA, "Priya", "Haircut", day, 660,
            "already taken", ["10:30 AM"], "book haircut tomorrow 11am",
        )
        records = store.all_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["occurrences"], 2)

    def test_different_slot_is_a_new_record(self):
        store = MissedStore()
        day = _tomorrow()
        store.record_refused(
            PRIYA, "Priya", "Haircut", day, 660,
            "already taken", ["10:30 AM"], "book haircut tomorrow 11am",
        )
        store.record_refused(
            PRIYA, "Priya", "Haircut", day, 720,
            "already taken", ["12:30 PM"], "book haircut tomorrow 12pm",
        )
        self.assertEqual(len(store.all_records()), 2)

    def test_same_question_twice_is_one_record(self):
        store = MissedStore()
        store.record_unanswered(
            PRIYA, "Priya", "do you do bridal makeup?", "cannot answer", 1
        )
        store.record_unanswered(
            PRIYA, "Priya", "do you do bridal makeup?", "cannot answer", 1
        )
        records = store.all_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["occurrences"], 2)

    def test_demo_phone_is_never_recorded(self):
        store = MissedStore()
        self.assertIsNone(
            store.record_refused(
                "919810000099", "Demo", "Haircut", _tomorrow(), 660,
                "already taken", [], "book haircut tomorrow 11am",
            )
        )
        self.assertIsNone(
            store.record_unanswered(
                "919810000099", "Demo", "do you do bridal?", "cannot answer"
            )
        )
        self.assertEqual(store.all_records(), [])

    def test_resolve_needs_a_valid_reason(self):
        store = MissedStore()
        record = store.record_refused(
            PRIYA, "Priya", "Haircut", _tomorrow(), 660,
            "already taken", [], "book haircut tomorrow 11am",
        )
        self.assertIsNone(store.resolve(record["id"], "lost"))
        self.assertIsNotNone(store.resolve(record["id"], "called"))
        self.assertEqual(store.all_records()[0]["status"], "resolved")
        self.assertEqual(store.all_records()[0]["resolved_how"], "called")

    def test_resolve_for_booking_only_matches_the_service(self):
        store = MissedStore()
        day = _tomorrow()
        store.record_refused(
            PRIYA, "Priya", "Haircut", day, 660,
            "already taken", [], "book haircut tomorrow 11am",
        )
        store.record_refused(
            PRIYA, "Priya", "Facial", day, 660,
            "already taken", [], "book facial tomorrow 11am",
        )
        store.resolve_for_booking(PRIYA, "Haircut")
        by_service = {r["service"]: r for r in store.all_records()}
        self.assertEqual(by_service["Haircut"]["status"], "resolved")
        self.assertEqual(by_service["Haircut"]["resolved_how"], "booked")
        self.assertEqual(by_service["Facial"]["status"], "open")

    def test_resolve_for_handoff(self):
        store = MissedStore()
        store.record_unanswered(
            PRIYA, "Priya", "do you do bridal makeup?", "cannot answer", 7
        )
        store.record_unanswered(
            ASHA, "Asha", "is parking free?", "cannot answer", 8
        )
        store.resolve_for_handoff(7, "answered")
        by_phone = {r["phone"]: r for r in store.all_records()}
        self.assertEqual(by_phone[PRIYA]["status"], "resolved")
        self.assertEqual(by_phone[PRIYA]["resolved_how"], "answered")
        self.assertEqual(by_phone[ASHA]["status"], "open")

    def test_open_newest_is_newest_first(self):
        store = MissedStore()
        store.record_refused(
            PRIYA, "Priya", "Haircut", _tomorrow(), 660,
            "already taken", [], "first", now_ts=1000,
        )
        store.record_refused(
            ASHA, "Asha", "Haircut", _tomorrow(), 720,
            "already taken", [], "second", now_ts=2000,
        )
        self.assertEqual(
            [r["phone"] for r in store.open_newest()], [ASHA, PRIYA]
        )

    def test_open_today_counts_only_today(self):
        store = MissedStore()
        store.record_refused(
            PRIYA, "Priya", "Haircut", _tomorrow(), 660,
            "already taken", [], "today",
        )
        yesterday = int(
            (datetime.now(IST) - timedelta(days=1)).timestamp()
        )
        store.record_refused(
            ASHA, "Asha", "Haircut", _tomorrow(), 660,
            "already taken", [], "yesterday", now_ts=yesterday,
        )
        self.assertEqual(store.open_today(), 1)

    def test_resolved_older_than_thirty_days_are_pruned(self):
        store = MissedStore()
        old = store.record_refused(
            PRIYA, "Priya", "Haircut", _tomorrow(), 660,
            "already taken", [], "old",
        )
        store.resolve(old["id"], "called")
        store._records[0]["resolved_at"] = int(
            (datetime.now(IST) - timedelta(days=31)).timestamp()
        )
        store.record_refused(
            ASHA, "Asha", "Haircut", _tomorrow(), 660,
            "already taken", [], "new",
        )
        phones = [r["phone"] for r in store.all_records()]
        self.assertEqual(phones, [ASHA])

    def test_open_records_are_never_pruned(self):
        store = MissedStore()
        ancient = int((datetime.now(IST) - timedelta(days=90)).timestamp())
        store.record_refused(
            PRIYA, "Priya", "Haircut", _tomorrow(), 660,
            "already taken", [], "ancient", now_ts=ancient,
        )
        store.record_refused(
            ASHA, "Asha", "Haircut", _tomorrow(), 660,
            "already taken", [], "new",
        )
        self.assertEqual(len(store.all_records()), 2)

    def test_plain_reason_covers_every_refusal(self):
        for error in REFUSAL_ERRORS:
            self.assertNotEqual(plain_reason(error), error)
            self.assertIn(".", plain_reason(error))
        self.assertEqual(
            plain_reason("already taken"), "That slot was already taken."
        )
        self.assertEqual(
            plain_reason("cannot answer"), "Bot did not know the answer."
        )
        self.assertEqual(plain_reason("model failed"), "The bot could not reply.")
        self.assertEqual(plain_reason("something new"), "something new")


class TestMissedFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = _studio(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _script(self, *turns):
        self.studio.llm = ScriptedLLM(list(turns))

    def test_already_taken_is_recorded(self):
        service = _profile().services[0]
        self.studio.bookings.book(
            RAHUL, "Rahul", service, _tomorrow(), 660
        )
        self._script(*_book_script(
            "Haircut", "tomorrow", "11am",
            "Sorry, 11:00 AM tomorrow is already taken.",
        ))
        self.studio.handle_message(
            PRIYA, "book haircut tomorrow 11am", "Priya"
        )
        records = self.studio.missed.all_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "refused")
        self.assertEqual(records[0]["reason"], "already taken")
        self.assertEqual(records[0]["service"], "Haircut")
        self.assertEqual(records[0]["start_min"], 660)
        self.assertEqual(records[0]["date"], _tomorrow().isoformat())
        self.assertEqual(records[0]["phone"], PRIYA)
        self.assertEqual(records[0]["name"], "Priya")
        self.assertIn("10:30 AM", records[0]["alternatives"])
        self.assertEqual(records[0]["trigger"], "book haircut tomorrow 11am")

    def test_each_refusal_reason_is_recorded(self):
        cases = [
            ("closed", _profile(closed=True), "Haircut", "tomorrow", "11am", None),
            ("time passed", None, "Haircut", "today", "11am", None),
            ("invalid slot", None, "Haircut", "tomorrow", "11:20am", None),
            ("could not understand the time", None, "Haircut", "tomorrow", "sometime", None),
            ("below the lowest price", None, "Haircut", "tomorrow", "11am", 300),
            ("fixed price", None, "Facial", "tomorrow", "11am", 1200),
        ]
        for error, profile, service, day, time, price in cases:
            with tempfile.TemporaryDirectory() as tmp:
                studio = _studio(tmp, profile=profile)
                now = datetime(2026, 11, 17, 15, 0, tzinfo=IST)
                studio.llm = ScriptedLLM(
                    _book_script(
                        service, day, time,
                        "Sorry, that did not work.", price=price,
                    )
                )
                studio.handle_message(
                    PRIYA, f"book {service} {day} {time}", "Priya", now=now
                )
                records = studio.missed.all_records()
                self.assertEqual(len(records), 1, error)
                self.assertEqual(records[0]["reason"], error, error)

    def test_not_free_when_the_owner_blocked_the_slot(self):
        self.studio.blocks.add(_tomorrow(), 660, 690, "Lunch")
        self._script(*_book_script(
            "Haircut", "tomorrow", "11am",
            "Sorry, 11:00 AM tomorrow is not available.",
        ))
        self.studio.handle_message(
            PRIYA, "book haircut tomorrow 11am", "Priya"
        )
        records = self.studio.missed.all_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["reason"], "not free")

    def test_two_tries_at_the_same_slot_make_one_record(self):
        service = _profile().services[0]
        self.studio.bookings.book(
            RAHUL, "Rahul", service, _tomorrow(), 660
        )
        self._script(
            T("create_booking", {"service": "Haircut", "day": "tomorrow", "time": "11am"}),
            T("create_booking", {"service": "Haircut", "day": "tomorrow", "time": "11am"}),
            C("Sorry, 11:00 AM tomorrow is already taken."),
        )
        self.studio.handle_message(
            PRIYA, "book haircut tomorrow 11am", "Priya"
        )
        records = self.studio.missed.all_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["occurrences"], 2)

    def test_refusal_then_booking_in_the_same_turn_is_not_recorded(self):
        service = _profile().services[0]
        self.studio.bookings.book(
            RAHUL, "Rahul", service, _tomorrow(), 660
        )
        self._script(
            T("create_booking", {"service": "Haircut", "day": "tomorrow", "time": "11am"}),
            T("create_booking", {"service": "Haircut", "day": "tomorrow", "time": "12pm"}),
            C("Done! Your Haircut is booked for tomorrow at 12:00 PM."),
        )
        self.studio.handle_message(
            PRIYA, "book haircut tomorrow 11am", "Priya"
        )
        self.assertEqual(self.studio.missed.all_records(), [])
        self.assertEqual(len(self.studio.bookings.all_bookings()), 2)

    def test_already_yours_is_a_win_not_a_missed_customer(self):
        self._script(*_book_script(
            "Haircut", "tomorrow", "11am",
            "Done! Your Haircut is booked for tomorrow at 11:00 AM.",
        ))
        self.studio.handle_message(
            PRIYA, "book haircut tomorrow 11am", "Priya"
        )
        self._script(*_book_script(
            "Haircut", "tomorrow", "11am",
            "You already have that booking.",
        ))
        self.studio.handle_message(
            PRIYA, "book haircut tomorrow 11am again", "Priya"
        )
        self.assertEqual(self.studio.missed.all_records(), [])
        self.assertEqual(len(self.studio.bookings.all_bookings()), 1)

    def test_later_booking_resolves_the_refusal_as_booked(self):
        service = _profile().services[0]
        self.studio.bookings.book(
            RAHUL, "Rahul", service, _tomorrow(), 660
        )
        self._script(*_book_script(
            "Haircut", "tomorrow", "11am",
            "Sorry, 11:00 AM tomorrow is already taken.",
        ))
        self.studio.handle_message(
            PRIYA, "book haircut tomorrow 11am", "Priya"
        )
        self.assertEqual(len(self.studio.missed.open_newest()), 1)
        self._script(*_book_script(
            "Haircut", "tomorrow", "12pm",
            "Done! Your Haircut is booked for tomorrow at 12:00 PM.",
        ))
        self.studio.handle_message(
            PRIYA, "book haircut tomorrow 12pm", "Priya"
        )
        self.assertEqual(self.studio.missed.open_newest(), [])
        self.assertEqual(
            self.studio.missed.all_records()[0]["resolved_how"], "booked"
        )

    def test_reschedule_refusal_and_resolve(self):
        self._script(*_book_script(
            "Haircut", "tomorrow", "12pm",
            "Done! Your Haircut is booked for tomorrow at 12:00 PM.",
        ))
        self.studio.handle_message(
            PRIYA, "book haircut tomorrow 12pm", "Priya"
        )
        service = _profile().services[0]
        self.studio.bookings.book(
            RAHUL, "Rahul", service, _tomorrow(), 660
        )
        self._script(
            T("reschedule_booking", {"service": "Haircut", "day": "tomorrow", "time": "11am"}),
            C("Sorry, 11:00 AM tomorrow is already taken."),
        )
        self.studio.handle_message(
            PRIYA, "move my haircut to tomorrow 11am", "Priya"
        )
        records = self.studio.missed.all_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["reason"], "already taken")
        self._script(
            T("reschedule_booking", {"service": "Haircut", "day": "tomorrow", "time": "1pm"}),
            C("Done! Moved to tomorrow at 1:00 PM."),
        )
        self.studio.handle_message(
            PRIYA, "move my haircut to tomorrow 1pm", "Priya"
        )
        self.assertEqual(self.studio.missed.open_newest(), [])
        self.assertEqual(
            self.studio.missed.all_records()[0]["resolved_how"], "booked"
        )

    def test_hand_over_cannot_answer_records_one(self):
        self._script(
            T("hand_over", {"reason": "cannot answer: do you do bridal makeup?"}),
            C("Let me pass you to the team, they will reply here soon."),
        )
        self.studio.handle_message(
            PRIYA, "do you do bridal makeup?", "Priya"
        )
        records = self.studio.missed.all_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "unanswered")
        self.assertEqual(records[0]["reason"], "cannot answer")
        self.assertEqual(records[0]["question"], "do you do bridal makeup?")
        self.assertEqual(records[0]["handoff_id"], 1)

    def test_hand_over_for_a_person_records_none(self):
        self._script(
            T("hand_over", {"reason": "asked for a person"}),
            C("Let me pass you to the team."),
        )
        self.studio.handle_message(PRIYA, "i want to talk to a person", "Priya")
        self.assertEqual(self.studio.missed.all_records(), [])

    def test_model_failure_records_one_unanswered(self):
        studio = _studio(self.tmp.name, llm=RaisingLLM())
        studio.handle_message(PRIYA, "do you do bridal makeup?", "Priya")
        records = studio.missed.all_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "unanswered")
        self.assertEqual(records[0]["reason"], "model failed")
        self.assertIsNotNone(records[0]["handoff_id"])

    def test_no_record_without_a_key(self):
        studio = Studio(
            _profile(),
            LeadStore(Path(self.tmp.name) / "leads2.json"),
            bookings=BookingStore(),
            missed=MissedStore(),
            llm=None,
        )
        studio.handle_message(PRIYA, "book haircut tomorrow 11am", "Priya")
        self.assertEqual(studio.missed.all_records(), [])

    def test_no_record_while_a_handoff_is_open(self):
        self._script(
            T("hand_over", {"reason": "asked for a person"}),
            C("Let me pass you to the team."),
        )
        self.studio.handle_message(PRIYA, "i want to talk to a person", "Priya")
        self.studio.handle_message(PRIYA, "book haircut tomorrow 11am", "Priya")
        self.assertEqual(self.studio.missed.all_records(), [])

    def test_no_record_in_demo_mode(self):
        studio = _studio(self.tmp.name, demo_mode=True)
        service = _profile().services[0]
        studio.bookings.book(RAHUL, "Rahul", service, _tomorrow(), 660)
        studio.llm = ScriptedLLM(_book_script(
            "Haircut", "tomorrow", "11am",
            "Sorry, 11:00 AM tomorrow is already taken.",
        ))
        # The demo seed itself carries sample missed records, so the
        # turn must add none rather than the store staying empty.
        before = len(studio.missed.all_records())
        studio.handle_message(PRIYA, "book haircut tomorrow 11am", "Priya")
        self.assertEqual(len(studio.missed.all_records()), before)

    def test_no_record_from_a_voice_note(self):
        self.studio.handle_non_text_message(PRIYA, "audio", "Priya")
        self.assertEqual(self.studio.missed.all_records(), [])
        self.assertEqual(len(self.studio.handoffs.all_handoffs()), 1)

    def test_plain_i_do_not_know_records_nothing(self):
        studio = _studio(
            self.tmp.name, llm=FixedLLM("I do not know, sorry.")
        )
        studio.handle_message(PRIYA, "do you do bridal makeup?", "Priya")
        self.assertEqual(studio.missed.all_records(), [])


class TestMissedDashboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = _studio(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _refuse(self, phone, name, reason="already taken", start=660):
        return self.studio.missed.record_refused(
            phone, name, "Haircut", _tomorrow(), start,
            reason, ["10:30 AM"], "book haircut tomorrow 11am",
        )

    def test_dashboard_shows_bad_experience(self):
        self._refuse(PRIYA, "Priya")
        self._refuse(ASHA, "Asha", reason="closed", start=600)
        page = self.studio.dashboard_html("today")
        self.assertIn("Bad experience", page)
        text = _missed_text(page)
        self.assertIn("Priya", text)
        self.assertIn("That slot was already taken.", text)
        self.assertIn("Haircut", text)
        self.assertIn("11:00 AM", text)
        self.assertIn("Asha", text)
        self.assertIn("Shop is closed that day.", text)
        self.assertIn("Call Priya", text)
        self.assertIn("Mark handled", text)
        self.assertIn("Bot offered: 10:30 AM.", text)
        self.assertIn("2 people left without booking today.", page)

    def test_dashboard_hides_bad_experience_when_there_is_none(self):
        page = self.studio.dashboard_html("today")
        self.assertNotIn("Bad experience", page)
        self.assertNotIn("left without booking today", page)

    def test_dashboard_shows_newest_three_and_counts_the_rest(self):
        for phone in (PRIYA, RAHUL, ASHA, MEERA):
            self._refuse(phone, phone[-4:])
        page = self.studio.dashboard_html("today")
        text = _missed_text(page)
        self.assertIn("And 1 more.", text)
        self.assertEqual(text.count("Call "), 3)

    def test_dashboard_flags_a_regular(self):
        service = _profile().services[0]
        self.studio.bookings.book(
            PRIYA, "Priya", service, india_today() - timedelta(days=10), 600
        )
        self.studio.bookings.book(
            PRIYA, "Priya", service, india_today() - timedelta(days=5), 600
        )
        self._refuse(PRIYA, "Priya")
        text = _missed_text(self.studio.dashboard_html("today"))
        self.assertIn("Regular, booked 2 times before.", text)

    def test_unanswered_hidden_while_its_handoff_is_open(self):
        self.studio.llm = ScriptedLLM([
            T("hand_over", {"reason": "cannot answer: do you do bridal makeup?"}),
            C("Let me pass you to the team."),
        ])
        self.studio.handle_message(
            PRIYA, "do you do bridal makeup?", "Priya"
        )
        page = self.studio.dashboard_html("today")
        self.assertNotIn("Bad experience", page)
        # The owner resolves the handoff without replying: the
        # question is still unanswered, so it surfaces now.
        self.studio.handoffs.resolve(1)
        page = self.studio.dashboard_html("today")
        self.assertIn("Bad experience", page)
        self.assertIn("Bot did not know the answer.", _missed_text(page))

    def test_missed_copy_has_no_dash(self):
        self._refuse(PRIYA, "Priya")
        self._refuse(ASHA, "Asha", reason="below the lowest price")
        self.studio.missed.record_unanswered(
            MEERA, "Meera", "do you do bridal makeup?", "cannot answer", None
        )
        page = self.studio.dashboard_html("today")
        text = _missed_text(page)
        self.assertNotIn("-", text)
        self.assertNotIn("\u2013", text)
        self.assertNotIn("\u2014", text)

    def test_missed_endpoint(self):
        self._refuse(PRIYA, "Priya")
        httpd = make_server(self.studio, port=0)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/missed"
            ) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read())
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(len(data["missed"]), 1)
        self.assertEqual(data["missed"][0]["phone"], PRIYA)

    def test_missed_resolve_route(self):
        record = self._refuse(PRIYA, "Priya")
        httpd = make_server(self.studio, port=0)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            status = _post(port, "/missed/resolve", {"missed_id": record["id"], "how": "called"})
            self.assertEqual(status, 303)
            status = _post(port, "/missed/resolve", {"missed_id": record["id"], "how": "lost"})
            self.assertEqual(status, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(self.studio.missed.open_newest(), [])

    def test_owner_reply_resolves_the_linked_unanswered(self):
        self.studio.llm = ScriptedLLM([
            T("hand_over", {"reason": "cannot answer: do you do bridal makeup?"}),
            C("Let me pass you to the team."),
        ])
        self.studio.handle_message(
            PRIYA, "do you do bridal makeup?", "Priya"
        )
        httpd = make_server(self.studio, port=0)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            status = _post(
                port, "/handoffs/1/reply", {"text": "Yes, we do bridal makeup."}
            )
            self.assertEqual(status, 303)
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(self.studio.missed.open_newest(), [])
        self.assertEqual(
            self.studio.missed.all_records()[0]["resolved_how"], "answered"
        )


if __name__ == "__main__":
    unittest.main()
