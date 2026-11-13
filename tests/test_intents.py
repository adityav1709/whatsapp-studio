import unittest

from whatsapp_studio.intents import detect_intent


class TestIntents(unittest.TestCase):
    def test_greetings(self):
        for text in ["hi", "hello", "hey there", "namaste", "good morning"]:
            self.assertEqual(detect_intent(text), "greeting", text)

    def test_hours(self):
        for text in ["what are your timings?", "kitne baje khulta hai", "are you open on sunday?"]:
            self.assertEqual(detect_intent(text), "hours", text)

    def test_price(self):
        for text in ["facial ka price kya hai", "how much for a haircut?", "what are your rates?"]:
            self.assertEqual(detect_intent(text), "price", text)

    def test_booking(self):
        for text in ["I want to book an appointment", "book karna hai", "do you have a slot tomorrow?"]:
            self.assertEqual(detect_intent(text), "booking", text)

    def test_location(self):
        for text in ["where are you located?", "kahan ho aap", "address kya hai"]:
            self.assertEqual(detect_intent(text), "location", text)

    def test_human(self):
        self.assertEqual(detect_intent("can I talk to a human please"), "human")

    def test_thanks(self):
        self.assertEqual(detect_intent("thanks a lot!"), "thanks")

    def test_fallback(self):
        for text in ["asdfgh", "", "   "]:
            self.assertEqual(detect_intent(text), "fallback", repr(text))

    def test_question_beats_greeting(self):
        self.assertEqual(detect_intent("hi, what is the price of a facial?"), "price")


if __name__ == "__main__":
    unittest.main()
