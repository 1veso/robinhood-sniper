"""Cross-platform offline tests without installation or PYTHONPATH configuration."""
from pathlib import Path
import sys
import unittest

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / "src"))
sys.path.insert(0, str(root))  # Embedded Python does not add the script directory.
suite = unittest.defaultTestLoader.discover(str(root / "tests"))
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
