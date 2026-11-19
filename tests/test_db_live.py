"""Live Postgres tests. Skipped unless WHATSAPP_STUDIO_TEST_DATABASE_URL
is set: they need a real database, and they create and drop their own
schema, so never point them at a database with real data.

Run: WHATSAPP_STUDIO_TEST_DATABASE_URL=postgres://user:pass@host/db \
     python3 -m unittest discover -s tests -p "test_db_live.py"
"""
import os
import unittest
from datetime import date, timedelta

try:
    import psycopg
except ImportError:
    psycopg = None

from whatsapp_studio.bookings import india_today
from whatsapp_studio.config import Service
from whatsapp_studio.db import build_stores, check_db
from whatsapp_studio.leads import DuplicateMessageError

URL = os.environ.get("WHATSAPP_STUDIO_TEST_DATABASE_URL")
SCHEMA = "whatsapp_studio_test"
PHONE = "919999900001"
DAY = date(2026, 11, 20)


@unittest.skipUnless(
    URL and psycopg is not None,
    "needs WHATSAPP_STUDIO_TEST_DATABASE_URL and psycopg",
)
class TestPostgresLive(unittest.TestCase):
    def setUp(self):
        conn = psycopg.connect(URL, autocommit=True)
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        conn.close()
        self.stores = build_stores(
            URL, options=f"-c search_path={SCHEMA}"
        )

    def tearDown(self):
        self.stores.pool.close()
        conn = psycopg.connect(URL, autocommit=True)
        conn.execute(f"DROP SCHEMA {SCHEMA} CASCADE")
        conn.close()

    def test_database_answers(self):
        self.assertTrue(check_db(self.stores.pool))

    def test_leads_round_trip_and_duplicate(self):
        lead = self.stores.leads.record_message(
            PHONE, "hi", "greeting", "Priya", wamid="wamid.live.1"
        )
        self.assertEqual(lead["message_count"], 1)
        self.assertTrue(self.stores.leads.has_wamid(PHONE, "wamid.live.1"))
        with self.assertRaises(DuplicateMessageError):
            self.stores.leads.record_message(
                PHONE, "hi again", "greeting", "Priya", wamid="wamid.live.1"
            )
        self.assertEqual(len(self.stores.leads.all_leads()), 1)
        self.assertEqual(
            self.stores.leads.get(PHONE)["messages"][0]["text"], "hi"
        )

    def test_bookings_round_trip_capacity_and_reschedule(self):
        service = Service("Haircut", 499, 30)
        booking = self.stores.bookings.book(
            PHONE, "Priya", service, DAY, 660
        )
        self.assertIsNotNone(booking)
        self.assertEqual(booking["date"], "2026-11-20")
        # The same slot is taken now.
        self.assertIsNone(
            self.stores.bookings.book(
                "919999900002", "Rahul", service, DAY, 660
            )
        )
        self.assertEqual(len(self.stores.bookings.all_bookings()), 1)
        cancelled, moved = self.stores.bookings.reschedule(
            PHONE, "Priya", service, DAY + timedelta(days=1), 720
        )
        self.assertEqual(cancelled["id"], booking["id"])
        self.assertEqual(moved["date"], "2026-11-21")
        self.assertEqual(
            self.stores.bookings.cancel_latest(PHONE)["id"], moved["id"]
        )
        self.assertEqual(self.stores.bookings.all_bookings(), [])

    def test_handoffs_and_blocks(self):
        handoff = self.stores.handoffs.open(
            PHONE, "Priya", "asked for a person", "talk to a person"
        )
        self.assertEqual(handoff["status"], "open")
        self.assertEqual(
            self.stores.handoffs.open_for(PHONE)["id"], handoff["id"]
        )
        self.assertEqual(
            self.stores.handoffs.resolve(handoff["id"])["status"], "resolved"
        )
        block = self.stores.blocks.add(DAY, 600, 660, "lunch")
        self.assertTrue(self.stores.blocks.is_blocked(DAY, 630, 30))
        self.assertFalse(self.stores.blocks.is_blocked(DAY, 700, 30))
        self.assertEqual(
            self.stores.blocks.remove(block["id"])["id"], block["id"]
        )

    def test_missed_round_trip(self):
        record = self.stores.missed.record_refused(
            PHONE, "Priya", "Haircut", DAY, 660, "already taken",
            ["11:30 AM"], "book haircut tomorrow 11am",
        )
        bumped = self.stores.missed.record_refused(
            PHONE, "Priya", "Haircut", DAY, 660, "already taken",
            ["11:30 AM"], "book haircut tomorrow 11am",
        )
        self.assertEqual(bumped["id"], record["id"])
        self.assertEqual(bumped["occurrences"], 2)
        self.stores.missed.record_unanswered(
            PHONE, "Priya", "Do you do keratin?", "cannot answer"
        )
        self.assertEqual(len(self.stores.missed.open_newest()), 2)
        self.stores.missed.resolve_for_booking(PHONE, "Haircut")
        self.assertEqual(len(self.stores.missed.open_newest()), 1)
        self.assertEqual(
            self.stores.missed.open_newest()[0]["kind"], "unanswered"
        )

    def test_outbox_round_trip(self):
        entries = [
            {"to": PHONE, "body": "Hi Priya", "intent": None, "ts": 1000},
            {"to": PHONE, "body": "Your booking is confirmed",
             "intent": "booking", "ts": 1001, "send_status": "sent",
             "wamid": "wamid.out.1"},
        ]
        self.stores.outbox.save(entries)
        loaded = self.stores.outbox.load()
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["body"], "Hi Priya")
        self.assertEqual(loaded[1]["send_status"], "sent")
        self.assertEqual(loaded[1]["wamid"], "wamid.out.1")

    def test_today_counts(self):
        # A booking today shows up in the dashboard's numbers.
        service = Service("Haircut", 499, 30)
        self.stores.bookings.book(
            PHONE, "Priya", service, india_today(), 660
        )
        self.assertEqual(len(self.stores.bookings.bookings_on(india_today())), 1)


if __name__ == "__main__":
    unittest.main()
