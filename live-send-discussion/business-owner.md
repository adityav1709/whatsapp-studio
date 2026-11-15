# Live sending — the business owner's brief

Written from the point of view of the owner of a small salon / clinic in
India. I am on my feet all day, cutting hair or with a patient. I check my
phone between customers. I am not technical. I will not read a manual, open
a dashboard, or install a new app for this.

## The one-line truth

Live sending is the moment the bot stops being a demo and starts touching
real customers on my real number. That is the whole point of this product —
and the whole risk. Everything before this was safe because nothing left
the building. The second a message goes out, my name is on it, and if it
is wrong, the customer blames me, not the bot. So I will not switch this on
because someone told me it works. I will switch it on when I have seen it
work on my own phone, I know how to stop it in one second, and I believe
nothing bad can happen while I am not looking.

## The setup — can I actually do it?

No. Not on my own, and do not pretend otherwise.

To send through Meta I need a Meta Business account, a WhatsApp Business
number, and an access token. Every one of those words is a place I will get
stuck. Meta Business Suite is a maze of screens that ask for things I do
not have on hand. The access token is a long string of gibberish that I
have to copy exactly without breaking it, and I will not know if I broke
it until something fails silently. Business verification asks for documents
— GST, a certificate, an address proof — and then makes me wait. And the
webhook step, where Meta calls back to check the server, is not something
I can do or even check.

Where I give up: the second screen that asks me for something I do not
understand. I will not sit through a setup video. I will not read a help
article. I will say "do it later" and later never comes.

So the setup must be done FOR me, not BY me. The person selling me this
sets it up, on a call or in person, in one sitting, while I watch. My part
is: give my phone number, say yes when Meta asks me to confirm, and hand
over nothing else. If the setup needs me to do more than that, it is not a
product for me yet.

## The number problem — the hidden barrier nobody mentions

My business number is on the WhatsApp Business app today. It is the number
on my board, my visiting card, my Google listing. Every customer has it
saved. If I move that number to the Cloud API, I cannot use the WhatsApp
Business app on it any more. A number can only live in one place.

I will not give up my number. I will not give up the app I answer
customers on every day. If "going live" means losing that, my answer is no
before you finish the sentence.

There are only two honest ways around this, and the product has to say
which one it is using, in plain words:

1. A second number for the bot. A new SIM, a number the bot lives on,
   customers message that number. It costs a SIM and it means the bot is
   not on the number customers already know — so someone has to tell
   customers about it, or the bot never gets a message.
2. Move my real number to the API and I answer customers a different way
   (through the dashboard, or not at all). I will only accept this if
   someone shows me, on my own phone, that answering still works and is
   not slower than the app.

If the product does not have an answer to this, it does not have a pilot.
This is the question that kills the sale, and it will come up in the first
five minutes of talking to a real owner.

## What does it cost?

Here is what I have been told, and what I believe: Meta gives a free
allowance (around 1,000 customer conversations a month) and after that
charges a few paise per message. A shop like mine sends dozens of messages
a day, not thousands, so the bill would be nothing or tens of rupees a
month. I do not believe the exact number until I see it — rates change,
and "free" from a big company usually has a catch — so do not promise me
a number. Show me the bill, or show me where the bill would come from.

The money is not the barrier. The barrier is everything else. But the
things that cost me are not rupees, they are these:

- The messaging limit. A new number can only message a few hundred people
  a day at first, and Meta raises it only if customers do not complain.
  If customers block or report the number, the limit drops and the number
  can get restricted. My number is my business. I will not risk it for a
  bot I have not tested.
- Template messages. If the bot wants to message a customer more than a
  day after they last wrote, Meta makes it use a pre-approved template,
  and templates can be rejected. The bot cannot just write whatever it
  wants, whenever it wants. The product has to respect that, or the
  messages will simply not go out and I will not know why.
- The 24-hour window. The bot can reply freely only within a day of the
  customer's last message. After that it is templates or nothing. An owner
  who does not know this will think the bot is broken when it goes quiet.

## What would make me refuse

- Fear my number gets banned. This is number one. My number is my shop.
  One wrong message, a few customers hitting block, and Meta restricts it.
  I will not hand my number to a bot I have not seen work.
- Fear of spam. If the bot messages a customer who did not ask, or
  messages the same person twice, or messages at 2 in the morning, I am
  the one who looks bad. The bot must only ever reply to someone who
  messaged first, and only during working hours.
- Business verification. Documents, waiting, a rejection I do not
  understand. If verification is needed before anything works, most small
  owners will stop there.
- Handing over my Meta / Facebook account. I will not give a stranger the
  keys to my business account, and I will not understand what I am giving
  away. Setup has to work without me handing over my login.
- Cost uncertainty. I do not trust "free". I assume a bill is coming and
  I do not know how big it is. A surprise bill, even a small one, is a
  reason to switch it off and never come back.
- Losing the WhatsApp Business app on my number. See above. This alone
  is a no.
- No way to stop it. If I cannot turn it off instantly, I will not turn
  it on.

## What would make me trust it

- A test message to myself first. Before any customer sees anything, the
  bot sends me a message on my own phone — a price reply, a booking
  confirmation, a handoff alert — and I read it and say "yes, that is
  what I want my customers to get". No test, no live. This is not
  optional.
- Shadow mode before live mode. The bot writes the replies, I see them,
  nothing goes out. I watch it for a day or two, I see it get things
  right, and then I say the word and it goes live. The switch from shadow
  to live is mine, not the product's.
- I see exactly what the bot will send, before it sends it. The outbox,
  in plain language, on my phone or on one screen. No surprises. If the
  bot is going to say "Rs 1499", I want to have read "Rs 1499" before a
  customer does.
- A clear off switch, and proof it works. One word, one button, and the
  bot stops — and it tells me it stopped. If I am on leave or the shop is
  closed for a day, I need to stop it without a settings page. And I need
  to know it actually stopped, not hope.
- The bot only replies to people who message first. Never initiates,
  never follows up on its own, never sends a "we miss you" message. That
  rule is what keeps it from being spam, and I need to hear it said out
  loud.
- A cap I can understand. "The bot will never send more than 50 messages
  a day" or "only between 10am and 8pm". A limit I can hold in my head is
  worth more than any setting.
- The handoff promise, kept. The moment a customer asks for a person, the
  bot stops and I take over. Live sending without a working handoff is
  just a bot talking to my customers with no adult in the room.

## The one thing that makes me say "turn it off" on day one

The bot says something wrong to a real customer, and I hear about it from
the customer. A wrong price. A booking it confirmed that does not exist. A
reply that sounds rude or confused. A message sent at midnight. A customer
telling me "your bot is annoying" or "I messaged and nobody replied".

One of those, once, and it is off forever. Not paused, not "let me fix
it" — off. Because my reputation is the shop, and a bot that embarrasses
me in front of a customer costs more than every message it ever answered
correctly.

The second thing, almost as bad: I get a notification that the bot replied
to someone and I do not understand what it said, or it contradicts
something I already told that customer. If the bot and I disagree in front
of a customer, the customer will think we are confused, and they will be
right.

## The rollout I would actually accept

1. Shadow mode. The bot drafts replies, I read them, nothing is sent.
   A day or two of this, and I decide.
2. Test on my own phone. The bot sends live messages only to me, as the
   customer. I see the price reply, the booking confirmation, the
   handoff alert. I say yes or no to each one.
3. Go live, with a cap. Working hours only, only replies to incoming
   messages, and I know how to stop it in one second.
4. Setup done for me, in one sitting, by the person selling it. I give
   my number and say yes when asked. Nothing else.
5. The number question answered up front, in plain words: second number
   or my real number, and what changes for me either way. No surprises
   after I say yes.
6. A weekly check, not a dashboard. One message a week telling me what
   the bot did — how many customers it answered, how many handoffs, did
   anything go wrong. If nothing went wrong, I do not want to hear from
   it at all.

## What NOT to build, and what NOT to ask me to do

- Do not ask me to create a Meta Business account, copy a token, verify
  my business, or touch a webhook. That is your job, not mine.
- Do not ask me to give you my Facebook or Meta login. Ever.
- Do not make the bot message customers first, or follow up on its own.
  It replies. That is all.
- Do not make the bot send anything outside working hours. A message at
  midnight is not helpful, it is a complaint waiting to happen.
- Do not hide what the bot sends. If I cannot see the message before a
  customer does, I do not trust it.
- Do not make "stop" a settings page. One word, one button, and it stops.
- Do not charge me for the setup, the test, or the first month. I will
  not pay to find out if something works.
- Do not put my real number at risk to save a step. If the safe way is a
  second number or a test first, do the safe way.

## The blunt summary

Live sending is not a technical step. It is a trust step. The bot is
about to speak for me, on my number, to my customers, while I am busy
with someone else's hair. I will let it do that only when I have seen it
speak correctly on my own phone, I know I can stop it instantly, and I
believe my number is safe. Give me a test first, a shadow mode, a cap, an
off switch, and an honest answer about my number — and I will turn it on.
Skip any one of those, and I will keep answering every message myself,
because that is the only way I know it gets done right.
