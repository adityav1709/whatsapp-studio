"""Postgres shops: every store scoped to one shop_id.

The dev-loop check for the shop_id change. There is no live
Postgres on this VM, so these tests run the stores against the
same scripted FakeCursor pattern as test_db.py and assert the
exact statements sent: every query carries shop_id, two stores
over one cursor differ only in that param, and the migration
issues its statements in order. The live two-shop isolation
check lives in test_db_live.py, where a real database exists.
The FakePool tests prove the SQL text; the live tests prove the
database agrees. Neither alone is enough.
"""
import sys
import types
import unittest
from datetime import date
from unittest import mock

try:
    import psycopg  # noqa: F401
except ImportError:
    _psycopg = types.ModuleType("psycopg")
    _rows = types.ModuleType("psycopg.rows")

    class dict_row:
        """The row factory the stores pass; the fake cursor ignores it."""

    _rows.dict_row = dict_row
    _psycopg.rows = _rows
    sys.modules.setdefault("psycopg", _psycopg)
    sys.modules.setdefault("psycopg.rows", _rows)

from whatsapp_studio.config import Service
from whatsapp_studio.db import (
    DEFAULT_SHOP_ID,
    SCHEMA,
    PgBlockStore,
    PgBookingStore,
    PgHandoffStore,
    PgLeadStore,
    PgMissedStore,
    PgOutboxStore,
    build_stores,
    migrate_schema,
)

try:
    # Under discover the tests directory is on the path.
    from test_db import FakeCursor, FakePool
except ImportError:  # pragma: no cover - single-module runs
    # Under `python -m unittest tests.test_postgres_shop` it is not.
    from tests.test_db import FakeCursor, FakePool

PHONE = "919999900001"
DAY = date(2026, 11, 20)
SHOP = "snips"
OTHER_SHOP = "glow"


def _lead_row(**overrides):
    row = {
        "phone": PHONE,
        "name": "Priya",
        "first_seen": 1000,
        "last_seen": 1000,
        "message_count": 1,
        "intents": ["greeting"],
        "messages": [{"ts": 1000, "text": "hi", "intent": "greeting", "wamid": None}],
    }
    row.update(overrides)
    return row


def _booking_row(**overrides):
    row = {
        "id": 7,
        "phone": PHONE,
        "name": "Priya",
        "service": "Haircut",
        "price": 499,
        "date": DAY,
        "start_min": 660,
        "duration_min": 30,
        "created_ts": 1000,
    }
    row.update(overrides)
    return row


def _handoff_row(**overrides):
    row = {
        "id": 3,
        "phone": PHONE,
        "name": "Priya",
        "reason": "needs a person",
        "trigger": "talk to someone",
        "opened_at": 1000,
        "resolved_at": None,
        "status": "open",
    }
    row.update(overrides)
    return row


def _block_row(**overrides):
    row = {
        "id": 2,
        "date": DAY,
        "start_min": 660,
        "end_min": 720,
        "reason": "closed",
        "created_ts": 1000,
    }
    row.update(overrides)
    return row


def _missed_row(kind="refused", **overrides):
    row = {
        "id": 5,
        "kind": kind,
        "phone": PHONE,
        "name": "Priya",
        "created_ts": 1000,
        "last_seen_ts": 1000,
        "occurrences": 1,
        "status": "open",
        "resolved_at": None,
        "resolved_how": None,
        "service": "Haircut",
        "date": DAY,
        "start_min": 660,
        "reason": "already taken",
        "alternatives": ["11:30 AM"],
        "trigger": "book haircut tomorrow 11am",
        "question": "",
        "handoff_id": None,
    }
    if kind == "unanswered":
        row.update(
            {
                "service": None,
                "date": None,
                "start_min": None,
                "alternatives": [],
                "trigger": "",
                "question": "Do you do keratin?",
            }
        )
    row.update(overrides)
    return row


def _outbox_row(**overrides):
    row = {
        "recipient": PHONE,
        "body": "hi",
        "intent": None,
        "ts": 1000,
        "handoff_id": None,
        "send_status": "recorded",
        "wamid": None,
        "send_error": None,
        "truncated": False,
    }
    row.update(overrides)
    return row


def _service():
    return Service("Haircut", 499, 30, capacity=1)


def _assert_scoped(test, cursor):
    """Every statement sent names shop_id, and carries this shop's
    id in its params. One forgotten predicate is the leak, so the
    check runs over all of them, not a sample."""
    test.assertTrue(cursor.executed, "no statement was sent at all")
    for sql, params in cursor.executed:
        test.assertIn("shop_id", sql, sql)
        test.assertIn(SHOP, params, sql)


class TestShopIdConstruction(unittest.TestCase):
    def test_none_means_default_shop(self):
        store = PgLeadStore(FakePool(), None)
        self.assertEqual(store._shop_id, DEFAULT_SHOP_ID)
        self.assertEqual(DEFAULT_SHOP_ID, "default")

    def test_blank_shop_id_refuses_at_construction(self):
        for bad in ("", "   "):
            with self.assertRaises(ValueError):
                PgLeadStore(FakePool(), bad)

    def test_build_stores_passes_the_shop_through(self):
        pool = FakePool()
        with mock.patch(
            "whatsapp_studio.db.connect_pool", return_value=pool
        ), mock.patch("whatsapp_studio.db.ensure_schema"), mock.patch(
            "whatsapp_studio.db.migrate_schema"
        ):
            stores = build_stores("postgres://fake", "/tmp", shop_id="snips")
        self.assertEqual(stores.leads._shop_id, "snips")
        self.assertEqual(stores.bookings._shop_id, "snips")
        self.assertEqual(stores.handoffs._shop_id, "snips")
        self.assertEqual(stores.blocks._shop_id, "snips")
        self.assertEqual(stores.missed._shop_id, "snips")
        self.assertEqual(stores.outbox._shop_id, "snips")

    def test_schema_carries_shop_id_on_every_table(self):
        ddl = " ".join(SCHEMA)
        for table in (
            "leads", "seen_wamids", "bookings", "handoffs",
            "blocks", "missed", "outbox",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", ddl)
        # Composite keys: one lead row and one dedup record per shop.
        self.assertIn("PRIMARY KEY (shop_id, phone)", ddl)
        self.assertIn("PRIMARY KEY (shop_id, wamid)", ddl)
        # Every index is shop-prefixed; the old unscoped names are gone.
        for old in (
            "bookings_date_start_idx", "bookings_phone_date_idx",
            "handoffs_phone_status_idx", "handoffs_status_opened_idx",
            "blocks_date_start_idx", "missed_phone_status_idx",
            "missed_status_created_idx", "outbox_recipient_ts_idx",
        ):
            self.assertNotIn(old, ddl)


class TestMigration(unittest.TestCase):
    def test_statements_in_order(self):
        cursor = FakeCursor()
        migrate_schema(FakePool(cursor), "snips")
        sqls = [sql for sql, _ in cursor.executed]
        # One transaction worth of statements: add the column, stamp
        # the old rows, make it NOT NULL, rebuild the two keys, swap
        # the indexes. The order matters: the key cannot be rebuilt
        # while shop_id is still NULL.
        add_at = next(i for i, s in enumerate(sqls) if "ADD COLUMN IF NOT EXISTS shop_id" in s)
        backfill_at = next(i for i, s in enumerate(sqls) if s.startswith("UPDATE") and "WHERE shop_id IS NULL" in s)
        not_null_at = next(i for i, s in enumerate(sqls) if "SET NOT NULL" in s)
        pk_at = next(i for i, s in enumerate(sqls) if "ADD PRIMARY KEY (shop_id, phone)" in s)
        drop_old_at = next(i for i, s in enumerate(sqls) if "DROP INDEX IF EXISTS bookings_date_start_idx" in s)
        self.assertLess(add_at, backfill_at)
        self.assertLess(backfill_at, not_null_at)
        self.assertLess(not_null_at, pk_at)
        self.assertLess(pk_at, drop_old_at)
        # Every table is covered, and the backfill stamps this shop.
        for table in (
            "leads", "seen_wamids", "bookings", "handoffs",
            "blocks", "missed", "outbox",
        ):
            self.assertTrue(
                any(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS shop_id" in s for s in sqls),
                table,
            )
            self.assertTrue(
                any(s.startswith(f"UPDATE {table} SET shop_id") for s in sqls),
                table,
            )
        backfills = [p for s, p in cursor.executed if s.startswith("UPDATE") and "WHERE shop_id IS NULL" in s]
        self.assertEqual(len(backfills), 7)
        self.assertTrue(all(p == ("snips",) for p in backfills))
        # No default is left behind: a forgotten shop_id must fail.
        self.assertTrue(any("DROP DEFAULT" in s for s in sqls))
        # Both keys are rebuilt, and the new indexes land.
        self.assertTrue(any("ADD PRIMARY KEY (shop_id, wamid)" in s for s in sqls))
        self.assertTrue(any("bookings_shop_date_start_idx" in s for s in sqls))

    def test_blank_backfill_id_refuses(self):
        with self.assertRaises(ValueError):
            migrate_schema(FakePool(), "  ")


class TestLeadStoreScoping(unittest.TestCase):
    def test_record_message_scopes_all_three_statements(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": {"wamid": "wamid.1"}},
            {"one": None},
            {"one": _lead_row()},
        )
        PgLeadStore(FakePool(cursor), SHOP).record_message(
            PHONE, "hi", "greeting", "Priya", wamid="wamid.1", ts=1000
        )
        self.assertEqual(len(cursor.executed), 3)
        _assert_scoped(self, cursor)
        self.assertIn("ON CONFLICT (shop_id, wamid)", cursor.executed[0][0])
        self.assertIn("ON CONFLICT (shop_id, phone)", cursor.executed[2][0])

    def test_set_name_has_wamid_all_leads_get(self):
        cursor = FakeCursor()
        cursor.script({"one": _lead_row()}, {"one": {"1": 1}}, {"all": []}, {"one": None})
        store = PgLeadStore(FakePool(cursor), SHOP)
        store.set_name(PHONE, "Priya")
        store.has_wamid(PHONE, "wamid.1")
        store.all_leads()
        store.get(PHONE)
        self.assertEqual(len(cursor.executed), 4)
        _assert_scoped(self, cursor)

    def test_two_shops_differ_only_in_shop_id(self):
        cursor = FakeCursor()
        cursor.script({"all": []}, {"all": []})
        PgLeadStore(FakePool(cursor), SHOP).all_leads()
        PgLeadStore(FakePool(cursor), OTHER_SHOP).all_leads()
        first, second = cursor.executed
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], ("snips",))
        self.assertEqual(second[1], ("glow",))


class TestBookingStoreScoping(unittest.TestCase):
    def test_reads_scoped(self):
        cursor = FakeCursor()
        cursor.script({"all": []}, {"all": []})
        store = PgBookingStore(FakePool(cursor), SHOP)
        store.bookings_on(DAY)
        store.all_bookings()
        _assert_scoped(self, cursor)

    def test_book_scopes_the_capacity_lock_and_the_insert(self):
        cursor = FakeCursor()
        cursor.script({"all": []}, {"one": _booking_row()})
        store = PgBookingStore(FakePool(cursor), SHOP)
        booked = store.book(PHONE, "Priya", _service(), DAY, 660)
        self.assertEqual(booked["phone"], PHONE)
        self.assertEqual(len(cursor.executed), 2)
        _assert_scoped(self, cursor)
        self.assertIn("FOR UPDATE", cursor.executed[0][0])

    def test_cancel_reject_rename_reschedule_scoped(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": _booking_row()},
            {"one": _booking_row()},
            {"one": _booking_row()},
            {"one": _booking_row()},
            {"one": _booking_row()},
            {"one": _booking_row()},
            {"one": _booking_row()},
            {"all": []},
            {"one": _booking_row()},
            {"one": _booking_row()},
        )
        store = PgBookingStore(FakePool(cursor), SHOP)
        store.cancel_latest(PHONE)
        store.reject(PHONE, DAY, 660)
        store.update_latest_name(PHONE, "Priya")
        store.reschedule(PHONE, "Priya", _service(), DAY, 720)
        _assert_scoped(self, cursor)
        # Id-based writes carry the shop predicate too: a wrong
        # shop id must turn them into a no-op, not a cross-shop hit.
        for sql, _ in cursor.executed:
            if sql.startswith(("DELETE", "UPDATE")):
                self.assertIn("shop_id = %s", sql, sql)


class TestHandoffBlockScoping(unittest.TestCase):
    def test_handoff_scoped(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": None},
            {"one": _handoff_row()},
            {"one": _handoff_row()},
            {"one": _handoff_row()},
            {"all": []},
        )
        store = PgHandoffStore(FakePool(cursor), SHOP)
        store.open(PHONE, "Priya", "needs a person", "talk to someone", opened_at=1000)
        store.open_for(PHONE)
        store.resolve(3)
        store.all_handoffs()
        self.assertEqual(len(cursor.executed), 5)
        _assert_scoped(self, cursor)

    def test_block_scoped(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": _block_row()},
            {"one": _block_row()},
            {"all": []},
            {"all": []},
        )
        store = PgBlockStore(FakePool(cursor), SHOP)
        store.add(DAY, 660, 720, "closed")
        store.remove(2)
        store.all_blocks()
        store.blocks_on(DAY)
        self.assertEqual(len(cursor.executed), 4)
        _assert_scoped(self, cursor)


class TestMissedStoreScoping(unittest.TestCase):
    def test_record_refused_scopes_match_insert_and_prune(self):
        cursor = FakeCursor()
        cursor.script({"one": None}, {"one": _missed_row()})
        store = PgMissedStore(FakePool(cursor), SHOP)
        store.record_refused(
            PHONE, "Priya", "Haircut", DAY, 660,
            "already taken", ["11:30 AM"], "book haircut tomorrow 11am",
            now_ts=1000,
        )
        self.assertEqual(len(cursor.executed), 3)
        _assert_scoped(self, cursor)
        # The shop is part of the match, so the FOR UPDATE lock
        # cannot lock another shop's open record for the same phone.
        self.assertIn("shop_id = %s", cursor.executed[0][0])

    def test_record_unanswered_with_handoff_scoped(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": None},
            {"one": _missed_row(kind="unanswered")},
            {"one": _missed_row(kind="unanswered", handoff_id=3)},
        )
        store = PgMissedStore(FakePool(cursor), SHOP)
        store.record_unanswered(
            PHONE, "Priya", "Do you do keratin?", "no answer",
            handoff_id=3, now_ts=1000,
        )
        self.assertEqual(len(cursor.executed), 4)
        _assert_scoped(self, cursor)

    def test_resolve_for_handoff_scoped(self):
        cursor = FakeCursor()
        cursor.script({"rowcount": 1}, {})
        store = PgMissedStore(FakePool(cursor), SHOP)
        store.resolve_for_handoff(3, "answered")
        self.assertEqual(len(cursor.executed), 2)
        _assert_scoped(self, cursor)

    def test_resolves_and_reads_scoped(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": _missed_row()},
            {},
            {"rowcount": 1},
            {},
            {"all": []},
            {"all": []},
        )
        # resolve (UPDATE + prune), resolve_for_booking (UPDATE +
        # prune), all_records, open_newest.
        store = PgMissedStore(FakePool(cursor), SHOP)
        store.resolve(5, "called")
        store.resolve_for_booking(PHONE, "Haircut")
        store.all_records()
        store.open_newest()
        self.assertEqual(len(cursor.executed), 6)
        _assert_scoped(self, cursor)
        for sql, _ in cursor.executed:
            if sql.startswith("DELETE"):
                self.assertIn("shop_id = %s", sql, sql)


class TestOutboxStoreScoping(unittest.TestCase):
    def test_load_and_save_scoped(self):
        cursor = FakeCursor()
        cursor.script({"all": [_outbox_row()]})
        store = PgOutboxStore(FakePool(cursor), SHOP)
        store.load()
        store.save([{"to": PHONE, "body": "hi", "intent": None, "ts": 1000}])
        self.assertEqual(len(cursor.executed), 3)
        _assert_scoped(self, cursor)
        # The delete that used to wipe every shop's queued replies
        # is now scoped to this shop.
        self.assertEqual(
            cursor.executed[1][0], "DELETE FROM outbox WHERE shop_id = %s"
        )
        self.assertEqual(cursor.executed[1][1], ("snips",))


class TestFullFlowAudit(unittest.TestCase):
    def test_no_statement_in_a_full_flow_misses_shop_id(self):
        """record, book, handoff, missed, outbox save: the audit
        the briefs asked for. Partial scoping is worse than none,
        so the whole flow is checked, not a sample of it."""
        cursor = FakeCursor()
        cursor.script(
            {"one": {"wamid": "wamid.1"}},
            {"one": None},
            {"one": _lead_row()},
            {"all": []},
            {"one": _booking_row()},
            {"one": None},
            {"one": _handoff_row()},
            {"one": None},
            {"one": _missed_row()},
        )
        pool = FakePool(cursor)
        PgLeadStore(pool, SHOP).record_message(
            PHONE, "hi", "greeting", "Priya", wamid="wamid.1", ts=1000
        )
        PgBookingStore(pool, SHOP).book(PHONE, "Priya", _service(), DAY, 660)
        PgHandoffStore(pool, SHOP).open(
            PHONE, "Priya", "needs a person", "talk to someone", opened_at=1000
        )
        PgMissedStore(pool, SHOP).record_refused(
            PHONE, "Priya", "Haircut", DAY, 660,
            "already taken", ["11:30 AM"], "book haircut tomorrow 11am",
            now_ts=1000,
        )
        PgOutboxStore(pool, SHOP).save(
            [{"to": PHONE, "body": "hi", "intent": None, "ts": 1000}]
        )
        _assert_scoped(self, cursor)


if __name__ == "__main__":
    unittest.main()
