"""Validate and audit the internal requirement corpus without model calls.

Run from project root: python -m evaluation.requirements_v2 --output PATH
The 30 groups are authored request scenarios, not external reference circuits.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from core import catalog, clarify, llm

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "data" / "requirements_v2" / "requirements.json"
CATEGORY_DISPOSITIONS = {"explicit":"plan", "ambiguous":"clarify",
                         "unsupported_model":"reject", "negative":"reject"}


def fingerprint(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_dataset(dataset: dict) -> dict:
    if not isinstance(dataset, dict) or not isinstance(dataset.get("metadata"), dict):
        raise ValueError("dataset must contain metadata")
    metadata = dataset["metadata"]
    if metadata.get("dataset_kind") != "internal_original_requirements":
        raise ValueError("this corpus must be labelled internal_original_requirements")
    if metadata.get("external_examples_imported") != 0 or metadata.get("reference_circuit_count") != 0:
        raise ValueError("internal text tasks cannot count as external reference circuits")
    if metadata.get("T03_external_circuit_goal_met") is not False:
        raise ValueError("T03 external circuit goal must remain unmet for this corpus")
    cases, tasks = dataset.get("cases"), dataset.get("tasks")
    if not isinstance(cases, list) or not isinstance(tasks, list):
        raise ValueError("cases and tasks must be arrays")
    case_index = {}
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("each case must be an object")
        cid = case.get("case_id")
        if not isinstance(cid, str) or not cid or cid in case_index:
            raise ValueError(f"missing or duplicate case_id: {cid!r}")
        if case.get("split") not in ("dev", "holdout"):
            raise ValueError(f"{cid}: invalid split")
        if case.get("source") != "internal_original_requirement" or case.get("has_reference_circuit") is not False:
            raise ValueError(f"{cid}: internal original source label required")
        if not isinstance(case.get("requirements"), list) or not case["requirements"]:
            raise ValueError(f"{cid}: requirements missing")
        if not isinstance(case.get("intended_parts"), list) or any(t not in catalog.CATALOG for t in case["intended_parts"]):
            raise ValueError(f"{cid}: invalid intended parts")
        if not isinstance(case.get("related_family"), str) or not case["related_family"]:
            raise ValueError(f"{cid}: related_family missing")
        case_index[cid] = case
    seen_tasks, seen_prompts, cases_used = set(), set(), set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("each task must be an object")
        tid, cid = task.get("task_id"), task.get("case_id")
        if not isinstance(tid, str) or not tid or tid in seen_tasks:
            raise ValueError(f"missing or duplicate task_id: {tid!r}")
        seen_tasks.add(tid)
        if cid not in case_index or task.get("split") != case_index[cid]["split"]:
            raise ValueError(f"{tid}: task/case split mismatch or unknown case")
        cases_used.add(cid)
        prompt = task.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"{tid}: prompt missing")
        prompt_key = "".join(prompt.split()).casefold()
        if prompt_key in seen_prompts:
            raise ValueError(f"{tid}: repeated normalized prompt")
        seen_prompts.add(prompt_key)
        category = task.get("category")
        if category not in CATEGORY_DISPOSITIONS or task.get("expected_disposition") != CATEGORY_DISPOSITIONS[category]:
            raise ValueError(f"{tid}: category/disposition mismatch")
        for key in ("required_types", "forbidden_types"):
            values = task.get(key)
            if not isinstance(values, list) or any(t not in catalog.CATALOG for t in values):
                raise ValueError(f"{tid}: invalid catalog type in {key}")
        if set(task["required_types"]) & set(task["forbidden_types"]):
            raise ValueError(f"{tid}: contradictory required/forbidden types")
        for key in ("missing_fields", "unsupported_models", "negative_rule_ids"):
            values = task.get(key)
            if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                raise ValueError(f"{tid}: invalid {key}")
        annotation_key = {"ambiguous":"missing_fields", "unsupported_model":"unsupported_models",
                          "negative":"negative_rule_ids"}.get(category)
        if annotation_key and not task[annotation_key]:
            raise ValueError(f"{tid}: {annotation_key} annotation required")
        for key in ("missing_fields", "unsupported_models", "negative_rule_ids"):
            if key != annotation_key and task[key]:
                raise ValueError(f"{tid}: unrelated label {key}")
    if cases_used != set(case_index):
        raise ValueError("every case must contain at least one task")
    split_counts = Counter(task["split"] for task in tasks)
    if len(cases) != 30 or len(tasks) != 80 or split_counts["holdout"] < 20:
        raise ValueError("internal corpus target is 30 case groups/80 tasks/at least 20 holdout tasks")
    for key, observed in (("case_group_count",len(cases)), ("task_count",len(tasks)),
                          ("holdout_task_count",split_counts["holdout"])):
        if metadata.get(key) != observed:
            raise ValueError(f"metadata {key} does not match actual count")
    family_dev = {case["related_family"] for case in cases if case["split"] == "dev"}
    family_holdout = {case["related_family"] for case in cases if case["split"] == "holdout"}
    return {"case_groups":len(cases), "tasks":len(tasks), "split_tasks":dict(split_counts),
            "categories":dict(Counter(task["category"] for task in tasks)),
            "split_categories":{split:dict(Counter(task["category"] for task in tasks if task["split"] == split))
                                for split in ("dev", "holdout")},
            "related_families_in_both_splits":sorted(family_dev & family_holdout),
            "external_reference_circuits":0, "T03_complete":False,
            "internal_count_target_met":True}


def load_dataset(path: str | Path = DEFAULT_DATASET) -> dict:
    dataset = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_dataset(dataset)
    return dataset


def evaluate_task(task: dict) -> dict:
    started = time.perf_counter()
    matched = catalog.match_components(task["prompt"])
    brief = llm.build_generation_brief(task["prompt"], catalog.build_matched_lib(matched))
    clarification = clarify.rule_analyze(task["prompt"])
    unknown = set(brief["unsupported_part_numbers"])
    blockers = brief["blockers"]
    detected_rules = {item["rule_id"] for item in blockers}
    expected_rules = set(task["negative_rule_ids"])
    # This is only the deterministic preflight dispatch, not an accepted circuit.
    observed = "reject" if unknown or blockers else ("clarify" if clarification["status"] == "clarify" else "plan")
    selected = set(brief["required_types"])
    missing = sorted(set(task["required_types"]) - selected)
    forbidden = sorted(set(task["forbidden_types"]) & selected)
    return {"task_id":task["task_id"], "case_id":task["case_id"], "split":task["split"],
            "category":task["category"], "expected_disposition":task["expected_disposition"],
            "observed_preflight_disposition":observed,
            "disposition_match":observed == task["expected_disposition"],
            "required_types":task["required_types"], "selected_types":sorted(selected),
            "missing_required_types":missing, "forbidden_types_found":forbidden,
            "catalog_selection_correct":not missing and not forbidden,
            "expected_unsupported_models":task["unsupported_models"],
            "observed_unsupported_models":sorted(unknown),
            "unsupported_detection_correct":unknown == set(task["unsupported_models"]),
            "expected_missing_fields":task["missing_fields"],
            "clarify_questions":clarification["questions"],
            "expected_negative_rule_ids":task["negative_rule_ids"],
            "requirement_blockers":blockers,
            "observed_negative_rule_ids":sorted(detected_rules),
            "negative_rule_detection":expected_rules.issubset(detected_rules) if expected_rules else None,
            "unexpected_blockers_on_nonnegative_task":bool(blockers) and not expected_rules,
            "candidate_evidence":brief["explicit_candidates"],
            "elapsed_ms":round((time.perf_counter() - started) * 1000, 3),
            "generation_path":"not_run_offline", "structure_valid":None,
            "erc_passed":None, "template_fallback":None, "model_calls":0}


def summarize(records: list[dict]) -> dict:
    return {"tasks":len(records),
            "preflight_disposition_matches":sum(row["disposition_match"] for row in records),
            "catalog_selection_correct":sum(row["catalog_selection_correct"] for row in records),
            "unsupported_detection_correct":sum(row["unsupported_detection_correct"] for row in records),
            "negative_tasks":sum(bool(row["expected_negative_rule_ids"]) for row in records),
            "negative_text_rule_matches":sum(row["negative_rule_detection"] is True for row in records),
            "unexpected_blockers_on_nonnegative_tasks":sum(row["unexpected_blockers_on_nonnegative_task"] for row in records),
            "required_type_total":sum(len(row["required_types"]) for row in records),
            "required_type_found":sum(len(row["required_types"]) - len(row["missing_required_types"]) for row in records),
            "total_elapsed_ms":round(sum(row["elapsed_ms"] for row in records), 3)}


def run_offline(dataset: dict) -> dict:
    acceptance = validate_dataset(dataset)
    records = [evaluate_task(task) for task in dataset["tasks"]]
    return {"schema_version":"2.0", "dataset_sha256":fingerprint(dataset),
            "run_metadata":{"mode":"deterministic_offline_preflight", "model_calls":0,
                            "created_at":datetime.now(timezone.utc).isoformat(),
                            "core_sha256":{name:hashlib.sha256((ROOT / "core" / name).read_bytes()).hexdigest()
                                           for name in ("catalog.py", "clarify.py", "llm.py", "intent_constraints.py")}},
            "dataset_metadata":dataset["metadata"], "acceptance":acceptance,
            "summary":summarize(records),
            "by_split":{split:summarize([row for row in records if row["split"] == split])
                        for split in ("dev", "holdout")},
            "by_category":{category:summarize([row for row in records if row["category"] == category])
                           for category in CATEGORY_DISPOSITIONS},
            "records":records,
            "limitations":["仅测确定性预规划；不评价模型、网表、ERC、PCB或制造结果",
                           "负例规则标签是需求层标注，不等同现有 ERC 规则 ID",
                           "文本阻断只覆盖明确措辞；未解析完整电路，未核对电流、容限、具体 MPN 或元件参数",
                           "规则按本内部需求集开发；负例命中数不能称为独立泛化、ERC 或物理验证效果",
                           "内部案例分区不能称为外部独立盲测；T03 仍未完成"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = run_offline(load_dataset(args.dataset))
    if args.output:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"acceptance":report["acceptance"], "summary":report["summary"],
                      "by_category":report["by_category"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
