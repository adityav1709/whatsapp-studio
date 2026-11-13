# WhatsApp Studio — AI receptionist for Indian small businesses

A small business in India lives on WhatsApp. Customers message "facial ka price kya hai?",
"timing kya hai?", "book karna hai" — and the owner is busy cutting hair, drilling teeth,
or training clients. WhatsApp Studio answers those messages instantly, captures the
customer as a lead, and lets the owner see everything on one simple dashboard.

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
- Booking slots: real availability + confirmation messages
- Human handoff: notify the owner on WhatsApp/Telegram when a customer asks
- Optional LLM replies (pluggable, off by default — costs money, so opt-in)
- Multi-business: one install serving many shops, each with its own profile
- Admin onboarding: business fills a form, gets a working receptionist

Built by Muse (Aurora) as part of an autonomous build loop.
