"""The Gemini brain: the same chat interface as OpenAI, different API.

A Gemini key (GEMINI_API_KEY) selects this client; it translates the
OpenAI shaped messages the Studio sends into generateContent calls
and reads the function calls back into the same tool call shape, so
the Studio never knows which brain answered. These tests use scripted
stand-ins, never a real key or network.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from whatsapp_studio.app import Studio
from whatsapp_studio.bookings import BookingStore
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.llm import (
    DEFAULT_GEMINI_MODEL,
    LLM_TOOLS,
    GeminiClient,
    OpenAIClient,
    client_from_env,
)


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
    def test_gemini_key_selects_gemini(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "AIza-test"}, clear=True):
            client = client_from_env()
        self.assertIsInstance(client, GeminiClient)
        self.assertEqual(client.api_key, "AIza-test")
        self.assertEqual(client.model, DEFAULT_GEMINI_MODEL)
        self.assertEqual(client.model, "gemini-flash-latest")

    def test_gemini_model_from_env(self):
        with mock.patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "AIza-test", "GEMINI_MODEL": "gemini-2.5-flash"},
            clear=True,
        ):
            client = client_from_env()
        self.assertEqual(client.model, "gemini-2.5-flash")

    def test_gemini_wins_when_both_keys_are_set(self):
        with mock.patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "AIza-test", "LLM_API_KEY": "sk-llm"},
            clear=True,
        ):
            client = client_from_env()
        self.assertIsInstance(client, GeminiClient)

    def test_openai_key_still_selects_openai(self):
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-llm"}, clear=True):
            client = client_from_env()
        self.assertIsInstance(client, OpenAIClient)

    def test_no_key_means_no_client(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(client_from_env())


class TestGeminiClient(unittest.TestCase):
    def _fake_response(self, payload):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        return FakeResponse()

    def _text_reply(self, text):
        return {
            "candidates": [{"content": {"parts": [{"text": text}]}}],
        }

    def test_posts_generate_content_and_parses_reply(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["key"] = request.headers.get("X-goog-api-key")
            return self._fake_response(self._text_reply("Hello!"))

        client = GeminiClient("AIza-test", "gemini-flash-latest")
        with mock.patch(
            "whatsapp_studio.llm.urllib.request.urlopen", fake_urlopen
        ):
            result = client.chat(
                [
                    {"role": "system", "content": "You are a receptionist."},
                    {"role": "user", "content": "hi"},
                ]
            )
        self.assertEqual(result["content"], "Hello!")
        self.assertEqual(result["tool_calls"], [])
        self.assertIn(
            "generativelanguage.googleapis.com/v1beta/models/"
            "gemini-flash-latest:generateContent",
            captured["url"],
        )
        self.assertEqual(captured["key"], "AIza-test")
        self.assertEqual(
            captured["body"]["system_instruction"]["parts"][0]["text"],
            "You are a receptionist.",
        )
        self.assertEqual(captured["body"]["contents"][0]["role"], "user")
        self.assertEqual(
            captured["body"]["contents"][0]["parts"][0]["text"], "hi"
        )
        self.assertNotIn("tools", captured["body"])
        self.assertEqual(captured["body"]["generationConfig"]["maxOutputTokens"], 500)

    def test_tools_become_function_declarations(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return self._fake_response(self._text_reply("ok"))

        client = GeminiClient("AIza-test")
        with mock.patch(
            "whatsapp_studio.llm.urllib.request.urlopen", fake_urlopen
        ):
            client.chat([{"role": "user", "content": "hi"}], tools=LLM_TOOLS)
        declarations = captured["body"]["tools"][0]["functionDeclarations"]
        self.assertEqual(len(declarations), len(LLM_TOOLS))
        first = declarations[0]
        self.assertEqual(first["name"], "get_price")
        self.assertIn("price", first["description"])
        self.assertEqual(first["parameters"]["type"], "object")
        self.assertIn("service", first["parameters"]["properties"])
        prices = [d for d in declarations if d["name"] == "list_prices"][0]
        self.assertEqual(prices["parameters"]["type"], "object")

    def test_parses_function_call(self):
        def fake_urlopen(request, timeout=None):
            return self._fake_response(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "get_price",
                                            "args": {"service": "Facial"},
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        client = GeminiClient("AIza-test")
        with mock.patch(
            "whatsapp_studio.llm.urllib.request.urlopen", fake_urlopen
        ):
            result = client.chat(
                [{"role": "user", "content": "facial price?"}], tools=LLM_TOOLS
            )
        self.assertIsNone(result["content"])
        self.assertEqual(len(result["tool_calls"]), 1)
        call = result["tool_calls"][0]
        self.assertEqual(call["name"], "get_price")
        self.assertEqual(call["arguments"], {"service": "Facial"})

    def test_text_and_function_call_in_one_reply(self):
        def fake_urlopen(request, timeout=None):
            return self._fake_response(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "Let me check that for you."},
                                    {
                                        "functionCall": {
                                            "name": "list_prices",
                                            "args": {},
                                        }
                                    },
                                ]
                            }
                        }
                    ]
                }
            )

        client = GeminiClient("AIza-test")
        with mock.patch(
            "whatsapp_studio.llm.urllib.request.urlopen", fake_urlopen
        ):
            result = client.chat([{"role": "user", "content": "prices?"}])
        self.assertEqual(result["content"], "Let me check that for you.")
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertEqual(result["tool_calls"][0]["name"], "list_prices")
        self.assertEqual(result["tool_calls"][0]["arguments"], {})

    def test_assistant_tool_calls_and_tool_results_translate(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return self._fake_response(self._text_reply("Facial is Rs 1499."))

        client = GeminiClient("AIza-test")
        messages = [
            {"role": "system", "content": "You are a receptionist."},
            {"role": "user", "content": "facial price?"},
            {
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
            },
            {
                "role": "tool",
                "tool_call_id": "call-9",
                "content": '{"ok": true, "price": 1499}',
            },
        ]
        with mock.patch(
            "whatsapp_studio.llm.urllib.request.urlopen", fake_urlopen
        ):
            client.chat(messages)
        contents = captured["body"]["contents"]
        self.assertEqual(contents[0]["role"], "user")
        model = contents[1]
        self.assertEqual(model["role"], "model")
        self.assertEqual(
            model["parts"][0]["functionCall"]["name"], "get_price"
        )
        self.assertEqual(
            model["parts"][0]["functionCall"]["args"], {"service": "Facial"}
        )
        function = contents[2]
        self.assertEqual(function["role"], "function")
        response = function["parts"][0]["functionResponse"]
        self.assertEqual(response["name"], "get_price")
        self.assertEqual(response["response"], {"ok": True, "price": 1499})

    def test_tool_result_without_a_call_id_matches_in_order(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return self._fake_response(self._text_reply("done"))

        client = GeminiClient("AIza-test")
        messages = [
            {"role": "user", "content": "facial price?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "",
                        "type": "function",
                        "function": {
                            "name": "get_price",
                            "arguments": '{"service": "Facial"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-0",
                "content": '{"ok": true, "price": 1499}',
            },
        ]
        with mock.patch(
            "whatsapp_studio.llm.urllib.request.urlopen", fake_urlopen
        ):
            client.chat(messages)
        function = captured["body"]["contents"][2]
        response = function["parts"][0]["functionResponse"]
        self.assertEqual(response["name"], "get_price")

    def test_consecutive_user_turns_merge(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return self._fake_response(self._text_reply("ok"))

        client = GeminiClient("AIza-test")
        with mock.patch(
            "whatsapp_studio.llm.urllib.request.urlopen", fake_urlopen
        ):
            client.chat(
                [
                    {"role": "user", "content": "hi"},
                    {"role": "user", "content": "facial price?"},
                ]
            )
        contents = captured["body"]["contents"]
        self.assertEqual(len(contents), 1)
        self.assertEqual(
            [p["text"] for p in contents[0]["parts"]],
            ["hi", "facial price?"],
        )

    def test_http_error_raises(self):
        import urllib.error

        def failing_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-flash-latest:generateContent",
                400,
                "bad request",
                {},
                None,
            )

        client = GeminiClient("AIza-bad")
        with mock.patch(
            "whatsapp_studio.llm.urllib.request.urlopen", failing_urlopen
        ):
            with self.assertRaises(RuntimeError) as caught:
                client.chat([{"role": "user", "content": "hi"}])
        self.assertIn("Gemini API error: HTTP 400", str(caught.exception))

    def test_no_candidates_raises(self):
        def fake_urlopen(request, timeout=None):
            return self._fake_response({"candidates": []})

        client = GeminiClient("AIza-test")
        with mock.patch(
            "whatsapp_studio.llm.urllib.request.urlopen", fake_urlopen
        ):
            with self.assertRaises(RuntimeError) as caught:
                client.chat([{"role": "user", "content": "hi"}])
        self.assertIn("Gemini returned no candidates", str(caught.exception))


class TestStudioWithGemini(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_response(self, payload):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        return FakeResponse()

    def _studio(self, client):
        return Studio(
            _profile(),
            LeadStore(Path(self.tmp.name) / "leads.json"),
            bookings=BookingStore(),
            handoffs=HandoffStore(),
            notifier=FakeNotifier(),
            llm=client,
        )

    def test_gemini_books_through_get_price(self):
        payloads = [
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "get_price",
                                        "args": {"service": "Facial"},
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Facial is Rs 1499 for 60 minutes."}
                            ]
                        }
                    }
                ]
            },
        ]

        def fake_urlopen(request, timeout=None):
            return self._fake_response(payloads.pop(0))

        studio = self._studio(GeminiClient("AIza-test"))
        with mock.patch(
            "whatsapp_studio.llm.urllib.request.urlopen", fake_urlopen
        ):
            entry = studio.handle_message(
                "919810000001", "facial ka price kya hai?", "Rahul"
            )
        self.assertIn("Rs 1499", entry["body"])
        self.assertEqual(len(payloads), 0)


if __name__ == "__main__":
    unittest.main()
