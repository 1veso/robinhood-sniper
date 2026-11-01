# scan.py
# leads come from X, not from a paid listing. Grok finds the CAs real accounts are posting.
import csv, os, re
from config import CHECKS
from grok import ask
from prompts import SCAN_PROMPT
import enter

BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")   # a Solana address, or it's not a lead

seen = {r["mint"] for r in csv.DictReader(open(CHECKS))} if os.path.exists(CHECKS) else set()
try:
    leads = ask("scan", SCAN_PROMPT, {"chain": "solana"}, days_back=1, effort="low").get("leads") or []
except Exception as e:
    print(f"scan failed: {e}")
    leads = []

for lead in leads[:8]:
    mint = str(lead.get("mint", "")).strip()
    if not BASE58.match(mint) or mint in seen:
        continue
    v = enter.run(mint)
    if v["verdict"] == "BUY":
        print(f"{v['symbol']}: BUY {v['confidence']:.2f} | {v.get('x_read')} | {v['action']}")
