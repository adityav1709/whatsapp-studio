"""Start WhatsApp Studio with the demo business profile.

Usage:  python3 run.py
Env:    PORT (default 8000), VERIFY_TOKEN (default "studio-demo")
"""
from __future__ import annotations

import os
from pathlib import Path

from whatsapp_studio.app import Studio, make_server
from whatsapp_studio.config import BusinessProfile
from whatsapp_studio.leads import LeadStore

BASE = Path(__file__).resolve().parent


def main():
    profile = BusinessProfile.from_json(BASE / "demo" / "demo_business.json")
    store = LeadStore(BASE / "data" / "leads.json")
    studio = Studio(profile, store, verify_token=os.environ.get("VERIFY_TOKEN", "studio-demo"))
    port = int(os.environ.get("PORT", "8000"))
    httpd = make_server(studio, port=port)
    print(f"WhatsApp Studio running for {profile.name} on http://127.0.0.1:{port}")
    print("Webhook: POST /webhook   Leads: GET /leads   Dashboard: GET /")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
