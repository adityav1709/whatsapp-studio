# Dashboard redesign — the design brief

Voice: design and user experience. What the dashboard should look, feel,
and read like. WhatsApp Studio v1.0 is live; the bot works, the bookings
work, the handoff works. The dashboard does not. It is five plain bordered
tables stacked on top of each other, no styling, no layout, no thought
about who reads it. Aditya's feedback is blunt and correct: a business
owner would not want this. This brief is about fixing that, and only that.

## The owner reading it

A salon or clinic owner in India, on their feet all day, checking their
phone between customers. They are not technical. They will not read a
manual, study a table, or learn what a column means. They open the
dashboard for one of two reasons: something needs them right now, or the
day is over and they want to know what happened. The owner brief said it
plainly: the dashboard's one real job is end-of-day review, not live
monitoring. So the dashboard has to answer two questions and nothing else:
"Does anyone need me right now?" and "How did today go?" If it cannot
answer those in five seconds on a phone, it is a developer demo, not a
product.

## What it should show first

The top of the screen is one line that tells the owner the shop is fine,
or it is not. Something like: "Today: 12 people messaged, 3 booked, 1
waiting for you." One sentence, the whole day. If the number waiting is
zero, that sentence should say so and the owner can close the page and get
back to work. That is the whole point of the first screen: peace of mind
in five seconds.

Under that, three big numbers, side by side on a laptop and stacked on a
phone: people waiting for you now, bookings today, messages the bot
answered today. Big numbers, plain labels, nothing else. A number with a
label is the fastest thing a busy person can read.

Then, in this order:

1. **People waiting for you.** This is the first list and the only urgent
   one. Each person is a card, not a table row: their name, their phone
   number as a tap-to-call link, what they said in their own words, what
   the bot already told them, and how long they have been waiting. If
   someone has waited more than a few hours, that card should look
   urgent — a warm colour, a clear "waiting 3 hours" — because a person
   waiting three hours is a customer about to leave. One card, one tap to
   call, one tap to say "I handled it". Nothing else on the card.
2. **Today's bookings.** The day at a glance, in time order: name,
   service, time, price. A simple list, not a table. The owner should be
   able to glance at it between customers and know who is coming next.
3. **Replies the bot sent.** Only the ones that matter: failed sends
   first, so a broken message is never missed, then the last few. The
   owner does not need to read every reply the bot ever sent. They need
   to know if one went wrong.
4. **People who messaged.** The full customer list, last. It is a record,
   not something the owner checks during the day.

The current order is backwards: replies first, handoffs third, buried
under a table. Flip it. The thing that needs a human goes first. The thing
that is only a record goes last.

## How it should be laid out on a phone

The owner is on a phone. Design for the phone first, then let the laptop
be the wide version of the same page. That means:

- One column. Nothing side by side on a phone. No wide tables, no
  horizontal scrolling, ever. A table that needs sideways scrolling on a
  phone is a table nobody reads.
- Every list becomes stacked cards on a phone. A card is one person or
  one booking, with the important thing big and the detail small.
- Big tap targets. The call link and the "I handled it" button must be
  thumb-sized, not tiny text links. The owner is tapping between
  customers, often one-handed.
- The stop button must be unmissable. The owner brief demands a way to
  stop the bot in one second, with proof it stopped. Right now the stop
  control is a small button buried in a paragraph halfway down the page.
  It should be a big, clear control at the top: "Bot is ON" with a large
  "Stop the bot" button next to it, and when it is off it should say so
  loudly — "Bot is OFF — replies are only being recorded" — so the owner
  never has to wonder whether it actually stopped.
- The page must load fast and work on a slow connection. No images, no
  web fonts, no JavaScript framework. That is not a limitation to work
  around; it is the right design for a shop owner's phone.

## How it should look

Clean, modern, trustworthy. It should feel like a real product, not a
page a developer threw together. In plain terms:

- White background, one accent colour, lots of empty space. The accent
  should be WhatsApp green — it is the colour of the app the business
  already lives in, so it reads as familiar and safe, not as a new brand
  to learn. Green means everything is fine. A warm red or amber means
  something needs attention. Never use red for decoration, only for
  "this needs you".
- Cards, not grid lines. Soft corners, a light border, a little shadow
  if it costs nothing. The current black grid lines make every section
  look like a spreadsheet, and a spreadsheet is the opposite of what an
  owner wants to open.
- One typeface, the phone's own system font, in three sizes only: big
  for the numbers and names, normal for the message text, small for the
  detail like "waiting 2 hours". No bold everywhere, no tiny grey text
  that cannot be read in sunlight.
- The business name at the top, large, with one short line under it that
  says what this page is: "Your WhatsApp receptionist". Not "AI
  receptionist on WhatsApp. Replies generated: 47." — nobody cares how
  many replies were generated, and the sentence reads like a log file.
- Nothing on the page should look like a database. No IDs, no codes, no
  message IDs, no raw status words. If a piece of information only makes
  sense to a developer, it does not belong on this page.

This has to be achievable with plain HTML and inline styling only — the
product runs on the standard library, no external assets, no frameworks.
That is fine. A clean page with good spacing and one colour does not need
a framework. What it needs is restraint: fewer boxes, bigger text, more
air.

## What the words should say

No jargon. An owner does not say "leads", "handoffs", "intents", or
"outbox", so the dashboard must not say them either. Rename everything
into the owner's words:

- "Leads" becomes "People who messaged". A lead is a sales word. A person
  who messaged the shop is a customer.
- "Handoffs" becomes "People waiting for you". That is what it is: a
  person waiting for the owner to pick up.
- "Handoffs (resolved)" becomes a count, not a section: "3 handled
  today". Nobody re-reads people they already helped.
- "Recent replies" becomes "Replies the bot sent". And the status words
  become plain English: "Sent", "Not sent", "Failed — needs you". Never
  "recorded", "sending", "send_status". An owner does not know what a
  send status is, and should not have to.
- "Resolve" on the button becomes "I handled it". That is what the owner
  is saying when they tap it.
- "Sending: LIVE / PAUSED / DEMO / DRY RUN" becomes "Bot is ON" or "Bot
  is OFF", plus one short honest line about what that means. "Demo" and
  "dry run" are developer words. The owner only needs to know: is the bot
  talking to my customers right now, or is it only writing drafts?
- "Intents" — the column that says what kind of message it was — should
  not exist at all. The owner does not care whether a message was
  classified as a price question or a booking. They care what the person
  said. Show the words, not the label.
- Prices stay as they are: "Rs 1,499" is exactly how an owner talks.

Every heading on the page should read like something the owner would say
out loud. If a heading sounds like it came from a database, rewrite it.

## What to cut

Be ruthless. The current dashboard shows everything the system knows,
and that is why nobody wants to open it. Cut:

- The "Intents" column. It is a developer label, not information.
- The resolved handoffs table. Keep a count ("3 handled today"), delete
  the table. Nobody scrolls back through people they already helped.
- The message ID and detail columns on the replies table. An owner will
  never read a message ID. If a send failed, say why in plain words
  ("WhatsApp refused it" or "no connection"), not a code.
- The "Replies generated: 47" counter line. It is a vanity number for
  the builder, not the owner.
- The full replies history. Keep failed sends and the last few. The rest
  is a log, and logs belong in a file, not on the owner's screen.
- The five-column tables. Every table should become either a short list
  or a set of cards. If a section cannot be read on a phone without
  scrolling sideways, it should not be a table.

What stays: people waiting for you, today's bookings, failed sends, the
last few replies, the customer list, and the stop button. That is the
whole dashboard. Everything else is clutter that makes the important
things harder to find.

## The test for whether this worked

Show it to an owner and watch, without explaining anything. They should
be able to answer, in under ten seconds and without asking a question:
is anyone waiting for me, who is coming today, and is the bot on or off.
If they have to ask what a word means, or scroll sideways, or hunt for
the stop button, the redesign failed. The bar is not "prettier". The bar
is "an owner would open this every day and feel in control of their shop".
