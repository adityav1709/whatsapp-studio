# WhatsApp Studio — AI receptionist for Indian small businesses

A small business in India lives on WhatsApp. Customers message "facial ka price kya hai?",
"timing kya hai?", "book karna hai" — and the owner is busy cutting hair, drilling teeth,
or training clients. WhatsApp Studio answers those messages instantly, captures the
customer as a lead, and lets the owner see everything on one simple dashboard.

**v1.2 — the complete product: the bot holds a conversation, and nothing it says is a lie**
- Conversation, not one message: the bot remembers what it asked — "book facial"
  then "tomorrow 11am" confirms; a slot list then "11am" or "yes" books it;
  "book" then "haircut" then a time works too. A fresh message naming its own
  service and day always starts over. An explicit cancel, human request, or
  reschedule is never read as an answer to the old question
- A confirmed booking is one the shop can keep: the slot is validated against
  opening hours, the slot grid, and closing time before anything is confirmed —
  "3am", "9am", and a 60-minute facial at 8 PM are refused with the nearest
  real slots. "12 baje" is noon, not midnight. Re-booking your own slot says
  "you already have a booking at that time"
- All times are India time (Asia/Kolkata, fixed +5:30, no tzdata): bookings,
  cancellations, open-hours checks, and the dashboard all use IST, so a
  booking made at 00:30 IST is for the right day
- Non-text messages get an answer, never silence: a voice note or photo gets a
  plain reply ("I can only read typed messages right now"), a lead record, a
  handoff, and an owner ping. Redeliveries are still answered once
- Reschedule actually reschedules: the old booking is cancelled and the bot
  asks for the new day and time; with nothing to move it says so plainly
- A closed day says "closed", never "fully booked": "closed" is a valid hours
  value, and the bot refuses bookings on it with the closed message
- The words real customers use: "parso" (day after tomorrow), "aaj shaam" /
  "dopahar" / "subah" as times, and Devanagari for price, booking, hours,
  human, thanks, and greeting
- Data survives a crash: every JSON write goes through a uniquely-named temp
  file plus rename, so a crash mid-write cannot corrupt a file. And the owner
  has a way out: `GET /export` (auth-gated) downloads all leads, bookings,
  and handoffs as one file — linked from the dashboard as "Export my data"
- More than one customer at a time: a `capacity` number per service (default
  1) — a two-chair salon takes two bookings at the same slot
- A failed send pings the owner too, through the same notifier as handoffs
  and bookings (Telegram when configured, best-effort)
- The demo runs the prospect's own shop: `PROFILE_PATH` env var in `run.py`
  (default `demo/demo_business.json`) so a prospect's profile JSON can be
  served without editing code
- Wanted, built after the demanded list: the owner replies from the dashboard
  (a reply box on each handoff card — sent through the existing sender, the
  handoff resolved, and with sending off the button says so plainly);
  today's money, tomorrow's bookings, and the week line on the dashboard
  ("Rs X booked today", "Tomorrow", "This week: N bookings, Rs X"); the
  greeting says "I'm the assistant at ..."; repeat customers get "Welcome
  back"; an `offers` field the bot can quote; and a booking with no name
  asks "May I have your name?" and records the answer
- Honest limits, unchanged: data lives in JSON files on the server's disk,
  and on Render's free tier that disk is ephemeral — a redeploy can wipe it.
  Atomic writes stop a crash corrupting a file; the export is the backup.
  Export before every redeploy. A persistent disk is a paid decision for
  when the first owner pays. The owner ping is Telegram (or the dashboard)
  until Meta approves the WhatsApp template — the WhatsApp ping is not
  built in code and must not be pretended. The bot reads typed messages,
  not voice notes; it holds a short conversation, not a free-form chat.
  One install per shop, 3–4 shops, no scale work.

**v1.1 — dashboard redesign, owner booking ping, outbox on disk**
- Webhook signature verification (`whatsapp_studio/security.py`): when
  `WHATSAPP_APP_SECRET` is set, every POST to `/webhook` must carry a
  valid `X-Hub-Signature-256` header (HMAC-SHA256 over the raw body,
  keyed by the app secret, constant-time comparison). A missing or
  invalid signature gets a 401 and the payload is never processed. The
  GET verification (`hub.verify_token`) is untouched. Without the env
  var, the server behaves exactly as before — demo mode, no verification
- Dashboard auth: when `DASHBOARD_PASSWORD` is set, the dashboard
  (GET `/`) and every data endpoint (`/leads`, `/bookings`, `/handoffs`,
  POST `/handoffs/resolve`, POST `/sending/*`) require HTTP Basic Auth
  (any username, the password is the gate; a wrong or missing credential
  gets a 401 with a `WWW-Authenticate` challenge). The webhook
  (GET/POST `/webhook`, which Meta calls and cannot log in) and
  `/health` stay open. Without the env var, no auth — exactly as before
- Both are off by default and come from the environment at runtime,
  never from the repo. Startup prints whether each is on. **Both must
  be set before a public pilot** — until then the server only ever ran
  on localhost. The secret and the password are never logged or returned
  in any response

**v1.1 — dashboard redesign, owner booking ping, outbox on disk**
- Dashboard rebuilt for the owner's phone: one column, cards not
  tables, viewport meta, WhatsApp green, white background, system
  font. First on the page is today at a glance (people messaged,
  bookings today, people waiting, plus the rupees total for today's
  bookings), then people waiting for you, then today's bookings, then
  replies the bot sent, then people who messaged. The big stop/resume
  control sits at the top
- People waiting are cards, not rows: who they are, what they asked,
  the last thing they said, how long they have waited, a tap-to-call
  link, and a large "I handled it" button. A resolved handoff is a
  count ("0 handled today"), not a row
- The owner's words, not jargon: "People waiting for you" instead of
  a handoffs table, "The bot told them..." instead of a status code.
  Failed sends are loud — "SEND FAILED" plus the plain reason ("did
  not reach the customer") — so a silent failure is impossible to miss
- Owner ping for a new booking: the moment a booking is confirmed,
  the owner gets a ping through the same notifier as handoffs
  (Telegram when configured, best-effort — a failed ping never breaks
  the booking). Double-booked attempts and cancellations do not ping
- Outbox persisted: replies the bot sent are now saved to
  `data/outbox.json` (last 100), loaded on startup, so the reply
  history survives a restart. Bookings, leads and handoffs were
  already on disk; the outbox was the last in-memory-only piece
- Honest limit: on Render's free tier the disk is ephemeral — a
  redeploy or a restart on a new instance can still lose
  `data/*.json`. Bookings, leads, handoffs and the outbox all live in
  JSON files under `data/`; a real database or a persistent disk is
  the proper fix when the pilot grows beyond 3–4 shops. Until then,
  the dashboard is the owner's record, not the disk

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
  covers the owner's fast notification meanwhile. Webhook signature
  verification and dashboard auth landed in v1.0 (above)

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
Edit `demo/demo_business.json` (or set `PROFILE_PATH` to your own JSON file
and restart): name, category, city, hours (a day's value may be "closed"),
services with INR prices and an optional `capacity` (how many customers at
once), address, `offers`, greeting. No code changes needed. A service
with no published price yet leaves `price` out (or `null`): the bot says
the salon confirms the price instead of quoting a number nobody set, and
the booking still works. `profiles/` holds draft pilot profiles for real
prospects — Snips Salon, Gurgaon is the first — with only verified facts
in them; prices wait for the owner to confirm.

**Going live**
Sending replies through Meta for real needs a WhatsApp Business app and a
`WHATSAPP_TOKEN` + `PHONE_NUMBER_ID` (both, from the environment at
runtime — never in the repo). With both set, the server sends customer
replies live; without them it stays in demo mode and records everything
to the outbox. `WHATSAPP_DRY_RUN=1` forces record-only even with a token
set, so a pilot can watch exactly what would be sent before any message
leaves. A confirmed booking pings the owner through the notifier
(Telegram when configured). The owner WhatsApp ping is v0.4b (needs a template).

**Before a public pilot (v1.0 security)**
Set both of these from the environment at runtime — never in the repo:
- `WHATSAPP_APP_SECRET` — the Meta app secret. With it set, POST
  `/webhook` requires a valid `X-Hub-Signature-256` header; without it
  anyone who finds the URL can inject fake messages.
- `DASHBOARD_PASSWORD` — one shared password for the dashboard and the
  data endpoints (HTTP Basic Auth, any username). Without it anyone who
  finds the server can read every lead and resolve handoffs.
Both are off by default for the local demo; the server prints whether
each is on at startup. `GET /webhook` (Meta's verification) and
`/health` always stay open.

**Roadmap**
- v0.4b: owner ping on WhatsApp when a handoff opens (needs a template
  message + Meta approval)
- Optional LLM replies (pluggable, off by default — costs money, so opt-in)
- Multi-business: one install serving many shops, each with its own profile
- Admin onboarding: business fills a form, gets a working receptionist

Built by Muse (Aurora) as part of an autonomous build loop.
