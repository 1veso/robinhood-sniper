# enter.py
# one contract address in, one verdict out. limits, door, Grok, snapshot. first "no" ends it.
import csv, json, os, sys, time
from config import CHECKS, PENDING, MIN_CONFIDENCE, PAPER, TICKET_USD
import book, facts, rpc
from grok import ask
from prompts import ENTRY_PROMPT


def log(row: dict):
    cols = ["time", "mint", "symbol", "stage", "verdict", "confidence", "reason"]
    new = not os.path.exists(CHECKS)
    with open(CHECKS, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k) for k in cols})


def check(mint: str) -> dict:
    if (g := book.guard(mint)):
        return {"stage": "limits", "verdict": "SKIP", "reason": g}
    try:
        t, m = facts.token(mint), facts.pool(mint)
    except Exception as e:
        return {"stage": "door", "verdict": "SKIP", "reason": f"source down: {e}"}
    if (k := facts.door(t, m)):
        return {"stage": "door", "verdict": "SKIP", "reason": k, "symbol": t["symbol"]}
    try:
        ans = ask("entry", ENTRY_PROMPT, {"token": t, "pool": m})
    except Exception as e:
        return {"stage": "grok", "verdict": "SKIP", "reason": f"grok failed: {e}"}
    verdict, conf = ans.get("verdict"), float(ans.get("confidence") or 0)
    if verdict == "BUY" and conf < MIN_CONFIDENCE:
        verdict, ans["why"] = "SKIP", f"low confidence {conf}"
    out = {**ans, "stage": "grok", "verdict": verdict, "confidence": conf,
           "symbol": m["symbol"] or t["symbol"], "reason": ans.get("why"),
           "price": m["price"], "liq_usd": m["liq_usd"]}
    if verdict == "BUY":
        try:
            out["snap"] = rpc.snapshot(t)   # no snapshot, no trade. we can't guard what we can't see
            out["snap"]["x_read"] = ans.get("x_read")
        except Exception as e:
            out.update(verdict="SKIP", stage="rpc", reason=f"snapshot failed: {e}")
    return out


def run(mint: str) -> dict:
    v = check(mint)
    log({"time": time.strftime("%Y-%m-%d %H:%M"), "mint": mint, **v})
    if v["verdict"] == "BUY":
        if PAPER:
            book.open_position(mint, v["symbol"], v["price"], v["liq_usd"], v["snap"])
            v["action"] = f"PAPER BUY ${TICKET_USD} at {v['price']}"
        else:
            pend = json.load(open(PENDING)) if os.path.exists(PENDING) else {}
            pend[mint] = {"symbol": v["symbol"], "liq_usd": v["liq_usd"], "snap": v["snap"]}
            json.dump(pend, open(PENDING, "w"), indent=2)
            v["action"] = (f"APPROVAL NEEDED: buy ${TICKET_USD} of {v['symbol']} in your own wallet, "
                           f"then: python book.py add {mint} {TICKET_USD} <fill price>")
    v.pop("snap", None)
    return v


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1].strip()), indent=2))
