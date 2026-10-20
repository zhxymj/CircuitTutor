"""Recheck the fixed CGHD holdout geometry without changing archived OCR results.

Run from the project root:
    python -X utf8 -B -m evaluation.image_geometry_recheck

Requires the existing evaluation environment's Pillow, NumPy, and Node.js.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

import numpy as np
from PIL import Image, ImageOps


ROOT = pathlib.Path(__file__).resolve().parents[1]
SAMPLE_ROOT = ROOT / "data" / "image_samples" / "cghd_v15"
MANIFEST_PATH = SAMPLE_ROOT / "manifest.json"
OUTPUT_PATH = ROOT / "evaluation" / "image_public_results.json"
RECOGNIZER_PATH = ROOT / "web" / "image_recognition.js"
COMMAND = "python -X utf8 -B -m evaluation.image_geometry_recheck"
NODE_PROGRAM = """
const fs = require('node:fs');
const I = require('./web/image_recognition.js');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const bytes = Buffer.from(input.rgba, 'base64');
const result = I.recognizeRaster({
  width: input.width, height: input.height, data: new Uint8ClampedArray(bytes),
}, {sourceId: input.id, threshold: 170, minRun: 4});
process.stdout.write(JSON.stringify({
  geometry_limit: result.issues.some(issue => issue.code === 'GEOMETRY_LIMIT'),
  detections: result.detections.map(d => ({type: d.type, box: d.box})),
  connections: result.circuit.connections.length,
}));
"""


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def overlap(a: dict, b: dict) -> float:
    x1 = max(a["x"], b["x"])
    y1 = max(a["y"], b["y"])
    x2 = min(a["x"] + a["w"], b["x"] + b["w"])
    y2 = min(a["y"] + a["h"], b["y"] + b["h"])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    union = a["w"] * a["h"] + b["w"] * b["h"] - intersection
    return intersection / union if union else 0.0


def evaluate(sample: dict) -> dict:
    image = ImageOps.exif_transpose(Image.open(SAMPLE_ROOT / sample["image"])).convert("RGBA")
    ratio = min(1.0, 700 / max(image.size))
    width = max(1, math.floor(image.width * ratio + 0.5))
    height = max(1, math.floor(image.height * ratio + 0.5))
    original = np.asarray(image)
    ys = np.minimum(image.height - 1, np.floor(np.arange(height) / ratio).astype(int))
    xs = np.minimum(image.width - 1, np.floor(np.arange(width) / ratio).astype(int))
    pixels = original[np.ix_(ys, xs)]
    payload = {
        "id": sample["id"], "width": width, "height": height,
        "rgba": base64.b64encode(pixels.tobytes()).decode("ascii"),
    }
    completed = subprocess.run(
        ["node", "-e", NODE_PROGRAM], input=json.dumps(payload), text=True,
        capture_output=True, cwd=ROOT, check=True,
    )
    result = json.loads(completed.stdout)
    truth = []
    for item in sample["objects"]:
        if item["name"] not in ("resistor", "capacitor.unpolarized"):
            continue
        box = item["bbox"]
        x, y = float(box["xmin"]), float(box["ymin"])
        truth.append({
            "type": "resistor" if item["name"] == "resistor" else "capacitor",
            "box": {"x": x, "y": y, "w": float(box["xmax"]) - x, "h": float(box["ymax"]) - y},
        })
    matched = set()
    tp = 0
    for detection in result["detections"]:
        box = {key: detection["box"][key] / ratio for key in ("x", "y", "w", "h")}
        best_index, best_overlap = -1, 0.0
        for index, item in enumerate(truth):
            score = overlap(box, item["box"]) if index not in matched and detection["type"] == item["type"] else 0.0
            if score > best_overlap:
                best_index, best_overlap = index, score
        if best_overlap >= 0.5:
            matched.add(best_index)
            tp += 1
    return {
        "id": sample["id"], "drafter": sample["drafter"],
        "image": sample["image"], "annotation": sample["annotation"],
        "image_sha256": next(f["sha256"] for f in sample["files"] if f["path"] == sample["image"]),
        "annotation_sha256": next(f["sha256"] for f in sample["files"] if f["path"] == sample["annotation"]),
        "oriented_dimensions": [image.width, image.height], "working_dimensions": [width, height],
        "geometry_limit": result["geometry_limit"],
        "component": {"tp": tp, "fp": len(result["detections"]) - tp, "fn": len(truth) - tp, "gt_count": len(truth)},
        "inferred_connections": result["connections"],
    }


def main() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if len(manifest["samples"]) != 30:
        raise ValueError("Expected exactly 30 fixed CGHD samples")
    groups = {
        split: [item for item in manifest["samples"] if item["split"] == split]
        for split in ("development", "holdout")
    }
    if len(groups["development"]) != 20 or len(groups["holdout"]) != 10:
        raise ValueError("Expected 20 development and 10 holdout samples")
    if {item["drafter"] for item in groups["development"]} & {item["drafter"] for item in groups["holdout"]}:
        raise ValueError("Drafter leakage between development and holdout")
    for sample in manifest["samples"]:
        for file in sample["files"]:
            if sha256(SAMPLE_ROOT / file["path"]) != file["sha256"]:
                raise ValueError("Manifest SHA256 mismatch: " + file["path"])
    cases = [evaluate(item) for item in groups["holdout"]]
    totals = {
        "images": len(cases),
        "geometry_limit": sum(item["geometry_limit"] for item in cases),
        "component_tp": sum(item["component"]["tp"] for item in cases),
        "component_fp": sum(item["component"]["fp"] for item in cases),
        "component_fn": sum(item["component"]["fn"] for item in cases),
        "inferred_connections": sum(item["inferred_connections"] for item in cases),
        "component_precision": None,
    }
    if totals["component_tp"] + totals["component_fp"]:
        totals["component_precision"] = totals["component_tp"] / (totals["component_tp"] + totals["component_fp"])
    totals["component_recall"] = totals["component_tp"] / (totals["component_tp"] + totals["component_fn"])
    previous = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    previous["current_geometry_recheck"] = {
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "command": COMMAND,
        "source": manifest["source"], "license": manifest["license"],
        "manifest": "data/image_samples/cghd_v15/manifest.json",
        "manifest_sha256": sha256(MANIFEST_PATH),
        "recognizer": "web/image_recognition.js", "recognizer_sha256": sha256(RECOGNIZER_PATH),
        "runtime": {
            "python": sys.version.split()[0], "pillow": Image.__version__, "numpy": np.__version__,
            "node": subprocess.check_output(["node", "--version"], text=True).strip(),
        },
        "method": "Pillow EXIF transpose and RGBA JPEG decode; NumPy reproduces fixed 700-pixel nearest-neighbor mapping; unchanged JavaScript recognizeRaster geometry; IoU >= 0.5 on resistor and unpolarized-capacitor boxes.",
        "options": {"max_dimension": 700, "resize": "nearest-neighbor", "threshold": 170, "minRun": 4, "line_budget": 700, "matching_iou": 0.5},
        "scope": "Only the fixed 10 drafter-disjoint holdout images; alternate JPEG decoder may differ at threshold boundaries from archived jpeg-js run.",
        "ocr_status": "incomplete: F-drive Tesseract.js/jpeg-js runtime absent; archived OCR is not a result of this recheck",
        "manual_correction_status": "incomplete: no per-sample corrected candidate and validation-gate evidence",
        "network_accuracy": None,
        "totals": totals, "cases": cases,
    }
    OUTPUT_PATH.write_text(json.dumps(previous, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"totals": totals, "cases": cases}, ensure_ascii=False))


if __name__ == "__main__":
    main()
