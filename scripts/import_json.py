"""One-off import: the JSON files under data/ into Postgres.

Usage:
    python3 scripts/import_json.py --dry-run
    python3 scripts/import_json.py --confirm
    python3 scripts/import_json.py --verify

Rules, all deliberate. Demo phones (9198100000*) are never imported:
they are sample customers, not real ones. The import runs in one
transaction and refuses unless every table is empty for this shop, so
a second run cannot double the data and a failed run leaves nothing
behind. --shop-id names the shop the rows belong to (default
"default", the single-shop id); a second shop imports its own JSON
files under its own id, and the import refuses when an imported id
already exists in a table for any shop, since ids are global across
shops. --dry-run prints what would be imported and writes nothing.
--confirm is required to write. --verify only checks: it compares
the counts and a sample of records, Postgres against the JSON
files, and writes nothing.

Ids are preserved for handoffs, blocks and missed records, and the
sequences are moved past the highest id, so later rows never collide
with imported ones. Bookings never had ids in JSON; they get fresh
ones. Leads are keyed by phone, so there is no id to preserve. Every
message wamid is carried into seen_wamids, so a redelivery after the
move is still caught as a duplicate.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whatsapp_studio.blocks import BlockStore
from whatsapp_studio.bookings import BookingStore
from whatsapp_studio.db import (
    DEFAULT_SHOP_ID,
    TABLES,
    JsonOutboxStore,
    connect_pool,
    ensure_schema,
)
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.missed import MissedStore

DEMO_PREFIX = "9198100000"
SEQUENCES = (
    ("handoffs", "handoffs_id_seq"),
    ("blocks", "blocks_id_seq"),
    ("missed", "missed_id_seq"),
)


def _is_demo(phone):
    return (phone or "").startswith(DEMO_PREFIX)


def load_json(data_dir):
    """The JSON files, read through the same stores the app uses."""
    return {
        "leads": LeadStore(data_dir / "leads.json").all_leads(),
        "bookings": BookingStore(data_dir / "bookings.json").all_bookings(),
        "handoffs": HandoffStore(data_dir / "handoffs.json").all_handoffs(),
        "blocks": BlockStore(data_dir / "blocks.json").all_blocks(),
        "missed": MissedStore(data_dir / "missed.json").all_records(),
        "outbox": JsonOutboxStore(data_dir / "outbox.json").load(),
    }


def filter_demo(data):
    """Drop every demo phone, and count what was dropped."""
    dropped = {}
    out = {}
    for key, records in data.items():
        kept = []
        for record in records:
            phone = record.get("phone") or record.get("to") or ""
            if _is_demo(phone):
                dropped[key] = dropped.get(key, 0) + 1
            else:
                kept.append(record)
        out[key] = kept
    return out, dropped


def table_counts(cur, shop_id):
    counts = {}
    for table in TABLES:
        cur.execute(
            f"SELECT COUNT(*) FROM {table} WHERE shop_id = %s",
            (shop_id,),
        )
        counts[table] = cur.fetchone()["count"]
    return counts


def id_collisions(cur, data):
    """Ids the JSON files would reuse that already exist in a table,
    for any shop. Ids are global across shops, so a second shop's
    export starting at 1 would collide with the first shop's rows;
    the old global empty-table guard accidentally prevented this,
    the per-shop guard does not. Returns {table: [id, ...]}."""
    collisions = {}
    for key, table in (
        ("handoffs", "handoffs"),
        ("blocks", "blocks"),
        ("missed", "missed"),
    ):
        ids = [record["id"] for record in data[key]]
        if not ids:
            continue
        cur.execute(
            f"SELECT id FROM {table} WHERE id = ANY(%s)",
            (ids,),
        )
        found = sorted(row["id"] for row in cur.fetchall())
        if found:
            collisions[table] = found
    return collisions


def _outbox_expected(entry):
    """The dict Postgres will hold for a JSON outbox entry: the entry
    as written, with the defaults the store applies to missing keys,
    and the same key omissions the row converter makes: no handoff
    key when there is none, no truncated key when it is False."""
    expected = {
        "to": entry.get("to", ""),
        "body": entry.get("body") or "",
        "intent": entry.get("intent"),
        "ts": entry.get("ts") or 0,
        "send_status": entry.get("send_status") or "recorded",
        "wamid": entry.get("wamid"),
        "send_error": entry.get("send_error"),
    }
    if entry.get("handoff") is not None:
        expected["handoff"] = entry["handoff"]
    if entry.get("truncated"):
        expected["truncated"] = True
    return expected


def import_all(cur, data, shop_id):
    """Every insert, in one transaction. The caller owns the
    transaction: on any error nothing lands. Every row is stamped
    with shop_id: the import is how a shop's JSON files land in the
    shared tables, so a row without it would be invisible to the
    shop that made it."""
    for lead in data["leads"]:
        # Leads are keyed by phone, not by id: there is no id to
        # preserve and no sequence to move.
        cur.execute(
            "INSERT INTO leads (shop_id, phone, name, first_seen, last_seen,"
            " message_count, intents, messages)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            (
                shop_id,
                lead["phone"],
                lead["name"],
                lead["first_seen"],
                lead["last_seen"],
                lead["message_count"],
                lead["intents"],
                json.dumps(lead["messages"]),
            ),
        )
        for message in lead["messages"]:
            if message.get("wamid"):
                cur.execute(
                    "INSERT INTO seen_wamids (shop_id, wamid, phone, received_ts)"
                    " VALUES (%s, %s, %s, %s) ON CONFLICT (shop_id, wamid) DO NOTHING",
                    (shop_id, message["wamid"], lead["phone"], message["ts"]),
                )
    for booking in data["bookings"]:
        cur.execute(
            "INSERT INTO bookings (shop_id, phone, name, service, price,"
            " date, start_min, duration_min, created_ts)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                shop_id,
                booking["phone"],
                booking["name"],
                booking["service"],
                booking["price"],
                date.fromisoformat(booking["date"]),
                booking["start_min"],
                booking["duration_min"],
                booking["created_ts"],
            ),
        )
    for handoff in data["handoffs"]:
        cur.execute(
            "INSERT INTO handoffs (shop_id, id, phone, name, reason, trigger,"
            " opened_at, resolved_at, status)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                shop_id,
                handoff["id"],
                handoff["phone"],
                handoff["name"],
                handoff["reason"],
                handoff["trigger"],
                handoff["opened_at"],
                handoff["resolved_at"],
                handoff["status"],
            ),
        )
    for block in data["blocks"]:
        cur.execute(
            "INSERT INTO blocks (shop_id, id, date, start_min, end_min,"
            " reason, created_ts)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                shop_id,
                block["id"],
                date.fromisoformat(block["date"]),
                block["start_min"],
                block["end_min"],
                block["reason"],
                block["created_ts"],
            ),
        )
    for record in data["missed"]:
        if record["kind"] == "refused":
            cur.execute(
                "INSERT INTO missed (shop_id, id, kind, phone, name, created_ts,"
                " last_seen_ts, occurrences, status, resolved_at,"
                " resolved_how, service, date, start_min, reason,"
                " alternatives, trigger)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " %s, %s, %s, %s, %s, %s, %s)",
                (
                    shop_id,
                    record["id"],
                    record["kind"],
                    record["phone"],
                    record["name"],
                    record["created_ts"],
                    record["last_seen_ts"],
                    record["occurrences"],
                    record["status"],
                    record["resolved_at"],
                    record["resolved_how"],
                    record["service"],
                    date.fromisoformat(record["date"])
                    if record["date"]
                    else None,
                    record["start_min"],
                    record["reason"],
                    record["alternatives"],
                    record["trigger"],
                ),
            )
        else:
            cur.execute(
                "INSERT INTO missed (shop_id, id, kind, phone, name, created_ts,"
                " last_seen_ts, occurrences, status, resolved_at,"
                " resolved_how, question, reason, handoff_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " %s, %s, %s, %s)",
                (
                    shop_id,
                    record["id"],
                    record["kind"],
                    record["phone"],
                    record["name"],
                    record["created_ts"],
                    record["last_seen_ts"],
                    record["occurrences"],
                    record["status"],
                    record["resolved_at"],
                    record["resolved_how"],
                    record["question"],
                    record["reason"],
                    record["handoff_id"],
                ),
            )
    for entry in data["outbox"]:
        expected = _outbox_expected(entry)
        cur.execute(
            "INSERT INTO outbox (shop_id, recipient, body, intent, ts,"
            " handoff_id, send_status, wamid, send_error, truncated)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                shop_id,
                expected["to"],
                expected["body"],
                expected["intent"],
                expected["ts"],
                expected.get("handoff"),
                expected["send_status"],
                expected["wamid"],
                expected["send_error"],
                expected.get("truncated", False),
            ),
        )
    for table, sequence in SEQUENCES:
        cur.execute(
            f"SELECT setval('{sequence}',"
            f" COALESCE((SELECT MAX(id) FROM {table}), 1))"
        )


def spot_check(cur, data, shop_id):
    """Compare a sample of rows, Postgres against the JSON files.
    Both sides are sorted by a stable key before comparing, since
    the JSON stores and the SQL orderings do not agree with each
    other. Only this shop's rows are read: another shop's rows
    would fail the comparison for records this shop never had.
    Returns a list of mismatch descriptions; empty means they
    match."""
    from whatsapp_studio.db import (
        block_from_row,
        booking_from_row,
        handoff_from_row,
        lead_from_row,
        missed_from_row,
        outbox_from_row,
    )

    mismatches = []
    checks = (
        ("leads", "SELECT * FROM leads WHERE shop_id = %s ORDER BY last_seen DESC LIMIT 3",
         lead_from_row, data["leads"][:3], lambda r: r,
         lambda r: r["phone"]),
        ("bookings", "SELECT * FROM bookings WHERE shop_id = %s ORDER BY date, start_min LIMIT 3",
         booking_from_row, data["bookings"][:3],
         lambda r: {k: v for k, v in r.items() if k != "id"},
         lambda r: (r["date"], r["start_min"], r["phone"])),
        ("handoffs", "SELECT * FROM handoffs WHERE shop_id = %s"
         " ORDER BY status <> 'open', opened_at DESC LIMIT 3",
         handoff_from_row, data["handoffs"][:3], lambda r: r,
         lambda r: r["id"]),
        ("blocks", "SELECT * FROM blocks WHERE shop_id = %s ORDER BY date, start_min LIMIT 3",
         block_from_row, data["blocks"][:3], lambda r: r,
         lambda r: r["id"]),
        ("missed", "SELECT * FROM missed WHERE shop_id = %s ORDER BY created_ts DESC LIMIT 3",
         missed_from_row, data["missed"][:3], lambda r: r,
         lambda r: r["id"]),
        ("outbox", "SELECT * FROM outbox WHERE shop_id = %s ORDER BY ts, id LIMIT 3",
         outbox_from_row, [_outbox_expected(e) for e in data["outbox"][:3]],
         lambda r: r, lambda r: (r["ts"], r["to"])),
    )
    for name, query, convert, expected, normalize, sort_key in checks:
        cur.execute(query, (shop_id,))
        rows = sorted(
            (normalize(convert(row)) for row in cur.fetchall()),
            key=sort_key,
        )
        if rows != sorted(expected, key=sort_key):
            mismatches.append(
                f"{name}: {len(rows)} rows read back, expected"
                f" {len(expected)} matching records"
            )
    return mismatches


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Import the JSON files under data/ into Postgres."
    )
    parser.add_argument("--data-dir", default="data",
                        help="directory holding the JSON files")
    parser.add_argument("--database-url", default=None,
                        help="Postgres URL (default: DATABASE_URL)")
    parser.add_argument("--shop-id", default=DEFAULT_SHOP_ID,
                        help="shop the rows belong to"
                        f" (default: {DEFAULT_SHOP_ID})")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true",
                       help="print what would be imported, write nothing")
    group.add_argument("--confirm", action="store_true",
                       help="actually import")
    group.add_argument("--verify", action="store_true",
                       help="compare Postgres against the JSON files")
    args = parser.parse_args(argv)

    try:
        from psycopg.rows import dict_row
    except ImportError:
        print("psycopg is not installed. Run: pip install 'psycopg[binary,pool]'")
        return 1

    data_dir = Path(args.data_dir)
    database_url = args.database_url
    if not database_url:
        import os

        database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("No database URL. Pass --database-url or set DATABASE_URL.")
        return 1

    data = load_json(data_dir)
    data, dropped = filter_demo(data)
    if dropped:
        print("Demo phones dropped:", ", ".join(
            f"{key} {count}" for key, count in sorted(dropped.items())))

    pool = connect_pool(database_url)
    ensure_schema(pool)
    shop_id = args.shop_id
    if not shop_id.strip():
        print("Refusing: --shop-id is blank.")
        return 1
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            counts = table_counts(cur, shop_id)
            collisions = id_collisions(cur, data)
    nonempty = {t: c for t, c in counts.items() if c}
    if args.dry_run:
        print(f"Would import for shop {shop_id}:")
        for key in ("leads", "bookings", "handoffs", "blocks", "missed", "outbox"):
            print(f"  {key}: {len(data[key])}")
        wamids = sum(
            1 for lead in data["leads"]
            for message in lead["messages"] if message.get("wamid")
        )
        print(f"  seen_wamids: {wamids}")
        if nonempty:
            print(f"Tables are NOT empty for shop {shop_id}:", ", ".join(
                f"{t} {c}" for t, c in sorted(nonempty.items())))
            print("The import would refuse. Empty the tables first.")
        if collisions:
            print("Id collisions with other shops' rows:",
                  ", ".join(
                      f"{t} {ids}" for t, ids in sorted(collisions.items())))
            print("The import would refuse. Those ids already exist.")
        return 0
    if args.verify:
        if not nonempty:
            print("Nothing to verify: the tables are empty.")
            return 1
        expected = {
            "leads": len(data["leads"]),
            "bookings": len(data["bookings"]),
            "handoffs": len(data["handoffs"]),
            "blocks": len(data["blocks"]),
            "missed": len(data["missed"]),
            "outbox": len(data["outbox"]),
            "seen_wamids": sum(
                1 for lead in data["leads"]
                for message in lead["messages"] if message.get("wamid")
            ),
        }
        problems = [
            f"{table}: Postgres has {counts[table]}, JSON has {expected[table]}"
            for table in TABLES
            if counts[table] != expected[table]
        ]
        with pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                problems += spot_check(cur, data, shop_id)
        if problems:
            print("Verify FAILED:")
            for problem in problems:
                print(f"  {problem}")
            return 1
        print("Verify passed: counts and sample records match the JSON files.")
        return 0
    if nonempty:
        print(f"Refusing: tables are not empty for shop {shop_id}:",
              ", ".join(f"{t} {c}" for t, c in sorted(nonempty.items())))
        print("The import only runs into empty tables for this shop. Empty them first,")
        print("or point DATABASE_URL at a fresh database.")
        return 1
    if collisions:
        print("Refusing: these ids already exist in the tables, for another shop:",
              ", ".join(f"{t} {ids}" for t, ids in sorted(collisions.items())))
        print("Ids are global across shops. Reassign them in the JSON files first.")
        return 1
    try:
        with pool.connection() as conn:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    import_all(cur, data, shop_id)
                    mismatches = spot_check(cur, data, shop_id)
                    if mismatches:
                        raise ValueError("\n".join(mismatches))
                    counts = table_counts(cur, shop_id)
    except ValueError as exc:
        # Raised inside the transaction, so nothing landed.
        print("Spot check failed, rolled back:")
        print(exc)
        return 1
    print("Imported:")
    for table in TABLES:
        print(f"  {table}: {counts[table]}")
    print("Spot check passed: the first records match the JSON files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
