"""Many shops on one install: routing by WhatsApp number id.

A router builds one Studio per shop. A message is matched to its shop
by the number id in the webhook payload, answered with that shop
profile, and recorded in that shop stores only. An unknown number id
gets no reply and no store write; a payload with no number id is
dropped in live mode and falls back to the default shop in demo mode
only. Tests use a throwaway directory per case, never the real data/.
"""
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.multishop import ShopRouter, load_registry, migrate_stores

SHOP_A = "111111111111111"
SHOP_B = "222222222222222"


def _profile(name, price, phone):
    return BusinessProfile(
        name=name,
        category="salon",
        city="Gurgaon",
        phone=phone,
        hours={"daily": "10:00 AM - 8:00 PM"},
        services=[Service("Haircut", price, 30)],
        address="Sector 50, Gurgaon",
    )


class PromptEchoLLM:
    """Answers with the shop name and Haircut price from the system
    prompt, so the reply proves which profile the model was given."""

    def __init__(self):
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append(messages)
        system = next(
            (m.get("content", "") for m in messages if m.get("role") == "system"),
            "",
        )
        name = re.search(r"receptionist for ([^,]+),", system)
        price = re.search(r"- Haircut: Rs (\d+)", system)
        shop = name.group(1) if name else "?"
        rupees = price.group(1) if price else "?"
        return {"content": f"Haircut at {shop} is Rs {rupees}.", "tool_calls": []}


def _payload(number_id, phone, text, wamid="wamid-1"):
    value = {
        "messaging_product": "whatsapp",
        "contacts": [{"profile": {"name": "Rahul"}, "wa_id": phone}],
        "messages": [
            {
                "from": phone,
                "id": wamid,
                "text": {"body": text},
                "timestamp": "1790532000",
                "type": "text",
            }
        ],
    }
    if number_id is not None:
        value["metadata"] = {"phone_number_id": number_id}
    return {"entry": [{"changes": [{"field": "messages", "value": value}]}]}


class TestRegistryValidation(unittest.TestCase):
    def test_two_numbers_for_one_shop_fails(self):
        with self.assertRaises(ValueError):
            load_registry(
                self._write(
                    {
                        SHOP_A: {"shop": "glow", "profile": "profiles/a.json"},
                        SHOP_B: {"shop": "glow", "profile": "profiles/b.json"},
                    }
                )
            )

    def test_two_shops_sharing_one_profile_fails(self):
        with self.assertRaises(ValueError):
            load_registry(
                self._write(
                    {
                        SHOP_A: {"shop": "glow", "profile": "profiles/a.json"},
                        SHOP_B: {"shop": "snips", "profile": "profiles/a.json"},
                    }
                )
            )

    def test_entry_missing_shop_or_profile_fails(self):
        with self.assertRaises(ValueError):
            load_registry(self._write({SHOP_A: {"shop": "glow"}}))
        with self.assertRaises(ValueError):
            load_registry(self._write({SHOP_A: {"profile": "profiles/a.json"}}))

    def test_empty_registry_fails(self):
        with self.assertRaises(ValueError):
            load_registry(self._write({}))

    def test_shop_id_must_be_a_plain_name(self):
        with self.assertRaises(ValueError):
            load_registry(
                self._write(
                    {SHOP_A: {"shop": "../escape", "profile": "profiles/a.json"}}
                )
            )

    def _write(self, registry):
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(registry, tmp)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return tmp.name


class TestShopRouter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "profiles").mkdir()
        (self.base / "data").mkdir()
        self.profile_a = self.base / "profiles" / "glow.json"
        self.profile_b = self.base / "profiles" / "snips.json"
        self.profile_a.write_text(
            json.dumps(_profile("Glow and Grace Salon", 499, "919999999999").to_dict()),
            encoding="utf-8",
        )
        self.profile_b.write_text(
            json.dumps(_profile("Snips Salon", 799, "919899127791").to_dict()),
            encoding="utf-8",
        )
        self.registry = {
            SHOP_A: {"shop": "glow", "profile": "profiles/glow.json"},
            SHOP_B: {"shop": "snips", "profile": "profiles/snips.json"},
        }
        self.llm = PromptEchoLLM()

    def _router(self, registry=None, llm=None, database_url=None):
        return ShopRouter(
            registry if registry is not None else self.registry,
            str(self.profile_a),
            self.base,
            database_url=database_url,
            llm=llm if llm is not None else self.llm,
        )

    def test_routes_two_shops_with_no_cross_reads(self):
        router = self._router()
        count = router.handle_webhook_payload(
            _payload(SHOP_A, "919810000001", "haircut price?", "wamid-a")
        )
        count += router.handle_webhook_payload(
            _payload(SHOP_B, "919810000001", "haircut price?", "wamid-b")
        )
        self.assertEqual(count, 2)
        shop_a = router.studio_for(SHOP_A)
        shop_b = router.studio_for(SHOP_B)
        self.assertEqual(
            [e["body"] for e in shop_a.outbox],
            ["Haircut at Glow and Grace Salon is Rs 499."],
        )
        self.assertEqual(
            [e["body"] for e in shop_b.outbox],
            ["Haircut at Snips Salon is Rs 799."],
        )
        self.assertEqual(len(shop_a.leads.all_leads()), 1)
        self.assertEqual(len(shop_b.leads.all_leads()), 1)
        self.assertEqual(shop_a.leads.all_leads()[0]["messages"][0]["text"], "haircut price?")
        self.assertEqual(shop_b.leads.all_leads()[0]["messages"][0]["text"], "haircut price?")

    def test_same_customer_two_shops_stays_separate(self):
        router = self._router()
        router.handle_webhook_payload(
            _payload(SHOP_A, "919810000001", "book haircut tomorrow 11am", "wamid-a")
        )
        router.handle_webhook_payload(
            _payload(SHOP_B, "919810000001", "book haircut tomorrow 11am", "wamid-b")
        )
        # The same phone has one lead record per shop, never shared.
        self.assertEqual(len(router.studio_for(SHOP_A).leads.all_leads()), 1)
        self.assertEqual(len(router.studio_for(SHOP_B).leads.all_leads()), 1)

    def test_unknown_number_id_writes_nothing_and_replies_to_nobody(self):
        router = self._router()
        count = router.handle_webhook_payload(
            _payload("999999999999999", "919810000001", "haircut price?", "wamid-x")
        )
        self.assertEqual(count, 0)
        for shop in (router.studio_for(SHOP_A), router.studio_for(SHOP_B)):
            self.assertEqual(shop.leads.all_leads(), [])
            self.assertEqual(shop.outbox, [])
        self.assertEqual(self.llm.calls, [])

    def test_missing_number_id_drops_in_live_mode(self):
        router = self._router()
        self.assertFalse(router.default_studio.demo_mode)
        count = router.handle_webhook_payload(
            _payload(None, "919810000001", "haircut price?", "wamid-x")
        )
        self.assertEqual(count, 0)
        self.assertEqual(router.default_studio.leads.all_leads(), [])
        self.assertEqual(self.llm.calls, [])

    def test_missing_number_id_uses_default_shop_in_demo_mode_only(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"DEMO_MODE": "1"}):
            router = self._router()
        self.assertTrue(router.default_studio.demo_mode)
        leads_before = len(router.default_studio.leads.all_leads())
        count = router.handle_webhook_payload(
            _payload(None, "919810009999", "haircut price?", "wamid-x")
        )
        self.assertEqual(count, 1)
        leads = router.default_studio.leads.all_leads()
        self.assertEqual(len(leads), leads_before + 1)
        self.assertTrue(
            any(
                m["text"] == "haircut price?"
                for lead in leads
                for m in lead["messages"]
            )
        )
        self.assertIn(
            "Haircut at Glow and Grace Salon is Rs 499.",
            [e["body"] for e in router.default_studio.outbox],
        )

    def test_registry_with_no_default_entry_fails_boot(self):
        registry = {
            SHOP_B: {"shop": "snips", "profile": "profiles/snips.json"},
        }
        with self.assertRaises(ValueError):
            self._router(registry=registry)

    def test_missing_profile_file_fails_boot(self):
        registry = {
            SHOP_A: {"shop": "glow", "profile": "profiles/glow.json"},
            SHOP_B: {"shop": "snips", "profile": "profiles/missing.json"},
        }
        with self.assertRaises(ValueError):
            self._router(registry=registry)

    def test_postgres_with_two_shops_boots(self):
        # The refusal is lifted: the two-shop isolation test in
        # test_db_live.py now passes against a real database, so a
        # second shop on Postgres boots. The stores are faked here
        # because this file never opens a real database; the live
        # test proves the scoping, this proves the router hands each
        # shop its own id.
        from unittest import mock

        from whatsapp_studio.db import build_stores as real_build_stores

        seen = []

        def fake_build_stores(database_url, data_dir, shop_id=None, **kwargs):
            seen.append((database_url, shop_id))
            return real_build_stores(None, data_dir)

        with mock.patch(
            "whatsapp_studio.multishop.build_stores",
            side_effect=fake_build_stores,
        ):
            router = self._router(
                database_url="postgresql://localhost/studio"
            )
        self.assertEqual(router.shop_ids, ["glow", "snips"])
        self.assertEqual(
            {shop for _, shop in seen}, {"glow", "snips"}
        )
        self.assertTrue(
            all(url == "postgresql://localhost/studio" for url, _ in seen)
        )
        count = router.handle_webhook_payload(
            _payload(SHOP_A, "919810000001", "haircut price?", "wamid-a")
        )
        count += router.handle_webhook_payload(
            _payload(SHOP_B, "919810000001", "haircut price?", "wamid-b")
        )
        self.assertEqual(count, 2)
        self.assertEqual(
            len(router.studio_for(SHOP_A).leads.all_leads()), 1
        )
        self.assertEqual(
            len(router.studio_for(SHOP_B).leads.all_leads()), 1
        )

    def test_postgres_with_one_shop_boots(self):
        registry = {
            SHOP_A: {"shop": "glow", "profile": "profiles/glow.json"},
        }
        router = self._router(registry=registry, database_url=None)
        self.assertEqual(router.shop_ids, ["glow"])

    def test_shop_ids_default_first(self):
        router = self._router()
        self.assertEqual(router.shop_ids, ["glow", "snips"])

    def test_concurrent_messages_to_two_shops_land_right(self):
        router = self._router()
        errors = []

        def send(number_id, phone, wamid):
            try:
                router.handle_webhook_payload(
                    _payload(number_id, phone, "haircut price?", wamid)
                )
            except Exception as exc:  # noqa: BLE001 - collected, asserted below
                errors.append(exc)

        threads = []
        for i in range(10):
            threads.append(
                threading.Thread(
                    target=send, args=(SHOP_A, f"9198100001{i:02d}", f"wamid-a{i}")
                )
            )
            threads.append(
                threading.Thread(
                    target=send, args=(SHOP_B, f"9198100002{i:02d}", f"wamid-b{i}")
                )
            )
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        shop_a = router.studio_for(SHOP_A)
        shop_b = router.studio_for(SHOP_B)
        self.assertEqual(len(shop_a.leads.all_leads()), 10)
        self.assertEqual(len(shop_b.leads.all_leads()), 10)
        self.assertTrue(
            all("Glow and Grace Salon" in e["body"] for e in shop_a.outbox)
        )
        self.assertTrue(all("Snips Salon" in e["body"] for e in shop_b.outbox))

    def test_second_shop_never_seeds_demo_data(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"DEMO_MODE": "1"}):
            router = self._router()
        self.assertEqual(router.studio_for(SHOP_B).leads.all_leads(), [])
        self.assertEqual(router.studio_for(SHOP_B).bookings.all_bookings(), [])


class TestMigrateStores(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        (self.data / "leads.json").write_text(
            json.dumps(
                {
                    "919810000001": {
                        "phone": "919810000001",
                        "name": "Rahul",
                        "first_seen": 1790532000,
                        "last_seen": 1790532000,
                        "message_count": 1,
                        "intents": [],
                        "messages": [{"text": "hi", "ts": 1790532000}],
                    },
                    "919810000002": {
                        "phone": "919810000002",
                        "name": "Priya",
                        "first_seen": 1790532000,
                        "last_seen": 1790532000,
                        "message_count": 1,
                        "intents": [],
                        "messages": [{"text": "hi", "ts": 1790532000}],
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.data / "bookings.json").write_text(
            json.dumps([{"phone": "919810000001", "service": "Haircut"}]),
            encoding="utf-8",
        )
        (self.data / "outbox.json").write_text(json.dumps([]), encoding="utf-8")

    def test_moves_files_and_keeps_counts(self):
        counts = migrate_stores(self.data, "glow")
        self.assertEqual(counts, {"leads.json": 2, "bookings.json": 1, "outbox.json": 0})
        target = self.data / "shops" / "glow"
        moved = json.loads((target / "leads.json").read_text())
        self.assertEqual(sorted(moved), ["919810000001", "919810000002"])
        self.assertFalse((self.data / "leads.json").exists())
        backups = list(self.data.glob("backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "leads.json").exists())

    def test_refuses_when_target_already_holds_data(self):
        migrate_stores(self.data, "glow")
        (self.data / "leads.json").write_text(
            json.dumps({"919810000009": {"phone": "919810000009"}}), encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            migrate_stores(self.data, "glow")

    def test_refuses_when_nothing_to_move(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        with self.assertRaises(ValueError):
            migrate_stores(empty, "glow")

    def test_router_refuses_boot_until_migration_runs(self):
        base = Path(self.tmp.name)
        (base / "profiles").mkdir()
        profile = base / "profiles" / "glow.json"
        profile.write_text(
            json.dumps(_profile("Glow and Grace Salon", 499, "919999999999").to_dict()),
            encoding="utf-8",
        )
        registry = {SHOP_A: {"shop": "glow", "profile": "profiles/glow.json"}}
        with self.assertRaises(ValueError):
            ShopRouter(registry, str(profile), base, llm=PromptEchoLLM())
        migrate_stores(self.data, "glow")
        router = ShopRouter(registry, str(profile), base, llm=PromptEchoLLM())
        self.assertEqual(router.shop_ids, ["glow"])
        self.assertEqual(len(router.default_studio.leads.all_leads()), 2)


if __name__ == "__main__":
    unittest.main()
