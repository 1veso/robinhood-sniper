"""Explicit crew comparison initialization and bounded read-only coordination."""
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from sniper.crew import initialize,run,status
from sniper.crew_evidence import encode

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('init','run','status'))
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--authorization-reference')
    a=p.parse_args()
    if a.command=='init':
        if not a.authorization_reference:p.error('--authorization-reference is required for init')
        out=initialize(a.root,authorization_reference=a.authorization_reference)
    else:out=run(a.root) if a.command=='run' else status(a.root)
    print(encode(out))
    if a.command=='run' and out.get('status')=='ERROR':sys.exit(1)
