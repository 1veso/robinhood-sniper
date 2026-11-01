# facts.py
# the door. one rugcheck report and one dexscreener read per token, then plain comparisons.
import time
import requests
from config import ENTRY, TICKET_USD

RC = "https://api.rugcheck.xyz/v1/tokens"
DS = "https://api.dexscreener.com/tokens/v1/solana"


def token(mint: str) -> dict:
    r = requests.get(f"{RC}/{mint}/report", timeout=20)
    r.raise_for_status()
    d = r.json()
    t = d.get("token") or {}
    known = d.get("knownAccounts") or {}

    def is_pool(h):  # pool vaults and lockers sit in top holders. they are not whales
        tag = known.get(h.get("owner")) or known.get(h.get("address")) or {}
        return str(tag.get("type", "")).upper() in ("AMM", "LOCKER")

    holders = [h for h in (d.get("topHolders") or []) if not is_pool(h)]
    markets = sorted(d.get("markets") or [], reverse=True,
                     key=lambda m: ((m.get("lp") or {}).get("baseUSD") or 0)
                                 + ((m.get("lp") or {}).get("quoteUSD") or 0))
    lp = (markets[0].get("lp") or {}) if markets else {}
    supply = t.get("supply") or 0
    risks = d.get("risks") or []
    return {
        "mint": mint,
        "symbol": (d.get("tokenMeta") or {}).get("symbol"),
        "creator": d.get("creator"),
        "rugged": bool(d.get("rugged")),
        "risk_score": d.get("score_normalised"),
        "danger": [x.get("name") for x in risks if x.get("level") == "danger"],
        "warn": [x.get("name") for x in risks if x.get("level") == "warn"],
        "mint_authority": t.get("mintAuthority", d.get("mintAuthority")),
        "freeze_authority": t.get("freezeAuthority", d.get("freezeAuthority")),
        "lp_locked_pct": lp.get("lpLockedPct"),
        "top10_pct": round(sum(h.get("pct") or 0 for h in holders[:10]), 2) if holders else None,
        "top10_accounts": [h.get("address") for h in holders[:10] if h.get("address")],
        "dev_pct": round((d.get("creatorBalance") or 0) / supply * 100, 2) if supply else None,
        "insiders": sum(1 for h in holders if h.get("insider")) + (d.get("graphInsidersDetected") or 0),
    }


def pool(mint: str) -> dict | None:
    r = requests.get(f"{DS}/{mint}", timeout=15)
    r.raise_for_status()
    data = r.json()
    pairs = data if isinstance(data, list) else (data.get("pairs") or [])
    pairs = [p for p in pairs if (p.get("baseToken") or {}).get("address") == mint]
    if not pairs:
        return None
    p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
    h1 = (p.get("txns") or {}).get("h1") or {}
    born = p.get("pairCreatedAt")
    return {
        "symbol": (p.get("baseToken") or {}).get("symbol"),
        "price": float(p.get("priceUsd") or 0),
        "liq_usd": (p.get("liquidity") or {}).get("usd") or 0,
        "age_min": round((time.time() * 1000 - born) / 60000) if born else None,
        "buys_1h": h1.get("buys", 0),
        "sells_1h": h1.get("sells", 0),
    }


def door(t: dict, m: dict | None) -> str | None:
    """first failure wins and gets logged. a missing field is a kill, never a pass."""
    if t["risk_score"] is None:                                    return "no_report"
    if t["rugged"]:                                                return "already_rugged"
    if t["mint_authority"]:                                        return "mint_open"
    if t["freeze_authority"]:                                      return "freeze_open"
    if t["danger"]:                                                return "danger:" + t["danger"][0]
    if t["risk_score"] > ENTRY["max_risk_score"]:                  return "risk_score"
    if (t["lp_locked_pct"] or 0) < ENTRY["min_lp_locked_pct"]:     return "lp_unlocked"
    if t["top10_pct"] is None or t["top10_pct"] > ENTRY["max_top10_pct"]:   return "top10_heavy"
    if t["dev_pct"] is None or t["dev_pct"] > ENTRY["max_dev_pct"]:         return "dev_holds"
    if t["insiders"] > ENTRY["max_insiders"]:                      return "insiders"
    if not t["creator"]:                                           return "no_creator"
    if not m or m["price"] <= 0:                                   return "no_pool"
    if m["liq_usd"] < ENTRY["min_liquidity_usd"]:                  return "thin_pool"
    if TICKET_USD / m["liq_usd"] > ENTRY["max_ticket_share"]:      return "ticket_too_big"
    if m["age_min"] is None or m["age_min"] < ENTRY["min_age_min"]:          return "too_fresh"
    if m["age_min"] > ENTRY["max_age_hours"] * 60:                 return "too_old"
    if m["buys_1h"] >= 20 and m["sells_1h"] == 0:                  return "nobody_can_sell"
    return None
