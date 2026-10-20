import base64
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

# Keep direct execution (`python tests/test_upstream_vision.py`) equivalent to
# unittest discovery from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import upstream_vision
from tools import circuitvision_worker


PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(
    b"\x89PNG\r\n\x1a\nfixture"
).decode("ascii")


def worker_result():
    return {
        "status": "needs_manual_correction",
        "circuit": None,
        "accepted": False,
        "electrical_validated": False,
        "image": {
            "width": 320,
            "height": 240,
            "source_name": "sample.png",
            "sha256": "a" * 64,
            "exif_applied": True,
        },
        "detections": [{
            "class_id": 4,
            "class_name": "resistor",
            "type": "resistor",
            "score": 0.81,
            "box": {"x": 20, "y": 30, "w": 50, "h": 18},
            "pins": None,
            "value": None,
        }],
        "segmentation": {
            "status": "segmented",
            "mask_data_url": "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode("ascii"),
            "width": 320,
            "height": 240,
            "nonzero_pixels": 1200,
            "threshold": 0.0,
            "meaning": "foreground mask; electrical connectivity not inferred",
        },
        "topology": {"status": "not_inferred", "pins": None, "connections": [], "nets": []},
        "issues": [
            {"code": "VALUES_UNKNOWN", "message": "元件数值尚未确认"},
            {"code": "TOPOLOGY_NOT_INFERRED", "message": "电气连接尚未识别"},
            {"code": "ENGINEER_REVIEW_REQUIRED", "message": "请人工校正元件和连线"},
        ],
        "provenance": {
            "source_commit": "fc5b775043b577668906b6d0bbb238a690a82965",
            "checkpoint_sha256": "b" * 64,
            "runtime": {},
        },
    }


class UpstreamVisionBridgeTests(unittest.TestCase):
    def test_rgb_is_converted_to_bgr_before_model_pipeline(self):
        rgb = object()
        bgr = object()
        cv2_module = SimpleNamespace(COLOR_RGB2BGR=17, cvtColor=Mock(return_value=bgr))
        self.assertIs(circuitvision_worker._to_bgr(rgb, cv2_module), bgr)
        cv2_module.cvtColor.assert_called_once_with(rgb, 17)

    def test_only_exactly_supported_visual_classes_receive_type_hints(self):
        self.assertEqual(circuitvision_worker.TYPE_BY_CLASS.get("resistor"), "resistor")
        self.assertEqual(circuitvision_worker.TYPE_BY_CLASS.get("capacitor.unpolarized"), "capacitor")
        self.assertEqual(circuitvision_worker.TYPE_BY_CLASS.get("voltage.dc"), "voltage_source")
        for ambiguous in ("resistor.adjustable", "capacitor.polarized", "voltage.ac", "diode.light_emitting", "transistor.fet"):
            self.assertIsNone(circuitvision_worker.TYPE_BY_CLASS.get(ambiguous), ambiguous)

    def test_geometry_exclusion_preserves_junction_crossover_and_whole_circuit_pixels(self):
        detections = [{'class_name': name, 'box': {'x': index, 'y': 2, 'w': 3, 'h': 4}}
                      for index, name in enumerate(('junction', 'crossover', 'circuit', 'resistor', 'text', 'unknown'))]
        self.assertTrue(hasattr(circuitvision_worker, '_geometry_exclusion_boxes'),
                        'geometric markers must be preserved in the wire mask')
        boxes = circuitvision_worker._geometry_exclusion_boxes(detections)
        self.assertEqual(boxes, [{'x': 3, 'y': 2, 'w': 3, 'h': 4},
                                 {'x': 4, 'y': 2, 'w': 3, 'h': 4},
                                 {'x': 5, 'y': 2, 'w': 3, 'h': 4}])
        self.assertEqual([item['class_name'] for item in detections],
                         ['junction', 'crossover', 'circuit', 'resistor', 'text', 'unknown'])

    def test_rejects_non_image_data_url_as_invalid_request(self):
        with self.assertRaises(upstream_vision.UpstreamVisionError) as caught:
            upstream_vision.analyze_image("https://example.invalid/image.png")
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_reports_missing_isolated_runtime(self):
        with patch.object(upstream_vision, "_runtime_paths", return_value=None):
            with self.assertRaises(upstream_vision.UpstreamVisionError) as caught:
                upstream_vision.analyze_image(PNG_DATA_URL)
        self.assertEqual(caught.exception.code, "runtime_unavailable")

    def test_success_preserves_raw_unknown_values_and_never_accepts_circuit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            python = root / "python.exe"
            worker = root / "worker.py"
            worktree = root / "CircuitVision"
            python.touch()
            worker.touch()
            worktree.mkdir()
            paths = SimpleNamespace(python=python, worker=worker, worktree=worktree)
            expected = worker_result()
            process = Mock()
            process.returncode = 0
            process.communicate.return_value = (json.dumps(expected).encode(), b"model logs")
            with patch.object(upstream_vision, "_runtime_paths", return_value=paths), \
                    patch.object(upstream_vision, "_runtime_ready", return_value=True), \
                    patch.object(upstream_vision.subprocess, "Popen", return_value=process) as popen:
                result = upstream_vision.analyze_image(PNG_DATA_URL, source_name="sample.png")

        self.assertEqual(result["detections"][0]["class_name"], "resistor")
        self.assertIsNone(result["detections"][0]["pins"])
        self.assertIsNone(result["detections"][0]["value"])
        self.assertEqual(result["topology"], {"status": "not_inferred", "pins": None, "connections": [], "nets": []})
        self.assertIsNone(result["circuit"])
        self.assertFalse(result["accepted"])
        self.assertFalse(result["electrical_validated"])
        process.communicate.assert_called_once_with(timeout=180)
        child_env = popen.call_args.kwargs["env"]
        self.assertEqual(child_env["PYTHON_DOTENV_DISABLED"], "1")
        for key_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
            self.assertNotIn(key_name, child_env)

    def test_maps_malformed_worker_stdout_to_inference_failed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            python = root / "python.exe"
            worker = root / "worker.py"
            worktree = root / "CircuitVision"
            python.touch()
            worker.touch()
            worktree.mkdir()
            paths = SimpleNamespace(python=python, worker=worker, worktree=worktree)
            process = Mock()
            process.returncode = 0
            process.communicate.return_value = (b"not-json", b"")
            with patch.object(upstream_vision, "_runtime_paths", return_value=paths), \
                    patch.object(upstream_vision, "_runtime_ready", return_value=True), \
                    patch.object(upstream_vision.subprocess, "Popen", return_value=process):
                with self.assertRaises(upstream_vision.UpstreamVisionError) as caught:
                    upstream_vision.analyze_image(PNG_DATA_URL)
        self.assertEqual(caught.exception.code, "inference_failed")

    def test_rejects_non_finite_or_mismatched_worker_result(self):
        invalid = worker_result()
        invalid["detections"][0]["score"] = math.nan
        with self.assertRaises(upstream_vision.UpstreamVisionError) as caught:
            upstream_vision._validate_worker_result(invalid)
        self.assertEqual(caught.exception.code, "inference_failed")

        invalid = worker_result()
        invalid["segmentation"]["width"] = 319
        with self.assertRaises(upstream_vision.UpstreamVisionError) as caught:
            upstream_vision._validate_worker_result(invalid)
        self.assertEqual(caught.exception.code, "inference_failed")

    def test_rejects_geometry_that_claims_electrical_acceptance(self):
        invalid = worker_result()
        invalid['wire_geometry'] = {'status': 'geometry_candidates', 'candidate_only': True,
                                    'accepted': True, 'electrical_validated': False,
                                    'width': 320, 'height': 240, 'segments': [], 'points': []}
        with self.assertRaises(upstream_vision.UpstreamVisionError) as caught:
            upstream_vision._validate_worker_result(invalid)
        self.assertEqual(caught.exception.code, 'inference_failed')

        invalid['wire_geometry']['accepted'] = False
        invalid['wire_geometry']['segments'] = [{'x1': 0, 'y1': 0, 'x2': math.nan, 'y2': 1}]
        with self.assertRaises(upstream_vision.UpstreamVisionError) as caught:
            upstream_vision._validate_worker_result(invalid)
        self.assertEqual(caught.exception.code, "inference_failed")

    def test_timeout_terminates_only_the_worker_process_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            python = root / "python.exe"
            worker = root / "worker.py"
            worktree = root / "CircuitVision"
            python.touch()
            worker.touch()
            worktree.mkdir()
            paths = SimpleNamespace(python=python, worker=worker, worktree=worktree)
            process = Mock()
            process.pid = 42123
            process.returncode = 1
            process.communicate.side_effect = [upstream_vision.subprocess.TimeoutExpired("worker", 90), (b"", b"")]
            with patch.object(upstream_vision, "_runtime_paths", return_value=paths), \
                    patch.object(upstream_vision, "_runtime_ready", return_value=True), \
                    patch.object(upstream_vision.subprocess, "Popen", return_value=process), \
                    patch.object(upstream_vision.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as taskkill, \
                    patch.object(upstream_vision.os, "name", "nt"):
                with self.assertRaises(upstream_vision.UpstreamVisionError) as caught:
                    upstream_vision.analyze_image(PNG_DATA_URL)
        self.assertEqual(caught.exception.code, "timeout")
        taskkill.assert_called_once()
        self.assertEqual(taskkill.call_args.args[0], ["taskkill", "/PID", "42123", "/T", "/F"])
        self.assertFalse(taskkill.call_args.kwargs.get('text', False),
                         'unused localized taskkill output must not be decoded as UTF-8')
        process.communicate.assert_called()
        process.kill.assert_not_called()

    def test_returns_busy_without_starting_a_second_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            python = root / "python.exe"
            worker = root / "worker.py"
            worktree = root / "CircuitVision"
            python.touch()
            worker.touch()
            worktree.mkdir()
            paths = SimpleNamespace(python=python, worker=worker, worktree=worktree)
            with patch.object(upstream_vision, "_runtime_paths", return_value=paths), \
                    patch.object(upstream_vision, "_runtime_ready", return_value=True), \
                    patch.object(upstream_vision.subprocess, "run") as run:
                self.assertTrue(upstream_vision._WORKER_LOCK.acquire(blocking=False))
                try:
                    with self.assertRaises(upstream_vision.UpstreamVisionError) as caught:
                        upstream_vision.analyze_image(PNG_DATA_URL)
                finally:
                    upstream_vision._WORKER_LOCK.release()
        self.assertEqual(caught.exception.code, "busy")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
