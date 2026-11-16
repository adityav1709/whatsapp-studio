"""Block a time, and the negotiation switch.

The owner blocks a time on the Bookings tab and the bot treats it
like a time already taken: it never offers it and never books over
it, and it never says the word blocked to a customer. A block over an
existing booking is refused on the dashboard. The negotiation switch
is a real control: off means fixed prices only, and the code enforces
it even if the model sends an agreed price.

Time-dependent checks pin explicit times or use tomorrow, never the
current hour, so the suite passes whatever hour it runs in.
"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from test_llm import C, FixedLLM, ScriptedLLM, T
from whatsapp_studio.app import Studio, make_server
from whatsapp_studio.blocks import BlockStore
from whatsapp_studio.bookings import BookingStore, IST, india_today
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.llm import build_system_prompt


def make_profile():
    return BusinessProfile(
        name="Glow & Grace Salon",
        category="salon",
        city="Gurgaon",
        phone="919999999999",
        hours={"daily": "10:00 AM - 8:00 PM"},
        services=[
            Service("Haircut", 499, 30, floor_price=400),
            Service("Facial", 1499, 60),
        ],
        address="Sector 50, Gurgaon",
    )


def make_studio(tmp, llm=None):
    return Studio(
        make_profile(),
        LeadStore(Path(tmp) / "leads.json"),
        bookings=BookingStore(),
        handoffs=HandoffStore(),
        blocks=BlockStore(),
        llm=llm if llm is not None else FixedLLM("Thanks for messaging!"),
    )


def _hand_over_turns():
    return [
        T("hand_over", {"reason": "asked for a person"}),
        C("I've passed this to the team. They will reply here within 30 minutes."),
    ]


def _tool_result(llm):
    """The tool result the Studio fed back on the last model call."""
    messages = [
        m for m in llm.calls[-1]["messages"] if m["role"] == "tool"
    ]
    return json.loads(messages[0]["content"])


class TestBlockStore(unittest.TestCase):
    def test_add_list_overlap_and_remove(self):
        store = BlockStore()
        day = date(2026, 11, 17)
        block = store.add(day, 14 * 60, 16 * 60, "Lunch")
        self.assertEqual(block["date"], "2026-11-17")
        self.assertEqual(block["reason"], "Lunch")
        self.assertEqual(len(store.blocks_on(day)), 1)
        self.assertEqual(len(store.blocks_on(date(2026, 11, 18))), 0)
        # A 30 minute haircut at 3pm runs into the block.
        self.assertTrue(store.is_blocked(day, 15 * 60, 30))
        # A haircut at 1:30pm ends exactly when the block starts.
        self.assertFalse(store.is_blocked(day, 13 * 60 + 30, 30))
        # A haircut at 4pm starts exactly when the block ends.
        self.assertFalse(store.is_blocked(day, 16 * 60, 30))
        self.assertEqual(len(store.overlaps(day, 15 * 60, 16 * 60)), 1)
        self.assertEqual(len(store.overlaps(day, 10 * 60, 11 * 60)), 0)
        removed = store.remove(block["id"])
        self.assertEqual(removed["id"], block["id"])
        self.assertEqual(store.all_blocks(), [])
        self.assertIsNone(store.remove(block["id"]))

    def test_persists_to_disk_and_loads_tolerantly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blocks.json"
            store = BlockStore(path)
            store.add(date(2026, 11, 17), 14 * 60, 16 * 60, "Lunch")
            reloaded = BlockStore(path)
            self.assertEqual(len(reloaded.all_blocks()), 1)
            self.assertEqual(reloaded.all_blocks()[0]["reason"], "Lunch")
            Path(tmp, "blocks.json").write_text("not json", encoding="utf-8")
            self.assertEqual(BlockStore(path).all_blocks(), [])


class TestBotRefusesBlockedTimes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = make_studio(self.tmp.name)
        self.tomorrow = india_today() + timedelta(days=1)

    def tearDown(self):
        self.tmp.cleanup()

    def _block_tomorrow_lunch(self):
        return self.studio.blocks.add(
            self.tomorrow, 14 * 60, 16 * 60, "Lunch"
        )

    def test_create_booking_at_a_blocked_time_is_refused(self):
        self._block_tomorrow_lunch()
        self.studio.llm = ScriptedLLM(
            [
                T(
                    "create_booking",
                    {"service": "Haircut", "day": "tomorrow", "time": "3pm"},
                ),
                C("That time is not available, but 4:00 PM is free."),
            ]
        )
        entry = self.studio.handle_message(
            "919810000001", "book haircut tomorrow 3pm", "Rahul"
        )
        result = _tool_result(self.studio.llm)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not free")
        self.assertIn("4:00 PM", result["alternatives"])
        self.assertNotIn("3:00 PM", result["alternatives"])
        self.assertEqual(len(self.studio.bookings.all_bookings()), 0)
        # The word blocked never reaches the customer, not in the tool
        # result and not in the reply the bot sent.
        self.assertNotIn("blocked", json.dumps(result).lower())
        self.assertNotIn("blocked", entry["body"].lower())

    def test_check_availability_never_offers_a_blocked_slot(self):
        self._block_tomorrow_lunch()
        self.studio.llm = ScriptedLLM(
            [
                T(
                    "check_availability",
                    {"service": "Haircut", "day": "tomorrow", "time": "3pm"},
                ),
                C("3:00 PM is not available, 4:00 PM is free."),
            ]
        )
        self.studio.handle_message(
            "919810000001", "is 3pm free tomorrow for a haircut", "Rahul"
        )
        result = _tool_result(self.studio.llm)
        self.assertTrue(result["ok"])
        self.assertNotIn("3:00 PM", result["slots"])
        self.assertIn("4:00 PM", result["slots"])

    def test_reschedule_to_a_blocked_time_is_refused(self):
        self.studio.llm = ScriptedLLM(
            [
                T(
                    "create_booking",
                    {"service": "Haircut", "day": "tomorrow", "time": "11am"},
                ),
                C("Done! Your Haircut is booked for tomorrow at 11:00 AM."),
            ]
        )
        self.studio.handle_message(
            "919810000001", "book haircut tomorrow 11am", "Rahul"
        )
        self.assertEqual(len(self.studio.bookings.all_bookings()), 1)
        self._block_tomorrow_lunch()
        self.studio.llm = ScriptedLLM(
            [
                T(
                    "reschedule_booking",
                    {"day": "tomorrow", "time": "3pm"},
                ),
                C("3:00 PM is not available, 4:00 PM is free."),
            ]
        )
        self.studio.handle_message(
            "919810000001", "move my haircut to tomorrow 3pm", "Rahul"
        )
        result = _tool_result(self.studio.llm)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not free")
        # The old booking survives: a failed move never destroys one.
        self.assertEqual(len(self.studio.bookings.all_bookings()), 1)
        self.assertEqual(
            self.studio.bookings.all_bookings()[0]["start_min"], 11 * 60
        )

    def test_unblock_then_the_same_booking_works(self):
        block = self._block_tomorrow_lunch()
        self.studio.blocks.remove(block["id"])
        self.studio.llm = ScriptedLLM(
            [
                T(
                    "create_booking",
                    {"service": "Haircut", "day": "tomorrow", "time": "3pm"},
                ),
                C("Done! Your Haircut is booked for tomorrow at 3:00 PM."),
            ]
        )
        self.studio.handle_message(
            "919810000001", "book haircut tomorrow 3pm", "Rahul"
        )
        self.assertEqual(len(self.studio.bookings.all_bookings()), 1)

    def test_glance_mentions_a_block_today(self):
        # Pin the clock so the block is always in the future today.
        pinned = datetime(2026, 11, 16, 11, 0, tzinfo=IST)
        with mock.patch(
            "whatsapp_studio.app.india_now", return_value=pinned
        ), mock.patch(
            "whatsapp_studio.app.india_today",
            return_value=pinned.date(),
        ):
            self.studio.blocks.add(pinned.date(), 14 * 60, 16 * 60, "Lunch")
            html = self.studio.dashboard_html()
        self.assertIn("2 hours blocked for Lunch.", html)


class TestTodayTabIsClean(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = make_studio(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_today_has_no_booking_or_customer_detail(self):
        self.studio.llm = ScriptedLLM(_hand_over_turns())
        self.studio.handle_message(
            "919810000001", "can I talk to a person please", "Rahul"
        )
        self.studio.handle_message("919810000001", "my booking is wrong", "Rahul")
        today = self.studio.dashboard_html("today")
        self.assertNotIn("People waiting for you", today)
        self.assertNotIn("Handoffs", today)
        self.assertNotIn("919810000001", today)
        self.assertNotIn("can I talk to a person please", today)
        # The counts are still there: they say which tab to tap.
        self.assertIn("waiting for you", today)
        self.assertIn("bookings today", today)
        self.assertIn("people messaged today", today)
        # The detail lives on the Customers tab.
        customers = self.studio.dashboard_html("customers")
        self.assertIn("People waiting for you", customers)
        self.assertIn("Handoffs", customers)
        self.assertIn("919810000001", customers)
        self.assertIn("can I talk to a person please", customers)
        self.assertIn("Resolve", customers)


class TestNegotiationSwitch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = make_studio(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_switch_is_on_by_default_and_shown_on_every_tab(self):
        self.assertTrue(self.studio.negotiation_on)
        for tab in ("today", "bookings", "customers", "replies", "conversation"):
            html = self.studio.dashboard_html(tab)
            self.assertIn("Let the bot negotiate prices", html)
            self.assertIn("Negotiation: ON", html)

    def test_prompt_changes_with_the_switch(self):
        on = build_system_prompt(make_profile(), negotiation_on=True)
        self.assertIn("Negotiation: a customer may ask for a lower price", on)
        off = build_system_prompt(make_profile(), negotiation_on=False)
        self.assertIn("Prices are fixed", off)
        self.assertNotIn("Negotiation: a customer may ask", off)
        self.assertNotIn("never below the lowest price listed", off)

    def test_model_gets_the_fixed_price_prompt_when_off(self):
        self.studio.disable_negotiation()
        self.studio.llm = ScriptedLLM([C("The Haircut is Rs 499.")])
        self.studio.handle_message("919810000001", "haircut ka price?", "Rahul")
        system = self.studio.llm.calls[0]["messages"][0]["content"]
        self.assertIn("Prices are fixed", system)
        self.assertNotIn("Negotiation: a customer may ask", system)

    def test_agreed_price_below_list_is_refused_when_off(self):
        self.studio.disable_negotiation()
        self.studio.llm = ScriptedLLM(
            [
                T(
                    "create_booking",
                    {
                        "service": "Haircut",
                        "day": "tomorrow",
                        "time": "11am",
                        "price": 450,
                    },
                ),
                C("The Haircut is Rs 499 and the price is fixed."),
            ]
        )
        self.studio.handle_message(
            "919810000001", "haircut 450 me kar do", "Rahul"
        )
        result = _tool_result(self.studio.llm)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "fixed price")
        self.assertEqual(result["price"], 499)
        self.assertEqual(len(self.studio.bookings.all_bookings()), 0)

    def test_same_agreed_price_books_when_on(self):
        self.studio.llm = ScriptedLLM(
            [
                T(
                    "create_booking",
                    {
                        "service": "Haircut",
                        "day": "tomorrow",
                        "time": "11am",
                        "price": 450,
                    },
                ),
                C("Done! Your Haircut is booked for tomorrow at 11:00 AM for Rs 450."),
            ]
        )
        self.studio.handle_message(
            "919810000001", "haircut 450 me kar do", "Rahul"
        )
        self.assertEqual(len(self.studio.bookings.all_bookings()), 1)
        self.assertEqual(self.studio.bookings.all_bookings()[0]["price"], 450)

    def test_switch_can_be_turned_back_on(self):
        self.studio.disable_negotiation()
        self.studio.enable_negotiation()
        self.assertTrue(self.studio.negotiation_on)
        self.assertIn("Negotiation: ON", self.studio.dashboard_html())


class TestBlockHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = make_studio(self.tmp.name)
        self.httpd = make_server(self.studio, port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.tomorrow = india_today() + timedelta(days=1)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _url(self, path):
        return "http://127.0.0.1:{}{}".format(self.port, path)

    def _get(self, path):
        with urllib.request.urlopen(self._url(path)) as resp:
            return resp.status, resp.read().decode()

    def _post(self, path, fields):
        data = urllib.parse.urlencode(fields).encode("utf-8")
        req = urllib.request.Request(self._url(path), data=data, method="POST")
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()

    def test_block_form_is_on_the_bookings_tab(self):
        status, body = self._get("/?tab=bookings")
        self.assertEqual(status, 200)
        self.assertIn("Block a time", body)
        self.assertIn("Blocked times", body)
        self.assertIn("type='date' name='day'", body)
        self.assertIn("type='time' name='start'", body)
        self.assertIn("type='time' name='end'", body)
        self.assertIn("name='reason'", body)
        self.assertIn("No blocked times.", body)

    def test_add_block_then_unblock_it(self):
        status, body = self._post(
            "/blocks/add",
            {
                "day": self.tomorrow.isoformat(),
                "start": "14:00",
                "end": "16:00",
                "reason": "Lunch",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("Blocked", body)
        self.assertIn("Lunch", body)
        self.assertEqual(len(self.studio.blocks.all_blocks()), 1)
        block_id = self.studio.blocks.all_blocks()[0]["id"]
        self.assertIn("Open this time again", body)
        status, body = self._post("/blocks/remove", {"block_id": str(block_id)})
        self.assertEqual(status, 200)
        self.assertIn("Opened that time again.", body)
        self.assertEqual(self.studio.blocks.all_blocks(), [])

    def test_block_over_an_existing_booking_is_refused(self):
        self.studio.bookings.book(
            "919810000002", "Neha", make_profile().services[0],
            self.tomorrow, 15 * 60,
        )
        status, body = self._post(
            "/blocks/add",
            {
                "day": self.tomorrow.isoformat(),
                "start": "14:00",
                "end": "16:00",
                "reason": "Lunch",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("already has a booking", body)
        self.assertIn("Neha", body)
        self.assertIn("3:00 PM", body)
        self.assertIn("Cancel it or move it first", body)
        self.assertEqual(self.studio.blocks.all_blocks(), [])
        # The booking survives: a refused block never deletes one.
        self.assertEqual(len(self.studio.bookings.all_bookings()), 1)

    def test_block_in_the_past_is_refused(self):
        yesterday = india_today() - timedelta(days=1)
        status, body = self._post(
            "/blocks/add",
            {
                "day": yesterday.isoformat(),
                "start": "14:00",
                "end": "16:00",
                "reason": "Lunch",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("already gone", body)
        self.assertEqual(self.studio.blocks.all_blocks(), [])

    def test_negotiation_toggle_over_http(self):
        status, body = self._post("/negotiation/disable", {})
        self.assertEqual(status, 200)
        self.assertIn("Negotiation: OFF", body)
        self.assertFalse(self.studio.negotiation_on)
        status, body = self._post("/negotiation/enable", {})
        self.assertEqual(status, 200)
        self.assertIn("Negotiation: ON", body)
        self.assertTrue(self.studio.negotiation_on)

    def test_export_includes_blocks(self):
        self.studio.blocks.add(self.tomorrow, 14 * 60, 16 * 60, "Lunch")
        status, body = self._get("/export")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["blocks"]), 1)
        self.assertEqual(payload["blocks"][0]["reason"], "Lunch")
