"""Behavioral tests for honest YOLO evaluation and split isolation (stdlib only)."""
import importlib.util
import json
from contextlib import contextmanager, nullcontext
import socket
import tempfile
import types
import unittest
from unittest.mock import patch
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "train_yolo_detector.py"
if SCRIPT.exists():
    spec = importlib.util.spec_from_file_location("yolo_training", SCRIPT)
    training = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(training)
else:
    training = None


def box(name="resistor", xyxy=(0, 0, 10, 10), score=0.9):
    return {"class_name": name, "xyxy": list(xyxy), "score": score}


class YoloTrainingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(training, "training tool has not been implemented")

    def test_relocated_paths_keep_project_identity_and_missing_files_fail(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / "tests") as folder:
            root = Path(folder)
            old = "C:/Users/30789/Desktop/智绘电路-企业命题演示包/智绘电路/"
            data = root / "data/image_samples/original"
            data.mkdir(parents=True)
            (data / "manifest.json").write_text("{}", encoding="utf8")
            out = root / "evidence/yolo_training/source"
            (out / "fit/weights").mkdir(parents=True)
            best = out / "fit/weights/best.pt"
            best.write_bytes(b"registered")
            summary = {"status": "complete", "dataset": old + "data/image_samples/original",
                "manifest_sha256": training.sha256(data / "manifest.json"), "evaluation_config": training.EVAL_CONFIG,
                "best_checkpoint": {"path": old + "evidence/yolo_training/source/fit/weights/best.pt",
                    "sha256": training.sha256(best), "origin": "local_training",
                    "manifest_sha256": training.sha256(data / "manifest.json")}}
            summary_path = out / "run_summary.json"
            summary_path.write_text(json.dumps(summary), encoding="utf8")
            with patch.object(training, "ROOT", root):
                loaded, actual = training.warm_start_from_summary(summary_path)
                self.assertEqual(actual, best)
                self.assertEqual(loaded, summary)
                best.write_bytes(b"changed")
                with self.assertRaisesRegex(RuntimeError, "SHA256"):
                    training.warm_start_from_summary(summary_path)
                best.unlink()
                with self.assertRaises(FileNotFoundError):
                    training.warm_start_from_summary(summary_path)

    def test_runtime_yaml_uses_current_root_without_rewriting_frozen_yaml(self):
        import yaml
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / "tests") as folder:
            root = Path(folder)
            dataset = root / "data/image_samples/test"
            dataset.mkdir(parents=True)
            source = dataset / "dataset.yaml"
            source.write_text(yaml.safe_dump({"path": "C:/Users/30789/Desktop/智绘电路-企业命题演示包/智绘电路/data/image_samples/test",
                "train": "images/train", "val": "images/val", "test": "images/test", "names": {0: "resistor", 1: "capacitor.unpolarized"}}), encoding="utf8")
            original = source.read_bytes()
            out = root / "new_run"
            out.mkdir()
            with patch.object(training, "ROOT", root):
                runtime = training.runtime_dataset_yaml(dataset, out)
            self.assertEqual(Path(yaml.safe_load(runtime.read_text(encoding="utf8"))["path"]), dataset)
            self.assertEqual(source.read_bytes(), original)

    def test_explicit_drafter_split_accepts_more_real_training_images(self):
        manifest = {"split_drafters": {"train": [1], "val": [17], "test": [31]},
                    "expected_counts": {"train": 2, "val": 1, "test": 1}, "samples": []}
        for index, (split, drafter) in enumerate([("train", 1), ("train", 1), ("val", 17), ("test", 31)]):
            manifest["samples"].append({"id": str(index), "image": f"{index}.jpg", "split": split,
                "drafter": drafter, "source_split": "holdout" if split == "test" else "development"})
        try:
            counts = training.validate_manifest_splits(manifest)
        except ValueError as error:
            self.fail(f"Explicit new drafter split must be accepted: {error}")
        self.assertEqual(counts, {"train": 2, "val": 1, "test": 1})
        manifest["samples"][-1]["drafter"] = 17
        with self.assertRaisesRegex(ValueError, "drafter"):
            training.validate_manifest_splits(manifest)

    def test_loading_val_never_returns_test_samples_and_default_remains_test(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / "tests") as folder:
            root = Path(folder)
            samples = []
            for split, drafter in [("train", 1), ("val", 17), ("test", 31)]:
                for category in ("images", "labels"):
                    (root / category / split).mkdir(parents=True)
                image = root / "images" / split / f"{drafter}.jpg"
                label = root / "labels" / split / f"{drafter}.txt"
                image.write_bytes(split.encode())
                label.write_text("1 0.5 0.5 0.2 0.2\n", encoding="utf8")
                samples.append({"id": str(drafter), "drafter": drafter, "split": split,
                    "source_split": "holdout" if split == "test" else "development",
                    "image": image.relative_to(root).as_posix(), "label": label.relative_to(root).as_posix(),
                    "image_sha256": training.sha256(image), "label_sha256": training.sha256(label),
                    "image_geometry": {"width": 100, "height": 100}})
            manifest = {"samples": samples, "split_drafters": {"train": [1], "val": [17], "test": [31]},
                        "expected_counts": {"train": 1, "val": 1, "test": 1}}
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
            import yaml
            (root / "dataset.yaml").write_text(yaml.safe_dump({"path": str(root), "train": "images/train",
                "val": "images/val", "test": "images/test", "names": {0: "resistor", 1: "capacitor.unpolarized"}}), encoding="utf8")
            import inspect
            self.assertIn("split", inspect.signature(training.load_dataset).parameters,
                          "Dataset loading must explicitly select val without test inference")
            _, val = training.load_dataset(root, split="val")
            self.assertEqual([s["id"] for s in val], ["17"])
            self.assertEqual(val[0]["ground_truth"][0]["xyxy"], [40.0, 40.0, 60.0, 60.0])
            _, test = training.load_dataset(root)
            self.assertEqual([s["id"] for s in test], ["31"])

    def test_warm_start_checks_its_original_dataset_not_new_target(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / "tests") as folder:
            root = Path(folder)
            source = root / "old_data"
            source.mkdir()
            (source / "manifest.json").write_text('{"old": true}', encoding="utf8")
            best = root / "best.pt"
            best.write_bytes(b"registered two class head")
            summary = {"status": "complete", "dataset": str(source), "manifest_sha256": training.sha256(source / "manifest.json"),
                       "evaluation_config": training.EVAL_CONFIG,
                       "best_checkpoint": {"path": str(best), "sha256": training.sha256(best), "origin": "local_training",
                                           "manifest_sha256": training.sha256(source / "manifest.json")}}
            summary_path = root / "run_summary.json"
            summary_path.write_text(json.dumps(summary), encoding="utf8")
            self.assertTrue(hasattr(training, "warm_start_from_summary"), "Warm-start provenance gate is missing")
            loaded, checkpoint = training.warm_start_from_summary(summary_path)
            self.assertEqual(checkpoint, best)
            self.assertEqual(loaded["dataset"], str(source))
            best.write_bytes(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "SHA256"):
                training.warm_start_from_summary(summary_path)

    def test_checkpoint_selection_uses_val_map_and_last_tied_epoch(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / "tests") as folder:
            root = Path(folder)
            (root / "fit" / "weights").mkdir(parents=True)
            (root / "fit" / "results.csv").write_text(
                "epoch,metrics/mAP50-95(B),metrics/precision(B)\n1,0.4,0.99\n2,0.6,0.8\n3,0.6,0.7\n4,0.5,1.0\n", encoding="utf8")
            summary = {"source_checkpoint": {"sha256": "source"}, "manifest_sha256": "data"}
            training.collect_training_artifacts(root, summary)
            self.assertIn("checkpoint_selection", summary)
            self.assertEqual(summary["checkpoint_selection"]["best_epoch"], 3)
            self.assertEqual(summary["checkpoint_selection"]["best_score"], 0.6)

    def test_val_only_preflight_uses_local_source_and_never_requests_test(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / "tests") as folder:
            root = Path(folder)
            dataset = root / "new_data"
            dataset.mkdir()
            (dataset / "manifest.json").write_text('{"new":true}', encoding="utf8")
            (dataset / "dataset.yaml").write_text("names: {}", encoding="utf8")
            best = root / "best.pt"
            best.write_bytes(b"source")
            source = {"dataset": str(root / "old_data"), "manifest_sha256": "old-data-sha",
                      "best_checkpoint": {"path": str(best), "origin": "local_training", "sha256": training.sha256(best)}}
            source_path = root / "source_summary.json"
            source_path.write_text(json.dumps(source), encoding="utf8")
            manifest = {"split_drafters": {"train": [1], "val": [17], "test": [31]},
                        "expected_counts": {"train": 1, "val": 1, "test": 1}, "samples": []}
            for split, drafter in [("train", 1), ("val", 17), ("test", 31)]:
                manifest["samples"].append({"id": str(drafter), "image": f"{drafter}.jpg", "split": split,
                    "drafter": drafter, "source_split": "holdout" if split == "test" else "development"})
            def load_data(path, split="test"):
                if split != "val":
                    raise RuntimeError("test evaluation requested during val-only run")
                return manifest, [{"id": "val", "image": "unused", "ground_truth": [box()]}]
            args = types.SimpleNamespace(output=root / "out", dataset=dataset, epochs=50, preflight=True,
                                         evaluate_only=False, validation_only=True, warm_start_summary=source_path)
            model = types.SimpleNamespace(names={0: "resistor", 1: "capacitor.unpolarized"})
            with patch.object(training, "load_dataset", side_effect=load_data), \
                 patch.object(training, "warm_start_from_summary", return_value=(source, best)), \
                 patch.object(training, "load_registered_model", return_value=model):
                result = training.run(args)
            self.assertEqual(result["status"], "ready", result["errors"])
            self.assertEqual(result["evaluation_split"], "val")
            config = json.loads((args.output / "config.json").read_text(encoding="utf8"))
            self.assertEqual(config["nbs"], 2)
            self.assertEqual(config["warmup_epochs"], 0)
            self.assertFalse(config["resume"])

    def test_training_callbacks_reject_reset_head_and_count_real_update_events(self):
        self.assertTrue(hasattr(training, "configure_training_callbacks"), "Training setup and optimizer audit missing")
        import torch
        callbacks = {}
        tensor = torch.tensor([1.0, 2.0])
        key = "model.23.one2one_cv3.0.2.weight"
        wrapped = types.SimpleNamespace(state_dict=lambda: {key: tensor})
        model = types.SimpleNamespace(model=wrapped, add_callback=lambda name, callback: callbacks.update({name: callback}))
        summary = {}
        training.configure_training_callbacks(model, summary)
        optimizer = torch.optim.SGD([tensor], lr=0.1)
        trainer = types.SimpleNamespace(model=wrapped, optimizer=optimizer)
        callbacks["on_train_start"](trainer)
        self.assertTrue(summary["head_transfer"]["unchanged"])
        optimizer.step()
        optimizer.step()
        self.assertEqual(summary["optimizer_steps"], 2)
        tensor[0] = -1.0
        with self.assertRaisesRegex(RuntimeError, "classification head"):
            callbacks["on_train_start"](trainer)

    def test_duplicate_predictions_cannot_match_one_ground_truth_twice(self):
        result = training.match_boxes([box()], [box(), box(score=0.8)])
        self.assertEqual((result["tp"], result["fp"], result["fn"]), (1, 1, 0))

    def test_offline_network_uses_socket_errors_and_restores_socket(self):
        original_lookup = socket.getaddrinfo
        # The framework catches OSError to classify the runtime as offline.
        with training.offline_network():
            try:
                socket.getaddrinfo("localhost", 443)
                online = True
            except OSError:
                online = False
            except Exception as error:
                self.fail(f"Offline failure must follow socket OSError semantics: {error}")
            self.assertFalse(online)
            with socket.socket() as connection, self.assertRaisesRegex(OSError, "Network access disabled"):
                connection.connect(("127.0.0.1", 9))
        self.assertIs(socket.getaddrinfo, original_lookup)

    def test_matching_requires_class_and_inclusive_half_iou(self):
        # Intersection 50, union 100: exact IoU .5 must match.
        result = training.match_boxes([box()], [box(xyxy=(0, 0, 5, 10))])
        self.assertEqual(result["tp"], 1)
        wrong = training.match_boxes([box()], [box("capacitor.unpolarized")])
        self.assertEqual((wrong["tp"], wrong["fp"], wrong["fn"]), (0, 1, 1))
        below = training.match_boxes([box()], [box(xyxy=(0, 0, 4.99, 10))])
        self.assertEqual(below["tp"], 0)

    def test_empty_predictions_have_undefined_precision(self):
        result = training.evaluate_samples(
            [{"id": "empty", "image": "unused", "ground_truth": [box()]}],
            lambda sample: [],
        )
        self.assertIsNone(result["summary"]["precision"])
        self.assertEqual(result["summary"]["recall"], 0.0)
        self.assertEqual(result["summary"]["precision_denominator"], 0)

    def test_failed_image_remains_in_denominator_and_marks_incomplete(self):
        def predict(sample):
            if sample["id"] == "broken":
                raise RuntimeError("decode failed")
            return [box()]

        samples = [
            {"id": "ok", "image": "unused", "ground_truth": [box()]},
            {"id": "broken", "image": "unused", "ground_truth": [box(), box()]},
        ]
        result = training.evaluate_samples(samples, predict)
        summary = result["summary"]
        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual(summary["images_expected"], 2)
        self.assertEqual(summary["images_failed"], 1)
        self.assertEqual(summary["recall_denominator"], 3)
        self.assertEqual(summary["fn"], 2)
        self.assertIsNone(summary["recall"])
        self.assertAlmostEqual(summary["recall_lower_bound"], 1 / 3)
        self.assertIn("decode failed", result["images"][1]["error"])

    def test_class_breakdown_keeps_missing_class_denominators(self):
        result = training.match_boxes([box(), box("capacitor.unpolarized")], [box()])
        self.assertEqual(result["per_class"]["capacitor.unpolarized"]["fn"], 1)

    def test_test_image_cannot_appear_in_training_lists(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / "tests") as folder:
            root = Path(folder)
            for split, name in [("train", "shared.jpg"), ("val", "validation.jpg"), ("test", "shared.jpg")]:
                (root / "images" / split).mkdir(parents=True)
                (root / "images" / split / name).write_bytes(name.encode())
            with self.assertRaisesRegex(ValueError, "overlap"):
                training.validate_split_lists(root, strict_counts=False)

    def test_completed_epochs_counts_actual_csv_rows(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / "tests") as folder:
            path = Path(folder) / "results.csv"
            path.write_text("epoch,train/box_loss\n1,0.9\n2,0.7\n", encoding="utf8")
            self.assertEqual(training.completed_epochs(path), 2)

    def test_interrupted_csv_row_does_not_count_as_completed_epoch(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / "tests") as folder:
            path = Path(folder) / "results.csv"
            path.write_text("epoch,train/box_loss\n1,0.9\n2,\n", encoding="utf8")
            self.assertEqual(training.completed_epochs(path), 1)

    def test_registered_local_checkpoint_rejects_changed_bytes(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / "tests") as folder:
            path = Path(folder) / "best.pt"
            path.write_bytes(b"own training output")
            registration = {"path": str(path), "sha256": training.sha256(path), "origin": "local_training"}
            path.write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "SHA256"):
                training.verify_local_checkpoint(path, registration)

    def test_partial_training_keeps_actual_epochs_and_best_registration(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / "tests") as folder:
            root = Path(folder)
            (root / "fit" / "weights").mkdir(parents=True)
            (root / "fit" / "weights" / "best.pt").write_bytes(b"partial training weights")
            (root / "fit" / "results.csv").write_text("epoch,loss\n1,0.9\n", encoding="utf8")
            summary = {"source_checkpoint": {"sha256": "source"}, "manifest_sha256": "data"}
            self.assertTrue(hasattr(training, "collect_training_artifacts"), "partial training artifacts need registration")
            training.collect_training_artifacts(root, summary)
            self.assertEqual(summary["epochs_completed"], 1)
            self.assertEqual(summary["best_checkpoint"]["origin"], "local_training")
            self.assertEqual(summary["best_checkpoint"]["manifest_sha256"], "data")

    def test_evaluation_only_rejects_dataset_changed_after_training(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / "tests") as folder:
            root = Path(folder)
            dataset = root / "data"
            dataset.mkdir()
            (dataset / "manifest.json").write_text("{}", encoding="utf8")
            best = root / "best.pt"
            best.write_bytes(b"own output")
            summary = {"manifest_sha256": "old_manifest", "best_checkpoint": {
                "path": str(best), "sha256": training.sha256(best), "origin": "local_training"}}
            (root / "run_summary.json").write_text(json.dumps(summary), encoding="utf8")
            self.assertTrue(hasattr(training, "registered_checkpoint_from_summary"), "evaluation only needs provenance gate")
            with self.assertRaisesRegex(RuntimeError, "manifest"):
                training.registered_checkpoint_from_summary(root, dataset)

    def test_evaluation_only_rejects_changed_evaluation_config(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / "tests") as folder:
            root = Path(folder)
            dataset = root / "data"
            dataset.mkdir()
            (dataset / "manifest.json").write_text("{}", encoding="utf8")
            manifest_hash = training.sha256(dataset / "manifest.json")
            best = root / "best.pt"
            best.write_bytes(b"own output")
            summary = {"manifest_sha256": manifest_hash, "evaluation_config": dict(training.EVAL_CONFIG, conf=0.9),
                       "best_checkpoint": {"path": str(best), "sha256": training.sha256(best),
                                           "origin": "local_training", "manifest_sha256": manifest_hash}}
            (root / "run_summary.json").write_text(json.dumps(summary), encoding="utf8")
            with self.assertRaisesRegex(RuntimeError, "evaluation config"):
                training.registered_checkpoint_from_summary(root, dataset)

    def test_source_checkpoint_must_support_both_target_classes(self):
        self.assertTrue(hasattr(training, "validate_source_classes"), "baseline classes require validation")
        with self.assertRaisesRegex(RuntimeError, "target classes"):
            training.validate_source_classes({0: "resistor", 1: "diode"})

    def test_training_reloads_pristine_source_after_baseline_fuses_cached_model(self):
        fused = types.SimpleNamespace(fused=True)
        pristine = types.SimpleNamespace(fused=False, names={0: "resistor", 1: "capacitor.unpolarized"})
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / "tests") as folder:
            source = Path(folder) / "source.pt"
            source.write_bytes(b"verified public source")
            detector = types.SimpleNamespace(_model=fused, MODEL_PATH=source, _checkpoint_globals=nullcontext)
            core = types.ModuleType("core")
            core.image_detector = detector
            ultra = types.ModuleType("ultralytics")
            ultra.YOLO = lambda path, task: pristine
            self.assertTrue(hasattr(training, "fresh_public_model"), "training must reload unfused source")
            with patch.dict("sys.modules", {"core": core, "ultralytics": ultra}):
                model = training.fresh_public_model({"path": str(source), "sha256": training.sha256(source)})
            self.assertFalse(model.fused)
            self.assertIs(detector._model, fused)

    def test_own_checkpoint_loading_limits_namespace_allowlist_to_load_scope(self):
        # Only mock slow framework loading; registration/hash validation stays real.
        active = []
        namespace = type("OfficialNamespace", (), {})
        @contextmanager
        def safe_globals(classes):
            self.assertEqual(classes, [namespace])
            active.extend(classes)
            try:
                yield
            finally:
                active.clear()
        torch = types.ModuleType("torch")
        torch.serialization = types.SimpleNamespace(safe_globals=lambda classes: self.fail("Reuse the runtime approved context"))
        utils = types.ModuleType("ultralytics.utils")
        utils.IterableSimpleNamespace = namespace
        tasks = types.ModuleType("ultralytics.nn.tasks")
        tasks.SAFE_LOAD = True
        nn = types.ModuleType("ultralytics.nn")
        nn.tasks = tasks
        ultra = types.ModuleType("ultralytics")
        ultra.nn = nn
        ultra.utils = utils
        def load(path, task):
            self.assertEqual(active, [namespace], "Official namespace must be allowed locally during checkpoint loading")
            return types.SimpleNamespace(names={0: "resistor", 1: "capacitor.unpolarized"})
        ultra.YOLO = load
        core = types.ModuleType("core")
        core.image_detector = types.SimpleNamespace(RUNTIME_ROOT=SCRIPT.parent.parent,
                                                    _checkpoint_globals=lambda: safe_globals([namespace]))
        modules = {"torch": torch, "ultralytics": ultra, "ultralytics.nn": nn,
                   "ultralytics.nn.tasks": tasks, "ultralytics.utils": utils, "core": core}
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / "tests") as folder:
            best = Path(folder) / "best.pt"
            best.write_bytes(b"own output")
            registration = {"path": str(best), "sha256": training.sha256(best), "origin": "local_training"}
            with patch.dict("sys.modules", modules), patch.dict("os.environ"):
                model = training.load_registered_model(best, registration)
            self.assertEqual(model.names, {0: "resistor", 1: "capacitor.unpolarized"})
            self.assertEqual(active, [])

    def test_split_manifest_checks_source_development_and_drafter_overlap(self):
        samples = [
            {"id": "a", "split": "train", "source_split": "development", "drafter": 1, "image": "a.jpg"},
            {"id": "b", "split": "val", "source_split": "development", "drafter": 2, "image": "b.jpg"},
            {"id": "c", "split": "test", "source_split": "holdout", "drafter": 1, "image": "c.jpg"},
        ]
        with self.assertRaisesRegex(ValueError, "drafter"):
            training.validate_manifest_splits({"samples": samples}, strict_counts=False)
        samples[2]["drafter"] = 3
        samples[0]["source_split"] = "holdout"
        with self.assertRaisesRegex(ValueError, "development"):
            training.validate_manifest_splits({"samples": samples}, strict_counts=False)

    def test_manifest_train_assignment_stays_inside_fixed_drafter_range(self):
        samples = []
        for drafter in range(1, 31):
            split = "train" if drafter <= 16 else "val" if drafter <= 20 else "test"
            samples.append({"id": str(drafter), "drafter": drafter, "split": split,
                            "source_split": "development" if drafter <= 20 else "holdout",
                            "image": f"{drafter}.jpg"})
        samples[0]["split"], samples[16]["split"] = "val", "train"
        with self.assertRaisesRegex(ValueError, "fixed"):
            training.validate_manifest_splits({"samples": samples})


if __name__ == "__main__":
    unittest.main()
