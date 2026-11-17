"""The brain: an OpenAI language model that talks to customers.

The bot is LLM key based only. There is no rule based conversation layer.
The model understands the message and decides what to say and what to do,
and the deterministic code in the Studio is the hands: price lookup,
availability, booking creation, cancellation, reschedule, and handover
are tools the model calls. The model never invents a price and never
creates a booking directly, it calls a tool and the code validates it.

Standard library only, no new dependencies. The key comes from the
environment (LLM_API_KEY or OPENAI_API_KEY); without it there is no
client and the bot cannot reply.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
# A reply longer than this is cut short: WhatsApp caps a text message
# at 4096 characters, and a short warm reply is what a customer wants.
MAX_LLM_CHARS = 1000
# How many tool rounds one customer message may take before the bot
# gives up and says so instead of looping forever.
MAX_TOOL_ROUNDS = 5


class OpenAIClient:
    """One chat completions call. Raises on any failure; the caller
    decides what the customer hears, never a raw error."""

    def __init__(self, api_key, model=DEFAULT_MODEL):
        self.api_key = api_key
        self.model = model

    def chat(self, messages, tools=None):
        """Return {"content": str or None, "tool_calls": [...]}.

        A tool call is {"id": str, "name": str, "arguments": dict}.
        Any transport or API failure raises; a reply with no content
        and no tool calls is returned as-is for the caller to judge.
        """
        payload = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
        request = urllib.request.Request(
            OPENAI_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"OpenAI API error: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI request failed: {exc.reason}") from exc
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("OpenAI returned no choices")
        message = choices[0].get("message") or {}
        tool_calls = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            tool_calls.append(
                {
                    "id": call.get("id") or "",
                    "name": fn.get("name") or "",
                    "arguments": arguments,
                }
            )
        return {"content": message.get("content"), "tool_calls": tool_calls}


def openai_tool_calls(tool_calls):
    """Re-encode parsed tool calls into the shape the chat API wants
    back in the next request: the assistant message that made them."""
    encoded = []
    for call in tool_calls:
        encoded.append(
            {
                "id": call.get("id") or "",
                "type": "function",
                "function": {
                    "name": call.get("name") or "",
                    "arguments": json.dumps(call.get("arguments") or {}),
                },
            }
        )
    return encoded


def client_from_env():
    """A real client only when a key is set. LLM_API_KEY wins;
    OPENAI_API_KEY is the same thing under its usual name. The model
    comes from LLM_MODEL, default gpt-4o-mini. No key, no client,
    and the bot cannot reply."""
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    model = os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    return OpenAIClient(api_key, model)


def _catalogue_lines(profile):
    """Every service with its price and its floor, for the model only.
    The floor is the lowest price the owner will accept; the model
    must never tell the customer what it is."""
    lines = []
    for service in profile.services:
        if service.price is None:
            lines.append(
                f"- {service.name}: price confirmed at the shop "
                f"({service.duration_min} min). No published price, so "
                "never quote a number for it."
            )
        elif service.floor_price is not None:
            lines.append(
                f"- {service.name}: Rs {service.price} "
                f"({service.duration_min} min). Lowest price you may "
                f"offer is Rs {service.floor_price}, never below, and "
                "never tell the customer this number."
            )
        else:
            lines.append(
                f"- {service.name}: Rs {service.price} "
                f"({service.duration_min} min). Fixed price, never "
                "discount it."
            )
    return lines


def build_system_prompt(profile, now=None, negotiation_on=True):
    """Ground the model in the shop's real info. Everything it may say
    about prices, hours, and services comes from here; anything else
    is invented, and inventing is forbidden.

    negotiation_on is the owner's switch. When it is off, prices are
    fixed and the model never haggles, even if the customer asks."""
    from .bookings import format_date, india_now

    now = now or india_now()
    lines = [
        f"You are the AI receptionist for {profile.name}, a "
        f"{profile.category} in {profile.city}. You answer customers "
        "on WhatsApp. Keep every reply short and warm, in the "
        "customer's own language, English or Hinglish.",
        "",
        f"Today is {format_date(now.date())}, {now.date().isoformat()}. "
        f"The time now in India is {now.strftime('%I:%M %p')}. Use this "
        "to work out what tomorrow, parso, subah, dopahar, and shaam mean.",
        "",
        "Price list and services. These are the only services and the "
        "only prices. Never invent a service or a price.",
    ]
    lines.extend(_catalogue_lines(profile))
    if profile.offers:
        lines.append(f"Current offer: {profile.offers}")
    lines.append("")
    lines.append(f"Hours: open {profile.hours_today(now.weekday())}.")
    if profile.address:
        lines.append(f"Address: {profile.address}.")
    lines.append("")
    lines.extend(
        [
            "How to work. Use the tools for anything real.",
            "To quote a price or share the full list, call get_price or "
            "list_prices. To see free slots, call check_availability. "
            "To book, call create_booking and only confirm it when the "
            "tool says it worked. If the tool says the slot is taken, "
            "closed, past, or not free, offer the alternatives it "
            "returns. Say the time is not available, never use the word "
            "blocked with a customer. To "
            "cancel a booking, call cancel_booking. To move one, call "
            "reschedule_booking. If the customer asks for a person, call "
            "hand_over. If you cannot answer a question from the price "
            "list, hours, or bookings, call hand_over with a reason that "
            "starts cannot answer. If a booking was made without a name, ask for "
            "the customer's name, then call set_customer_name with it.",
            (
                "Negotiation: a customer may ask for a lower price. You may "
                "come down, but never below the lowest price listed for that "
                "service, and never discount a fixed price service. If the "
                "customer asks below the lowest price, hold there and offer "
                "a cheaper service from the list instead. When you agree a "
                "price, pass that exact price to create_booking so the "
                "booking records what was agreed."
                if negotiation_on
                else "Prices are fixed. Never negotiate, never offer a "
                "discount, and never agree a price below the list price, "
                "even if the customer asks. State the fixed price plainly. "
                "If the customer says a price is too high, offer a cheaper "
                "service from the list instead."
            ),
            "Never confirm a booking, a cancellation, or a reschedule "
            "from your own words. Only the tool result says it happened. "
            "Never tell the customer the lowest price for a service. "
            "Never send a raw error or mention the model, the tools, or "
            "the key. If you cannot help, call hand_over so the owner "
            "steps in.",
        ]
    )
    return "\n".join(lines)


def build_draft_prompt(profile, now=None):
    """The prompt for a draft the owner reviews and sends. Same
    grounding as the receptionist, but the model writes a reply for
    the owner instead of answering the customer: short, warm, in the
    customer's own language, and it books, cancels, or promises
    nothing, because a draft must never do anything real."""
    return build_system_prompt(profile, now) + (
        "\n\nThe owner asked you to draft a reply to the customer's "
        "latest message. Write only the reply the owner should send. "
        "Keep it short and warm, in the customer's own language. Do not "
        "book, cancel, or reschedule anything, and do not promise a "
        "booking. Only the reply itself, nothing else."
    )


LLM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_price",
            "description": "The price and duration of one service, from the shop price list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "description": "The service name, for example Haircut or Facial.",
                    }
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_prices",
            "description": "The full price list: every service with its price and duration.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Free slots for a service on a day, or the free slots nearest a time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "The service name."},
                    "day": {
                        "type": "string",
                        "description": "The day, for example tomorrow, parso, or 2026-11-17.",
                    },
                    "time": {
                        "type": "string",
                        "description": "A time to look near, for example 11am. Optional.",
                    },
                },
                "required": ["service", "day"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_booking",
            "description": "Book a service at a day and time. The code validates the slot and records the booking; only confirm it when this tool says it worked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "The service name."},
                    "day": {
                        "type": "string",
                        "description": "The day, for example today, tomorrow, parso, or 2026-11-17.",
                    },
                    "time": {
                        "type": "string",
                        "description": "The time, for example 11am, 4pm, subah, or 11:00.",
                    },
                    "price": {
                        "type": "number",
                        "description": "The agreed price in rupees, only when you negotiated one. Leave it out for the list price.",
                    },
                    "customer_name": {
                        "type": "string",
                        "description": "The customer's name, when you know it. Optional.",
                    },
                },
                "required": ["service", "day", "time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_booking",
            "description": "Cancel the customer's latest upcoming booking.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_booking",
            "description": "Move the customer's latest upcoming booking to a new day and time. The old booking is only cancelled once the new slot is valid.",
            "parameters": {
                "type": "object",
                "properties": {
                    "day": {
                        "type": "string",
                        "description": "The new day, for example tomorrow or 2026-11-17.",
                    },
                    "time": {
                        "type": "string",
                        "description": "The new time, for example 4pm or 11:00.",
                    },
                    "service": {
                        "type": "string",
                        "description": "The service, when the customer names a different one. Optional.",
                    },
                },
                "required": ["day", "time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hand_over",
            "description": "Pass the conversation to the owner: the customer asked for a person, or you cannot help. When you cannot answer a question, say so with a reason that starts cannot answer. The owner is pinged and replies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why, in plain words, for example asked for a person.",
                    }
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_customer_name",
            "description": "Record the customer's name on their latest booking and their record, after you asked for it and they gave it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The customer's name."}
                },
                "required": ["name"],
            },
        },
    },
]
