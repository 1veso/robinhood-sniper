"""Optional, local analytical snapshots of the existing paper ledgers."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]

if __name__ == '__main__':
    from sniper.desk_analytics import main
    raise SystemExit(main(ROOT))
