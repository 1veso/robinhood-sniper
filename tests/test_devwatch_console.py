"""DEV WATCH's seventh view must not trade, execute config, or merge accounts."""
import csv
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from sniper.console import ConsoleData, make_server
from sniper.devwatch_console import view, source_status
from sniper.signal_readiness import inventory

SOURCE = Path(__file__).resolve().parents[1]


class DevWatchConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root/'dev_watch'
        self.folder.mkdir()
        for name in ['config.py','book.py','facts.py','grok.py','prompts.py','scan.py','enter.py',
                     'rpc.py','watch.py','pulse.py','report.py','ARTICLE.txt','article-manifest.json',
                     'approved-live-exit-fix.json']:
            (self.folder/name).write_bytes((SOURCE/'dev_watch'/name).read_bytes())
        (self.folder/'.env').write_text('XAI_API_KEY=xai-DO-NOT-EXPOSE\n')

    def write(self, name, value):
        (self.folder/name).write_text(json.dumps(value))

    def rows(self,name,fields,rows):
        with (self.folder/name).open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)

    def test_empty_root_does_not_initialize_or_read_other_accounts(self):
        empty=self.root/'another-root'
        data=ConsoleData(empty)
        with patch.object(data,'jobs',return_value={}), patch.object(data,'solana',side_effect=AssertionError):
            result=data.state('devwatch')
        self.assertFalse(result['initialized'])
        self.assertEqual(result['execution_target'],'manual')
        self.assertFalse(empty.exists())

    def test_projection_separates_pending_alerts_from_realized_fills_and_costs(self):
        self.write('positions.json',{'mint':{'symbol':'ABC','qty':10,'entry':2,'pulse':'fading','opened':100,
                   'pending_exit':{'id':'confirm-id','qty':5,'reason':'x_fading'},'private_extra':'DO NOT RETURN'}})
        self.write('pending.json',{'proposal':{'symbol':'NEW','snap':{'private':'DO NOT RETURN'}}})
        self.rows('trades.csv',['time','mint','symbol','action','usd','pnl','reason'],
                  [dict(time='2026-10-31 17:00',mint='mint',symbol='ABC',action='partial',usd='8',pnl='-2',reason='stop')])
        self.rows('costs.csv',['time','job','usd','x_posts'],[dict(time='2026-10-31 17:00',job='scan',usd='.5',x_posts=3)])
        before={p.name:p.read_bytes() for p in self.folder.iterdir()}
        result=view(self.root)
        self.assertEqual(result['mode'],'MANUAL LIVE')
        self.assertEqual(result['summary']['realized_pnl'],'-2')
        self.assertEqual(result['summary']['net_realized_after_api'],'-2.5')
        self.assertEqual(result['positions'][0]['qty'],'10')
        self.assertEqual(result['positions'][0]['pending_exit']['id'],'confirm-id')
        self.assertNotIn('cash',result['summary'])
        self.assertNotIn('DO NOT',json.dumps(result))
        self.assertNotIn('xai-',json.dumps(result))
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.folder.iterdir()})

    def test_config_is_parsed_not_executed_and_manifest_mismatch_is_visible(self):
        marker=self.root/'unsafe'
        with (self.folder/'config.py').open('a') as f:
            f.write(f'\nopen({str(marker)!r}, "w").write("executed")\n')
        result=view(self.root)
        self.assertFalse(result['status']['sources_verified'])
        self.assertFalse(marker.exists())

    def test_secret_bearing_error_text_is_redacted(self):
        fields=['time','mint','symbol','stage','verdict','confidence','reason']
        self.rows('checks.csv',fields,[dict(zip(fields,['now','m','A','rpc','SKIP','','source https://provider.test/key/SECRET xai-SECRET Bearer SECRET api_key=SECRET']))])
        self.assertNotIn('SECRET',json.dumps(view(self.root)))

    def test_full_counts_are_not_limited_to_last_hundred_display_rows(self):
        fields=['time','mint','symbol','stage','verdict','confidence','reason']
        self.rows('checks.csv',fields,[dict(zip(fields,['now',str(i),'A','door','SKIP','','thin_pool'])) for i in range(130)])
        result=view(self.root)
        self.assertEqual(result['summary']['checked'],130)
        self.assertEqual(len(result['checks']),100)
        self.assertEqual(result['rejection_counts']['thin_pool'],130)

    def test_original_sources_and_only_approved_fix_are_verified(self):
        self.assertTrue(source_status(self.folder))
        with (self.folder/'facts.py').open('a') as f:f.write('\n# unauthorized change\n')
        self.assertFalse(source_status(self.folder))

    def test_readiness_waits_for_backup_without_reading_credentials(self):
        (self.root/'runtime').mkdir()
        (self.root/'runtime/xai-api-key.txt').write_text('SECRET')
        result=inventory(self.root)
        self.assertEqual(len(result['bots']),7)
        self.assertTrue(result['credential_file_present'])
        self.assertTrue(all(x['state']=='RESTORE_REQUIRED' for x in result['bots'][:6]))
        self.assertFalse(result['automated_real_money_execution_enabled'])
        self.assertNotIn('SECRET',json.dumps(result))
        self.assertFalse((self.root/'rug_desk').exists())

    def test_http_readonly_routes_and_asset(self):
        with patch.object(ConsoleData,'jobs',return_value={}):
            server=make_server(self.root,port=0)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                client=http.client.HTTPConnection('127.0.0.1',server.server_port)
                for route in ['/api/state?bot=devwatch','/api/signals','/api/health','/devwatch.js']:
                    client.request('GET',route);response=client.getresponse();body=response.read()
                    self.assertEqual(response.status,200,route)
                    self.assertNotIn(b'xai-DO-NOT-EXPOSE',body)
                    if route=='/api/health':self.assertEqual(len(json.loads(body)['bot_views']),7)
                client.request('POST','/api/devwatch/confirm',body='{}',headers={'Content-Type':'application/json'})
                response=client.getresponse();response.read();self.assertNotEqual(response.status,200)
                client.close()
            finally:
                server.shutdown();server.server_close();thread.join()

    def test_packaging_includes_source_and_excludes_live_data_and_keys(self):
        spec=importlib.util.spec_from_file_location('devwatch_packaging',SOURCE/'packaging/build_windows.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.write('positions.json',{})
        self.write('pending.json',{})
        self.write('costs.csv',{})
        (self.folder/'wallet-api-key.txt').write_text('SECRET')
        packaged={p.relative_to(self.root).as_posix() for p in module.source_files(self.root)}
        self.assertIn('dev_watch/book.py',packaged)
        self.assertIn('dev_watch/article-manifest.json',packaged)
        for name in ['.env','positions.json','pending.json','costs.csv','wallet-api-key.txt']:
            self.assertNotIn('dev_watch/'+name,packaged)


if __name__=='__main__': unittest.main()
