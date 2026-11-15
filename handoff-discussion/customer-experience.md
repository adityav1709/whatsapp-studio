# Human handoff — the customer's experience

Voice: customer experience. What a real customer on WhatsApp feels, not how the
system works. WhatsApp Studio v0.2, demo business Glow & Grace Salon, Gurgaon
(open daily 10:00 AM – 8:00 PM).

## The problem today

The bot already detects a request for a human and replies: "Of course, I will
connect you with a person from the team. They usually reply within a few hours
during working hours. Meanwhile, can I help with prices, timings, or booking?"

From the customer's side, that message is a lie three ways. Nobody is actually
told. "Within a few hours" is not a time. And "meanwhile, can I help" invites
the customer to keep talking to the bot — the opposite of handing over. If the
customer keeps messaging, the bot keeps answering as if nothing happened. The
customer asked for a person and got more bot. That is the moment trust breaks,
and it is usually the moment the customer was already frustrated. A handoff
that the owner never hears about is the same as no handoff at all.

## How a customer asks for a human

Rarely with the words "connect me to a human." What they actually write:

- Direct: "talk to someone", "real person", "call me", "agent se baat karao",
  "kisi se baat karni hai", "madam se baat karni hai", "insaan se baat karao".
- Mid-task: "my booking is wrong, let me talk to a person", "I need to talk
  to someone about the price" — a human request wrapped inside another topic.
- Frustration, not a request: the same question asked twice, "you don't
  understand", "tum samajh nahi rahe", "not this", "hello??" after an
  unhelpful answer, or a second fallback reply in a row. The customer is
  asking for a human without saying the words.
- A phone call: "call me" means they want a voice call, not a chat handoff.
  That is a callback request, and it needs a number and a time, not a chat
  queue.

Two consequences. First, an explicit request for a human must beat every other
topic — today the booking, price, hours and location topics all outrank it, so
a customer who says "let me talk to a person about my booking" gets pushed
back into the booking flow. Second, the bot should offer a human itself after
it fails twice in a row, instead of waiting to be asked. The customer should
never have to fight the bot to reach a person.

## What the customer sees while waiting

WhatsApp shows read ticks. The customer can see the business has read their
message, so silence after that feels like being ignored, not like being queued.
The bot must acknowledge immediately, in one message, and the message must do
three things: confirm the handoff happened, say who will reply, and give an
honest wait.

- During open hours: "I've passed this to Ritu at the salon. She usually
  replies within 30 minutes." A real name and a real time, not "the team" and
  "a few hours."
- The customer should not be asked to repeat themselves. The bot should hand
  the human the conversation so far, and say so: "I've shared our chat with
  her, so you don't need to explain again."
- One question at most while they wait: "what is this about, in one line?"
  — and only if it helps the human. Never an interrogation while someone is
  waiting.
- If the wait is going to be long, say so honestly. "She is with a customer
  right now, she will reply in about an hour" keeps a customer. "Within a few
  hours" loses them.

## When no human is available

After 8:00 PM the shop is closed, and a handoff request at 9:00 PM must not
promise a human soon. The customer needs three things instead: an honest
statement that nobody is there right now, a specific time they will hear back
("we open at 10 AM, you will hear from us by 11"), and a way to leave their
message so they do not have to come back tomorrow and start over. The bot
should also offer, once, what it can still do right now — prices, timings, a
booking for tomorrow — but only as an offer, not as a deflection. If the
customer says no, the bot stops and takes the message.

The same applies when the owner is simply busy. "Nobody is available right
now" is a fine answer. A fake promise of a quick reply is not.

## How the customer knows a human has taken over

The customer must be able to tell, or the handoff has not happened. A human
says their name and talks like a person: "Hi, this is Ritu from Glow & Grace,
what happened with your booking?" The bot's messages are formal and templated;
a human's are not, and that difference is the signal. A short line from the
human introducing themselves is worth more than any system label, because on
WhatsApp there is no label — there is only the tone of the message.

Equally important: the bot must go quiet the moment the handoff starts. From
that point, every message the customer sends goes to the human and nobody
else. The customer should never get a bot reply in the middle of talking to a
person.

## How the bot resumes after the human is done

A handoff has to end cleanly, or the customer ends up in limbo. The human
should be able to hand back clearly — a short line to the customer like "I'll
let our assistant help you finish the booking" — and then the bot resumes.
When it does, it must not restart the conversation. No "Hello! Welcome to Glow
& Grace Salon" again, and no asking what the customer needs as if the last ten
minutes did not happen. Its first message should show it knows what just
happened: pick up the booking, the question, or simply ask "shall we continue
with the booking?"

If the human simply stops replying, the bot should check back after a
reasonable wait with an honest update — "Ritu had to step away, shall I help
you finish this, or should she call you back?" — not silence, and not a
cheerful restart.

## What the bot must NEVER do during a handoff

- Keep auto-replying while the human is talking. One bot message over a human
  conversation breaks the illusion that anyone is listening, and it can
  contradict the human.
- Send booking confirmations, cancellations, or slot offers while the human
  is mid-conversation. A confirmation that lands while the human is still
  sorting out the problem reads as the business not listening.
- Pretend to be the human. Never sign a bot message with a person's name, and
  never answer as if it were the owner.
- Promise a callback time the owner cannot keep. A missed promised callback
  is worse than no promise.
- Ask the customer to repeat what they already told the bot, or what they
  told the human.
- Keep offering help after the handoff is agreed. "Meanwhile, can I help with
  prices?" after the customer asked for a person is the bot refusing the
  handoff.
- Leave the customer with no way back. If the human does not reply, the
  customer must get an honest update, not silence and not a restart.

## The test that matters

A customer who asks for a human at 7:30 PM about a wrong booking should feel
this: heard immediately, told who is coming and when, never asked to repeat
themselves, talking to a named person within the promised time, and handed
back to the bot only when the problem is solved. If any step of that feels
like talking to a wall, the handoff has failed, whatever the system recorded.
