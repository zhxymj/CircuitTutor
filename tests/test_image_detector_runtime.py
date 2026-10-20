"""Portable detector runtime, device forwarding, and restricted loading gates."""
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import image_detector


class DetectorRuntimeTests(unittest.TestCase):
    def test_default_runtime_finds_sibling_toolchain_after_move(self):
        expected = ROOT.parents[1] / "智绘电路工具链"
        self.assertEqual(image_detector.RUNTIME_ROOT, expected)
        self.assertTrue((image_detector.MODEL_ROOT / "acquisition.json").is_file())

    def test_configured_runtime_selects_checkpoint_location(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            env = dict(os.environ, CIRCUIT_VISION_RUNTIME_ROOT=directory,
                       PYTHONPATH=str(ROOT))
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-c",
                 "from core.image_detector import MODEL_PATH; print(MODEL_PATH)"],
                env=env, capture_output=True, text=True, encoding="utf8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(result.stdout.strip()), Path(directory) / "vision-models" / "hcd" / "best.pt")

    def test_requested_device_is_used_and_reported(self):
        self.assertIn("device", inspect.signature(image_detector.detect_image).parameters)
        from PIL import Image
        calls = []
        def predict(**kwargs):
            calls.append(kwargs)
            return [SimpleNamespace(boxes=SimpleNamespace(data=SimpleNamespace(
                cpu=lambda: SimpleNamespace(tolist=lambda: []))))]
        model = SimpleNamespace(names={0: "resistor"}, predict=predict)
        audit = {"repository": "test", "revision": "test", "archive_sha256_expected": "0",
                 "checkpoint": {"sha256": "0"}, "rights_status": "test"}
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            filename = Path(directory) / "input.png"
            Image.new("RGB", (16, 16), "white").save(filename)
            with patch.object(image_detector, "_load_model", return_value=(model, audit)), \
                    patch.object(image_detector.importlib.metadata, "version", return_value="test"):
                result = image_detector.detect_image(filename, image_size=640, device="0")
        self.assertEqual(calls[0]["device"], "0")
        self.assertEqual(result["parameters"]["device"], "0")
        self.assertEqual(result["detections"], [])

    def test_restricted_loading_uses_supported_environment_value(self):
        self._load_fixture(safe_load=True)
        self.assertEqual(os.environ["ULTRALYTICS_SAFE_LOAD"], "true")

    def test_runtime_without_restricted_loading_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "restricted checkpoint loading"):
            self._load_fixture(safe_load=False)

    def test_official_argument_container_is_allowed_only_during_loading(self):
        import torch
        before = list(torch.serialization.get_safe_globals())
        model, _ = self._load_fixture(safe_load=True)
        self.assertTrue(model["namespace_allowed"])
        self.assertEqual(torch.serialization.get_safe_globals(), before)

    def test_legacy_training_metadata_loads_with_restricted_pickle(self):
        self.assertTrue(hasattr(image_detector, "_checkpoint_globals"))
        import numpy as np
        import torch
        from ultralytics.utils import IterableSimpleNamespace
        from ultralytics.utils.loss import E2EDetectLoss, v8DetectionLoss, BboxLoss, DFLoss
        from ultralytics.utils.tal import TaskAlignedAssigner
        metadata = {"args": IterableSimpleNamespace(conf=0.25), "fitness": np.float64(0.5)}
        for cls in (E2EDetectLoss, v8DetectionLoss, BboxLoss, DFLoss, TaskAlignedAssigner):
            metadata[cls.__name__] = cls.__new__(cls)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            checkpoint = Path(directory) / "metadata.pt"
            torch.save(metadata, checkpoint)
            before = list(torch.serialization.get_safe_globals())
            with image_detector._checkpoint_globals():
                loaded = torch.load(checkpoint, weights_only=True)
            self.assertEqual(loaded["args"].conf, 0.25)
            self.assertEqual(loaded["fitness"], 0.5)
            self.assertIsInstance(loaded["E2EDetectLoss"], E2EDetectLoss)
            self.assertEqual(torch.serialization.get_safe_globals(), before)

    def _load_fixture(self, safe_load):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            checkpoint = root / "best.pt"
            checkpoint.write_bytes(b"test checkpoint")
            audit = {"archive_sha256_verified": True, "checkpoint": {
                "zip_member_crc_verified": True,
                "sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest()}}
            (root / "acquisition.json").write_text(json.dumps(audit), encoding="utf8")
            import torch
            import ultralytics.utils.loss
            import ultralytics.utils.tal
            package = ModuleType("ultralytics")
            package.YOLO = lambda *args, **kwargs: {
                "namespace_allowed": SimpleNamespace in torch.serialization.get_safe_globals()}
            utils = ModuleType("ultralytics.utils")
            utils.IterableSimpleNamespace = SimpleNamespace
            package.utils = utils
            nn = ModuleType("ultralytics.nn")
            tasks = ModuleType("ultralytics.nn.tasks")
            tasks.SAFE_LOAD = safe_load
            package.nn = nn
            nn.tasks = tasks
            with patch.object(image_detector, "MODEL_ROOT", root), \
                    patch.object(image_detector, "MODEL_PATH", checkpoint), \
                    patch.object(image_detector, "_model", None), \
                    patch.object(image_detector, "_audit", None), \
                    patch.dict(sys.modules, {"ultralytics": package, "ultralytics.utils": utils, "ultralytics.nn": nn,
                                             "ultralytics.nn.tasks": tasks}):
                return image_detector._load_model()


if __name__ == "__main__":
    unittest.main()
