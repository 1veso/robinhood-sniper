"""Run the Pump / Jev shadow worker; no order, signer or live command."""
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from sniper.jev_review import run, view
from sniper.jev_dossier import encode

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('once','status'))
    parser.add_argument('--root',type=Path,default=ROOT)
    args=parser.parse_args()
    result=run(args.root) if args.command=='once' else view(args.root)
    print(encode(result))
    if args.command=='once' and result.get('errors'): sys.exit(1)
