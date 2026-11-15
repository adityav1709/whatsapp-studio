"""Push the sample day through the live WhatsApp Studio webhook.

The live showcase seeds itself at boot in demo mode, so you normally
do not need this. It stays as the manual way to push the same sample
day through the webhook on a fresh instance: demo mode only records
replies, nothing is sent to a real phone. Uses the 9198100000xx
sample range so the numbers are clearly not real customers. Do not
run it against the live showcase once it has seeded: the same phones
would get duplicate messages.
"""

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whatsapp_studio.demo_data import webhook_steps  # noqa: E402

URL = "https://whatsapp-studio-x31n.onrender.com/webhook"


def payload(phone, name, wamid, msg_type, body=None):
    msg = {"from": phone, "id": wamid, "type": msg_type}
    if msg_type == "text":
        msg["text"] = {"body": body}
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "demo",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "919999999999",
                                "phone_number_id": "demo",
                            },
                            "contacts": [
                                {"profile": {"name": name}, "wa_id": phone}
                            ],
                            "messages": [msg],
                        },
                    }
                ],
            }
        ],
    }


def post(data):
    req = urllib.request.Request(
        URL,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status, resp.read().decode("utf-8")


# A realistic day at Glow & Grace Salon, from the shared sample day
# in whatsapp_studio/demo_data.py: Rahul asks a price then books a
# haircut, Priya books a facial for tomorrow, Amit asks for a person,
# Neha books a hair spa, Vikram sends a voice note, Sana asks a price
# and does not book.
for phone, name, wamid, msg_type, body in webhook_steps():
    status, resp = post(payload(phone, name, wamid, msg_type, body))
    print(f"{name} ({msg_type}): HTTP {status} {resp}")
