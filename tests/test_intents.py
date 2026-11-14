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

    def test_human_beats_booking(self):
        self.assertEqual(
            detect_intent("let me talk to a person about my booking"), "human"
        )

    def test_talk_about_prices_is_price(self):
        self.assertEqual(detect_intent("talk to me about prices"), "price")

    def test_hinglish_human_requests(self):
        for text in [
            "kisi se baat karni hai",
            "manager se baat karni hai",
            "agent se baat karao",
            "speak to someone please",
        ]:
            self.assertEqual(detect_intent(text), "human", text)

    def test_reschedule(self):
        for text in [
            "reschedule my booking",
            "I want to reschedule",
            "move my booking to tomorrow",
            "change my appointment",
        ]:
            self.assertEqual(detect_intent(text), "reschedule", text)

    def test_cancel_still_cancel(self):
        self.assertEqual(detect_intent("cancel my booking"), "cancel")

    def test_offer(self):
        for text in ["any offers?", "is there a discount?", "any deals today?"]:
            self.assertEqual(detect_intent(text), "offer", text)

    def test_devanagari_price(self):
        self.assertEqual(detect_intent("फेशियल की कीमत क्या है?"), "price")

    def test_devanagari_booking(self):
        self.assertEqual(detect_intent("बुक करना है"), "booking")
        self.assertEqual(detect_intent("अपॉइंटमेंट चाहिए"), "booking")

    def test_devanagari_greeting_and_human(self):
        self.assertEqual(detect_intent("नमस्ते"), "greeting")
        self.assertEqual(detect_intent("मैनेजर से बात करनी है"), "human")

    def test_devanagari_hours(self):
        self.assertEqual(detect_intent("कब खुला है?"), "hours")
        self.assertEqual(detect_intent("आज बंद है क्या?"), "hours")

    def test_fallback(self):
        for text in ["asdfgh", "", "   "]:
            self.assertEqual(detect_intent(text), "fallback", repr(text))

    def test_question_beats_greeting(self):
        self.assertEqual(detect_intent("hi, what is the price of a facial?"), "price")


if __name__ == "__main__":
    unittest.main()
