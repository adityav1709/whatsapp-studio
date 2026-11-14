"""Start WhatsApp Studio with the demo business profile.

Usage:  python3 run.py
Env:    PORT (default 8000), HOST (default 127.0.0.1; set 0.0.0.0 when
        hosting so the platform can reach it), VERIFY_TOKEN (default
        "studio-demo")
"""
from __future__ import annotations

import os
from pathlib import Path

from whatsapp_studio.app import Studio, make_server
from whatsapp_studio.bookings import BookingStore
from whatsapp_studio.config import BusinessProfile
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore

BASE = Path(__file__).resolve().parent


def main():
    profile = BusinessProfile.from_json(BASE / "demo" / "demo_business.json")
    store = LeadStore(BASE / "data" / "leads.json")
    bookings = BookingStore(BASE / "data" / "bookings.json")
    handoffs = HandoffStore(BASE / "data" / "handoffs.json")
    studio = Studio(profile, store, verify_token=os.environ.get("VERIFY_TOKEN", "studio-demo"), bookings=bookings, handoffs=handoffs, outbox_path=BASE / "data" / "outbox.json")
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    httpd = make_server(studio, host=host, port=port)
    print(f"WhatsApp Studio running for {profile.name} on http://{host}:{port}")
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
