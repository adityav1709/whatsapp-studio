# Live sending — the customer and owner experience

Voice: customer and owner experience. What it feels like the day this
goes live, not how it works. WhatsApp Studio v0.3, demo business Glow &
Grace Salon, Gurgaon (open daily 10:00 AM – 8:00 PM).

## The change that matters

Until now nothing the bot does can hurt anyone. Every reply has gone
into an outbox on a laptop. No real customer has ever seen a word from
it. Live sending ends that. The first time the server sends a message
for real, a wrong price, a wrong slot, a duplicate, or a message to the
wrong person is no longer a test artifact — it is the salon telling a
real customer something untrue, or a stranger receiving someone else's
booking. Everything below is written with that in mind.

## What changes for the owner

Today the owner gets nothing when a handoff opens unless they watch the
dashboard or Telegram. The day live sending goes on, they start getting
real WhatsApp pings on their personal phone. That is the thing the
owner brief demanded: a handoff that does not arrive as a normal
WhatsApp message on the owner's phone does not exist.

What that ping must contain has not changed since the handoff
discussion: the customer's name and phone number (one tap to call),
their actual words, what the bot already told them, and what happened
with their booking. No links, no "open dashboard for details", and it
must look different from a normal customer message — "Customer needs
you" first — so it is not missed in a busy chat list. One new
requirement: the ping must come from the business's number, the one the
owner recognises. A handoff ping from an unknown number reads as spam
and will be ignored or blocked.

What can go wrong with the ping, now that it is real:

- **It arrives late.** A ping that lands ten minutes after the customer
  asked is a customer who has already left. The owner calls, nobody
  answers, and the ping starts to feel useless. Within a minute or it
  does not count.
- **It arrives at the wrong time.** A ping at 11 PM wakes the owner for
  a customer who can wait until morning. The owner brief was explicit:
  do not wake me after hours. The ping must respect working hours
  exactly like the customer reply does. One 2 AM ping is a
  mute-the-number event, and then the whole feature is dead.
- **It arrives for nothing.** A false-positive handoff — the bot
  misunderstood and opened a handoff by accident — now costs the owner
  a real interruption. Three of those and the owner switches it off and
  never switches it back on.
- **It arrives twice.** A duplicate ping makes the owner think the
  system is broken, or worse, call the same customer twice. A
  duplicated ping is almost as bad as a missed one.
- **It goes to the wrong number.** This is the serious one. The ping
  carries a customer's name and phone number. If the owner's number is
  misconfigured, a stranger receives a real customer's details. That is
  not an annoyance, it is a privacy breach. The owner's number must be
  verified before live sending is turned on: send a test ping, have the
  owner confirm they got it. No confirmation, no live mode.
- **It arrives for a customer who is already served.** The owner
  handled it by phone an hour ago, then the ping arrives. Stale pings
  teach the owner to ignore all pings.

## What changes for the customer

Today the customer gets nothing real. The day live sending goes on, a
bot starts replying to real customers on WhatsApp, from the business's
number, in the business's name. Every wrong reply is now the salon's
reputation, not a test log.

What must never go wrong:

- **The same reply twice.** This is the number one risk. Meta retries
  a webhook when the server is slow, and a server that is restarted or
  retried can process the same incoming message twice. A customer who
  gets the same message twice knows instantly something is broken, and
  a customer who gets the same booking confirmation twice will wonder
  if they are booked twice. The bot must never send the same reply
  twice for one incoming message, whatever Meta or the network does.
- **A reply to the wrong person.** A reply meant for one customer sent
  to another leaks one customer's booking and details to a stranger.
  Every send must go to exactly the phone the message came from, and
  nothing else. There is no acceptable version of this mistake.
- **A reply that crosses the human.** The bot replying over the owner
  while the owner is talking to the customer, or a bot reply landing
  after a handoff opened. The total pause from v0.3 has to hold in
  live mode too, and there is a new gap: the owner takes over by
  calling, and a phone call does not tell the server anything. If the
  owner resolved the handoff and then calls the customer, the bot has
  resumed and can reply while the owner is on the phone. That edge
  needs an answer before the first pilot, not after.
- **A promise the shop cannot keep.** A booking confirmation for a
  slot that was actually taken, a price that is out of date, a
  cancellation confirmation for a booking that is still live. In demo
  mode these were harmless. Live, the customer turns up and there is
  no slot, or pays a different price than the bot quoted. That is a
  lost customer and a bad review, and it is the bot's fault.
- **The bot replying to itself.** If the bot's own sent message, or
  the owner's reply, comes back through the webhook as an incoming
  message, the bot must not answer it. A bot that talks to itself in
  a loop will spam a customer until someone kills the server.
- **The bot answering a customer the owner already answered.** The
  owner replied by phone, then the bot also replies with different
  information. Two voices, two answers, and the customer trusts
  neither.
- **Silence where a reply was promised.** If the bot said "I'll pass
  this to the team" and then nothing happens, the customer waits and
  leaves. Live sending does not fix the handoff; it makes the promise
  louder. The handoff has to actually reach the owner, every time.

Two constraints from Meta's side that shape the experience:

- **The 24-hour window.** After a day with no message from the
  customer, the business can only send template messages, not free
  text. A bot that tries to reply to a stale conversation gets
  rejected and the customer hears nothing. The bot must not promise a
  reply it cannot send, and a rejection like that must surface to the
  owner instead of vanishing silently.
- **Message order.** A customer who sends three messages quickly must
  get replies in order, not interleaved. A reply to the first message
  arriving after a reply to the third reads as the business not
  listening.

## Can the owner set this up?

What Meta needs, in plain terms: a Meta Business account, a WhatsApp
Business app, the business's phone number registered to that app, a
long-lived access token, a phone number ID, and a webhook address that
Meta can reach over the public internet, plus a verify token. That is a
developer's afternoon, not an owner's errand.

The honest answer is no, a non-technical salon owner cannot do this
alone, and will not try. The owner brief said it plainly: no manual,
no dashboard, no new app, no settings page. Live-sending setup is
exactly the kind of thing they will not do. So the setup has to be
dead simple for the owner, which means one of two things:

- **Someone else does it for them.** For the first pilot, that is
  Aditya, on a call with the owner, once. The owner gives two numbers
  — the business WhatsApp number and their personal number — and
  approves the prompts Meta shows. They never touch the Meta
  dashboard. This is the only realistic v0.4 path.
- **Or the product does it for them.** That would mean a guided
  onboarding where the owner types two numbers and the system handles
  the Meta side. That needs Meta's embedded signup and a business
  verification flow, which is a much bigger build and needs Meta's
  approval. It is not a v0.4 job, and it should not be promised as
  one.

Three setup truths the product has to be honest about:

- **The business number is a real decision.** The bot sends from the
  business's WhatsApp number. If the owner already uses that number
  for personal WhatsApp, registering it with the Cloud API can
  conflict with the phone app — a number generally cannot live on
  both at once. The owner may need a second number for the business,
  or to move the business off their personal WhatsApp. That is an
  awkward conversation, and it has to happen before setup, not
  discovered halfway through.
- **The token can die.** A token can expire or be revoked, and when
  it does every send fails. The owner will not know. There has to be
  a visible live / not-live state somewhere the installer can see,
  and a way to put in a new token without touching code. Silent
  token death is silent business death.
- **The server has to be reachable.** The webhook address must be
  public and on HTTPS, which means the server cannot just run on the
  owner's laptop. It has to live somewhere that stays on. If the
  machine sleeps, the bot goes silent. The owner needs to understand
  the bot is a service that runs somewhere, not an app on their
  phone — and the installer needs to own that hosting, not the
  owner.

## When sending fails

A send can fail for ordinary reasons: no network, Meta is down, the
token is bad, Meta rejects the message. The experience of a failure is
what decides whether the product survives its first bad day.

- **The customer must not be left in silence.** A failed reply means
  a customer asked a question and got nothing. They will assume they
  are being ignored. The safest failure mode is escalation: a send
  that fails should behave like a handoff — the owner gets pinged
  that the bot could not reply, so a human answers. A customer
  stranded in silence is worse than a customer handed to a human
  late.
- **The owner must know sending is broken.** If every send is
  failing and nobody knows, the business loses customers all day.
  There has to be a visible sending-health state on the dashboard,
  and the first failure with an authentication error should be
  flagged loudly, not retried quietly on every message. A bad token
  failing a hundred times is one problem, not a hundred.
- **Retries must never duplicate.** A failed send can be retried a
  small number of times, but only when the failure is clear — no
  connection, no response at all. After a timeout, the message may
  already have gone through, and retrying sends it twice. The rule
  has to be: retry only what certainly did not send. And every send
  attempt has to be recorded with its result, so there is a trail
  and a duplicate can be spotted.
- **What must never silently break.** Three things. The handoff: if
  the owner's ping fails, the handoff must still be recorded and
  visible, exactly as v0.3 already does for Telegram. The booking: a
  booking that was recorded but whose confirmation never reached the
  customer must not be treated as unbooked — it has to be flagged
  for the owner to confirm by hand. And the record: every send
  attempt, its result, and its reason must be logged, because when a
  customer says "your bot never replied", the owner needs an answer
  that is not a guess.

## What to build first (v0.4, experience view)

1. The send path, with a delivery record: every send logged with its
   result (sent, failed, why). No send without a record.
2. The owner's WhatsApp ping on handoff — the thing v0.3 deferred —
   with the same content rules as the owner brief, and only inside
   working hours.
3. Duplicate protection: the same incoming message never produces
   two replies, whatever Meta or the network does.
4. A visible live / not-live state and a kill switch: one action
   that stops all sending instantly, back to recording only. The
   owner must be able to stop the bot in seconds when it misbehaves.
5. Failure behaviour: a failed send escalates to the owner, is
   logged, and the dashboard shows sending health.
6. Setup done by the installer, not the owner: the owner gives two
   numbers, the installer does the Meta side. A written setup
   checklist, and a test ping the owner confirms before live mode
   is switched on.

## What NOT to build (yet)

- Do not let the bot send any reply whose wording the owner has not
  seen. The pilot starts with the owner reading the exact messages
  the bot will send.
- Do not auto-retry sends in any way that can duplicate a message.
- Do not send outside the 24-hour window and drop the failure
  silently — handle it as a handoff.
- Do not expose the dashboard publicly without login. It matters
  more now, because the dashboard can resolve handoffs and its state
  affects real customers.
- Do not promise the owner a self-serve Meta onboarding. That is a
  later product, not this version.

## The blunt summary

Live sending is the first version that can actually harm a real
customer, and that changes what "done" means. A bug in demo mode was
a wrong line in a log. A bug in live mode is a customer who got the
wrong price, a duplicate message, a booking that does not exist, or a
stranger who received someone's phone number. The first pilot has to
be small and watched: one business, the installer watching the first
day's messages, a kill switch the owner can hit, and the owner's
number verified with a test ping before a single real reply goes out.
If the bot misbehaves on day one and there is no fast way to stop it,
the owner will not give it a day two.
