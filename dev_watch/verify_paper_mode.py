"""Offline paper entry/exit and Unicode-log proof; no requests or real ledger writes."""
import csv
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent
os.chdir(ROOT)
import book
import config
import enter
from verify_article import verify


def main():
    verify()
    assert config.PAPER is True and enter.PAPER is True
    assert sys.flags.utf8_mode, 'Use python -X utf8 for Windows-safe article logging'
    scratch=ROOT/'runtime/verification';scratch.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as folder:
        folder=Path(folder)
        verdict={'verdict':'BUY','stage':'grok','symbol':'検証🧪','confidence':0.9,
                 'price':1,'liq_usd':40000,'snap':{'creator':'fixture','dev0':100,
                 'dev0_pct':1,'holders0':{'fixture-account':500}}}
        with patch.object(book,'POSITIONS',str(folder/'positions.json')), \
             patch.object(book,'TRADES',str(folder/'trades.csv')), \
             patch.object(enter,'CHECKS',str(folder/'checks.csv')), \
             patch.object(enter,'PENDING',str(folder/'pending.json')), \
             patch.object(enter,'check',return_value=verdict):
            result=enter.run('OFFLINE_PAPER_FIXTURE')
            assert result['action'].startswith('PAPER BUY')
            assert book.load()['OFFLINE_PAPER_FIXTURE']['qty']==25
            assert not (folder/'pending.json').exists()
            assert book.sell('OFFLINE_PAPER_FIXTURE',1,2,'offline_fixture')==25
            assert book.load()=={}
            with (folder/'checks.csv').open(encoding='utf-8',newline='') as handle:
                assert next(csv.DictReader(handle))['symbol']=='検証🧪'
    print('PASS: isolated paper entry and exit, simulated P&L, Unicode CSV and no pending live order')


if __name__=='__main__':main()
