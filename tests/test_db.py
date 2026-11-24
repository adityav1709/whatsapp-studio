"""Postgres stores against a fake driver: no database, no network.

psycopg is optional: the JSON path runs without it, so when it is
not installed just enough of it is faked in sys.modules for the
store code to import. The fake pool hands out one scripted cursor:
each execute pops the next scripted outcome, so a test spells out
exactly what the database answers, in order, and asserts the exact
statements the store sent.
"""
import sys
import tempfile
import time
import types
import unittest
from datetime import date
from pathlib import Path
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

from whatsapp_studio.bookings import india_today
from whatsapp_studio.config import Service
from whatsapp_studio.db import (
    JsonOutboxStore,
    PgBlockStore,
    PgBookingStore,
    PgHandoffStore,
    PgLeadStore,
    PgMissedStore,
    PgOutboxStore,
    build_stores,
    check_db,
    connect_pool,
    ensure_schema,
)
from whatsapp_studio.leads import DuplicateMessageError

PHONE = "919999900001"
DAY = date(2026, 11, 20)


class FakeCursor:
    """One scripted cursor. Each execute pops the next outcome, so a
    test controls fetchone, fetchall and rowcount per statement."""

    def __init__(self):
        self.executed = []
        self._outcomes = []
        self.rowcount = 0

    def script(self, *outcomes):
        self._outcomes.extend(outcomes)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        outcome = self._outcomes.pop(0) if self._outcomes else {}
        self._one = outcome.get("one")
        self._all = outcome.get("all", [])
        self.rowcount = outcome.get("rowcount", 0)

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._all)


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self, row_factory=None):
        return self._cursor

    def transaction(self):
        return _NullContext()


class FakePool:
    """A pool that hands out the same scripted cursor every time."""

    def __init__(self, cursor=None):
        self.cursor = cursor or FakeCursor()

    def connection(self):
        return FakeConnection(self.cursor)


class FailingPool:
    """A pool whose connections raise: the database is down."""

    def connection(self):
        raise ConnectionError("database is down")


def _lead_row(**overrides):
    row = {
        "phone": PHONE,
        "name": "Priya",
        "first_seen": 1000,
        "last_seen": 1000,
        "message_count": 1,
        "intents": ["greeting"],
        "messages": [
            {"ts": 1000, "text": "hi", "intent": "greeting", "wamid": None}
        ],
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
        "reason": "asked for a person",
        "trigger": "talk to a person",
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
        "start_min": 600,
        "end_min": 660,
        "reason": "lunch",
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
                "handoff_id": None,
            }
        )
    row.update(overrides)
    return row


def _outbox_row(**overrides):
    row = {
        "recipient": PHONE,
        "body": "Hi Priya",
        "intent": "greeting",
        "ts": 1000,
        "handoff_id": None,
        "send_status": "recorded",
        "wamid": None,
        "send_error": None,
        "truncated": False,
    }
    row.update(overrides)
    return row


class TestPgLeadStore(unittest.TestCase):
    def test_new_lead_with_wamid_claims_first(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": {"wamid": "wamid.1"}},
            {"one": None},
            {"one": _lead_row()},
        )
        lead = PgLeadStore(FakePool(cursor)).record_message(
            PHONE, "hi", "greeting", "Priya", wamid="wamid.1", ts=1000
        )
        self.assertEqual(lead["phone"], PHONE)
        self.assertEqual(lead["message_count"], 1)
        self.assertEqual(lead["messages"][0]["text"], "hi")
        self.assertEqual(lead["intents"], ["greeting"])
        # The wamid claim runs before the lead is even looked up, so
        # a racing redelivery loses the claim first.
        self.assertIn("seen_wamids", cursor.executed[0][0])
        self.assertIn("ON CONFLICT (shop_id, wamid)", cursor.executed[0][0])
        self.assertIn("FOR UPDATE", cursor.executed[1][0])
        self.assertIn("ON CONFLICT (shop_id, phone)", cursor.executed[2][0])

    def test_duplicate_wamid_raises_and_records_nothing(self):
        cursor = FakeCursor()
        cursor.script({"one": None})
        with self.assertRaises(DuplicateMessageError):
            PgLeadStore(FakePool(cursor)).record_message(
                PHONE, "hi again", "greeting", "Priya", wamid="wamid.1"
            )
        self.assertEqual(len(cursor.executed), 1)

    def test_returning_customer_keeps_name_and_appends(self):
        cursor = FakeCursor()
        existing = _lead_row(message_count=2, messages=[
            {"ts": 900, "text": "hello", "intent": "greeting", "wamid": None},
            {"ts": 1000, "text": "hi", "intent": "greeting", "wamid": None},
        ])
        updated = _lead_row(
            message_count=3,
            last_seen=1100,
            messages=existing["messages"] + [
                {"ts": 1100, "text": "book haircut", "intent": None, "wamid": None}
            ],
        )
        cursor.script({"one": existing}, {"one": updated})
        lead = PgLeadStore(FakePool(cursor)).record_message(
            PHONE, "book haircut", None, "Someone Else", ts=1100
        )
        # A name already on file is not overwritten by a new one.
        self.assertEqual(lead["name"], "Priya")
        self.assertEqual(lead["message_count"], 3)
        self.assertEqual(len(lead["messages"]), 3)

    def test_has_wamid(self):
        cursor = FakeCursor()
        cursor.script({"one": {"1": 1}}, {"one": None})
        store = PgLeadStore(FakePool(cursor))
        self.assertTrue(store.has_wamid(PHONE, "wamid.1"))
        self.assertFalse(store.has_wamid(PHONE, "wamid.2"))
        self.assertFalse(store.has_wamid(PHONE, ""))
        self.assertEqual(len(cursor.executed), 2)

    def test_all_leads_and_get(self):
        cursor = FakeCursor()
        cursor.script(
            {"all": [_lead_row(), _lead_row(phone="919999900002", name="Rahul")]},
            {"one": _lead_row()},
            {"one": None},
        )
        store = PgLeadStore(FakePool(cursor))
        self.assertEqual(len(store.all_leads()), 2)
        self.assertEqual(store.get(PHONE)["name"], "Priya")
        self.assertIsNone(store.get("919999900099"))

    def test_ping(self):
        cursor = FakeCursor()
        cursor.script({"one": {"1": 1}})
        self.assertTrue(PgLeadStore(FakePool(cursor)).ping())
        self.assertFalse(PgLeadStore(FailingPool()).ping())


class TestPgBookingStore(unittest.TestCase):
    def _service(self, capacity=1):
        return Service("Haircut", 499, 30, capacity=capacity)

    def test_book_free_slot(self):
        cursor = FakeCursor()
        cursor.script({"all": []}, {"one": _booking_row()})
        booking = PgBookingStore(FakePool(cursor)).book(
            PHONE, "Priya", self._service(), DAY, 660
        )
        self.assertEqual(booking["service"], "Haircut")
        self.assertEqual(booking["date"], "2026-11-20")
        self.assertEqual(booking["price"], 499)
        self.assertEqual(booking["id"], 7)
        # The lock runs before the insert, in the same transaction.
        self.assertIn("FOR UPDATE", cursor.executed[0][0])
        self.assertIn("INSERT INTO bookings", cursor.executed[1][0])

    def test_book_full_slot_returns_none_without_insert(self):
        cursor = FakeCursor()
        cursor.script({"all": [{"id": 1}]})
        booking = PgBookingStore(FakePool(cursor)).book(
            PHONE, "Priya", self._service(), DAY, 660
        )
        self.assertIsNone(booking)
        self.assertEqual(len(cursor.executed), 1)

    def test_book_second_chair_when_capacity_two(self):
        cursor = FakeCursor()
        cursor.script({"all": [{"id": 1}]}, {"one": _booking_row(id=8)})
        booking = PgBookingStore(FakePool(cursor)).book(
            PHONE, "Priya", self._service(capacity=2), DAY, 660
        )
        self.assertIsNotNone(booking)
        self.assertEqual(booking["id"], 8)

    def test_cancel_latest(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": _booking_row()}, {"one": _booking_row()}
        )
        cancelled = PgBookingStore(FakePool(cursor)).cancel_latest(PHONE)
        self.assertEqual(cancelled["id"], 7)
        self.assertIn("DELETE FROM bookings", cursor.executed[1][0])

    def test_cancel_latest_with_nothing(self):
        cursor = FakeCursor()
        cursor.script({"one": None})
        self.assertIsNone(PgBookingStore(FakePool(cursor)).cancel_latest(PHONE))
        self.assertEqual(len(cursor.executed), 1)

    def test_reject_removes_the_booking(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": _booking_row()}, {"one": _booking_row()}
        )
        rejected = PgBookingStore(FakePool(cursor)).reject(PHONE, DAY, 660)
        self.assertEqual(rejected["id"], 7)
        self.assertIn("DELETE FROM bookings", cursor.executed[1][0])

    def test_reject_with_nothing(self):
        cursor = FakeCursor()
        cursor.script({"one": None})
        self.assertIsNone(PgBookingStore(FakePool(cursor)).reject(PHONE, DAY, 660))
        self.assertEqual(len(cursor.executed), 1)

    def test_reschedule_moves_in_one_transaction(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": _booking_row()},
            {"all": []},
            {},
            {"one": _booking_row(id=9, date=date(2026, 11, 21), start_min=720)},
        )
        cancelled, booking = PgBookingStore(FakePool(cursor)).reschedule(
            PHONE, "Priya", self._service(), date(2026, 11, 21), 720
        )
        self.assertEqual(cancelled["id"], 7)
        self.assertEqual(booking["id"], 9)
        self.assertEqual(booking["date"], "2026-11-21")
        statements = [sql for sql, _ in cursor.executed]
        self.assertTrue(any("DELETE FROM bookings" in s for s in statements))
        self.assertTrue(any("INSERT INTO bookings" in s for s in statements))

    def test_reschedule_full_slot_keeps_old_booking(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": _booking_row()},
            {"all": [{"id": 99}]},
        )
        cancelled, booking = PgBookingStore(FakePool(cursor)).reschedule(
            PHONE, "Priya", self._service(), date(2026, 11, 21), 720
        )
        self.assertIsNone(cancelled)
        self.assertIsNone(booking)
        self.assertEqual(len(cursor.executed), 2)

    def test_reschedule_with_nothing_to_move(self):
        cursor = FakeCursor()
        cursor.script({"one": None})
        cancelled, booking = PgBookingStore(FakePool(cursor)).reschedule(
            PHONE, "Priya", self._service(), date(2026, 11, 21), 720
        )
        self.assertIsNone(cancelled)
        self.assertIsNone(booking)


class TestPgHandoffStore(unittest.TestCase):
    def test_open_new_and_return_existing(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": None},
            {"one": _handoff_row()},
            {"one": _handoff_row()},
        )
        store = PgHandoffStore(FakePool(cursor))
        opened = store.open(PHONE, "Priya", "asked for a person", "talk to a person")
        self.assertEqual(opened["status"], "open")
        self.assertEqual(opened["trigger"], "talk to a person")
        existing = store.open(PHONE, "Priya", "asked for a person", "talk to a person")
        self.assertEqual(existing["id"], 3)
        self.assertEqual(len(cursor.executed), 3)

    def test_resolve(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": _handoff_row(status="resolved", resolved_at=1100)},
            {"one": None},
        )
        store = PgHandoffStore(FakePool(cursor))
        resolved = store.resolve(3)
        self.assertEqual(resolved["status"], "resolved")
        self.assertIsNone(store.resolve(99))

    def test_all_handoffs(self):
        cursor = FakeCursor()
        cursor.script({"all": [_handoff_row(), _handoff_row(id=4, status="resolved")]})
        self.assertEqual(len(PgHandoffStore(FakePool(cursor)).all_handoffs()), 2)


class TestPgBlockStore(unittest.TestCase):
    def test_add_remove_and_list(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": _block_row()},
            {"one": _block_row()},
            {"one": None},
            {"all": [_block_row()]},
        )
        store = PgBlockStore(FakePool(cursor))
        added = store.add(DAY, 600, 660, "lunch")
        self.assertEqual(added["date"], "2026-11-20")
        self.assertEqual(store.remove(2)["id"], 2)
        self.assertIsNone(store.remove(99))
        self.assertEqual(len(store.all_blocks()), 1)

    def test_is_blocked_uses_blocks_on(self):
        cursor = FakeCursor()
        cursor.script(
            {"all": [_block_row()]},
            {"all": []},
        )
        store = PgBlockStore(FakePool(cursor))
        self.assertTrue(store.is_blocked(DAY, 630, 30))
        self.assertFalse(store.is_blocked(DAY, 700, 30))


class TestPgMissedStore(unittest.TestCase):
    def test_record_refused_new(self):
        cursor = FakeCursor()
        cursor.script({"one": None}, {"one": _missed_row()})
        record = PgMissedStore(FakePool(cursor)).record_refused(
            PHONE, "Priya", "Haircut", DAY, 660, "already taken",
            ["11:30 AM"], "book haircut tomorrow 11am", now_ts=1000,
        )
        self.assertEqual(record["kind"], "refused")
        self.assertEqual(record["occurrences"], 1)
        self.assertEqual(record["alternatives"], ["11:30 AM"])
        # A refusal carries no question key, like the JSON store.
        self.assertNotIn("question", record)
        self.assertIn("INSERT INTO missed", cursor.executed[1][0])

    def test_record_refused_bumps_open_record(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": _missed_row()},
            {"one": _missed_row(occurrences=2, last_seen_ts=1100)},
        )
        record = PgMissedStore(FakePool(cursor)).record_refused(
            PHONE, "Priya", "Haircut", DAY, 660, "already taken",
            ["11:30 AM"], "book haircut tomorrow 11am again", now_ts=1100,
        )
        self.assertEqual(record["occurrences"], 2)
        self.assertIn("occurrences = occurrences + 1", cursor.executed[1][0])

    def test_demo_phone_never_recorded(self):
        cursor = FakeCursor()
        store = PgMissedStore(FakePool(cursor))
        self.assertIsNone(store.record_refused(
            "919810000009", "Divya", "Haircut", DAY, 660,
            "already taken", [], "book haircut", now_ts=1000,
        ))
        self.assertIsNone(store.record_unanswered(
            "919810000009", "Divya", "Do you do keratin?",
            "cannot answer", now_ts=1000,
        ))
        self.assertEqual(cursor.executed, [])

    def test_record_unanswered_new_and_bump_sets_handoff(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": None},
            {"one": _missed_row(kind="unanswered")},
            {},
            {"one": _missed_row(kind="unanswered")},
            {"one": _missed_row(kind="unanswered", occurrences=2)},
            {"one": _missed_row(kind="unanswered", occurrences=2, handoff_id=4)},
            {},
        )
        store = PgMissedStore(FakePool(cursor))
        record = store.record_unanswered(
            PHONE, "Priya", "Do you do keratin?", "cannot answer", now_ts=1000,
        )
        self.assertEqual(record["kind"], "unanswered")
        self.assertEqual(record["question"], "Do you do keratin?")
        # An unanswered question carries no service key, like JSON.
        self.assertNotIn("service", record)
        bumped = store.record_unanswered(
            PHONE, "Priya", "Do you do keratin?", "cannot answer",
            handoff_id=4, now_ts=1100,
        )
        self.assertEqual(bumped["occurrences"], 2)
        self.assertEqual(bumped["handoff_id"], 4)

    def test_resolve(self):
        cursor = FakeCursor()
        cursor.script(
            {"one": _missed_row(status="resolved", resolved_at=1100, resolved_how="called")},
            {"one": None},
        )
        store = PgMissedStore(FakePool(cursor))
        resolved = store.resolve(5, "called")
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["resolved_how"], "called")
        self.assertIsNone(store.resolve(99, "called"))
        # An unknown reason never touches the database.
        before = len(cursor.executed)
        self.assertIsNone(store.resolve(5, "not a reason"))
        self.assertEqual(len(cursor.executed), before)

    def test_resolve_for_booking_prunes_only_when_changed(self):
        cursor = FakeCursor()
        cursor.script({"rowcount": 0}, {"rowcount": 2}, {})
        store = PgMissedStore(FakePool(cursor))
        store.resolve_for_booking(PHONE, "Haircut")
        self.assertEqual(len(cursor.executed), 1)
        store.resolve_for_booking(PHONE, "Haircut")
        self.assertEqual(len(cursor.executed), 3)
        self.assertIn("DELETE FROM missed", cursor.executed[2][0])

    def test_open_newest_and_open_today(self):
        now = int(time.time())
        today = india_today()
        cursor = FakeCursor()
        cursor.script(
            {"all": [_missed_row(created_ts=now)]},
            {"all": [
                _missed_row(created_ts=now),
                _missed_row(id=6, status="resolved", created_ts=now - 86400),
            ]},
        )
        store = PgMissedStore(FakePool(cursor))
        self.assertEqual(len(store.open_newest()), 1)
        self.assertEqual(store.open_today(today), 1)


class TestPgOutboxStore(unittest.TestCase):
    def test_load_shapes(self):
        cursor = FakeCursor()
        cursor.script({"all": [
            _outbox_row(),
            _outbox_row(recipient="919999900002", handoff_id=4, truncated=True),
        ]})
        entries = PgOutboxStore(FakePool(cursor)).load()
        self.assertEqual(entries[0]["to"], PHONE)
        self.assertEqual(entries[0]["send_status"], "recorded")
        # The handoff and truncated keys appear only when set.
        self.assertNotIn("handoff", entries[0])
        self.assertNotIn("truncated", entries[0])
        self.assertEqual(entries[1]["handoff"], 4)
        self.assertTrue(entries[1]["truncated"])

    def test_save_rewrites_whole_list_capped_at_100(self):
        cursor = FakeCursor()
        entries = [
            {"to": PHONE, "body": f"msg {i}", "intent": None, "ts": 1000 + i}
            for i in range(101)
        ]
        PgOutboxStore(FakePool(cursor)).save(entries)
        self.assertEqual(len(cursor.executed), 101)
        self.assertIn("DELETE FROM outbox", cursor.executed[0][0])
        # The delete is scoped to this shop, and every insert carries
        # the shop id first, so the body sits one param later now.
        self.assertIn("WHERE shop_id = %s", cursor.executed[0][0])
        self.assertEqual(cursor.executed[1][1][2], "msg 1")
        self.assertEqual(cursor.executed[-1][1][2], "msg 100")

    def test_save_empty_only_deletes(self):
        cursor = FakeCursor()
        PgOutboxStore(FakePool(cursor)).save([])
        self.assertEqual(len(cursor.executed), 1)


class TestJsonOutboxStore(unittest.TestCase):
    def test_round_trip_and_tolerant_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outbox.json"
            store = JsonOutboxStore(path)
            self.assertEqual(store.load(), [])
            entries = [{"to": PHONE, "body": "hi", "intent": None, "ts": 1}]
            store.save(entries)
            self.assertTrue(path.exists())
            self.assertEqual(JsonOutboxStore(path).load(), entries)
            path.write_text("not json at all", encoding="utf-8")
            self.assertEqual(JsonOutboxStore(path).load(), [])

    def test_no_path_stays_in_memory(self):
        store = JsonOutboxStore()
        self.assertEqual(store.load(), [])
        store.save([{"to": PHONE, "body": "hi", "intent": None, "ts": 1}])


class TestBuildStores(unittest.TestCase):
    def test_no_url_means_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            stores = build_stores(None, tmp)
            self.assertEqual(stores.leads.backend, "json")
            self.assertEqual(stores.bookings.backend, "json")
            self.assertEqual(stores.handoffs.backend, "json")
            self.assertEqual(stores.blocks.backend, "json")
            self.assertEqual(stores.missed.backend, "json")
            self.assertEqual(stores.outbox.backend, "json")
            self.assertIsNone(stores.pool)

    def test_url_means_postgres(self):
        pool = FakePool()
        with mock.patch(
            "whatsapp_studio.db.connect_pool", return_value=pool
        ) as connect, mock.patch(
            "whatsapp_studio.db.ensure_schema"
        ) as schema, mock.patch(
            "whatsapp_studio.db.migrate_schema"
        ) as migrate:
            stores = build_stores("postgres://fake", "/tmp")
        connect.assert_called_once_with("postgres://fake")
        schema.assert_called_once_with(pool)
        # The migration runs on every Postgres boot, stamping old
        # rows with the single-shop id.
        migrate.assert_called_once_with(pool, "default")
        self.assertEqual(stores.leads.backend, "postgres")
        self.assertEqual(stores.bookings.backend, "postgres")
        self.assertEqual(stores.outbox.backend, "postgres")
        self.assertIs(stores.pool, pool)


class TestEnsureSchemaAndCheckDb(unittest.TestCase):
    def test_ensure_schema_creates_every_table(self):
        cursor = FakeCursor()
        ensure_schema(FakePool(cursor))
        sql = " ".join(sql for sql, _ in cursor.executed)
        for table in (
            "leads", "seen_wamids", "bookings", "handoffs",
            "blocks", "missed", "outbox",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)

    def test_check_db(self):
        cursor = FakeCursor()
        cursor.script({"one": {"1": 1}})
        self.assertTrue(check_db(FakePool(cursor)))
        self.assertFalse(check_db(FailingPool()))


class TestConnectPool(unittest.TestCase):
    def _fake_pool_module(self, fail_times):
        module = types.ModuleType("psycopg_pool")
        state = {"fail_times": fail_times, "calls": []}

        class FakeConnectionPool:
            def __init__(self, url, **kwargs):
                state["calls"].append((url, kwargs))
                if state["fail_times"] > 0:
                    state["fail_times"] -= 1
                    raise ConnectionError("waking up")
                self.url = url

        module.ConnectionPool = FakeConnectionPool
        return module, state

    def test_retries_then_succeeds(self):
        module, state = self._fake_pool_module(fail_times=2)
        with mock.patch.dict(sys.modules, {"psycopg_pool": module}):
            with mock.patch("time.sleep") as sleep:
                pool = connect_pool("postgres://fake")
        self.assertEqual(len(state["calls"]), 3)
        self.assertEqual(sleep.call_count, 2)
        url, kwargs = state["calls"][0]
        self.assertEqual(url, "postgres://fake")
        self.assertEqual(kwargs["min_size"], 1)
        self.assertEqual(kwargs["max_size"], 4)
        self.assertEqual(kwargs["connect_timeout"], 5)
        self.assertTrue(kwargs["open"])
        self.assertEqual(pool.url, "postgres://fake")

    def test_gives_up_after_three_tries(self):
        module, state = self._fake_pool_module(fail_times=3)
        with mock.patch.dict(sys.modules, {"psycopg_pool": module}):
            with mock.patch("time.sleep") as sleep:
                with self.assertRaises(RuntimeError):
                    connect_pool("postgres://fake")
        self.assertEqual(len(state["calls"]), 3)
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
