"""WhatsApp Studio on Postgres: the same stores, a database underneath.

The JSON stores stay the default. When DATABASE_URL is set, these
Postgres stores take over, with the exact same public methods and
dicts carrying the same fields, so app.py never knows which backend
answered. Bookings also carry their database id, which the JSON
store never had. One table per store, plus seen_wamids for webhook
dedup and the outbox. Epoch seconds stay epoch seconds and dates
stay dates, so no time logic anywhere else changes.

Every table carries a shop_id column, and every store instance
belongs to one shop: many shops share one database and never see
each other's rows. Leads are keyed by (shop_id, phone) and dedup
records by (shop_id, wamid), so the same customer at two shops is
two separate leads. The first boot runs migrate_schema, which adds
the column to databases created before it existed and stamps their
rows with the shop that was live on them.

psycopg is imported lazily, inside connect_pool only. Importing this
module, running the JSON path, and running the existing tests all work
with psycopg not installed.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from .bookings import (
    IST,
    _first_win_from,
    _moments_from,
    _regulars_from,
    india_now,
    india_today,
)
from .leads import DuplicateMessageError
from .missed import RESOLVED_HOW

# Demo phones are sample data, never real customers.
_DEMO_PREFIX = "9198100000"

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS leads (
        shop_id TEXT NOT NULL,
        phone TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        first_seen BIGINT NOT NULL,
        last_seen BIGINT NOT NULL,
        message_count INTEGER NOT NULL DEFAULT 0,
        intents TEXT[] NOT NULL DEFAULT '{}',
        messages JSONB NOT NULL DEFAULT '[]',
        PRIMARY KEY (shop_id, phone)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seen_wamids (
        shop_id TEXT NOT NULL,
        wamid TEXT NOT NULL,
        phone TEXT NOT NULL,
        received_ts BIGINT NOT NULL,
        PRIMARY KEY (shop_id, wamid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bookings (
        id BIGSERIAL PRIMARY KEY,
        shop_id TEXT NOT NULL,
        phone TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        service TEXT NOT NULL,
        price INTEGER,
        date DATE NOT NULL,
        start_min INTEGER NOT NULL,
        duration_min INTEGER NOT NULL,
        created_ts BIGINT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS bookings_shop_date_start_idx "
    "ON bookings (shop_id, date, start_min)",
    "CREATE INDEX IF NOT EXISTS bookings_shop_phone_date_idx "
    "ON bookings (shop_id, phone, date, start_min)",
    """
    CREATE TABLE IF NOT EXISTS handoffs (
        id BIGSERIAL PRIMARY KEY,
        shop_id TEXT NOT NULL,
        phone TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL,
        trigger TEXT NOT NULL DEFAULT '',
        opened_at BIGINT NOT NULL,
        resolved_at BIGINT,
        status TEXT NOT NULL CHECK (status IN ('open', 'resolved'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS handoffs_shop_phone_status_idx "
    "ON handoffs (shop_id, phone, status)",
    "CREATE INDEX IF NOT EXISTS handoffs_shop_status_opened_idx "
    "ON handoffs (shop_id, status, opened_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS blocks (
        id BIGSERIAL PRIMARY KEY,
        shop_id TEXT NOT NULL,
        date DATE NOT NULL,
        start_min INTEGER NOT NULL,
        end_min INTEGER NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        created_ts BIGINT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS blocks_shop_date_start_idx "
    "ON blocks (shop_id, date, start_min)",
    """
    CREATE TABLE IF NOT EXISTS missed (
        id BIGSERIAL PRIMARY KEY,
        shop_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('refused', 'unanswered')),
        phone TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        created_ts BIGINT NOT NULL,
        last_seen_ts BIGINT NOT NULL,
        occurrences INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
        resolved_at BIGINT,
        resolved_how TEXT CHECK (
            resolved_how IN ('booked', 'called', 'dismissed', 'answered')),
        service TEXT,
        date DATE,
        start_min INTEGER,
        reason TEXT,
        alternatives TEXT[] NOT NULL DEFAULT '{}',
        trigger TEXT,
        question TEXT,
        handoff_id BIGINT
    )
    """,
    "CREATE INDEX IF NOT EXISTS missed_shop_phone_status_idx "
    "ON missed (shop_id, phone, status)",
    "CREATE INDEX IF NOT EXISTS missed_shop_status_created_idx "
    "ON missed (shop_id, status, created_ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS outbox (
        id BIGSERIAL PRIMARY KEY,
        shop_id TEXT NOT NULL,
        recipient TEXT NOT NULL,
        body TEXT NOT NULL,
        intent TEXT,
        ts BIGINT NOT NULL,
        handoff_id BIGINT,
        send_status TEXT NOT NULL DEFAULT 'recorded',
        wamid TEXT,
        send_error TEXT,
        truncated BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    "CREATE INDEX IF NOT EXISTS outbox_shop_recipient_ts_idx "
    "ON outbox (shop_id, recipient, ts)",
]

# Tables the import refuses to touch unless they are empty.
TABLES = ("leads", "seen_wamids", "bookings", "handoffs", "blocks",
          "missed", "outbox")

# The shop id a single-shop install writes under. A registry names
# its own shops; the default shop in a registry must carry the same
# id the rows were stamped with (see migrate_schema).
DEFAULT_SHOP_ID = "default"

# Indexes the scoped ones replace. The migration drops these after
# the shop-prefixed indexes land, so a scoped query never plans
# against an index that still spans every shop.
_OLD_INDEXES = (
    "bookings_date_start_idx",
    "bookings_phone_date_idx",
    "handoffs_phone_status_idx",
    "handoffs_status_opened_idx",
    "blocks_date_start_idx",
    "missed_phone_status_idx",
    "missed_status_created_idx",
    "outbox_recipient_ts_idx",
)


def _atomic_write(path, text):
    """Write a file so a crash mid-write cannot corrupt it: a uniquely
    named temp file in the same directory, then a rename. Same shape
    as the one each JSON store keeps for itself."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _psycopg():
    """The driver, imported only when a database is actually used.
    Returns (psycopg, dict_row)."""
    import psycopg
    from psycopg.rows import dict_row

    return psycopg, dict_row


def connect_pool(database_url, tries=3, **kwargs):
    """One small pool for the whole process. Retries briefly for a
    database that is still waking up, then raises: starting without a
    database would serve a shop that cannot see its bookings."""
    from psycopg_pool import ConnectionPool

    # psycopg_pool takes connection parameters in kwargs, not as
    # pool arguments: passing connect_timeout at the top level is
    # rejected by every version the requirements pin.
    connection_kwargs = {"connect_timeout": 5}
    connection_kwargs.update(kwargs.pop("kwargs", {}) or {})
    connection_kwargs.update(kwargs)
    last = None
    for attempt in range(tries):
        try:
            return ConnectionPool(
                database_url,
                min_size=1,
                max_size=4,
                kwargs=connection_kwargs,
                open=True,
            )
        except Exception as exc:  # noqa: BLE001 - retried, then raised
            last = exc
            if attempt < tries - 1:
                time.sleep(5)
    raise RuntimeError(
        "Could not reach the database. Check DATABASE_URL and restart."
    ) from last


def ensure_schema(pool):
    """Create every table and index, idempotently. A restart is a
    no-op. Never changes a column: the day a column needs adding,
    this stops being enough and a real migration step arrives."""
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                for statement in SCHEMA:
                    cur.execute(statement)


def migrate_schema(pool, default_shop_id):
    """The first real migration: add shop_id to every table.

    Runs once per boot, in one transaction, after ensure_schema. A
    database created by the new SCHEMA already has the column, the
    composite keys and the scoped indexes, so every statement below
    is a no-op there. A database created by the old DDL gets the
    column added, its existing rows stamped with default_shop_id,
    the column made NOT NULL, the two single-column primary keys
    rebuilt as (shop_id, ...), the scoped indexes created and the
    old unscoped ones dropped.

    default_shop_id is the id the rows already in the database
    belong to: the registry's default shop when a registry exists,
    DEFAULT_SHOP_ID when there is none. It is passed in, never
    guessed here: stamping rows with the wrong id would make them
    invisible to the shop that made them, with no error. A blank id
    refuses outright. A failed run rolls the whole transaction back,
    so a boot that crashes halfway is safe to retry.
    """
    if not (default_shop_id or "").strip():
        raise ValueError("migrate_schema needs a shop id to stamp old rows with.")
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                for table in TABLES:
                    cur.execute(
                        f"ALTER TABLE {table}"
                        " ADD COLUMN IF NOT EXISTS shop_id TEXT"
                    )
                for table in TABLES:
                    cur.execute(
                        f"UPDATE {table} SET shop_id = %s"
                        " WHERE shop_id IS NULL",
                        (default_shop_id,),
                    )
                for table in TABLES:
                    cur.execute(
                        f"ALTER TABLE {table}"
                        " ALTER COLUMN shop_id SET NOT NULL"
                    )
                    cur.execute(
                        f"ALTER TABLE {table}"
                        " ALTER COLUMN shop_id DROP DEFAULT"
                    )
                for table, key in (
                    ("leads", "phone"),
                    ("seen_wamids", "wamid"),
                ):
                    cur.execute(
                        f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS"
                        f" {table}_pkey"
                    )
                    cur.execute(
                        f"ALTER TABLE {table} ADD PRIMARY KEY"
                        f" (shop_id, {key})"
                    )
                for statement in SCHEMA:
                    if "CREATE INDEX" in statement:
                        cur.execute(statement)
                for index in _OLD_INDEXES:
                    cur.execute(f"DROP INDEX IF EXISTS {index}")


def check_db(pool):
    """True when the database answers, False otherwise. Used by
    /health so a dead database fails loud instead of serving an
    empty shop."""
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:  # noqa: BLE001 - any failure means down
        return False


def _date_iso(value):
    """A DATE column back to the ISO string the JSON store returns."""
    if isinstance(value, date):
        return value.isoformat()
    return value


def lead_from_row(row):
    return {
        "phone": row["phone"],
        "name": row["name"] or "",
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "message_count": row["message_count"],
        "intents": list(row["intents"] or []),
        "messages": list(row["messages"] or []),
    }


def booking_from_row(row):
    return {
        "id": row["id"],
        "phone": row["phone"],
        "name": row["name"] or "",
        "service": row["service"],
        "price": row["price"],
        "date": _date_iso(row["date"]),
        "start_min": row["start_min"],
        "duration_min": row["duration_min"],
        "created_ts": row["created_ts"],
    }


def handoff_from_row(row):
    return {
        "id": row["id"],
        "phone": row["phone"],
        "name": row["name"] or "",
        "reason": row["reason"],
        "trigger": row["trigger"] or "",
        "opened_at": row["opened_at"],
        "resolved_at": row["resolved_at"],
        "status": row["status"],
    }


def block_from_row(row):
    return {
        "id": row["id"],
        "date": _date_iso(row["date"]),
        "start_min": row["start_min"],
        "end_min": row["end_min"],
        "reason": row["reason"] or "",
        "created_ts": row["created_ts"],
    }


def missed_from_row(row):
    """The same key set the JSON store keeps: the common fields, plus
    the refused-only fields for a refusal and the unanswered-only
    fields for an unanswered question."""
    record = {
        "id": row["id"],
        "kind": row["kind"],
        "phone": row["phone"],
        "name": row["name"] or "",
        "created_ts": row["created_ts"],
        "last_seen_ts": row["last_seen_ts"],
        "occurrences": row["occurrences"],
        "status": row["status"],
        "resolved_at": row["resolved_at"],
        "resolved_how": row["resolved_how"],
    }
    if row["kind"] == "refused":
        record.update(
            {
                "service": row["service"] or "",
                "date": _date_iso(row["date"]) if row["date"] else None,
                "start_min": row["start_min"],
                "reason": row["reason"],
                "alternatives": list(row["alternatives"] or []),
                "trigger": row["trigger"] or "",
            }
        )
    else:
        record.update(
            {
                "question": row["question"] or "",
                "reason": row["reason"],
                "handoff_id": row["handoff_id"],
            }
        )
    return record


def outbox_from_row(row):
    entry = {
        "to": row["recipient"],
        "body": row["body"],
        "intent": row["intent"],
        "ts": row["ts"],
        "send_status": row["send_status"] or "recorded",
        "wamid": row["wamid"],
        "send_error": row["send_error"],
    }
    if row["handoff_id"] is not None:
        entry["handoff"] = row["handoff_id"]
    if row["truncated"]:
        entry["truncated"] = True
    return entry


class _PgStore:
    """One pool, shared by every store. A store method checks a
    connection out, does its work in one transaction, and returns it.
    Never one shared connection: a psycopg connection is not safe for
    two threads at once.

    Every store instance belongs to one shop, set at construction and
    never changed after. The shop id rides in every statement, so
    forgetting a filter is a construction-time impossibility rather
    than a per-call discipline. A blank id refuses here, at
    construction, never at the first query."""

    backend = "postgres"

    def __init__(self, pool, shop_id=None):
        self._pool = pool
        shop_id = shop_id if shop_id is not None else DEFAULT_SHOP_ID
        if not shop_id.strip():
            raise ValueError("A Postgres store needs a shop id.")
        self._shop_id = shop_id

    def ping(self):
        return check_db(self._pool)

    def _cursor(self, conn):
        _, dict_row = _psycopg()
        return conn.cursor(row_factory=dict_row)


class PgLeadStore(_PgStore):
    """Leads in Postgres. Same methods and same dicts as LeadStore.

    The wamid is the dedup record, not the message list: a message id
    already in seen_wamids raises DuplicateMessageError, in the same
    transaction that records the message, so two threads can never
    both answer one delivery. The message list stays a display
    history, capped at 50 per phone as today."""

    def record_message(self, phone, text, intent, sender_name="", wamid=None, ts=None):
        now = ts if ts is not None else int(time.time())
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    if wamid:
                        cur.execute(
                            "INSERT INTO seen_wamids (shop_id, wamid, phone, received_ts)"
                            " VALUES (%s, %s, %s, %s)"
                            " ON CONFLICT (shop_id, wamid) DO NOTHING RETURNING wamid",
                            (self._shop_id, wamid, phone, now),
                        )
                        if cur.fetchone() is None:
                            raise DuplicateMessageError(wamid)
                    cur.execute(
                        "SELECT * FROM leads WHERE shop_id = %s AND phone = %s FOR UPDATE",
                        (self._shop_id, phone),
                    )
                    row = cur.fetchone()
                    if row is None:
                        lead = {
                            "phone": phone,
                            "name": sender_name or "",
                            "first_seen": now,
                            "last_seen": now,
                            "message_count": 0,
                            "intents": [],
                            "messages": [],
                        }
                    else:
                        lead = lead_from_row(row)
                    lead["last_seen"] = now
                    lead["message_count"] += 1
                    if sender_name and not lead["name"]:
                        lead["name"] = sender_name
                    if intent and intent not in lead["intents"]:
                        lead["intents"].append(intent)
                    lead["messages"].append(
                        {"ts": now, "text": text, "intent": intent, "wamid": wamid}
                    )
                    lead["messages"] = lead["messages"][-50:]
                    cur.execute(
                        "INSERT INTO leads"
                        " (shop_id, phone, name, first_seen, last_seen,"
                        " message_count, intents, messages)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)"
                        " ON CONFLICT (shop_id, phone) DO UPDATE SET"
                        " name = EXCLUDED.name,"
                        " last_seen = EXCLUDED.last_seen,"
                        " message_count = EXCLUDED.message_count,"
                        " intents = EXCLUDED.intents,"
                        " messages = EXCLUDED.messages"
                        " RETURNING *",
                        (
                            self._shop_id,
                            lead["phone"],
                            lead["name"],
                            lead["first_seen"],
                            lead["last_seen"],
                            lead["message_count"],
                            lead["intents"],
                            json.dumps(lead["messages"]),
                        ),
                    )
                    return lead_from_row(cur.fetchone())

    def set_name(self, phone, name):
        if not name:
            return None
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    cur.execute(
                        "UPDATE leads SET name = %s WHERE shop_id = %s AND phone = %s"
                        " RETURNING *",
                        (name, self._shop_id, phone),
                    )
                    row = cur.fetchone()
        return lead_from_row(row) if row else None

    def has_wamid(self, phone, wamid):
        if not wamid:
            return False
        with self._pool.connection() as conn:
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT 1 FROM seen_wamids WHERE shop_id = %s AND wamid = %s",
                    (self._shop_id, wamid),
                )
                return cur.fetchone() is not None

    def all_leads(self):
        with self._pool.connection() as conn:
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT * FROM leads WHERE shop_id = %s ORDER BY last_seen DESC",
                    (self._shop_id,),
                )
                return [lead_from_row(row) for row in cur.fetchall()]

    def get(self, phone):
        with self._pool.connection() as conn:
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT * FROM leads WHERE shop_id = %s AND phone = %s",
                    (self._shop_id, phone),
                )
                row = cur.fetchone()
        return lead_from_row(row) if row else None


class PgBookingStore(_PgStore):
    """Bookings in Postgres. Same methods and same dicts as
    BookingStore. The pure logic (regulars, first win, moments) runs
    in Python over rows shaped exactly like the JSON dicts, so the two
    backends cannot disagree. Booking itself is atomic: the overlapping
    rows are locked, counted against capacity, and only then inserted,
    so two webhooks can never take the same slot."""

    def bookings_on(self, day):
        with self._pool.connection() as conn:
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT * FROM bookings WHERE shop_id = %s AND date = %s"
                    " ORDER BY start_min",
                    (self._shop_id, day),
                )
                return [booking_from_row(row) for row in cur.fetchall()]

    def all_bookings(self):
        with self._pool.connection() as conn:
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT * FROM bookings WHERE shop_id = %s"
                    " ORDER BY date, start_min",
                    (self._shop_id,),
                )
                return [booking_from_row(row) for row in cur.fetchall()]

    def regulars(self, today=None, quiet_days=30):
        if today is None:
            today = india_today()
        return _regulars_from(self.all_bookings(), today, quiet_days)

    def first_win(self, today=None):
        if today is None:
            today = india_today()
        return _first_win_from(self.all_bookings(), today)

    def moments(self, hours_for_weekday, now=None):
        now = now or india_now()
        return _moments_from(self.all_bookings(), hours_for_weekday, now)

    def overlapping(self, day, start_min, duration_min):
        end = start_min + duration_min
        return [
            b for b in self.bookings_on(day)
            if start_min < b["start_min"] + b["duration_min"]
            and b["start_min"] < end
        ]

    def is_free(self, day, start_min, duration_min, capacity=1):
        return len(self.overlapping(day, start_min, duration_min)) < capacity

    def free_slots(self, day, service, hours_text):
        from .bookings import is_closed, parse_hours_range

        if is_closed(hours_text):
            return []
        span = parse_hours_range(hours_text)
        if span is None:
            return []
        open_min, close_min = span
        slots = []
        start = open_min
        while start + service.duration_min <= close_min:
            if self.is_free(day, start, service.duration_min, service.capacity):
                slots.append(start)
            start += service.duration_min
        return slots

    def book(self, phone, name, service, day, start_min, price=None):
        """Confirm a booking if the slot is free; returns it, else
        None. The lock and the count run in one transaction, so a slot
        that looks free to two threads at once is only booked once."""
        end = start_min + service.duration_min
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    cur.execute(
                        "SELECT id FROM bookings WHERE shop_id = %s AND date = %s"
                        " AND start_min < %s"
                        " AND start_min + duration_min > %s"
                        " FOR UPDATE",
                        (self._shop_id, day, end, start_min),
                    )
                    if len(cur.fetchall()) >= service.capacity:
                        return None
                    cur.execute(
                        "INSERT INTO bookings"
                        " (shop_id, phone, name, service, price, date,"
                        " start_min, duration_min, created_ts)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        " RETURNING *",
                        (
                            self._shop_id,
                            phone,
                            name or "",
                            service.name,
                            service.price if price is None else price,
                            day,
                            start_min,
                            service.duration_min,
                            int(time.time()),
                        ),
                    )
                    return booking_from_row(cur.fetchone())

    def _latest_upcoming(self, cur, phone):
        cur.execute(
            "SELECT * FROM bookings WHERE shop_id = %s AND phone = %s AND date >= %s"
            " ORDER BY date DESC, start_min DESC LIMIT 1 FOR UPDATE",
            (self._shop_id, phone, india_today()),
        )
        row = cur.fetchone()
        return booking_from_row(row) if row else None

    def cancel_latest(self, phone):
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    latest = self._latest_upcoming(cur, phone)
                    if latest is None:
                        return None
                    cur.execute(
                        "DELETE FROM bookings WHERE id = %s AND shop_id = %s RETURNING *",
                        (latest["id"], self._shop_id),
                    )
                    return booking_from_row(cur.fetchone())

    def reject(self, phone, day, start_min):
        """Remove one booking the owner rejected, by phone, date and
        start time; returns it, or None when there is no such booking.
        Same shape as cancel_latest, matched on the booking itself
        instead of the latest one."""
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    cur.execute(
                        "SELECT * FROM bookings WHERE shop_id = %s AND phone = %s"
                        " AND date = %s AND start_min = %s"
                        " ORDER BY id LIMIT 1 FOR UPDATE",
                        (self._shop_id, phone, day, start_min),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    cur.execute(
                        "DELETE FROM bookings WHERE id = %s AND shop_id = %s RETURNING *",
                        (row["id"], self._shop_id),
                    )
                    return booking_from_row(cur.fetchone())

    def update_latest_name(self, phone, name):
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    latest = self._latest_upcoming(cur, phone)
                    if latest is None:
                        return None
                    cur.execute(
                        "UPDATE bookings SET name = %s WHERE id = %s AND shop_id = %s"
                        " RETURNING *",
                        (name, latest["id"], self._shop_id),
                    )
                    return booking_from_row(cur.fetchone())

    def reschedule(self, phone, name, service, day, start_min, price=None):
        """Move the latest upcoming booking to a new slot, in one
        transaction. Returns (cancelled, booking). When the new slot
        is not free, or there is no booking to move, both are None
        and the old booking stays exactly where it was."""
        end = start_min + service.duration_min
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    latest = self._latest_upcoming(cur, phone)
                    if latest is None:
                        return None, None
                    cur.execute(
                        "SELECT id FROM bookings WHERE shop_id = %s AND date = %s"
                        " AND start_min < %s"
                        " AND start_min + duration_min > %s"
                        " FOR UPDATE",
                        (self._shop_id, day, end, start_min),
                    )
                    others = [
                        row for row in cur.fetchall()
                        if row["id"] != latest["id"]
                    ]
                    if len(others) >= service.capacity:
                        return None, None
                    cur.execute(
                        "DELETE FROM bookings WHERE id = %s AND shop_id = %s",
                        (latest["id"], self._shop_id),
                    )
                    cur.execute(
                        "INSERT INTO bookings"
                        " (shop_id, phone, name, service, price, date,"
                        " start_min, duration_min, created_ts)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        " RETURNING *",
                        (
                            self._shop_id,
                            phone,
                            name or "",
                            service.name,
                            service.price if price is None else price,
                            day,
                            start_min,
                            service.duration_min,
                            int(time.time()),
                        ),
                    )
                    return latest, booking_from_row(cur.fetchone())


class PgHandoffStore(_PgStore):
    """Handoffs in Postgres. Same methods and same dicts as
    HandoffStore."""

    def open(self, phone, name, reason, trigger_message, opened_at=None):
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    existing = self._open_for(cur, phone)
                    if existing is not None:
                        return existing
                    cur.execute(
                        "INSERT INTO handoffs"
                        " (shop_id, phone, name, reason, trigger, opened_at,"
                        " resolved_at, status)"
                        " VALUES (%s, %s, %s, %s, %s, %s, NULL, 'open')"
                        " RETURNING *",
                        (
                            self._shop_id,
                            phone,
                            name or "",
                            reason,
                            trigger_message,
                            opened_at if opened_at is not None else int(time.time()),
                        ),
                    )
                    return handoff_from_row(cur.fetchone())

    def _open_for(self, cur, phone):
        cur.execute(
            "SELECT * FROM handoffs WHERE shop_id = %s AND phone = %s AND status = 'open'"
            " LIMIT 1",
            (self._shop_id, phone),
        )
        row = cur.fetchone()
        return handoff_from_row(row) if row else None

    def open_for(self, phone):
        with self._pool.connection() as conn:
            with self._cursor(conn) as cur:
                return self._open_for(cur, phone)

    def resolve(self, handoff_id):
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    cur.execute(
                        "UPDATE handoffs SET status = 'resolved',"
                        " resolved_at = %s"
                        " WHERE id = %s AND shop_id = %s AND status = 'open'"
                        " RETURNING *",
                        (int(time.time()), handoff_id, self._shop_id),
                    )
                    row = cur.fetchone()
        return handoff_from_row(row) if row else None

    def all_handoffs(self):
        with self._pool.connection() as conn:
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT * FROM handoffs WHERE shop_id = %s"
                    " ORDER BY status <> 'open', opened_at DESC",
                    (self._shop_id,),
                )
                return [handoff_from_row(row) for row in cur.fetchall()]


class PgBlockStore(_PgStore):
    """Blocks in Postgres. Same methods and same dicts as BlockStore."""

    def add(self, day, start_min, end_min, reason=""):
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    cur.execute(
                        "INSERT INTO blocks"
                        " (shop_id, date, start_min, end_min, reason, created_ts)"
                        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                        (
                            self._shop_id,
                            day,
                            start_min,
                            end_min,
                            reason or "",
                            int(time.time()),
                        ),
                    )
                    return block_from_row(cur.fetchone())

    def remove(self, block_id):
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    cur.execute(
                        "DELETE FROM blocks WHERE id = %s AND shop_id = %s RETURNING *",
                        (block_id, self._shop_id),
                    )
                    row = cur.fetchone()
        return block_from_row(row) if row else None

    def all_blocks(self):
        with self._pool.connection() as conn:
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT * FROM blocks WHERE shop_id = %s ORDER BY date, start_min",
                    (self._shop_id,),
                )
                return [block_from_row(row) for row in cur.fetchall()]

    def blocks_on(self, day):
        key = day.isoformat() if isinstance(day, date) else day
        with self._pool.connection() as conn:
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT * FROM blocks WHERE shop_id = %s AND date = %s"
                    " ORDER BY date, start_min",
                    (self._shop_id, key),
                )
                return [block_from_row(row) for row in cur.fetchall()]

    def overlaps(self, day, start_min, end_min):
        return [
            b for b in self.blocks_on(day)
            if start_min < b["end_min"] and b["start_min"] < end_min
        ]

    def is_blocked(self, day, start_min, duration_min):
        return bool(
            self.overlaps(day, start_min, start_min + duration_min)
        )


class PgMissedStore(_PgStore):
    """Missed customers in Postgres. Same methods and same dicts as
    MissedStore. A refusal the bot fixed in the same turn never lands
    here; a later booking closes the open refused records for that
    service as booked."""

    _RETENTION_DAYS = 30

    def _prune(self, cur):
        """Drop resolved records older than the retention window.
        Open records are never dropped: dropping one would hide a
        missed customer."""
        cutoff = int(time.time()) - self._RETENTION_DAYS * 86400
        cur.execute(
            "DELETE FROM missed"
            " WHERE shop_id = %s AND status = 'resolved'"
            " AND (resolved_at IS NULL OR resolved_at < %s)",
            (self._shop_id, cutoff),
        )

    def _bump_or_insert(self, cur, record, match):
        # The shop is part of the match itself, not bolted on after:
        # without it the FOR UPDATE lock would lock another shop's
        # open record for the same phone and service.
        match = {"shop_id": self._shop_id, **match}
        clauses = []
        values = []
        for column, value in match.items():
            if value is None:
                clauses.append(f"{column} IS NULL")
            else:
                clauses.append(f"{column} = %s")
                values.append(value)
        where = " AND ".join(clauses)
        cur.execute(
            f"SELECT * FROM missed WHERE status = 'open' AND {where}"
            " FOR UPDATE",
            values,
        )
        row = cur.fetchone()
        if row is not None:
            existing = missed_from_row(row)
            cur.execute(
                "UPDATE missed SET occurrences = occurrences + 1,"
                " last_seen_ts = %s, trigger = %s, alternatives = %s"
                " WHERE id = %s AND shop_id = %s RETURNING *",
                (
                    record["last_seen_ts"],
                    record.get("trigger", ""),
                    record.get("alternatives", []),
                    existing["id"],
                    self._shop_id,
                ),
            )
            return missed_from_row(cur.fetchone())
        fields = (
            "shop_id", "kind", "phone", "name", "created_ts", "last_seen_ts",
            "occurrences", "status", "resolved_at", "resolved_how",
            "service", "date", "start_min", "reason", "alternatives",
            "trigger", "question", "handoff_id",
        )
        record = {"shop_id": self._shop_id, **record}
        cur.execute(
            "INSERT INTO missed"
            " (shop_id, kind, phone, name, created_ts, last_seen_ts,"
            " occurrences, status, resolved_at, resolved_how,"
            " service, date, start_min, reason, alternatives,"
            " trigger, question, handoff_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            tuple(record.get(f) for f in fields),
        )
        return missed_from_row(cur.fetchone())

    def record_refused(self, phone, name, service, day, start_min, reason, alternatives, trigger, now_ts=None, sample=False):
        if (phone or "").startswith(_DEMO_PREFIX) and not sample:
            return None
        now_ts = now_ts if now_ts is not None else int(time.time())
        record = {
            "kind": "refused",
            "phone": phone,
            "name": name or "",
            "created_ts": now_ts,
            "last_seen_ts": now_ts,
            "occurrences": 1,
            "status": "open",
            "resolved_at": None,
            "resolved_how": None,
            "service": service or "",
            "date": day.isoformat() if day is not None else None,
            "start_min": start_min,
            "reason": reason,
            "alternatives": list(alternatives or []),
            "trigger": (trigger or "")[:280],
            "question": "",
            "handoff_id": None,
        }
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    out = self._bump_or_insert(
                        cur,
                        record,
                        {
                            "kind": "refused",
                            "phone": phone,
                            "service": service or "",
                            "date": record["date"],
                            "start_min": start_min,
                            "reason": reason,
                        },
                    )
                    self._prune(cur)
                    return out

    def record_unanswered(self, phone, name, question, reason, handoff_id=None, now_ts=None, sample=False):
        if (phone or "").startswith(_DEMO_PREFIX) and not sample:
            return None
        now_ts = now_ts if now_ts is not None else int(time.time())
        record = {
            "kind": "unanswered",
            "phone": phone,
            "name": name or "",
            "created_ts": now_ts,
            "last_seen_ts": now_ts,
            "occurrences": 1,
            "status": "open",
            "resolved_at": None,
            "resolved_how": None,
            "service": "",
            "date": None,
            "start_min": None,
            "reason": reason,
            "alternatives": [],
            "trigger": "",
            "question": (question or "")[:280],
            "handoff_id": handoff_id,
        }
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    out = self._bump_or_insert(
                        cur,
                        record,
                        {
                            "kind": "unanswered",
                            "phone": phone,
                            "question": record["question"],
                        },
                    )
                    if handoff_id is not None and out["handoff_id"] is None:
                        cur.execute(
                            "UPDATE missed SET handoff_id = %s WHERE id = %s AND shop_id = %s"
                            " RETURNING *",
                            (handoff_id, out["id"], self._shop_id),
                        )
                        out = missed_from_row(cur.fetchone())
                    self._prune(cur)
                    return out

    def resolve(self, record_id, how):
        if how not in RESOLVED_HOW:
            return None
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    cur.execute(
                        "UPDATE missed SET status = 'resolved',"
                        " resolved_at = %s, resolved_how = %s"
                        " WHERE id = %s AND shop_id = %s AND status = 'open'"
                        " RETURNING *",
                        (int(time.time()), how, record_id, self._shop_id),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        self._prune(cur)
        return missed_from_row(row) if row else None

    def resolve_for_booking(self, phone, service):
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    cur.execute(
                        "UPDATE missed SET status = 'resolved',"
                        " resolved_at = %s, resolved_how = 'booked'"
                        " WHERE shop_id = %s AND status = 'open' AND kind = 'refused'"
                        " AND phone = %s AND service = %s",
                        (int(time.time()), self._shop_id, phone, service),
                    )
                    if cur.rowcount:
                        self._prune(cur)

    def resolve_for_handoff(self, handoff_id, how):
        if how not in RESOLVED_HOW or handoff_id is None:
            return
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    cur.execute(
                        "UPDATE missed SET status = 'resolved',"
                        " resolved_at = %s, resolved_how = %s"
                        " WHERE shop_id = %s AND status = 'open' AND kind = 'unanswered'"
                        " AND handoff_id = %s",
                        (int(time.time()), how, self._shop_id, handoff_id),
                    )
                    if cur.rowcount:
                        self._prune(cur)

    def all_records(self):
        with self._pool.connection() as conn:
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT * FROM missed WHERE shop_id = %s ORDER BY created_ts DESC",
                    (self._shop_id,),
                )
                return [missed_from_row(row) for row in cur.fetchall()]

    def open_newest(self, limit=20):
        with self._pool.connection() as conn:
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT * FROM missed WHERE shop_id = %s AND status = 'open'"
                    " ORDER BY created_ts DESC LIMIT %s",
                    (self._shop_id, limit),
                )
                return [missed_from_row(row) for row in cur.fetchall()]

    def open_today(self, today=None):
        from datetime import datetime

        if today is None:
            today = india_today()
        count = 0
        for record in self.all_records():
            if record["status"] != "open":
                continue
            if (
                datetime.fromtimestamp(record["created_ts"], tz=IST).date()
                == today
            ):
                count += 1
        return count


class JsonOutboxStore:
    """The outbox as a JSON file, exactly as app.py kept it: the last
    100 entries, tolerant load, atomic write. In memory when no path
    is given."""

    backend = "json"

    def __init__(self, path=None):
        self.path = Path(path) if path else None

    def load(self):
        if self.path is None or not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(data, list):
            return []
        return data[-100:]

    def save(self, entries):
        if self.path is None:
            return
        entries = list(entries)[-100:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.path, json.dumps(entries, indent=2))


class PgOutboxStore(_PgStore):
    """The outbox in Postgres. Same list of dicts back, same 100
    entry cap. The whole list is written at once, in one transaction,
    the way the JSON file is rewritten on every reply."""

    def load(self):
        with self._pool.connection() as conn:
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT * FROM outbox WHERE shop_id = %s ORDER BY ts, id",
                    (self._shop_id,),
                )
                rows = [outbox_from_row(row) for row in cur.fetchall()]
        return rows[-100:]

    def save(self, entries):
        entries = list(entries)[-100:]
        with self._pool.connection() as conn:
            with conn.transaction():
                with self._cursor(conn) as cur:
                    # Scoped: the old unscoped DELETE wiped every
                    # shop's queued replies the moment one shop saved.
                    cur.execute(
                        "DELETE FROM outbox WHERE shop_id = %s",
                        (self._shop_id,),
                    )
                    for entry in entries:
                        cur.execute(
                            "INSERT INTO outbox"
                            " (shop_id, recipient, body, intent, ts, handoff_id,"
                            " send_status, wamid, send_error, truncated)"
                            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (
                                self._shop_id,
                                entry.get("to", ""),
                                entry.get("body") or "",
                                entry.get("intent"),
                                entry.get("ts") or 0,
                                entry.get("handoff"),
                                entry.get("send_status") or "recorded",
                                entry.get("wamid"),
                                entry.get("send_error"),
                                bool(entry.get("truncated")),
                            ),
                        )


def build_stores(database_url=None, data_dir=None, shop_id=None, **pool_kwargs):
    """All six stores plus the outbox, on one backend. A DATABASE_URL
    means Postgres: one pool, schema created, the shop_id migration
    run, Postgres stores scoped to shop_id. Without one it means the
    JSON files exactly as run.py builds them today, and no database
    is needed. Local dev, demo mode, and every existing test keep
    working with no database and no new install.

    shop_id is the shop these stores belong to. None means the
    single-shop install, which writes under DEFAULT_SHOP_ID. A
    registry passes each shop's own id, so two shops on one database
    never see each other's rows."""
    from .blocks import BlockStore
    from .bookings import BookingStore
    from .handoffs import HandoffStore
    from .leads import LeadStore
    from .missed import MissedStore

    if database_url:
        shop_id = shop_id if shop_id is not None else DEFAULT_SHOP_ID
        if not shop_id.strip():
            raise ValueError("A Postgres store set needs a shop id.")
        pool = connect_pool(database_url, **pool_kwargs)
        ensure_schema(pool)
        migrate_schema(pool, shop_id)
        return SimpleNamespace(
            leads=PgLeadStore(pool, shop_id),
            bookings=PgBookingStore(pool, shop_id),
            handoffs=PgHandoffStore(pool, shop_id),
            blocks=PgBlockStore(pool, shop_id),
            missed=PgMissedStore(pool, shop_id),
            outbox=PgOutboxStore(pool, shop_id),
            pool=pool,
        )
    data = Path(data_dir) if data_dir else None
    return SimpleNamespace(
        leads=LeadStore(data / "leads.json" if data else None),
        bookings=BookingStore(data / "bookings.json" if data else None),
        handoffs=HandoffStore(data / "handoffs.json" if data else None),
        blocks=BlockStore(data / "blocks.json" if data else None),
        missed=MissedStore(data / "missed.json" if data else None),
        outbox=JsonOutboxStore(data / "outbox.json" if data else None),
        pool=None,
    )
