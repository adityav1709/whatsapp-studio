"""End-to-end demo: boots the server, sends sample WhatsApp messages,
prints the auto-replies and the captured leads, then shuts down.

Usage: python3 scripts/demo.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whatsapp_studio.app import Studio, make_server  # noqa: E402
from whatsapp_studio.config import BusinessProfile  # noqa: E402
from whatsapp_studio.leads import LeadStore  # noqa: E402


def main():
    profile = BusinessProfile.from_json(ROOT / "demo" / "demo_business.json")
    with tempfile.TemporaryDirectory() as tmp:
        store = LeadStore(Path(tmp) / "leads.json")
        studio = Studio(profile, store)
        httpd = make_server(studio, port=0)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            payload = json.loads((ROOT / "demo" / "sample_webhook_payload.json").read_text())
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/webhook",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                print("webhook ->", resp.status, resp.read().decode())
            print()
            for entry in studio.outbox:
                print(f"--- reply to {entry['to']} (intent: {entry['intent']}) ---")
                print(entry["body"])
                print()
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/leads") as resp:
                leads = json.loads(resp.read())["leads"]
            print(f"leads captured: {len(leads)}")
            for lead in leads:
                print(
                    f"  {lead['phone']} ({lead.get('name') or 'unknown'}): "
                    f"{lead['message_count']} msg, intents={lead['intents']}"
                )
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    main()
