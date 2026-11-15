"""The demo runs the prospect's own shop: PROFILE_PATH in run.py."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run as run_module
from whatsapp_studio.llm import OpenAIClient, client_from_env


class TestProfilePath(unittest.TestCase):
    def test_default_profile_is_the_demo_salon(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROFILE_PATH", None)
            profile = run_module.load_profile()
        self.assertEqual(profile.name, "Glow & Grace Salon")

    def test_profile_path_points_at_another_shop(self):
        other = {
            "name": "Snips Salon",
            "category": "salon",
            "city": "Gurgaon",
            "phone": "919810000000",
            "hours": {"daily": "9:00 AM - 9:00 PM"},
            "services": [
                {"name": "Trim", "price": 299, "duration_min": 20},
            ],
            "address": "Sector 43, Gurgaon",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snips.json"
            path.write_text(json.dumps(other), encoding="utf-8")
            with mock.patch.dict(os.environ, {"PROFILE_PATH": str(path)}):
                profile = run_module.load_profile()
        self.assertEqual(profile.name, "Snips Salon")
        self.assertEqual(profile.services[0].name, "Trim")
        self.assertEqual(profile.services[0].price, 299)


class TestLlmFromEnv(unittest.TestCase):
    """run.py builds the Studio's LLM client from the environment:
    LLM_API_KEY first, OPENAI_API_KEY as the alias, LLM_MODEL for
    the model. No key means no client, and the bot cannot reply."""

    def test_llm_api_key_wins(self):
        with mock.patch.dict(
            os.environ,
            {"LLM_API_KEY": "sk-llm", "OPENAI_API_KEY": "sk-openai"},
        ):
            client = client_from_env()
        self.assertIsInstance(client, OpenAIClient)
        self.assertEqual(client.api_key, "sk-llm")
        self.assertEqual(client.model, "gpt-4o-mini")

    def test_openai_api_key_is_the_alias(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-openai"}):
            os.environ.pop("LLM_API_KEY", None)
            client = client_from_env()
        self.assertIsInstance(client, OpenAIClient)
        self.assertEqual(client.api_key, "sk-openai")

    def test_model_from_env(self):
        with mock.patch.dict(
            os.environ, {"LLM_API_KEY": "sk-llm", "LLM_MODEL": "gpt-4o"}
        ):
            client = client_from_env()
        self.assertEqual(client.model, "gpt-4o")

    def test_no_key_means_no_client(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("LLM_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            self.assertIsNone(client_from_env())


if __name__ == "__main__":
    unittest.main()
