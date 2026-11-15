import tempfile
import unittest
from pathlib import Path

from whatsapp_studio.leads import LeadStore


class TestLeadStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "leads.json"
        self.store = LeadStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_lead_created(self):
        lead = self.store.record_message("919810000001", "hi", None, "Rahul")
        self.assertEqual(lead["phone"], "919810000001")
        self.assertEqual(lead["name"], "Rahul")
        self.assertEqual(lead["message_count"], 1)

    def test_repeat_messages_accumulate(self):
        self.store.record_message("919810000001", "hi", None, "Rahul")
        lead = self.store.record_message("919810000001", "price?", None, "Rahul")
        self.assertEqual(lead["message_count"], 2)
        # No routed intent any more: the model understands the message,
        # the lead record keeps only the words.
        self.assertEqual(lead["intents"], [])

    def test_persistence_across_instances(self):
        self.store.record_message("919810000001", "hi", None, "Rahul")
        reopened = LeadStore(self.path)
        lead = reopened.get("919810000001")
        self.assertIsNotNone(lead)
        self.assertEqual(lead["message_count"], 1)

    def test_all_leads_returns_both(self):
        self.store.record_message("919810000001", "hi", None)
        self.store.record_message("919810000002", "hi", None)
        leads = self.store.all_leads()
        self.assertEqual(len(leads), 2)


if __name__ == "__main__":
    unittest.main()
