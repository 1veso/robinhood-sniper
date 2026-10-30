"""Run the dependency-free JavaScript cockpit behavior checks when Node is present."""
from pathlib import Path
import shutil
import subprocess
import unittest


class HeliumCockpitUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for JavaScript cockpit behavior checks')
    def test_recorded_events_freshness_and_notifications(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which('node'), str(root / 'tests/test_helium_cockpit_ui.js')],
            cwd=root, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
