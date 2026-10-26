"""Run without installing packages: python run.py --help."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from sniper.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
