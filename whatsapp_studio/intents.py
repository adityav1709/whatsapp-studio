"""Rule-based intent detection for incoming WhatsApp messages.

Deliberately dependency-free and transparent: every match is a keyword
hit, so a business owner can see exactly why a reply was chosen.
English + Hinglish keywords, since SMB customers in India write both.
"""
from __future__ import annotations

import re

GREETING = "greeting"
HOURS = "hours"
PRICE = "price"
BOOKING = "booking"
CANCEL = "cancel"
RESCHEDULE = "reschedule"
OFFER = "offer"
LOCATION = "location"
HUMAN = "human"
THANKS = "thanks"
FALLBACK = "fallback"

_PATTERNS = {
    GREETING: [r"\b(hi|hii+|hello|hey|namaste|namaskar|good\s+(morning|afternoon|evening))\b", r"नमस्ते"],
    HOURS: [r"\b(timing|timings|hours|open|close|closed|kab\s+khulta|kitne\s+baje|open\s+hai)\b", r"समय", r"खुला", r"बंद"],
    PRICE: [r"\b(price|prices|pricing|rate|rates|cost|charges|fees|kitne\s+ka|kitna|kya\s+price|charges\s+kya|how\s+much)\b", r"कीमत", r"कितने", r"कितना"],
    BOOKING: [r"\b(book|booking|appointment|slot|reserve|schedule|book\s+karna|appointment\s+chahiye)\b", r"बुक", r"अपॉइंटमेंट"],
    CANCEL: [r"\bcancel\b.*\b(booking|appointment|slot)\b",
             r"\bcancel\s+my\b"],
    RESCHEDULE: [r"\breschedule\b",
                 r"\b(move|change|shift)\b.*\b(booking|appointment|slot)\b"],
    OFFER: [r"\b(offer|offers|discount|deal|deals|coupon|promo)\b"],
    LOCATION: [r"\b(where|location|address|kahan|kaha|kahaan|address\s+kya|location\s+kya|reach|direction)\b"],
    HUMAN: [
        # Person-specific only: "talk to" on its own is not a handoff —
        # "talk to me about prices" is a price question, not a person request.
        r"\btalk to (a |the )?(person|human|someone|manager|owner)\b",
        r"\bspeak to (a |the )?(person|human|someone|manager|owner)\b",
        r"\b(real person|human being)\b",
        r"\b(human|manager|owner)\b",
        r"\bcall me\b",
        r"\b(agent|insaan|madam|kisi)\s+se\s+baat\b",
        r"इंसान", r"मैनेजर",
    ],
    THANKS: [r"\b(thank|thanks|shukriya|dhanyavad)\b", r"धन्यवाद"],
}

# A message asking something beats a bare greeting. A request for a person
# beats a topic (booking, price, hours) but not a cancellation: "let me
# talk to a person about my booking" is a handoff, "cancel my booking" is
# still a cancellation. A reschedule beats a fresh booking: "move my
# booking" is moving the old one, not making a new one.
_PRIORITY = [CANCEL, HUMAN, RESCHEDULE, BOOKING, PRICE, OFFER, HOURS, LOCATION, THANKS, GREETING]


def detect_intent(text):
    """Return the best-matching intent for a message, or 'fallback'."""
    if not text or not text.strip():
        return FALLBACK
    lowered = text.lower()
    for intent in _PRIORITY:
        for pattern in _PATTERNS[intent]:
            if re.search(pattern, lowered):
                return intent
    return FALLBACK
