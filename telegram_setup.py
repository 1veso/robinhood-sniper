"""Pair a local bot with the user's explicit private-chat setup message."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from sniper.telegram import AlertStore, TelegramClient
from supervise import atomic_json, instance_lock


def choose_private_chat(updates, now):
    if not isinstance(updates, list):
        raise ValueError("Telegram updates are malformed")
    chats = set()
    for update in updates:
        message = update.get("message", {}) if isinstance(update, dict) else {}
        chat, sender = message.get("chat", {}), message.get("from", {})
        date = message.get("date")
        if (message.get("text", "").strip() == "SNIPER SETUP" and chat.get("type") == "private"
                and type(chat.get("id")) is int and chat["id"] > 0
                and sender.get("id") == chat["id"] and sender.get("is_bot") is False
                and type(date) is int and 0 <= now - date <= 1800):
            chats.add(chat["id"])
    if len(chats) != 1:
        raise ValueError("Need exactly one recent private SNIPER SETUP message; ambiguous recipients are never selected")
    return chats.pop()


def connect(runtime, expected_bot, *, send_test=False, client=None):
    if not expected_bot or not expected_bot.lstrip("@").strip():
        raise ValueError("Provide the intended bot username with --expected-bot")
    client = client or TelegramClient((runtime / "telegram-bot-token.txt").read_text(encoding="utf-8-sig").strip())
    me = client.call("getMe")
    if (not isinstance(me, dict) or me.get("is_bot") is not True or type(me.get("id")) is not int
            or str(me.get("username", "")).lower() != expected_bot.lstrip("@").lower()):
        raise ValueError("Saved token does not belong to the expected bot")
    webhook = client.call("getWebhookInfo")
    if not isinstance(webhook, dict) or webhook.get("url"):
        raise ValueError("Bot has an existing webhook or unknown webhook state; use a dedicated bot")
    updates = client.call("getUpdates", {"timeout": 0, "limit": 100})
    now = int(time.time())
    chat_id = choose_private_chat(updates, now)
    recipient = {"bot_id": me["id"], "bot_username": me["username"], "chat_id": chat_id}
    path = runtime / "telegram-recipient.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8-sig")) != recipient:
        raise ValueError("A different recipient is already paired; review before replacing it")
    folder = runtime / "telegram"
    folder.mkdir(parents=True, exist_ok=True)
    with instance_lock(folder / "monitor.lock"), ExitStack() as resources:
        if (folder / "identity.json").exists() and not (folder / "alerts.sqlite3").is_file():
            raise ValueError("Alert journal is missing; restore it before pairing")
        store = AlertStore(folder / "alerts.sqlite3")
        resources.callback(store.close)
        if (folder / "identity.json").exists():
            marker = json.loads((folder / "identity.json").read_text())
            if marker.get("alerts") != store.identity:
                raise ValueError("Alert journal identity differs from established state")
        store.bind(me["id"], chat_id)
        atomic_json(path, recipient)
        key = f"connection-test:{me['id']}:{chat_id}"
        if send_test:
            store.enqueue(key, {"kind": "connection_test", "created_at": now, "expires_at": now + 300,
                "message": "SNIPER connection test\nTelegram is connected to your local PAPER / RESEARCH monitor.\n"
                           "This is a setup message, not a market signal. No trade was placed.\n"
                           "Market alerts require current safety evidence and a verified purchase by a reviewed wallet.\n"
                           "The Windows monitor runs every two minutes while this computer is awake and you are signed in."}, now)
            store.deliver(client, chat_id, lambda p: p.get("kind") == "connection_test" and int(time.time()) <= p["expires_at"])
        row = store.db.execute("SELECT state,message_id FROM outbox WHERE token=?", (key,)).fetchone()
        result = {"bot_username": me["username"], "paired": True, "test_requested": send_test,
                  "test_state": row["state"] if row else "NOT_SENT", "test_message_id": row["message_id"] if row else None}
        atomic_json(folder / "setup-status.json", result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-bot", required=True)
    parser.add_argument("--send-test", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = connect(ROOT / "runtime", args.expected_bot, send_test=args.send_test)
    except Exception as exc:
        print(json.dumps({"status": "SETUP_PENDING", "error_type": type(exc).__name__,
            "next_step": "Check saved token, intended bot username, and one private SNIPER SETUP message sent within 30 minutes. Existing webhook or different paired recipient is not overwritten."}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
