# WhatsApp Studio — a free AI receptionist for small businesses on WhatsApp

Customers message "facial ka price kya hai?", "timing kya hai?", "book karna hai" and the owner is busy cutting hair, drilling teeth, or training clients. WhatsApp Studio answers those messages instantly, takes the booking, and hands over to the owner when a human is needed. Free for now. Live demo: https://whatsapp-studio-x31n.onrender.com/

## Install

Needs Python 3.10 or newer. Nothing else to install for local use, the standard library is all it uses. Postgres storage needs one install, `pip install -r requirements.txt`, and only when `DATABASE_URL` is set.

```bash
git clone https://github.com/adityav1709/whatsapp-studio.git
cd whatsapp-studio
python3 run.py
```

Open http://127.0.0.1:8000 in a browser. The demo salon is live at once, no keys needed.

To run your own shop, edit `demo/demo_business.json` or point `PROFILE_PATH` at your own profile file, then restart. Name, services, prices, hours, address, all live in that one file, no code changes needed.

To let the bot reply for real, set two things from the environment, never in the repo:

- `LLM_API_KEY` — your OpenAI key. Without it the bot cannot reply. The model is `LLM_MODEL`, gpt-4o-mini by default, a few paise per message at most for a small shop.
- `GEMINI_API_KEY` — your Google Gemini key, an alternative to the OpenAI key. When it is set the bot uses Gemini instead. The model is `GEMINI_MODEL`, gemini-flash-latest by default.
- `WHATSAPP_TOKEN` and `PHONE_NUMBER_ID` — from your WhatsApp Business app. With both set the server sends customer replies live, without them it stays in demo mode and records everything to the outbox.

Before a public pilot also set `WHATSAPP_APP_SECRET` and `DASHBOARD_PASSWORD`. Details are in Going live and Before a public pilot below.

Run the tests with `python3 -m unittest discover -s tests` from the project folder.

**Saving your data (Postgres)**
- By default the shop saves to JSON files under `data/`, and on Render's free tier a redeploy can wipe them. Set `DATABASE_URL` to a Postgres connection string and the shop saves there instead: one table each for customers, bookings, handoffs, blocks, missed customers and the reply history, plus a dedup table for incoming message ids. The tables are created at startup.
- Local dev needs nothing: with no `DATABASE_URL` set, the JSON files are used exactly as before, and no database install is needed.
- Demo mode never runs against a real database. `DEMO_MODE=1` with `DATABASE_URL` set refuses to start, so sample customers can never land on real slots.
- Moving existing data over: export first, then `python3 scripts/import_json.py --dry-run` to see what would move, then `--confirm` to move it. The import runs in one step, refuses unless the tables are empty, and never imports demo phones. `--verify` checks the counts and a sample of records after the move.
- If the database goes down, the shop says so: `/health` answers 503 and the dashboard line under Setup says bookings are saved on this server for now. The bot never silently falls back to JSON on a live install.
- Honest limit: Render's free Postgres is a 30 day trial with no backups. Before real shop data lands, move to the paid tier, about $6 to $7 a month, or export regularly.

**v2.1 — the data survives a redeploy: Postgres storage**
- Set `DATABASE_URL` and every store moves to Postgres: customers, bookings, handoffs, blocks, missed customers, the outbox, and the dedup table that stops a redelivered WhatsApp message being answered twice. Without it, the JSON files carry on exactly as before.
- Two bookings can no longer take the same slot: the availability check and the insert happen as one locked step in the database, so a second webhook cannot slip in between. A reschedule is one step too: the old booking only goes when the new one is in.
- A redelivered message is answered once: the message id claim and the message record happen in one database step, so a Meta redelivery that arrives while the first is still being handled is caught.
- The dashboard says plainly how the shop saves: a quiet line under Setup, "Your bookings are saved safely" on Postgres, or a plain warning on JSON. `/health` gains `db` and `db_ok`, and answers 503 while the database is down.
- `/export` now covers everything: missed customers and the reply history join leads, bookings, handoffs and blocks, with the export time and the counts.

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
  not voice notes; it holds a free-form conversation through the model,
  but it can only do what its tools allow, so prices and bookings stay
  exact. One install per shop, 3–4 shops, no scale work.

**v2.0 — the LLM is the brain, tabs, and a dashboard with a point of view**
- The bot is LLM key based only. The model (OpenAI, gpt-4o-mini by
  default) understands the message and decides what to say and what to
  do. The rule based intents and template replies are gone. The model
  never invents a price and never creates a booking directly: it calls
  a tool and the code validates and records it. Tools: get price, list
  prices (the full catalogue), check availability, create booking,
  cancel booking, reschedule booking, hand over to the owner, set the
  customer's name. Slot validation, availability, and price lookup stay
  as code, so prices and bookings stay exact
- Negotiation and the catalogue run through the model now. It negotiates
  naturally within the floor prices (a floor price per service in the
  profile; no floor means a fixed price, never discounted), and it uses
  the full catalogue as context. A booking at an agreed price records
  the agreed price next to the list price
- The key is required, not optional. Without `LLM_API_KEY` (or the
  `OPENAI_API_KEY` alias) the bot cannot reply: the message is recorded
  and nobody is answered, and the dashboard says plainly that a key is
  needed. If the model call fails, the bot hands over to the owner
  instead of going silent. The key comes from the environment at
  runtime, never from the repo or a data file. The owner supplies their
  own key or Aditya uses his. Set it on Render as an env var
  (`LLM_API_KEY`); the model is `LLM_MODEL` (default `gpt-4o-mini`).
  gpt-4o-mini is cheap: a few paise per message at most for a small shop
- The dashboard has tabs now, server side, no JavaScript: Today (the
  glance, the money, anything wrong, the people waiting), Bookings
  (today, tomorrow, the week line), Customers (who messaged, who asked
  and did not book, what people asked about most), Replies (failures
  only), Conversation (the full WhatsApp style thread for each waiting
  person). A tab is a link to the same page with `?tab=`. The shop name
  and the bot on/off control stay on every tab. Today also carries a
  quiet Setup checklist at the bottom (bot replies, number connected,
  prices filled in, owner alerts on), hidden in demo mode: each item
  says what is missing and exactly which setting fixes it, so a fresh
  install can be checked in one glance before a pitch. Today also
  shows Bad experience, the people who wanted a booking and got a no,
  or asked a question the bot could not answer, newest first with a
  call button, so the owner can call them back. A record closes on
  its own when they book later, and a question the bot could not
  answer stays hidden while its handoff is open, since it is already
  under People waiting. Today also shows one quiet gold line under
  the money when the bot makes the first booking of the day, naming
  who booked what and when, with a bigger version once ever for the
  shop's first booking. It never appears in demo mode or dry run.
  Add ?mock=1 to the page address to preview it with sample data:
  the win line and the moments line both show, and the page says
  it is a mock preview, so nothing reads as a real customer.
  Next to it, one more quiet line for the bookings the bot made while
  the shop was closed, in the most recent closed stretch: while you
  were closed, who booked and for how much. It stays all day during
  the open day that follows, and goes away on its own when the next
  closed stretch has no bookings. When the first booking of the day
  was itself made after hours, only the moments line shows.
  Today also shows one quiet line after the setup checklist inviting
  the owner to test the bot himself: text the shop number from
  another phone and ask the price of the first priced service, with
  the number as a tap to chat link. It only appears when a test would
  actually get an answer, live sending with a key set and a price on
  file, and it stays hidden in demo mode
- The look is bold, not soft: a deep warm charcoal page, a warm gold
  accent, today's money huge up front, strong type, a greeting that
  knows the time of day. The people waiting are real people: a big
  name, their words as a quote, a call button that leads
- When the bot is off because no WhatsApp number is connected, the page
  says so plainly and says what to do next (ask Aditya to connect the
  number), instead of a dead "Bot is OFF" with nowhere to go

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
  `/health` is generic on purpose (status, database backend and
  health only — no shop name, no token in the response)
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

**Set up your shop (onboarding form)**
A prospect who wants a receptionist fills in one page at `/onboard`:
no account, no password, nothing written to the server. Shop name,
kind of shop, services with prices or blank, hours, closed days, and
her WhatsApp number, typed twice. Prices are always optional: a blank
price means the shop confirms it, and a "price varies" tick means the
price depends on the service. The form validates everything on the
server and shows the shop back as the bot will know it, with a sample
of her own bot answering a customer, then hands her a profile file to
download and send to Aditya. Nothing is live until he connects her
number and deploys it. Form bodies over 64 KB are refused. The
dashboard footer links here.

**Owner setup form**
The dashboard has a Setup tab, behind the dashboard password, where the
owner fills in prices, durations and hours herself: one price and one
time per service, one line per day (like `10:00 AM - 8:00 PM`, or
`closed`). A blank price means the shop confirms it. Saving updates the
profile file and the running bot at once, and the previous version is
kept next to the file as `<profile>.bak`, so a wrong save can be undone.
Prices saved here stay until the next deploy, then the file goes back
to what is in the repo: download the profile from Export my data and
commit it to make a save permanent. The demo page cannot be changed.

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
  finds the server can read every lead and resolve handoffs. With more
  than one shop (a registry file), this password becomes the master: it
  opens a shop picker listing every shop, and it is never shared with an
  owner. Each shop gets its own login from `DASHBOARD_PASSWORD_<SHOP>`
  (the shop id uppercased, hyphens as underscores): that password opens
  exactly its shop and nothing else, and a shop password never takes a
  shop in the URL. Two shops sharing one password, or a shop password
  equal to the master, refuses boot. A shop with no password set has no
  owner login (the master still opens it). A wrong password is never a
  lockout: the 401 says to try again or message Aditya on WhatsApp for
  a reset. One quirk of Basic Auth: a browser caches one credential per
  site, so a person running two shops needs a separate browser profile
  for the second login.
Both are off by default for the local demo; the server prints whether
each is on at startup. `GET /webhook` (Meta's verification) and
`/health` always stay open.

**Roadmap**
- v0.4b: owner ping on WhatsApp when a handoff opens (needs a template
  message + Meta approval)
- Many shops on one install: webhook routing is done, each WhatsApp
  number id maps to its shop in profiles/registry.json and each shop
  keeps its own stores under data/shops/<shop id>/; per shop dashboard
  auth is done too, each shop has its own password and the master
  password opens a shop picker
- Admin onboarding: done, a prospect fills in /onboard and downloads
  her profile

**Second shop**

To serve a second shop on the same install, first move the current
stores with `python3 scripts/migrate_multishop.py <shop id>`, then
add `profiles/registry.json` mapping each WhatsApp number id to its
shop id and profile file:

```json
{
  "111111111111111": {"shop": "glow", "profile": "profiles/glow.json"},
  "222222222222222": {"shop": "snips", "profile": "profiles/snips.json"}
}
```

The entry whose profile file matches `PROFILE_PATH` is the default
shop, the one the dashboard shows. A message for an unknown number id
gets no reply and no store write. Postgres plus more than one shop
refuses to boot until the shop id column lands.

Built by Muse (Aurora) as part of an autonomous build loop.
