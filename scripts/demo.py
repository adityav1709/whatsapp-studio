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
from whatsapp_studio.bookings import BookingStore  # noqa: E402
from whatsapp_studio.config import BusinessProfile  # noqa: E402
from whatsapp_studio.handoffs import HandoffStore  # noqa: E402
from whatsapp_studio.leads import LeadStore  # noqa: E402


class FakeSender:
    """Stand-in for the Meta sender: records what would be sent, never
    touches a network. The demo runs live with this, so the whole send
    loop is visible without a token."""

    def __init__(self):
        self.calls = []

    def send(self, to, body):
        self.calls.append((to, body))
        return f"wamid.demo.{len(self.calls)}"


class DemoLLM:
    """A canned model for the demo: it calls the booking and handoff
    tools the way the real model would, then answers in words from
    what the tool said. No key, no network, so the demo runs offline."""

    def chat(self, messages, tools=None):
        # The Studio feeds the tool result back as a "tool" message;
        # answer from it, so a refused booking is refused in words too.
        last = messages[-1]
        if last["role"] == "tool":
            result = json.loads(last["content"])
            if result.get("ok"):
                if "booking" in result:
                    booking = result["booking"]
                    return {
                        "content": (
                            f"Done! Your {booking['service']} is booked for "
                            f"{booking['date']} at {booking['start']}."
                        ),
                        "tool_calls": [],
                    }
                if result.get("handoff"):
                    return {
                        "content": (
                            "I've passed this to the team. They will reply "
                            "here within 30 minutes."
                        ),
                        "tool_calls": [],
                    }
                return {"content": "Done!", "tool_calls": []}
            error = result.get("error", "")
            if error == "already taken":
                alts = ", ".join(result.get("alternatives", [])[:3])
                return {
                    "content": (
                        "Sorry, that time is already taken. "
                        f"Free slots nearby: {alts}."
                    ),
                    "tool_calls": [],
                }
            return {
                "content": f"Sorry, I could not do that ({error}).",
                "tool_calls": [],
            }
        text = messages[-1]["content"].lower()
        if "person" in text:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "hand_over",
                        "arguments": {"reason": "asked for a person"},
                    }
                ],
            }
        if "book" in text:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "create_booking",
                        "arguments": {
                            "service": "Haircut",
                            "day": "tomorrow",
                            "time": "11am",
                        },
                    }
                ],
            }
        return {
            "content": "Facial is Rs 1499 for 60 minutes. How can I help?",
            "tool_calls": [],
        }


def main():
    profile = BusinessProfile.from_json(ROOT / "demo" / "demo_business.json")
    with tempfile.TemporaryDirectory() as tmp:
        store = LeadStore(Path(tmp) / "leads.json")
        sender = FakeSender()
        studio = Studio(
            profile, store, bookings=BookingStore(), handoffs=HandoffStore(),
            sender=sender, llm=DemoLLM(),
        )
        print(f"sending state: {studio.sending_state()} (fake sender, no token)")
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
            # Priya already booked through the webhook above, so
            # booking the same slot again says it is already hers.
            # Asha tries the same slot and is refused with alternatives.
            reply = studio.handle_message(
                "919810000002", "book haircut tomorrow 11am", "Priya"
            )
            print(f"--- reply to {reply['to']} ---")
            print(reply["body"])
            print()
            reply = studio.handle_message(
                "919810000003", "book haircut tomorrow 11am", "Asha"
            )
            print(f"--- reply to {reply['to']} ---")
            print(reply["body"])
            print()
            for entry in studio.outbox[:2]:
                print(f"--- reply to {entry['to']} ---")
                print(entry["body"])
                print()
            # A handoff: Meera asks for a person, then a follow-up that
            # gets no reply while the handoff is open.
            reply = studio.handle_message(
                "919810000004", "can I talk to a person please", "Meera"
            )
            print(f"--- reply to {reply['to']} ---")
            print(reply["body"])
            print()
            paused = studio.handle_message(
                "919810000004", "hello?? my booking is wrong", "Meera"
            )
            print(
                "--- follow-up from 919810000004: "
                + ("no reply (handoff open)" if paused["body"] is None else paused["body"])
            )
            print()
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/handoffs") as resp:
                handoffs = json.loads(resp.read())["handoffs"]
            print(f"open handoffs: {len([h for h in handoffs if h['status'] == 'open'])}")
            for handoff in handoffs:
                print(
                    f"  {handoff['phone']} ({handoff.get('name') or 'unknown'}): "
                    f"{handoff['reason']} — \"{handoff['trigger']}\""
                )
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
                dashboard = resp.read().decode()
            print(f"dashboard shows handoffs: {'Handoffs' in dashboard}")
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/leads") as resp:
                leads = json.loads(resp.read())["leads"]
            print(f"leads captured: {len(leads)}")
            for lead in leads:
                last = lead["messages"][-1]["text"] if lead["messages"] else ""
                print(
                    f"  {lead['phone']} ({lead.get('name') or 'unknown'}): "
                    f"{lead['message_count']} msg, last: {last[:60]}"
                )
            # Live sending, with the fake sender: every reply above was
            # "sent" on a daemon thread, so wait for the outcomes and
            # show the audit trail.
            studio.wait_for_sends()
            print(f"sends attempted: {len(sender.calls)}")
            for entry in studio.outbox:
                outcome = entry.get("send_status") or "recorded"
                detail = entry.get("wamid") or entry.get("send_error") or ""
                print(f"  to {entry['to']}: {outcome} {detail}")
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as resp:
                health = json.loads(resp.read())
            print(f"health: live={health['live']} sending={health['sending']}")
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    main()
