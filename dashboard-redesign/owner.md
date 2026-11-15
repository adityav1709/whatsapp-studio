# Dashboard redesign — the business owner's brief

Written from the point of view of the owner of a small salon / clinic in
India. I am on my feet all day, cutting hair or with a patient. I check my
phone between customers. I am not technical. Aditya asked me what I think
of the dashboard, and the honest answer is: I would not open it. It looks
like a developer's tool, not something for my shop. This brief is what
would make me open it, use it, and show it to another shop owner.

## The one-line truth

I already told you in the handoff brief: I will not watch a dashboard.
WhatsApp is where my eyes are. The dashboard has exactly one job — the
end-of-day check. When I close the shop, I want to know in two minutes:
did I miss anyone, how did today go, and is anything wrong. If it cannot
answer those three questions in the first five seconds, I will never open
it twice. A dashboard that tries to be a live monitoring console will
fail, because I will not monitor anything.

## What would make me open it every day

The first five seconds have to answer three questions, in plain words,
with nothing to read through:

1. Does anyone need me right now? Either "2 people need you" or "all
   clear, nobody needs you". A good day should feel like a good day. If
   the answer is nobody, I should be able to close it in two seconds and
   get back to work. The dashboard's best day is a boring day.
2. How did today go? How many people are coming today, at what times,
   and how much is that worth. That is the only number that matters to
   me. Not how many replies were generated. Not how many leads. Rupees
   and bookings.
3. Did anything go wrong? One line, only if something did. If the bot
   said something wrong or a message did not reach a customer, tell me
   that first, in plain words, and tell me who to call. If nothing went
   wrong, say nothing went wrong and let me go.

That is it. That is the whole dashboard. Everything else is detail I will
look at only when one of those three answers makes me look.

## What the current dashboard gets wrong

I opened it on my phone. Here is what I saw, honestly:

- **Five tables of raw data and no order.** It looks like a database
  dumped on a page. I have to read everything to find the one thing
  that matters, and I will not read everything. The most important
  thing — who needs me — is sitting in the middle of the page as a
  table. It should be the first thing on the screen, and it should be
  big.
- **It leads with a developer's number.** "AI receptionist on WhatsApp.
  Replies generated: 47." I do not care how many replies were
  generated. That is a number for the person who built it, not for me.
  I care how many customers were helped, how many booked, and how much
  money that is. The dashboard never mentions rupees once.
- **It speaks in words I do not know.** "Intent" is a column heading.
  "wamid" is printed next to messages. "Truncated to 4096 chars."
  "SEND FAILED." "DEMO — no WhatsApp token configured." "DRY RUN."
  Every one of those words makes me feel stupid, and I will not use
  something that makes me feel stupid. A failed send should say "this
  message did not reach the customer — call them", not show me a code.
  A bot that is not live yet should say "the bot is watching and
  drafting replies, nothing is sent yet", not "DEMO — no token".
- **The handoff is a seven-column table.** Name, phone, reason,
  trigger, open for, messages since, and a button. The customer's
  actual words are squeezed into a cell with up to ten messages crammed
  together. On my phone I have to scroll sideways, and I cannot read
  it. The thing I need — what they said, and a way to call them — is
  buried. A handoff is not a row in a table. It is a person who needs
  me, and it should look like one: their name big, their words in
  full, what the bot already told them, and a call button.
- **The Resolve button is tiny and at the far end of a wide table.**
  On a phone I have to scroll across and tap a small button with my
  thumb. I will not do that. I will call the customer and forget to
  press it, so the handoff stays open forever and the list rots. If a
  button matters, it has to be big enough to hit with a thumb, and it
  has to be next to the thing it acts on.
- **There is no "nothing needs you" state.** When there are no
  handoffs it says "No open handoffs." in a grey table cell. Flat, no
  feeling. A clear day should say so clearly, so I know I can stop
  looking.
- **It does not feel like my shop.** No colour, no shape, just black
  text and grey table borders. It looks like a server page, not
  something I own. My business name is there, but it reads like a file
  name, not like my shop. If I am going to show this to another shop
  owner, it has to look like a real product, not a test page.
- **The login is a grey box with no explanation.** The browser asks
  for a username and password and tells me nothing about what I am
  opening. I had to be told what to type. A login that does not say
  whose shop it is and what it is for is a login I will not trust.

## What would make me trust it and show it to someone else

- The first screen tells me, in one glance, "2 people need you" or
  "all clear", "3 bookings today, Rs 4,500", "the bot answered 11
  customers, nothing went wrong". That is a screen I would show
  another shop owner and say "look, my bot did this today". That is
  what "worth paying for" looks like.
- Every customer has a name and a face in my head. Show names big,
  phone numbers I can tap to call, and the customer's actual words,
  not labels. Never show me "price" or "booking" or "human" as a
  category — show me what they said.
- Proof the bot is behaving. I want to read the exact words it sent
  today, in plain language, and say "yes, that is what I want my
  customers to get". Not a log of every reply — a short list of what
  went out, with anything that failed called out as "did not reach the
  customer, call them".
- The off switch must be one big button, visible at the top, and when
  I hit it the screen must say clearly "the bot is stopped, it will
  not reply to anyone". Proof, not hope. I need to know it actually
  stopped.
- It should feel like my shop: my business name big, today's date, my
  hours, my money. If it looks like a product I own, I will treat it
  like one. If it looks like a test page, I will treat it like a test.

## What I need on my phone

I am not on a laptop. I never will be. Everything below is not a nice
extra, it is the whole product:

- One column. No sideways scrolling, ever. If I have to scroll
  sideways, I am gone.
- Big text, big buttons. I read this standing up, between customers,
  in bad light, with one hand. A button has to be the size of my
  thumb. Text has to be big enough to read without pinching and
  zooming.
- The call button must dial. One tap and my phone calls the customer.
  I will not copy a number and paste it into the dialler. If the
  number is plain text I cannot tap, it is useless to me.
- The most important thing at the top. I will not scroll through five
  tables to find the one customer who needs me. Who needs me, then
  today, then everything else.
- It has to load fast. If I open it between customers and it spins
  for a minute, I will close it and never open it again. A slow first
  load is a dead product for me. (I was told the free hosting sleeps
  when idle and takes about a minute to wake up. That alone would kill
  this for me, and it needs an honest answer, not a shrug.)
- No pinch-zoom, no tiny grey text, no tables wider than my screen.

## What I would never tolerate

- **Jargon.** Intent, wamid, webhook, token, outbox, send status,
  DRY RUN, DEMO. Any word I have to ask what it means is a word that
  makes me feel stupid, and I will close the page. If a word is not
  something I would say to a customer, it does not belong on my screen.
- **Clutter.** Five tables, twenty columns, every message the bot ever
  sent. I do not want a log. I want an answer. Show me what needs me,
  show me today, and hide the rest until I ask for it.
- **Numbers I do not understand.** "Replies generated", "message
  count", "intents". The only numbers I understand are customers,
  bookings, and rupees. Every other number is noise, and noise is why
  I will not open it.
- **Anything that makes me feel stupid.** An error I cannot fix, a
  setting I do not understand, a blank page, a login with no
  explanation. If the product makes me feel like I did something
  wrong, I will blame the product and never come back.
- **Being asked to do admin work.** Tagging, classifying, resolving
  by typing, copying numbers, reading a manual. If the dashboard asks
  me to do work, it has failed. The dashboard should do the work and
  show me the result. My only jobs are: call the customer, and press
  one big button when something is done.

## What to build first (dashboard redesign)

1. The top of the page, in plain words: who needs me right now — open
   handoffs as big cards, each with the customer's name big, their
   words in full, what the bot already told them, a tap-to-call
   button, and a big resolve button. Or, if nobody needs me, a clear
   "all clear" so I can close it in two seconds.
2. Today at a glance: bookings today with times and rupees, customers
   the bot answered, and one line if anything went wrong. Nothing
   else at the top.
3. One big off switch at the top, with proof when it is hit: "the bot
   is stopped, it will not reply to anyone".
4. What the bot sent today — short, plain, newest first — with
   anything that failed called out as "did not reach the customer,
   call them". No codes, no message ids.
5. The end-of-day missed list: the people I did not get to, so I can
   call them back before I close. This is the one list I will actually
   use every day.

## What NOT to build

- More tables, more columns, more raw data. If the answer is "show
  everything", it is the wrong answer.
- Any jargon, anywhere on the screen. Not in a heading, not in a
  status line, not in an error.
- Analytics, charts, tags, categories, priorities. I do not want a
  report. I want to know who needs me and how today went.
- Anything that needs a laptop, a mouse, or two hands.
- A settings page. If something needs a setting, it should have a
  sensible default and stay out of my way.
- A dashboard that pretends to be live monitoring. I will not watch
  it live. Build it for the two-minute end-of-day check and the
  glance between customers, and stop there.

## The blunt summary

The bot is good. The dashboard is not. The bot answers my customers,
books them in, and gets out of the way when a person is needed — that
is the product. The dashboard looks like the person who built the bot
made it for himself, not for me. I do not need more data. I need the
three answers — who needs me, how did today go, did anything go
wrong — in the first five seconds, on my phone, in words I would use
myself, with a call button I can hit with my thumb. Give me that, and
I will open it every day and show it to the next shop owner I meet.
Give me another table of system labels, and I will keep answering
every message myself, because that is the only way I know it gets done
right.
