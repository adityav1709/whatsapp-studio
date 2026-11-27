"""The LLM brain: the model talks, the code does the real work.

The Studio calls the model with the shop profile and the thread; the
model answers in words or calls a tool, and the Studio runs the tool
in code. These tests use scripted stand-ins, never a real key or
network. A turn is a dict: {"content": str} for words, or
{"tool_calls": [{"name": str, "arguments": dict}]} for a tool call.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from whatsapp_studio.app import Studio, handoff_confirmation
from whatsapp_studio.bookings import BookingStore, IST, india_today
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.llm import (
    DEFAULT_MODEL,
    LLM_TOOLS,
    OpenAIClient,
    build_system_prompt,
    client_from_env,
)


def C(text):
    """A scripted turn where the model answers in words."""
    return {"content": text, "tool_calls": []}


def T(name, arguments, call_id="call-1"):
    """A scripted turn where the model calls one tool."""
    return {
        "content": None,
        "tool_calls": [{"id": call_id, "name": name, "arguments": arguments}],
    }


class ScriptedLLM:
    """Stand-in for the OpenAI client: returns canned turns in order,
    and records every chat() call so a test can see what the Studio
    sent, including the tool results it fed back. Raises when the
    script runs out, so a test that forgets a turn fails loudly
    instead of passing on a made-up reply."""

    def __init__(self, turns, default=None):
        self.turns = list(turns)
        self.default = default
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append({"messages": list(messages), "tools": tools})
        if self.turns:
            return self.turns.pop(0)
        if self.default is not None:
            return self.default
        raise AssertionError("ScriptedLLM ran out of scripted turns")


class FixedLLM:
    """Always answers with the same words. For tests that only need
    the bot to have answered, not what it said."""

    def __init__(self, content):
        self.content = content
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append({"messages": list(messages), "tools": tools})
        return {"content": self.content, "tool_calls": []}


class RaisingLLM:
    """The model is down: every call raises, like a real outage."""

    def __init__(self, error=None):
        self.error = error or RuntimeError("OpenAI API error: HTTP 500")
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append({"messages": list(messages), "tools": tools})
        raise self.error


def _profile():
    return BusinessProfile(
        name="Glow & Grace Salon",
        category="salon",
        city="Gurgaon",
        phone="919999999999",
        hours={"daily": "10:00 AM - 8:00 PM"},
        services=[
            Service("Haircut", 499, 30, floor_price=400),
            Service("Facial", 1499, 60),
            Service("Hair Spa", None, 45),
        ],
        address="Sector 50, Gurgaon",
        offers="20% off on all facials this week",
    )


class FakeNotifier:
    def __init__(self):
        self.calls = []
        self.booking_calls = []

    def notify(self, business_name, handoff):
        self.calls.append((business_name, handoff))

    def notify_booking(self, business_name, booking):
        self.booking_calls.append((business_name, booking))


class TestClientFromEnv(unittest.TestCase):
    def test_llm_api_key_wins(self):
        with mock.patch.dict(
            os.environ,
            {"LLM_API_KEY": "sk-llm", "OPENAI_API_KEY": "sk-openai"},
        ):
            client = client_from_env()
        self.assertIsInstance(client, OpenAIClient)
        self.assertEqual(client.api_key, "sk-llm")
        self.assertEqual(client.model, DEFAULT_MODEL)

    def test_openai_api_key_is_the_alias(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-openai"}):
            os.environ.pop("LLM_API_KEY", None)
            client = client_from_env()
        self.assertIsInstance(client, OpenAIClient)
        self.assertEqual(client.api_key, "sk-openai")

    def test_no_key_means_no_client(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("LLM_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            self.assertIsNone(client_from_env())

    def test_model_from_env(self):
        with mock.patch.dict(
            os.environ, {"LLM_API_KEY": "sk-llm", "LLM_MODEL": "gpt-4o"}
        ):
            client = client_from_env()
        self.assertEqual(client.model, "gpt-4o")


class TestOpenAIClient(unittest.TestCase):
    def _fake_response(self, payload):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        return FakeResponse()

    def test_posts_chat_completion_and_parses_reply(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["auth"] = request.headers.get("Authorization")
            return self._fake_response(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Hello!",
                                "tool_calls": [],
                            }
                        }
                    ]
                }
            )

        client = OpenAIClient("sk-test", "gpt-4o-mini")
        with mock.patch(
            "whatsapp_studio.llm.urllib.request.urlopen", fake_urlopen
        ):
            result = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result["content"], "Hello!")
        self.assertEqual(result["tool_calls"], [])
        self.assertIn("api.openai.com/v1/chat/completions", captured["url"])
        self.assertEqual(captured["auth"], "Bearer sk-test")
        self.assertEqual(captured["body"]["model"], "gpt-4o-mini")

    def test_parses_tool_calls(self):
        def fake_urlopen(request, timeout=None):
            return self._fake_response(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-9",
                                        "type": "function",
                                        "function": {
                                            "name": "get_price",
                                            "arguments": '{"service": "Facial"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )

        client = OpenAIClient("sk-test")
        with mock.patch(
            "whatsapp_studio.llm.urllib.request.urlopen", fake_urlopen
        ):
            result = client.chat(
                [{"role": "user", "content": "facial price?"}], tools=LLM_TOOLS
            )
        self.assertIsNone(result["content"])
        self.assertEqual(len(result["tool_calls"]), 1)
        call = result["tool_calls"][0]
        self.assertEqual(call["id"], "call-9")
        self.assertEqual(call["name"], "get_price")
        self.assertEqual(call["arguments"], {"service": "Facial"})

    def test_http_error_raises(self):
        import urllib.error

        def failing_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                "https://api.openai.com/v1/chat/completions",
                401,
                "unauthorized",
                {},
                None,
            )

        client = OpenAIClient("sk-bad")
        with mock.patch(
            "whatsapp_studio.llm.urllib.request.urlopen", failing_urlopen
        ):
            with self.assertRaises(RuntimeError):
                client.chat([{"role": "user", "content": "hi"}])

    def test_no_choices_raises(self):
        def fake_urlopen(request, timeout=None):
            return self._fake_response({"choices": []})

        client = OpenAIClient("sk-test")
        with mock.patch(
            "whatsapp_studio.llm.urllib.request.urlopen", fake_urlopen
        ):
            with self.assertRaises(RuntimeError):
                client.chat([{"role": "user", "content": "hi"}])


class TestSystemPrompt(unittest.TestCase):
    def test_contains_the_real_prices_and_never_invents_one(self):
        prompt = build_system_prompt(_profile())
        self.assertIn("Rs 499", prompt)
        self.assertIn("Rs 1499", prompt)
        self.assertIn("Haircut", prompt)
        self.assertIn("Facial", prompt)
        self.assertIn("Hair Spa", prompt)
        self.assertIn("10:00 AM - 8:00 PM", prompt)
        self.assertIn("Sector 50, Gurgaon", prompt)
        self.assertIn("20% off on all facials", prompt)
        self.assertIn("Never invent a service or a price", prompt)

    def test_floor_is_in_the_prompt_but_flagged_never_to_reveal(self):
        prompt = build_system_prompt(_profile())
        self.assertIn("Rs 400", prompt)
        self.assertIn("never tell the customer this number", prompt)

    def test_unpriced_service_never_gets_a_number(self):
        prompt = build_system_prompt(_profile())
        self.assertIn("price confirmed at the shop", prompt)
        # The only rupees in the prompt are the real list and floor prices.
        self.assertNotIn("Rs None", prompt)

    def test_forbids_confirming_a_booking_without_a_tool(self):
        prompt = build_system_prompt(_profile())
        self.assertIn("only confirm it when the tool says it worked", prompt)
        self.assertIn("call hand_over", prompt)

    def test_knows_today_and_now(self):
        now = datetime(2026, 11, 15, 10, 0, tzinfo=IST)
        prompt = build_system_prompt(_profile(), now=now)
        self.assertIn("2026-11-15", prompt)
        self.assertIn("10:00 AM", prompt)


class TestStudioLLM(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notifier = FakeNotifier()

    def tearDown(self):
        self.tmp.cleanup()

    def _studio(self, llm):
        return Studio(
            _profile(),
            LeadStore(Path(self.tmp.name) / "leads.json"),
            bookings=BookingStore(),
            handoffs=HandoffStore(),
            notifier=self.notifier,
            llm=llm,
        )

    def test_llm_answers_a_general_question(self):
        studio = self._studio(
            ScriptedLLM([C("We are a salon in Gurgaon. How can I help?")])
        )
        entry = studio.handle_message("919810000001", "do you do bridal?", "Rahul")
        self.assertEqual(entry["body"], "We are a salon in Gurgaon. How can I help?")
        self.assertEqual(len(studio.outbox), 1)
        # The system prompt and the tools went with the call.
        call = studio.llm.calls[0]
        self.assertIn("Glow & Grace Salon", call["messages"][0]["content"])
        self.assertEqual(call["tools"], LLM_TOOLS)

    def test_llm_calls_get_price_and_the_tool_runs_it(self):
        studio = self._studio(
            ScriptedLLM(
                [
                    T("get_price", {"service": "Facial"}),
                    C("Facial is Rs 1499 for 60 minutes."),
                ]
            )
        )
        entry = studio.handle_message(
            "919810000001", "facial ka price kya hai?", "Rahul"
        )
        self.assertIn("Rs 1499", entry["body"])
        # The tool result the Studio fed back carries the real price.
        tool_messages = [
            m
            for m in studio.llm.calls[-1]["messages"]
            if m["role"] == "tool"
        ]
        self.assertEqual(len(tool_messages), 1)
        result = json.loads(tool_messages[0]["content"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["price"], 1499)

    def test_llm_books_through_the_tool_and_the_booking_is_real(self):
        studio = self._studio(
            ScriptedLLM(
                [
                    T(
                        "create_booking",
                        {"service": "Haircut", "day": "tomorrow", "time": "11am"},
                    ),
                    C("Done! Your Haircut is booked for tomorrow at 11:00 AM."),
                ]
            )
        )
        entry = studio.handle_message(
            "919810000001", "book haircut tomorrow 11am", "Rahul"
        )
        self.assertIn("booked", entry["body"])
        self.assertEqual(len(studio.bookings.all_bookings()), 1)
        booking = studio.bookings.all_bookings()[0]
        self.assertEqual(booking["service"], "Haircut")
        self.assertEqual(booking["price"], 499)
        self.assertEqual(len(self.notifier.booking_calls), 1)

    def test_tool_refusal_reaches_the_model(self):
        studio = self._studio(
            ScriptedLLM(
                [
                    T(
                        "create_booking",
                        {"service": "Haircut", "day": "tomorrow", "time": "11am"},
                    ),
                    C("Done! Your Haircut is booked for tomorrow at 11:00 AM."),
                ]
            )
        )
        studio.handle_message("919810000001", "book haircut tomorrow 11am", "Rahul")
        studio.llm = ScriptedLLM(
            [
                T(
                    "create_booking",
                    {"service": "Haircut", "day": "tomorrow", "time": "11am"},
                ),
                C("Sorry, 11:00 AM tomorrow is already taken."),
            ]
        )
        entry = studio.handle_message(
            "919810000002", "book haircut tomorrow 11am", "Priya"
        )
        self.assertIn("already taken", entry["body"])
        tool_messages = [
            m
            for m in studio.llm.calls[-1]["messages"]
            if m["role"] == "tool"
        ]
        result = json.loads(tool_messages[0]["content"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "already taken")
        self.assertIn("10:30 AM", result["alternatives"])
        self.assertEqual(len(studio.bookings.all_bookings()), 1)

    def test_negotiated_price_below_the_floor_is_refused(self):
        studio = self._studio(
            ScriptedLLM(
                [
                    T(
                        "create_booking",
                        {
                            "service": "Haircut",
                            "day": "tomorrow",
                            "time": "11am",
                            "price": 300,
                        },
                    ),
                    C("The lowest I can do for a Haircut is Rs 400."),
                ]
            )
        )
        entry = studio.handle_message(
            "919810000001", "haircut 300 me kar do", "Rahul"
        )
        self.assertIn("Rs 400", entry["body"])
        self.assertEqual(len(studio.bookings.all_bookings()), 0)
        tool_messages = [
            m
            for m in studio.llm.calls[-1]["messages"]
            if m["role"] == "tool"
        ]
        result = json.loads(tool_messages[0]["content"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "below the lowest price")
        self.assertEqual(result["lowest"], 400)

    def test_negotiated_price_at_the_floor_books_at_that_price(self):
        studio = self._studio(
            ScriptedLLM(
                [
                    T(
                        "create_booking",
                        {
                            "service": "Haircut",
                            "day": "tomorrow",
                            "time": "11am",
                            "price": 400,
                        },
                    ),
                    C("Done! Your Haircut is booked for tomorrow at 11:00 AM for Rs 400."),
                ]
            )
        )
        entry = studio.handle_message(
            "919810000001", "haircut 400 me kar do", "Rahul"
        )
        self.assertIn("Rs 400", entry["body"])
        booking = studio.bookings.all_bookings()[0]
        self.assertEqual(booking["price"], 400)

    def test_fixed_price_service_cannot_be_discounted(self):
        studio = self._studio(
            ScriptedLLM(
                [
                    T(
                        "create_booking",
                        {
                            "service": "Facial",
                            "day": "tomorrow",
                            "time": "11am",
                            "price": 1000,
                        },
                    ),
                    C("Facial is a fixed Rs 1499."),
                ]
            )
        )
        studio.handle_message("919810000001", "facial 1000 me kar do", "Rahul")
        self.assertEqual(len(studio.bookings.all_bookings()), 0)
        tool_messages = [
            m
            for m in studio.llm.calls[-1]["messages"]
            if m["role"] == "tool"
        ]
        result = json.loads(tool_messages[0]["content"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "fixed price")

    def test_llm_failure_opens_a_handoff_instead_of_silence(self):
        studio = self._studio(RaisingLLM())
        # Open hours, pinned: without a now the confirmation wording
        # follows the real clock and flips after closing time.
        entry = studio.handle_message(
            "919810000001",
            "hello?",
            "Rahul",
            now=datetime(2026, 11, 16, 11, 0, tzinfo=IST),
        )
        self.assertIn("passed this to the team", entry["body"])
        handoff = studio.handoffs.open_for("919810000001")
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff["reason"], "couldn't reply")
        self.assertEqual(len(self.notifier.calls), 1)
        self.assertEqual(len(studio.outbox), 1)

    def test_llm_failure_after_hours_is_honest_about_closing(self):
        studio = self._studio(RaisingLLM())
        entry = studio.handle_message(
            "919810000001",
            "hello?",
            "Rahul",
            now=datetime(2026, 11, 16, 2, 30, tzinfo=IST),
        )
        self.assertIn("We're closed right now", entry["body"])
        self.assertIn("passed your message to the team", entry["body"])
        self.assertIn("opens at 10:00 AM", entry["body"])
        self.assertNotIn("30 minutes", entry["body"])
        self.assertIsNotNone(studio.handoffs.open_for("919810000001"))

    def test_no_key_means_no_reply_but_the_lead_is_kept(self):
        studio = self._studio(None)
        entry = studio.handle_message("919810000001", "hi, price?", "Rahul")
        self.assertIsNone(entry["body"])
        self.assertTrue(entry["no_llm"])
        self.assertEqual(len(studio.outbox), 0)
        self.assertEqual(len(studio.leads.all_leads()), 1)
        html = studio.dashboard_html()
        self.assertIn("needs an LLM key", html)
        self.assertIn("LLM_API_KEY", html)

    def test_dashboard_says_nothing_about_a_key_when_one_is_set(self):
        studio = self._studio(ScriptedLLM([C("hi")]))
        html = studio.dashboard_html()
        self.assertNotIn("needs an LLM key", html)

    def test_history_goes_to_the_model_on_the_second_message(self):
        studio = self._studio(
            ScriptedLLM(
                [
                    C("Hello! How can I help?"),
                    C("Welcome back, Rahul!"),
                ]
            )
        )
        studio.handle_message("919810000001", "hi", "Rahul")
        studio.handle_message("919810000001", "hi", "Rahul")
        second_call = studio.llm.calls[1]["messages"]
        roles = [(m["role"], m["content"]) for m in second_call]
        self.assertIn(("user", "hi"), roles)
        self.assertIn(("assistant", "Hello! How can I help?"), roles)

    def test_hand_over_tool_opens_the_handoff(self):
        studio = self._studio(
            ScriptedLLM(
                [
                    T("hand_over", {"reason": "asked for a person"}),
                    C("I've passed this to the team. They will reply here within 30 minutes."),
                ]
            )
        )
        entry = studio.handle_message(
            "919810000001", "can I talk to a person please", "Rahul",
            now=datetime(2026, 11, 15, 11, 0, tzinfo=IST),
        )
        self.assertIn("passed this to the team", entry["body"])
        handoff = studio.handoffs.open_for("919810000001")
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff["reason"], "asked for a person")
        self.assertEqual(len(self.notifier.calls), 1)


class TestHandoffConfirmation(unittest.TestCase):
    def test_open_hours_promises_30_minutes(self):
        now = datetime(2026, 11, 15, 11, 0, tzinfo=IST)
        reply = handoff_confirmation(_profile(), "couldn't reply", now)
        self.assertIn("30 minutes", reply)

    def test_after_hours_is_honest_about_closing(self):
        now = datetime(2026, 11, 15, 22, 0, tzinfo=IST)
        reply = handoff_confirmation(_profile(), "couldn't reply", now)
        self.assertIn("closed", reply.lower())
        self.assertIn("10:00 AM", reply)
        self.assertNotIn("30 minutes", reply)


if __name__ == "__main__":
    unittest.main()
