# rpc.py
# reads wallets straight from Solana. this is how the desk sees the dev move before the chart does.
import requests
from config import RPC_URL


def call(method: str, params: list):
    r = requests.post(RPC_URL, timeout=15,
                      json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(f"{method}: {d['error'].get('message')}")
    return d["result"]


def _ui(acc) -> float:
    return float(acc["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)


def wallet_balance(owner: str, mint: str) -> float:
    """every token account this wallet holds for this mint, summed."""
    res = call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
    return sum(_ui(a["account"]) for a in res["value"])


def balances(token_accounts: list[str]) -> dict[str, float]:
    """one call for all ten. a closed account comes back null, and null means emptied."""
    res = call("getMultipleAccounts", [token_accounts, {"encoding": "jsonParsed"}])
    return {a: (_ui(acc) if acc else 0.0) for a, acc in zip(token_accounts, res["value"])}


def supply(mint: str) -> float:
    return float(call("getTokenSupply", [mint])["value"]["uiAmount"] or 0)


def snapshot(t: dict) -> dict:
    """the wallets at the moment you buy. every watch.py run is a diff against this."""
    total = supply(t["mint"])
    dev0 = wallet_balance(t["creator"], t["mint"])
    holders0 = balances(t["top10_accounts"]) if t["top10_accounts"] else {}
    return {"creator": t["creator"], "dev0": dev0,
            "dev0_pct": round(dev0 / total * 100, 3) if total else 0,
            "holders0": holders0}
