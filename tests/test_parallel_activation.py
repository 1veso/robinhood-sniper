"""Isolated activation/restart checks; never writes an installed account."""
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import explore as worker
from sniper.adapters.evm_store import EvmStore
from sniper.entry_strategies import entry_rules, rules_hash
from sniper.ledger import Ledger, json_text
from sniper.parallel_entry import PROFILE, ParallelEntryEngine
from test_engine import NOW, quote
from test_paper_reentry import eligible


class ParallelActivationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name)
        self.folder = self.runtime / "exploration"
        self.folder.mkdir()
        policy, rules, _ = worker.profile_settings("legacy")
        ledger, store, engine, original = worker.open_trial(self.folder, policy, start=True, now=NOW, rules=rules)
        try:
            self.assertEqual(engine.consider(eligible("keep"), quote(), NOW)["verdict"], "PAPER_BUY")
            asset = "eip155:4663:closed-fixture"
            self.assertEqual(engine.consider(eligible("close", asset=asset, now=NOW + 1),
                                            quote(asset, NOW + 1), NOW + 1)["verdict"], "PAPER_BUY")
            result = engine.watch({"eip155:4663:token1": quote(now=NOW + 2),
                                   asset: quote(asset, NOW + 2, ".5")}, NOW + 2)
            self.assertTrue(any(item["reason"] == "stop_loss" for item in result))
            store.db.execute("INSERT INTO evm_metadata(key,value) VALUES('activation_fixture_checkpoint','123')")
        finally:
            ledger.close()
            store.close()
        with patch.object(worker.time, "time", return_value=NOW + 10):
            worker.activate_v2(self.runtime)
        with patch.object(worker.time, "time", return_value=NOW + 20):
            worker.activate_reentry(self.runtime)
        self.policy, self.old_rules, _ = worker.profile_settings("two-wallet-30k-reentry")
        self.original = original
        self.before = self.state()

    def state(self):
        ledger = Ledger(self.folder / "ledger.sqlite3", self.policy.starting_cash_usd, require_existing=True)
        store = EvmStore(self.folder / "events.sqlite3")
        try:
            return {
                "snapshot": ledger.snapshot(), "account_id": ledger._get("account_id"),
                "policy": ledger._get("policy"), "policy_version": ledger._get("policy_version"),
                "trial": json.loads(ledger._get("trial_manifest")),
                "entry_rules": ledger._get("entry_rules"), "entry_revision": ledger._get("entry_revision"),
                "source_id": store.identity,
                "checkpoint": store.db.execute("SELECT value FROM evm_metadata WHERE key='activation_fixture_checkpoint'").fetchone()[0],
                "events": [dict(row) for row in ledger.conn.execute("SELECT * FROM events ORDER BY rowid")],
                "financial_tables": {table: [dict(row) for row in ledger.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in ("positions", "closed_positions", "fills", "decisions", "daily_equity", "equity_history")},
            }
        finally:
            ledger.close()
            store.close()

    def activate(self, *, eligibility="reviewed_tracked", reentry="fresh_post_close", at=NOW + 30):
        return worker.activate_parallel(self.runtime, whale_eligibility=eligibility, whale_reentry=reentry, now=at)

    def assert_preserved(self, after):
        for key in ("snapshot", "account_id", "policy", "policy_version", "source_id", "checkpoint", "financial_tables"):
            self.assertEqual(after[key], self.before[key], key)
        for key in ("started_at", "ends_at", "account_id", "source_id", "policy_version"):
            self.assertEqual(after["trial"][key], self.before["trial"][key], key)
        self.assertEqual(after["trial"]["ends_at"], self.original["ends_at"])
        for old in self.before["events"]:
            self.assertIn(old, after["events"])

    def test_activation_preserves_account_money_holdings_exits_deadline_and_history(self):
        selector = self.activate()
        after = self.state()
        self.assert_preserved(after)
        entries = entry_rules()
        revision = rules_hash(entries)
        self.assertEqual(after["entry_rules"], json_text(entries))
        self.assertEqual(after["entry_revision"], revision)
        self.assertEqual(after["trial"]["entry_rules"], entries)
        self.assertEqual(after["trial"]["entry_revision"], revision)
        self.assertEqual(after["trial"]["entry_revision_started_at"], NOW + 30)
        self.assertEqual(selector, json.loads((self.runtime / "active-exploration.json").read_text()))
        self.assertEqual(selector["profile"], PROFILE)
        self.assertEqual(selector["account_id"], self.before["account_id"])
        self.assertEqual(selector["entry_revision"], revision)
        self.assertEqual(selector["previous_profile"], "two-wallet-30k-reentry")
        self.assertEqual(worker.active_profile(self.runtime), PROFILE)
        new = [row for row in after["events"] if row not in self.before["events"]]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["kind"], "policy_revision")
        payload = json.loads(new[0]["payload"])
        self.assertEqual(payload["previous_trial"], self.before["trial"])
        self.assertEqual(payload["new_trial"], after["trial"])
        self.assertEqual(payload["previous_policy"], payload["new_policy"])

    def test_same_activation_restarts_without_duplicate_revision_or_new_clock(self):
        first = self.activate()
        after = self.state()
        second = self.activate(at=NOW + 100)
        self.assertEqual(first, second)
        self.assertEqual(self.state(), after)
        policy, rules, _ = worker.profile_settings(PROFILE)
        ledger, store, engine, trial = worker.open_trial(self.folder, policy, rules=rules)
        try:
            self.assertIsInstance(engine, ParallelEntryEngine)
            self.assertEqual(store.identity, self.before["source_id"])
            self.assertEqual(trial["ends_at"], self.before["trial"]["ends_at"])
        finally:
            ledger.close()
            store.close()

    def test_committed_activation_recovers_interrupted_trial_publication(self):
        original = worker.atomic_json
        def fail(path, value):
            if path.name == "trial.json":
                raise OSError("isolated publication interruption")
            original(path, value)
        with patch.object(worker, "atomic_json", side_effect=fail), self.assertRaises(OSError):
            self.activate()
        committed = self.state()
        self.assertEqual(json.loads((self.folder / "trial.json").read_text()), self.before["trial"])
        self.assertEqual(committed["trial"]["entry_revision_started_at"], NOW + 30)
        self.activate(at=NOW + 100)
        self.assertEqual(self.state(), committed)
        self.assertEqual(json.loads((self.folder / "trial.json").read_text()), committed["trial"])
        self.assert_preserved(committed)

    def test_committed_activation_recovers_interrupted_selector_publication(self):
        original = worker.atomic_json
        def fail(path, value):
            if path.name == "active-exploration.json":
                raise OSError("isolated selector interruption")
            original(path, value)
        with patch.object(worker, "atomic_json", side_effect=fail), self.assertRaises(OSError):
            self.activate()
        committed = self.state()
        self.assertEqual(json.loads((self.runtime / "active-exploration.json").read_text())["profile"], "two-wallet-30k-reentry")
        self.activate(at=NOW + 100)
        self.assertEqual(self.state(), committed)
        self.assertEqual(worker.active_profile(self.runtime), PROFILE)

    def test_explicit_alternative_options_are_pinned_without_changing_b(self):
        self.activate(eligibility="observed_recipient", reentry="cooldown_24h")
        after = self.state()
        self.assert_preserved(after)
        self.assertEqual(after["trial"]["entry_rules"], entry_rules(
            whale_eligibility="observed_recipient", whale_reentry="cooldown_24h"))
        self.assertEqual(after["trial"]["entry_rules"]["two_wallet_v3"]["reentry"], "fresh_post_close")
        self.assertEqual(self.policy.cooldown_seconds, 0)

    def test_choices_are_required_and_invalid_values_do_not_mutate_account(self):
        with self.assertRaises(TypeError):
            worker.activate_parallel(self.runtime)
        for eligibility, reentry in (("unknown", "fresh_post_close"), ("reviewed_tracked", "unknown")):
            with self.subTest(eligibility=eligibility, reentry=reentry), self.assertRaises(ValueError):
                self.activate(eligibility=eligibility, reentry=reentry)
            self.assertEqual(self.state(), self.before)

    def test_changed_choice_is_not_an_idempotent_restart(self):
        self.activate()
        after = self.state()
        with self.assertRaisesRegex(ValueError, "reviewed account/revision|later revisions need review"):
            self.activate(eligibility="observed_recipient")
        self.assertEqual(self.state(), after)

    def test_monetary_or_exit_policy_change_is_rejected_without_activation(self):
        policy, rules, name = worker.profile_settings(PROFILE)
        with patch.object(worker, "profile_settings", return_value=(replace(policy, stop_loss_pct=Decimal("10")), rules, name)):
            with self.assertRaisesRegex(ValueError, "monetary"):
                self.activate()
        self.assertEqual(self.state(), self.before)

    def test_missing_established_source_does_not_create_replacement(self):
        (self.folder / "events.sqlite3").unlink()
        with self.assertRaisesRegex(ValueError, "established paper account"):
            self.activate()
        self.assertFalse((self.folder / "events.sqlite3").exists())

    def test_unrecorded_manifest_change_is_rejected(self):
        changed = dict(self.before["trial"], ends_at=self.before["trial"]["ends_at"] + 1)
        (self.folder / "trial.json").write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unrecorded trial manifest"):
            self.activate()
        self.assertEqual(self.state(), self.before)

    def test_source_identity_mismatch_is_rejected(self):
        store = EvmStore(self.folder / "events.sqlite3")
        try:
            store.db.execute("UPDATE evm_metadata SET value='00000000-0000-4000-8000-000000000001' WHERE key='identity'")
        finally:
            store.close()
        with self.assertRaisesRegex(ValueError, "Account/source identity"):
            self.activate()
        self.assertIsNone(self.state()["entry_revision"])

    def test_invalid_activation_time_cannot_be_recorded(self):
        for invalid in (True, -1, 1.5, "1800000000"):
            with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, "Activation time"):
                self.activate(at=invalid)
            self.assertEqual(self.state(), self.before)

    def test_wrong_selector_account_or_profile_cannot_be_overwritten(self):
        original = json.loads((self.runtime / "active-exploration.json").read_text())
        for key, value in (("account_id", "different-account"), ("profile", "legacy")):
            changed = dict(original, **{key: value})
            (self.runtime / "active-exploration.json").write_text(json.dumps(changed))
            with self.subTest(field=key), self.assertRaisesRegex(ValueError, "Active selector"):
                self.activate()
            self.assertEqual(self.state(), self.before)
            self.assertEqual(json.loads((self.runtime / "active-exploration.json").read_text()), changed)

    def test_manifest_policy_version_must_match_ledger_policy(self):
        ledger = Ledger(self.folder / "ledger.sqlite3", self.policy.starting_cash_usd, require_existing=True)
        changed = dict(self.before["trial"], policy_version="different-policy")
        try:
            ledger._set("trial_manifest", json.dumps(changed, sort_keys=True))
        finally:
            ledger.close()
        (self.folder / "trial.json").write_text(json.dumps(changed))
        before_attempt = self.state()
        with self.assertRaisesRegex(ValueError, "Trial monetary policy"):
            self.activate()
        self.assertEqual(self.state(), before_attempt)

    def test_explicit_activation_never_initializes_an_empty_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "established paper account"):
                worker.activate_parallel(root, whale_eligibility="reviewed_tracked",
                                         whale_reentry="fresh_post_close", now=NOW)
            self.assertEqual(list(root.iterdir()), [])

    def test_existing_holding_keeps_original_stop_after_activation(self):
        self.activate()
        policy, rules, _ = worker.profile_settings(PROFILE)
        ledger, store, engine, _ = worker.open_trial(self.folder, policy, rules=rules)
        try:
            held = ledger.positions()[0]
            self.assertEqual(held, self.before["snapshot"]["positions"][0])
            result = engine.watch({held["asset_id"]: quote(held["asset_id"], NOW + 31, ".5")}, NOW + 31)
            self.assertEqual(result[0]["reason"], "stop_loss")
            self.assertEqual(ledger.snapshot()["open_positions"], 0)
        finally:
            ledger.close()
            store.close()


if __name__ == "__main__":
    unittest.main()
