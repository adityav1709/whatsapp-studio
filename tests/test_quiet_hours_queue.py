"""Quiet-hours queue: a handoff opened after hours holds the owner
ping for opening, then one morning message and one nudge. No real
network: a fake sender records the template payloads, a recording
Telegram stands in for the bot API, and every time is a pinned IST
datetime."""
import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from test_live_send import FakeSender, make_profile
from test_llm import FixedLLM
from test_owner_whatsapp_ping import template_error
from whatsapp_studio.app import Studio, make_server
from whatsapp_studio.bookings import IST, next_opening
from whatsapp_studio.config import BusinessProfile, Service
from whatsapp_studio.handoffs import HandoffStore
from whatsapp_studio.leads import LeadStore
from whatsapp_studio.notify import (
    FanoutNotifier,
    NullNotifier,
    TelegramNotifier,
    WhatsAppNotifier,
)
from whatsapp_studio.pending_pings import PendingPingStore

OWNER = "919876543210"
TEMPLATE = "owner_handoff_alert"

# Friday Nov 27 2026, after the 8pm close; Saturday opens 10am.
FRI_1100 = datetime(2026, 11, 27, 11, 0, tzinfo=IST)
FRI_2320 = datetime(2026, 11, 27, 23, 20, tzinfo=IST)
FRI_2345 = datetime(2026, 11, 27, 23, 45, tzinfo=IST)
SAT_0930 = datetime(2026, 11, 28, 9, 30, tzinfo=IST)
SAT_1000 = datetime(2026, 11, 28, 10, 0, tzinfo=IST)
SAT_1014 = datetime(2026, 11, 28, 10, 14, tzinfo=IST)
SAT_1015 = datetime(2026, 11, 28, 10, 15, tzinfo=IST)
SAT_1030 = datetime(2026, 11, 28, 10, 30, tzinfo=IST)
SAT_1100 = datetime(2026, 11, 28, 11, 0, tzinfo=IST)
THU_2320 = datetime(2026, 11, 26, 23, 20, tzinfo=IST)
FRI_0200 = datetime(2026, 11, 27, 2, 0, tzinfo=IST)


def _epoch(moment):
    return int(moment.timestamp())


class RecordingTelegram(TelegramNotifier):
    """Stand-in for the bot API: records the text, never touches a network."""

    def __init__(self):
        super().__init__("token", "chat")
        self.sent = []

    def _send(self, text):
        self.sent.append(text)
        return True


def make_studio(tmp, sender=None, telegram=None, hours=None, demo_mode=False,
                notifier=None):
    """A Studio with a fan-out notifier whose WhatsApp half reads the
    shop's live state, the way the env-built one does, so the kill
    switch and dry run are honoured by the queue too."""
    sender = sender if sender is not None else FakeSender()
    telegram = telegram if telegram is not None else RecordingTelegram()
    holder = {}
    if notifier is None:
        whatsapp = WhatsAppNotifier(
            sender, OWNER, TEMPLATE,
            is_live=lambda: holder["studio"].live and not holder["studio"].demo_mode,
        )
        notifier = FanoutNotifier([telegram, whatsapp])
    studio = Studio(
        make_profile() if hours is None else _profile_with_hours(hours),
        LeadStore(Path(tmp) / "leads.json"),
        handoffs=HandoffStore(Path(tmp) / "handoffs.json"),
        sender=sender,
        notifier=notifier,
        pending_pings=PendingPingStore(Path(tmp) / "pending_pings.json"),
        demo_mode=demo_mode,
        llm=FixedLLM("Facial is Rs 1499 for 60 minutes."),
    )
    holder["studio"] = studio
    return studio, sender, telegram


def _profile_with_hours(hours):
    profile = make_profile()
    profile.hours = hours
    return profile


def _hold(studio, phone, name, moment, reason="asked for a person"):
    """Open a handoff at a pinned time and hold its ping."""
    handoff = studio.handoffs.open(
        phone, name, reason, "can I talk to a person please",
        opened_at=_epoch(moment),
    )
    result = studio.notify_handoff(handoff, now=moment)
    return handoff, result


class NextOpeningTest(unittest.TestCase):
    def test_opening_today_before_open(self):
        profile = make_profile()
        self.assertEqual(
            next_opening(profile.hours_today, datetime(2026, 11, 27, 9, 0, tzinfo=IST)),
            datetime(2026, 11, 27, 10, 0, tzinfo=IST),
        )

    def test_opening_tomorrow_after_close(self):
        profile = make_profile()
        self.assertEqual(
            next_opening(profile.hours_today, FRI_2320),
            SAT_1000,
        )

    def test_closed_day_skipped(self):
        profile = _profile_with_hours(
            {"daily": "10:00 AM - 8:00 PM", "sun": "closed"}
        )
        # Saturday 9pm: Sunday is shut, so the next opening is Monday.
        self.assertEqual(
            next_opening(
                profile.hours_today,
                datetime(2026, 11, 28, 21, 0, tzinfo=IST),
            ),
            datetime(2026, 11, 30, 10, 0, tzinfo=IST),
        )

    def test_unparseable_hours_fail_open(self):
        profile = _profile_with_hours({"daily": "whenever"})
        now = datetime(2026, 11, 27, 23, 20, tzinfo=IST)
        self.assertEqual(next_opening(profile.hours_today, now), now)

    def test_no_opening_in_eight_days_returns_none(self):
        profile = _profile_with_hours(
            {day: "closed" for day in
             ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
        )
        self.assertIsNone(next_opening(profile.hours_today, FRI_2320))


class HoldTest(unittest.TestCase):
    def test_after_hours_hold_sends_nothing_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, sender, telegram = make_studio(tmp)
            _handoff, result = _hold(studio, "919999999998", "Priya", FRI_2320)
            self.assertEqual(result, "held")
            self.assertEqual(sender.template_calls, [])
            self.assertEqual(telegram.sent, [])
            saved = json.loads(
                (Path(tmp) / "pending_pings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["phone"], "919999999998")
            self.assertIsNone(saved[0]["ping_sent_at"])
            self.assertFalse(saved[0]["nudged"])

    def test_hold_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, _sender, _telegram = make_studio(tmp)
            handoff, _result = _hold(studio, "919999999998", "Priya", FRI_2320)
            studio.notify_handoff(handoff, now=FRI_2320)
            self.assertEqual(len(studio.pending_pings.all()), 1)

    def test_open_hours_sends_at_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, sender, telegram = make_studio(tmp)
            handoff = studio.handoffs.open(
                "919999999998", "Priya", "asked for a person",
                "can I talk to a person please", opened_at=_epoch(FRI_1100),
            )
            result = studio.notify_handoff(handoff, now=FRI_1100)
            self.assertEqual(result, "sent")
            self.assertEqual(len(sender.template_calls), 1)
            self.assertEqual(len(telegram.sent), 1)
            self.assertIn("Customer needs you", telegram.sent[0])
            self.assertEqual(studio.pending_pings.all(), [])

    def test_demo_mode_never_enqueues(self):
        with tempfile.TemporaryDirectory() as tmp:
            # No notifier at all, the way the env fallback builds a
            # demo install: nothing to send through, nothing to hold.
            studio, _sender, _telegram = make_studio(
                tmp, demo_mode=True, notifier=NullNotifier()
            )
            handoff = studio.handoffs.open(
                "919999999998", "Priya", "asked for a person",
                "can I talk to a person please", opened_at=_epoch(FRI_2320),
            )
            studio.notify_handoff(handoff, now=FRI_2320)
            self.assertEqual(studio.pending_pings.all(), [])

    def test_no_opening_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, sender, telegram = make_studio(
                tmp,
                hours={day: "closed" for day in
                       ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
            )
            _handoff, result = _hold(studio, "919999999998", "Priya", FRI_2320)
            self.assertEqual(result, "held")
            studio.deliver_due_pings(now=FRI_2320 + timedelta(minutes=10))
            self.assertEqual(len(sender.template_calls), 1)
            self.assertEqual(len(telegram.sent), 1)


class DeliveryTest(unittest.TestCase):
    def test_send_at_opening_once_with_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, sender, telegram = make_studio(tmp)
            _hold(studio, "919999999998", "Priya", FRI_2320)
            _hold(studio, "919999999997", "Rahul", FRI_2345)
            studio.deliver_due_pings(now=SAT_0930)
            self.assertEqual(sender.template_calls, [])
            self.assertEqual(telegram.sent, [])
            studio.deliver_due_pings(now=SAT_1000)
            self.assertEqual(len(sender.template_calls), 2)
            reasons = [call[3][2] for call in sender.template_calls]
            self.assertTrue(
                reasons[0].startswith("Waiting since last night at 11:20 PM: "),
                reasons[0],
            )
            self.assertTrue(
                reasons[1].startswith("Waiting since last night at 11:45 PM: "),
                reasons[1],
            )
            self.assertEqual(len(telegram.sent), 1)
            summary = telegram.sent[0]
            self.assertIn("Customers waited overnight at Glow & Grace Salon", summary)
            self.assertLess(summary.index("Priya"), summary.index("Rahul"))
            self.assertIn("waiting since last night at 11:20 PM", summary)
            self.assertIn("waiting since last night at 11:45 PM", summary)
            entries = studio.pending_pings.all()
            self.assertEqual(len(entries), 2)
            self.assertTrue(all(e["ping_sent_at"] is not None for e in entries))

    def test_single_handoff_summary_is_singular(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, _sender, telegram = make_studio(tmp)
            _hold(studio, "919999999998", "Priya", FRI_2320)
            studio.deliver_due_pings(now=SAT_1000)
            self.assertIn("Customer waited overnight at Glow & Grace Salon", telegram.sent[0])

    def test_nudge_once_after_fifteen_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, _sender, telegram = make_studio(tmp)
            _hold(studio, "919999999998", "Priya", FRI_2320)
            studio.deliver_due_pings(now=SAT_1000)
            self.assertEqual(len(telegram.sent), 1)
            studio.deliver_due_pings(now=SAT_1014)
            self.assertEqual(len(telegram.sent), 1)
            studio.deliver_due_pings(now=SAT_1015)
            self.assertEqual(len(telegram.sent), 2)
            self.assertIn("Still waiting, no reply yet", telegram.sent[1])
            self.assertIn("waiting since last night at 11:20 PM", telegram.sent[1])
            self.assertEqual(studio.pending_pings.all(), [])
            studio.deliver_due_pings(now=SAT_1030)
            self.assertEqual(len(telegram.sent), 2)

    def test_resolve_before_opening_sends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, sender, telegram = make_studio(tmp)
            handoff, _result = _hold(studio, "919999999998", "Priya", FRI_2320)
            studio.handoffs.resolve(handoff["id"])
            studio.deliver_due_pings(now=SAT_1000)
            self.assertEqual(sender.template_calls, [])
            self.assertEqual(telegram.sent, [])
            self.assertEqual(studio.pending_pings.all(), [])

    def test_resolve_after_ping_before_nudge_sends_no_nudge(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, _sender, telegram = make_studio(tmp)
            handoff, _result = _hold(studio, "919999999998", "Priya", FRI_2320)
            studio.deliver_due_pings(now=SAT_1000)
            self.assertEqual(len(telegram.sent), 1)
            studio.handoffs.resolve(handoff["id"])
            studio.deliver_due_pings(now=SAT_1015)
            self.assertEqual(len(telegram.sent), 1)
            self.assertEqual(studio.pending_pings.all(), [])

    def test_kill_switch_keeps_entry_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, sender, telegram = make_studio(tmp)
            _hold(studio, "919999999998", "Priya", FRI_2320)
            studio.disable_sending()
            studio.deliver_due_pings(now=SAT_1000)
            self.assertEqual(sender.template_calls, [])
            self.assertEqual(telegram.sent, [])
            self.assertEqual(len(studio.pending_pings.all()), 1)
            self.assertIsNone(studio.pending_pings.all()[0]["ping_sent_at"])
            studio.enable_sending()
            studio.deliver_due_pings(now=SAT_1000)
            self.assertEqual(len(sender.template_calls), 1)
            self.assertEqual(len(telegram.sent), 1)

    def test_restart_still_sends_the_held_ping(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, _sender, _telegram = make_studio(tmp)
            _hold(studio, "919999999998", "Priya", FRI_2320)
            studio2, sender2, telegram2 = make_studio(tmp)
            studio2.deliver_due_pings(now=SAT_1000)
            self.assertEqual(len(sender2.template_calls), 1)
            self.assertEqual(len(telegram2.sent), 1)
            self.assertTrue(
                sender2.template_calls[0][3][2].startswith(
                    "Waiting since last night at 11:20 PM: "
                )
            )

    def test_hours_edit_after_hold_moves_the_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, sender, _telegram = make_studio(tmp)
            _hold(studio, "919999999998", "Priya", FRI_2320)
            profile = make_profile()
            profile.hours = {"daily": "11:00 AM - 8:00 PM"}
            studio.profile = profile
            studio.deliver_due_pings(now=SAT_1000)
            self.assertEqual(sender.template_calls, [])
            studio.deliver_due_pings(now=SAT_1100)
            self.assertEqual(len(sender.template_calls), 1)

    def test_failed_send_drops_and_surfaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = FakeSender(fail_template_with=template_error())
            studio, _sender, telegram = make_studio(tmp, sender=sender)
            _hold(studio, "919999999998", "Priya", FRI_2320)
            studio.deliver_due_pings(now=SAT_1000)
            self.assertEqual(studio.pending_pings.all(), [])
            self.assertEqual(len(telegram.sent), 1)
            whatsapp = next(
                n for n in studio._notifier_children()
                if isinstance(n, WhatsAppNotifier)
            )
            self.assertEqual(whatsapp.last_failure["code"], 132000)
            self.assertIn("template not approved", whatsapp.failure_note())

    def test_whatsapp_only_shop_gets_no_nudge(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = FakeSender()
            whatsapp = WhatsAppNotifier(
                sender, OWNER, TEMPLATE, is_live=lambda: True
            )
            studio, _sender, _telegram = make_studio(
                tmp, sender=sender, notifier=whatsapp
            )
            _hold(studio, "919999999998", "Priya", FRI_2320)
            studio.deliver_due_pings(now=SAT_1000)
            self.assertEqual(len(sender.template_calls), 1)
            studio.deliver_due_pings(now=SAT_1015)
            self.assertEqual(len(sender.template_calls), 1)
            self.assertEqual(studio.pending_pings.all(), [])

    def test_telegram_only_summary_decides(self):
        with tempfile.TemporaryDirectory() as tmp:
            telegram = RecordingTelegram()
            studio, _sender, _telegram = make_studio(
                tmp, telegram=telegram, notifier=telegram
            )
            _hold(studio, "919999999998", "Priya", FRI_2320)
            studio.deliver_due_pings(now=SAT_1000)
            self.assertEqual(len(telegram.sent), 1)
            self.assertIn("Customer waited overnight", telegram.sent[0])
            studio.deliver_due_pings(now=SAT_1015)
            self.assertEqual(len(telegram.sent), 2)
            self.assertIn("Still waiting", telegram.sent[1])
            self.assertEqual(studio.pending_pings.all(), [])


class DashboardTest(unittest.TestCase):
    def test_held_line_and_no_needs_you_now_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, _sender, _telegram = make_studio(tmp)
            _hold(studio, "919999999998", "Priya", THU_2320)
            # 2am, before opening: the ping is held, and the handoff
            # is already older than the 4 hour alert.
            customers = studio.dashboard_html("customers", now=FRI_0200)
            self.assertIn("Owner ping held until opening at 10:00 AM", customers)
            self.assertNotIn("Waiting a long time", customers)
            today = studio.dashboard_html("today", now=FRI_0200)
            self.assertNotIn("Needs you now", today)

    def test_checklist_held_line_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, _sender, _telegram = make_studio(tmp)
            _hold(studio, "919999999998", "Priya", FRI_2320)
            items = dict(
                (label, (ok, hint)) for label, ok, hint in studio.setup_checklist()
            )
            held = [
                (label, ok)
                for label, (ok, _hint) in items.items()
                if label.startswith("Owner ping held")
            ]
            self.assertEqual(
                held, [("Owner ping held until 10:00 AM (1 handoff)", True)]
            )

    def test_new_copy_has_no_dash(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, _sender, telegram = make_studio(tmp)
            _hold(studio, "919999999998", "Priya", FRI_2320)
            _hold(studio, "919999999997", "Rahul", FRI_2345)
            customers = studio.dashboard_html("customers", now=FRI_0200)
            held_line = "Owner ping held until opening at 10:00 AM"
            self.assertIn(held_line, customers)
            checklist_title = next(
                label
                for label, _ok, _hint in studio.setup_checklist()
                if label.startswith("Owner ping held")
            )
            studio.deliver_due_pings(now=SAT_1000)
            studio.deliver_due_pings(now=SAT_1015)
            texts = [held_line, checklist_title] + list(telegram.sent)
            for text in texts:
                self.assertNotIn("-", text)
                self.assertNotIn("\u2013", text)
                self.assertNotIn("\u2014", text)


class HttpSweepTest(unittest.TestCase):
    def test_health_check_fires_the_held_ping(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio, sender, telegram = make_studio(tmp)
            _hold(studio, "919999999998", "Priya", FRI_2320)
            httpd = make_server(studio, port=0, now=SAT_1000)
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health"
                ) as resp:
                    self.assertEqual(resp.status, 200)
            finally:
                httpd.shutdown()
                httpd.server_close()
            self.assertEqual(len(sender.template_calls), 1)
            self.assertEqual(len(telegram.sent), 1)


if __name__ == "__main__":
    unittest.main()
