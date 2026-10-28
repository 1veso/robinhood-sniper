"""Separate Neon paper bot. No live mode, wallet signer or transaction submission."""
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from rug_desk.book import encode
from sniper.neon import initialize,cycle,NeonBook,publish
import time


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--app-root',type=Path,default=ROOT)
    p.add_argument('command',choices=['initialize','signals','watch','status'])
    p.add_argument('--no-send',action='store_true')
    args=p.parse_args();app=args.app_root.resolve()
    if args.command=='initialize':out=initialize(app)
    elif args.command in ('signals','watch'):out=cycle(app,args.command,send=not args.no_send)
    else:
        book=NeonBook(app/'neon_bot')
        try:out=publish(book,app,int(time.time()))
        finally:book.close()
    print(encode({k:v for k,v in out.items() if k not in ('candidates','fills','positions','decisions')}))


if __name__=='__main__':main()
