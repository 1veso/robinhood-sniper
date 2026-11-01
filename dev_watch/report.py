# report.py
# the evening read. everything comes from the csv files, nothing from the bot's memory.
import csv, os, time
from collections import Counter
from config import CHECKS, COSTS, PAPER
import book


def rows(path, day):
    return [r for r in csv.DictReader(open(path)) if r["time"].startswith(day)] if os.path.exists(path) else []


day = time.strftime("%Y-%m-%d")
checks, costs = rows(CHECKS, day), rows(COSTS, day)
exits = [t for t in book.trades() if t["time"].startswith(day) and t["action"] != "open"]
stage = Counter(r["stage"] for r in checks if r["verdict"] == "SKIP")
why_out = Counter(t["reason"] for t in exits)
pnl, spend = book.today_pnl(), sum(float(c["usd"]) for c in costs)

print(f"DESK {day} {'PAPER' if PAPER else 'LIVE'}")
print(f"checked {len(checks)} | door killed {stage['door']} | grok skipped {stage['grok']} | "
      f"limits {stage['limits']} | bought {sum(r['verdict'] == 'BUY' for r in checks)}")
print(f"exits: " + (", ".join(f"{k} {n}" for k, n in why_out.most_common()) or "none"))
print(f"realized ${pnl:+.2f} | grok ${spend:.2f} "
      f"({sum(int(c['x_posts']) for c in costs)} X posts read) | net ${pnl - spend:+.2f}")
print(f"open now: {', '.join(p['symbol'] + ' ' + p['pulse'] for p in book.load().values()) or 'nothing'}")
