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
        install with no key still says so plainly)
"""
from __future__ import annotations

import os
from pathlib import Path

from whatsapp_studio.app import Studio, make_server
from whatsapp_studio.blocks import BlockStore
from whatsapp_studio.bookings import BookingStore
from whatsapp_studio.config import BusinessProfile
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.llm import client_from_env

BASE = Path(__file__).resolve().parent


def load_profile():
    """The business this install serves. PROFILE_PATH lets Aditya point
    the server at a prospect's JSON file without editing code."""
    path = os.environ.get("PROFILE_PATH") or str(BASE / "demo" / "demo_business.json")
    return BusinessProfile.from_json(path)


def main():
    profile = load_profile()
    store = LeadStore(BASE / "data" / "leads.json")
    bookings = BookingStore(BASE / "data" / "bookings.json")
    handoffs = HandoffStore(BASE / "data" / "handoffs.json")
    blocks = BlockStore(BASE / "data" / "blocks.json")
    studio = Studio(profile, store, verify_token=os.environ.get("VERIFY_TOKEN", "studio-demo"), bookings=bookings, handoffs=handoffs, blocks=blocks, outbox_path=BASE / "data" / "outbox.json", llm=client_from_env())
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    httpd = make_server(studio, host=host, port=port)
    print(f"WhatsApp Studio running for {profile.name} on http://{host}:{port}")
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
    print("Webhook: POST /webhook   Leads: GET /leads   Bookings: GET /bookings   Handoffs: GET /handoffs   Dashboard: GET /")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
