"""One-shot offline YOLO + fine-tuned SAM2 worker for CircuitVision."""

import argparse
import base64
import contextlib
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback


SOURCE_COMMIT = "fc5b775043b577668906b6d0bbb238a690a82965"
SOURCE_LICENSE = "Apache-2.0"
WEIGHTS_LICENSE = "unknown"
EXPECTED_CHECKPOINTS = {
    "yolo": "53266900960b44426696908a798a1669d19825010618690ec183282a4be639ff",
    "sam2_base": "2647878d5dfa5098f2f8649825738a9345572bae2d4350a2468587ece47dd318",
    "sam2_finetuned": "ff219e3ea853bcd23fef7d22642c3c6341237e2a2a3f11c1f3898c8f48becd01",
}
TYPE_BY_CLASS = {
    "resistor": "resistor",
    "capacitor": "capacitor",
    "capacitor.unpolarized": "capacitor",
    "inductor": "inductor",
    "voltage.dc": "voltage_source",
    "gnd": "ground_marker",
}
# These detector regions describe wire markings or the drawing extent. Their
# pixels must remain available to geometry extraction (as in upstream's mask
# preservation list); this does not assign electrical meaning to a crossing.
WIRE_REGION_CLASSES = frozenset({'junction', 'crossover', 'circuit', 'vss'})


class InvalidImage(ValueError):
    pass


def _to_bgr(rgb, cv2_module):
    return cv2_module.cvtColor(rgb, cv2_module.COLOR_RGB2BGR)


def _geometry_exclusion_boxes(detections):
    return [item['box'] for item in detections
            if item['class_name'] not in WIRE_REGION_CLASSES]


def _json_parameter(value):
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return list(value)
    except TypeError:
        return str(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _emit_error(code: str, message: str, exit_code: int) -> int:
    print(json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False, separators=(",", ":")))
    return exit_code


def analyze_file(image_path: Path, source_name: str, max_pixels: int) -> dict:
    started = time.perf_counter()
    source_bytes = image_path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(source_bytes)) as original:
            if original.format not in {"PNG", "JPEG"}:
                raise InvalidImage("仅接受可解码的 PNG 或 JPEG 图片。")
            if original.width * original.height > max_pixels:
                raise InvalidImage(f"图片超过 {max_pixels:,} 像素限制。")
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.load()
    except InvalidImage:
        raise
    except Exception as exc:
        raise InvalidImage("图片无法解码为有效的 PNG 或 JPEG。") from exc

    project_root = Path(__file__).resolve().parents[1]
    runtime_root = project_root / "evidence" / "upgrade" / "open_source_integration" / "runtime"
    worktree = runtime_root / "worktree" / "CircuitVision"
    yolo_path = worktree / "models" / "YOLO" / "best_large_model_yolo.pt"
    sam2_base_path = worktree / "models" / "SAM2" / "sam2.1_hiera_large.pt"
    sam2_finetuned_path = worktree / "models" / "SAM2" / "best_miou_model_SAM_latest.pth"
    config_path = worktree / "models" / "configs" / "sam2.1_hiera_l.yaml"

    checkpoints = {
        "yolo": yolo_path,
        "sam2_base": sam2_base_path,
        "sam2_finetuned": sam2_finetuned_path,
    }
    for label, path in checkpoints.items():
        if not path.is_file():
            raise RuntimeError(f"本地模型文件不存在：{path.name}")
        actual = _sha256(path)
        if actual != EXPECTED_CHECKPOINTS[label]:
            raise RuntimeError(f"本地模型校验失败：{path.name}")
    if not config_path.is_file():
        raise RuntimeError("SAM2 模型配置文件不存在。")

    # Set every library cache to the isolated runtime before importing models.
    cache_root = runtime_root / "cache"
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["YOLO_AUTOINSTALL"] = "false"
    os.environ["ULTRALYTICS_AUTOINSTALL"] = "false"
    # Use Ultralytics' own restricted allowlist for its YOLO checkpoint, not
    # unrestricted pickle loading or PyTorch's global force flag.
    os.environ.pop("TORCH_FORCE_WEIGHTS_ONLY_LOAD", None)
    os.environ.pop("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", None)
    os.environ["ULTRALYTICS_SAFE_LOAD"] = "true"
    os.environ["YOLO_CONFIG_DIR"] = str(cache_root / "ultralytics")
    os.environ["MPLCONFIGDIR"] = str(cache_root / "matplotlib")
    for key_name in (
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_API_KEY",
        "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
    ):
        os.environ.pop(key_name, None)

    import numpy as np
    import cv2
    import torch

    with contextlib.redirect_stdout(sys.stderr):
        from core.integrations.circuitvision.circuit_analyzer import CircuitAnalyzer
        from core.integrations.hcd_topology import compute_wire_geometry
        import ultralytics.nn.tasks as ultralytics_tasks
        if not ultralytics_tasks.SAFE_LOAD:
            raise RuntimeError("Ultralytics restricted SAFE_LOAD is not active.")
        init_started = time.perf_counter()
        analyzer = CircuitAnalyzer(
            yolo_path=str(yolo_path),
            sam2_config_path="configs/sam2.1/sam2.1_hiera_l.yaml",
            sam2_base_checkpoint_path=str(sam2_base_path),
            sam2_finetuned_checkpoint_path=str(sam2_finetuned_path),
            use_sam2=True,
            debug=False,
        )
        model_init_ms = round((time.perf_counter() - init_started) * 1000)
        if not analyzer.use_sam2 or analyzer.sam2_model is None or analyzer.sam2_transforms is None:
            raise RuntimeError("CircuitVision 微调 SAM2 未能加载。")
        rgb = np.asarray(image, dtype=np.uint8)
        bgr = _to_bgr(rgb, cv2)
        yolo_started = time.perf_counter()
        raw_boxes = analyzer.bboxes(bgr)
        yolo_ms = round((time.perf_counter() - yolo_started) * 1000)
        predictor_args = getattr(getattr(analyzer.yolo, "predictor", None), "args", None)
        yolo_parameters = {}
        for key in ("conf", "imgsz", "iou", "max_det"):
            value = getattr(predictor_args, key, None)
            if value is not None:
                yolo_parameters[key] = _json_parameter(value)

        detections = []
        for raw in raw_boxes:
            class_name = str(raw.get("class", ""))
            x0 = int(raw["xmin"])
            y0 = int(raw["ymin"])
            x1 = int(raw["xmax"])
            y1 = int(raw["ymax"])
            score = float(raw["confidence"])
            if not math.isfinite(score):
                raise RuntimeError("YOLO 返回了无效置信度。")
            detections.append({
                "class_id": int(raw["_yolo_class_id_temp"]),
                "class_name": class_name,
                "type": TYPE_BY_CLASS.get(class_name),
                "score": score,
                "box": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
                "pins": None,
                "value": None,
            })

        # CircuitAnalyzer and Ultralytics' numpy-array predictor both expect BGR.
        sam_started = time.perf_counter()
        mask, _overlay, _mask_extent = analyzer.segment_with_sam2(bgr)
        sam2_ms = round((time.perf_counter() - sam_started) * 1000)
        if mask is None or mask.shape[:2] != (image.height, image.width):
            raise RuntimeError("CircuitVision SAM2 未返回与原图同尺寸的前景掩码。")
        geometry_started = time.perf_counter()
        wire_geometry = compute_wire_geometry(mask, component_boxes=_geometry_exclusion_boxes(detections))
        geometry_ms = round((time.perf_counter() - geometry_started) * 1000)

    mask_bytes = io.BytesIO()
    Image.fromarray(mask).save(mask_bytes, format="PNG", optimize=True)
    mask_data_url = "data:image/png;base64," + base64.b64encode(mask_bytes.getvalue()).decode("ascii")
    runtime_versions = {
        name: importlib.metadata.version(name)
        for name in ("torch", "torchvision", "ultralytics", "SAM-2", "numpy", "opencv-python")
    }

    return {
        "status": "needs_manual_correction",
        "circuit": None,
        "accepted": False,
        "electrical_validated": False,
        "image": {
            "width": image.width,
            "height": image.height,
            "source_name": source_name,
            "sha256": source_sha256,
            "exif_applied": True,
        },
        "detections": detections,
        "segmentation": {
            "status": "segmented",
            "mask_data_url": mask_data_url,
            "width": image.width,
            "height": image.height,
            "nonzero_pixels": int(np.count_nonzero(mask)),
            "threshold": 0.0,
            "meaning": "foreground mask; electrical connectivity not inferred",
        },
        "topology": {"status": "not_inferred", "pins": None, "connections": [], "nets": []},
        "wire_geometry": wire_geometry,
        "issues": [
            {"code": "VALUES_UNKNOWN", "message": "元件数值尚未确认，请人工填写。"},
            {"code": "TOPOLOGY_NOT_INFERRED", "message": "电气引脚和连线拓扑尚未推断，请人工校正。"},
            {"code": "ENGINEER_REVIEW_REQUIRED", "message": "检测框和分割掩码仅为视觉候选，需工程师审核。"},
        ],
        "provenance": {
            "source_repository": "JKc66/CircuitVision",
            "source_commit": SOURCE_COMMIT,
            "source_license": SOURCE_LICENSE,
            "weights_license": WEIGHTS_LICENSE,
            "implementation": "core.integrations.circuitvision",
            "checkpoint_sha256": EXPECTED_CHECKPOINTS["sam2_finetuned"],
            "checkpoints_sha256": EXPECTED_CHECKPOINTS,
            "config": "configs/sam2.1/sam2.1_hiera_l.yaml (installed SAM-2 package registry)",
            "runtime": runtime_versions,
            "parameters": {
                "device": "cpu",
                "max_image_pixels": max_pixels,
                "sam2_threshold": 0.0,
                "sam2_config": "configs/sam2.1/sam2.1_hiera_l.yaml",
                "yolo": {**yolo_parameters, "device": "cpu", "restricted_safe_load": True},
            },
            "timings_ms": {
                "model_init": model_init_ms,
                "yolo": yolo_ms,
                "sam2": sam2_ms,
                "wire_geometry": geometry_ms,
                "total": round((time.perf_counter() - started) * 1000),
            },
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--source-name", default="uploaded-image")
    parser.add_argument("--max-pixels", default=20_000_000, type=int)
    args = parser.parse_args(argv)

    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = analyze_file(args.input, args.source_name, args.max_pixels)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except InvalidImage as exc:
        return _emit_error("invalid_request", str(exc), 2)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        return _emit_error("inference_failed", f"离线图像推理失败：{exc}", 1)


if __name__ == "__main__":
    raise SystemExit(main())
