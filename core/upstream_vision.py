"""Isolated, offline bridge to the pinned CircuitVision runtime."""

import base64
import binascii
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import NamedTuple


MAX_DATA_URL_BYTES = 10 * 1024 * 1024
WORKER_TIMEOUT_SECONDS = 180
MAX_IMAGE_PIXELS = 20_000_000
_WORKER_LOCK = threading.BoundedSemaphore(1)
_DATA_URL_RE = re.compile(r"^data:image/(png|jpeg);base64,([A-Za-z0-9+/]*={0,2})$")
_API_KEY_NAMES = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
)


class UpstreamVisionError(Exception):
    """A stable error boundary for the independent CircuitVision worker."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class RuntimePaths(NamedTuple):
    python: Path
    worker: Path
    worktree: Path


def _runtime_paths():
    project_root = Path(__file__).resolve().parents[1]
    configured_root = os.environ.get("CIRCUIT_VISION_RUNTIME_ROOT")
    if configured_root:
        toolchain_root = Path(configured_root).expanduser().resolve()
    else:
        parents = Path(__file__).resolve().parents
        toolchain_root = parents[3] / "智绘电路工具链"

    runtime_root = project_root / "evidence" / "upgrade" / "open_source_integration" / "runtime"
    return RuntimePaths(
        python=toolchain_root / "open-source" / "circuitvision" / "venv" / "Scripts" / "python.exe",
        worker=project_root / "tools" / "circuitvision_worker.py",
        worktree=runtime_root / "worktree" / "CircuitVision",
    )


def _decode_data_url(image_data_url: str):
    if not isinstance(image_data_url, str):
        raise UpstreamVisionError("invalid_request", "图片必须使用 PNG 或 JPEG base64 数据 URL。")
    try:
        encoded_size = len(image_data_url.encode("ascii"))
    except UnicodeEncodeError as exc:
        raise UpstreamVisionError("invalid_request", "图片数据 URL 格式无效。") from exc
    if encoded_size > MAX_DATA_URL_BYTES:
        raise UpstreamVisionError("invalid_request", "图片请求超过 10 MiB 限制。")
    match = _DATA_URL_RE.fullmatch(image_data_url)
    if not match:
        raise UpstreamVisionError("invalid_request", "仅接受 PNG/JPEG base64 数据 URL。")

    mime_subtype, encoded = match.groups()
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UpstreamVisionError("invalid_request", "图片 base64 编码无效。") from exc
    if not image_bytes:
        raise UpstreamVisionError("invalid_request", "图片数据为空。")

    expected_signature = b"\x89PNG\r\n\x1a\n" if mime_subtype == "png" else b"\xff\xd8\xff"
    if not image_bytes.startswith(expected_signature):
        raise UpstreamVisionError("invalid_request", "图片内容与声明的 PNG/JPEG 类型不一致。")
    return mime_subtype, image_bytes


def _sanitize_source_name(source_name: str, mime_subtype: str) -> str:
    if source_name is None:
        source_name = ""
    if not isinstance(source_name, str) or len(source_name) > 512:
        raise UpstreamVisionError("invalid_request", "source_name 无效。")
    name = source_name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char.isprintable())[:255].strip()
    if not name:
        name = "uploaded-image.png" if mime_subtype == "png" else "uploaded-image.jpg"
    return name


def _validate_worker_result(result):
    if not isinstance(result, dict):
        raise UpstreamVisionError("inference_failed", "CircuitVision worker 返回格式无效。")
    if result.get("status") != "needs_manual_correction":
        raise UpstreamVisionError("inference_failed", "CircuitVision worker 未返回待人工校正结果。")
    if result.get("circuit") is not None or result.get("accepted") is not False or result.get("electrical_validated") is not False:
        raise UpstreamVisionError("inference_failed", "CircuitVision worker 返回了不允许自动接受的电路状态。")

    detections = result.get("detections")
    if not isinstance(detections, list):
        raise UpstreamVisionError("inference_failed", "CircuitVision worker 未返回原始检测列表。")
    image = result.get("image")
    if not isinstance(image, dict) or type(image.get("width")) is not int or type(image.get("height")) is not int:
        raise UpstreamVisionError("inference_failed", "CircuitVision worker 图片尺寸字段无效。")
    width, height = image["width"], image["height"]
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise UpstreamVisionError("inference_failed", "CircuitVision worker 图片尺寸超出限制。")
    if not isinstance(image.get("source_name"), str) or not isinstance(image.get("sha256"), str) or len(image["sha256"]) != 64 or image.get("exif_applied") is not True:
        raise UpstreamVisionError("inference_failed", "CircuitVision worker 图片来源字段无效。")
    for detection in detections:
        if not isinstance(detection, dict) or not {"class_id", "class_name", "type", "score", "box", "pins", "value"}.issubset(detection):
            raise UpstreamVisionError("inference_failed", "CircuitVision worker 检测字段不完整。")
        if type(detection["class_id"]) is not int or not isinstance(detection["class_name"], str) or not detection["class_name"]:
            raise UpstreamVisionError("inference_failed", "CircuitVision worker 检测类别字段无效。")
        if detection["type"] is not None and not isinstance(detection["type"], str):
            raise UpstreamVisionError("inference_failed", "CircuitVision worker 检测类型字段无效。")
        score = detection["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
            raise UpstreamVisionError("inference_failed", "CircuitVision worker 检测置信度无效。")
        box = detection["box"]
        if not isinstance(box, dict) or not {"x", "y", "w", "h"}.issubset(box):
            raise UpstreamVisionError("inference_failed", "CircuitVision worker 检测框字段无效。")
        coordinates = [box[key] for key in ("x", "y", "w", "h")]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in coordinates):
            raise UpstreamVisionError("inference_failed", "CircuitVision worker 检测框坐标无效。")
        x, y, box_width, box_height = coordinates
        if x < 0 or y < 0 or box_width <= 0 or box_height <= 0 or x + box_width > width + 1 or y + box_height > height + 1:
            raise UpstreamVisionError("inference_failed", "CircuitVision worker 检测框超出图片范围。")
        if detection["pins"] is not None or detection["value"] is not None:
            raise UpstreamVisionError("inference_failed", "CircuitVision worker 不得猜测引脚或元件数值。")

    segmentation = result.get("segmentation")
    if not isinstance(segmentation, dict) or segmentation.get("status") != "segmented":
        raise UpstreamVisionError("inference_failed", "CircuitVision worker 未返回真实分割掩码。")
    if segmentation.get("width") != width or segmentation.get("height") != height:
        raise UpstreamVisionError("inference_failed", "CircuitVision worker 分割掩码尺寸与图片不一致。")
    nonzero = segmentation.get("nonzero_pixels")
    threshold = segmentation.get("threshold")
    if type(nonzero) is not int or not 0 <= nonzero <= width * height or isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
        raise UpstreamVisionError("inference_failed", "CircuitVision worker 分割掩码元数据无效。")
    mask_url = segmentation.get("mask_data_url")
    mask_match = _DATA_URL_RE.fullmatch(mask_url) if isinstance(mask_url, str) else None
    if not mask_match or mask_match.group(1) != "png":
        raise UpstreamVisionError("inference_failed", "CircuitVision worker 分割掩码编码无效。")
    try:
        mask_bytes = base64.b64decode(mask_match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UpstreamVisionError("inference_failed", "CircuitVision worker 分割掩码编码无效。") from exc
    if not mask_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise UpstreamVisionError("inference_failed", "CircuitVision worker 分割掩码不是 PNG。")

    topology = result.get("topology")
    if not isinstance(topology, dict) or topology.get("status") != "not_inferred" or topology.get("connections") != [] or topology.get("nets") != []:
        raise UpstreamVisionError("inference_failed", "CircuitVision worker 拓扑状态必须保持未推断。")
    geometry = result.get("wire_geometry")
    if geometry is not None:
        if (not isinstance(geometry, dict) or geometry.get("status") != "geometry_candidates"
                or geometry.get("candidate_only") is not True or geometry.get("accepted") is not False
                or geometry.get("electrical_validated") is not False
                or geometry.get("width") != width or geometry.get("height") != height
                or not isinstance(geometry.get("segments"), list) or not isinstance(geometry.get("points"), list)):
            raise UpstreamVisionError("inference_failed", "导线几何候选不得自动通过电气确认。")
        for items, keys in ((geometry["segments"], ("x1", "y1", "x2", "y2")),
                            (geometry["points"], ("x", "y"))):
            for item in items:
                if not isinstance(item, dict) or any(
                    isinstance(item.get(key), bool) or not isinstance(item.get(key), (int, float))
                    or not math.isfinite(item[key]) for key in keys
                ):
                    raise UpstreamVisionError("inference_failed", "导线几何候选坐标无效。")
    issues = result.get("issues")
    required_issue_codes = {"VALUES_UNKNOWN", "TOPOLOGY_NOT_INFERRED", "ENGINEER_REVIEW_REQUIRED"}
    if not isinstance(issues, list) or not required_issue_codes.issubset({item.get("code") for item in issues if isinstance(item, dict)}):
        raise UpstreamVisionError("inference_failed", "CircuitVision worker 缺少人工复核提示。")
    return result


def _terminate_worker_tree(process):
    """Stop only the subprocess tree started for this request, then reap it."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.communicate(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _communicate_worker(process, *, timeout, input=None, is_cancelled=None):
    """Drain worker pipes while checking cancellation; reap only its own tree."""
    deadline = time.monotonic() + timeout
    pending_input = {} if input is None else {'input': input}
    try:
        if is_cancelled is None:
            return process.communicate(timeout=timeout, **pending_input)
        while True:
            if is_cancelled():
                raise InterruptedError('Worker request cancelled')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            try:
                return process.communicate(timeout=min(.2, remaining), **pending_input)
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise
            finally:
                # communicate retains pending stdin after a timeout.
                pending_input = {}
    except (subprocess.TimeoutExpired, InterruptedError):
        _terminate_worker_tree(process)
        raise


def _runtime_ready(paths):
    if paths is None:
        return False
    required = (
        paths.python,
        paths.worker,
        Path(__file__).resolve().parent / "integrations" / "circuitvision" / "circuit_analyzer.py",
        Path(__file__).resolve().parent / "integrations" / "circuitvision" / "sam2_infer.py",
        paths.worktree / "models" / "configs" / "sam2.1_hiera_l.yaml",
        paths.worktree / "models" / "YOLO" / "best_large_model_yolo.pt",
        paths.worktree / "models" / "SAM2" / "sam2.1_hiera_large.pt",
        paths.worktree / "models" / "SAM2" / "best_miou_model_SAM_latest.pth",
    )
    return all(path.is_file() for path in required)


def analyze_image(image_data_url: str, *, source_name: str = "", is_cancelled=None) -> dict:
    """Run one offline image analysis in an isolated CPU-only subprocess."""
    mime_subtype, image_bytes = _decode_data_url(image_data_url)
    source_name = _sanitize_source_name(source_name, mime_subtype)
    paths = _runtime_paths()
    if not _runtime_ready(paths):
        raise UpstreamVisionError("runtime_unavailable", "CircuitVision 隔离运行环境或模型文件不可用。")
    if not _WORKER_LOCK.acquire(blocking=False):
        raise UpstreamVisionError("busy", "CircuitVision 正在处理另一张图片，请稍后重试。")

    suffix = ".png" if mime_subtype == "png" else ".jpg"
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="circuitvision-", suffix=suffix, delete=False) as temporary:
            temporary.write(image_bytes)
            temporary_path = Path(temporary.name)

        environment = os.environ.copy()
        for key_name in _API_KEY_NAMES:
            environment.pop(key_name, None)
        environment.update({
            "PYTHON_DOTENV_DISABLED": "1",
            "CUDA_VISIBLE_DEVICES": "",
            "YOLO_AUTOINSTALL": "false",
            "ULTRALYTICS_AUTOINSTALL": "false",
            "MPLCONFIGDIR": str(paths.worktree.parent / "cache" / "matplotlib"),
            "YOLO_CONFIG_DIR": str(paths.worktree.parent / "cache" / "ultralytics"),
        })

        command = [
            str(paths.python), "-B", "-m", "tools.circuitvision_worker",
            "--input", str(temporary_path),
            "--source-name", source_name,
            "--max-pixels", str(MAX_IMAGE_PIXELS),
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=str(Path(__file__).resolve().parents[1]),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = _communicate_worker(process, timeout=WORKER_TIMEOUT_SECONDS,
                                                 is_cancelled=is_cancelled)
        except subprocess.TimeoutExpired as exc:
            raise UpstreamVisionError("timeout", "CircuitVision 离线分析超过 180 秒。") from exc
        except InterruptedError as exc:
            raise UpstreamVisionError("cancelled", "已取消本机图片识别。") from exc
        except OSError as exc:
            raise UpstreamVisionError("runtime_unavailable", "无法启动 CircuitVision 隔离进程。") from exc

        stdout = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else stdout
        stderr = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr

        try:
            worker_output = json.loads(stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise UpstreamVisionError("inference_failed", "CircuitVision worker 未返回有效 JSON。") from exc

        if process.returncode != 0:
            error = worker_output.get("error", {}) if isinstance(worker_output, dict) else {}
            code = error.get("code")
            if code == "invalid_request":
                raise UpstreamVisionError(code, error.get("message", "图片内容无效。"))
            raise UpstreamVisionError("inference_failed", error.get("message", "CircuitVision 离线推理失败。"))

        return _validate_worker_result(worker_output)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        _WORKER_LOCK.release()
