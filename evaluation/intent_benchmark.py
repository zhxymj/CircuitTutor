"""离线需求规划评测；不调用在线模型，也不将目录命中算作电路生成。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from typing import Any

from core import catalog, clarify, llm


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CASES = os.path.join(ROOT, "data", "intent_offline_cases.json")


def load_cases(path: str = DEFAULT_CASES) -> dict:
    with open(path, encoding="utf-8") as handle:
        dataset = json.load(handle)
    if not isinstance(dataset.get("metadata"), dict) or not isinstance(dataset.get("cases"), list):
        raise ValueError("数据集必须包含 metadata 和 cases")
    seen = set()
    prompts = set()
    for case in dataset["cases"]:
        cid = case.get("id")
        if not isinstance(cid, str) or not cid or cid in seen:
            raise ValueError(f"样本 id 缺失或重复：{cid!r}")
        seen.add(cid)
        if case.get("split") not in ("dev", "holdout"):
            raise ValueError(f"{cid}: split 只能是 dev/holdout")
        if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
            raise ValueError(f"{cid}: prompt 不能为空")
        prompt_key = "".join(case["prompt"].split()).casefold()
        if prompt_key in prompts:
            raise ValueError(f"{cid}: 与其他划分出现重复需求")
        prompts.add(prompt_key)
        if case.get("expected_clarify") not in ("clear", "clarify"):
            raise ValueError(f"{cid}: expected_clarify 无效")
        for key in ("required_types", "forbidden_types"):
            values = case.get(key)
            if not isinstance(values, list) or any(t not in catalog.CATALOG for t in values):
                raise ValueError(f"{cid}: {key} 包含目录外类型或格式错误")
    return dataset


def evaluate_case(case: dict) -> dict:
    start = time.perf_counter()
    matched = catalog.match_components(case["prompt"])
    brief = llm.build_generation_brief(case["prompt"], catalog.build_matched_lib(matched))
    question = clarify.rule_analyze(case["prompt"])
    candidate_types = set(brief["required_types"])
    missing = sorted(set(case["required_types"]) - candidate_types)
    forbidden = sorted(set(case["forbidden_types"]) & candidate_types)
    expected_unknown = set(case.get("expected_unsupported", []))
    unknown = set(brief["unsupported_part_numbers"])
    return {
        "id": case["id"], "split": case["split"],
        "candidate_types": sorted(candidate_types),
        "candidate_evidence": brief["explicit_candidates"],
        "signal_nets": brief["signal_nets"],
        "missing_required_types": missing,
        "forbidden_types_found": forbidden,
        "unsupported_part_numbers": sorted(unknown),
        "unsupported_detection_correct": unknown == expected_unknown,
        "clarify_status": question["status"],
        "clarify_questions": question["questions"],
        "clarify_correct": question["status"] == case["expected_clarify"],
        "candidate_selection_correct": not missing and not forbidden,
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
        "generation_path": "not_run_offline",
        "structure_valid": None,
        "template_fallback": None,
    }


def run(dataset: dict) -> dict:
    records = [evaluate_case(case) for case in dataset["cases"]]
    raw = json.dumps(dataset, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    summary: dict[str, Any] = {}
    for split in ("dev", "holdout", "all"):
        subset = [row for row in records if split == "all" or row["split"] == split]
        summary[split] = {
            "count": len(subset),
            "candidate_selection_correct": sum(row["candidate_selection_correct"] for row in subset),
            "clarify_correct": sum(row["clarify_correct"] for row in subset),
            "unsupported_detection_correct": sum(row["unsupported_detection_correct"] for row in subset),
            "total_elapsed_ms": round(sum(row["elapsed_ms"] for row in subset), 3),
        }
    return {
        "schema_version": "1.0",
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "dataset_metadata": dataset["metadata"],
        "summary": summary,
        "records": records,
        "limitations": [
            "内部构造的离线回归样本，不能估计公开数据泛化准确率",
            "未调用在线模型，structure_valid 和 template_fallback 不适用",
            "目录命中不等于连接正确、ERC 通过或可制造",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=DEFAULT_CASES)
    parser.add_argument("--output", help="逐例 JSON 输出路径；不提供时只打印汇总")
    args = parser.parse_args(argv)
    result = run(load_cases(args.cases))
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
