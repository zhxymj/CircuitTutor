# -*- coding: utf-8 -*-
"""把已完成的本地训练运行登记为可选的本地研究权重。

只复制证据目录里的 best.pt 到仓库外的工具链目录，并写出
registration.json 供 core.image_detector 校验。权重不随仓库分发，
登记中固定写明“仅研究评估、禁止再分发”。
"""
import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.image_detector import LOCAL_CLASSES, RUNTIME_ROOT


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path, help="包含 run_summary.json 的训练运行目录")
    parser.add_argument("--dest", type=Path, default=RUNTIME_ROOT / "vision-models" / "hcd-local",
                        help="本地权重目录（默认在仓库外的工具链中）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的本地权重与登记")
    args = parser.parse_args()

    run_dir = args.run.expanduser().resolve()
    summary_path = run_dir / "run_summary.json"
    if not summary_path.is_file():
        raise SystemExit(f"缺少训练摘要：{summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf8"))
    if summary.get("status") != "complete" or summary.get("epochs_completed") != summary.get("epochs_requested"):
        raise SystemExit("只登记状态为 complete 且完成全部 epoch 的训练运行")
    best = summary.get("best_checkpoint") or {}
    best_path = Path(str(best.get("path") or "")).expanduser()
    if not best_path.is_file():
        raise SystemExit(f"训练摘要中的 best.pt 不存在：{best_path}")
    best_sha = sha256(best_path)
    if best.get("sha256") != best_sha:
        raise SystemExit("best.pt 的 SHA256 与训练摘要不一致")
    classes = {str(key): value for key, value in (summary.get("trained_classes") or {}).items()}
    expected = {str(key): value for key, value in LOCAL_CLASSES.items()}
    if classes != expected:
        raise SystemExit("训练类别必须严格为 resistor 与 capacitor.unpolarized")
    dataset_manifest = summary.get("manifest_sha256")
    if not isinstance(dataset_manifest, str) or len(dataset_manifest) != 64:
        raise SystemExit("训练摘要缺少数据集 manifest SHA256")

    dest = args.dest.expanduser().resolve()
    if dest == ROOT or ROOT in dest.parents:
        raise SystemExit("拒绝把权重写入仓库；请使用仓库外的工具链目录")
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / "best.pt"
    registration_path = dest / "registration.json"
    if (target.exists() or registration_path.exists()) and not args.force:
        raise SystemExit(f"目标已存在：{dest}；需要覆盖时加 --force")
    shutil.copy2(best_path, target)
    copied_sha = sha256(target)
    if copied_sha != best_sha:
        raise SystemExit("复制后的权重 SHA256 不一致")

    registration = {
        "schema_version": 1,
        "kind": "local_research_finetune",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "checkpoint": {
            "path": "best.pt",
            "bytes": target.stat().st_size,
            "sha256": copied_sha,
            "classes": {str(key): value for key, value in LOCAL_CLASSES.items()},
        },
        "source_run": {
            "run_summary": str(summary_path),
            "run_summary_sha256": sha256(summary_path),
            "best_checkpoint_sha256": best_sha,
            "dataset": summary.get("dataset"),
            "dataset_manifest_sha256": dataset_manifest,
            "epochs_completed": summary.get("epochs_completed"),
            "training_mode": summary.get("training_mode"),
            "source_checkpoint_sha256": (summary.get("source_checkpoint") or {}).get("sha256"),
            "post_training": summary.get("post_training"),
            "training_elapsed_seconds": summary.get("training_elapsed_seconds"),
        },
        "rights": {
            "redistribution": "prohibited",
            "usage": "research_evaluation_only",
            "upstream_runtime_license": "AGPL-3.0 (YOLOv10)",
            "upstream_rights_status": "Author Apache declaration and upstream YOLO derivative obligations unresolved",
            "notice": "本地微调权重仅限研究评估，不得再分发；上游 YOLOv10 为 AGPL-3.0 衍生，HCD 根许可证未明确。",
        },
    }
    registration_path.write_text(
        json.dumps(registration, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf8",
    )
    print(json.dumps({
        "status": "installed",
        "checkpoint": str(target),
        "sha256": copied_sha,
        "bytes": target.stat().st_size,
        "registration": str(registration_path),
        "source_run": str(run_dir),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
