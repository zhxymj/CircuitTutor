"""Independent public HCD YOLO detector; no HCD repository code is imported.

Detection boxes are symbol bodies, never electrical ports or connections.
The author's Apache declaration and upstream AGPL obligations are separately
recorded in the acquisition manifest; weights are not bundled in this project.
"""
from contextlib import redirect_stdout
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys

def _runtime_root():
    configured = os.environ.get("CIRCUIT_VISION_RUNTIME_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    desktop = Path.home() / "Desktop" / "智绘电路工具链"
    # 项目与工具链 2026-10-14 起同在 D://智绘电路// 下（兄弟目录），
    # 相对定位优先于 Desktop 硬编码，整体搬迁后无需改代码。
    # 2026-10-14 起布局：D://智绘电路//智绘电路工具链 与
    # D://智绘电路//智绘电路-企业命题演示包//智绘电路// 并列；
    # 逐级向上找兄弟工具链，兼容整体搬迁与不同嵌套深度。
    here = Path(__file__).resolve()
    siblings = [here.parents[n] / "智绘电路工具链" for n in (2, 3, 4) if len(here.parents) > n]
    for candidate in siblings + [desktop, desktop / "智绘电路工具链"]:
        if (candidate / "vision-models" / "hcd" / "acquisition.json").is_file():
            return candidate
    return siblings[0]


RUNTIME_ROOT = _runtime_root()
MODEL_ROOT = RUNTIME_ROOT / "vision-models" / "hcd"
MODEL_PATH = MODEL_ROOT / "best.pt"
_model = None
_audit = None


def _checkpoint_globals():
    """Scope audited official training metadata types to a restricted load."""
    import numpy as np
    from numpy._core.multiarray import scalar
    import torch
    from ultralytics.utils import IterableSimpleNamespace
    from ultralytics.utils.loss import E2EDetectLoss, v8DetectionLoss, BboxLoss, DFLoss
    from ultralytics.utils.tal import TaskAlignedAssigner
    # The static checkpoint scan contains these legacy loss objects and one
    # NumPy scalar with dtype('f8'); no checkpoint-provided imports are resolved.
    return torch.serialization.safe_globals([
        IterableSimpleNamespace, E2EDetectLoss, v8DetectionLoss, BboxLoss, DFLoss,
        TaskAlignedAssigner, np.dtype, np.dtypes.Float64DType, scalar,
    ])


def _load_model():
    global _model, _audit
    if _model is not None:
        return _model, _audit
    # Restrict serialized model loading to the official runtime's known classes;
    # never fall back to unrestricted pickle or checkpoint-requested installs.
    os.environ["ULTRALYTICS_SAFE_LOAD"] = "true"
    os.environ["YOLO_AUTOINSTALL"] = "false"
    os.environ["YOLO_CONFIG_DIR"] = str(RUNTIME_ROOT / "vision-python-config")
    os.environ["TORCH_HOME"] = str(RUNTIME_ROOT / "vision-python-cache" / "torch")
    audit = json.loads((MODEL_ROOT / "acquisition.json").read_text(encoding="utf8"))
    if not audit.get("archive_sha256_verified") or not audit.get("checkpoint", {}).get("zip_member_crc_verified"):
        raise RuntimeError("Verified official archive/checkpoint acquisition required")
    actual = hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()
    if actual != audit["checkpoint"]["sha256"]:
        raise RuntimeError("Checkpoint SHA256 changed; inference stopped")
    with redirect_stdout(sys.stderr):
        import ultralytics.nn.tasks as tasks
        if not getattr(tasks, "SAFE_LOAD", False):
            raise RuntimeError("Runtime does not enable restricted checkpoint loading; inference stopped")
        from ultralytics import YOLO
        with _checkpoint_globals():
            model = YOLO(str(MODEL_PATH), task="detect")
    _model, _audit = model, audit
    return model, audit


def detect_image(filename, confidence=0.25, image_size=1280, device="cpu"):
    """Return raw original-image-coordinate detections; no ports are guessed."""
    filename = Path(filename).resolve(strict=True)
    if not filename.is_file() or filename.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        raise ValueError("A local PNG/JPEG file is required")
    if not 0.01 <= confidence <= 0.99 or image_size not in {640, 1280}:
        raise ValueError("Invalid detector parameters")
    from PIL import Image, ImageOps
    with Image.open(filename) as original:
        if original.width * original.height > 20_000_000:
            raise ValueError("Input exceeds 20 million pixels")
        image = ImageOps.exif_transpose(original).convert("RGB")
    model, audit = _load_model()
    with redirect_stdout(sys.stderr):
        predictions = model.predict(source=image, imgsz=image_size, conf=confidence,
                                    iou=0.7, max_det=1000, device=device, verbose=False,
                                    save=False, save_txt=False, augment=False)
    output = predictions[0]
    detections = []
    for row in output.boxes.data.cpu().tolist():
        x0, y0, x1, y1, score, class_id = row[:6]
        class_id = int(class_id)
        class_name = model.names[class_id]
        detections.append({"class_id": class_id, "class_name": class_name,
                           "type": {"resistor": "resistor", "capacitor.unpolarized": "capacitor"}.get(class_name),
                           "score": score, "scoreMeaning": "uncalibrated model confidence, not circuit correctness",
                           "box": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
                           "pins": None, "pin_status": "not_inferred"})
    return {"method": "hcd-yolov10-public-checkpoint", "detections": detections,
            "image": {"width": image.width, "height": image.height, "exif_applied": True},
            "model": {"repository": audit["repository"], "revision": audit["revision"],
                      "archive_sha256": audit["archive_sha256_expected"],
                      "checkpoint_sha256": audit["checkpoint"]["sha256"],
                      "classes": model.names, "license_status": audit["rights_status"],
                      "training_overlap_with_cghd": "unknown"},
            "runtime": {name: importlib.metadata.version(name) for name in ["torch", "torchvision", "ultralytics", "Pillow"]},
            "parameters": {"confidence": confidence, "image_size": image_size, "iou": 0.7, "max_det": 1000,
                           "device": device, "augment": False},
            "issues": [{"code": "DETECTOR_PORTS_UNAVAILABLE", "message": "检测框只表示符号体，未推断可信引脚和网络，类型、方向、数值及连接需人工确认"},
                       {"code": "MODEL_LICENSE_REVIEW", "message": "作者Apache声明与上游YOLO AGPL衍生义务待核对；权重未打包分发"}]}
