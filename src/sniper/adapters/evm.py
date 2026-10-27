"""Read-only Robinhood Chain / Uniswap V3 observations.

There are deliberately no signing, send-transaction or order APIs in this module.
A confirmed purchase is evidence of a swap, not evidence of wallet skill, wallet
independence, contract safety, executable liquidity, or a profitable opportunity.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from .evm_store import EvmStore

DEFAULT_RPC = "https://rpc.mainnet.chain.robinhood.com"
CHAIN_ID = 4663
FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS = "0x" + "0" * 40


class RpcError(RuntimeError):
    """An RPC transport, protocol, or server failure. Never means 'safe'."""


class DecodeError(ValueError):
    """Malformed or unsupported ABI data; callers must not infer missing facts."""


class ReorgError(RuntimeError):
    """A reorganisation cannot be reconciled within the retained horizon."""


def address(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise DecodeError("expected a 20-byte EVM address")
    return value.lower()


def hash32(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise DecodeError("expected a 32-byte hash")
    return value.lower()


def quantity(value: Any) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", value):
        raise DecodeError("invalid JSON-RPC hexadecimal quantity")
    return int(value, 16)


def _words(data: Any, count: int | None = None) -> list[str]:
    if not isinstance(data, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{64})*", data):
        raise DecodeError("ABI data must contain complete 32-byte words")
    words = [data[i:i + 64].lower() for i in range(2, len(data), 64)]
    if count is not None and len(words) != count:
        raise DecodeError(f"expected {count} ABI words, got {len(words)}")
    return words


def _address_word(word: str) -> str:
    if len(word) != 64 or word[:24] != "0" * 24:
        raise DecodeError("address word has nonzero upper bytes")
    return address("0x" + word[-40:])


def _signed(word: str, bits: int = 256) -> int:
    value = int(word, 16)
    if bits < 256:
        low = value & ((1 << bits) - 1)
        signed = low - (1 << bits) if low & (1 << (bits - 1)) else low
        if value != signed % (1 << 256):
            raise DecodeError("invalid sign extension in ABI integer")
        return signed
    return value - (1 << 256) if value & (1 << 255) else value


def token_units(raw: int, decimals: int) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise DecodeError("raw token amount must be an integer")
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 36:
        raise DecodeError("unsupported or missing token decimals (supported: 0..36)")
    with localcontext() as ctx:
        ctx.prec = 110
        return Decimal(raw).scaleb(-decimals)


def _finite_positive(value: Any) -> Decimal:
    if isinstance(value, (bool, float)):
        raise DecodeError("amounts must be decimal strings or Decimal, not floats/bools")
    try:
        n = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DecodeError("invalid decimal amount") from exc
    if not n.is_finite() or n <= 0:
        raise DecodeError("amount must be finite and positive")
    return n


class RpcClient:
    READ_METHODS = frozenset({
        "eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_getLogs",
        "eth_getTransactionByHash", "eth_getTransactionReceipt", "eth_getCode", "eth_call",
        "eth_getTransactionCount",
    })

    def __init__(self, url: str = DEFAULT_RPC, *, timeout: float = 15,
                 transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                 max_retries: int = 2, sleeper: Callable[[float], None] = time.sleep) -> None:
        parts = urlparse(url)
        if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("RPC requires HTTPS (HTTP is allowed only for local test servers)")
        if parts.username or parts.password or parts.fragment:
            raise ValueError("RPC URL must not contain user credentials or a fragment")
        if not 0 < timeout <= 60 or not 0 <= max_retries <= 3:
            raise ValueError("invalid timeout or retry limit")
        self.url, self.timeout = url, timeout
        self.transport, self.max_retries, self.sleeper = transport, max_retries, sleeper
        self._id = 0

    def _http(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, allow_nan=False).encode()
        req = urllib.request.Request(self.url, data=body,
                                     headers={"Content-Type": "application/json", "User-Agent": "SNIPER-Paper/0.1"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            raw = response.read(16_000_001)
        if len(raw) > 16_000_000:
            raise RpcError("RPC response exceeds 16 MB; reduce the block range")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise RpcError("RPC returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise RpcError("RPC returned a non-object response")
        return result

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        if method not in self.READ_METHODS:
            raise RpcError(f"read-only RPC refuses method: {method}")
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []}
        for attempt in range(self.max_retries + 1):
            try:
                response = (self.transport or self._http)(payload)
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == self.max_retries:
                    raise RpcError(f"RPC HTTP failure ({exc.code}); observation unavailable") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt == self.max_retries:
                    raise RpcError("RPC connection failed; observation unavailable") from None
            self.sleeper(min(0.25 * (2 ** attempt), 2))
        if not isinstance(response, dict) or response.get("jsonrpc") != "2.0" or response.get("id") != payload["id"]:
            raise RpcError("RPC envelope or response id is invalid")
        if response.get("error") is not None:
            error = response["error"]
            code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
            raise RpcError(f"RPC {method} returned error {code}; observation unavailable")
        if "result" not in response:
            raise RpcError("RPC response has neither result nor usable error")
        return response["result"]

    def verify_chain(self, expected: int = CHAIN_ID) -> int:
        actual = quantity(self.call("eth_chainId"))
        if actual != expected:
            raise RpcError(f"wrong chain: expected {expected}, received {actual}")
        return actual


@dataclass(frozen=True)
class EvmRegistry:
    chain_id: int = CHAIN_ID
    factory: str = FACTORY
    bases: Mapping[str, str] = field(default_factory=lambda: {USDG: "USDG", WETH: "WETH"})
    explorer: str = "https://robinhoodchain.blockscout.com"

    def __post_init__(self) -> None:
        if self.chain_id != CHAIN_ID:
            raise ValueError("this adapter supports only verified Robinhood mainnet chain 4663")
        object.__setattr__(self, "factory", address(self.factory))
        object.__setattr__(self, "bases", {address(k): str(v) for k, v in self.bases.items()})
        if not self.bases:
            raise ValueError("at least one explicitly configured base token is required")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None = None) -> "EvmRegistry":
        return cls(**dict(data or {}))


def _log_common(log: Mapping[str, Any]) -> dict[str, Any]:
    if log.get("removed") is True:
        raise DecodeError("removed log is not canonical evidence")
    return {
        "address": address(log.get("address")),
        "tx_hash": hash32(log.get("transactionHash")),
        "log_index": quantity(log.get("logIndex")),
        "block_number": quantity(log.get("blockNumber")),
        "block_hash": hash32(log.get("blockHash")),
    }


def decode_pool_created(log: Mapping[str, Any]) -> dict[str, Any]:
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) != 4 or hash32(topics[0]) != POOL_CREATED_TOPIC:
        raise DecodeError("not a canonical V3 PoolCreated event")
    t = [hash32(value)[2:] for value in topics[1:]]
    words = _words(log.get("data"), 2)
    token0, token1 = _address_word(t[0]), _address_word(t[1])
    fee, tick_spacing = int(t[2], 16), _signed(words[0], 24)
    pool = _address_word(words[1])  # pool is word 2; word 1 is tickSpacing!
    if token0 >= token1 or token0 == ZERO_ADDRESS or pool == ZERO_ADDRESS:
        raise DecodeError("invalid token ordering or zero address")
    if not 0 < fee < 1_000_000 or not 0 < tick_spacing < 16_384:
        raise DecodeError("unsupported V3 fee or tick spacing")
    return {**_log_common(log), "kind": "pool_created", "token0": token0, "token1": token1,
            "fee": fee, "tick_spacing": tick_spacing, "pool": pool}


def decode_swap(log: Mapping[str, Any]) -> dict[str, Any]:
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) != 3 or hash32(topics[0]) != SWAP_TOPIC:
        raise DecodeError("not a canonical V3 Swap event")
    sender, recipient = (_address_word(hash32(topic)[2:]) for topic in topics[1:])
    words = _words(log.get("data"), 5)
    sqrt_price, liquidity, tick = int(words[2], 16), int(words[3], 16), _signed(words[4], 24)
    if not 0 < sqrt_price < 2 ** 160 or not 0 <= liquidity < 2 ** 128 or not -887272 <= tick <= 887272:
        raise DecodeError("invalid V3 swap state")
    return {**_log_common(log), "kind": "swap", "sender": sender, "recipient": recipient,
            "amount0_raw": str(_signed(words[0])), "amount1_raw": str(_signed(words[1])),
            "sqrt_price_x96": str(sqrt_price), "liquidity_raw": str(liquidity), "tick": tick}


def decode_transfer(log: Mapping[str, Any]) -> dict[str, Any]:
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) != 3 or hash32(topics[0]) != TRANSFER_TOPIC:
        raise DecodeError("not an ERC-20 Transfer event")
    words = _words(log.get("data"), 1)
    return {**_log_common(log), "kind": "transfer", "from": _address_word(hash32(topics[1])[2:]),
            "to": _address_word(hash32(topics[2])[2:]), "amount_raw": str(int(words[0], 16))}


def verify_purchase(swap: Mapping[str, Any], pool: Mapping[str, Any],
                    receipt: Mapping[str, Any], transaction: Mapping[str, Any],
                    wallet: str, base_addresses: Mapping[str, str]) -> dict[str, Any] | None:
    """Prove a direct EOA's *net* base spend and token acquisition in one tx.

    Rejects transfers/airdrops, native-ETH-only routes, delegated smart-wallet
    transactions, reverted transactions, and buys without attributable payment.
    Direct pool routes only: wallet-to-pool base payment and pool-to-wallet
    token receipt must exactly match the swap. Complex routes are unverified.
    Returned raw amounts are whole-transaction net flows, not summed per pool.
    """
    wallet = address(wallet)
    try:
        if address(transaction.get("from")) != wallet or quantity(receipt.get("status")) != 1:
            return None
        if hash32(receipt.get("transactionHash")) != swap["tx_hash"] or hash32(transaction.get("hash")) != swap["tx_hash"]:
            return None
        if hash32(receipt.get("blockHash")) != swap["block_hash"] or quantity(receipt.get("blockNumber")) != swap["block_number"]:
            return None
        if pool["pool"] != swap["address"]:
            return None
        t0, t1 = pool["token0"], pool["token1"]
        if (t0 in base_addresses) == (t1 in base_addresses):
            return None
        base, token = (t0, t1) if t0 in base_addresses else (t1, t0)
        base_amount = int(swap["amount0_raw"] if base == t0 else swap["amount1_raw"])
        token_amount = int(swap["amount1_raw"] if base == t0 else swap["amount0_raw"])
        if base_amount <= 0 or token_amount >= 0:
            return None
        logs = receipt.get("logs")
        if not isinstance(logs, list):
            return None
        matched = False
        net: dict[str, int] = {base: 0, token: 0}
        direct_base_paid = direct_token_received = 0
        for log in logs:
            if not isinstance(log, dict):
                return None
            if (log.get("topics") or [None])[0] == SWAP_TOPIC and address(log.get("address")) == swap["address"]:
                actual = decode_swap(log)
                if actual["log_index"] == swap["log_index"]:
                    matched = all(actual[key] == swap[key] for key in actual)
            if (log.get("topics") or [None])[0] != TRANSFER_TOPIC:
                continue
            if address(log.get("address")) not in net:
                continue
            transfer = decode_transfer(log)
            if transfer["tx_hash"] != swap["tx_hash"] or transfer["block_hash"] != swap["block_hash"]:
                return None
            amount = int(transfer["amount_raw"])
            if transfer["address"] == base and transfer["from"] == wallet and transfer["to"] == pool["pool"]:
                direct_base_paid += amount
            if transfer["address"] == token and transfer["from"] == pool["pool"] and transfer["to"] == wallet:
                direct_token_received += amount
            if transfer["to"] == wallet:
                net[transfer["address"]] += amount
            if transfer["from"] == wallet:
                net[transfer["address"]] -= amount
        if not matched or net[base] >= 0 or net[token] <= 0:
            return None
        # Exact direct payment and receipt prevents unrelated transfers in the
        # same transaction from being attributed to this pool. Complex routes
        # and fee-on-transfer tokens are unsupported, never optimistically inferred.
        if direct_base_paid != base_amount or direct_token_received != -token_amount:
            return None
        if net[base] != -direct_base_paid or net[token] != direct_token_received:
            return None
        return {"wallet": wallet, "token_address": token, "quote_address": base,
                "amount_raw": str(net[token]), "spent_raw": str(-net[base]), "verified_swap": True}
    except (DecodeError, KeyError, TypeError, ValueError):
        return None


def _decode_text(raw: Any) -> str | None:
    if not isinstance(raw, str) or not re.fullmatch(r"0x[0-9a-fA-F]*", raw):
        return None
    data = bytes.fromhex(raw[2:])
    if len(data) == 32:
        return data.rstrip(b"\0").decode("utf-8", errors="replace")[:128]
    if len(data) < 64:
        return None
    offset = int.from_bytes(data[:32], "big")
    if offset != 32 or offset + 32 > len(data):
        return None
    length = int.from_bytes(data[offset:offset + 32], "big")
    if length > 512 or offset + 32 + length > len(data):
        return None
    return data[offset + 32:offset + 32 + length].decode("utf-8", errors="replace")[:128]


class EvmCollector:
    def __init__(self, rpc: RpcClient, registry: EvmRegistry | Mapping[str, Any] | None = None, *,
                 store: EvmStore | None = None, watchlist: Mapping[str, Mapping[str, Any]] | None = None,
                 quote_usd: Mapping[str, Mapping[str, Any]] | None = None,
                 confirmations: int = 5, max_blocks_per_request: int = 500,
                 max_reorg_depth: int = 256, clock: Callable[[], float] = time.time) -> None:
        self.rpc = rpc
        self.registry = registry if isinstance(registry, EvmRegistry) else EvmRegistry.from_dict(registry)
        self.store = store or EvmStore()
        self.watchlist = {address(k): dict(v) for k, v in (watchlist or {}).items()}
        self.quote_usd = {address(k): dict(v) for k, v in (quote_usd or {}).items()}
        if isinstance(confirmations, bool) or not 1 <= confirmations <= 10_000:
            raise ValueError("confirmations must be between 1 and 10000")
        if not 1 <= max_blocks_per_request <= 2000 or not 1 <= max_reorg_depth <= 10000:
            raise ValueError("invalid block or reorganisation bounds")
        self.confirmations, self.max_blocks_per_request = confirmations, max_blocks_per_request
        self.max_reorg_depth, self.clock = max_reorg_depth, clock
        self._metadata: dict[tuple[str, int], dict[str, Any]] = {}

    def block(self, number: int) -> dict[str, Any]:
        raw = self.rpc.call("eth_getBlockByNumber", [hex(number), False])
        if not isinstance(raw, dict) or quantity(raw.get("number")) != number:
            raise RpcError(f"block {number} unavailable or mismatched")
        return {"number": number, "hash": hash32(raw.get("hash")),
                "parent_hash": hash32(raw.get("parentHash")), "timestamp": quantity(raw.get("timestamp"))}

    def token_metadata(self, token: str, block_number: int) -> dict[str, Any]:
        token = address(token)
        key = (token, block_number)
        if key in self._metadata:
            return self._metadata[key]
        result: dict[str, Any] = {"address": token, "decimals": None, "symbol": None, "name": None, "errors": []}
        for name, selector in [("decimals", "0x313ce567"), ("symbol", "0x95d89b41"), ("name", "0x06fdde03")]:
            try:
                raw = self.rpc.call("eth_call", [{"to": token, "data": selector}, hex(block_number)])
                if name == "decimals":
                    val = int(_words(raw, 1)[0], 16)
                    if not 0 <= val <= 36:
                        raise DecodeError("unsupported decimal precision")
                    result[name] = val
                else:
                    result[name] = _decode_text(raw)
            except (RpcError, DecodeError):
                result["errors"].append(f"{name}_unavailable")
        self._metadata[key] = result
        return result

    def contract_safety(self, token: str, block_number: int | None = None) -> dict[str, Any]:
        """Conservative observations, intentionally NOT a contract audit.

        owner()==0 proves only one ownership slot. A successful/reverted mint
        probe cannot establish general mint permissions, so no such probe runs.
        """
        self.rpc.verify_chain(self.registry.chain_id)
        token = address(token)
        tag = hex(block_number) if block_number is not None else "latest"
        checks = {"contract_controls": None, "sellability": None, "liquidity_controls": None,
                  "holder_distribution": None, "deployer_history": None,
                  "contract_present": None, "mint_disabled": None, "freeze_disabled": None,
                  "permissions_verified": None, "sell_route_verified": None,
                  "liquidity_verified": None, "holders_verified": None}
        details: dict[str, Any] = {"owner": None, "owner_renounced": None, "errors": []}
        try:
            code = self.rpc.call("eth_getCode", [token, tag])
            if not isinstance(code, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", code):
                raise DecodeError("invalid bytecode response")
            checks["contract_present"] = code != "0x"
        except (RpcError, DecodeError):
            details["errors"].append("bytecode_unavailable")
        try:
            raw = self.rpc.call("eth_call", [{"to": token, "data": "0x8da5cb5b"}, tag])
            owner = _address_word(_words(raw, 1)[0])
            details.update(owner=owner, owner_renounced=(owner == ZERO_ADDRESS))
        except (RpcError, DecodeError):
            details["errors"].append("owner_unavailable")
        return {"asset_id": f"eip155:{self.registry.chain_id}/erc20:{token}",
                "observed_at": int(self.clock()), "checks": checks,
                "source": "EVM read-only observations; not a safety certificate", "details": details}

    def _recover_reorg(self) -> dict[str, Any] | None:
        cursor = self.store.cursor(self.registry.chain_id)
        if not cursor:
            return None
        now = int(self.clock())
        current = self.block(cursor["block_number"])
        if current["hash"] == cursor["block_hash"]:
            return None
        checkpoints = self.store.checkpoints(self.registry.chain_id, max(0, cursor["block_number"] - self.max_reorg_depth))
        for checkpoint in checkpoints:
            header = self.block(checkpoint["block_number"])
            if header["hash"] == checkpoint["block_hash"]:
                invalidated = self.store.rewind(self.registry.chain_id, header, now)
                return {"rewind_to": header["number"], "previous_tip": cursor["block_number"], "invalidated_events": invalidated}
        raise ReorgError("no verified common ancestor within saved reorganisation horizon; collector halted")

    def _usd_spend(self, base: str, spent: Decimal, now: int) -> str | None:
        observation = self.quote_usd.get(base)
        if not observation:
            return None
        if not isinstance(observation, Mapping):
            raise DecodeError("base-price observation must be an object")
        ts = observation.get("observed_at")
        if isinstance(ts, bool) or not isinstance(ts, int) or not 0 <= now - ts <= 30:
            return None
        price = _finite_positive(observation.get("price_usd"))
        with localcontext() as ctx:
            ctx.prec = 80
            return str(spent * price)

    def _logs(self, addresses: str | list[str], topic: str, start: int, end: int) -> list[dict[str, Any]]:
        result = self.rpc.call("eth_getLogs", [{"address": addresses, "topics": [topic], "fromBlock": hex(start), "toBlock": hex(end)}])
        if not isinstance(result, list):
            raise RpcError("eth_getLogs did not return a list")
        if len(result) > 100_000:
            raise RpcError("too many logs; reduce block range")
        return result

    def _chunk(self, start: int, end: int) -> dict[str, Any]:
        now = int(self.clock())
        cursor_before = self.store.cursor(self.registry.chain_id)
        headers: dict[int, dict[str, Any]] = {}
        end_anchor = self.block(end)
        events, pools, swaps, buys, errors = [], [], [], [], []
        def enrich(event: dict[str, Any]) -> dict[str, Any]:
            number = event["block_number"]
            if not start <= number <= end:
                raise DecodeError("RPC returned an event outside requested range")
            if number not in headers:
                headers[number] = self.block(number)
            if event["block_hash"] != headers[number]["hash"]:
                raise ReorgError("event and canonical header disagree; retry after chain settles")
            return {**event, "chain_id": self.registry.chain_id, "observed_at": now,
                    "chain_timestamp": headers[number]["timestamp"],
                    "event_id": f'{self.registry.chain_id}:{event["tx_hash"]}:{event["log_index"]}:{event["kind"]}'}
        for raw in self._logs(self.registry.factory, POOL_CREATED_TOPIC, start, end):
            try:
                pool = decode_pool_created(raw)
                if pool["address"] != self.registry.factory:
                    raise DecodeError("pool was not emitted by configured factory")
                pool = enrich(pool)
                pools.append(pool); events.append(pool)
            except DecodeError as exc:
                errors.append({"stage": "pool_decode", "reason": str(exc)})
        known = self.store.pools(self.registry.chain_id)
        known.update({p["pool"]: p for p in pools})
        eligible = {k: p for k, p in known.items()
                    if (p["token0"] in self.registry.bases) != (p["token1"] in self.registry.bases)}
        if len(eligible) > 2000:
            raise RpcError("more than 2000 active pools; explicitly narrow the research universe")
        receipt_cache: dict[str, Any] = {}
        transaction_cache: dict[str, Any] = {}
        seen_buys: set[tuple[str, str, str]] = set()
        pool_ids = sorted(eligible)
        for offset in range(0, len(pool_ids), 50):
            for raw in self._logs(pool_ids[offset:offset + 50], SWAP_TOPIC, start, end):
                try:
                    swap = enrich(decode_swap(raw))
                    if swap["address"] not in eligible:
                        raise DecodeError("swap emitted by an unregistered pool")
                    swaps.append(swap); events.append(swap)
                    if not self.watchlist:
                        continue
                    tx_hash = swap["tx_hash"]
                    if tx_hash not in transaction_cache:
                        transaction_cache[tx_hash] = self.rpc.call("eth_getTransactionByHash", [tx_hash])
                    transaction = transaction_cache[tx_hash]
                    if not isinstance(transaction, dict):
                        raise RpcError("transaction unavailable; will retry chunk")
                    wallet = address(transaction.get("from"))
                    if wallet not in self.watchlist:
                        continue
                    if tx_hash not in receipt_cache:
                        receipt_cache[tx_hash] = self.rpc.call("eth_getTransactionReceipt", [tx_hash])
                    receipt = receipt_cache[tx_hash]
                    if not isinstance(receipt, dict):
                        raise RpcError("receipt unavailable; will retry chunk")
                    raw_buy = verify_purchase(swap, eligible[swap["address"]], receipt, transaction, wallet, self.registry.bases)
                    if not raw_buy:
                        continue
                    identity = (wallet, raw_buy["token_address"], tx_hash)
                    if identity in seen_buys:
                        continue
                    seen_buys.add(identity)
                    meta = self.token_metadata(raw_buy["token_address"], swap["block_number"])
                    quote_meta = self.token_metadata(raw_buy["quote_address"], swap["block_number"])
                    if meta["decimals"] is None or quote_meta["decimals"] is None:
                        errors.append({"stage": "normalization", "tx_hash": tx_hash, "reason": "token_decimals_unavailable"})
                        continue
                    amount = token_units(int(raw_buy["amount_raw"]), meta["decimals"])
                    spent = token_units(int(raw_buy["spent_raw"]), quote_meta["decimals"])
                    tag = self.watchlist[wallet]
                    cluster = tag.get("cluster_id")
                    qualification = tag.get("qualification_evidence") or tag.get("evidence")
                    qualified = tag.get("qualified") is True and isinstance(cluster, str) and bool(cluster.strip()) and bool(qualification)
                    buy = {**swap, **raw_buy, "kind": "verified_buy", "asset_id": f'eip155:{self.registry.chain_id}/erc20:{raw_buy["token_address"]}',
                           "pool_id": swap["address"], "amount": str(amount), "spent_amount": str(spent),
                           "spent_usd": self._usd_spend(raw_buy["quote_address"], spent, now),
                           "symbol": meta["symbol"] or raw_buy["token_address"][:10],
                           "cluster_id": cluster if isinstance(cluster, str) else None,
                           "qualified": bool(qualified), "qualification_evidence": qualification,
                           "independence_verified_by_collector": False,
                           "event_id": f'{self.registry.chain_id}:{tx_hash}:{raw_buy["token_address"]}:{wallet}:verified_buy'}
                    buys.append(buy); events.append(buy)
                except DecodeError as exc:
                    errors.append({"stage": "swap_decode", "reason": str(exc)})
        end_header = self.block(end)
        if end_anchor["hash"] != end_header["hash"] or (end in headers and headers[end]["hash"] != end_header["hash"]):
            raise ReorgError("chain changed while collecting; no chunk committed")
        # The end-block hash anchors the full ancestor chain. A change during
        # collection invalidates the chunk before any observations are committed.
        first_header = self.block(start - 1) if start > 0 else self.block(0)
        saved_headers = list(headers.values()) + [first_header, end_header]
        if cursor_before and start == cursor_before["block_number"] + 1 and first_header["hash"] != cursor_before["block_hash"]:
            raise ReorgError("chunk no longer extends saved cursor")
        tip = end_header
        if cursor_before and end < cursor_before["block_number"]:
            tip = self.block(cursor_before["block_number"])
            if tip["hash"] != cursor_before["block_hash"]:
                raise ReorgError("cursor changed during historical rescan")
        inserted = self.store.commit_chunk(self.registry.chain_id, events, saved_headers, tip, expected_cursor=cursor_before)
        return {"new_pools": pools, "swaps": swaps, "verified_buys": buys, "errors": errors, "inserted_events": inserted}

    def scan(self, from_block: int, to_block: int) -> dict[str, Any]:
        """Collect a bounded confirmed interval (maximum 5000 blocks per call)."""
        if any(isinstance(x, bool) or not isinstance(x, int) for x in [from_block, to_block]):
            raise ValueError("block bounds must be integers")
        if from_block < 0 or to_block < from_block or to_block - from_block + 1 > 5000:
            raise ValueError("scan requires a nonnegative ordered range of at most 5000 blocks")
        self.rpc.verify_chain(self.registry.chain_id)
        head = quantity(self.rpc.call("eth_blockNumber"))
        if to_block > head - self.confirmations:
            raise ValueError("scan extends beyond the configured confirmation boundary")
        reorg = self._recover_reorg()
        cursor = self.store.cursor(self.registry.chain_id)
        if cursor and from_block > cursor["block_number"] + 1:
            raise ValueError("range would skip blocks after the saved cursor")
        result: dict[str, Any] = {"chain_id": self.registry.chain_id, "from_block": from_block,
            "to_block": to_block, "head": head, "confirmation_blocks": self.confirmations,
            "finality": "sequencer confirmation buffer, not Ethereum finality", "reorg": reorg,
            "new_pools": [], "swaps": [], "verified_buys": [], "errors": [], "inserted_events": 0,
            "tracked_wallets": len(self.watchlist)}
        for start in range(from_block, to_block + 1, self.max_blocks_per_request):
            chunk = self._chunk(start, min(start + self.max_blocks_per_request - 1, to_block))
            for name in ["new_pools", "swaps", "verified_buys", "errors"]:
                result[name].extend(chunk[name])
            result["inserted_events"] += chunk["inserted_events"]
        result["cursor"] = self.store.cursor(self.registry.chain_id)
        self._metadata.clear()
        return result

    def poll(self, start_block: int | None = None, max_blocks: int = 500) -> dict[str, Any]:
        if isinstance(max_blocks, bool) or not isinstance(max_blocks, int) or not 1 <= max_blocks <= 5000:
            raise ValueError("max_blocks must be between 1 and 5000")
        self.rpc.verify_chain(self.registry.chain_id)
        reorg = self._recover_reorg()
        head = quantity(self.rpc.call("eth_blockNumber"))
        end = head - self.confirmations
        cursor = self.store.cursor(self.registry.chain_id)
        if cursor:
            start = cursor["block_number"] + 1
            if start_block is not None and start_block != start:
                raise ValueError("start_block conflicts with saved cursor; use a separate DB for another research interval")
        elif start_block is not None:
            start = start_block
        else:
            start = max(0, end - max_blocks + 1)
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise ValueError("start_block must be nonnegative")
        if start > end:
            return {"chain_id": self.registry.chain_id, "head": head, "cursor": cursor, "reorg": reorg,
                    "new_pools": [], "swaps": [], "verified_buys": [], "errors": [],
                    "inserted_events": 0, "tracked_wallets": len(self.watchlist), "status": "caught_up"}
        result = self.scan(start, min(end, start + max_blocks - 1))
        result["reorg"] = reorg or result["reorg"]
        return result
