import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo
import desktop


class DesktopTests(unittest.TestCase):
    def test_input_allowlist_never_overwrites_existing_or_accepts_seeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'input.txt';source.write_text('server-key-test')
            desktop.import_input(root,'jupiter-server-api-key.txt',source)
            source.write_text('different-key')
            with self.assertRaises(ValueError):desktop.import_input(root,'jupiter-server-api-key.txt',source)
            with self.assertRaises(ValueError):desktop.import_input(root,'seed.txt',source)
            self.assertEqual((root/'runtime/jupiter-server-api-key.txt').read_text().strip(),'server-key-test')

    def test_berlin_report_gate_includes_dst_and_excludes_previews(self):
        for month in (1,7):
            for hour,minute,wanted in [(20,30,False),(21,29,False),(21,30,True),(21,59,True),(22,0,False)]:
                stamp=datetime(2026,month,5,hour,minute,tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
                self.assertEqual(desktop.report_due(stamp),wanted)

    def test_new_trial_requires_telegram_ack_before_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(desktop,'initialize'),patch.object(desktop,'install_jobs') as jobs:
            with self.assertRaisesRegex(ValueError,'Pair Telegram'):
                desktop.start_paper(Path(tmp))
            jobs.assert_not_called()

    def test_continue_keeps_same_trial_and_no_provider_or_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'runtime/exploration/trial.json';path.parent.mkdir(parents=True)
            text=json.dumps({'account_id':'same','ends_at':123});path.write_text(text)
            with patch.object(desktop,'initialize'),patch.object(desktop,'install_jobs'),patch('explore.activate_v2') as activate:
                out=desktop.start_paper(root)
            activate.assert_not_called();self.assertEqual(path.read_text(),text);self.assertEqual(out['account_id'],'same')

    def test_missing_trial_worker_does_not_implicitly_start(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(desktop,'child') as child:
            for name in ('paper','shadow','research','monitor'):
                self.assertEqual(desktop.worker(name,Path(tmp)),0)
            child.assert_not_called()

    def test_report_completion_is_persistent_and_failed_run_does_not_consume_day(self):
        now=int(datetime(2026,10,28,21,31,tzinfo=ZoneInfo('Europe/Berlin')).timestamp())
        with tempfile.TemporaryDirectory() as tmp, patch.object(desktop.time,'time',return_value=now), patch.object(desktop,'child',side_effect=[1,0]) as child:
            root=Path(tmp)
            self.assertEqual(desktop.worker('report',root),1)
            self.assertFalse((root/'runtime/desktop/report-day.json').exists())
            self.assertEqual(desktop.worker('report',root),0)
            self.assertEqual(desktop.worker('report',root),0)
            self.assertEqual(child.call_count,2)

    def test_timeout_is_a_failed_completion_with_no_report_claim(self):
        now=int(datetime(2026,10,28,21,31,tzinfo=ZoneInfo('Europe/Berlin')).timestamp())
        with tempfile.TemporaryDirectory() as tmp, patch.object(desktop.time,'time',return_value=now), patch.object(desktop,'child',side_effect=desktop.subprocess.TimeoutExpired('child',240)):
            root=Path(tmp)
            self.assertEqual(desktop.worker('report',root),124)
            self.assertFalse((root/'runtime/desktop/report-day.json').exists())
            self.assertEqual(json.loads((root/'runtime/desktop/report.json').read_text())['exit_code'],124)

    def test_helium_scan_timeout_does_not_consume_watch_worker(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(desktop, 'child', side_effect=[
                desktop.subprocess.TimeoutExpired('helium-scan', 50), 0]) as child:
            root = Path(tmp)
            self.assertEqual(desktop.worker('helium-scan', root), 124)
            self.assertEqual(desktop.worker('helium-watch', root), 0)
            self.assertEqual(child.call_args_list[0].args[:2], (['helium_bot.py', 'scan'], 50))
            self.assertEqual(child.call_args_list[1].args[:2], (['helium_bot.py', 'watch'], 50))
            self.assertEqual(json.loads((root/'runtime/desktop/helium-scan.json').read_text())['phase'], 'FAILED')
            self.assertEqual(json.loads((root/'runtime/desktop/helium-watch.json').read_text())['phase'], 'COMPLETE')
            self.assertFalse((root/'helium_bot/runtime/book.sqlite3').exists())

    def test_release_source_allowlist_excludes_private_runtime(self):
        spec=importlib.util.spec_from_file_location('build_windows',Path(__file__).parents[1]/'packaging/build_windows.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ('desktop.py','runtime/jupiter-server-api-key.txt','rug_desk/runtime/account.json','rug_desk/config.py','src/sniper/__pycache__/secret.py','config/paper.toml',
                         'helium_bot/config.json','helium_bot/README.md','helium_bot/runtime/book.sqlite3','helium_bot/runtime/paid-ai.json','helium_bot/.env'):
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('sentinel')
            self.assertEqual({p.relative_to(root).as_posix() for p in module.source_files(root)},
                             {'desktop.py','rug_desk/config.py','config/paper.toml','helium_bot/config.json','helium_bot/README.md'})


class HeliumCliTests(unittest.TestCase):
    def make_root(self, directory):
        root = Path(directory)
        target = root/'helium_bot/config.json'
        target.parent.mkdir()
        target.write_bytes((Path(__file__).parents[1]/'helium_bot/config.json').read_bytes())
        return root

    def test_live_rejected_before_creating_account(self):
        import helium_bot
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                helium_bot.main(['init', '--root', tmp, '--mode', 'live'])
            self.assertEqual(result.exception.code, 2)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_init_is_isolated_idempotent_and_preserves_paid_settings(self):
        import helium_bot
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = self.make_root(tmp)
            other = root/'krypton_bot/runtime/sentinel.json'
            other.parent.mkdir(parents=True)
            other.write_text('existing account stays intact')
            self.assertEqual(helium_bot.main(['init', '--root', str(root)]), 0)
            folder = root/'helium_bot/runtime'
            for name in ('paid-ai.json', 'paid-jev.json'):
                settings = json.loads((folder/name).read_text())
                self.assertIs(settings['enabled'], False)
                self.assertIs(settings['operator_authorized'], False)
            with contextlib.closing(sqlite3.connect(folder/'book.sqlite3')) as db, db:
                identity = db.execute("SELECT value FROM meta WHERE key='account_id'").fetchone()[0]
                db.execute("UPDATE meta SET value=? WHERE key='cash'", (json.dumps('900'),))
            saved_authorization = '{"enabled":false,"operator_authorized":false,"reference":"preserve me"}'
            (folder/'paid-jev.json').write_text(saved_authorization)
            self.assertEqual(helium_bot.main(['init', '--root', str(root)]), 0)
            with contextlib.closing(sqlite3.connect(folder/'book.sqlite3')) as db:
                self.assertEqual(db.execute("SELECT value FROM meta WHERE key='account_id'").fetchone()[0], identity)
                self.assertEqual(json.loads(db.execute("SELECT value FROM meta WHERE key='cash'").fetchone()[0]), '900')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 0)
            self.assertEqual((folder/'paid-jev.json').read_text(), saved_authorization)
            self.assertEqual(other.read_text(), 'existing account stays intact')
            self.assertFalse((root/'rug_desk/runtime').exists())
            self.assertFalse((root/'neon_bot/runtime').exists())

    def test_status_is_read_only_before_initialization(self):
        import helium_bot
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(helium_bot.main(['status', '--root', tmp]), 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_watch_retains_original_policy_when_entry_config_drifts(self):
        import helium_bot
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = self.make_root(tmp)
            helium_bot.main(['init', '--root', str(root)])
            path = root/'helium_bot/config.json'
            changed = json.loads(path.read_text())
            changed['stop_loss_pct'] = '0.50'
            path.write_text(json.dumps(changed))
            with patch('sniper.helium.service.watch', return_value=[]) as watch:
                self.assertEqual(helium_bot.main(['watch', '--root', str(root)]), 0)
                self.assertEqual(watch.call_args.args[1].cfg['stop_loss_pct'], '0.25')
            with patch('sniper.helium.service.scan', return_value=[]) as scan:
                with self.assertRaises(ValueError):
                    helium_bot.main(['scan', '--root', str(root)])
                scan.assert_not_called()


if __name__=='__main__': unittest.main()
