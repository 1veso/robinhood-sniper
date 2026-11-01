import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from rug_desk.book import Book
from sniper.paper_trial import start_observation


class TrialTests(unittest.TestCase):
    def test_window_does_not_reset_or_invent_fills_and_stops_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            book=Book(Path(tmp)/'rug_desk',initialize=True)
            try:
                with patch('sniper.paper_trial.status',return_value={'enabled':True}):
                    first=start_observation(book,1800000000,'Explicit test request',['Incomplete factual evidence'])
                    again=start_observation(book,1800000010,'Repeated request',[])
                self.assertEqual(first,again)
                self.assertEqual(first['ends_at']-first['started_at'],604800)
                self.assertIn('closed',book.limits('fixture',first['ends_at']))
                self.assertEqual(book.fills(),[])
                self.assertEqual(first['entry_blockers_at_start'],['Incomplete factual evidence'])
                with patch('sniper.paper_trial.status',return_value={'enabled':False}):
                    with self.assertRaises(ValueError):start_observation(book,1800000010,'Request',[])
            finally:book.close()
