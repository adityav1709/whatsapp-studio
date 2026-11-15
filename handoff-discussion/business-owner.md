# Human handoff — the business owner's brief

Written from the point of view of the owner of a small salon / clinic in
India. I am on my feet all day, cutting hair or with a patient. I check my
phone between customers. I am not technical. I will not read a manual, open
a dashboard, or install a new app for this.

## The one-line truth

If the handoff does not arrive as a normal WhatsApp message on my personal
phone, it does not exist. I will not watch a dashboard. I will not check
email. I will not install Telegram. WhatsApp is where my eyes are, and it
is the only channel that reaches me while I am working.

## How I should be notified

- One WhatsApp message, to my personal number, within a minute of the
  customer asking. Not a link, not a summary that makes me open something.
  The message itself must carry everything.
- Only during working hours (10am to 8pm for the demo salon). If a customer
  asks at 11pm, do not wake me. Tell the customer we open at 10am and that
  we will call them first thing, and put it in a morning list for me.
- Only when it is a real handoff. If you ping me for every "hi", every
  price question the bot already answered, or every message the bot did not
  understand, I will mute the number within a week and the whole feature
  is dead. A handoff means: the customer asked for a person, or the bot
  failed them twice in a row, or they sound angry / are complaining.
- It must look different from a normal customer message so I do not miss
  it in a busy chat list. Start it clearly, for example "Customer needs
  you" or similar, then the details.

## What the message must contain

I decide in five seconds whether to stop what I am doing. Give me:

1. The customer's name and phone number. If I have to open anything to
   find the number, I will not bother.
2. What they want, in their own words — the actual last message, not a
   summary. One or two lines.
3. What the bot already told them. I must not repeat the price or the
   booking the bot already gave, and I must not contradict it.
4. Whether they already tried to book, and what happened (booked, slot
   taken, cancelled). A booking that failed is the most common reason a
   customer asks for a person.

No full chat history. No links. No "open dashboard for details".

## How I take over

I will do exactly one of two things: call the customer, or reply to the
message. Calling is my default — it is faster than typing and it is how I
already handle every real customer.

So the notification must make calling one tap: the customer's number right
there in the message. If I reply instead, I will reply to your notification
message on my own phone, in plain words. That is the whole takeover flow.
There is no login, no button, no "type TAKEOVER", no dashboard. I will not
do any of those, ever.

The moment I step in, the bot must stop talking to that customer. Nothing
is worse than the bot and me both replying at the same time with different
answers. The customer will think we are confused, and they will be right.

## How I hand back to the bot

I will forget to hand back. Do not rely on me remembering anything.

Hand back automatically: if I have not replied in that conversation for
some time (say 15 to 30 minutes), the bot takes over again. If I want to
hand back sooner, I can just say "done" in my reply to the notification.
That is the only extra word I am willing to learn, and even that is optional.

## What makes me trust this vs ignore it

I will judge the whole product by the first two or three handoffs.

- I trust it when the notification is fast, the customer is real, and the
  details are right. If the first handoff saves me a booking I would have
  lost, I will keep it on forever.
- I ignore it when it cries wolf. False alarms, slow notifications, or a
  customer who already left by the time I read it — three of those and I
  switch it off and never switch it back on.
- The bot must never promise what I cannot do. Right now it tells the
  customer "a person will reply within a few hours" and nobody is notified
  at all. That is a lie, and the customer waits hours, gets nothing, and
  never comes back. Until this feature works, the bot should say something
  honest, like "I will pass this to the team" — and then actually pass it.
- I must be able to switch handoff off easily. If I am on leave or the shop
  is closed for a day, I need one simple way to stop the notifications, not
  a settings page.

## What is too complicated (I will not do any of this)

- Log in anywhere, open a dashboard, or check a website.
- Install Telegram, or any new app.
- Remember command words or codes.
- Tag, classify, label, or rate a conversation.
- Type a customer's number by hand from one screen into another.
- Read a manual or watch a setup video.
- Pay for anything extra to make this work. If handoff needs a paid
  message or a paid service, it is not happening.

Setup must be one thing: give my personal phone number once, when the
business is set up. That is it.

## What happens if I miss the notification

I will miss some. I am with a customer, or my phone is on silent, or I am
driving. The product must handle that, not blame me.

- Nudge me once more after some time (say 15 minutes) if I have not
  replied. One nudge, not five. Five nudges is spam and I will mute it.
- Do not leave the customer hanging. If I have not replied in a reasonable
  time, the bot should tell the customer honestly, for example that the
  team is busy and will call them back, and give them a time ("we will call
  you by 6pm today" is better than "soon").
- Keep a missed list for the end of the day. When I close the shop, I want
  to see the two or three people I missed and call them back. This is the
  one job the dashboard is actually good for: end-of-day review, not live
  monitoring.

## What to build first (v1)

1. One owner phone number in the business profile (different from the
   business WhatsApp number).
2. One WhatsApp message to that number when a real handoff triggers, with
   name, number, the customer's words, what the bot already said, and
   booking status.
3. Takeover by the owner replying to that message (or calling the customer
   directly). Bot goes quiet for that customer the moment the owner steps
   in.
4. Automatic hand-back after a period of silence, plus "done" as an
   optional shortcut.
5. Working-hours only. After hours, the customer gets an honest message
   and the handoff waits for the morning.
6. One nudge if I miss it, plus an end-of-day missed list.
7. An easy off switch.

## What NOT to build

- Takeover buttons or controls in the dashboard. I will not use them.
- Telegram notifications. I do not have it and will not install it.
- Routing to multiple staff members. V1 is one owner. A salon with three
  staff is a later problem, and most small shops are one owner anyway.
- Tags, categories, priorities, or analytics on handoffs.
- A separate app or login for the owner.
- Anything that asks the customer to do extra work during a handoff
  (forms, menus, "press 1"). They asked for a person because they are
  already frustrated. Do not make it worse.

## The blunt summary

The bot's job is to handle the easy stuff and get out of the way the
second a human is needed. My job is to talk to the customer. The handoff
is a bridge between the two, and the bridge is a single WhatsApp message
on my phone. If that message is fast, short, and complete, I will use this
every day. If it is slow, spammy, or makes me open anything else, I will
turn it off and go back to answering every message myself.
