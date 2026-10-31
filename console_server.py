"""Local observation console with bounded Radeon paper entry switches."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from sniper.console import main

if __name__ == '__main__':
    main(Path(__file__).resolve().parent)
