# Human Handoff — Engineering Brief (v0.3)

Role: engineering voice. This is a design brief, not code. Everything below
stays inside the hard constraints: Python standard library only, zero cost,
no credentials in the repo, demo mode only, tests must actually pass.

## Where we are today

The `human` intent already exists in `intents.py` (patterns like "real
person", "talk to", "call me") and `replies.py` already answers it with
"Of course, I will connect you with a person from the team. They usually
reply within a few hours." That is a promise the system does not keep: the
message is recorded as a lead and a reply goes to the outbox, but nothing
changes, nobody is told, and the bot keeps auto-replying to the customer as
if nothing happened. v0.3 is about making that promise true. Nothing about
the existing intent detection needs inventing; the work is state, pause,
and surfacing.

The central dispatch point is `Studio.handle_message` in `app.py`. Every
incoming message flows through it: detect intent, handle booking/cancel or
build a reply, record the lead, append the reply to the outbox. Handoff
belongs in that one place, checked before intent dispatch, so it cannot be
bypassed by any message shape.

## How handoff should be detected

Two triggers, both rule-based, both explainable to a business owner.

**1. Explicit ask.** The existing `human` intent. A message whose only real
content is asking for a person — "can I talk to someone", "connect me to a
human", "manager se baat karni hai" — opens a handoff. Two things to know:

- Priority stays as it is. Today `human` sits below booking, price, hours,
  and location in the priority list, so "talk to me about prices" gets
  prices, not a handoff. That is the right behaviour: a customer asking
  about something specific should get the answer, and only a pure "I want
  a person" escalates. Do not promote `human` above the others.
- False positives are cheap and acceptable. The patterns will occasionally
  catch a message that was not really a handoff request ("someone told me
  about you"). The cost is one handoff the owner resolves in a single
  click, not a broken conversation. Widen the patterns slightly for the
  words owners actually hear — "manager", "owner", "speak to someone" —
  and stop there. Do not try to make the patterns clever.

**2. Repeated confusion.** The bot already has a `fallback` intent for
messages it cannot understand, and today it just repeats a generic "I can
help with prices, timings, booking..." reply. One fallback is normal (a
typo, an odd phrasing). Two fallbacks in a row from the same phone is the
signal the bot is stuck, and that should open a handoff automatically,
with the reason recorded as "couldn't help". The reply in that case must
say so plainly — "I couldn't quite get that, so let me connect you with a
person" — not pretend it understood.

Counting the streak costs no new storage: the lead record already keeps
each message with its intent (last 50), so the streak is the lead's recent
intents plus the current message. Two in a row, counting the current one,
is the rule. After a handoff opens, the streak resets — the customer is
already being handed over, and further confused messages must not open
duplicate handoffs.

One open handoff per phone, always. If a handoff is already open and the
customer asks for a human again, nothing new is created; the message is
just recorded. Duplicates would make the dashboard noisy and the owner
would stop trusting it.

What is deliberately not a trigger in v0.3: repeated booking failures (a
customer asking again and again for a taken slot). That needs a second
counter and its own wording, and it is a smaller problem than the two
triggers above. Defer it.

## How handoff state should be stored

Follow the pattern the repo already uses for bookings and leads: a small
store backed by one JSON file, `data/handoffs.json`, with the same rules.

- A handoff record holds: the customer's phone, their name if known, the
  reason (explicit ask, or repeated confusion), the message that triggered
  it, when it opened, when it was resolved, and its status (open or
  resolved). That is all. Message history is not duplicated here: the lead
  record already stores every message with a timestamp, so the thread
  during a handoff is the lead's messages from the handoff's opening time
  onward. One source of truth, no copies drifting apart.
- Load tolerantly, like the existing stores: a corrupt or missing file
  means an empty handoff list, never a crash. Save on every change, not in
  batches — a lost handoff is a lost customer. Keep the in-memory
  constructor that takes no path, exactly as `BookingStore` does, so tests
  can run without touching disk.
- Persistence across restarts matters more here than for leads. If the
  server restarts while the owner is mid-conversation with a customer, the
  handoff must still be open when it comes back up, or the bot will start
  auto-replying over the human. The JSON file gives that for free.
- Concurrency: the server is threaded and the existing stores do not lock.
  At a salon's volume (dozens of messages a day) the risk of two messages
  from the same customer landing in the same instant is negligible, and
  the worst case is a duplicate check that the one-open-handoff rule
  already guards against. Mirror the existing pattern rather than adding
  locking the rest of the codebase does not have.

## Pausing auto-replies, and resuming

This is the heart of the feature, and it must be total while it lasts.

- When a message arrives from a phone with an open handoff, the bot sends
  nothing. The message is still recorded in the lead history — the owner
  needs to see what the customer said while waiting — but nothing is
  appended to the outbox and no auto-reply is generated. The one exception
  is the confirmation sent at the moment the handoff opens: "I've connected
  you with a person from the team. They'll reply here shortly." That single
  message sets expectations; everything after it is silence from the bot.
- A handoff pauses everything, including booking. If the customer, while
  waiting, says "actually book me for tomorrow 11am", the bot will not book
  it. That is a real trade-off and it is the right one for v0.3: the human
  owns the conversation now, and a bot booking over the top of a human
  conversation would create double replies and double bookings. The owner
  can book it themselves, or resolve the handoff and let the customer ask
  again.
- Resuming is explicit and owner-driven. The owner marks the handoff
  resolved on the dashboard, and from the next message the bot replies
  normally again. Resolving sends no message to the customer: the owner is
  the one talking to them, and a bot message at that moment would be
  noise. If the customer writes again after the handoff is resolved, normal
  replies simply resume, silently.
- There is no timer that auto-resolves a handoff. A stuck handoff that
  stays open is visible on the dashboard, and that is the safer failure
  mode: a bot jumping back in hours later, after the owner already handled
  things by phone, would confuse the customer far more than a wait. The
  dashboard should instead mark handoffs open for a long time as urgent
  (say, over four hours), so a neglected one is hard to miss. Notification,
  below, is what prevents neglect — not a timer.

## Surfacing it, and notifying the owner

The dashboard is the source of truth. Notifications are best-effort
helpers on top of it, and a failed notification must never break message
handling.

**Dashboard (the primary surface, always on).** A "Handoffs" section at the
top of the existing dashboard, above bookings and leads, listing open
handoffs first: phone, name, reason, the triggering message, how long it
has been open, and a resolve button. Closed handoffs sit below, or are
hidden. Next to each open handoff, show the customer's messages since it
opened (read from the lead record). Add a `GET /handoffs` JSON endpoint
mirroring `/leads` and `/bookings`, so the data is reachable the same way.
Resolving must be a POST from a plain HTML form on the dashboard — a
button, no JavaScript — and it must validate that the handoff exists
before closing it. All customer text on the dashboard must be HTML-escaped
exactly as the leads and bookings tables already do; a crafted name or
message must not be able to run script in the owner's dashboard. One
honest note: the dashboard has no login today, same as `/leads` and
`/bookings`. It is only safe because the demo binds to localhost. If the
dashboard is ever exposed beyond the owner's machine, authentication
becomes a prerequisite, not a nice-to-have — but that is out of scope for
v0.3.

**Telegram (the fast notification, optional, off by default).** The
strongest free option, and the one most Indian small-business owners will
actually feel: the server sends the owner a Telegram message the moment a
handoff opens, using the Telegram Bot API over a plain HTTPS POST (the
standard library can do this with no new dependency). The owner creates
their own bot, supplies their own bot token and chat id as environment
variables at runtime, and nothing is stored in the repo — the exact same
pattern as `WHATSAPP_TOKEN` today. If the variables are not set, the
feature is simply off and handoffs queue on the dashboard. If the Telegram
call fails — no network, a bad token — the failure is swallowed, the
handoff is still recorded, and the dashboard still shows it. The message
itself should be short and useful: business name, customer name and phone,
the reason, and the triggering message, so the owner can decide without
opening the dashboard. The setup step (creating a bot and pasting a token)
is the owner's own credential, not ours, and it is a reasonable ask for a
pilot.

**What not to build for notification in v0.3.** A WhatsApp message to the
owner needs the live Meta sending path, which the repo deliberately has
not wired up yet; once live sending exists, owner notification over
WhatsApp becomes natural, but it is not a v0.3 job. Email would need SMTP
credentials, which is a worse version of the Telegram option. A generic
outbound webhook (POST a small JSON payload to a URL the owner supplies)
is nearly the same code as the Telegram call and is the obvious next
step — but it is a second notifier to test and document, so it waits for
the version after this one.

## Smallest shippable v0.3 vs what to defer

**v0.3 ships:**

- Explicit ask opens a handoff (existing `human` intent, patterns widened
  slightly), and two fallbacks in a row open one with reason "couldn't
  help". One open handoff per phone; duplicates impossible.
- The JSON handoff store (`data/handoffs.json`), same pattern as bookings:
  tolerant load, save on every change, in-memory mode for tests.
- Pause: while a handoff is open, messages are recorded and nothing is
  auto-replied, except the single confirmation at handoff creation.
  Resume: the owner resolves it on the dashboard, and the bot replies
  normally from the next message. No timer, no auto-resolve.
- Dashboard handoffs section (open first, with the waiting thread and a
  resolve button), a `GET /handoffs` endpoint, and a POST resolve action.
  Long-open handoffs shown as urgent.
- Optional Telegram notification, owner-supplied token and chat id via
  environment variables, off by default, best-effort and never blocking
  message handling.
- The confirmation reply the customer gets at handoff creation, worded
  honestly. Today's `human` reply promises "they usually reply within a
  few hours" — keep that promise, but only because v0.3 makes it true
  through notification plus the dashboard, not because the words say so.
- Tests that prove it: explicit ask opens a handoff; two fallbacks open
  one and one fallback does not; no outbox entries while a handoff is
  open; resolving resumes auto-replies; an open handoff survives a
  restart (reload from disk); the dashboard and endpoint show it; the
  Telegram call is tested with a stand-in, never a real network call.
  The demo script should show the flow end to end: a customer asks for a
  person, sends a follow-up that gets no reply, and the dashboard shows
  the open handoff.

**Deferred:**

- Booking-failure escalation (repeated taken slots) as a third trigger.
- Auto-resolve timers, and any auto-close of stale handoffs.
- The owner replying to the customer through the dashboard — that needs
  live sending, which is a bigger step than handoff.
- Generic outbound webhook and email notifications.
- WhatsApp notification to the owner (waits for the live Meta path).
- Per-business owner routing, which waits for multi-business support.
- Handoff analytics (counts, response times) — useful once real pilots
  exist, not before.
- Anything smarter than the fallback streak for detecting confusion. That
  would need a language model or much heavier rules, and the rule-based
  design is the point of this product.

## Risks and open questions for the product discussion

- The owner who never checks the dashboard and never configures Telegram
  will leave customers waiting in silence. The honest mitigation is the
  pilot conversation: handoff only works if the owner agrees to watch one
  of the two surfaces. The confirmation message should not over-promise
  while that agreement is untested.
- A customer who triggers a handoff by accident (a false positive on the
  human patterns) loses the bot until the owner resolves it. That is the
  cost of a total pause, and the resolve button is the release valve. If
  false positives turn out common in real pilots, the patterns need
  narrowing, not the pause logic.
- The dashboard has no authentication. Fine on localhost for the demo and
  the first pilot; a real deployment needs it before the dashboard is
  reachable from anywhere else.
