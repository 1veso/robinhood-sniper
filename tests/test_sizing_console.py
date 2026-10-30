import http.client
from pathlib import Path
import tempfile
import threading
import unittest
from sniper.console import make_server


class SizingConsoleTests(unittest.TestCase):
    def test_fixed_derived_reports_are_read_only_and_do_not_initialize(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server = make_server(root, port=0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            def get(path):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                try:
                    conn.request('GET', path)
                    response = conn.getresponse()
                    return response.status, response.read()
                finally: conn.close()
            try:
                self.assertEqual(get('/api/radeon-sizing')[0], 404)
                folder = root/'runtime/radeon-sizing'
                self.assertFalse(folder.exists())
                folder.mkdir(parents=True)
                (folder/'report.json').write_text('{"excluded_from_strategy":true}')
                (folder/'report.html').write_text('<h1>Separate size study</h1>')
                self.assertEqual(get('/api/radeon-sizing'), (200, b'{"excluded_from_strategy":true}'))
                self.assertEqual(get('/radeon-sizing'), (200, b'<h1>Separate size study</h1>'))
                status, css = get('/sizing.css')
                self.assertEqual(status, 200)
                self.assertIn(b'overflow:auto', css)
                self.assertFalse(list(root.rglob('*.sqlite3')))
            finally:
                server.shutdown(); worker.join(5); server.server_close()
