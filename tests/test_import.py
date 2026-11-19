"""The import script against a fake driver: no database, no network.

The script is loaded from scripts/import_json.py. Its pool is
patched to the same scripted fake cursor test_db.py uses, so a test
spells out the table counts and the rows read back, in order.
"""
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

try:
    import psycopg  # noqa: F401
except ImportError:
    _psycopg = types.ModuleType("psycopg")
    _rows = types.ModuleType("psycopg.rows")

    class dict_row:
        pass

    _rows.dict_row = dict_row
    _psycopg.rows = _rows
    sys.modules.setdefault("psycopg", _psycopg)
    sys.modules.setdefault("psycopg.rows", _rows)

from test_db import FakeCursor, FakePool

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "import_json", ROOT / "scripts" / "import_json.py"
)
import_json = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(import_json)

PHONE = "919999900001"
DEMO_PHONE = "919810000001"
DAY = "2026-11-20"


def _write_fixtures(directory):
    """A small shop in JSON: one real record of each kind, plus demo
    phones that must never be imported."""
    data = Path(directory)
    (data / "leads.json").write_text(json.dumps({
        PHONE: {
            "phone": PHONE,
            "name": "Priya",
            "first_seen": 1000,
            "last_seen": 1100,
            "message_count": 1,
            "intents": ["greeting"],
            "messages": [
                {"ts": 1100, "text": "hi", "intent": "greeting", "wamid": "w1"}
            ],
        },
        DEMO_PHONE: {
            "phone": DEMO_PHONE,
            "name": "Sample",
            "first_seen": 1000,
            "last_seen": 1000,
            "message_count": 1,
            "intents": [],
            "messages": [
                {"ts": 1000, "text": "hi", "intent": None, "wamid": None}
            ],
        },
    }), encoding="utf-8")
    (data / "bookings.json").write_text(json.dumps([
        {
            "phone": PHONE, "name": "Priya", "service": "Haircut",
            "price": 499, "date": DAY, "start_min": 660,
            "duration_min": 30, "created_ts": 1000,
        },
        {
            "phone": DEMO_PHONE, "name": "Sample", "service": "Haircut",
            "price": 499, "date": DAY, "start_min": 720,
            "duration_min": 30, "created_ts": 1000,
        },
    ]), encoding="utf-8")
    (data / "handoffs.json").write_text(json.dumps([
        {
            "id": 1, "phone": PHONE, "name": "Priya",
            "reason": "asked for a person", "trigger": "talk to a person",
            "opened_at": 1000, "resolved_at": None, "status": "open",
        },
    ]), encoding="utf-8")
    (data / "blocks.json").write_text(json.dumps([
        {
            "id": 1, "date": DAY, "start_min": 600, "end_min": 660,
            "reason": "lunch", "created_ts": 1000,
        },
    ]), encoding="utf-8")
    (data / "missed.json").write_text(json.dumps([
        {
            "id": 1, "kind": "refused", "phone": PHONE, "name": "Priya",
            "created_ts": 1000, "last_seen_ts": 1000, "occurrences": 1,
            "status": "open", "resolved_at": None, "resolved_how": None,
            "service": "Haircut", "date": DAY, "start_min": 660,
            "reason": "already taken", "alternatives": ["11:30 AM"],
            "trigger": "book haircut tomorrow 11am",
        },
        {
            "id": 2, "kind": "unanswered", "phone": PHONE, "name": "Priya",
            "created_ts": 1100, "last_seen_ts": 1100, "occurrences": 1,
            "status": "open", "resolved_at": None, "resolved_how": None,
            "question": "Do you do keratin?", "reason": "cannot answer",
            "handoff_id": None,
        },
    ]), encoding="utf-8")
    (data / "outbox.json").write_text(json.dumps([
        {"to": PHONE, "body": "Hi Priya", "intent": None, "ts": 1000},
        {"to": DEMO_PHONE, "body": "Hi Sample", "intent": None, "ts": 1001},
    ]), encoding="utf-8")
    return data


def _counts(*values):
    """Scripted table counts, in TABLES order."""
    return [{"one": {"count": value}} for value in values]


def _spot_rows():
    """The rows the spot check reads back: the fixture records as
    Postgres would return them."""
    lead = {
        "phone": PHONE, "name": "Priya", "first_seen": 1000,
        "last_seen": 1100, "message_count": 1, "intents": ["greeting"],
        "messages": [
            {"ts": 1100, "text": "hi", "intent": "greeting", "wamid": "w1"}
        ],
    }
    booking = {
        "id": 1, "phone": PHONE, "name": "Priya", "service": "Haircut",
        "price": 499, "date": date(2026, 11, 20), "start_min": 660,
        "duration_min": 30, "created_ts": 1000,
    }
    handoff = {
        "id": 1, "phone": PHONE, "name": "Priya",
        "reason": "asked for a person", "trigger": "talk to a person",
        "opened_at": 1000, "resolved_at": None, "status": "open",
    }
    block = {
        "id": 1, "date": date(2026, 11, 20), "start_min": 600,
        "end_min": 660, "reason": "lunch", "created_ts": 1000,
    }
    refused = {
        "id": 1, "kind": "refused", "phone": PHONE, "name": "Priya",
        "created_ts": 1000, "last_seen_ts": 1000, "occurrences": 1,
        "status": "open", "resolved_at": None, "resolved_how": None,
        "service": "Haircut", "date": date(2026, 11, 20), "start_min": 660,
        "reason": "already taken", "alternatives": ["11:30 AM"],
        "trigger": "book haircut tomorrow 11am",
        "question": None, "handoff_id": None,
    }
    unanswered = {
        "id": 2, "kind": "unanswered", "phone": PHONE, "name": "Priya",
        "created_ts": 1100, "last_seen_ts": 1100, "occurrences": 1,
        "status": "open", "resolved_at": None, "resolved_how": None,
        "service": None, "date": None, "start_min": None,
        "reason": "cannot answer", "alternatives": [],
        "trigger": None, "question": "Do you do keratin?",
        "handoff_id": None,
    }
    outbox = {
        "recipient": PHONE, "body": "Hi Priya", "intent": None, "ts": 1000,
        "handoff_id": None, "send_status": "recorded", "wamid": None,
        "send_error": None, "truncated": False,
    }
    return [
        {"all": [lead]},
        {"all": [booking]},
        {"all": [handoff]},
        {"all": [block]},
        {"all": [unanswered, refused]},
        {"all": [outbox]},
    ]


class TestImportJson(unittest.TestCase):
    def _run_main(self, pool, args):
        """Run the script's main with a fake pool. ensure_schema is
        patched out: it is covered by test_db.py, and here it would
        only eat the scripted outcomes."""
        out = io.StringIO()
        with mock.patch.object(
            import_json, "connect_pool", return_value=pool
        ), mock.patch.object(import_json, "ensure_schema"), redirect_stdout(out):
            result = import_json.main(args)
        return result, out.getvalue()

    def test_demo_phones_are_filtered(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _write_fixtures(tmp)
            loaded = import_json.load_json(data)
            filtered, dropped = import_json.filter_demo(loaded)
            self.assertEqual(len(filtered["leads"]), 1)
            self.assertEqual(len(filtered["bookings"]), 1)
            self.assertEqual(len(filtered["outbox"]), 1)
            self.assertEqual(len(filtered["missed"]), 2)
            self.assertEqual(dropped, {"leads": 1, "bookings": 1, "outbox": 1})

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _write_fixtures(tmp)
            cursor = FakeCursor()
            cursor.script(*_counts(0, 0, 0, 0, 0, 0, 0))
            pool = FakePool(cursor)
            result, output = self._run_main(pool, [
                "--dry-run", "--data-dir", str(data),
                "--database-url", "postgres://fake",
            ])
            self.assertEqual(result, 0)
            self.assertIn("Would import:", output)
            self.assertIn("leads: 1", output)
            self.assertIn("seen_wamids: 1", output)
            self.assertNotIn("INSERT", " ".join(
                sql for sql, _ in cursor.executed))

    def test_confirm_imports_everything_and_spot_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _write_fixtures(tmp)
            cursor = FakeCursor()
            # The import itself runs statements that return no rows:
            # one insert per record, one per wamid, one setval per
            # sequence. Each still pops a scripted outcome, so they
            # need placeholders or the spot check reads the wrong ones.
            loaded, _ = import_json.filter_demo(import_json.load_json(data))
            wamids = sum(
                1 for lead in loaded["leads"]
                for message in lead["messages"] if message.get("wamid")
            )
            import_statements = (
                len(loaded["leads"]) + wamids + len(loaded["bookings"])
                + len(loaded["handoffs"]) + len(loaded["blocks"])
                + len(loaded["missed"]) + len(loaded["outbox"])
                + len(import_json.SEQUENCES)
            )
            cursor.script(
                *_counts(0, 0, 0, 0, 0, 0, 0),
                *[{} for _ in range(import_statements)],
                *_spot_rows(),
                *_counts(1, 1, 1, 1, 1, 2, 1),
            )
            pool = FakePool(cursor)
            result, output = self._run_main(pool, [
                "--confirm", "--data-dir", str(data),
                "--database-url", "postgres://fake",
            ])
            self.assertEqual(result, 0)
            self.assertIn("Imported:", output)
            self.assertIn("Spot check passed", output)
            sql = " ".join(sql for sql, _ in cursor.executed)
            self.assertIn("INSERT INTO leads", sql)
            self.assertIn("INSERT INTO seen_wamids", sql)
            self.assertIn("INSERT INTO bookings", sql)
            self.assertIn("INSERT INTO handoffs", sql)
            self.assertIn("INSERT INTO blocks", sql)
            self.assertIn("INSERT INTO missed", sql)
            self.assertIn("INSERT INTO outbox", sql)
            self.assertIn("setval('handoffs_id_seq'", sql)
            self.assertIn("setval('missed_id_seq'", sql)
            # No demo phone anywhere in the statements or params.
            self.assertNotIn(DEMO_PHONE, str(cursor.executed))

    def test_refuses_nonempty_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _write_fixtures(tmp)
            cursor = FakeCursor()
            cursor.script(*_counts(0, 0, 2, 0, 0, 0, 0))
            pool = FakePool(cursor)
            result, output = self._run_main(pool, [
                "--confirm", "--data-dir", str(data),
                "--database-url", "postgres://fake",
            ])
            self.assertEqual(result, 1)
            self.assertIn("Refusing", output)
            self.assertNotIn("INSERT", " ".join(
                sql for sql, _ in cursor.executed))

    def test_verify_passes_when_counts_and_samples_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _write_fixtures(tmp)
            cursor = FakeCursor()
            cursor.script(
                *_counts(1, 1, 1, 1, 1, 2, 1),
                *_spot_rows(),
            )
            pool = FakePool(cursor)
            result, output = self._run_main(pool, [
                "--verify", "--data-dir", str(data),
                "--database-url", "postgres://fake",
            ])
            self.assertEqual(result, 0)
            self.assertIn("Verify passed", output)

    def test_verify_fails_on_count_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _write_fixtures(tmp)
            cursor = FakeCursor()
            cursor.script(
                *_counts(5, 1, 1, 1, 1, 2, 1),
                *_spot_rows(),
            )
            pool = FakePool(cursor)
            result, output = self._run_main(pool, [
                "--verify", "--data-dir", str(data),
                "--database-url", "postgres://fake",
            ])
            self.assertEqual(result, 1)
            self.assertIn("Verify FAILED", output)
            self.assertIn("leads: Postgres has 5, JSON has 1", output)

    def test_verify_with_empty_tables_reports_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _write_fixtures(tmp)
            cursor = FakeCursor()
            cursor.script(*_counts(0, 0, 0, 0, 0, 0, 0))
            pool = FakePool(cursor)
            result, output = self._run_main(pool, [
                "--verify", "--data-dir", str(data),
                "--database-url", "postgres://fake",
            ])
            self.assertEqual(result, 1)
            self.assertIn("Nothing to verify", output)


if __name__ == "__main__":
    unittest.main()
