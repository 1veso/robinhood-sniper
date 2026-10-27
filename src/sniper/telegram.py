"""Telegram transport and durable, conservative delivery. No trading APIs."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.request
import uuid


class DeliveryError(RuntimeError):
    def __init__(self, message, *, uncertain=False, retry_after=120):
        super().__init__(message)
        self.uncertain = uncertain
        self.retry_after = retry_after


class TelegramClient:
    def __init__(self, token, *, opener=urllib.request.urlopen):
        if not isinstance(token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Telegram token is missing or malformed")
        self._token, self._opener = token, opener

    def call(self, method, payload=None):
        if method not in {"getMe", "getWebhookInfo", "getUpdates", "sendMessage"}:
            raise ValueError("Unsupported Telegram method")
        sending = method == "sendMessage"
        request = urllib.request.Request(f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(payload or {}, allow_nan=False).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "SNIPER-Monitor/0.1"}, method="POST")
        try:
            with self._opener(request, timeout=10) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("Response too large")
            data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise DeliveryError("Telegram server response uncertain", uncertain=sending) from None
            delay = 120
            try:
                body = json.loads(exc.read(10000))
                value = body.get("parameters", {}).get("retry_after")
                if type(value) is int and 0 < value <= 86400:
                    delay = value
            except (ValueError, OSError, AttributeError):
                pass
            raise DeliveryError(f"Telegram rejected request (HTTP {exc.code})", retry_after=delay) from None
        except (OSError, ValueError):
            # Exception URLs and provider descriptions can include the token.
            raise DeliveryError("Telegram response unavailable or invalid", uncertain=sending) from None
        if not isinstance(data, dict) or type(data.get("ok")) is not bool:
            raise DeliveryError("Telegram acknowledgement malformed", uncertain=sending)
        if not data["ok"]:
            delay = (data.get("parameters") or {}).get("retry_after", 120)
            delay = delay if type(delay) is int and 0 < delay <= 86400 else 120
            code = data.get("error_code")
            raise DeliveryError("Telegram rejected request", uncertain=sending and (type(code) is not int or code >= 500), retry_after=delay)
        return data.get("result")

    def send(self, chat_id, text):
        if type(chat_id) is not int or not isinstance(text, str) or not 1 <= len(text.encode("utf-16-le")) // 2 <= 4096:
            raise ValueError("Invalid Telegram destination or message length")
        result = self.call("sendMessage", {"chat_id": chat_id, "text": text,
                            "link_preview_options": {"is_disabled": True}})
        if (not isinstance(result, dict) or type(result.get("message_id")) is not int
                or not isinstance(result.get("chat"), dict) or result["chat"].get("id") != chat_id):
            raise DeliveryError("Telegram acknowledgement missing or mismatched", uncertain=True)
        return result["message_id"]


class AlertStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS assessments(token TEXT PRIMARY KEY, observed_at INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(
                token TEXT PRIMARY KEY, created_at INTEGER NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt INTEGER NOT NULL DEFAULT 0, message_id INTEGER, last_error TEXT);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('identity',?)", (str(uuid.uuid4()),))
            self.db.execute("UPDATE outbox SET state='uncertain',last_error='Interrupted send; inspect Telegram before resolving' WHERE state='sending'")

    @property
    def identity(self):
        return self.db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()[0]

    def bind(self, bot_id, chat_id):
        value = json.dumps([bot_id, chat_id])
        previous = self.db.execute("SELECT value FROM metadata WHERE key='recipient'").fetchone()
        if previous and previous[0] != value:
            raise ValueError("Alert journal belongs to another Telegram recipient; review before changing it")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('recipient',?)", (value,))

    def assess(self, token, payload, now):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO assessments VALUES (?,?,?)", (token, now, json.dumps(payload, allow_nan=False)))

    def assessment(self, token):
        row = self.db.execute("SELECT payload FROM assessments WHERE token=?", (token,)).fetchone()
        return json.loads(row[0]) if row else None

    def enqueue(self, token, payload, now):
        with self.db:
            return bool(self.db.execute("INSERT OR IGNORE INTO outbox(token,created_at,payload) VALUES (?,?,?)",
                                       (token, now, json.dumps(payload, allow_nan=False))).rowcount)

    def counts(self):
        return dict(self.db.execute("SELECT state,COUNT(*) FROM outbox GROUP BY state").fetchall())

    def resolve(self, token, resolution):
        row = self.db.execute("SELECT state FROM outbox WHERE token=?", (token,)).fetchone()
        if row is None or row[0] != "uncertain":
            raise ValueError("Only an uncertain delivery can be resolved")
        with self.db:
            if resolution == "not-delivered":
                self.db.execute("DELETE FROM outbox WHERE token=?", (token,))
            elif resolution in {"received", "suppress"}:
                self.db.execute("UPDATE outbox SET state=?,last_error='Operator resolved' WHERE token=?",
                                ("sent" if resolution == "received" else "suppressed", token))
            else:
                raise ValueError("Invalid resolution")

    def deliver(self, client, chat_id, validate, *, now=None, limit=3):
        now = int(time.time()) if now is None else now
        sent = 0
        rows = self.db.execute("SELECT * FROM outbox WHERE state='pending' AND next_attempt<=? ORDER BY created_at,token LIMIT ?", (now, limit)).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            # Revalidate time, current watchlist, safety and canonical anchors.
            # Provider failure propagates without sending or removing the item.
            if not validate(payload):
                with self.db:
                    self.db.execute("UPDATE outbox SET state='suppressed',last_error='Evidence expired or invalidated before delivery' WHERE token=?", (row["token"],))
                continue
            with self.db:
                self.db.execute("UPDATE outbox SET state='sending',attempts=attempts+1 WHERE token=?", (row["token"],))
            try:
                message_id = client.send(chat_id, payload["message"])
            except DeliveryError as exc:
                with self.db:
                    self.db.execute("UPDATE outbox SET state=?,next_attempt=?,last_error=? WHERE token=?",
                        ("uncertain" if exc.uncertain else "pending", now + exc.retry_after, str(exc), row["token"]))
                break
            except Exception:
                with self.db:
                    self.db.execute("UPDATE outbox SET state='uncertain',last_error='Unexpected send failure; delivery uncertain' WHERE token=?", (row["token"],))
                break
            else:
                with self.db:
                    self.db.execute("UPDATE outbox SET state='sent',message_id=?,last_error=NULL WHERE token=?", (message_id, row["token"]))
                sent += 1
                break  # At most one Telegram message per two-minute cycle.
        return sent

    def close(self):
        self.db.close()
