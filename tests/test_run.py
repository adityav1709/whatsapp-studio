"""The demo runs the prospect's own shop: PROFILE_PATH in run.py."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run as run_module


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


if __name__ == "__main__":
    unittest.main()
