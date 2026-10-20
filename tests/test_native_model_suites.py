"""Run numeric integration suites in their already provisioned model environment."""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.upstream_vision import _runtime_paths


class NativeModelSuites(unittest.TestCase):
    def test_existing_vision_environment_runs_native_numeric_suites(self):
        runtime = _runtime_paths()
        self.assertTrue(runtime.python.is_file(),
                        'INCOMPLETE: existing CircuitVision venv is unavailable; numeric suites did not run')
        for name in ('test_native_circuitvision.py', 'test_hcd_topology.py'):
            with self.subTest(suite=name):
                result = subprocess.run([str(runtime.python), '-B', str(ROOT/'tests'/name)],
                                        cwd=str(ROOT), capture_output=True, text=True,
                                        encoding='utf-8', errors='replace', timeout=240)
                if result.stdout:
                    print(result.stdout, end='')
                if result.stderr:
                    print(result.stderr, end='', file=sys.stderr)
                self.assertEqual(result.returncode, 0, name + ' failed in the existing model environment')


if __name__ == '__main__':
    unittest.main()
