"""Start WhatsApp Studio with a business profile.

Usage:  python3 run.py
Env:    PORT (default 8000), HOST (default 127.0.0.1; set 0.0.0.0 when
        hosting so the platform can reach it), VERIFY_TOKEN (default
        "studio-demo"), PROFILE_PATH (default demo/demo_business.json —
        point it at a prospect's own profile JSON to serve their shop),
        LLM_API_KEY or OPENAI_API_KEY (required: without a key the bot
        records messages and replies to nobody), LLM_MODEL (default
        gpt-4o-mini), DEMO_MODE=1 (showcase: the bot reads as on with
        sample customers on empty stores; off by default, a real
        install with no key still says so plainly),
        DATABASE_URL (set to save bookings, customers and replies in
        Postgres instead of JSON files; without it the JSON files
        under data/ are used, exactly as before)
        A registry file at profiles/registry.json means a second shop:
        each WhatsApp number id maps to a shop id and a profile file,
        and the router serves each shop from data/shops/<shop id>/.
        With no file the server serves the one shop exactly as before.
"""
from __future__ import annotations

import os
from pathlib import Path

from whatsapp_studio.app import Studio, make_server
from whatsapp_studio.config import BusinessProfile
from whatsapp_studio.db import build_stores
from whatsapp_studio.llm import client_from_env
from whatsapp_studio.multishop import ShopRouter, load_registry

BASE = Path(__file__).resolve().parent


def profile_path():
    """The file the profile came from. The setup form saves through
    this path, so it is computed once and shared."""
    return os.environ.get("PROFILE_PATH") or str(BASE / "demo" / "demo_business.json")


def load_profile():
    """The business this install serves. PROFILE_PATH lets Aditya point
    the server at a prospect's JSON file without editing code."""
    return BusinessProfile.from_json(profile_path())


def main():
    profile = load_profile()
    database_url = os.environ.get("DATABASE_URL") or None
    # Demo mode never runs against a real database: seeding sample
    # customers into it would put fake bookings on real slots. Refuse
    # loudly at boot instead of starting in a broken state.
    if database_url and os.environ.get("DEMO_MODE") == "1":
        raise SystemExit(
            "DEMO_MODE=1 refuses to run against a real database."
            " Unset DEMO_MODE or DATABASE_URL and restart."
        )
    stores = build_stores(database_url, BASE / "data")
    llm = client_from_env()
    # A registry file means a second shop: the router builds one Studio
    # per shop, default shop included, each reading its own stores
    # under data/shops/<shop id>/. With no file the server serves the
    # one shop exactly as before, so the live install is untouched
    # until shop two is real.
    router = None
    registry_path = BASE / "profiles" / "registry.json"
    if registry_path.exists():
        router = ShopRouter(
            load_registry(registry_path),
            profile_path(),
            BASE,
            database_url=database_url,
            llm=llm,
            verify_token=os.environ.get("VERIFY_TOKEN", "studio-demo"),
        )
        studio = router.default_studio
    else:
        studio = Studio(
            profile,
            stores.leads,
            verify_token=os.environ.get("VERIFY_TOKEN", "studio-demo"),
            bookings=stores.bookings,
            handoffs=stores.handoffs,
            blocks=stores.blocks,
            missed=stores.missed,
            outbox_store=stores.outbox,
            llm=llm,
            profile_path=profile_path(),
        )
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    httpd = make_server(studio, host=host, port=port, router=router)
    print(f"WhatsApp Studio running for {profile.name} on http://{host}:{port}")
    if router is not None:
        print(f"Shops on this install: {', '.join(router.shop_ids)}.")
    if database_url:
        print("Storage: Postgres (DATABASE_URL set).")
    else:
        print("Storage: JSON files under data/. Set DATABASE_URL for Postgres.")
    if studio.llm is not None:
        print(f"LLM: on (model {studio.llm.model}).")
    else:
        print("LLM: OFF, no LLM_API_KEY or OPENAI_API_KEY set. The bot cannot reply.")
    if studio.demo_mode:
        print("DEMO_MODE=1: sample customers seeded on empty stores, bot shows ON.")
    if studio.live:
        print("Live mode — replies will be sent on WhatsApp.")
    elif studio.dry_run:
        print("Dry-run mode — replies are recorded only (WHATSAPP_DRY_RUN=1).")
    else:
        print("Demo mode — replies are recorded only (set WHATSAPP_TOKEN + PHONE_NUMBER_ID to go live).")
    if studio.app_secret:
        print("Webhook signature verification: ON (WHATSAPP_APP_SECRET set).")
    else:
        print("Webhook signature verification: OFF — set WHATSAPP_APP_SECRET before a public pilot.")
    if studio.dashboard_password:
        print("Dashboard password: set.")
    else:
        print("Dashboard password: NOT SET — set DASHBOARD_PASSWORD before a public pilot.")
    print("Webhook: POST /webhook   Leads: GET /leads   Bookings: GET /bookings   Handoffs: GET /handoffs   Missed: GET /missed   Dashboard: GET /")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
