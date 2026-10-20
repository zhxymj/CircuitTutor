"""Opt-in real CPU inference while upstream source imports are unavailable.

Run with the existing CircuitVision venv: python -B tests/test_native_circuitvision_smoke.py
This is an upstream smoke sample, not a held-out accuracy evaluation.
"""
import contextlib
import hashlib
import importlib.abc
import io
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:] = [str(ROOT)] + [p for p in sys.path if p and 'worktree' not in p.lower() and 'third_party' not in p.lower()]
from core import upstream_vision
from tools import circuitvision_worker


class BlockUpstream(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'src' or fullname.startswith('src.'):
            raise ImportError('Upstream CircuitVision Python source deliberately unavailable')
        if path and any('worktree' in str(p).lower() or 'third_party' in str(p).lower() for p in path):
            raise ImportError('Evidence and third_party Python source deliberately unavailable')
        return None


@unittest.skipUnless(__name__ == '__main__' or os.environ.get('NATIVE_VISION_REAL_SMOKE') == '1',
                     'opt-in real CPU inference; run this script directly')
class NativeCircuitVisionSmoke(unittest.TestCase):
    def test_real_upstream_sample_without_upstream_source_imports(self):
        finder = BlockUpstream()
        sys.meta_path.insert(0, finder)
        image_path = ROOT/'third_party/upstream/CircuitVision/static/images/circuits_1.jpg'
        log = io.StringIO()
        try:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                try:
                    result = circuitvision_worker.analyze_file(image_path, image_path.name, 20_000_000)
                except Exception as exc:
                    self.fail('Built-in inference must not require upstream Python source: ' + repr(exc))
        finally:
            sys.meta_path.remove(finder)
        upstream_vision._validate_worker_result(result)
        self.assertEqual(len(result['detections']), 14)
        self.assertEqual(result['segmentation']['nonzero_pixels'], 26685)
        self.assertEqual(result['topology'], {'status':'not_inferred','pins':None,'connections':[],'nets':[]})
        self.assertTrue(result['wire_geometry']['candidate_only'])
        self.assertFalse(result['wire_geometry']['accepted'])
        self.assertEqual(result['provenance']['parameters']['device'], 'cpu')
        loaded = {name:str(getattr(module,'__file__','')) for name,module in sys.modules.items()
                  if name.startswith('core.integrations.circuitvision')}
        self.assertTrue(loaded)
        self.assertTrue(all('core' in value and 'evidence' not in value for value in loaded.values()))
        evidence = {
            'sample':str(image_path.relative_to(ROOT)),
            'sample_sha256':hashlib.sha256(image_path.read_bytes()).hexdigest(),
            'scope':'real upstream smoke; not holdout accuracy; upstream Python imports blocked',
            'imports_blocked':['src','evidence/**/worktree/**','third_party/** Python modules'],
            'runtime_modules':loaded,
            'result':result,
        }
        target=ROOT/'evidence/upgrade/native_integration/circuitvision.json'
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__ == '__main__':
    unittest.main()
