"""Integrity and non-inflation checks for the new requirement corpus."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import json
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluation import requirements_v2


class RequirementsV2Tests(unittest.TestCase):
    def setUp(self):
        self.dataset = requirements_v2.load_dataset()

    def test_requested_internal_counts_and_labels(self):
        counts = requirements_v2.validate_dataset(self.dataset)
        self.assertEqual((counts["case_groups"], counts["tasks"]), (30, 80))
        self.assertEqual(counts["split_tasks"], {"dev":60, "holdout":20})
        self.assertEqual(counts["categories"], {"explicit":30, "ambiguous":20,
                                              "unsupported_model":15, "negative":15})
        self.assertEqual(counts["split_categories"]["holdout"], {"explicit":10, "ambiguous":3,
                                                               "unsupported_model":4, "negative":3})
        self.assertFalse(counts["T03_complete"])
        self.assertEqual(counts["external_reference_circuits"], 0)

    def test_case_variants_cannot_leak_between_splits(self):
        tampered = deepcopy(self.dataset)
        tampered["tasks"][0]["split"] = "holdout"
        with self.assertRaisesRegex(ValueError, "split mismatch"):
            requirements_v2.validate_dataset(tampered)

    def test_duplicate_text_and_unearned_external_claim_rejected(self):
        duplicate = deepcopy(self.dataset)
        duplicate["tasks"][-1]["prompt"] = "  " + duplicate["tasks"][0]["prompt"]
        with self.assertRaisesRegex(ValueError, "repeated normalized prompt"):
            requirements_v2.validate_dataset(duplicate)
        inflated = deepcopy(self.dataset)
        inflated["metadata"]["T03_external_circuit_goal_met"] = True
        with self.assertRaisesRegex(ValueError, "must remain unmet"):
            requirements_v2.validate_dataset(inflated)

    def test_source_builder_is_reproducible_across_two_calls(self):
        path = ROOT / "data" / "requirements_v2" / "build_dataset.py"
        spec = importlib.util.spec_from_file_location("requirements_original_source", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.build_dataset(), module.build_dataset())
        self.assertEqual(module.build_dataset(), self.dataset)

    def test_split_manifest_tracks_data_and_related_family_overlap(self):
        manifest = json.loads((ROOT / "data" / "requirements_v2" / "split_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["dataset_sha256"], requirements_v2.fingerprint(self.dataset))
        dev, holdout = manifest["partitions"]["dev"], manifest["partitions"]["holdout"]
        self.assertFalse(set(dev["case_ids"]) & set(holdout["case_ids"]))
        self.assertFalse(set(dev["task_ids"]) & set(holdout["task_ids"]))
        # Shared broad families are visible rather than presented as independence.
        self.assertTrue(set(dev["related_families"]) & set(holdout["related_families"]))
        report = json.loads((ROOT / "data" / "requirements_v2" / "offline_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["dataset_sha256"], manifest["dataset_sha256"])
        self.assertEqual({row["task_id"] for row in report["records"]},
                         set(dev["task_ids"]) | set(holdout["task_ids"]))

    def test_offline_audit_preserves_unrun_engineering_metrics(self):
        report = requirements_v2.run_offline(self.dataset)
        self.assertEqual(report["summary"]["tasks"], 80)
        self.assertEqual(report["by_split"]["holdout"]["tasks"], 20)
        self.assertEqual(report["dataset_sha256"], requirements_v2.fingerprint(self.dataset))
        for row in report["records"]:
            self.assertEqual(row["model_calls"], 0)
            self.assertEqual(row["generation_path"], "not_run_offline")
            self.assertIsNone(row["structure_valid"])
            self.assertIsNone(row["erc_passed"])
            self.assertIsNone(row["template_fallback"])
            if row["category"] != "negative":
                self.assertIsNone(row["negative_rule_detection"])
        negative_rows = [row for row in report["records"] if row["category"] == "negative"]
        self.assertEqual(len(negative_rows), 15)
        self.assertTrue(all(row["expected_negative_rule_ids"] for row in negative_rows))

    def test_explicit_blockers_take_precedence_over_clarification(self):
        task = deepcopy(self.dataset["tasks"][0])
        task["prompt"] = "用某个主控连接 I2C 传感器，把 SDA 与 SCL 短接"
        task["category"] = "negative"
        task["expected_disposition"] = "reject"
        task["negative_rule_ids"] = ["SDA_SCL_SHORT"]
        row = requirements_v2.evaluate_task(task)
        self.assertEqual(row["observed_preflight_disposition"], "reject")
        self.assertTrue(row["clarify_questions"])
        self.assertTrue(row["negative_rule_detection"])
        self.assertIsNone(row["erc_passed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
