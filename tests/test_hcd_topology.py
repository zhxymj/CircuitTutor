"""Real binary-mask fixtures for HCD's geometry-only integration."""
import importlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class HCDGeometryTests(unittest.TestCase):
    def setUp(self):
        try:
            self.hcd = importlib.import_module("core.integrations.hcd_topology")
        except ModuleNotFoundError:
            self.hcd = None
        self.assertIsNotNone(self.hcd, "HCD native geometry integration is missing")

    def assertUntrusted(self, result):
        self.assertEqual(result["status"], "geometry_candidates")
        self.assertTrue(result["candidate_only"])
        self.assertFalse(result["accepted"])
        self.assertFalse(result["electrical_validated"])
        self.assertEqual(result["topology"], {
            "status": "not_inferred", "pins": None, "connections": [], "nets": []})
        self.assertNotIn("circuit", result)
        json.dumps(result, allow_nan=False)

    def test_empty_mask_does_not_invent_wires(self):
        result = self.hcd.compute_wire_geometry(np.zeros((48, 80), np.uint8))
        self.assertEqual(result["segments"], [])
        self.assertEqual(result["points"], [])
        self.assertEqual(result["pixel_regions"], [])
        self.assertUntrusted(result)

    def test_line_is_extracted_from_actual_pixels_without_supplied_segments(self):
        mask = np.zeros((80, 100), np.uint8)
        mask[20, 10:91] = 255
        result = self.hcd.compute_wire_geometry(mask)
        self.assertGreaterEqual(len(result["segments"]), 1)
        self.assertGreaterEqual(len(result["points"]), 2)
        self.assertTrue(all(s["pixel_support"] >= 0.9 for s in result["segments"]))
        self.assertUntrusted(result)

    def test_unwired_supplied_segment_is_rejected_by_pixel_evidence(self):
        result = self.hcd.compute_wire_geometry(np.zeros((80, 100), np.uint8),
            line_segments=[[10, 20, 90, 20]])
        self.assertEqual(result["segments"], [])
        self.assertUntrusted(result)

    def test_component_box_removes_symbol_strokes_without_mutating_input(self):
        mask = np.zeros((80, 100), np.uint8)
        mask[20, 10:91] = 255
        original = mask.copy()
        result = self.hcd.compute_wire_geometry(mask,
            component_boxes=[{"x": 0, "y": 10, "w": 100, "h": 20}],
            line_segments=[[10, 20, 90, 20]])
        self.assertEqual(result["segments"], [])
        np.testing.assert_array_equal(mask, original)

    def test_reversed_duplicate_segments_are_removed(self):
        mask = np.zeros((80, 100), np.uint8)
        mask[20, 10:91] = 255
        result = self.hcd.compute_wire_geometry(mask,
            line_segments=[[10, 20, 90, 20], [90, 20, 10, 20]])
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(len(result["points"]), 2)

    def test_hcd_clustering_uses_cluster_medians_and_retains_noise(self):
        actual = self.hcd.merge_points_adaptive([(1, 1), (3, 1), (40, 30)],
            base_eps=3, min_samples=2)
        self.assertEqual(actual, [(2.0, 1.0), (40.0, 30.0)])

    def test_hcd_adaptive_eps_is_clamped_for_distant_points(self):
        self.assertEqual(self.hcd.estimate_eps([(0, 0), (100, 0), (200, 0)],
            k=1, min_eps=2, max_eps=9), 9.0)

    def test_hcd_sampler_measures_missing_pixels(self):
        mask = np.zeros((20, 20), np.uint8)
        mask[5, 2:7] = 255
        self.assertAlmostEqual(self.hcd.sample_line_connectivity(mask, 2, 5, 11, 5,
            radius=0), 0.5)

    def test_hcd_cc_filters_noise_and_does_not_join_disconnected_wires(self):
        mask = np.zeros((30, 40), np.uint8)
        mask[5, 2:20] = 255
        mask[20, 2:20] = 255
        mask[28, 38] = 255
        cc = self.hcd.build_wire_cc_index(mask, min_area=3)
        self.assertNotEqual(int(cc["labels"][5, 3]), int(cc["labels"][20, 3]))
        self.assertFalse(bool(cc["keep"][cc["labels"][28, 38]]))

    def test_hcd_snap_returns_evidence_without_clamping_outside_point(self):
        mask = np.zeros((30, 40), np.uint8)
        mask[5, 2:20] = 255
        cc = self.hcd.build_wire_cc_index(mask, min_area=3)
        x, y, label, distance = self.hcd.snap_point_to_cc(cc, 10, 7, max_r=3)
        self.assertEqual((x, y, distance), (10.0, 5.0, 2.0))
        self.assertGreater(label, 0)
        self.assertEqual(self.hcd.snap_point_to_cc(cc, -10, 5, max_r=3)[2], 0)

    def test_hcd_noise_filter_preserves_thin_wire_and_removes_single_dot(self):
        mask = np.zeros((30, 40), np.uint8)
        mask[5, 2:20] = 255
        mask[28, 38] = 255
        result = self.hcd.filter_noise_wires(mask, min_area=3, min_length=5)
        self.assertEqual(int(result[5, 10]), 255)
        self.assertEqual(int(result[28, 38]), 0)

    def test_crossing_without_dot_is_ambiguous_and_has_no_electrical_node(self):
        mask = np.zeros((100, 100), np.uint8)
        mask[50, 10:91] = 255
        mask[10:91, 50] = 255
        result = self.hcd.compute_wire_geometry(mask,
            line_segments=[[10, 50, 90, 50], [50, 10, 50, 90]])
        self.assertEqual(len(result["ambiguous_intersections"]), 1)
        crossing = result["ambiguous_intersections"][0]
        self.assertEqual((crossing["x"], crossing["y"]), (50.0, 50.0))
        self.assertEqual(crossing["kind"], "unconfirmed_crossing")
        self.assertUntrusted(result)

    def test_short_circuit_shaped_pixels_and_missing_ground_cannot_pass(self):
        mask = np.zeros((80, 100), np.uint8)
        mask[20, 10:91] = 255
        mask[60, 10:91] = 255
        mask[20:61, 10] = 255
        result = self.hcd.compute_wire_geometry(mask,
            line_segments=[[10, 20, 90, 20], [10, 60, 90, 60], [10, 20, 10, 60]])
        self.assertGreater(len(result["segments"]), 0)
        self.assertUntrusted(result)
        self.assertNotIn("ground", result)

    def test_unknown_component_ports_are_not_inferred_from_bbox(self):
        mask = np.zeros((80, 100), np.uint8)
        mask[20, 0:100] = 255
        result = self.hcd.compute_wire_geometry(mask,
            component_boxes=[{"x": 40, "y": 10, "w": 20, "h": 20}])
        self.assertUntrusted(result)
        self.assertNotIn("ports", result)

    def test_segments_are_bounded_and_manifest_identifies_real_sources(self):
        mask = np.ones((80, 100), np.uint8) * 255
        result = self.hcd.compute_wire_geometry(mask,
            line_segments=[[2, y, 90, y] for y in range(5, 65, 5)], max_segments=3)
        self.assertEqual(len(result["segments"]), 3)
        manifest = result["provenance"]
        self.assertEqual(manifest["source_commit"], "5e5a957f41bdb60f4835fb7c887452a6dc469520")
        self.assertIsNone(manifest["license"])
        self.assertGreaterEqual(len(manifest["functions"]), 6)
        self.assertUntrusted(result)

    def test_import_does_not_require_cv_or_sklearn_on_server(self):
        completed = subprocess.run([sys.executable, "-S", "-B", "-c",
            "import core.integrations.hcd_topology; import sys; "
            "assert not any(n in sys.modules for n in ('cv2','numpy','sklearn'))"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_invalid_mask_and_coordinates_are_rejected(self):
        for mask in (np.zeros((0, 4)), np.zeros((4, 4, 3)), np.ones((4, 4)) * np.nan):
            with self.subTest(shape=mask.shape):
                with self.assertRaises(ValueError):
                    self.hcd.compute_wire_geometry(mask)
        with self.assertRaises(ValueError):
            self.hcd.compute_wire_geometry(np.zeros((20, 20)), line_segments=[[0, 0, float("nan"), 3]])


if __name__ == "__main__":
    unittest.main()
