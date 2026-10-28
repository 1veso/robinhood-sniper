"""Review worker and evidence-bound promotions. Never signs or submits a trade."""
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from sniper.jev_review import run, view
from sniper.jev_dossier import encode

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('once','status','readiness','promote'))
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--to',choices=('ACTIVE_PAPER','LIVE_READY','LIVE'))
    parser.add_argument('--evidence',type=Path)
    parser.add_argument('--authorization-reference')
    parser.add_argument('--paper-account-id')
    parser.add_argument('--live-account-id')
    args=parser.parse_args()
    if args.command=='promote':
        import json
        from sniper.jev_lifecycle import record
        if not args.evidence:parser.error('--evidence is required')
        result=record(args.root,args.to,json.loads(args.evidence.read_text(encoding='utf-8')),
            args.authorization_reference,paper_account_id=args.paper_account_id,live_account_id=args.live_account_id)
    elif args.command=='readiness':
        from sniper.jev_readiness import write_report
        result=write_report(args.root)
    else:result=run(args.root) if args.command=='once' else view(args.root)
    print(encode(result))
    if args.command=='once' and result.get('errors'): sys.exit(1)
