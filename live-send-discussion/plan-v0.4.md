# WhatsApp Studio v0.4 — Live sending (synthesised plan)

Sources: the three product briefs in `live-send-discussion/` —
`customer-owner-experience.md`, `business-owner.md`, `engineering.md`.
This plan is the synthesis; where the briefs disagree, the call below is
the decision. Implement this plan, not the individual briefs.

## Goal

Make the bot able to send messages for real, safely. Until now every reply
went into an outbox on a laptop and no real customer ever saw a word. Live
sending ends that. This version is the first one that can actually harm a
real customer, so the safety rails are the feature, not an afterthought.

## The central call: customer replies first, owner ping second

The customer and owner briefs both want the owner's WhatsApp ping first —
it is the thing v0.3 deferred, and the owner brief says a WhatsApp message
on the owner's phone is the only notification that works. The engineering
brief says the owner ping cannot be reliable yet, and it is right: the ping
is business-initiated and will almost always fall outside Meta's 24-hour
window, so a reliable WhatsApp ping needs a template message (Meta
approval, a business-admin step, not code). A ping that fails on the window
recreates the exact v0.3 trust problem — the owner thinks handoff works
when it does not.

So the order is: **v0.4a is customer replies live** (always inside the
24-hour window, plain text works, no template approval). **v0.4b is the
owner ping on WhatsApp** once a template is approved — or it stays on
Telegram (v0.3) if the template is not worth it. Telegram already covers
the owner's fast notification meanwhile, so the owner is not left blind.

Be honest about this in the README: the owner WhatsApp ping is NOT in
v0.4a, and why.

## v0.4a scope (ship this)

1. **Sender.** A new module following the `notify.py` pattern: a small
   injectable sender with a `send(to, body)` method, stdlib `urllib` POST
   to the pinned Graph API version (`https://graph.facebook.com/v21.0/
   {PHONE_NUMBER_ID}/messages`, Bearer token, JSON body), returning Meta's
   message id (`wamid`). Pin the API version — never "latest".
   `sender_from_env()` builds a real sender only when BOTH
   `WHATSAPP_TOKEN` and `PHONE_NUMBER_ID` are set; otherwise it returns
   nothing and the server stays in demo mode. `Studio.live` becomes "a
   sender exists" (today it only checks the token, which is a bug: a token
   without a phone number id cannot send). The token is never logged,
   never returned in any response, never stored in the repo.
2. **Outbox is the audit trail.** In `handle_message`, the reply is
   generated and appended to the outbox FIRST, and only then, if live,
   is the send attempted. The outbox entry records the outcome: sent or
   failed, the `wamid` when it went out, the error when it did not.
   Plain text only. Meta caps a text message at 4096 characters; refuse
   or truncate an over-long body and record that, rather than letting
   Meta reject it silently.
3. **Duplicate protection (the top risk).** Meta redelivers a webhook
   when it does not get a 200 fast. Dedup on Meta's message id
   (`wamid`, `msg["id"]`): record it in the lead's message record (it
   survives restarts via `leads.json`) and skip any message whose
   `wamid` is already there. The check happens synchronously, before
   `handle_message` does any work. If a payload has no id, process it
   normally. One send attempt only, never a blind retry — a retry after
   a timeout can double-send, because the first POST may have succeeded
   and only the response was lost.
4. **Wrong-number guards.** The `to` comes only from the webhook's
   `from` (`wa_id`) for a customer reply — never from message text,
   never constructed. Validate digits-only. Never send to the
   business's own number. Keep the customer-reply and owner-ping call
   sites separate so they cannot be mixed up (the owner ping is v0.4b;
   do not build it here).
5. **Best-effort send, never breaking handling.** Business state (lead,
   booking, handoff) is committed BEFORE the send is attempted. The send
   is wrapped so it can never raise into `handle_message` or the webhook
   handler. The webhook must return its 200 fast: do the send on a
   daemon thread, so Meta never waits on a slow network call. The thread
   updates the outbox entry with the outcome. If the thread itself fails,
   the entry stays marked unsent — visible, not lost. A failed send is
   recorded, not retried blindly.
6. **Demo vs live, and dry-run.** Live only when both env vars are set.
   Otherwise demo mode: outbox only, exactly as today. Startup prints
   which mode the server is in. `/health` reports `live: true/false`
   (no token in the response). A dry-run mode, `WHATSAPP_DRY_RUN=1`,
   forces record-only even when the token is set, so a pilot can watch
   exactly what would be sent before any message leaves.
7. **Kill switch and visible state.** The dashboard shows a clear
   live / not-live state. A kill switch (a POST endpoint plus a
   dashboard button) stops all sending instantly — sends go to the
   outbox only until it is switched back on. The owner must be able to
   stop the bot in seconds when it misbehaves.
8. **Failure behaviour.** A failed send is recorded in the outbox and
   shown on the dashboard as a send-failed marker. It does not crash
   the server and does not silently pretend it sent. (Escalating a
   failed send to the owner as a handoff is a good idea but is a v0.4b
   judgement call — do not build it here; record and surface it.)

## Tests (all must pass; the suite is 60 tests today, it must stay green)

- The fake sender receives the reply in live mode.
- Demo mode (no env vars) never calls the sender.
- A raising sender does not break `handle_message` or the webhook 200.
- A repeated `wamid` is processed once (dedup), and the duplicate
  produces no second outbox entry and no second send.
- The outbox records sent and failed (with `wamid` / error).
- Dry-run mode records but does not call the sender.
- The kill switch stops sending (outbox only) and re-enabling resumes.
- The sender refuses / truncates an over-long body and records it.
- A wrong-number guard: a send is never attempted to the business's
  own number or a non-digit recipient.
- The demo script shows the flow with the fake sender so the whole
  loop is visible without a token.
- No test ever touches the network.

## Do NOT build in v0.4a

- The owner ping on WhatsApp (v0.4b — needs a template message and
  Meta approval, a business-admin step, not code).
- Template messages, media messages, delivery/read tracking.
- Auto-retry of failed sends.
- Webhook signature verification and dashboard auth — these are
  prerequisites that must land BEFORE a public pilot (the webhook is
  reachable from the internet), but they are the next build, not v0.4a.
  Say so plainly in the README.
- Multi-business, LLM replies, onboarding flows.

## Constraints (standing)

Python standard library only, zero cost, no credentials in the repo, demo
mode must keep working with no token. Tests must actually pass; never fake
a passing run. Fewer comments: only where the why or a trade-off cannot be
read from the code, in a senior engineer's human voice. No AI-slop. Update
the README honestly, including that the owner WhatsApp ping is v0.4b
(needs a template) and that signature verification + dashboard auth are
prerequisites before a public pilot. Do not push to GitHub; leave the work
committed locally and report back.
