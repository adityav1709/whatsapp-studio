# WhatsApp Studio — AI receptionist for Indian small businesses

A small business in India lives on WhatsApp. Customers message "facial ka price kya hai?",
"timing kya hai?", "book karna hai" — and the owner is busy cutting hair, drilling teeth,
or training clients. WhatsApp Studio answers those messages instantly, captures the
customer as a lead, and lets the owner see everything on one simple dashboard.

**v0.3 — human handoff (the promise kept)**
- A customer asking for a person ("talk to someone", "manager se baat karni
  hai", "call me") — or the bot failing them twice in a row — opens a
  handoff: it is recorded, the bot goes quiet for that customer, and the
  owner is surfaced the request. One honest confirmation goes to the
  customer; after hours it says plainly the shop is closed and exactly
  when they will hear back
- Handoffs are persisted to JSON (`GET /handoffs`, dashboard at `GET /`),
  open ones first with the customer's messages since it opened, long-open
  ones marked urgent. The owner resolves one with a button on the
  dashboard and the bot resumes from the next message. No timers, no
  auto-resolve
- Optional Telegram ping to the owner the moment a handoff opens
  (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` env vars, off by default,
  best-effort — a failed ping never breaks the handoff). The notifier is a
  small interface, so other channels can plug in later
- Honest limit: the owner's WhatsApp ping is NOT in v0.3 — it needs the
  live Meta send path, which is the next milestone. Until then, the owner
  watches the dashboard or Telegram

**v0.2 — booking with real availability**
- Customers book in plain language: "book haircut tomorrow 11am",
  "facial on Saturday 4pm", "kal 11 baje" — day and time are parsed from
  the message (English + Hinglish)
- Slots are generated from the business's opening hours and the service's
  duration, and checked against confirmed bookings before anything is
  promised — a double-booked slot is refused with the nearest free
  alternatives instead
- No time given? The bot lists the free slots for that day. Past times
  are rejected with what is still free today
- "cancel my booking" cancels the customer's latest upcoming booking
- Bookings are persisted to JSON and shown on the dashboard
  (`GET /bookings`, dashboard at `GET /`)

**v0.1 — what works today**
- Webhook endpoint that accepts Meta WhatsApp Cloud API message payloads
  (`POST /webhook`, with Meta-style `hub.verify_token` verification on `GET /webhook`)
- Intent detection in English + Hinglish: greeting, hours, price, booking,
  location, human handoff, thanks — with a helpful fallback
- Auto-replies built from the business profile (services, prices, hours, address)
- Lead capture: every new phone number becomes a lead with message history,
  persisted to JSON (`GET /leads`, dashboard at `GET /`)
- Zero dependencies — Python 3.10+ standard library only. No money spent, no
  accounts needed to run it.

**Quickstart**
```bash
python3 run.py            # serves the demo salon on http://127.0.0.1:8000
python3 scripts/demo.py   # boots the server, fires sample WhatsApp messages, prints replies + leads
python3 -m unittest discover -s tests -v   # test suite (run from this directory)
```

**Try it with curl**
```bash
# webhook verification (what Meta calls when you register the webhook)
curl "http://127.0.0.1:8000/webhook?hub.mode=subscribe&hub.verify_token=studio-demo&hub.challenge=hello"

# send a sample customer message
curl -X POST http://127.0.0.1:8000/webhook -H 'Content-Type: application/json' \
  -d @demo/sample_webhook_payload.json

# see the captured leads
curl http://127.0.0.1:8000/leads
```

**Configure a real business**
Edit `demo/demo_business.json` (or point `run.py` at your own JSON): name,
category, city, hours, services with INR prices, address. No code changes needed.

**Going live (not wired up yet — deliberately)**
Sending replies through Meta for real needs a WhatsApp Business app and a
`WHATSAPP_TOKEN` + `PHONE_NUMBER_ID`. The code is structured for it
(`Studio.live`, outbox), but this repo ships in demo mode: replies are
generated and recorded, never sent. No credentials live in this repo.

**Roadmap**
- Owner ping on WhatsApp when a handoff opens (needs the live Meta send path)
- Optional LLM replies (pluggable, off by default — costs money, so opt-in)
- Multi-business: one install serving many shops, each with its own profile
- Admin onboarding: business fills a form, gets a working receptionist

Built by Muse (Aurora) as part of an autonomous build loop.
