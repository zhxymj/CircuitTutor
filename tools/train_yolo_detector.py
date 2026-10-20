"""Offline public-checkpoint fine tuning with fixed, auditable CGHD evaluation.

Uses the existing restricted Ultralytics loader. No packages or weights are
downloaded. CGHD holdout is disjoint from this fine-tuning split; overlap with
the public checkpoint's original training data remains unknown.
"""
import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import csv
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import platform
import re
import socket
import sys
import time
import traceback


try:
    from .prepare_yolo_dataset import resolve_recorded_path
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from prepare_yolo_dataset import resolve_recorded_path

ROOT = Path(__file__).resolve().parents[1]
CLASSES = ("resistor", "capacitor.unpolarized")
EXPECTED_COUNTS = {"train": 16, "val": 4, "test": 10}
EVAL_CONFIG = {"conf": 0.25, "imgsz": 640, "iou": 0.5, "seed": 42, "max_det": 1000,
               "augment": False, "verbose": False, "save": False, "save_txt": False}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf8")


def box_iou(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0, x1 - x0) * max(0, y1 - y0)
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union else 0.0


def match_boxes(ground_truth, predictions, threshold=0.5):
    """Confidence-ordered, class-aware one-to-one matching at IoU >= threshold."""
    matched = set()
    pairs = []
    per_class = {name: {"tp": 0, "fp": 0, "fn": 0} for name in CLASSES}
    for pi in sorted(range(len(predictions)), key=lambda i: (-predictions[i].get("score", 0), i)):
        prediction = predictions[pi]
        name = prediction["class_name"]
        if name not in per_class:
            raise ValueError("Evaluation predictions must contain only the two target classes")
        candidates = [(box_iou(prediction["xyxy"], target["xyxy"]), gi)
                      for gi, target in enumerate(ground_truth)
                      if gi not in matched and target["class_name"] == name]
        overlap, gi = max(candidates, default=(0.0, -1))
        if gi >= 0 and overlap >= threshold:
            matched.add(gi)
            pairs.append({"prediction_index": pi, "ground_truth_index": gi, "iou": overlap})
            per_class[name]["tp"] += 1
        else:
            per_class[name]["fp"] += 1
    for gi, target in enumerate(ground_truth):
        if gi not in matched:
            per_class[target["class_name"]]["fn"] += 1
    return {key: sum(row[key] for row in per_class.values()) for key in ("tp", "fp", "fn")} | {
        "matches": pairs, "per_class": per_class}


def evaluate_samples(samples, predictor):
    rows = []
    totals = {key: 0 for key in ("tp", "fp", "fn")}
    per_class = {name: {key: 0 for key in totals} for name in CLASSES}
    for sample in samples:
        start = time.perf_counter()
        row = {"id": sample["id"], "image": str(sample["image"]),
               "ground_truth": sample["ground_truth"], "status": "complete", "error": None}
        try:
            row["predictions"] = predictor(sample)
            row.update(match_boxes(sample["ground_truth"], row["predictions"]))
        except Exception as error:
            row.update(match_boxes(sample["ground_truth"], []))
            row.update(status="incomplete", predictions=None, error=f"{type(error).__name__}: {error}")
        row["elapsed_seconds"] = time.perf_counter() - start
        for key in totals:
            totals[key] += row[key]
        for name in CLASSES:
            for key in totals:
                per_class[name][key] += row["per_class"][name][key]
        rows.append(row)
    failures = sum(row["status"] != "complete" for row in rows)
    pd, rd = totals["tp"] + totals["fp"], totals["tp"] + totals["fn"]
    complete = not failures
    summary = totals | {"status": "complete" if complete else "incomplete", "images_expected": len(samples),
        "images_completed": len(samples) - failures, "images_failed": failures,
        "precision_numerator": totals["tp"], "precision_denominator": pd,
        "recall_numerator": totals["tp"], "recall_denominator": rd,
        "precision": totals["tp"] / pd if complete and pd else None,
        "recall": totals["tp"] / rd if complete and rd else None,
        "recall_lower_bound": totals["tp"] / rd if rd else None,
        "failed_image_fn_policy": "all annotated targets counted as unevaluated misses; primary metrics null when incomplete",
        "per_class": per_class, "matching_iou": 0.5,
        "matching": "confidence ordered class-aware greedy one-to-one"}
    return {"summary": summary, "images": rows}


def validate_manifest_splits(manifest, strict_counts=True):
    samples = manifest["samples"]
    ids, image_paths, drafters = set(), set(), {}
    counts = dict.fromkeys(EXPECTED_COUNTS, 0)
    for sample in samples:
        split = sample["split"]
        if split not in counts:
            raise ValueError(f"Unknown split {split}")
        counts[split] += 1
        if split in {"train", "val"} and sample["source_split"] != "development":
            raise ValueError("Only source development samples may enter train or val")
        if split == "test" and sample["source_split"] != "holdout":
            raise ValueError("Test samples must come from source holdout")
        if sample["id"] in ids or sample["image"] in image_paths:
            raise ValueError("Sample/image overlap across splits")
        ids.add(sample["id"])
        image_paths.add(sample["image"])
        drafter = sample["drafter"]
        if drafter in drafters and drafters[drafter] != split:
            raise ValueError("drafter overlap across splits")
        drafters[drafter] = split
    declared = manifest.get("split_drafters")
    expected = manifest.get("expected_counts", EXPECTED_COUNTS)
    if declared is not None:
        if set(declared) != set(EXPECTED_COUNTS) or set(expected) != set(EXPECTED_COUNTS):
            raise ValueError("Explicit drafter splits/counts require train/val/test")
        assigned = [drafter for split in declared.values() for drafter in split]
        if len(assigned) != len(set(assigned)):
            raise ValueError("Declared drafter overlap across splits")
        for sample in samples:
            if sample["drafter"] not in declared[sample["split"]]:
                raise ValueError("Sample differs from declared drafter split")
    if strict_counts and counts != expected:
        raise ValueError(f"Expected split counts {expected}; got {counts}")
    if strict_counts:
        for sample in samples:
            drafter = sample["drafter"]
            fixed = sample["split"] if declared is not None else "train" if 1 <= drafter <= 16 else "val" if 17 <= drafter <= 20 else "test" if 21 <= drafter <= 30 else None
            if sample["split"] != fixed:
                raise ValueError("Manifest differs from fixed drafter split 1..16/17..20/21..30")
    return counts


def validate_split_lists(root, strict_counts=True, expected_counts=None):
    """Inspect exact image directories passed to Ultralytics, including extras."""
    seen_names, seen_hashes = {}, {}
    counts = {}
    for split in EXPECTED_COUNTS:
        paths = sorted(p for p in (Path(root) / "images" / split).rglob("*")
                       if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"})
        counts[split] = len(paths)
        for path in paths:
            digest = sha256(path)
            if path.stem in seen_names or digest in seen_hashes:
                raise ValueError("Image/name/hash overlap in actual split directories")
            seen_names[path.stem], seen_hashes[digest] = split, split
    if strict_counts and counts != (expected_counts or EXPECTED_COUNTS):
        raise ValueError(f"Unexpected actual split image counts: {counts}")
    return counts


def completed_epochs(path):
    if not Path(path).is_file():
        return 0
    with Path(path).open(encoding="utf8", newline="") as stream:
        return sum(1 for row in csv.DictReader(stream)
                   if row and all(isinstance(value, str) and value.strip() for value in row.values()))


def verify_local_checkpoint(path, registration):
    if registration.get("origin") != "local_training" or resolve_recorded_path(registration["path"], ROOT).resolve() != Path(path).resolve():
        raise RuntimeError("Local training checkpoint registration required")
    if sha256(path) != registration["sha256"]:
        raise RuntimeError("Local checkpoint SHA256 mismatch")


def collect_training_artifacts(output, summary):
    summary["epochs_completed"] = completed_epochs(output / "fit" / "results.csv")
    best = output / "fit" / "weights" / "best.pt"
    if best.is_file():
        summary["best_checkpoint"] = {"path": str(best), "sha256": sha256(best), "origin": "local_training",
            "source_checkpoint_sha256": summary["source_checkpoint"]["sha256"],
            "manifest_sha256": summary["manifest_sha256"]}
    for filename in ("args.yaml", "results.csv"):
        path = output / "fit" / filename
        if path.is_file():
            summary.setdefault("training_metadata", {})[filename] = {"path": str(path), "sha256": sha256(path)}
    results = output / "fit" / "results.csv"
    if results.is_file():
        with results.open(encoding="utf8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        candidates = [(float(row["metrics/mAP50-95(B)"]), int(row["epoch"])) for row in rows
                      if row.get("metrics/mAP50-95(B)") and row.get("epoch")]
        if candidates:
            score, epoch = max(candidates)
            summary["checkpoint_selection"] = {"split": "val", "metric": "metrics/mAP50-95(B)",
                "tie_policy": "last epoch at maximum score", "best_epoch": epoch, "best_score": score,
                "basis": "local Ultralytics fitness weights [0,0,0,1]; best saved on equality; actual results.csv"}


def registered_checkpoint_from_summary(output, dataset):
    summary = json.loads((output / "run_summary.json").read_text(encoding="utf8"))
    if sha256(dataset / "manifest.json") != summary.get("manifest_sha256"):
        raise RuntimeError("Dataset manifest SHA256 differs from the registered training run")
    if summary.get("evaluation_config") != EVAL_CONFIG:
        raise RuntimeError("Registered evaluation config differs from the current fixed evaluation config")
    registration = summary["best_checkpoint"]
    best = resolve_recorded_path(registration["path"], ROOT)
    verify_local_checkpoint(best, registration)
    if registration.get("manifest_sha256") != summary["manifest_sha256"]:
        raise RuntimeError("Checkpoint manifest registration mismatch")
    return summary, best


def warm_start_from_summary(path):
    """Check the source run against its own dataset, independently of the target."""
    path = Path(path).resolve(strict=True)
    source = json.loads(path.read_text(encoding="utf8"))
    if source.get("status") != "complete":
        raise RuntimeError("Warm-start requires a completed registered training run")
    return registered_checkpoint_from_summary(path.parent, resolve_recorded_path(source["dataset"], ROOT))


def validate_source_classes(names):
    if not isinstance(names, dict) or not set(CLASSES).issubset(set(names.values())):
        raise RuntimeError("Public checkpoint must support both exact target classes for baseline comparison")


def fresh_public_model(source_checkpoint):
    """Reload verified source bytes; inference can fuse the baseline model in place."""
    from core import image_detector
    path = image_detector.MODEL_PATH
    if path.resolve() != Path(source_checkpoint["path"]).resolve() or sha256(path) != source_checkpoint["sha256"]:
        raise RuntimeError("Public training source SHA256/path changed after baseline evaluation")
    from ultralytics import YOLO
    with restricted_metadata_context():
        model = YOLO(str(path), task="detect")
    validate_source_classes(model.names)
    return model


def restricted_metadata_context():
    """Reuse the runtime's audited fixed metadata types, locally, under SAFE_LOAD."""
    from core import image_detector
    return image_detector._checkpoint_globals()


def load_registered_model(best, registration):
    verify_local_checkpoint(best, registration)
    sys.path.insert(0, str(ROOT))
    from core import image_detector
    os.environ["ULTRALYTICS_SAFE_LOAD"] = "true"
    os.environ["YOLO_AUTOINSTALL"] = "false"
    os.environ["YOLO_CONFIG_DIR"] = str(image_detector.RUNTIME_ROOT / "vision-python-config")
    os.environ["TORCH_HOME"] = str(image_detector.RUNTIME_ROOT / "vision-python-cache" / "torch")
    import ultralytics.nn.tasks as tasks
    if not getattr(tasks, "SAFE_LOAD", False):
        raise RuntimeError("Restricted checkpoint loading must remain enabled after training")
    from ultralytics import YOLO
    with restricted_metadata_context():
        model = YOLO(str(best), task="detect")
    if model.names != {0: CLASSES[0], 1: CLASSES[1]}:
        raise RuntimeError("Registered training checkpoint has unexpected classes")
    return model


@contextmanager
def offline_network():
    """Fail closed on any unexpected online package, font, or weight fetch."""
    previous_connect = socket.socket.connect
    previous_connect_ex = socket.socket.connect_ex
    previous_lookup = socket.getaddrinfo
    def denied(*args, **kwargs):
        raise OSError("Network access disabled for this local training run")
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.getaddrinfo = denied
    try:
        yield
    finally:
        socket.socket.connect = previous_connect
        socket.socket.connect_ex = previous_connect_ex
        socket.getaddrinfo = previous_lookup


def load_dataset(root, split="test"):
    if split not in EXPECTED_COUNTS:
        raise ValueError("Evaluation split must be train, val, or test")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf8"))
    validate_manifest_splits(manifest)
    validate_split_lists(root, expected_counts=manifest.get("expected_counts"))
    import yaml
    config = yaml.safe_load((root / "dataset.yaml").read_text(encoding="utf8"))
    if resolve_recorded_path(config["path"], ROOT).resolve() != root.resolve() or any(config[key] != f"images/{key}" for key in EXPECTED_COUNTS):
        raise ValueError("Dataset YAML must use the audited train/val/test directories")
    if config["names"] != {0: CLASSES[0], 1: CLASSES[1]}:
        raise ValueError("Dataset must contain exactly the two agreed target classes")
    tests = []
    for sample in manifest["samples"]:
        image, label = root / sample["image"], root / sample["label"]
        if sha256(image) != sample["image_sha256"] or sha256(label) != sample["label_sha256"]:
            raise ValueError(f"Dataset hash mismatch: {sample['id']}")
        if image.parent != root / "images" / sample["split"] or label.parent != root / "labels" / sample["split"]:
            raise ValueError(f"Dataset split path mismatch: {sample['id']}")
        if sample["split"] != split:
            continue
        width, height = sample["image_geometry"]["width"], sample["image_geometry"]["height"]
        gt = []
        for line in label.read_text(encoding="utf8").splitlines():
            values = line.split()
            if len(values) != 5:
                raise ValueError("Invalid YOLO label row")
            class_id = int(values[0])
            if class_id not in (0, 1):
                raise ValueError("Unknown YOLO class")
            cx, cy, w, h = map(float, values[1:])
            gt.append({"class_name": CLASSES[class_id], "xyxy": [(cx-w/2)*width, (cy-h/2)*height,
                       (cx+w/2)*width, (cy+h/2)*height]})
        tests.append({"id": sample["id"], "image": str(image), "ground_truth": gt})
    return manifest, tests


def runtime_dataset_yaml(dataset, output):
    """Give Ultralytics the current location without changing frozen input bytes."""
    import yaml
    dataset = Path(dataset).resolve()
    source = dataset / "dataset.yaml"
    config = yaml.safe_load(source.read_text(encoding="utf8"))
    if resolve_recorded_path(config["path"], ROOT).resolve() != dataset:
        raise ValueError("Dataset YAML root differs from the selected dataset")
    if Path(config["path"]).resolve() == dataset:
        return source
    config["path"] = dataset.as_posix()
    runtime = Path(output) / "runtime_dataset.yaml"
    runtime.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf8")
    return runtime


def predictor_for(model, device):
    def predict(sample):
        prediction = model.predict(source=sample["image"], device=device, **EVAL_CONFIG)[0]
        output = []
        for x0, y0, x1, y1, score, class_id in prediction.boxes.data.cpu().tolist():
            name = model.names[int(class_id)]
            if name in CLASSES:
                output.append({"class_name": name, "class_id": int(class_id), "score": score,
                               "xyxy": [x0, y0, x1, y1]})
        return output
    return predict


def configure_training_callbacks(model, summary):
    """Audit source classification tensors before updates and count actual steps."""
    def head_digests(module):
        return {key: hashlib.sha256(value.detach().float().cpu().contiguous().numpy().tobytes()).hexdigest()
                for key, value in module.state_dict().items()
                if re.search(r"(?:^|\.)(?:one2one_)?cv3\.\d+\.2\.(?:weight|bias)$", key)}
    before = head_digests(model.model)
    if not before:
        raise RuntimeError("No audited classification head output tensors found")
    summary["head_transfer"] = {"keys": list(before), "digests_before": before, "unchanged": False}
    summary["optimizer_steps"] = 0
    summary["optimizer_steps_basis"] = "actual PyTorch optimizer.register_step_post_hook events; AMP disabled; local trainer emits no on_before_zero_grad"
    def verify_head(trainer):
        after = head_digests(trainer.model)
        summary["head_transfer"].update(digests_after_setup=after, unchanged=before == after)
        if before != after:
            raise RuntimeError("Warm-start classification head changed during training setup")
        trainer.optimizer.register_step_post_hook(count_step)
    def count_step(optimizer, positional, keyword):
        summary["optimizer_steps"] += 1
    model.add_callback("on_train_start", verify_head)


def evaluate_registered(args):
    """Reproduce post-training predictions without training or replacing source weights."""
    out, dataset = args.output.resolve(), args.dataset.resolve()
    summary = json.loads((out / "run_summary.json").read_text(encoding="utf8"))
    prior = {key: summary.get(key) for key in ("status", "stage", "errors", "elapsed_seconds")}
    summary["previous_attempts"] = summary.get("previous_attempts", []) + [prior]
    summary.update(status="incomplete", stage="evaluation_only", errors=[])
    start = time.perf_counter()
    with (out / "run.log").open("a", encoding="utf8") as log, redirect_stdout(log), redirect_stderr(log), offline_network():
        try:
            _, best = registered_checkpoint_from_summary(out, dataset)
            split = summary.get("evaluation_split", "test")
            _, samples = load_dataset(dataset, split=split)
            model = load_registered_model(best, summary["best_checkpoint"])
            device = summary["environment"]["device"]
            post = evaluate_samples(samples, predictor_for(model, device))
            write_json(out / f"post_{split}_results.json", post)
            summary["post_training"] = post["summary"]
            if post["summary"]["status"] != "complete":
                raise RuntimeError("Registered checkpoint evaluation incomplete")
            if summary["epochs_completed"] != summary["epochs_requested"]:
                raise RuntimeError("Checkpoint can be evaluated, but requested training epochs remain incomplete")
            summary.update(status="complete", stage="complete")
        except Exception as error:
            summary["errors"].append(f"{type(error).__name__}: {error}")
            traceback.print_exc()
        finally:
            summary["evaluation_only_elapsed_seconds"] = time.perf_counter() - start
            write_json(out / "run_summary.json", summary)
    return summary


def run(args):
    if args.evaluate_only:
        return evaluate_registered(args)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / "fit" / "weights" / "best.pt").exists():
        raise RuntimeError("Existing training checkpoint found; choose a fresh output directory")
    start = time.perf_counter()
    evaluation_split = "val" if getattr(args, "validation_only", False) else "test"
    warm_start_path = getattr(args, "warm_start_summary", None)
    summary = {"status": "incomplete", "stage": "preflight", "epochs_requested": args.epochs,
               "epochs_completed": 0, "training_attempted": False, "errors": [],
               "test_description": "CGHD source holdout; public pretraining overlap unknown; not an independent blind test",
               "dataset": str(args.dataset.resolve()), "environment": {"python": sys.version, "executable": sys.executable,
               "platform": platform.platform()}, "evaluation_config": EVAL_CONFIG,
               "network_access": "disabled", "package_install": "disabled"}
    summary["evaluation_split"] = evaluation_split
    if evaluation_split == "val":
        summary["test_inference"] = "not_run; test is excluded from training selection and before/after evaluation"
    prior_summary = out / "run_summary.json"
    if prior_summary.is_file():
        previous = json.loads(prior_summary.read_text(encoding="utf8"))
        summary["previous_attempts"] = previous.get("previous_attempts", []) + [{key: previous.get(key)
            for key in ("status", "stage", "epochs_completed", "training_attempted", "errors", "elapsed_seconds")}]
    rejected = out.parent / "failed_fused_model" / "run_summary.json"
    if rejected.is_file():
        summary["rejected_prior_run"] = str(rejected.parent)
    config = {"data": str(args.dataset.resolve() / "dataset.yaml"), "epochs": args.epochs,
              "imgsz": 640, "batch": 2, "workers": 0, "seed": 42, "amp": False,
              "deterministic": True, "plots": False, "mosaic": 0.0, "fliplr": 0.0,
              "flipud": 0.0, "scale": 0.1, "translate": 0.05, "degrees": 0.0,
              "shear": 0.0, "perspective": 0.0, "hsv_h": 0.0, "hsv_s": 0.0,
              "hsv_v": 0.0, "mixup": 0.0, "copy_paste": 0.0, "cache": False,
              "project": str(out), "name": "fit", "exist_ok": True,
              "save": True, "val": True, "patience": 100, "optimizer": "SGD"}
    if warm_start_path:
        config.update(lr0=0.001, lrf=0.1, nbs=2, warmup_epochs=0.0, resume=False)
        summary["training_mode"] = "registered two-class warm-start with a new optimizer"
    write_json(out / "config.json", config | {"evaluation": EVAL_CONFIG})
    write_json(out / "run_summary.json", summary)
    os.environ["YOLO_AUTOINSTALL"] = "false"
    os.environ["ULTRALYTICS_SAFE_LOAD"] = "true"
    os.environ["WANDB_MODE"] = "disabled"
    os.environ["COMET_MODE"] = "DISABLED"
    os.environ["MLFLOW_TRACKING_URI"] = ""
    handler = None
    with (out / "run.log").open("a", encoding="utf8") as log, redirect_stdout(log), redirect_stderr(log), offline_network():
        print(json.dumps({"event": "attempt_started", "previous_attempt_count": len(summary.get("previous_attempts", []))}))
        try:
            manifest_path = args.dataset / "manifest.json"
            summary["manifest_sha256"] = sha256(manifest_path)
            summary["dataset_yaml_sha256"] = sha256(args.dataset / "dataset.yaml")
            sys.path.insert(0, str(ROOT))
            from core import image_detector
            summary["source_checkpoint"] = {"path": str(image_detector.MODEL_PATH), "exists": image_detector.MODEL_PATH.is_file()}
            if image_detector.MODEL_PATH.is_file():
                summary["source_checkpoint"]["sha256"] = sha256(image_detector.MODEL_PATH)
            packages = {}
            for name in ("torch", "torchvision", "ultralytics", "Pillow", "numpy", "PyYAML"):
                try:
                    packages[name] = importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError:
                    packages[name] = None
            summary["environment"]["packages"] = packages
            missing = [name for name, version in packages.items() if version is None]
            if missing:
                raise RuntimeError("Required runtime packages unavailable; no installation attempted: " + ", ".join(missing))
            manifest, test_samples = load_dataset(args.dataset.resolve(), split=evaluation_split)
            summary["split_counts"] = validate_manifest_splits(manifest)
            import torch
            device = 0 if torch.cuda.is_available() else "cpu"
            config["device"] = device
            if not args.preflight:
                config["data"] = str(runtime_dataset_yaml(args.dataset, out))
            summary["environment"].update(cuda_available=torch.cuda.is_available(), torch_cuda_version=torch.version.cuda,
                                          device=device, gpu_name=torch.cuda.get_device_name(0) if device == 0 else None)
            if warm_start_path:
                source_summary, source_best = warm_start_from_summary(warm_start_path)
                summary["warm_start"] = {"summary_path": str(warm_start_path.resolve()),
                    "summary_sha256": sha256(warm_start_path), "source_dataset": source_summary["dataset"],
                    "source_manifest_sha256": source_summary["manifest_sha256"],
                    "checkpoint": source_summary["best_checkpoint"], "optimizer_resume": False}
                summary["source_checkpoint"] = dict(source_summary["best_checkpoint"])
                summary["source_checkpoint"]["path"] = str(source_best)
                same_dataset = args.dataset.resolve() == resolve_recorded_path(source_summary["dataset"], ROOT).resolve()
                summary["execution_plan"] = "existing_data_only" if same_dataset else "expanded_real_data"
                if same_dataset:
                    acquisition_path = ROOT / "data" / "image_samples" / "cghd_round2" / "acquisition.json"
                    acquisition = json.loads(acquisition_path.read_text(encoding="utf8")) if acquisition_path.is_file() else {}
                    summary["new_data_acquisition"] = {"status_at_training_start": acquisition.get("status", "not_ready"),
                        "new_images_used_in_this_run": 0, "acquisition_path": str(acquisition_path),
                        "acquisition_sha256_at_training_start": sha256(acquisition_path) if acquisition_path.is_file() else None}
                    summary["new_drafter31_test"] = "not_run; new dataset unavailable at training start"
                model = load_registered_model(source_best, source_summary["best_checkpoint"])
            else:
                model, audit = image_detector._load_model()
                validate_source_classes(model.names)
                summary["source_checkpoint"] = {"path": str(image_detector.MODEL_PATH), "sha256": sha256(image_detector.MODEL_PATH),
                    "acquisition": audit, "names": model.names}
            write_json(out / "config.json", config | {"evaluation": EVAL_CONFIG})
            if args.preflight:
                summary.update(status="ready", stage="preflight_only")
                return summary
            handler = logging.StreamHandler(log)
            logging.getLogger("ultralytics").addHandler(handler)
            summary["stage"] = f"pre_{evaluation_split}_evaluation"
            baseline = evaluate_samples(test_samples, predictor_for(model, device))
            write_json(out / f"pre_{evaluation_split}_results.json", baseline)
            summary["baseline"] = baseline["summary"]
            if baseline["summary"]["status"] != "complete":
                raise RuntimeError("Baseline image evaluation incomplete; training not started")
            if warm_start_path:
                model = load_registered_model(source_best, source_summary["best_checkpoint"])
                configure_training_callbacks(model, summary)
            else:
                model = fresh_public_model(summary["source_checkpoint"])
            summary["training_source_reloaded"] = True
            summary["training_model_fused_before_train"] = model.model.is_fused()
            if summary["training_model_fused_before_train"]:
                raise RuntimeError("Fresh public checkpoint is already fused; cannot use it for this training workflow")
            summary.update(stage="training", training_attempted=True)
            write_json(out / "run_summary.json", summary)
            training_start = time.perf_counter()
            try:
                with restricted_metadata_context():
                    model.train(**config)
            finally:
                summary["training_elapsed_seconds"] = time.perf_counter() - training_start
                collect_training_artifacts(out, summary)
            best = out / "fit" / "weights" / "best.pt"
            registration = summary["best_checkpoint"]
            write_json(out / "run_summary.json", summary)
            trained = load_registered_model(best, registration)
            summary["trained_classes"] = trained.names
            summary["stage"] = f"post_{evaluation_split}_evaluation"
            post = evaluate_samples(test_samples, predictor_for(trained, device))
            write_json(out / f"post_{evaluation_split}_results.json", post)
            summary["post_training"] = post["summary"]
            if summary["epochs_completed"] != args.epochs or post["summary"]["status"] != "complete":
                raise RuntimeError("Training or post-training image evaluation incomplete")
            if warm_start_path and not summary.get("head_transfer", {}).get("unchanged"):
                raise RuntimeError("Warm-start classification head transfer audit incomplete")
            summary.update(status="complete", stage="complete")
        except Exception as error:
            summary["errors"].append(f"{type(error).__name__}: {error}")
            traceback.print_exc()
        finally:
            if handler:
                logging.getLogger("ultralytics").removeHandler(handler)
            source = summary.get("source_checkpoint", {})
            if source.get("sha256") and Path(source["path"]).is_file():
                summary["source_checkpoint_sha256_after"] = sha256(source["path"])
                summary["source_checkpoint_unchanged"] = summary["source_checkpoint_sha256_after"] == source["sha256"]
            log.flush()
            text = (out / "run.log").read_text(encoding="utf8")
            summary["transferred_layers"] = [dict(loaded=int(a), total=int(b))
                for a, b in re.findall(r"Transferred\s+(\d+)/(\d+)\s+items", text)]
            if warm_start_path and summary["training_attempted"] and not any(
                    row["loaded"] == row["total"] == 1135 for row in summary["transferred_layers"]):
                summary.update(status="incomplete", stage="transfer_audit_failed")
                summary["errors"].append("Complete 1135/1135 warm-start transfer was not confirmed in the training log")
            summary["elapsed_seconds"] = time.perf_counter() - start
            write_json(out / "run_summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "data" / "image_samples" / "yolo_cghd_v15")
    parser.add_argument("--output", type=Path, default=ROOT / "evidence" / "yolo_training" / "current")
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--preflight", action="store_true", help="Verify dataset/runtime only; do not train")
    parser.add_argument("--evaluate-only", action="store_true", help="Reproduce post-test results from the SHA256-registered best.pt")
    parser.add_argument("--warm-start-summary", type=Path, help="Use a completed registered two-class best.pt with a new optimizer")
    parser.add_argument("--validation-only", action="store_true", help="Evaluate only validation images before/after training; do not infer test")
    args = parser.parse_args()
    if args.epochs < 1:
        parser.error("epochs must be positive")
    result = run(args)
    print(json.dumps({"status": result["status"], "stage": result["stage"], "epochs_completed": result["epochs_completed"],
                      "errors": result["errors"], "elapsed_seconds": result["elapsed_seconds"]}, ensure_ascii=False))
    return 0 if result["status"] in {"complete", "ready"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
