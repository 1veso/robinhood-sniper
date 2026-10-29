"""Bounded Windows snapshot publication retries never repeat trading work."""
import json
import os
from pathlib import Path
import tempfile
import threading
from unittest import TestCase, main
from unittest.mock import patch

from rug_desk.book import atomic


class AtomicPublicationTests(TestCase):
    def test_native_open_reader_release_allows_same_report_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            atomic(path, '{"generation":1,"fills":2}')
            held = path.open('rb')
            started, finished = threading.Event(), threading.Event()
            errors = []
            def publish():
                started.set()
                try:
                    atomic(path, '{"generation":2,"fills":2}')
                except Exception as exc:
                    errors.append(exc)
                finally:
                    finished.set()
            worker = threading.Thread(target=publish)
            try:
                worker.start()
                self.assertTrue(started.wait(2))
                if os.name == 'nt':
                    self.assertFalse(finished.wait(.08))
                self.assertEqual(json.load(held), {'generation': 1, 'fills': 2})
            finally:
                held.close()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(json.loads(path.read_text()), {'generation': 2, 'fills': 2})
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_retry_exhaustion_preserves_prior_report_and_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            atomic(path, 'old')
            error = PermissionError('sharing violation')
            error.winerror = 32
            with patch('rug_desk.book.os.replace', side_effect=error) as replace, \
                 patch('rug_desk.book.time.sleep') as sleep, \
                 patch('rug_desk.book.time.monotonic', return_value=100):
                with self.assertRaises(PermissionError):
                    atomic(path, 'new')
            self.assertEqual(replace.call_count, 8)
            self.assertEqual(sleep.call_count, 7)
            self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 1)
            self.assertEqual(path.read_text(), 'old')
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_unrelated_permission_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            atomic(path, 'old')
            with patch('rug_desk.book.os.replace', side_effect=PermissionError('denied')) as replace, \
                 patch('rug_desk.book.time.sleep') as sleep:
                with self.assertRaises(PermissionError):
                    atomic(path, 'new')
            self.assertEqual(replace.call_count, 1)
            sleep.assert_not_called()
            self.assertEqual(path.read_text(), 'old')


if __name__ == '__main__':
    main()
