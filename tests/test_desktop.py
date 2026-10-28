import importlib.util
import json
from pathlib import Path
import tempfile
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

    def test_release_source_allowlist_excludes_private_runtime(self):
        spec=importlib.util.spec_from_file_location('build_windows',Path(__file__).parents[1]/'packaging/build_windows.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ('desktop.py','runtime/jupiter-server-api-key.txt','rug_desk/runtime/account.json','rug_desk/config.py','src/sniper/__pycache__/secret.py','config/paper.toml'):
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('sentinel')
            self.assertEqual({p.relative_to(root).as_posix() for p in module.source_files(root)}, {'desktop.py','rug_desk/config.py','config/paper.toml'})


if __name__=='__main__': unittest.main()
