"""Executor boundaries with isolated files and fake providers; no live requests."""
from contextlib import ExitStack, closing, nullcontext, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from sniper.execution import ExecutionJournal
from sniper.execution_service import notify_execution
from sniper.telegram import DeliveryError


SPEC = importlib.util.spec_from_file_location(
    'sniper_executor_cli_integration', Path(__file__).resolve().parents[1] / 'executor.py')
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


def policy(**changes):
    value = {
        'account_id': 'isolated-integration-account', 'chain_id': 4663,
        'wallet': '0x' + '1' * 40, 'base_token': '0x' + '2' * 40,
        'base_decimals': 6, 'capital_raw': 1_000_000_000,
        'max_order_raw': 5_000_000, 'max_total_exposure_raw': 20_000_000,
        'daily_loss_raw': 20_000_000, 'max_gas_wei_day': 1000,
        'max_tx_gas_wei': 10, 'execution_target': 'automatic',
        'deployment_authorized': False, 'require_finalized': True,
        'user_approval_reference': 'ISOLATED TEST FIXTURE ONLY',
        'independent_review_reference': 'ISOLATED TEST FIXTURE ONLY',
        'keystore_path': 'never-read-fixture-keystore',
        'required_live_safety_checks': ['rug_screen_available'],
    }
    value.update(changes)
    return value


class ExecutorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['executor.py', '--app-root', str(self.root), *args]), \
                redirect_stdout(output), redirect_stderr(errors):
            result = CLI.main()
        return result, output.getvalue(), errors.getvalue()

    def test_live_commands_without_flag_stop_before_files_keys_or_network(self):
        for command in ('once', 'run', 'collect'):
            with self.subTest(command=command), ExitStack() as stack:
                read = stack.enter_context(patch.object(Path, 'read_text', side_effect=AssertionError('No credential or policy read')))
                cycle = stack.enter_context(patch.object(CLI, 'live_cycle', side_effect=AssertionError('No live worker')))
                collector = stack.enter_context(patch.object(CLI, 'collect_live', side_effect=AssertionError('No collector')))
                rpc = stack.enter_context(patch('sniper.execution_evm.ExecutionRpcClient', side_effect=AssertionError('No RPC client')))
                with self.assertRaises(SystemExit) as stopped:
                    self.invoke(command, '--mode', 'live')
                self.assertEqual(stopped.exception.code, 2)
                for mock in (read, cycle, collector, rpc):
                    mock.assert_not_called()
                self.assertFalse((self.root / 'runtime/executor').exists())

    def test_default_once_uses_shadow_without_constructing_live_backend(self):
        report = {'mode': 'SHADOW', 'real_money_enabled': False, 'signing_calls': 0, 'broadcast_calls': 0}
        with patch.object(CLI, 'shadow_cycle', return_value=report) as shadow, \
                patch.object(CLI, 'live_cycle', side_effect=AssertionError('No live worker')), \
                patch('sniper.execution_evm.ExecutionRpcClient', side_effect=AssertionError('No RPC')), \
                patch.object(CLI, 'instance_lock', return_value=nullcontext()):
            result, output, _ = self.invoke('once')
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output), report)
        shadow.assert_called_once_with(self.root)
        self.assertEqual(json.loads((self.root / 'runtime/executor/status.json').read_text()), report)
        self.assertFalse((self.root / 'runtime/executor/live').exists())

    def test_initialization_without_explicit_flag_never_calls_initializer(self):
        with patch.object(CLI, 'initialize_live', side_effect=AssertionError('No initializer')) as init, \
                patch.object(Path, 'read_text', side_effect=AssertionError('No policy or key read')):
            with self.assertRaises(SystemExit) as stopped:
                self.invoke('initialize-live', '--policy', 'nonexistent-policy.json')
        self.assertEqual(stopped.exception.code, 2)
        init.assert_not_called()
        self.assertFalse((self.root / 'runtime/executor').exists())

    def test_initializer_refuses_existing_account_without_mutating_it(self):
        existing = self.root / 'runtime/executor/live'
        existing.mkdir(parents=True)
        sentinel = existing / 'ledger.sqlite3'
        sentinel.write_bytes(b'existing-account-must-not-be-reset')
        path = self.root / 'policy.json'
        path.write_text('{}')
        with patch('sniper.execution_evm.ExecutionRpcClient', side_effect=AssertionError('No RPC')) as rpc:
            with self.assertRaisesRegex(ValueError, 'already exists'):
                CLI.initialize_live(self.root, path)
        rpc.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b'existing-account-must-not-be-reset')

    def test_initializer_rejects_unauthorized_policy_before_rpc_or_keystore(self):
        path = self.root / 'policy.json'
        path.write_text(json.dumps(policy()))
        with patch('sniper.execution_evm.ExecutionRpcClient', side_effect=AssertionError('No RPC')) as rpc, \
                patch('sniper.execution_evm.RestrictedEvmSigner.encrypted_keystore', side_effect=AssertionError('No signer')) as signer:
            with self.assertRaisesRegex(ValueError, 'Explicit initial live deployment'):
                CLI.initialize_live(self.root, path)
        rpc.assert_not_called()
        signer.assert_not_called()
        self.assertFalse((self.root / 'runtime/executor/live').exists())

    def test_initializer_wallet_balance_failure_creates_no_live_account(self):
        path = self.root / 'policy.json'
        path.write_text(json.dumps(policy(deployment_authorized=True)))
        runtime = self.root / 'runtime'
        runtime.mkdir()
        (runtime / 'quote-rpc-url.txt').write_text('https://invalid.example/read-only-test')
        adapter = MagicMock()
        adapter._anchor.return_value = {'block_number': 123}
        adapter._balance.return_value = 1
        with patch('sniper.execution_evm.ExecutionRpcClient') as rpc, \
                patch('sniper.execution_evm.EvmExecutionAdapter', return_value=adapter), \
                patch('sniper.execution_evm.RestrictedEvmSigner.encrypted_keystore', side_effect=AssertionError('No signer')) as signer, \
                patch('sniper.adapters.evm_store.EvmStore', side_effect=AssertionError('No new account')) as events:
            with self.assertRaisesRegex(ValueError, 'base balance'):
                CLI.initialize_live(self.root, path)
        self.assertNotIn('broadcast_enabled', rpc.call_args.kwargs)
        signer.assert_not_called()
        events.assert_not_called()
        self.assertFalse((runtime / 'executor/live').exists())

    def test_rejected_live_start_does_not_report_verified_live_authority(self):
        with patch.object(CLI, 'instance_lock', return_value=nullcontext()), \
                patch.object(CLI, 'live_cycle', side_effect=ValueError('Unapproved fixture policy')):
            result, output, _ = self.invoke('once', '--mode', 'live', '--allow-live-submission')
        report = json.loads(output)
        self.assertEqual(result, 2)
        self.assertEqual(report['health'], 'ERROR')
        self.assertIsNot(report.get('real_money_enabled'), True)

    def test_status_write_failure_is_error_not_successful_busy_worker(self):
        with patch.object(CLI, 'instance_lock', return_value=nullcontext()), \
                patch.object(CLI, 'shadow_cycle', return_value={'mode': 'SHADOW', 'real_money_enabled': False}), \
                patch.object(CLI, 'atomic_json', side_effect=OSError('Fixture storage unavailable')):
            result, output, _ = self.invoke('once')
        report = json.loads(output)
        self.assertEqual(result, 2)
        self.assertEqual(report['health'], 'ERROR')
        self.assertFalse(report['real_money_enabled'])
        self.assertNotEqual(report.get('status'), 'BUSY')

    def notification_setup(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        (runtime / 'telegram-bot-token.txt').write_text('9001:' + 'f' * 24)
        (runtime / 'telegram-recipient.json').write_text(json.dumps({'bot_id': 9001, 'chat_id': 7001}))
        folder = runtime / 'executor/live'
        folder.mkdir(parents=True)
        path = folder / 'ledger.sqlite3'
        journal = ExecutionJournal(path, policy(), create=True)
        with journal.transaction():
            journal._notice('fixture-order', 'receipt', {
                'order_id': 'fixture-order', 'tx_hash': '0x' + 'a' * 64,
                'input_raw': '5', 'output_raw': '100', 'fee_wei': '3', 'success': True,
            }, 1000)
        return runtime, folder, path, journal

    def fake_telegram(self, *, uncertain=False):
        client = MagicMock()
        client.call.return_value = {'id': 9001}
        client.send.return_value = 42
        if uncertain:
            client.send.side_effect = DeliveryError('Fixture lost acknowledgement', uncertain=True)
        return client

    def test_telegram_ack_persists_across_reopen_without_duplicate_delivery(self):
        runtime, folder, path, journal = self.notification_setup()
        client = self.fake_telegram()
        try:
            with patch('sniper.telegram.TelegramClient', return_value=client):
                first = notify_execution(journal, folder, runtime)
                self.assertEqual(first['outbox'], {'sent': 1})
                self.assertEqual(journal.notices(), [])
                journal.close()
                journal = ExecutionJournal(path, policy())
                second = notify_execution(journal, folder, runtime)
            self.assertEqual(second['outbox'], {'sent': 1})
            client.call.assert_called_once_with('getMe')
            client.send.assert_called_once()
            self.assertEqual(client.send.call_args.args[0], 7001)
            self.assertEqual(journal.snapshot()['fill_count'], 0)
            with closing(sqlite3.connect(folder / 'notifications.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT state,attempts,message_id FROM outbox').fetchone(), ('sent', 1, 42))
        finally:
            journal.close()

    def test_crash_after_outbox_ack_before_journal_ack_does_not_resend(self):
        runtime, folder, path, journal = self.notification_setup()
        client = self.fake_telegram()
        try:
            with patch('sniper.telegram.TelegramClient', return_value=client):
                with patch.object(journal, 'acknowledge_notice', side_effect=OSError('Fixture crash after durable outbox ACK')):
                    with self.assertRaises(OSError):
                        notify_execution(journal, folder, runtime)
                self.assertEqual(len(journal.notices()), 1)
                journal.close()
                journal = ExecutionJournal(path, policy())
                result = notify_execution(journal, folder, runtime)
            self.assertEqual(result['outbox'], {'sent': 1})
            self.assertEqual(journal.notices(), [])
            client.send.assert_called_once()
        finally:
            journal.close()

    def test_uncertain_telegram_delivery_neither_acknowledges_nor_blindly_retries(self):
        runtime, folder, path, journal = self.notification_setup()
        client = self.fake_telegram(uncertain=True)
        try:
            with patch('sniper.telegram.TelegramClient', return_value=client):
                first = notify_execution(journal, folder, runtime)
                journal.close()
                journal = ExecutionJournal(path, policy())
                second = notify_execution(journal, folder, runtime)
            self.assertEqual(first['outbox'], {'uncertain': 1})
            self.assertEqual(second['outbox'], {'uncertain': 1})
            self.assertEqual(len(journal.notices()), 1)
            client.send.assert_called_once()
            self.assertEqual(journal.snapshot()['fill_count'], 0)
        finally:
            journal.close()

    def test_wrong_bot_identity_sends_nothing_and_keeps_notice_unacknowledged(self):
        runtime, folder, _, journal = self.notification_setup()
        client = self.fake_telegram()
        client.call.return_value = {'id': 9999}
        try:
            with patch('sniper.telegram.TelegramClient', return_value=client):
                with self.assertRaisesRegex(ValueError, 'bot identity mismatch'):
                    notify_execution(journal, folder, runtime)
            client.send.assert_not_called()
            self.assertEqual(len(journal.notices()), 1)
            with closing(sqlite3.connect(folder / 'notifications.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT state,attempts,message_id FROM outbox').fetchone(), ('pending', 0, None))
        finally:
            journal.close()

    def test_acceptance_account_cannot_send_live_trade_notices(self):
        journal = ExecutionJournal(self.root / 'acceptance.sqlite3', policy(acceptance_mode=True), create=True)
        try:
            with patch('sniper.telegram.TelegramClient', side_effect=AssertionError('No Telegram')) as telegram, \
                    patch.object(Path, 'read_text', side_effect=AssertionError('No credentials')):
                result = notify_execution(journal, self.root / 'not-created', self.root)
            self.assertFalse(result['configured'])
            telegram.assert_not_called()
            self.assertFalse((self.root / 'not-created').exists())
        finally:
            journal.close()
