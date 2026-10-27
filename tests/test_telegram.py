import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error

from sniper.telegram import AlertStore, DeliveryError, TelegramClient
from telegram_setup import choose_private_chat, connect

TOKEN = "12345:" + "test-token-not-real-" * 2


def reply(data):
    return io.BytesIO(json.dumps(data).encode())


class TelegramTransportTests(unittest.TestCase):
    def test_plain_text_send_checks_actual_destination_and_acknowledgement(self):
        calls = []
        def opener(request, timeout):
            calls.append(json.loads(request.data))
            return reply({"ok": True, "result": {"message_id": 17, "chat": {"id": 42}}})
        client = TelegramClient(TOKEN, opener=opener)
        self.assertEqual(client.send(42, "<b>untrusted token</b>"), 17)
        self.assertNotIn("parse_mode", calls[0])
        self.assertEqual(calls[0]["chat_id"], 42)
        self.assertEqual(calls[0]["text"], "<b>untrusted token</b>")

    def test_timeout_or_mismatched_acknowledgement_is_uncertain_and_redacted(self):
        def failed(*args, **kwargs):
            raise urllib.error.URLError(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        for opener in (failed, lambda *_args, **_kwargs: reply({"ok": True, "result": {"message_id": 2, "chat": {"id": 99}}})):
            with self.assertRaises(DeliveryError) as raised:
                TelegramClient(TOKEN, opener=opener).send(42, "test")
            self.assertTrue(raised.exception.uncertain)
            self.assertNotIn(TOKEN, str(raised.exception))

    def test_rate_limit_has_confirmed_backoff_but_server_failure_is_uncertain(self):
        for code in (429, 503):
            def opener(*args, **kwargs):
                raise urllib.error.HTTPError("private-url", code, TOKEN, {}, reply({"ok": False, "parameters": {"retry_after": 321}}))
            with self.assertRaises(DeliveryError) as raised:
                TelegramClient(TOKEN, opener=opener).send(42, "test")
            self.assertEqual(raised.exception.uncertain, code == 503)
            if code == 429:
                self.assertEqual(raised.exception.retry_after, 321)
            self.assertNotIn(TOKEN, str(raised.exception))


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "alerts.sqlite3"
        self.store = AlertStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.store.enqueue("token-a", {"message": "signal"}, 100)

    def test_success_survives_restart_and_other_pool_cannot_duplicate_token(self):
        class Sender:
            def send(self, chat, text):
                return 12
        self.assertEqual(self.store.deliver(Sender(), 42, lambda _: True, now=100), 1)
        self.store.close()
        self.store = AlertStore(self.path)
        self.assertFalse(self.store.enqueue("token-a", {"message": "different pool"}, 102))
        self.assertEqual(self.store.deliver(Sender(), 42, lambda _: True, now=103), 0)
        self.assertEqual(self.store.counts(), {"sent": 1})

    def test_ambiguous_send_is_never_automatically_retried(self):
        class Sender:
            calls = 0
            def send(self, chat, text):
                self.calls += 1
                raise DeliveryError("timeout", uncertain=True)
        sender = Sender()
        self.store.deliver(sender, 42, lambda _: True, now=100)
        self.store.deliver(sender, 42, lambda _: True, now=1000)
        self.assertEqual(sender.calls, 1)
        self.assertEqual(self.store.counts(), {"uncertain": 1})
        self.store.resolve("token-a", "received")
        self.assertFalse(self.store.enqueue("token-a", {"message": "again"}, 1001))

    def test_interrupted_send_becomes_uncertain_after_restart(self):
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET state='sending'")
        self.store.close()
        self.store = AlertStore(self.path)
        self.assertEqual(self.store.counts(), {"uncertain": 1})

    def test_confirmed_rejection_retries_only_after_backoff(self):
        class Sender:
            calls = 0
            def send(self, chat, text):
                self.calls += 1
                if self.calls == 1:
                    raise DeliveryError("rate limit", retry_after=300)
                return 19
        sender = Sender()
        self.store.deliver(sender, 42, lambda _: True, now=100)
        self.store.deliver(sender, 42, lambda _: True, now=399)
        self.assertEqual(sender.calls, 1)
        self.store.deliver(sender, 42, lambda _: True, now=400)
        self.assertEqual(self.store.counts(), {"sent": 1})

    def test_expired_or_revoked_evidence_is_suppressed_before_send(self):
        class Sender:
            def send(self, *_):
                raise AssertionError("Must not send")
        self.store.deliver(Sender(), 42, lambda _: False, now=100)
        self.assertEqual(self.store.counts(), {"suppressed": 1})

    def test_provider_failure_keeps_pending_and_recipient_cannot_silently_change(self):
        self.store.bind(1, 42)
        with self.assertRaises(ValueError):
            self.store.bind(1, 99)
        def fail(_):
            raise RuntimeError("RPC unavailable")
        with self.assertRaises(RuntimeError):
            self.store.deliver(None, 42, fail, now=100)
        self.assertEqual(self.store.counts(), {"pending": 1})


class PairingTests(unittest.TestCase):
    def update(self, chat=42, timestamp=100):
        return {"message": {"text": "SNIPER SETUP", "date": timestamp,
                            "chat": {"id": chat, "type": "private"}, "from": {"id": chat, "is_bot": False}}}

    def test_pairing_requires_one_recent_explicit_private_recipient(self):
        self.assertEqual(choose_private_chat([self.update(), self.update()], 101), 42)
        for updates in ([], [self.update(), self.update(99)], [self.update(timestamp=0)]):
            with self.assertRaises(ValueError):
                choose_private_chat(updates, 1901)
        group = self.update()
        group["message"]["chat"]["type"] = "group"
        with self.assertRaises(ValueError):
            choose_private_chat([group], 101)

    def test_wrong_bot_or_existing_webhook_does_not_save_recipient(self):
        class Client:
            def call(self, method, payload=None):
                if method == "getMe":
                    return {"id": 1, "username": "CorrectBot", "is_bot": True}
                return {"url": "https://existing.example/webhook"}
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            for expected in ("WrongBot", "CorrectBot"):
                with self.assertRaises(ValueError):
                    connect(runtime, expected, client=Client())
            self.assertFalse((runtime / "telegram-recipient.json").exists())

    def test_pairing_test_is_acknowledged_and_not_sent_again_on_rerun(self):
        update = self.update(timestamp=int(time.time()))
        class Client:
            sends = 0
            def call(self, method, payload=None):
                return {"getMe": {"id": 1, "username": "CorrectBot", "is_bot": True},
                        "getWebhookInfo": {"url": ""}, "getUpdates": [update]}[method]
            def send(self, chat, message):
                self.sends += 1
                self.last_chat = chat
                return 123
        with tempfile.TemporaryDirectory() as folder:
            client = Client()
            first = connect(Path(folder), "@CorrectBot", client=client, send_test=True)
            second = connect(Path(folder), "CorrectBot", client=client, send_test=True)
            self.assertEqual(first["test_state"], "sent")
            self.assertEqual(second["test_message_id"], 123)
            self.assertEqual(client.sends, 1)
            self.assertEqual(client.last_chat, 42)
