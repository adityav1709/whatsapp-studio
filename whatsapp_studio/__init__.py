"""WhatsApp Studio: an AI receptionist for Indian small businesses on WhatsApp.

The brain is an OpenAI language model (gpt-4o-mini by default, key from
the environment); deterministic code runs the tools it calls (price
lookup, availability, booking), so prices and bookings stay exact.
Lead capture, bookings, handoffs, and the dashboard run on the Python
standard library only.
"""
