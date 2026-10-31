"""Synthetic, network-free signal and reviewed-registry acceptance checks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from sniper.adapters.evm import CHAIN_ID, SWAP_TOPIC, TRANSFER_TOPIC, WETH
from sniper.entry_strategies import entry_rules, evaluate_signals, load_wallet_registry, rules_hash
from sniper.wallet_analysis import apply_controller_reviews, cluster_controllers


NOW = 1_800_000_000
WALLET = "0x" + "11" * 20
OTHER = "0x" + "22" * 20
TOKEN = "0x" + "aa" * 20
POOL = "0x" + "33" * 20
POOL2 = "0x" + "44" * 20
TX = "0x" + "55" * 32
TX2 = "0x" + "66" * 32
BLOCK = "0x" + "77" * 32


def word(value):
    return f"{value % (1 << 256):064x}"


def make_proof(*, wallet=WALLET, tx=TX, pool=POOL, token=TOKEN, at=NOW - 10, direct=True):
    """Complete retained receipt/tx proof; direct=False has a different payer."""
    payer = wallet if direct else "0x" + "99" * 20
    base_raw, token_raw = 10 ** 18, 100 * 10 ** 18
    common = {"transactionHash": tx, "blockHash": BLOCK, "blockNumber": "0x64", "removed": False}
    def log(contract, index, topics, data):
        return {**common, "address": contract, "logIndex": hex(index), "topics": topics, "data": data}
    def topic(addr):
        return "0x" + addr[2:].rjust(64, "0")
    def transfer(contract, index, sender, recipient, amount):
        return log(contract, index, [TRANSFER_TOPIC, topic(sender), topic(recipient)], "0x" + word(amount))
    swap = log(pool, 3, [SWAP_TOPIC, topic(payer), topic(wallet)],
               "0x" + "".join(word(v) for v in [base_raw, -token_raw, 2 ** 96, 100, 0]))
    receipt = {**common, "status": "0x1", "logs": [
        transfer(WETH, 1, payer, pool, base_raw), transfer(token, 2, pool, wallet, token_raw), swap]}
    return {"chain_id": CHAIN_ID, "wallet": wallet, "token_address": token,
            "quote_address": WETH, "pool": pool, "fee": 3000,
            "amount_raw": str(token_raw), "spent_raw": str(base_raw), "spent_usd": "20",
            "verified_swap": True, "factory_verified": True, "observed_at": at,
            "tx_hash": tx, "block_hash": BLOCK, "block_number": 100, "log_index": 3,
            "transaction": {"hash": tx, "from": payer}, "receipt": receipt,
            "proof_scope": "direct wallet base payment and token receipt"}


def review(wallet=WALLET, cluster="reviewed-controller-1"):
    return {"chain_id": CHAIN_ID, "history_cutoff": NOW - 1000,
            "reviewed_at": NOW - 100, "expires_at": NOW + 3600,
            "independence_reviewed": True, "cluster_id": cluster,
            "scope": "synthetic current controller review", "evidence_urls": ["https://example.test/review"]}


def report(wallet=WALLET):
    return {"wallet": wallet, "cutoff": NOW - 1000, "history_complete": True,
            "performance_estimate_available": True, "performance_qualified": True,
            "is_eoa": True, "qualification_blockers": [], "closed_positions": 20,
            "win_rate": "0.60", "top_token_profit_share": "0.25", "average_hold_minutes": "45",
            "net_pnl_after_follower_stress_usd": "25", "last_week_realized_less_open_basis_usd": "20",
            "gas_usd": "3", "open_cost_basis_usd": "2", "turnover_usd": "500",
            "follower_cost_bps_each_side": "100", "open_inventory_mark": "zero_lower_bound",
            "funders": [], "counterparties": [], "controller_coverage_complete": True,
            "source": "synthetic verified history"}


def registry(*wallets):
    wallets = wallets or (WALLET,)
    reports = cluster_controllers([report(wallet) for wallet in wallets])
    reviews = {wallet: review(wallet, "controller-" + wallet) for wallet in wallets}
    selected = apply_controller_reviews(reports, reviews, now=NOW)
    return {"status": "READY", "wallets": selected, "checked_at": NOW,
            "updated_at": NOW, "evidence_hash": "synthetic-registry"}


class EntrySignalTests(unittest.TestCase):
    def signals(self, proofs, *, rules=None, registry_value=None, last_closed_at=None):
        return evaluate_signals(proofs, token=TOKEN, now=NOW, last_closed_at=last_closed_at,
            rules=rules or entry_rules(), registry=registry() if registry_value is None else registry_value)

    def test_one_reviewed_direct_buyer_qualifies_a_without_b(self):
        a, b = self.signals([make_proof()])
        self.assertTrue(a["qualified"])
        self.assertFalse(b["qualified"])
        self.assertEqual(a["required_wallets"], 1)
        self.assertTrue(a["confirmations"][0]["qualified"])
        self.assertFalse(b["confirmations"][0]["qualified"])

    def test_two_buyers_same_pool_qualify_b(self):
        a, b = self.signals([make_proof(), make_proof(wallet=OTHER, tx=TX2)])
        self.assertTrue(a["qualified"])
        self.assertTrue(b["qualified"])
        self.assertEqual(b["source_pools"], [POOL])

    def test_two_buyers_different_pools_qualify_b_preserving_both_proofs(self):
        proofs = [make_proof(), make_proof(wallet=OTHER, tx=TX2, pool=POOL2)]
        original = deepcopy(proofs)
        a, b = self.signals(proofs)
        self.assertTrue(a["qualified"] and b["qualified"])
        self.assertEqual(b["source_pools"], [POOL, POOL2])
        self.assertEqual(b["source_proofs"], proofs)
        self.assertEqual(proofs, original)
        json.dumps([a, b], allow_nan=False)

    def test_one_wallet_two_pools_does_not_qualify_b(self):
        a, b = self.signals([make_proof(), make_proof(tx=TX2, pool=POOL2)])
        self.assertTrue(a["qualified"])
        self.assertFalse(b["qualified"])
        self.assertEqual(b["observed_wallets"], 1)

    def test_same_transaction_cannot_be_two_confirmations(self):
        _, b = self.signals([make_proof(), make_proof(wallet=OTHER, pool=POOL2)])
        self.assertFalse(b["qualified"])

    def test_matching_retains_alternative_transaction_for_first_wallet(self):
        _, b = self.signals([make_proof(), make_proof(tx=TX2), make_proof(wallet=OTHER, tx=TX2, pool=POOL2)])
        self.assertTrue(b["qualified"])
        self.assertEqual(len({p["tx_hash"] for p in b["source_proofs"]}), 2)

    def test_different_token_does_not_contribute(self):
        _, b = self.signals([make_proof(), make_proof(wallet=OTHER, tx=TX2, token="0x" + "bb" * 20)])
        self.assertFalse(b["qualified"])
        self.assertEqual(b["rejected_proof_count"], 1)

    def test_empty_missing_or_stale_review_registry_does_not_block_b(self):
        proofs = [make_proof(), make_proof(wallet=OTHER, tx=TX2, pool=POOL2)]
        stale = registry()
        stale["updated_at"] = NOW - 301
        for value in ({}, {"status": "EMPTY", "wallets": {}}, stale):
            with self.subTest(registry=value.get("status")):
                a, b = self.signals(proofs, registry_value=value)
                self.assertFalse(a["qualified"])
                self.assertTrue(b["qualified"])

    def test_reviewed_a_requires_direct_funding_not_scope_label(self):
        a, b = self.signals([make_proof(direct=False), make_proof(wallet=OTHER, tx=TX2, pool=POOL2)])
        self.assertFalse(a["qualified"])
        self.assertTrue(b["qualified"])

    def test_review_expiration_is_rechecked_even_with_fresh_registry(self):
        current = registry()
        current["wallets"][WALLET]["qualification_evidence"]["controller_review"]["expires_at"] = NOW - 1
        a, b = self.signals([make_proof(), make_proof(wallet=OTHER, tx=TX2)], registry_value=current)
        self.assertFalse(a["qualified"])
        self.assertTrue(b["qualified"])

    def test_older_direct_proof_survives_newer_routed_purchase(self):
        a, _ = self.signals([make_proof(at=NOW - 30), make_proof(tx=TX2, direct=False)])
        self.assertTrue(a["qualified"])
        self.assertEqual(a["source_proofs"][0]["tx_hash"], TX)

    def test_retained_receipt_or_raw_amount_tampering_blocks_reviewed_a(self):
        for mutation in (lambda p: p.update(amount_raw="1"),
                         lambda p: p["receipt"].update(blockHash="0x" + "88" * 32),
                         lambda p: p["receipt"].update(status="0x0"),
                         lambda p: p.pop("transaction")):
            proof = make_proof()
            mutation(proof)
            a, _ = self.signals([proof])
            self.assertFalse(a["qualified"])

    def test_observed_a_option_does_not_claim_reviewed_wallet(self):
        a, b = self.signals([make_proof(direct=False)],
            rules=entry_rules(whale_eligibility="observed_recipient"), registry_value={})
        self.assertTrue(a["qualified"])
        self.assertFalse(a["confirmations"][0]["qualified"])
        self.assertEqual(a["controller_independence"], "unestablished")
        self.assertFalse(b["qualified"])

    def test_fresh_reentry_requires_observations_strictly_after_close(self):
        first, second = make_proof(at=NOW - 20), make_proof(wallet=OTHER, tx=TX2, at=NOW - 10)
        a, b = self.signals([first, second], last_closed_at=NOW - 20)
        self.assertFalse(a["qualified"])
        self.assertFalse(b["qualified"])
        self.assertEqual(b["observed_wallets"], 1)
        first = make_proof(at=NOW - 19)
        a, b = self.signals([first, second], last_closed_at=NOW - 20)
        self.assertTrue(a["qualified"] and b["qualified"])

    def test_a_cooldown_never_becomes_b_cooldown(self):
        rules = entry_rules(whale_reentry="cooldown_24h")
        proofs = [make_proof(), make_proof(wallet=OTHER, tx=TX2)]
        a, b = self.signals(proofs, rules=rules, last_closed_at=NOW - 100)
        self.assertFalse(a["qualified"])
        self.assertEqual(a["qualification_reason"], "cooldown_24h")
        self.assertTrue(b["qualified"])
        a, b = self.signals(proofs, rules=rules, last_closed_at=NOW - 86400)
        self.assertTrue(a["qualified"] and b["qualified"])

    def test_bad_proof_boundaries_fail_closed(self):
        mutations = {"chain_id": 1, "factory_verified": False, "verified_swap": 1,
                     "observed_at": NOW + 1, "spent_usd": "NaN", "fee": True,
                     "block_number": True, "log_index": -1, "quote_address": TOKEN,
                     "tx_hash": "bad", "amount_raw": "0", "pool": "bad"}
        for key, value in mutations.items():
            with self.subTest(field=key):
                proof = make_proof()
                proof[key] = value
                a, b = self.signals([proof])
                self.assertFalse(a["qualified"] or b["qualified"])
                self.assertEqual(a["rejected_proof_count"], 1)

    def test_window_and_minimum_spend_boundaries(self):
        rules = entry_rules(whale_eligibility="observed_recipient")
        proof = make_proof(at=NOW - 900)
        proof["spent_usd"] = "10"
        self.assertTrue(self.signals([proof], rules=rules)[0]["qualified"])
        for key, value in (("spent_usd", "9.999"), ("observed_at", NOW - 901)):
            bad = deepcopy(proof)
            bad[key] = value
            self.assertFalse(self.signals([bad], rules=rules)[0]["qualified"])

    def test_legacy_proof_chain_omission_is_fixed_chain_only(self):
        proof = make_proof()
        proof.pop("chain_id")
        self.assertTrue(self.signals([proof])[0]["qualified"])
        proof["chain_id"] = True
        self.assertFalse(self.signals([proof])[0]["qualified"])

    def test_rules_hash_is_deterministic_and_options_are_explicit(self):
        self.assertEqual(rules_hash(entry_rules()), rules_hash(deepcopy(entry_rules())))
        self.assertNotEqual(rules_hash(entry_rules()), rules_hash(entry_rules(whale_reentry="cooldown_24h")))
        with self.assertRaises(ValueError):
            entry_rules(whale_eligibility="any_string")
        invalid = entry_rules()
        invalid["two_wallet_v3"]["required_wallets"] = 1
        with self.assertRaises(ValueError):
            self.signals([], rules=invalid)


class WalletRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        (self.runtime / "research").mkdir()
        self.reports = [report()]
        self.reviews = {WALLET: review()}

    def publish(self, *, updated_at=NOW, selected=None):
        reports = cluster_controllers(deepcopy(self.reports))
        qualified = apply_controller_reviews(reports, self.reviews, now=NOW)
        payloads = {"research/wallet-research.json": {"updated_at": updated_at, "reports": reports},
                    "research/qualified-wallets.json": qualified if selected is None else selected,
                    "controller-reviews.json": self.reviews}
        for name, payload in payloads.items():
            (self.runtime / name).write_text(json.dumps(payload), encoding="utf-8")

    def test_current_cost_history_and_controller_binding_loads(self):
        self.publish()
        result = load_wallet_registry(self.runtime, NOW)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(list(result["wallets"]), [WALLET])
        self.assertEqual(len(result["evidence_hash"]), 64)

    def test_pending_history_refresh_cannot_use_old_qualified_publication(self):
        self.reports[0]['history_refresh_pending'] = True
        self.publish()
        self.assertEqual(load_wallet_registry(self.runtime, NOW)['wallets'], {})

    def test_atomic_registry_is_independent_of_bulk_export_but_retains_freshness_limit(self):
        self.publish()
        folder = self.runtime / 'research'
        summary = json.loads((folder / 'wallet-research.json').read_text())
        selected = json.loads((folder / 'qualified-wallets.json').read_text())
        (folder / 'registry-publication.json').write_text(json.dumps({
            **summary, 'schema': 'wallet_registry_v1', 'qualified_wallets': selected}))
        (folder / 'wallet-research.json').write_text('{}')
        (folder / 'qualified-wallets.json').write_text('{}')
        self.assertEqual(load_wallet_registry(self.runtime, NOW)['status'], 'READY')
        self.assertEqual(load_wallet_registry(self.runtime, NOW + 301)['status'], 'STALE')
        (folder / 'registry-publication.json').write_text('{}')
        self.publish()
        self.assertEqual(load_wallet_registry(self.runtime, NOW)['status'], 'UNAVAILABLE')

    def test_stale_publication_does_not_fall_back_to_legacy_watchlist(self):
        self.publish(updated_at=NOW - 301)
        (self.runtime / "wallets.json").write_text(json.dumps(registry()["wallets"]))
        result = load_wallet_registry(self.runtime, NOW)
        self.assertEqual(result["status"], "STALE")
        self.assertEqual(result["wallets"], {})

    def test_legacy_evidence_string_and_expired_review_never_qualify(self):
        self.publish(selected={WALLET: {"qualified": True, "cluster_id": "legacy",
                                       "qualification_evidence": "audited"}})
        self.assertEqual(load_wallet_registry(self.runtime, NOW)["wallets"], {})
        self.publish()
        self.assertEqual(load_wallet_registry(self.runtime, NOW + 3601)["wallets"], {})

    def test_missing_or_invalid_costs_fail_despite_qualified_flag(self):
        for key, value in (("gas_usd", None), ("net_pnl_after_follower_stress_usd", "NaN"),
                           ("history_complete", False), ("win_rate", "0.54"),
                           ("last_week_realized_less_open_basis_usd", "-0.01")):
            with self.subTest(field=key):
                self.reports = [report()]
                self.reports[0][key] = value
                self.publish()
                self.assertEqual(load_wallet_registry(self.runtime, NOW)["wallets"], {})

    def test_removed_or_mismatched_review_invalidates_published_wallet(self):
        self.publish()
        (self.runtime / "controller-reviews.json").write_text("{}")
        self.assertEqual(load_wallet_registry(self.runtime, NOW)["wallets"], {})
        self.publish()
        self.reviews[WALLET]["history_cutoff"] -= 1
        (self.runtime / "controller-reviews.json").write_text(json.dumps(self.reviews))
        self.assertEqual(load_wallet_registry(self.runtime, NOW)["wallets"], {})

    def test_expired_review_cannot_be_restored_by_fresh_export_timestamp(self):
        self.publish()
        self.reviews[WALLET]["expires_at"] = NOW - 1
        (self.runtime / "controller-reviews.json").write_text(json.dumps(self.reviews))
        result = load_wallet_registry(self.runtime, NOW)
        self.assertEqual(result["updated_at"], NOW)
        self.assertEqual(result["wallets"], {})

    def test_funding_links_cannot_be_split_by_reviews(self):
        self.reports = [report(), report(OTHER)]
        self.reports[0]["funders"] = ["shared-funder"]
        self.reports[1]["funders"] = ["shared-funder"]
        self.reviews[OTHER] = review(OTHER, "another-controller")
        self.publish(selected=registry(WALLET, OTHER)["wallets"])
        self.assertEqual(load_wallet_registry(self.runtime, NOW)["wallets"], {})

    def test_missing_malformed_and_empty_registry_are_safe_states(self):
        self.assertEqual(load_wallet_registry(self.runtime, NOW)["status"], "UNAVAILABLE")
        self.reports, self.reviews = [], {}
        self.publish()
        self.assertEqual(load_wallet_registry(self.runtime, NOW)["status"], "EMPTY")
        (self.runtime / "research/wallet-research.json").write_text("{")
        self.assertEqual(load_wallet_registry(self.runtime, NOW)["status"], "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
