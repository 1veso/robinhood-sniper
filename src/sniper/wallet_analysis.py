"""Cost-aware wallet qualification from reconciled history, never a leaderboard claim."""
from __future__ import annotations

from collections import defaultdict, deque
from decimal import Decimal
import hashlib

from .ledger import decimal

D = Decimal
DAY = 86400


def analyse_wallet(history: dict, *, follower_cost_bps: str = "100") -> dict:
    """FIFO on verified direct swaps; transferred inventory never acquires zero basis.

    Open inventory is marked at zero for the qualification stress test. This is
    explicitly a lower-bound valuation, not an assertion that the tokens are worthless.
    Pool fees are already reflected in actual token flows. Receipt gas is included
    once, including failed transactions and non-trade overhead. Follower friction
    is a separate assumption, not observed execution performance.
    """
    wallet = history["wallet"]
    cutoff = history["cutoff"]
    if type(cutoff) is not int or cutoff < 0:
        raise ValueError("invalid history cutoff")
    friction = decimal(follower_cost_bps, nonnegative=True) / D(10000)
    if friction > D("0.1"):
        raise ValueError("follower cost stress must be at most 1000 bps")
    blockers = list(history.get("gaps", []))
    if history.get("complete") is not True:
        blockers.append("history_incomplete")
    lots, cycles = defaultdict(deque), {}
    closed, seen = [], set()
    total_gas = overhead = turnover = realized = D(0)
    recent_net = D(0)
    inventory_taint = set(history.get("opening_inventory", []))
    records = history.get("records", [])
    order = [(r["timestamp"], r["block_number"], r["transaction_index"], r["id"]) for r in records]
    if order != sorted(order):
        raise ValueError("history must be in canonical chronological order")
    for r in records:
        if r["id"] in seen:
            raise ValueError("duplicate transaction in wallet history")
        seen.add(r["id"])
        if type(r["timestamp"]) is not int or not 0 <= r["timestamp"] <= cutoff:
            raise ValueError("future or malformed transaction timestamp")
        gas = decimal(r.get("gas_usd", "0"), nonnegative=True)
        total_gas += gas
        side = r["kind"]
        if side not in {"BUY", "SELL"}:
            overhead += gas
            if r["timestamp"] >= cutoff - 7 * DAY:
                recent_net -= gas
            if side == "TRANSFER":
                for token in r.get("tokens", []):
                    inventory_taint.add(token)
                    if lots[token]:
                        blockers.append("tracked_inventory_transferred:" + token)
            continue
        if r.get("verified") is not True:
            blockers.append("unverified_trade:" + r["id"])
            overhead += gas
            if r["timestamp"] >= cutoff - 7 * DAY:
                recent_net -= gas
            continue
        token = r["token"]
        amount = decimal(r["quantity"], nonnegative=True)
        value = decimal(r["value_usd"], nonnegative=True)
        if amount <= 0 or value <= 0:
            raise ValueError("trade quantity and value must be positive")
        turnover += value
        if side == "BUY":
            if token in inventory_taint:
                blockers.append("mixed_or_unknown_inventory:" + token)
            cycle = cycles.setdefault(token, {"opened_at": r["timestamp"], "pnl": D(0), "cost": D(0)})
            cycle["cost"] += value + gas
            lots[token].append({"qty": amount, "basis": value + gas})
            continue
        available = sum((lot["qty"] for lot in lots[token]), D(0))
        if token in inventory_taint or available < amount:
            blockers.append("sale_without_reconciled_basis:" + token)
            overhead += gas
            if r["timestamp"] >= cutoff - 7 * DAY:
                recent_net -= gas
            inventory_taint.add(token)
            continue
        remaining, basis = amount, D(0)
        while remaining:
            lot = lots[token][0]
            used = min(remaining, lot["qty"])
            cost = lot["basis"] * used / lot["qty"]
            basis += cost
            lot["basis"] -= cost
            lot["qty"] -= used
            remaining -= used
            if not lot["qty"]:
                lots[token].popleft()
        pnl = value - gas - basis
        realized += pnl
        # Weekly realized P&L allocates entry gas with the sold cost basis.
        if r["timestamp"] >= cutoff - 7 * DAY:
            recent_net += pnl
        cycles[token]["pnl"] += pnl
        if not lots[token]:
            cycle = cycles.pop(token)
            closed.append({"token": token, "pnl_usd": str(cycle["pnl"]),
                           "opened_at": cycle["opened_at"], "closed_at": r["timestamp"],
                           "hold_minutes": str(D(r["timestamp"] - cycle["opened_at"]) / 60)})
    open_basis = sum((lot["basis"] for token_lots in lots.values() for lot in token_lots), D(0))
    net = realized - overhead - open_basis
    stress = net - turnover * friction
    winners = sum(D(x["pnl_usd"]) > 0 for x in closed)
    win_rate = D(winners) / len(closed) if closed else D(0)
    positive = defaultdict(lambda: D(0))
    token_pnl = defaultdict(lambda: D(0))
    for trade in closed:
        token_pnl[trade["token"]] += D(trade["pnl_usd"])
    positive.update({k: max(D(0), v) for k, v in token_pnl.items()})
    positive_sum = sum(positive.values(), D(0))
    concentration = max(positive.values(), default=D(0)) / positive_sum if positive_sum else D(1)
    holding = sum((D(t["hold_minutes"]) for t in closed), D(0)) / len(closed) if closed else D(0)
    if len(closed) < 20:
        blockers.append("fewer_than_20_closed_positions")
    if win_rate < D("0.55"):
        blockers.append("win_rate_below_55_percent")
    if concentration > D("0.50"):
        blockers.append("one_token_profit_concentration_above_50_percent")
    if holding < 30:
        blockers.append("average_holding_below_30_minutes")
    if stress <= 0:
        blockers.append("nonpositive_profit_after_costs_and_open_inventory_stress")
    if recent_net - open_basis < 0:
        blockers.append("negative_last_week_after_open_inventory_stress")
    return {"wallet": wallet, "cutoff": cutoff, "performance_qualified": not blockers,
            "qualified": False, "qualification_blockers": sorted(set(blockers)),
            "history_complete": history.get("complete") is True,
            "closed_positions": len(closed), "win_rate": str(win_rate),
            "average_hold_minutes": str(holding), "top_token_profit_share": str(concentration),
            "realized_pnl_usd": str(realized - overhead), "gas_usd": str(total_gas),
            "open_cost_basis_usd": str(open_basis), "open_inventory_mark": "zero_lower_bound",
            "net_pnl_with_open_inventory_stress_usd": str(net),
            "net_pnl_after_follower_stress_usd": str(stress),
            "follower_cost_bps_each_side": str(follower_cost_bps),
            "turnover_usd": str(turnover), "last_week_realized_less_open_basis_usd": str(recent_net - open_basis),
            "closed_trades": closed, "funders": history.get("funders", []),
            "counterparties": history.get("counterparties", []),
            "controller_coverage_complete": history.get("controller_coverage_complete") is True,
            "is_eoa": history.get("is_eoa") is True,
            "source": history.get("source"),
            "limitations": "USD values use timestamped historical reference prices. Follower costs are stress assumptions. Selection is not proof of future performance."}


def cluster_controllers(reports: list[dict]) -> list[dict]:
    """Merge direct funding links/shared funders; absence of links is not identity proof."""
    parents = {r["wallet"]: r["wallet"] for r in reports}
    def root(wallet):
        while parents[wallet] != wallet:
            parents[wallet] = parents[parents[wallet]]
            wallet = parents[wallet]
        return wallet
    def union(a, b):
        a, b = root(a), root(b)
        parents[max(a, b)] = min(a, b)
    by_funder = defaultdict(list)
    for r in reports:
        for funder in set(r.get("funders", [])):
            by_funder[funder].append(r["wallet"])
            if funder in parents:
                union(r["wallet"], funder)
        for other in set(r.get("counterparties", [])) & parents.keys():
            union(r["wallet"], other)
    for wallets in by_funder.values():
        for wallet in wallets[1:]:
            union(wallets[0], wallet)
    members = defaultdict(list)
    for wallet in parents:
        members[root(wallet)].append(wallet)
    for r in reports:
        group = sorted(members[root(r["wallet"])])
        r["cluster_id"] = "funding-graph:" + hashlib.sha256("|".join(group).encode()).hexdigest()[:16]
        r["cluster_members"] = group
        r["controller_status"] = ("LINKED" if len(group) > 1 else "NO_OBSERVED_LINKS"
                                  if r.get("controller_coverage_complete") and r.get("funders") and r.get("is_eoa")
                                  else "UNKNOWN")
        r["independence_verified"] = False
        r["qualified"] = False
        r["controller_limitation"] = "Funding links are conservative heuristics, not beneficial-owner identity. Shared exchange withdrawals can overcluster; hidden links can be missed."
    return reports


def apply_controller_reviews(reports: list[dict], reviews: dict, *, now: int) -> dict:
    """Bind external identity evidence to the computed performance and graph.

    This is an attestation interface, not automatic proof of beneficial ownership.
    A review cannot split wallets linked by the current conservative graph.
    """
    valid = {}
    for r in reports:
        r["qualified"] = False
        item = reviews.get(r["wallet"], {})
        try:
            reviewed, expires = item["reviewed_at"], item["expires_at"]
            if type(reviewed) is not int or type(expires) is not int or not reviewed <= now <= expires or expires - reviewed > 7 * DAY:
                continue
            if item.get("chain_id") != 4663 or item.get("history_cutoff") != r["cutoff"]:
                continue
            if item.get("independence_reviewed") is not True or not item.get("scope", "").strip():
                continue
            cluster = item.get("cluster_id")
            refs = item.get("evidence_urls")
            if not isinstance(cluster, str) or not cluster.strip() or not isinstance(refs, list) or not refs or not all(isinstance(x, str) and x.startswith("https://") for x in refs):
                continue
            valid[r["wallet"]] = item
        except (KeyError, TypeError, AttributeError):
            continue
    conflicts = set()
    for r in reports:
        identities = {valid[w]["cluster_id"] for w in r["cluster_members"] if w in valid}
        if len(identities) > 1:
            conflicts.update(r["cluster_members"])
    selected = {}
    for r in reports:
        item = valid.get(r["wallet"])
        if item and r["wallet"] not in conflicts and r["performance_qualified"] and r["is_eoa"]:
            r["qualified"] = True
            r["controller_status"] = "EXTERNAL_REVIEW_ACCEPTED"
            selected[r["wallet"]] = {"qualified": True, "cluster_id": item["cluster_id"],
                "qualification_evidence": {"history_cutoff": r["cutoff"], "performance_source": r["source"],
                                           "controller_review": item, "method": "receipt_fifo_with_costs_v1"}}
        elif r["wallet"] in conflicts:
            r["controller_status"] = "REVIEW_CONFLICTS_WITH_FUNDING_LINKS"
    return selected
