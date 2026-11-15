# WhatsApp Studio v0.3 — Human handoff (synthesised plan)

Sources: the three product briefs in this folder — `customer-experience.md`,
`business-owner.md`, `engineering.md`. This plan is the synthesis; where the
briefs disagree, the call below is the decision. Implement this plan, not the
individual briefs.

## Goal

Make the existing promise true. The bot already says "I will connect you with
a person" and then does nothing. v0.3 makes that promise real: the handoff is
recorded, the bot goes quiet, the owner is surfaced the request, and the bot
resumes only when the owner is done.

## Calls made where the briefs disagree

1. **Notification channel.** The owner brief wants a WhatsApp message on the
   owner's personal phone and nothing else; the engineering brief says that
   needs the live Meta send path, which is not wired up yet. The call: v0.3
   ships the optional Telegram notifier (owner-supplied env token/chat id,
   off by default, best-effort, failure never blocks) plus a pluggable
   notifier interface so WhatsApp can be added later. The WhatsApp message
   to the owner is the explicit next milestone (v0.4, needs the live Meta
   send path). Do not pretend v0.3 solves the owner's phone ping; say so
   plainly in the README.
2. **HUMAN priority.** The customer brief wants a human request to outrank
   booking/price/hours; the engineering brief wants "talk to me about
   prices" to still get prices. The call: tighten the HUMAN patterns so
   they only match person-specific phrases (talk to a person/human/someone,
   speak to someone, call me, manager, owner, real person, agent se baat,
   kisi se baat, and similar Hinglish person phrases). "Talk to" on its own
   must NOT match. Then promote HUMAN above BOOKING/PRICE/HOURS/LOCATION
   and below CANCEL, so "let me talk to a person about my booking" opens a
   handoff while "talk to me about prices" still gets prices. Tests must
   prove both.
3. **Auto hand-back.** The owner brief wants auto hand-back after 15–30 min
   of silence (the owner will forget); the engineering brief wants no timer
   (a bot jumping back in after the owner handled things by phone is worse).
   The call: no auto-resolve in v0.3. A stuck handoff stays open and is
   marked urgent on the dashboard after ~4h. Auto hand-back is a follow-up
   decision after the first pilot, not a v0.3 job.
4. **After hours.** If a handoff is requested outside working hours, record
   it (it is the morning list) but the confirmation must be honest: shop
   closed, opens at X, will hear back by Y, message taken. Never promise a
   quick reply after hours.

## v0.3 scope (ship this)

1. **Detection, two triggers only.**
   - Explicit ask: the tightened, person-specific HUMAN intent (see call 2).
   - Repeated confusion: two fallback intents in a row from the same phone
     (counted from the lead's stored message intents, no new storage),
     reason recorded as "couldn't help", reply says so plainly.
   - One open handoff per phone. If one is already open, the message is
     recorded and nothing new is created.
2. **Store.** `HandoffStore` backed by `data/handoffs.json`, same pattern as
   `BookingStore`: tolerant load (corrupt/missing file = empty list, never a
   crash), save on every change, in-memory constructor with no path for
   tests. Record: phone, name, reason, trigger message, opened_at,
   resolved_at, status (open/resolved). Message history stays in the lead
   record (single source of truth); the handoff does not duplicate it.
3. **Pause, total.** While a handoff is open, incoming messages from that
   phone are recorded in the lead history but nothing is appended to the
   outbox and no auto-reply is generated — including booking. The one
   exception is the single confirmation sent at the moment the handoff
   opens. Check this in `Studio.handle_message` before intent dispatch so
   no message shape can bypass it.
4. **Resume.** Owner marks the handoff resolved on the dashboard; from the
   next message the bot replies normally again. Resolving sends no message
   to the customer. No timer, no auto-resolve.
5. **Confirmation reply.** One honest message at handoff creation. During
   open hours: confirm the handoff happened, say who will reply, give a
   real wait, and confirm the chat is passed on so the customer never
   repeats themselves. After hours: honest closed message (see call 4).
   Never "meanwhile, can I help with prices?" after the customer asked for
   a person. Replace the current hollow HUMAN reply.
6. **Dashboard + endpoint.** A "Handoffs" section at the top of the
   dashboard, open handoffs first: phone, name, reason, triggering message,
   how long open, the customer's messages since it opened (from the lead
   record), and a resolve button. Long-open handoffs (>~4h) marked urgent.
   `GET /handoffs` JSON endpoint mirroring `/leads` and `/bookings`.
   Resolve is a POST from a plain HTML form (a button, no JavaScript) that
   validates the handoff exists before closing it. All customer text
   HTML-escaped exactly as the leads/bookings tables already do.
7. **Notification.** Optional Telegram notifier, owner-supplied
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` env vars, off by default.
   Plain HTTPS POST via the standard library, short useful message
   (business name, customer name and phone, reason, triggering message).
   Best-effort: any failure is swallowed, the handoff is still recorded
   and the dashboard still shows it. Behind a small notifier interface so
   a WhatsApp notifier can plug in later. No credentials in the repo.
8. **Widen HUMAN patterns slightly** for the words owners actually hear
   (manager, owner, speak to someone) while keeping them person-specific
   (see call 2). Stop there; do not try to make the patterns clever.

## Tests (all must pass; the suite is 43 tests today, it must stay green)

- Explicit ask opens a handoff.
- "talk to me about prices" still gets prices, not a handoff.
- Two fallbacks in a row open a handoff; one fallback does not.
- No outbox entries while a handoff is open (including a booking attempt).
- Resolving resumes auto-replies from the next message.
- An open handoff survives a restart (reload from disk).
- Dashboard and `GET /handoffs` show the open handoff; resolve works.
- Telegram notifier tested with a stand-in, never a real network call.
- After-hours confirmation wording is honest (no quick-reply promise).
- Demo script shows the flow end to end: customer asks for a person,
  a follow-up gets no reply, dashboard shows the open handoff.

## Do NOT build in v0.3

- WhatsApp notification to the owner (waits for the live Meta send path).
- Auto-resolve timers or auto-closing stale handoffs.
- The owner replying to the customer through the dashboard.
- Booking-failure escalation as a third trigger.
- Generic outbound webhook, email notifications, multi-staff routing,
  tags/analytics, anything smarter than the fallback streak.

## Constraints (standing)

Python standard library only, zero cost, no credentials in the repo, demo
mode. Tests must actually pass; never fake a passing run. Fewer comments:
only where the why or a trade-off cannot be read from the code, in a senior
engineer's human voice. No AI-slop. Update the README to describe the
handoff honestly, including that the owner's WhatsApp ping is the next
milestone. Do not push to GitHub; leave the work committed locally and
report back.
