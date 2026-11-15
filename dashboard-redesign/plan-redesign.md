# WhatsApp Studio — Dashboard redesign + owner booking ping + outbox persistence (synthesised plan)

Sources: the three product briefs in `dashboard-redesign/` —
`owner.md`, `design.md`, `engineering.md`. Plus two gaps Aditya found
by asking questions (Nov 15 ~01:16 IST): the owner gets no ping when a
booking is made, and the reply history is in-memory only. This plan is
the synthesis; where the briefs disagree, the call below is the decision.
Implement this plan, not the individual briefs.

## Goal

Make the dashboard something a business owner would open every day and
show another shop owner — not a developer's data dump. The dashboard's
one job is the two-minute end-of-day check: who needs me, how did today
go, did anything go wrong. Plus two small fixes Aditya asked about: the
owner gets pinged when a booking is made, and the reply history survives
a restart.

## The central call: flip the order, phone first, owner's words

Today the page opens with "Recent replies" and buries the people waiting
for the owner third, in five plain bordered tables. The redesign leads
with one sentence — "Today: 12 people messaged, 3 booked, 1 waiting for
you" — then three big numbers, then people waiting for you first,
today's bookings second, replies third, customer list last. The thing
that needs a human goes first; the record goes last. One column, no
horizontal scrolling ever, cards not tables, thumb-sized tap targets,
WhatsApp green as the single accent, white background, system font in
three sizes only. No jargon anywhere: "Leads" becomes "People who
messaged", "Handoffs" becomes "People waiting for you", "Resolve"
becomes "I handled it", "Sending: LIVE/PAUSED/DEMO/DRY RUN" becomes
"Bot is ON" or "Bot is OFF". The Intents column, message IDs, and the
"Replies generated: 47" counter are gone.

## Scope (ship this)

### 1. Dashboard redesign — a rewrite of `dashboard_html()` in `app.py`
- Viewport meta tag (`width=device-width, initial-scale=1`), one inline
  `<style>` block (system font stack, 16px base so phones do not zoom on
  tap, centred max-width container, section headings, cards with rounded
  corners and soft borders, a coloured status pill for the sending state,
  large buttons). No JavaScript, no external assets, no separate
  stylesheet, no new routes, no new dependencies, no new env vars.
- "Today at a glance" strip at the top: one sentence (people messaged,
  bookings today, people waiting), then three big numbers (people
  waiting for you now, bookings today, messages the bot answered today).
  If nobody is waiting, say "all clear" clearly so the owner can close
  the page in two seconds.
- People waiting for you as cards (not a 7-column table): name big,
  phone as a tap-to-call `tel:` link (built from the digits-only phone,
  never raw text), what they said in their own words, what the bot
  already told them, how long they have been waiting, and a large
  "I handled it" button. Handoffs open over four hours get an urgent
  visual treatment (coloured edge or badge), not a text prefix.
- Today's bookings as a simple list in time order (name, service, time,
  price). The dashboard must still show "Bookings today", the service
  name, and the formatted time ("4:00 PM") — the tests pin those.
- Replies the bot sent as a compact list: failed sends first (loud,
  "did not reach the customer, call them", never a code), then the last
  few. Keep the exact "SEND FAILED" marker and the send error visible.
  Keep the exact labels the tests pin: "Sending: LIVE" + "Stop sending",
  "Sending: PAUSED" + "Resume sending".
- People who messaged as a capped compact list (latest ~50 leads).
  Resolved handoffs become a count ("3 handled today"), not a table.
- The stop/resume sending control stays big and at the top, with proof
  when hit ("the bot is stopped, it will not reply to anyone").
- Row caps so the page cannot balloon (latest ~50 leads, resolved
  handoffs already capped at 20, outbox already capped at the last 10).
- Keep every existing visible label the tests assert on, the JSON
  endpoints, the POST actions with their 303 redirects, Basic Auth
  gating, and HTML escaping of every customer-supplied string. The
  business name stays the page heading, escaped ("Glow &amp; Grace
  Salon").

### 2. Owner ping for a new booking (the gap Aditya found)
- When a booking is confirmed (`_handle_booking` succeeds), notify the
  owner through the existing notifier (same channel as handoffs —
  Telegram when configured), best-effort, never breaking the booking or
  the reply if the notifier fails. Message in plain words: who booked,
  what service, when, and the price. Do not ping for a failed/double
  booking or a cancellation — only a confirmed new booking. Keep it
  small: one notifier call, same pattern as the handoff notify.

### 3. Persist the outbox (the gap Aditya found)
- The outbox (reply history) is currently in-memory only and is wiped
  on restart. Persist it to a JSON file (`data/outbox.json`) the same
  way leads/bookings/handoffs are persisted: load on startup, save on
  every append. Cap what is kept (last ~100 entries) so the file cannot
  grow without limit. Do not change the outbox entry shape.
- Honest limit, document in the README: the data lives in JSON files on
  the server's disk, and on Render's free tier that disk is ephemeral,
  so a redeploy or a restart can still lose the files. A real database
  (or Render's persistent disk) is the proper fix and is a later
  version, not this one. Do not pretend otherwise.

## Tests (all must pass; the suite is 105 tests today, it must stay green)

- The full existing suite stays green, with no existing assertion
  weakened.
- New tests that prove: the viewport meta tag is present; the glance
  numbers appear and match the underlying data; an open handoff renders
  as a card with an "I handled it" button and a `tel:` link; the page
  contains "People waiting for you" and "Bookings today"; no unescaped
  customer text appears (a crafted name/message cannot run script);
  the page stays small (row caps hold); a confirmed booking triggers
  the notifier once (and a failed/double booking does not); the outbox
  survives a restart (write, reload, entries still there) and is capped.
- No test ever touches the network.

## Do NOT build in this pass

- Auto-refresh or live updates, charts, search/filter, the owner
  replying from the dashboard, any external asset (webfont, icon font,
  image, CSS framework), a settings page, a separate mobile app.
- A real database or persistent disk (document the ephemeral-disk limit
  honestly instead).
- The owner WhatsApp ping (v0.4b — needs a Meta template approval, a
  business-admin step, not code).

## Constraints (standing)

Python standard library only, zero cost, no credentials in the repo, demo
mode must keep working with no env vars set, fewer comments (only where
the why cannot be read from the code, senior engineer voice), no AI-slop.
Note the timezone lesson from v0.3: the VM clock may be IST or UTC
depending on the session — any time-dependent test must pin an explicit
time, never rely on the current hour. Update the README honestly
(including the ephemeral-disk limit and that the owner now gets pinged
for new bookings). Do not push to GitHub; leave the work committed
locally and report back.
