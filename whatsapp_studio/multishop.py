"""Many shops on one install: route each WhatsApp message to its shop.

Slice one is routing only. A registry file maps each WhatsApp number id
to a shop id and a profile file. The router builds one Studio per shop,
so each shop keeps its own profile, stores, sender, turn state, and
sending switch, and two messages to two shops at once cannot mix. The
dashboard, setup form, onboarding form, demo mode, and Postgres schema
are untouched: the router only ever touches the webhook path.

The router only exists when the registry file exists. With no file,
run.py behaves exactly as before, which is how the live single shop
install stays untouched until a second shop is real.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from .app import Studio
from .config import BusinessProfile
from .db import build_stores
from .send import WhatsAppSender

logger = logging.getLogger(__name__)

STORE_FILES = (
    "leads.json",
    "bookings.json",
    "handoffs.json",
    "blocks.json",
    "missed.json",
    "outbox.json",
)


def load_registry(path):
    """A registry file maps a WhatsApp number id to a shop and profile.

    Shape: {"<phone_number_id>": {"shop": "<shop id>",
    "profile": "profiles/<file>.json"}}. A shop id is a plain path
    segment, so it is safe to use as a directory name under data/shops.
    Anything ambiguous fails loudly at boot instead of routing a real
    customer to the wrong shop: two numbers for one shop, two shops
    sharing one profile file, or an entry missing a shop or profile.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"Registry {path} must be a non empty object.")
    shops = {}
    profiles = {}
    for number_id, entry in raw.items():
        if not isinstance(number_id, str) or not number_id:
            raise ValueError(f"Registry {path} has a non string number id.")
        if not isinstance(entry, dict):
            raise ValueError(
                f"Registry {path}: entry for {number_id} must be an object."
            )
        shop = entry.get("shop")
        profile = entry.get("profile")
        if not shop or not isinstance(shop, str):
            raise ValueError(
                f"Registry {path}: entry for {number_id} needs a shop id."
            )
        if "/" in shop or "\\" in shop or shop in (".", ".."):
            raise ValueError(
                f"Registry {path}: shop id {shop!r} is not a plain name."
            )
        if not profile or not isinstance(profile, str):
            raise ValueError(
                f"Registry {path}: entry for {number_id} needs a profile file."
            )
        if shop in shops:
            raise ValueError(
                f"Registry {path}: shop {shop!r} is listed under two numbers."
            )
        if profile in profiles:
            raise ValueError(
                f"Registry {path}: profile {profile!r} is shared by two shops."
            )
        shops[shop] = number_id
        profiles[profile] = shop
    return raw


def _count_records(path):
    """How many records a store file holds: list length, or key count."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return len(data)
    return 1


def migrate_stores(data_dir, shop_id):
    """Move the single shop JSON stores under data/shops/<shop id>/.

    A backup of every moved file is kept beside the new home, and the
    record counts before and after must match, or the move is undone
    and the error raised. Refuses when the target already holds files,
    so a second run can never overwrite a shop data.
    Returns the per file record counts.
    """
    data_dir = Path(data_dir)
    target_dir = data_dir / "shops" / shop_id
    if target_dir.exists() and any(target_dir.iterdir()):
        raise ValueError(f"{target_dir} already holds shop data; refusing to move.")
    present = [name for name in STORE_FILES if (data_dir / name).exists()]
    if not present:
        raise ValueError(f"No store files found under {data_dir}; nothing to move.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = data_dir / f"backup-{stamp}"
    counts_before = {name: _count_records(data_dir / name) for name in present}
    target_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        for name in present:
            shutil.copy2(data_dir / name, backup_dir / name)
            shutil.move(str(data_dir / name), str(target_dir / name))
        counts_after = {name: _count_records(target_dir / name) for name in present}
    except Exception:
        for name in present:
            moved = target_dir / name
            if moved.exists():
                shutil.move(str(moved), str(data_dir / name))
        raise
    if counts_before != counts_after:
        for name in present:
            shutil.move(str(target_dir / name), str(data_dir / name))
        raise ValueError(
            f"Store counts changed during the move: {counts_before} vs {counts_after}."
        )
    return counts_after


class ShopRouter:
    """One Studio per shop, picked by the WhatsApp number id.

    The router builds every shop, default shop included, so every shop
    reads its stores from data/shops/<shop id>/. The default shop is
    the registry entry whose profile file matches profile_path.
    """

    def __init__(self, registry, profile_path, base_dir,
                 database_url=None, llm=None, verify_token="studio-demo"):
        self.registry = registry
        self.profile_path = profile_path
        self.base_dir = Path(base_dir)
        self.database_url = database_url
        self.llm = llm
        self.verify_token = verify_token
        shop_ids = {entry["shop"] for entry in registry.values()}
        # Postgres has no shop column yet, so a second shop on Postgres
        # would mix two shops rows in one table. Refuse that boot.
        if database_url and len(shop_ids) > 1:
            raise RuntimeError(
                "Many shops refuses Postgres until the shop id column lands."
                " Serve one shop on Postgres, or run the JSON stores."
            )
        self._default_number_id = self._find_default_number_id()
        self._studios = {}
        self._build_shops()
        self.default_studio = self._studios[self._default_number_id]

    def _resolve_profile(self, profile):
        path = Path(profile)
        return path if path.is_absolute() else self.base_dir / path

    def _find_default_number_id(self):
        """The registry entry whose profile file is the default shop."""
        default_path = Path(self.profile_path).resolve()
        for number_id, entry in self.registry.items():
            if self._resolve_profile(entry["profile"]).resolve() == default_path:
                return number_id
        raise ValueError(
            "The registry has no entry for the default profile "
            f"{default_path}; add one before starting many shops."
        )

    def _shop_data_dir(self, shop_id):
        """Where a shop stores live, with the migration guard.

        A registry with no moved stores yet means the old files are
        still sitting under data/. Starting then would serve an empty
        shop while the real bookings sit one directory up, so boot
        refuses and names the script that moves them.
        """
        data_dir = self.base_dir / "data"
        shop_dir = data_dir / "shops" / shop_id
        if not shop_dir.exists() or not any(shop_dir.iterdir()):
            if any((data_dir / name).exists() for name in STORE_FILES):
                raise ValueError(
                    "Store files are still under data/ but the registry "
                    "expects them under data/shops/. Run "
                    f"python3 scripts/migrate_multishop.py {shop_id} first."
                )
        return shop_dir

    def _build_studio(self, number_id, entry, demo_mode):
        profile_path = self._resolve_profile(entry["profile"])
        if not profile_path.exists():
            raise ValueError(f"Profile file {profile_path} does not exist.")
        profile = BusinessProfile.from_json(profile_path)
        stores = build_stores(
            self.database_url,
            self._shop_data_dir(entry["shop"]),
        )
        token = os.environ.get("WHATSAPP_TOKEN")
        sender = WhatsAppSender(token, number_id) if token else None
        return Studio(
            profile,
            stores.leads,
            verify_token=self.verify_token,
            bookings=stores.bookings,
            handoffs=stores.handoffs,
            blocks=stores.blocks,
            missed=stores.missed,
            sender=sender,
            outbox_store=stores.outbox,
            llm=self.llm,
            demo_mode=demo_mode,
            profile_path=str(profile_path),
        )

    def _build_shops(self):
        for number_id, entry in self.registry.items():
            # The default shop reads demo mode from the environment, so
            # the live single shop behaves exactly as before. A second
            # shop never seeds demo data: sample customers belong to
            # the showcase shop only.
            demo_mode = None if number_id == self._default_number_id else False
            self._studios[number_id] = self._build_studio(
                number_id, entry, demo_mode=demo_mode
            )

    @property
    def shop_ids(self):
        """Every shop id in the registry, default shop first."""
        default_shop = self.registry[self._default_number_id]["shop"]
        rest = sorted(
            entry["shop"]
            for number_id, entry in self.registry.items()
            if number_id != self._default_number_id
        )
        return [default_shop] + rest

    def studio_for(self, number_id):
        """The Studio for a WhatsApp number id, or None when unknown."""
        if not number_id:
            return None
        if number_id == self._default_number_id:
            return self.default_studio
        return self._studios.get(number_id)

    def studio_for_shop(self, shop_id):
        """The Studio for a shop id, or None when no shop has it. The
        dashboard gate resolves a login to a shop id, then asks here,
        so a request never touches a shop through a number id it was
        not given."""
        for number_id, entry in self.registry.items():
            if entry["shop"] == shop_id:
                return self._studios[number_id]
        return None

    def handle_webhook_payload(self, payload):
        """Route each change to its shop and let that shop answer it.

        An unknown number id gets no reply, no booking, and no store
        write: the message is logged and dropped, and Meta still gets
        its 200, so it stops redelivering. A payload with no number id
        is dropped the same way in live mode; in demo mode only it
        falls back to the default shop, because demo payloads carry no
        number id. Returns the count of messages handled.
        """
        count = 0
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                number_id = ((value.get("metadata") or {}).get("phone_number_id") or "")
                studio = self.studio_for(number_id)
                if studio is None and not number_id and self.default_studio.demo_mode:
                    studio = self.default_studio
                if studio is None:
                    if number_id:
                        logger.warning(
                            "No shop registered for WhatsApp number id %s; "
                            "message dropped.",
                            number_id,
                        )
                    else:
                        logger.warning(
                            "Webhook payload with no WhatsApp number id; "
                            "message dropped."
                        )
                    continue
                count += studio.handle_webhook_payload(
                    {"entry": [{"changes": [change]}]}
                )
        return count
