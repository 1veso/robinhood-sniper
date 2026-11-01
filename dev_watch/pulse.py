# pulse.py
# Grok reads X for every token you hold, once an hour. watch.py acts on what it writes.
import datetime, time
from config import PULSE
import book
from grok import ask
from prompts import PULSE_PROMPT

for mint, p in book.load().items():
    if time.time() - p["last_pulse"] < PULSE["every_min"] * 60 - 30:
        continue
    held = datetime.datetime.fromtimestamp(p["opened"], datetime.timezone.utc)
    try:
        r = ask("pulse", PULSE_PROMPT, effort="low", turns=1, days_back=1, payload={
            "symbol": p["symbol"], "mint": mint,
            "held_since": held.strftime("%Y-%m-%d %H:%M UTC"), "x_read_at_entry": p.get("x_read")})
    except Exception as e:
        print(f"{p['symbol']}: pulse failed ({e}), keeping last read: {p['pulse']}")
        continue
    state = r.get("state") if r.get("state") in ("rising", "steady", "fading", "dead") else p["pulse"]
    book.update(mint, pulse=state, last_pulse=time.time())
    if state != p["pulse"]:
        warn = "; ".join(r.get("warnings") or []) or "no warnings"
        print(f"{p['symbol']}: X {p['pulse']} -> {state} | "
              f"{r.get('posts_hour_before')} -> {r.get('posts_last_hour')} posts/h | {warn}")
