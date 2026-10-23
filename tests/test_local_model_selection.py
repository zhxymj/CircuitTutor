# -*- coding: utf-8 -*-
"""本地可选权重：来源选择、登记校验与失败即停止。

不加载 YOLO，只验证 core.image_detector 的选择与登记边界；真实推理由
tools/vision_detect.py 的端到端验证覆盖。
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import image_detector as detector

PASS = 0


def check(name, condition):
    global PASS
    if not condition:
        raise AssertionError(f"失败：{name}")
    PASS += 1
    print(f"  ✔ {name}")


def expect_error(name, func, fragment):
    try:
        func()
    except Exception as error:  # noqa: BLE001 - 断言错误信息必须命中
        check(name, fragment in str(error))
        return
    raise AssertionError(f"失败：{name}（未抛出异常）")


def make_registration(checkpoint_bytes, classes=None, rights=None, best_sha=None):
    sha = hashlib.sha256(checkpoint_bytes).hexdigest()
    return {
        "schema_version": 1,
        "kind": "local_research_finetune",
        "checkpoint": {
            "path": "best.pt",
            "bytes": len(checkpoint_bytes),
            "sha256": sha,
            "classes": classes or {"0": "resistor", "1": "capacitor.unpolarized"},
        },
        "source_run": {
            "run_summary_sha256": "a" * 64,
            "best_checkpoint_sha256": best_sha or sha,
            "dataset_manifest_sha256": "b" * 64,
        },
        "rights": rights or {
            "redistribution": "prohibited",
            "usage": "research_evaluation_only",
            "notice": "研究用途",
        },
    }


def write_case(root: Path, payload: bytes, registration: dict):
    (root / "best.pt").write_bytes(payload)
    (root / "registration.json").write_text(
        json.dumps(registration, ensure_ascii=False), encoding="utf8")


def main():
    saved = {key: os.environ.get(key) for key in
             ("CIRCUIT_VISION_MODEL", "CIRCUIT_VISION_LOCAL_MODEL_ROOT")}
    try:
        os.environ.pop("CIRCUIT_VISION_MODEL", None)
        check("默认来源是 upstream", detector.model_source() == "upstream")

        os.environ["CIRCUIT_VISION_MODEL"] = "bogus"
        expect_error("非法来源被拒绝", detector.model_source, "upstream 或 local")

        os.environ["CIRCUIT_VISION_MODEL"] = "local"
        check("显式来源是 local", detector.model_source() == "local")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = b"local-checkpoint-bytes"
            write_case(root, payload, make_registration(payload))
            audit = detector._load_local_audit(root)
            check("登记通过并标记 local", audit["model_source"] == "local")
            check("类别固定为两分类",
                  audit["classes"] == {0: "resistor", 1: "capacitor.unpolarized"})
            check("登记返回权重哈希",
                  audit["checkpoint"]["sha256"] == hashlib.sha256(payload).hexdigest())
            check("登记保留训练运行元数据",
                  audit["source_run"]["dataset_manifest_sha256"] == "b" * 64)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = b"local-checkpoint-bytes"
            write_case(root, payload, make_registration(payload))
            (root / "best.pt").unlink()
            expect_error("缺权重文件时停止", lambda: detector._load_local_audit(root), "本地权重不存在")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = b"local-checkpoint-bytes"
            registration = make_registration(payload)
            registration["checkpoint"]["sha256"] = "0" * 64
            write_case(root, payload, registration)
            expect_error("哈希不一致时停止", lambda: detector._load_local_audit(root), "SHA256")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = b"local-checkpoint-bytes"
            registration = make_registration(payload, classes={"0": "resistor", "1": "diode"})
            write_case(root, payload, registration)
            expect_error("类别不符时停止", lambda: detector._load_local_audit(root), "类别")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = b"local-checkpoint-bytes"
            registration = make_registration(
                payload, rights={"redistribution": "allowed", "usage": "production"})
            write_case(root, payload, registration)
            expect_error("缺少研究用途声明时停止", lambda: detector._load_local_audit(root), "仅研究")

        with tempfile.TemporaryDirectory() as td:
            os.environ["CIRCUIT_VISION_LOCAL_MODEL_ROOT"] = str(Path(td).resolve())
            expect_error("选择 local 但未安装时停止", detector._load_model, "本地权重不存在")

        print(f"本地可选权重：{PASS} 项通过")
        return 0
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    raise SystemExit(main())
