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
LOCATION = "location"
HUMAN = "human"
THANKS = "thanks"
FALLBACK = "fallback"

_PATTERNS = {
    GREETING: [r"\b(hi|hii+|hello|hey|namaste|namaskar|good\s+(morning|afternoon|evening))\b"],
    HOURS: [r"\b(timing|timings|hours|open|close|closed|kab\s+khulta|kitne\s+baje|open\s+hai)\b"],
    PRICE: [r"\b(price|prices|pricing|rate|rates|cost|charges|fees|kitne\s+ka|kitna|kya\s+price|charges\s+kya|how\s+much)\b"],
    BOOKING: [r"\b(book|booking|appointment|slot|reserve|schedule|book\s+karna|appointment\s+chahiye)\b"],
    CANCEL: [r"\b(cancel|reschedule)\b.*\b(booking|appointment|slot)\b",
             r"\bcancel\s+my\b"],
    LOCATION: [r"\b(where|location|address|kahan|kaha|kahaan|address\s+kya|location\s+kya|reach|direction)\b"],
    HUMAN: [r"\b(human|agent|person|someone|real\s+person|call\s+me|talk\s+to)\b"],
    THANKS: [r"\b(thank|thanks|shukriya|dhanyavad)\b"],
}

# A message asking something beats a bare greeting.
_PRIORITY = [CANCEL, BOOKING, PRICE, HOURS, LOCATION, HUMAN, THANKS, GREETING]


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
