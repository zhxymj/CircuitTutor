"""Checks the export boundary: source integrity, geometry and drafter isolation."""
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/image_samples/cghd_v15"
SCRIPT = ROOT / "tools/prepare_yolo_dataset.py"
if SCRIPT.exists():
    spec = importlib.util.spec_from_file_location("prepare_yolo_dataset", SCRIPT)
    dataset = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dataset)
else:
    dataset = None


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def xml(width=40, height=20, name="resistor", box=(4, 2, 12, 10)):
    coordinates = "".join(f"<{k}>{v}</{k}>" for k, v in zip(
        ("xmin", "ymin", "xmax", "ymax"), box))
    return (f"<annotation><size><width>{width}</width><height>{height}</height>"
            f"</size><object><name>{name}</name><bndbox>{coordinates}</bndbox>"
            "</object></annotation>")


class YoloDatasetTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(dataset, "YOLO dataset exporter is not implemented")

    def test_sorted_drafters_have_disjoint_16_4_10_splits(self):
        source = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
        assigned = dataset.assign_splits(list(reversed(source["samples"])))
        self.assertEqual([s["drafter"] for s in assigned if s["split"] == "train"], list(range(1, 17)))
        self.assertEqual([s["drafter"] for s in assigned if s["split"] == "val"], [17, 18, 19, 20])
        self.assertEqual([s["drafter"] for s in assigned if s["split"] == "test"], list(range(21, 31)))
        self.assertTrue(all(s["source_split"] == "holdout" for s in assigned if s["split"] == "test"))

    def test_same_drafter_in_development_and_holdout_is_rejected(self):
        source = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
        source["samples"][-1]["drafter"] = 1
        with self.assertRaisesRegex(ValueError, "drafter"):
            dataset.assign_splits(source["samples"])

    def test_wrong_source_hash_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tmp:
            path = Path(tmp) / "file.jpg"
            path.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "SHA256"):
                dataset.verify_source_file(path, "0" * 64)

    def test_voc_coordinates_use_extents_without_invented_one_pixel_shift(self):
        labels, counts, ignored = dataset.convert_annotation(xml())
        self.assertEqual(labels, ["0 0.2000000000 0.3000000000 0.2000000000 0.4000000000"])
        self.assertEqual(counts, {"resistor": 1, "capacitor.unpolarized": 0})
        self.assertEqual(ignored, {})

    def test_invalid_boxes_are_rejected_including_ignored_classes(self):
        for name in ("resistor", "text"):
            for box in ((5, 2, 5, 10), (-1, 2, 12, 10), (4, 2, 41, 10), (4, 11, 12, 10), (4, 2, "nan", 10)):
                with self.subTest(name=name, box=box), self.assertRaisesRegex(ValueError, "bbox"):
                    dataset.convert_annotation(xml(name=name, box=box))

    def test_other_classes_produce_zero_labels_and_explicit_ignored_counts(self):
        labels, counts, ignored = dataset.convert_annotation(xml(name="capacitor.polarized"))
        self.assertEqual(labels, [])
        self.assertEqual(counts, {"resistor": 0, "capacitor.unpolarized": 0})
        self.assertEqual(ignored, {"capacitor.polarized": 1})

    def test_exif_oriented_size_is_checked_without_rotating_annotation_boxes(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tmp:
            path = Path(tmp) / "orientation8.jpg"
            image = Image.new("RGB", (20, 40), "white")
            exif = Image.Exif()
            exif[274] = 8
            image.save(path, exif=exif)
            geometry = dataset.image_geometry(path, (40, 20))
            self.assertEqual((geometry["raw_width"], geometry["raw_height"]), (20, 40))
            self.assertEqual((geometry["width"], geometry["height"]), (40, 20))
            self.assertEqual(geometry["effective_orientation"], 8)
            with self.assertRaisesRegex(ValueError, "dimensions"):
                dataset.image_geometry(path, (20, 40))

    def test_undefined_exif_zero_is_recorded_as_identity(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tmp:
            path = Path(tmp) / "orientation0.jpg"
            image = Image.new("RGB", (40, 20), "white")
            exif = Image.Exif()
            exif[274] = 0
            image.save(path, exif=exif)
            geometry = dataset.image_geometry(path, (40, 20))
            self.assertEqual(geometry["exif_orientation"], 0)
            self.assertEqual(geometry["effective_orientation"], 1)

    def test_real_export_preserves_all_source_hashes_labels_and_split_denominators(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tmp:
            output = Path(tmp) / "export"
            manifest = dataset.export_dataset(SOURCE, output)
            self.assertEqual({k: v["images"] for k, v in manifest["statistics"]["splits"].items()},
                             {"train": 16, "val": 4, "test": 10})
            self.assertEqual(manifest["statistics"]["total"]["boxes_by_class"],
                             {"resistor": 170, "capacitor.unpolarized": 75})
            self.assertEqual(manifest["license_status"], "conflicting_declarations_pending_review")
            self.assertEqual(len(manifest["samples"]), 30)
            sets = {split: {s["drafter"] for s in manifest["samples"] if s["split"] == split}
                    for split in ("train", "val", "test")}
            self.assertTrue(sets["train"].isdisjoint(sets["val"]))
            self.assertTrue(sets["train"].isdisjoint(sets["test"]))
            self.assertTrue(sets["val"].isdisjoint(sets["test"]))
            for sample in manifest["samples"]:
                self.assertEqual(sha256(SOURCE / sample["source_image"]), sample["source_image_sha256"])
                self.assertEqual(sha256(SOURCE / sample["source_annotation"]), sample["source_annotation_sha256"])
                self.assertEqual(sha256(output / sample["image"]), sample["image_sha256"])
                self.assertEqual(sha256(output / sample["label"]), sample["label_sha256"])
                with Image.open(output / sample["image"]) as image:
                    self.assertEqual(image.size, (sample["image_geometry"]["width"], sample["image_geometry"]["height"]))
                    self.assertIn(image.getexif().get(274, 1), (0, 1))
                rows = (output / sample["label"]).read_text().splitlines()
                self.assertEqual(len(rows), sum(sample["class_counts"].values()))
                for row in rows:
                    values = row.split()
                    self.assertIn(values[0], ("0", "1"))
                    self.assertTrue(all(0 <= float(v) <= 1 for v in values[1:]))
            sample3 = next(s for s in manifest["samples"] if s["drafter"] == 3)
            self.assertEqual(sample3["image_export_mode"], "exif_transposed_png")
            self.assertTrue((output / "dataset.yaml").is_file())
            self.assertTrue((output / "README.md").is_file())


if __name__ == "__main__":
    unittest.main()
