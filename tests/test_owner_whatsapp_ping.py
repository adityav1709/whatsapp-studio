"""Owner WhatsApp ping on handoff: a Meta template message to the
owner's personal number, Telegram kept as fallback through the
fan-out. No real network calls: a fake sender records the template
payload, and a fake HTTPError stands in for Meta's 132000 when the
template is not approved yet."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from test_live_send import FakeSender, make_profile
from test_llm import FixedLLM
from whatsapp_studio.app import Studio
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.notify import (
    FanoutNotifier,
    NullNotifier,
    TelegramNotifier,
    WhatsAppNotifier,
    notifier_from_env,
)

OWNER = "919876543210"
TEMPLATE = "owner_handoff_alert"


def template_error(code=132000):
    """A Meta error body as a real HTTPError would carry it."""
    body = json.dumps(
        {"error": {"code": code, "message": "Template does not exist"}}
    ).encode("utf-8")
    return HTTPError(
        "https://graph.facebook.com/v21.0/123/messages",
        400,
        "Bad Request",
        {},
        io.BytesIO(body),
    )


@contextlib.contextmanager
def studio_env(**overrides):
    """os.environ with every notifier var removed except the ones the
    test sets, so a test never inherits a real token from the shell."""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("TELEGRAM_", "OWNER_", "WHATSAPP_")):
            del env[key]
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=True):
        yield


def make_studio(tmp, sender=None, shop_id=None, demo_mode=False, dry_run=False):
    """A Studio with no explicit notifier, so the env fallback builds
    the real notifier from the patched environment."""
    return Studio(
        make_profile(),
        LeadStore(Path(tmp) / "leads.json"),
        handoffs=HandoffStore(),
        sender=sender,
        dry_run=dry_run,
        demo_mode=demo_mode,
        shop_id=shop_id,
        llm=FixedLLM("Facial is Rs 1499 for 60 minutes."),
    )


def checklist_item(items, label):
    for item_label, ok, hint in items:
        if item_label.startswith(label):
            return ok, hint
    return None, None


class RecordingNotifier:
    """Stand-in for a channel: records handoffs, never touches a network."""

    def __init__(self):
        self.calls = []

    def notify(self, business_name, handoff):
        self.calls.append((business_name, handoff))

    def notify_booking(self, business_name, booking):
        return None

    def notify_send_failed(self, business_name, entry):
        return None

    def notify_onboard(self, profile, seen_before=False):
        return None


class TemplatePayloadTest(unittest.TestCase):
    def test_template_payload_shape_and_truncation(self):
        sender = FakeSender()
        with tempfile.TemporaryDirectory() as tmp, studio_env(
            OWNER_WHATSAPP_NUMBER=OWNER, OWNER_TEMPLATE_NAME=TEMPLATE
        ):
            studio = make_studio(tmp, sender=sender)
            self.assertIsInstance(studio.notifier, WhatsAppNotifier)
            trigger = "can I talk to a person please, this is urgent " * 10
            handoff = studio.handoffs.open(
                "919999999998", "Priya", "asked for a person", trigger
            )
            studio.notifier.notify("Glow & Grace Salon", handoff)
        self.assertEqual(
            sender.template_calls,
            [
                (
                    OWNER,
                    TEMPLATE,
                    "en_US",
                    [
                        "Glow & Grace Salon",
                        "Priya (919999999998)",
                        "asked for a person",
                        trigger[:200],
                    ],
                )
            ],
        )


class UnapprovedTemplateTest(unittest.TestCase):
    def test_unapproved_template_swallowed_logged_and_handoff_stays_open(self):
        sender = FakeSender(fail_template_with=template_error())
        with tempfile.TemporaryDirectory() as tmp, studio_env(
            OWNER_WHATSAPP_NUMBER=OWNER, OWNER_TEMPLATE_NAME=TEMPLATE
        ):
            studio = make_studio(tmp, sender=sender)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                entry = studio.handle_non_text_message(
                    "919999999998", "audio", sender_name="Priya"
                )
                for thread in studio._send_threads:
                    thread.join(timeout=5)
            lines = [line for line in buf.getvalue().splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)
            self.assertIn("132000", lines[0])
            self.assertIn("owner whatsapp ping failed", lines[0])
            self.assertIsInstance(studio.notifier, WhatsAppNotifier)
            self.assertEqual(studio.notifier.last_failure["code"], 132000)
        # The handoff is real, the customer still got their
        # confirmation, and the ping was attempted once.
        self.assertIsNotNone(entry)
        self.assertIsNotNone(studio.handoffs.open_for("919999999998"))
        self.assertTrue(
            any(
                item.get("intent") == "handoff" and item.get("handoff")
                for item in studio.outbox
            )
        )
        self.assertEqual(len(sender.template_calls), 1)
        self.assertEqual(sender.template_calls[0][3][3], "sent a voice note")


class FanoutTest(unittest.TestCase):
    def test_both_channels_receive_the_handoff(self):
        telegram = RecordingNotifier()
        sender = FakeSender()
        whatsapp = WhatsAppNotifier(sender, OWNER, TEMPLATE)
        fanout = FanoutNotifier([telegram, whatsapp])
        handoff = {"phone": "919999999998", "name": "Priya",
                   "reason": "asked for a person", "trigger": "hello"}
        fanout.notify("Glow & Grace Salon", handoff)
        self.assertEqual(len(telegram.calls), 1)
        self.assertEqual(telegram.calls[0][1], handoff)
        self.assertEqual(len(sender.template_calls), 1)

    def test_whatsapp_failure_does_not_stop_telegram(self):
        telegram = RecordingNotifier()
        sender = FakeSender(fail_template_with=template_error())
        whatsapp = WhatsAppNotifier(sender, OWNER, TEMPLATE)
        fanout = FanoutNotifier([telegram, whatsapp])
        handoff = {"phone": "919999999998", "name": "Priya",
                   "reason": "asked for a person", "trigger": "hello"}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fanout.notify("Glow & Grace Salon", handoff)
        self.assertEqual(len(telegram.calls), 1)
        self.assertEqual(len(sender.template_calls), 1)
        self.assertIn("132000", buf.getvalue())

    def test_env_builds_fanout_when_both_channels_are_configured(self):
        with studio_env(
            TELEGRAM_BOT_TOKEN="token",
            TELEGRAM_CHAT_ID="123",
            OWNER_WHATSAPP_NUMBER=OWNER,
            OWNER_TEMPLATE_NAME=TEMPLATE,
        ):
            notifier = notifier_from_env(sender=FakeSender())
        self.assertIsInstance(notifier, FanoutNotifier)
        self.assertTrue(
            any(isinstance(n, TelegramNotifier) for n in notifier.notifiers)
        )
        self.assertTrue(
            any(isinstance(n, WhatsAppNotifier) for n in notifier.notifiers)
        )


class PerShopRoutingTest(unittest.TestCase):
    def test_each_shop_pings_only_its_own_owner(self):
        glow_sender = FakeSender()
        snips_sender = FakeSender()
        with tempfile.TemporaryDirectory() as tmp, studio_env(
            OWNER_WHATSAPP_NUMBER="913333333333",
            OWNER_WHATSAPP_NUMBER_GLOW="911111111111",
            OWNER_WHATSAPP_NUMBER_SNIPS="912222222222",
            OWNER_TEMPLATE_NAME=TEMPLATE,
        ):
            glow = make_studio(tmp, sender=glow_sender, shop_id="glow")
            snips = make_studio(tmp, sender=snips_sender, shop_id="snips")
            glow_handoff = glow.handoffs.open(
                "919999999998", "Priya", "asked for a person", "hello"
            )
            snips_handoff = snips.handoffs.open(
                "919999999997", "Rahul", "asked for a person", "hello"
            )
            glow.notifier.notify("Glow & Grace Salon", glow_handoff)
            snips.notifier.notify("Glow & Grace Salon", snips_handoff)
        self.assertEqual(
            [call[0] for call in glow_sender.template_calls], ["911111111111"]
        )
        self.assertEqual(
            [call[0] for call in snips_sender.template_calls], ["912222222222"]
        )


class NoSendTest(unittest.TestCase):
    def test_no_whatsapp_channel_in_demo_mode(self):
        sender = FakeSender()
        with tempfile.TemporaryDirectory() as tmp, studio_env(
            OWNER_WHATSAPP_NUMBER=OWNER, OWNER_TEMPLATE_NAME=TEMPLATE
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                studio = make_studio(tmp, sender=sender, demo_mode=True)
            self.assertIsInstance(studio.notifier, NullNotifier)
            studio.handle_non_text_message(
                "919999999998", "audio", sender_name="Priya"
            )
        self.assertEqual(sender.template_calls, [])

    def test_dry_run_logs_instead_of_sending(self):
        sender = FakeSender()
        with tempfile.TemporaryDirectory() as tmp, studio_env(
            OWNER_WHATSAPP_NUMBER=OWNER, OWNER_TEMPLATE_NAME=TEMPLATE
        ):
            studio = make_studio(tmp, sender=sender, dry_run=True)
            self.assertIsInstance(studio.notifier, WhatsAppNotifier)
            handoff = studio.handoffs.open(
                "919999999998", "Priya", "asked for a person", "hello"
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                studio.notifier.notify("Glow & Grace Salon", handoff)
        self.assertEqual(sender.template_calls, [])
        self.assertIn("sending is off", buf.getvalue())

    def test_kill_switch_logs_instead_of_sending(self):
        sender = FakeSender()
        with tempfile.TemporaryDirectory() as tmp, studio_env(
            OWNER_WHATSAPP_NUMBER=OWNER, OWNER_TEMPLATE_NAME=TEMPLATE
        ):
            studio = make_studio(tmp, sender=sender)
            studio.disable_sending()
            handoff = studio.handoffs.open(
                "919999999998", "Priya", "asked for a person", "hello"
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                studio.notifier.notify("Glow & Grace Salon", handoff)
        self.assertEqual(sender.template_calls, [])
        self.assertIn("sending is off", buf.getvalue())


class OffByDefaultTest(unittest.TestCase):
    def test_no_owner_vars_means_no_whatsapp_channel(self):
        with tempfile.TemporaryDirectory() as tmp, studio_env():
            studio = make_studio(tmp, sender=FakeSender())
            self.assertIsInstance(studio.notifier, NullNotifier)
            items = studio.setup_checklist()
            ok, _hint = checklist_item(items, "Owner alerts on")
            self.assertFalse(ok)
            self.assertIsNone(checklist_item(items, "Owner WhatsApp ping")[0])

    def test_telegram_alone_still_works_and_hides_the_whatsapp_line(self):
        with tempfile.TemporaryDirectory() as tmp, studio_env(
            TELEGRAM_BOT_TOKEN="token", TELEGRAM_CHAT_ID="123"
        ):
            studio = make_studio(tmp, sender=FakeSender())
            self.assertIsInstance(studio.notifier, TelegramNotifier)
            items = studio.setup_checklist()
            ok, _hint = checklist_item(items, "Owner alerts on")
            self.assertTrue(ok)
            self.assertIsNone(checklist_item(items, "Owner WhatsApp ping")[0])


class SelfSendTest(unittest.TestCase):
    def test_owner_number_equal_to_shop_number_builds_no_channel(self):
        # make_profile's phone is 919999999999: the shop's own number.
        with tempfile.TemporaryDirectory() as tmp, studio_env(
            OWNER_WHATSAPP_NUMBER="919999999999", OWNER_TEMPLATE_NAME=TEMPLATE
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                studio = make_studio(tmp, sender=FakeSender())
            self.assertIsInstance(studio.notifier, NullNotifier)
            self.assertIn("owner whatsapp ping off", buf.getvalue())
            _ok, hint = checklist_item(
                studio.setup_checklist(), "Owner WhatsApp ping"
            )
            self.assertFalse(_ok)
            self.assertIn("own number", hint)


class InvalidNumberTest(unittest.TestCase):
    def test_invalid_number_logs_once_and_builds_no_channel(self):
        with tempfile.TemporaryDirectory() as tmp, studio_env(
            OWNER_WHATSAPP_NUMBER="not-a-number", OWNER_TEMPLATE_NAME=TEMPLATE
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                studio = make_studio(tmp, sender=FakeSender())
            self.assertIsInstance(studio.notifier, NullNotifier)
            self.assertEqual(len(buf.getvalue().splitlines()), 1)
            self.assertIn("owner whatsapp ping off", buf.getvalue())
            _ok, hint = checklist_item(
                studio.setup_checklist(), "Owner WhatsApp ping"
            )
            self.assertFalse(_ok)
            self.assertIn("not a valid", hint)


class ChecklistTest(unittest.TestCase):
    def test_whatsapp_only(self):
        with tempfile.TemporaryDirectory() as tmp, studio_env(
            OWNER_WHATSAPP_NUMBER=OWNER, OWNER_TEMPLATE_NAME=TEMPLATE
        ):
            studio = make_studio(tmp, sender=FakeSender())
            items = studio.setup_checklist()
        ok, _hint = checklist_item(items, "Owner alerts on")
        self.assertTrue(ok)
        ok, _hint = checklist_item(items, "Owner WhatsApp ping to " + OWNER)
        self.assertTrue(ok)

    def test_both_channels(self):
        with tempfile.TemporaryDirectory() as tmp, studio_env(
            TELEGRAM_BOT_TOKEN="token",
            TELEGRAM_CHAT_ID="123",
            OWNER_WHATSAPP_NUMBER=OWNER,
            OWNER_TEMPLATE_NAME=TEMPLATE,
        ):
            studio = make_studio(tmp, sender=FakeSender())
            items = studio.setup_checklist()
        ok, _hint = checklist_item(items, "Owner alerts on")
        self.assertTrue(ok)
        ok, _hint = checklist_item(items, "Owner WhatsApp ping to " + OWNER)
        self.assertTrue(ok)

    def test_whatsapp_configured_but_no_sender(self):
        with tempfile.TemporaryDirectory() as tmp, studio_env(
            OWNER_WHATSAPP_NUMBER=OWNER, OWNER_TEMPLATE_NAME=TEMPLATE
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                studio = make_studio(tmp, sender=None)
            items = studio.setup_checklist()
        ok, hint = checklist_item(items, "Owner WhatsApp ping")
        self.assertFalse(ok)
        self.assertIn("WHATSAPP_TOKEN", hint)

    def test_last_failure_reported_on_the_checklist(self):
        sender = FakeSender(fail_template_with=template_error())
        with tempfile.TemporaryDirectory() as tmp, studio_env(
            OWNER_WHATSAPP_NUMBER=OWNER, OWNER_TEMPLATE_NAME=TEMPLATE
        ):
            studio = make_studio(tmp, sender=sender)
            handoff = studio.handoffs.open(
                "919999999998", "Priya", "asked for a person", "hello"
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                studio.notifier.notify("Glow & Grace Salon", handoff)
            items = studio.setup_checklist()
        ok, hint = checklist_item(items, "Owner WhatsApp ping")
        self.assertFalse(ok)
        self.assertIn("template not approved", hint)


if __name__ == "__main__":
    unittest.main()
