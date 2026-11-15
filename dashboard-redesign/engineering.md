# Dashboard redesign — Engineering Brief

Role: engineering voice. This is a design brief, not code. Everything below
stays inside the hard constraints: Python standard library only, zero cost,
no credentials in the repo, the dashboard must keep working with no extra
setup, tests must actually pass, and the app runs on Render's free tier (it
sleeps when idle, so the first load can be slow). The dashboard today is
server-rendered HTML from `dashboard_html()` in `whatsapp_studio/app.py`,
with no JavaScript framework and no external assets.

## Where we are today

The dashboard is one method, `Studio.dashboard_html()` in `app.py`
(~200 lines of string formatting), served at `GET /` by the stdlib
`ThreadingHTTPServer` handler. What it renders, in order: a heading with
the business name, "Replies generated: N", a sending-state line (LIVE /
PAUSED / DRY RUN / DEMO, with a Stop/Resume sending button), then five
plain tables built with `<table border='1' cellpadding='6'>` — Recent
replies, Handoffs (open), Handoffs (resolved), Bookings today, Leads.
There is no CSS at all, no viewport meta tag, no colour, no spacing
system: default browser font, default table borders. Every piece of
customer-supplied text (names, messages, triggers) is HTML-escaped before
it is interpolated, which is the one thing in the current page that must
never regress. Actions are plain HTML forms that POST and get a 303
redirect back to `/`: resolving a handoff, stopping and resuming sending.
The JSON endpoints (`/leads`, `/bookings`, `/handoffs`, `/health`) and the
webhook are separate routes and are untouched by a dashboard redesign.
Basic Auth (`DASHBOARD_PASSWORD`) gates `/` and the data endpoints; the
webhook and `/health` stay open.

The data the page needs is already loaded by the method: open and resolved
handoffs, all leads, today's bookings, the last 10 outbox entries, and the
sending state. Nothing new has to be fetched or stored for a redesign.
That is the key feasibility fact: this is a presentation change, not a
data change.

## What can be done with plain HTML and inline CSS

A lot — far more than the current page suggests. All of it fits in one
`<style>` block inside the same HTML response, which is the only asset
model available (see below for why there is no separate stylesheet).

- A clean system font stack, a comfortable base size, a centred container
  with a max width, generous spacing, and section headings that actually
  read as headings. This alone moves the page from "default browser
  output" to "a product".
- A "today at a glance" strip at the top: three or four big numbers
  computed from data the method already has — open handoffs needing
  attention, today's bookings, new leads, replies sent. This is the
  single biggest perceived improvement: the owner sees the state of the
  shop in one glance instead of scrolling five tables to work out whether
  anything needs them.
- The sending state as a coloured pill (green for LIVE, amber for PAUSED,
  grey for DEMO / DRY RUN) that is visible without reading a paragraph,
  with the Stop/Resume button next to it, big enough to hit on a phone.
  The kill switch is the owner's off switch; the business-owner brief was
  explicit that stopping must be one tap, and the redesign must keep it
  that prominent, not bury it under the new styling.
- Open handoffs as cards instead of a seven-column table: name, phone as
  a tap-to-call `tel:` link, reason, trigger message, how long it has
  been open, the customer's messages since it opened, and a large Resolve
  button. The existing "URGENT —" marker for handoffs open over four
  hours becomes a visual treatment on the card (a coloured edge or badge)
  rather than a text prefix. Cards stack vertically, so they read
  naturally on a phone.
- Bookings today as a simple list (time, service, name, price) instead of
  a table. Leads as a compact list or a table wrapped in a horizontally
  scrolling container. Recent replies as a compact list with the send
  status as a small label, keeping the "SEND FAILED" marker loud.
- The phone number as a `tel:` link is safe to build because the number
  comes from Meta's `wa_id` / the lead record and is digits-only; it must
  never be built from a name or from message text.

What cannot be done without a bigger change: real-time updates (that
needs JavaScript polling or server-sent events — see the defer list),
charts (no chart library is available and crude CSS bars are decoration,
not what the owner needs), a search box or filters (needs JavaScript or
extra query parameters and server-side filtering), and any external font
or image (see below). None of those is needed for the page to look and
feel like a real product.

## Why there is no separate stylesheet, no JavaScript, and no external assets

Three reasons, all structural. First, the server has no static-file
route: every asset would need a new endpoint, and every new endpoint has
to be secured (Basic Auth), tested, and kept in sync with the page. An
inline `<style>` block in the single HTML response needs none of that.
Second, the free tier sleeps when idle, so the first load is already slow
(~50 seconds is Render waking the instance, not the page). A second
request for a stylesheet would add another round trip on top of that, and
an external font or image would add more — each one a chance for the page
to arrive half-dressed on a slow connection. One response, everything in
it, is the fastest page this stack can serve. Third, zero JavaScript is a
feature here, not a limitation: the page works on any phone and any old
browser, there is no script that can fail and leave the owner staring at
a blank page, and the existing tests fetch `/` and assert on the returned
strings, which keeps working exactly as it does today. A tiny bit of
inline vanilla JavaScript is technically possible (the server just serves
a string), but it buys nothing the redesign needs and adds a failure mode,
so the recommendation is to keep the page at zero JavaScript.

The practical consequence for the design: the whole redesign lives in
`dashboard_html()`. It is a rewrite of one method (plus a CSS string),
roughly 250–350 lines replacing the current ~200. No new routes, no new
dependencies, no new environment variables, no change to the stores,
intents, replies, security, sending, or the handler routing. That small
blast radius is what makes this shippable in one pass.

## Fast and light on the free tier

The wake-up delay is Render's, not the page's, and no dashboard change
can fix it — the honest thing is to keep the page itself as light as
possible once the instance is awake. That means: one response, inline CSS
only, no images, no webfonts, no extra requests. The current page is a
few kilobytes of HTML; a styled version with a few hundred lines of CSS
is still well under 50 KB, which is trivial. The one real size risk is
unbounded rows: the method currently renders every lead and every
resolved handoff. At pilot volume that is fine, but a busy shop could
make the page grow without limit, so the redesign should cap what it
renders — latest ~50 leads, resolved handoffs already capped at 20, the
outbox already capped at the last 10 — and say so in the page if it is
truncating. The method already iterates the stores in memory, so there is
no extra query cost; the only cost is the size of the response, and the
caps keep it bounded. Do not add a per-request database call, a
recomputation, or anything that touches the network: the page must render
from the in-memory stores it already has.

## Working on a phone

The owner reads this between customers, on their phone, one-handed. The
current page fails that test in a specific way: the handoffs table has
seven columns and the leads table five, so on a ~360 px wide screen the
page overflows horizontally and the owner has to scroll sideways to find
the Resolve button. The redesign must fix exactly that.

- A viewport meta tag (`width=device-width, initial-scale=1`) in the head
  — the current page does not have one, so phones render it zoomed out.
  Without this, no amount of CSS helps.
- A single-column layout under a width breakpoint, with cards stacking
  vertically. The tables either become cards (handoffs, bookings) or sit
  inside a container with horizontal scrolling (leads), so the page
  itself never scrolls sideways.
- A base font size of 16 px, so mobile browsers do not zoom in when the
  owner taps anything, and touch targets at least ~44 px tall — the
  Resolve button and the Stop/Resume sending button especially. No
  hover-dependent behaviour anywhere: nothing on the page may require a
  mouse to reveal or to work.
- Short text, plain words. The owner scans, they do not read paragraphs.
  Keep the existing labels and statuses in plain language.

## Smallest shippable redesign

In order, each step is independently shippable and the first three carry
most of the visible improvement.

1. Viewport meta tag plus one inline `<style>` block: system font stack,
   16 px base, centred max-width container, section headings, card
   styling with rounded corners and soft borders, a coloured status pill
   for the sending state, and large buttons. The page keeps exactly the
   same sections and the same data; only the presentation changes.
2. A "today at a glance" strip at the top with the four numbers (open
   handoffs, today's bookings, new leads, replies sent) and the sending
   pill. This is the first thing the owner sees and the thing that makes
   the page feel like a product rather than a log dump.
3. Open handoffs as cards with a tap-to-call phone link, the waiting
   thread, the urgent treatment for handoffs open over four hours, and a
   large Resolve button. This is the section the owner acts on, so it
   goes first and reads first on a phone.
4. Bookings today as a list, recent replies as a compact list with the
   send status kept loud (especially "SEND FAILED"), leads as a capped
   compact list. Resolved handoffs stay a short, quiet list below.
5. Row caps (latest ~50 leads) so the page cannot balloon, and keep every
   existing visible label the tests assert on.

What must not change in this pass: the routes, the form actions and their
303 redirects, the auth gating, the JSON endpoints, the webhook and its
signature check, the escaping of all customer-supplied text, and the
demo script and `run.py` startup behaviour.

## What to defer

- Auto-refresh or live updates. Tempting, and the wrong call for this
  stack: it needs JavaScript polling or a long-lived server-sent-events
  connection, which complicates the threaded server, holds a connection
  open on a free tier that sleeps anyway, and risks breaking the simple
  request/response model that makes the current page robust. The owner
  checks the page between customers; a manual refresh is fine. Revisit
  only if a pilot owner actually asks for it.
- Charts and analytics (messages per day, response times). No chart
  library exists in this stack, and the numbers that matter to the owner
  are already in the glance strip. This is a later version's job, once
  real pilots exist.
- Search, filter, and sort on the dashboard. Needs JavaScript or extra
  query parameters and server-side filtering; the capped lists are enough
  at pilot volume.
- The owner replying to a customer from the dashboard. That is a real
  feature (a compose box plus live sending), not a redesign, and it
  belongs in the live-send track, not here.
- Any external asset: webfonts, icon fonts, images, a CSS framework.
  Each one needs a new route or an outside dependency, slows the first
  load on a sleeping free tier, and buys nothing the inline CSS cannot
  already do. System fonts and plain CSS shapes are enough.
- A separate mobile app or installable web app. The page is a web page;
  "add to home screen" from the phone's browser already works.

## What must not break

The tests that pin the dashboard today, and the behaviour behind them:

- `GET /` returns 200 and contains the escaped business name
  ("Glow &amp; Grace Salon"). Keep the business name as the page heading
  and keep escaping it.
- The dashboard contains "Bookings today", the service name, and the
  formatted time ("4:00 PM"). Keep those exact labels.
- The dashboard contains "Handoffs", the customer's phone, the trigger
  text, the follow-up message, and "Resolve". Keep those exact strings
  visible on the page.
- The dashboard contains "Sending: LIVE" and "Stop sending" when live,
  "SEND FAILED" plus the send error when a send fails, and "Sending:
  PAUSED" with "Resume sending" after the kill switch is hit. Keep those
  exact labels; the redesign may restyle them but must not rename them,
  because the tests and the owner's mental model both depend on them.
- Basic Auth gating: with no password the page is open; with a password
  set, `/` and the data endpoints need it and return 401 without it,
  while `/webhook` and `/health` stay open. The redesign touches none of
  that, but a test run must prove it still holds.
- HTML escaping of every customer-supplied string (names, phones in
  display text, messages, triggers, booking names). Any new rendering
  path — cards, lists, the glance strip — must escape the same way, and
  a crafted name or message must still not be able to run script in the
  owner's dashboard. The `tel:` link must be built from the digits-only
  phone, never from raw text.
- The JSON endpoints, the webhook (including signature verification and
  wamid dedup), the POST actions with their 303 redirects, the demo
  script, and `run.py` startup output all stay as they are. The full
  suite (105 tests today) must stay green, and the redesign should add
  tests that assert the new structure — the viewport meta tag is present,
  the glance numbers appear and match the underlying data, the open
  handoff renders as a card with a Resolve button and a `tel:` link, no
  unescaped customer text appears, and the page stays small — without
  weakening any existing assertion.

## The blunt summary

The dashboard does not need a new stack, a framework, or a single new
route. It needs a viewport tag, one inline style block, a glance strip,
and its tables turned into cards a thumb can use. That is a rewrite of
one method in `app.py`, it keeps every endpoint, every form, every label
the tests pin, and every escaping rule intact, and it is the whole job.
Anything that needs JavaScript, a second request, an outside asset, or a
new route is a different version, not this one.
