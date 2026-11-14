# WhatsApp Studio — AI receptionist for Indian small businesses

A small business in India lives on WhatsApp. Customers message "facial ka price kya hai?",
"timing kya hai?", "book karna hai" — and the owner is busy cutting hair, drilling teeth,
or training clients. WhatsApp Studio answers those messages instantly, captures the
customer as a lead, and lets the owner see everything on one simple dashboard.

**v0.4a — live sending: customer replies go out for real**
- A sender module (`whatsapp_studio/send.py`, same shape as the Telegram
  notifier): stdlib `urllib` POST to the pinned Graph API version
  (`v21.0`), Bearer token, returns Meta's message id (`wamid`).
  `sender_from_env()` builds a real sender only when BOTH `WHATSAPP_TOKEN`
  and `PHONE_NUMBER_ID` are set; a token alone is not live
- The outbox is the audit trail: every reply is recorded first, then
  sent. Each entry records sent / failed, the `wamid` when it went out,
  the error when it did not. Bodies over Meta's 4096-character cap are
  truncated and the truncation is recorded
- Duplicate protection: Meta redelivers a webhook when it does not get a
  200 fast, so incoming message ids (`wamid`) are recorded in the lead
  record and a redelivery is skipped — one reply per incoming message,
  never two. One send attempt only, never a blind retry
- Wrong-number guards: a reply goes only to the phone the message came
  from (digits only), never to the business's own number
- Best-effort send on a daemon thread: business state is committed
  before the send, the webhook returns its 200 fast, a failed send is
  recorded and shown on the dashboard as a send-failed marker, and it
  never crashes the server
- Demo vs live: live only when both env vars are set, otherwise demo
  mode (outbox only, exactly as before). `WHATSAPP_DRY_RUN=1` forces
  record-only even with a token set. Startup prints the mode;
  `/health` reports `live` and `sending` (no token in the response)
- Kill switch: a dashboard button (POST `/sending/disable`) stops all
  sending instantly — replies go to the outbox only until it is switched
  back on (POST `/sending/enable`)
- Honest limits: the owner's WhatsApp ping is NOT in v0.4a — it is
  business-initiated and usually falls outside Meta's 24-hour window,
  so a reliable ping needs a template message (Meta approval, a
  business-admin step, not code). That is v0.4b. Telegram (v0.3) still
  covers the owner's fast notification meanwhile. And: webhook
  signature verification and dashboard auth are prerequisites that must
  land BEFORE a public pilot — the webhook is reachable from the
  internet and the dashboard can resolve handoffs. They are the next
  build, not this one

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

**Going live**
Sending replies through Meta for real needs a WhatsApp Business app and a
`WHATSAPP_TOKEN` + `PHONE_NUMBER_ID` (both, from the environment at
runtime — never in the repo). With both set, the server sends customer
replies live; without them it stays in demo mode and records everything
to the outbox. `WHATSAPP_DRY_RUN=1` forces record-only even with a token
set, so a pilot can watch exactly what would be sent before any message
leaves. The owner WhatsApp ping is v0.4b (needs a template); webhook
signature verification and dashboard auth must land before a public pilot.

**Roadmap**
- v0.4b: owner ping on WhatsApp when a handoff opens (needs a template
  message + Meta approval)
- Webhook signature verification (`X-Hub-Signature-256`) and dashboard
  auth — prerequisites before any public pilot
- Optional LLM replies (pluggable, off by default — costs money, so opt-in)
- Multi-business: one install serving many shops, each with its own profile
- Admin onboarding: business fills a form, gets a working receptionist

Built by Muse (Aurora) as part of an autonomous build loop.
