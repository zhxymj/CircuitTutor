"""The built-in vision implementation must run without upstream application code."""
import importlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import upstream_vision


@unittest.skipUnless(importlib.util.find_spec('torch'), 'run in the existing CircuitVision venv')
class NativeCircuitVisionTests(unittest.TestCase):
    def native(self):
        spec = importlib.util.find_spec('core.integrations')
        self.assertIsNotNone(spec, 'CircuitVision implementation must be built into core')
        return importlib.import_module('core.integrations.circuitvision.circuit_analyzer')

    def test_yolo_boxes_preserve_original_class_and_coordinates(self):
        module = self.native()
        import torch
        analyzer = object.__new__(module.CircuitAnalyzer)
        boxes = SimpleNamespace(cls=torch.tensor([7.0]), conf=torch.tensor([0.75]),
                                xyxy=torch.tensor([[1.2, 2.7, 10.1, 12.8]]))
        class LocalPredictor:
            def predict(self, image, *, verbose, device):
                if device != 'cpu':
                    raise AssertionError('inference must explicitly use CPU')
                return [SimpleNamespace(boxes=boxes, names={7: 'voltage.ac'})]
        analyzer.yolo = LocalPredictor()
        self.assertEqual(analyzer.bboxes(None), [{
            'class': 'voltage.ac', '_yolo_class_id_temp': 7, 'confidence': 0.75,
            'xmin': 1, 'ymin': 3, 'xmax': 10, 'ymax': 13,
            'persistent_uid': 'voltage.ac_1_3_10_13'}])

    def test_segmentation_keeps_original_resolution_and_strict_positive_threshold(self):
        module = self.native()
        import numpy as np
        import torch
        analyzer = object.__new__(module.CircuitAnalyzer)
        analyzer.use_sam2 = True
        analyzer.sam2_device = torch.device('cpu')
        analyzer.debug = False
        class Transform:
            def __call__(self, image):
                return torch.zeros(3, 2, 2)
            def postprocess_masks(self, mask, orig_hw):
                if orig_hw != (2, 3):
                    raise AssertionError('original dimensions lost')
                return torch.tensor([[[[-1., 0., 1.], [2., -2., 0.]]]])
        class Model(torch.nn.Module):
            def forward(self, image):
                return torch.zeros(1, 1, 2, 2), None, None
        analyzer.sam2_transforms = Transform()
        analyzer.sam2_model = Model()
        mask, overlay, extent = analyzer.segment_with_sam2(np.zeros((2, 3, 3), dtype=np.uint8))
        self.assertEqual(mask.tolist(), [[0, 0, 255], [255, 0, 0]])
        self.assertEqual(extent, (0, 0, 3, 2))
        self.assertEqual(overlay[0, 2].tolist(), [0, 255, 0])

    def test_transform_postprocess_interpolates_logits_without_cuda_helpers(self):
        self.native()
        module = importlib.import_module('core.integrations.circuitvision.sam2_infer')
        import torch
        transform = module.SAM2Transforms(2, 0, 0, 0)
        output = transform.postprocess_masks(torch.tensor([[[[-1., 1.], [-1., 1.]]]]), (2, 3))
        self.assertEqual(tuple(output.shape), (1, 1, 2, 3))
        self.assertEqual(output[0, 0, 0].tolist(), [-1., 0., 1.])
        with self.assertRaises(ValueError):
            module.MultiKernelRefinement(kernel_sizes=[2])

    def test_runtime_readiness_only_requires_local_code_and_frozen_model_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = SimpleNamespace(python=root/'python.exe', worker=ROOT/'tools/circuitvision_worker.py', worktree=root/'models-root')
            paths.python.touch()
            for relative in ('models/configs/sam2.1_hiera_l.yaml', 'models/YOLO/best_large_model_yolo.pt',
                             'models/SAM2/sam2.1_hiera_large.pt', 'models/SAM2/best_miou_model_SAM_latest.pth'):
                target = paths.worktree/relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
            self.assertTrue(upstream_vision._runtime_ready(paths), 'app.py and src/*.py must not be runtime prerequisites')

    def test_wire_geometry_preserves_junction_crossing_and_circuit_region(self):
        import numpy as np
        from PIL import Image
        from tools import circuitvision_worker as worker
        from core.integrations.hcd_topology import compute_wire_geometry
        self.native()
        # No checkpoint is loaded by this fixture; exercise the worker geometry
        # path independently of Ultralytics' import-time environment setting.
        mask = np.zeros((80, 120), dtype=np.uint8)
        mask[39:42, 5:115] = 255
        original_mask = mask.copy()
        raw = [dict(**{'class': name}, _yolo_class_id_temp=index, confidence=0.8,
                    xmin=left, ymin=top, xmax=right, ymax=bottom)
               for index, (name, left, top, right, bottom) in enumerate([
                   ('circuit', 0, 0, 120, 80), ('junction', 20, 35, 30, 45),
                   ('crossover', 40, 35, 50, 45), ('vss', 60, 35, 70, 45),
                   ('resistor', 90, 10, 100, 20)])]
        analyzer = SimpleNamespace(use_sam2=True, sam2_model=object(), sam2_transforms=object(),
                                   yolo=None, bboxes=lambda _: raw,
                                   segment_with_sam2=lambda _: (mask, None, None))
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory)/'circuit.png'
            Image.new('RGB', (120, 80), 'white').save(image)
            with patch('core.integrations.circuitvision.circuit_analyzer.CircuitAnalyzer', return_value=analyzer), \
                    patch('ultralytics.nn.tasks.SAFE_LOAD', True), \
                    patch.object(worker, '_sha256', side_effect=list(worker.EXPECTED_CHECKPOINTS.values())), \
                    patch('core.integrations.hcd_topology.compute_wire_geometry', wraps=compute_wire_geometry) as geometry:
                result = worker.analyze_file(image, image.name, 20_000_000)
        self.assertGreater(len(result['wire_geometry']['segments']), 0,
                           'circuit/junction/crossover/vss boxes must preserve visible wire pixels')
        self.assertEqual(geometry.call_args.kwargs['component_boxes'], [{'x':90,'y':10,'w':10,'h':10}])
        self.assertEqual([item['class_name'] for item in result['detections']], [item['class'] for item in raw])
        self.assertTrue(np.array_equal(mask, original_mask), 'the original SAM2 mask must remain unchanged')
        self.assertEqual(result['segmentation']['nonzero_pixels'], int(np.count_nonzero(original_mask)))
        self.assertEqual(result['topology'], {'status':'not_inferred','pins':None,'connections':[],'nets':[]})


if __name__ == '__main__':
    unittest.main()
