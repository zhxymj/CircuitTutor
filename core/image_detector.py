"""Independent HCD YOLO detector with an optional local research checkpoint.

Detection boxes are symbol bodies, never electrical ports or connections.
The default model is the pinned upstream public checkpoint. A locally
fine-tuned checkpoint can be selected explicitly (``CIRCUIT_VISION_MODEL=local``)
only when it has a hash-registered provenance file; missing or mismatched
weights stop inference instead of silently falling back to the upstream model.
Weights are not bundled in this project and local derivatives are research-only.
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
LOCAL_MODEL_ROOT = RUNTIME_ROOT / "vision-models" / "hcd-local"
LOCAL_CLASSES = {0: "resistor", 1: "capacitor.unpolarized"}
_MODEL_SOURCE_ENV = "CIRCUIT_VISION_MODEL"
_LOCAL_MODEL_ROOT_ENV = "CIRCUIT_VISION_LOCAL_MODEL_ROOT"
_model = None
_audit = None
_model_key = None


def model_source() -> str:
    """Return the explicitly selected checkpoint source (default: upstream)."""

    source = (os.environ.get(_MODEL_SOURCE_ENV) or "upstream").strip().lower()
    if source not in {"upstream", "local"}:
        raise RuntimeError("CIRCUIT_VISION_MODEL 只能是 upstream 或 local")
    return source


def local_model_root() -> Path:
    configured = os.environ.get(_LOCAL_MODEL_ROOT_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return LOCAL_MODEL_ROOT


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_local_audit(root: Path | None = None) -> dict:
    """Validate a local research checkpoint against its registration file."""

    root = Path(root).expanduser().resolve() if root is not None else local_model_root()
    checkpoint_path = root / "best.pt"
    registration_path = root / "registration.json"
    if not checkpoint_path.is_file():
        raise RuntimeError(f"本地权重不存在：{checkpoint_path}；先运行 tools/install_local_model.py")
    if not registration_path.is_file():
        raise RuntimeError(f"本地权重登记不存在：{registration_path}")
    registration = json.loads(registration_path.read_text(encoding="utf8"))
    if registration.get("schema_version") != 1 or registration.get("kind") != "local_research_finetune":
        raise RuntimeError("本地权重登记格式不受支持")
    checkpoint = registration.get("checkpoint") or {}
    classes = {str(key): value for key, value in (checkpoint.get("classes") or {}).items()}
    expected = {str(key): value for key, value in LOCAL_CLASSES.items()}
    if classes != expected:
        raise RuntimeError("本地权重类别必须严格为 resistor 与 capacitor.unpolarized")
    actual_bytes = checkpoint_path.stat().st_size
    if checkpoint.get("bytes") not in (None, actual_bytes):
        raise RuntimeError("本地权重大小与登记不一致")
    actual = _sha256(checkpoint_path)
    if actual != checkpoint.get("sha256"):
        raise RuntimeError("本地权重 SHA256 与登记不一致；推理已停止")
    source_run = registration.get("source_run") or {}
    if source_run.get("best_checkpoint_sha256") != actual:
        raise RuntimeError("本地权重与其训练运行登记不一致")
    for key in ("run_summary_sha256", "dataset_manifest_sha256"):
        value = source_run.get(key)
        if not isinstance(value, str) or len(value) != 64:
            raise RuntimeError(f"本地权重登记缺少 {key}")
    rights = registration.get("rights") or {}
    if rights.get("redistribution") != "prohibited" or rights.get("usage") != "research_evaluation_only":
        raise RuntimeError("本地权重登记缺少“仅研究、禁止再分发”声明")
    return {
        "model_source": "local",
        "repository": "local-research-finetune",
        "revision": str(source_run.get("run_summary_sha256", ""))[:12],
        "checkpoint": {"sha256": actual, "bytes": actual_bytes},
        "rights_status": rights.get("notice") or "本地微调权重仅限研究评估，不得再分发",
        "classes": {int(key): value for key, value in classes.items()},
        "source_run": source_run,
        "registration_path": str(registration_path),
    }


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
    global _model, _audit, _model_key
    source = model_source()
    checkpoint_path = MODEL_PATH if source == "upstream" else (local_model_root() / "best.pt")
    try:
        stat = checkpoint_path.stat()
        key = (source, str(checkpoint_path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        key = (source, str(checkpoint_path), None, None)
    if _model is not None and _model_key == key:
        return _model, _audit
    # Restrict serialized model loading to the official runtime's known classes;
    # never fall back to unrestricted pickle or checkpoint-requested installs.
    os.environ["ULTRALYTICS_SAFE_LOAD"] = "true"
    os.environ["YOLO_AUTOINSTALL"] = "false"
    os.environ["YOLO_CONFIG_DIR"] = str(RUNTIME_ROOT / "vision-python-config")
    os.environ["TORCH_HOME"] = str(RUNTIME_ROOT / "vision-python-cache" / "torch")
    if source == "local":
        audit = _load_local_audit(local_model_root())
    else:
        audit = json.loads((MODEL_ROOT / "acquisition.json").read_text(encoding="utf8"))
        if not audit.get("archive_sha256_verified") or not audit.get("checkpoint", {}).get("zip_member_crc_verified"):
            raise RuntimeError("Verified official archive/checkpoint acquisition required")
        actual = _sha256(MODEL_PATH)
        if actual != audit["checkpoint"]["sha256"]:
            raise RuntimeError("Checkpoint SHA256 changed; inference stopped")
        audit["model_source"] = "upstream"
    with redirect_stdout(sys.stderr):
        import ultralytics.nn.tasks as tasks
        if not getattr(tasks, "SAFE_LOAD", False):
            raise RuntimeError("Runtime does not enable restricted checkpoint loading; inference stopped")
        from ultralytics import YOLO
        with _checkpoint_globals():
            model = YOLO(str(checkpoint_path), task="detect")
    if source == "local" and dict(model.names) != audit["classes"]:
        raise RuntimeError("本地权重实际类别与登记不一致；推理已停止")
    _model, _audit, _model_key = model, audit, key
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
    source = audit.get("model_source", "upstream")
    model_meta = {
        "source": source,
        "repository": audit.get("repository"),
        "revision": audit.get("revision"),
        "checkpoint_sha256": audit["checkpoint"]["sha256"],
        "classes": model.names,
        "license_status": audit.get("rights_status"),
        "training_overlap_with_cghd": audit.get("training_overlap_with_cghd", "unknown"),
    }
    if source == "upstream":
        model_meta["archive_sha256"] = audit.get("archive_sha256_expected")
    else:
        model_meta["local_run"] = audit.get("source_run")
    issues = [{"code": "DETECTOR_PORTS_UNAVAILABLE",
               "message": "检测框只表示符号体，未推断可信引脚和网络，类型、方向、数值及连接需人工确认"}]
    if source == "local":
        issues += [
            {"code": "LOCAL_MODEL_RESEARCH_ONLY",
             "message": "本地微调权重仅限研究评估，不得再分发；上游 YOLOv10 为 AGPL-3.0 衍生，HCD 根许可证未明确"},
            {"code": "LOCAL_MODEL_NOT_UPSTREAM",
             "message": "该权重来自小规模本地微调，只支持电阻与无极性电容两类，验证集仅 4 张图；其余符号不会被检测，指标不代表通用识别能力"},
        ]
    else:
        issues.append({"code": "MODEL_LICENSE_REVIEW",
                       "message": "作者Apache声明与上游YOLO AGPL衍生义务待核对；权重未打包分发"})
    return {"method": "hcd-yolov10-local-finetune" if source == "local" else "hcd-yolov10-public-checkpoint",
            "detections": detections,
            "image": {"width": image.width, "height": image.height, "exif_applied": True},
            "model": model_meta,
            "runtime": {name: importlib.metadata.version(name) for name in ["torch", "torchvision", "ultralytics", "Pillow"]},
            "parameters": {"confidence": confidence, "image_size": image_size, "iou": 0.7, "max_det": 1000,
                           "device": device, "augment": False},
            "issues": issues}
