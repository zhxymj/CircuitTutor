# -*- coding: utf-8 -*-
"""固定中文需求集的在线 AI 候选生成评测。

本模块把模型调用路径与最终电路质量分开计数。模型失败后得到的模板兜底
仍可用于演示，但绝不计为 AI 完成。数据集是项目内部固定构造集，只用于
端到端功能验证，不是公开数据集或生产分布的泛化准确率估计。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from core.catalog import CATALOG, build_erc_kb, build_matched_lib, match_components
from core.clarify import rule_analyze
from core.erc import run_erc
from core.llm import DeepSeekClient, build_generation_brief, validate_circuit_json


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CASES_PATH = os.path.join(ROOT, "evidence", "ai_requirements_cases.json")
DEFAULT_OUTPUT_DIR = os.path.join(ROOT, "evidence")


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 6) if denominator else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def load_cases(path: str = DEFAULT_CASES_PATH) -> dict:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict):
        raise ValueError("AI 需求数据必须包含 metadata 对象")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("AI 需求数据必须包含非空 cases 数组")
    seen = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("每条 AI 需求样本必须是对象")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen:
            raise ValueError(f"case_id 缺失或重复: {case_id!r}")
        seen.add(case_id)
        if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
            raise ValueError(f"{case_id}: prompt 不能为空")
        for key in ("required_types", "forbidden_types"):
            if not isinstance(case.get(key, []), list):
                raise ValueError(f"{case_id}: {key} 必须是数组")
            unknown = sorted({item for item in case.get(key, []) if item not in CATALOG})
            if unknown:
                raise ValueError(f"{case_id}: {key} 含目录外类型 {unknown}")
    return payload


def evaluate_case(case: dict, circuit: Optional[dict], generation_meta: Optional[dict],
                  elapsed_ms: float, error: Optional[str] = None) -> dict:
    """评估一条固定需求；模板兜底不会被计为 AI 完成。"""
    preflight = build_generation_brief(
        case["prompt"], build_matched_lib(match_components(case["prompt"])))
    clarification = rule_analyze(case["prompt"])
    generation_meta = generation_meta or {}
    path = str(generation_meta.get("path") or ("error" if error else "unknown"))
    llm_completed = path == "llm" and error is None
    fallback_reason_type = None
    note = str(generation_meta.get("note") or "")
    if path == "template" and "原因:" in note:
        candidate = note.split("原因:", 1)[1].split(":", 1)[0].split("）", 1)[0].strip()
        fallback_reason_type = (candidate if candidate.endswith(("Error", "Exception"))
                                else "other")
    structure_errors = (["没有生成 CircuitJSON"] if not isinstance(circuit, dict)
                        else validate_circuit_json(circuit, CATALOG))
    structure_valid = not structure_errors

    present_types = sorted({str(part.get("type"))
                            for part in (circuit or {}).get("parts", [])
                            if isinstance(part, dict) and part.get("type")})
    present = set(present_types)
    required = sorted(set(case.get("required_types") or []))
    forbidden = sorted(set(case.get("forbidden_types") or []))
    missing = sorted(set(required) - present)
    forbidden_found = sorted(set(forbidden) & present)
    recall = _ratio(len(required) - len(missing), len(required))

    erc_counts = {"fatal": 0, "major": 0, "minor": 0, "warning": 0}
    detected_rules = []
    if structure_valid:
        report = run_erc(circuit, build_erc_kb())
        erc_counts = report.counts()
        detected_rules = sorted({issue.rule for issue in report.issues})
    severe_erc_free = structure_valid and not erc_counts["fatal"] and not erc_counts["major"]
    requirements_satisfied = recall == 1.0 and not forbidden_found
    end_to_end_pass = bool(llm_completed and structure_valid and
                           requirements_satisfied and severe_erc_free)

    return {
        "case_id": case["case_id"],
        "prompt": case["prompt"],
        "catalog_preflight": preflight,
        "clarify_status": clarification["status"],
        "clarify_questions": clarification["questions"],
        "generation_path": path,
        "llm_completed": llm_completed,
        "fallback_reason_type": fallback_reason_type,
        "structure_valid": structure_valid,
        "structure_errors": structure_errors,
        "present_types": present_types,
        "required_types": required,
        "missing_required_types": missing,
        "required_type_recall": recall,
        "forbidden_types_found": forbidden_found,
        "requirements_satisfied": requirements_satisfied,
        "erc_counts": erc_counts,
        "detected_rules": detected_rules,
        "severe_erc_free": severe_erc_free,
        "end_to_end_pass": end_to_end_pass,
        "elapsed_ms": round(float(elapsed_ms), 3),
        "error": str(error)[:500] if error else None,
    }


def summarize_records(records: Iterable[dict]) -> dict:
    rows = list(records)
    attempted = len(rows)
    llm_completed = sum(bool(row.get("llm_completed")) for row in rows)
    fallbacks = sum(row.get("generation_path") == "template" for row in rows)
    errors = sum(row.get("generation_path") == "error" for row in rows)
    structure_valid = sum(bool(row.get("structure_valid")) for row in rows)
    requirements_satisfied = sum(bool(row.get("requirements_satisfied")) for row in rows)
    severe_erc_free = sum(bool(row.get("severe_erc_free")) for row in rows)
    end_to_end = sum(bool(row.get("end_to_end_pass")) for row in rows)
    required_total = sum(len(row.get("required_types") or []) for row in rows)
    required_found = sum(len(row.get("required_types") or []) -
                         len(row.get("missing_required_types") or []) for row in rows)
    elapsed = [float(row.get("elapsed_ms") or 0.0) for row in rows]
    return {
        "attempted": attempted,
        "llm_completed": llm_completed,
        "template_fallbacks": fallbacks,
        "generation_errors": errors,
        "structure_valid": structure_valid,
        "requirements_satisfied": requirements_satisfied,
        "severe_erc_free": severe_erc_free,
        "end_to_end_pass": end_to_end,
        "llm_completion_rate": _ratio(llm_completed, attempted),
        "structure_valid_rate": _ratio(structure_valid, attempted),
        "required_type_micro_recall": _ratio(required_found, required_total),
        "requirements_satisfied_rate": _ratio(requirements_satisfied, attempted),
        "severe_erc_free_rate": _ratio(severe_erc_free, attempted),
        "end_to_end_pass_rate": _ratio(end_to_end, attempted),
        "mean_elapsed_ms": round(sum(elapsed) / attempted, 3) if attempted else None,
        "total_elapsed_ms": round(sum(elapsed), 3),
    }


def write_results(output_dir: str, dataset: dict, records: list,
                  run_metadata: dict) -> Dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    dataset_material = {"metadata": dataset["metadata"], "cases": dataset["cases"]}
    result = {
        "schema_version": "1.0",
        "run_metadata": run_metadata,
        "dataset_metadata": dict(dataset["metadata"],
                                 fingerprint=_fingerprint(dataset_material)),
        "summary": summarize_records(records),
        "records": records,
        "limitations": [
            "固定内部构造需求集，不是公开数据集或独立生产分布样本",
            "模型别名可能由服务端滚动更新；结果只代表本次运行",
            "端到端通过只覆盖目录类型、CircuitJSON、必要元件和现有 ERC",
            "结果不涵盖器件手册、SI/PI、EMC、热和制造签核",
        ],
    }
    json_path = os.path.join(output_dir, "ai_requirements_results.json")
    csv_path = os.path.join(output_dir, "ai_requirements_summary.csv")
    with open(json_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    columns = [
        "case_id", "generation_path", "llm_completed", "fallback_reason_type", "structure_valid",
        "required_type_recall", "requirements_satisfied", "severe_erc_free",
        "end_to_end_pass", "fatal", "major", "minor", "warning",
        "elapsed_ms", "missing_required_types", "forbidden_types_found", "error",
    ]
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in records:
            counts = row.get("erc_counts") or {}
            writer.writerow({
                "case_id": row.get("case_id"),
                "generation_path": row.get("generation_path"),
                "llm_completed": row.get("llm_completed"),
                "fallback_reason_type": row.get("fallback_reason_type") or "",
                "structure_valid": row.get("structure_valid"),
                "required_type_recall": row.get("required_type_recall"),
                "requirements_satisfied": row.get("requirements_satisfied"),
                "severe_erc_free": row.get("severe_erc_free"),
                "end_to_end_pass": row.get("end_to_end_pass"),
                "fatal": counts.get("fatal", 0),
                "major": counts.get("major", 0),
                "minor": counts.get("minor", 0),
                "warning": counts.get("warning", 0),
                "elapsed_ms": row.get("elapsed_ms"),
                "missing_required_types": json.dumps(
                    row.get("missing_required_types") or [], ensure_ascii=False),
                "forbidden_types_found": json.dumps(
                    row.get("forbidden_types_found") or [], ensure_ascii=False),
                "error": row.get("error") or "",
            })
    return {"json": json_path, "csv": csv_path}


def run_online(dataset: dict) -> tuple[list, dict]:
    """运行一次在线固定集；调用方负责明确选择 --online。"""
    from ai2kicad import understand

    client = DeepSeekClient()
    model_alias = client.model
    api_configured = bool(client.api_key)
    client.close()
    if not api_configured:
        raise RuntimeError("未配置在线模型 API Key，不能运行 --online 评测")

    records = []
    started_at = _utc_now()
    for index, case in enumerate(dataset["cases"], 1):
        print(f"[{index}/{len(dataset['cases'])}] {case['case_id']}", flush=True)
        started = time.perf_counter()
        try:
            circuit, meta = understand(case["prompt"], offline=False, verbose=False)
            elapsed_ms = (time.perf_counter() - started) * 1000
            records.append(evaluate_case(case, circuit, meta, elapsed_ms))
        except Exception as exc:  # noqa: BLE001 - 失败必须落入证据而非中止整批
            elapsed_ms = (time.perf_counter() - started) * 1000
            records.append(evaluate_case(
                case, None, {"path": "error"}, elapsed_ms,
                error=f"{type(exc).__name__}: {exc}"))
    metadata = {
        "run_started_at": started_at,
        "run_finished_at": _utc_now(),
        "model_alias": model_alias,
        "temperature_policy": "initial_0_retry_0.3",
        "requested_online": True,
        "api_key_configured": True,
        "repeat_count_per_case": 1,
    }
    return records, metadata


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=DEFAULT_CASES_PATH)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--online", action="store_true",
                        help="明确允许调用已配置在线模型；不提供则只校验数据集")
    args = parser.parse_args(argv)

    dataset = load_cases(args.cases)
    if not args.online:
        print(f"数据集校验通过：{len(dataset['cases'])} 条；未调用在线模型")
        return 0
    records, metadata = run_online(dataset)
    paths = write_results(args.output_dir, dataset, records, metadata)
    summary = summarize_records(records)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"结果：{paths['json']}")
    print(f"摘要：{paths['csv']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
