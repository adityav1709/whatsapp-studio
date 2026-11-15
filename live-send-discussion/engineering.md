# Live sending — Engineering Brief (v0.4)

Role: engineering voice. This is a design brief, not code. Everything below
stays inside the hard constraints: Python standard library only, zero cost,
no credentials in the repo, demo mode must keep working when no token is set,
tests must actually pass.

## Where we are today

Replies are generated and recorded in the `outbox`, never sent. `Studio.live`
exists (`bool(os.environ.get("WHATSAPP_TOKEN"))`) but is never used to send
anything — it is a dead flag. The README says sending needs `WHATSAPP_TOKEN`
and `PHONE_NUMBER_ID`, and neither is wired up.

The notifier in `notify.py` is the model to follow: a small interface, a
stdlib `urllib` POST, best-effort with failure swallowed, credentials from
the environment, off by default, nothing secret in the repo. The sender
should be built the same way.

The webhook path matters for this feature. `handle_webhook_payload` iterates
entries, changes and messages, skips anything that is not text, and calls
`handle_message` per message, then the HTTP handler returns 200. Meta
retries delivery if it does not get a 200 quickly — that retry behaviour is
the source of the double-send problem below. The server is a
`ThreadingHTTPServer` with no locks on the stores. `BusinessProfile` has a
`phone` (the business number) and no owner phone yet.

## The send path

A new sender module, same shape as `notify.py`: an object with a
`send(to, body)` method, injectable into `Studio` like the notifier. The
real implementation does a stdlib `urllib` POST to
`https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages` with a Bearer
token and a JSON body, and returns Meta's message id (`wamid`). Pin the API
version (v21.0 or whatever is current when it is implemented) — never use
"latest", so a version change is a deliberate decision, not a surprise.
Short timeout, 10 seconds like the Telegram notifier.

`sender_from_env()` builds a real sender only when BOTH `WHATSAPP_TOKEN`
and `PHONE_NUMBER_ID` are set; otherwise it returns nothing and the server
stays in demo mode. `Studio.live` becomes "a sender exists". Today `live`
only checks the token, and that is a bug for this feature: a token without
a phone number id cannot send anything.

In `handle_message`, the reply is generated and appended to the outbox
FIRST — the outbox is the audit trail — and only then, if live, is the send
attempted. The outbox entry records the outcome: sent or failed, the `wamid`
when it went out, the error when it did not. Plain text only for v1: the
replies are already plain text by design, which is exactly why no template
approval is needed for them. Meta caps a text message at 4096 characters;
the sender should refuse or truncate an over-long body and record that,
rather than letting Meta reject it silently.

The token is never logged, never returned in any response, never stored in
the repo. Same rule as the Telegram token.

## Demo vs live

Live only when both env vars are set. Otherwise demo mode: outbox only,
exactly as today. Demo mode must keep working with no token — that is the
default, and it is the test mode.

Startup should print which mode the server is in, so the owner knows. `/health`
should report `live: true/false` (no token in the response).

A missing or wrong token means the send fails (401/403 from Meta). The
server must keep running, keep recording to the outbox, and make the failure
visible — the outbox entry says failed, and the dashboard shows a
send-failed marker. Never crash, never silently pretend it sent. A wrong
token is an owner setup problem, not a server crash.

A dry-run mode, `WHATSAPP_DRY_RUN=1`, forces record-only even when the
token is set, so a pilot can watch exactly what would be sent before any
message leaves. Demo mode (no token) is already a dry run; this flag is
for when the token IS set.

## The three classic failures

**1. Double sends (webhook redelivery).** Meta redelivers if it does not
get a 200 fast. Today a redelivery just duplicates an outbox entry, which
is harmless in demo; live, it sends the same reply twice. Fix it with
dedup on Meta's message id (`wamid`, `msg["id"]` — `handle_webhook_payload`
currently ignores it). Record the `wamid` in the lead's message record and
skip any message whose `wamid` is already there. That reuses the existing
persistence (`leads.json` survives restarts), so no new storage is needed.
The dedup check must happen synchronously, before `handle_message` does
any work. If a payload has no id (it should not happen from real Meta),
process it normally. And: one send attempt only, no automatic retry. A
retry after a timeout is the other classic double-send — the first POST may
have succeeded and only the response was lost. A failed send is recorded,
not retried blindly.

**2. Wrong number.** The `to` must come only from the webhook's `from`
(`wa_id`) for a customer reply, or from the profile's owner phone for an
owner ping — never from message text, never constructed. Validate
digits-only; refuse anything that does not look like a phone number. Never
send to the business's own number. Keep the two call sites (customer
reply, owner ping) separate so they cannot be mixed up.

**3. Failed send breaking handling.** The send must be best-effort like
the notifier: wrapped so it can never raise into `handle_message` or the
webhook handler. Business state (lead, booking, handoff) is committed
BEFORE the send is attempted, so a failed send never loses the state. And
the webhook must return its 200 fast: do the send on a daemon thread, so
Meta never waits on a slow network call. The thread updates the outbox
entry with the outcome. If the thread itself fails, the entry stays marked
unsent — visible, not lost.

## Rate limits and the free tier

Meta's free tier is 1,000 free service conversations a month per business.
A salon does a few dozen conversations a day at most, realistically a
handful — far under 1,000, so cost stays zero. The product must not create
conversations on its own: every send is a reply to a real customer message
or a real handoff, so the count tracks real demand.

Per-number throughput limits are orders of magnitude above a single shop's
traffic (a salon bursts at a few messages a minute). The binding constraint
is the 1,000 free conversations, not throughput. No rate-limit handling is
needed for v1 beyond a short timeout and not retrying blindly.

The 24-hour window is the real limit that matters. Meta only allows
free-form text within 24 hours of the customer's last message; outside
that, you need a template message (Meta approval). Customer replies are
always inside the window — they answer a message that just arrived — so
plain text works. A business-initiated message, like the owner ping, often
falls outside it. That is what drives the ordering decision below.

## Testing without real sends

Inject a fake sender that records calls and never touches the network —
the same pattern as `FakeNotifier` in the handoff tests. All live-send
tests use the stand-in; no test ever hits the network.

Tests that prove it: the fake sender receives the reply in live mode;
demo mode (no env vars) never calls the sender; a raising sender does not
break `handle_message` or the webhook 200; a repeated `wamid` is processed
once; the outbox records sent and failed; dry-run mode records but does
not call Meta. The demo script should show the flow with the fake sender
so the whole loop is visible without a token.

## Smallest shippable version — the order, and why

Recommendation: **customer replies first, owner ping second.**

The naive "smallest first" instinct says owner ping: one message type, one
recipient, smallest blast radius, and it proves the path on a friendly
recipient. It is right about blast radius and wrong about reliability. The
owner ping is business-initiated and will often fall outside the 24-hour
window, so a reliable WhatsApp ping needs a template message — Meta
approval, a business-admin step, not code. A ping that fails on the window
recreates the exact v0.3 trust problem: the owner thinks handoff works
when it does not. A customer reply that fails is visible immediately (the
customer writes again) and the failure is recorded in the outbox.

Customer replies are always inside the 24-hour window, so plain text works
with no template approval. That is the core product going live. And the
Telegram ping from v0.3 already covers the owner's fast notification, so
the owner is not left blind while customer replies go live.

So: v0.4a is customer replies live (sender, dedup, best-effort, outbox
audit, dry-run). v0.4b is the owner ping on WhatsApp once a template is
approved — or it stays on Telegram if the template is not worth it.

**v0.4a ships:** the sender module and `sender_from_env()`; live requires
both env vars; outbox sent/failed with `wamid`; `wamid` dedup via the lead
record; daemon-thread send so the webhook stays fast; dry-run flag;
startup mode line and `/health` live flag; tests with a fake sender; demo
with the fake sender; README updated honestly.

**Deliberately not in v0.4a:** the owner ping on WhatsApp (waits for a
template); template messages themselves; media messages; delivery and
read tracking; auto-retry of failed sends; webhook signature verification
(see below).

## Before any public live deployment (prerequisites, not features)

Webhook signature verification (Meta's `X-Hub-Signature-256`): today the
webhook only checks `verify_token` on GET. Anyone who finds the POST URL
can inject fake messages. Before the webhook is reachable from the
internet, verify the signature with the app secret (env var, same pattern
as the token). This is a security prerequisite, not a v0.4 feature, but it
must land before a public pilot.

Dashboard auth: the dashboard has no login (already flagged in v0.3). Fine
on localhost; a prerequisite before it is reachable from anywhere else.

The token and phone number id come from the environment at runtime, never
from the repo or the profile JSON. The profile JSON is for business
details, not secrets.

## Risks and open questions

- A failed send the owner does not notice: the outbox plus a dashboard
  marker is the visibility. If the token is wrong, every send fails and the
  owner sees it on the dashboard — but only if they look. For a pilot, the
  honest mitigation is the dry-run step first (watch what would be sent),
  then go live.
- Dedup after a restart: the `wamid` lives in the lead record (`leads.json`),
  so it survives restarts. If `leads.json` is lost or corrupt, dedup resets
  — the failure mode is a possible duplicate reply, not lost data.
  Acceptable for v1.
- The daemon thread and the outbox: the thread updates an entry after the
  webhook already returned. The dashboard might briefly show "sending"
  before it flips to sent or failed. Acceptable, as long as the entry
  carries a clear status so the owner is not confused.
- The owner ping and the 24-hour window: even after v0.4a, the ping needs a
  template to be reliable. That template is a business-admin step (Meta
  approval), and the pilot conversation should settle whether it is worth
  it or whether Telegram stays the owner channel.
