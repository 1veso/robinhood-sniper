"""Bounded, cached history acquisition with RPC corroboration and historical costs."""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from .evm import (RpcClient, RpcError, CHAIN_ID, FACTORY, WETH, USDG, SWAP_TOPIC,
                  TRANSFER_TOPIC, address, hash32, quantity, decode_swap,
                  decode_transfer, verify_purchase, token_units, _words, _address_word)


class ResearchPending(RuntimeError):
    pass


class ResearchCache:
    def __init__(self, root: Path, rpc: RpcClient, *, blockscout_key="", max_calls=60, seconds=45):
        self.root, self.rpc, self.key = root, rpc, blockscout_key
        root.mkdir(parents=True, exist_ok=True)
        self.remaining = max_calls
        self.deadline = time.monotonic() + seconds
        self.last_request = 0.0

    def reserve(self):
        if self.remaining <= 0 or time.monotonic() >= self.deadline:
            raise ResearchPending("Cycle budget reached; cached progress will resume")
        self.remaining -= 1
        wait = .35 - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        self.last_request = time.monotonic()

    def cached(self, key, fetch):
        path = self.root / (hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest() + ".json")
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        self.reserve()
        result = fetch()
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
        temp.replace(path)
        return result

    def call(self, method, params, *, anchor):
        return self.cached(["rpc", CHAIN_ID, anchor, method, params], lambda: self.rpc.call(method, params))

    def get(self, url, *, headers=None):
        def fetch():
            if urllib.parse.urlsplit(url).hostname == "api.blockscout.com":
                budget_path = self.root / "blockscout-usage.json"
                day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                usage = json.loads(budget_path.read_text()) if budget_path.exists() else {}
                count = usage.get("requests", 0) if usage.get("day") == day else 0
                if count >= 1000:
                    raise ResearchPending("Daily Blockscout request budget exhausted; resumes next UTC day")
                temporary = budget_path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"day": day, "requests": count + 1}))
                temporary.replace(budget_path)
            req = urllib.request.Request(url, headers={"User-Agent": "SNIPER-Research/0.1", "Accept": "application/json", **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=15) as response:
                    raw = response.read(16_000_001)
                if len(raw) > 16_000_000:
                    raise ValueError("History response exceeds size budget")
                if self.key and self.key.encode() in raw:
                    raise ValueError("Provider response contained credential material; discarded")
                return json.loads(raw)
            except urllib.error.HTTPError as exc:
                raise ResearchPending(f"History provider HTTP {exc.code}; no eligibility inferred") from None
            except urllib.error.URLError:
                raise ResearchPending("History provider connection unavailable") from None
        # Keys remain in the Authorization header, never the URL or cache payload.
        return self.cached(["https", url], fetch)

    def account_history(self, wallet, action, start, end):
        if not self.key:
            raise ResearchPending("BLOCKSCOUT_API_KEY_REQUIRED")
        results, seen = [], set()
        for page in range(1, 101):
            params = {"chain_id": CHAIN_ID, "module": "account", "action": action,
                      "address": address(wallet), "startblock": start, "endblock": end,
                      "sort": "asc", "page": page, "offset": 100}
            raw = self.get("https://api.blockscout.com/v2/api?" + urllib.parse.urlencode(params),
                           headers={"Authorization": "Bearer " + self.key})
            rows = raw.get("result") if isinstance(raw, dict) else None
            if not isinstance(rows, list):
                raise ResearchPending("History provider did not return a transaction list")
            if raw.get("status") not in ("1", 1) and not (not rows and raw.get("message") == "No transactions found"):
                raise ResearchPending("History provider did not confirm a successful response")
            fingerprint = json.dumps(rows, sort_keys=True)
            if rows and fingerprint in seen:
                raise ValueError("Repeated history page; completeness cannot be established")
            seen.add(fingerprint)
            for row in rows:
                block = int(row["blockNumber"])
                if not start <= block <= end:
                    raise ValueError("History row outside frozen block window")
            results.extend(rows)
            if len(rows) < 100:
                return results
        raise ResearchPending("Wallet history exceeds 100-page coverage budget")


def historical_price(series: list, timestamp: int, max_age: int = 7200) -> Decimal:
    points = sorted((int(p[0]) // 1000, Decimal(str(p[1]))) for p in series)
    index = bisect_right([p[0] for p in points], timestamp) - 1
    if index < 0 or timestamp - points[index][0] > max_age:
        raise ValueError("Historical USD price is absent or too old")
    price = points[index][1]
    if not price.is_finite() or price <= 0:
        raise ValueError("Invalid historical USD price")
    return price


def block_at_or_before(cache: ResearchCache, target: int, head: int, anchor: str) -> int:
    low, high = 0, head
    while low < high:
        mid = (low + high + 1) // 2
        block = cache.call("eth_getBlockByNumber", [hex(mid), False], anchor=anchor)
        if not isinstance(block, dict):
            raise ResearchPending("Archive block unavailable")
        if quantity(block["timestamp"]) <= target:
            low = mid
        else:
            high = mid - 1
    return low


def verified_pool(cache: ResearchCache, pool: str, block: int, anchor: str) -> dict | None:
    def call(to, data):
        return cache.call("eth_call", [{"to": to, "data": data}, hex(block)], anchor=anchor)
    t0 = _address_word(_words(call(pool, "0x0dfe1681"), 1)[0])
    t1 = _address_word(_words(call(pool, "0xd21220a7"), 1)[0])
    fee = int(_words(call(pool, "0xddca3f43"), 1)[0], 16)
    if fee >= 2 ** 24:
        raise ValueError("Invalid pool fee")
    data = "0x1698ee82" + t0[2:].zfill(64) + t1[2:].zfill(64) + f"{fee:064x}"
    registered = _address_word(_words(call(FACTORY, data), 1)[0])
    if registered != pool:
        return None
    return {"pool": pool, "token0": t0, "token1": t1, "fee": fee}


def reconstruct_wallet(cache: ResearchCache, wallet: str, snapshot: dict) -> dict:
    """Reconcile every indexed tx/transfer in a frozen 30-day window against RPC.

    Unsupported routes, unknown opening basis, missing historical prices, and
    index truncation block performance qualification. Incoming transfers never
    create profitable free inventory. All fetched evidence is kept in the cache.
    """
    wallet = address(wallet)
    end, cutoff, anchor = snapshot["block"], snapshot["timestamp"], snapshot["hash"]
    start = snapshot["start_block"]
    txs = cache.account_history(wallet, "txlist", start, end)
    # Include the lifetime transfer-index universe when finding boundary
    # holdings. Otherwise a losing token held untouched for the whole window
    # would disappear from the open-inventory assessment.
    all_transfers = cache.account_history(wallet, "tokentx", 0, end)
    transfers = [row for row in all_transfers if int(row["blockNumber"]) >= start]
    internals = cache.account_history(wallet, "txlistinternal", start, end)
    def indexed_hash(row):
        # Blockscout's internal-transaction endpoint uses transactionHash;
        # normal transactions and ERC-20 transfers use hash.
        value = row.get("hash", row.get("transactionHash"))
        if row.get("hash") and row.get("transactionHash") and row["hash"] != row["transactionHash"]:
            raise ValueError("Conflicting indexed transaction identities")
        return hash32(value)
    hashes = sorted({indexed_hash(r) for rows in (txs, transfers, internals) for r in rows})
    gaps, records, funders, counterparties, assets = [], [], set(), set(), set()
    assets.update(address(row["contractAddress"]) for row in all_transfers)
    prices = {}
    for coin, token in (("ethereum", WETH), ("global-dollar", USDG)):
        query = urllib.parse.urlencode({"vs_currency": "usd", "from": cutoff - 31 * 86400, "to": cutoff})
        raw = cache.get(f"https://api.coingecko.com/api/v3/coins/{coin}/market_chart/range?{query}")
        if not isinstance(raw, dict) or not isinstance(raw.get("prices"), list):
            raise ResearchPending("Historical price series unavailable")
        prices[token] = raw["prices"]
    code = cache.call("eth_getCode", [wallet, hex(end)], anchor=anchor)
    if code != "0x":
        gaps.append("contract_wallet_or_delegated_account_not_supported")
    before_nonce = quantity(cache.call("eth_getTransactionCount", [wallet, hex(max(0, start - 1))], anchor=anchor))
    after_nonce = quantity(cache.call("eth_getTransactionCount", [wallet, hex(end)], anchor=anchor))
    receipt_tokens, normalized_by_tx = {}, {}
    for tx_hash in hashes:
        tx = cache.call("eth_getTransactionByHash", [tx_hash], anchor=anchor)
        receipt = cache.call("eth_getTransactionReceipt", [tx_hash], anchor=anchor)
        if not isinstance(tx, dict) or not isinstance(receipt, dict):
            raise ResearchPending("Transaction or receipt unavailable")
        block = quantity(receipt["blockNumber"])
        if not start <= block <= end or hash32(tx["hash"]) != tx_hash or hash32(receipt["transactionHash"]) != tx_hash:
            raise ValueError("History/RPC transaction mismatch")
        header = cache.call("eth_getBlockByNumber", [hex(block), False], anchor=anchor)
        if hash32(header["hash"]) != hash32(receipt["blockHash"]) or hash32(tx["blockHash"]) != hash32(header["hash"]):
            raise ValueError("History/RPC canonical block mismatch")
        timestamp = quantity(header["timestamp"])
        sender = address(tx["from"])
        recipient = address(tx["to"]) if tx.get("to") else None
        mine = sender == wallet
        gas_usd = Decimal(0)
        if mine:
            try:
                gas_usd = token_units(quantity(receipt["gasUsed"]) * quantity(receipt["effectiveGasPrice"]), 18) * historical_price(prices[WETH], timestamp)
            except (KeyError, ValueError):
                gaps.append("missing_gas_cost:" + tx_hash)
        record = {"id": tx_hash, "timestamp": timestamp, "block_number": block,
                  "transaction_index": quantity(receipt["transactionIndex"]),
                  "kind": "GAS", "gas_usd": str(gas_usd), "verified": False}
        if quantity(receipt["status"]) != 1:
            records.append(record)
            continue
        if recipient == wallet and sender != wallet and quantity(tx.get("value", "0x0")) > 0:
            funders.add(sender)
        if mine and recipient and quantity(tx.get("value", "0x0")) > 0:
            counterparties.add(recipient)
        token_flows = []
        swaps = []
        for raw in receipt.get("logs", []):
            topic = (raw.get("topics") or [None])[0]
            if topic == TRANSFER_TOPIC and len(raw.get("topics", [])) == 3:
                transfer = decode_transfer(raw)
                if wallet in (transfer["from"], transfer["to"]):
                    token_flows.append(transfer)
                    assets.add(transfer["address"])
            elif topic == SWAP_TOPIC:
                swaps.append(decode_swap(raw))
        receipt_tokens[tx_hash] = token_flows
        supported = []
        if mine:
            for swap in swaps:
                try:
                    pool = verified_pool(cache, swap["address"], block, anchor)
                except (RpcError, ValueError):
                    gaps.append("pool_verification_unavailable:" + tx_hash)
                    continue
                if not pool:
                    continue
                bases = {WETH: "WETH", USDG: "USDG"}
                buy = verify_purchase(swap, pool, receipt, tx, wallet, bases)
                if buy:
                    supported.append(("BUY", buy))
                    continue
                # Reverse the base/token roles to prove a sell using the same
                # exact direct-transfer and whole-transaction net-flow checks.
                nonbases = {x: "token" for x in (pool["token0"], pool["token1"]) if x not in bases}
                sell = verify_purchase(swap, pool, receipt, tx, wallet, nonbases)
                if sell and sell["token_address"] in bases:
                    supported.append(("SELL", {"token_address": sell["quote_address"],
                                      "quote_address": sell["token_address"],
                                      "amount_raw": sell["spent_raw"], "spent_raw": sell["amount_raw"]}))
        if len(supported) == 1 and len(swaps) == 1:
            side, trade = supported[0]
            token, base = trade["token_address"], trade["quote_address"]
            try:
                def decimals(token):
                    raw = cache.call("eth_call", [{"to": token, "data": "0x313ce567"}, hex(block)], anchor=anchor)
                    return int(_words(raw, 1)[0], 16)
                units = token_units(int(trade["amount_raw"]), decimals(token))
                value = token_units(int(trade["spent_raw"]), decimals(base)) * historical_price(prices[base], timestamp)
                record.update(kind=side, token=token, quantity=str(units), value_usd=str(value), verified=True)
            except (RpcError, ValueError):
                gaps.append("trade_valuation_unavailable:" + tx_hash)
        elif token_flows or (mine and swaps):
            record.update(kind="TRANSFER", tokens=sorted({t["address"] for t in token_flows if t["address"] not in (WETH, USDG)}))
            if mine and swaps:
                gaps.append("unsupported_swap_route:" + tx_hash)
            if mine and token_flows:
                gaps.append("unsupported_token_transaction:" + tx_hash)
            for t in token_flows:
                if t["address"] in (WETH, USDG):
                    if t["to"] == wallet and t["from"] != wallet:
                        funders.add(t["from"])
                    if t["from"] == wallet and t["to"] != wallet:
                        counterparties.add(t["to"])
        records.append(record)
        normalized_by_tx[tx_hash] = record
        if mine and quantity(tx.get("value", "0x0")) > 0:
            gaps.append("native_value_transaction_needs_trace_review:" + tx_hash)
    # Reconcile the index's token rows with actual receipt logs, then reconcile
    # net flows against boundary balanceOf values to expose omitted transfers.
    outgoing = sum(1 for tx_hash in hashes if address(cache.call("eth_getTransactionByHash", [tx_hash], anchor=anchor)["from"]) == wallet)
    if after_nonce - before_nonce != outgoing:
        gaps.append("outgoing_transaction_history_does_not_reconcile_with_nonce")
    for row in transfers:
        token = address(row["contractAddress"])
        tx_hash = hash32(row["hash"])
        if not any(t["address"] == token and t["from"] == address(row["from"]) and
                   t["to"] == address(row["to"]) and int(t["amount_raw"]) == int(row["value"])
                   for t in receipt_tokens.get(tx_hash, [])):
            gaps.append("index_transfer_not_corroborated:" + tx_hash)
    opening = []
    for token in sorted(assets):
        data = "0x70a08231" + wallet[2:].zfill(64)
        try:
            before = int(_words(cache.call("eth_call", [{"to": token, "data": data}, hex(max(0, start - 1))], anchor=anchor), 1)[0], 16)
            after = int(_words(cache.call("eth_call", [{"to": token, "data": data}, hex(end)], anchor=anchor), 1)[0], 16)
            net = sum((int(t["amount_raw"]) * ((t["to"] == wallet) - (t["from"] == wallet))
                       for flows in receipt_tokens.values() for t in flows if t["address"] == token), 0)
            if before + net != after:
                gaps.append("token_balance_history_does_not_reconcile:" + token)
            if before and token not in (WETH, USDG):
                opening.append(token)
                gaps.append("opening_token_inventory_has_unknown_basis:" + token)
        except (RpcError, ValueError):
            gaps.append("boundary_balance_unavailable:" + token)
    # Internal transfers need traces for independent corroboration. Keep them as
    # conservative suspected links, and mark controller coverage incomplete.
    for row in internals:
        if row.get("isError") == "0" and int(row.get("value", 0)) > 0:
            if address(row["to"]) == wallet:
                funders.add(address(row["from"]))
            if address(row["from"]) == wallet:
                counterparties.add(address(row["to"]))
    records.sort(key=lambda r: (r["timestamp"], r["block_number"], r["transaction_index"], r["id"]))
    return {"wallet": wallet, "cutoff": cutoff, "from_block": start, "to_block": end,
            "complete": not gaps, "gaps": sorted(set(gaps)), "records": records,
            "opening_inventory": opening, "funders": sorted(funders),
            "counterparties": sorted(counterparties), "is_eoa": code == "0x",
            "controller_coverage_complete": not internals and not gaps,
            "source": "Blockscout indexed window + RPC receipts and balances + historical CoinGecko ETH/USDG",
            "coverage": "30-day Robinhood Chain window; supported direct factory swaps only; no cross-chain ownership proof"}
