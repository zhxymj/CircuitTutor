# -*- coding: utf-8 -*-
"""内置模板的结构、目录覆盖、KiCad 生成与官方复验评测。

本评测的样本是项目自带模板，不是“自然语言模型准确率”数据。它回答的是：
固定 CircuitJSON 能否通过结构检查、是否只使用已知器件、是否能生成 KiCad
文件，以及本机有 kicad-cli 时官方 ERC/网表复验是否成功。
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

from core.catalog import CATALOG, build_erc_kb
from core.erc import CircuitGraph, run_erc
from core.llm import validate_circuit_json
from kicad import export as kicad_export


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_PATH = os.path.join(ROOT, "data", "circuit_templates.json")
INTENTIONAL_NEGATIVE_FIXTURES = {"led_no_limit"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 6) if denominator else None


def _load_templates() -> Dict[str, dict]:
    with open(TEMPLATE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _expected_pin_nets(circuit: dict) -> Dict[str, str]:
    """独立按连接构造期望网络名，与导出器测试使用相同命名约定。"""
    parent: Dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        parent[find(left)] = find(right)

    part_types = {part.get("id"): part.get("type")
                  for part in circuit.get("parts", []) if isinstance(part, dict)}
    for connection in circuit.get("connections", []):
        refs = [item for item in connection[:2]
                if isinstance(item, str) and ":" in item]
        for ref in refs:
            find(ref)
        for ref in refs[1:]:
            union(refs[0], ref)
    groups: Dict[str, list] = {}
    for ref in parent:
        groups.setdefault(find(ref), []).append(ref)
    ordered = sorted(groups, key=lambda root: min(groups[root]))
    output = {}
    for root in ordered:
        members = sorted(groups[root])
        name = f"N{ordered.index(root) + 1}"
        for anchor, label in (("gnd", "GND"), ("vcc", "VCC")):
            if any(part_types.get(ref.split(":", 1)[0]) == anchor for ref in members):
                name = label
                break
        for ref in members:
            output[ref] = name
    return output


def _compare_official_netlist(circuit: dict,
                              observed: Dict[str, set]) -> tuple[bool, list]:
    part_types = {part.get("id"): part.get("type")
                  for part in circuit.get("parts", []) if isinstance(part, dict)}
    wanted: Dict[str, set] = {}
    for ref, net in _expected_pin_nets(circuit).items():
        ref_id, pin_name = ref.split(":", 1)
        mapping = kicad_export.TYPE_MAP.get(part_types.get(ref_id), (None, {}, None))[1]
        if pin_name not in mapping:
            continue
        wanted.setdefault(net, set()).add((ref_id, mapping[pin_name]))
    differences = []
    for net in sorted(set(wanted) | set(observed)):
        if net.startswith("unconnected-"):
            continue
        got = {node for node in observed.get(net, set())
               if not node[0].startswith("#")}
        expected = wanted.get(net, set())
        if got != expected:
            differences.append({
                "net": net,
                "expected": sorted([list(node) for node in expected]),
                "observed": sorted([list(node) for node in got]),
            })
    return not differences, differences


def evaluate_templates(*, run_official: bool = True) -> dict:
    templates = _load_templates()
    kb = build_erc_kb()
    cli = kicad_export.find_kicad_cli() if run_official else None
    cli_version = kicad_export.kicad_cli_version() if cli else None
    records = []
    known_parts = total_parts = 0
    total_start = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix="circuittutor-generation-") as temp_dir:
        for template_id, circuit in templates.items():
            started = time.perf_counter()
            role = ("intentional_negative" if template_id in INTENTIONAL_NEGATIVE_FIXTURES
                    else "production_candidate")
            structure_errors = validate_circuit_json(circuit, CATALOG)
            part_count = len(circuit.get("parts", []))
            known_count = sum(
                isinstance(part, dict) and part.get("type") in CATALOG
                for part in circuit.get("parts", [])
            )
            total_parts += part_count
            known_parts += known_count
            internal_report = run_erc(circuit, kb)
            export_status = "fail"
            export_error = None
            schematic_path = None
            try:
                case_dir = os.path.join(temp_dir, template_id)
                schematic_path, warnings = kicad_export.export_to_dir(
                    circuit, template_id, case_dir, kb=kb,
                    erc_report=internal_report)
                text = schematic_path.read_text(encoding="utf-8")
                if not text.startswith("(kicad_sch"):
                    raise ValueError("输出文件缺少 kicad_sch 根节点")
                export_status = "pass"
                export_warnings = list(warnings)
            except Exception as exc:  # noqa: BLE001
                export_error = f"{type(exc).__name__}: {exc}"
                export_warnings = []

            official_erc_status = "skip"
            official_erc_total = None
            official_erc_types = None
            official_netlist_status = "skip"
            official_netlist_differences = []
            if cli and export_status == "pass" and schematic_path is not None:
                official_erc = kicad_export.run_official_erc(schematic_path)
                if official_erc is None:
                    official_erc_status = "fail"
                else:
                    official_erc_total = official_erc["total"]
                    official_erc_types = official_erc["by_type"]
                    official_erc_status = "pass" if official_erc_total == 0 else "fail"
                observed_nets = kicad_export.export_official_netlist(schematic_path)
                if observed_nets is None:
                    official_netlist_status = "fail"
                else:
                    exact, official_netlist_differences = _compare_official_netlist(
                        circuit, observed_nets)
                    official_netlist_status = "pass" if exact else "fail"

            counts = internal_report.counts()
            records.append({
                "template_id": template_id,
                "title": circuit.get("title", template_id),
                "fixture_role": role,
                "part_count": part_count,
                "known_component_count": known_count,
                "unknown_component_count": part_count - known_count,
                "structural_valid": not structure_errors,
                "structure_errors": structure_errors,
                "internal_erc_passed": internal_report.passed,
                "internal_erc_counts": counts,
                "export_status": export_status,
                "export_error": export_error,
                "export_warnings": export_warnings,
                "official_erc_status": official_erc_status,
                "official_erc_violations": official_erc_total,
                "official_erc_types": official_erc_types,
                "official_netlist_status": official_netlist_status,
                "official_netlist_differences": official_netlist_differences,
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            })

    production = [row for row in records if row["fixture_role"] == "production_candidate"]
    structural_valid = sum(row["structural_valid"] for row in records)
    generated = sum(row["export_status"] == "pass" for row in records)
    official_attempted = sum(row["official_erc_status"] != "skip" for row in records)
    official_erc_passed = sum(row["official_erc_status"] == "pass" for row in records)
    netlist_attempted = sum(row["official_netlist_status"] != "skip" for row in records)
    netlist_passed = sum(row["official_netlist_status"] == "pass" for row in records)
    official_failures = ((official_attempted - official_erc_passed)
                         + (netlist_attempted - netlist_passed))
    official_status = (
        "skip" if not cli else "pass" if official_failures == 0 else "fail"
    )
    summary = {
        "template_count": len(records),
        "intentional_negative_count": len(records) - len(production),
        "production_candidate_count": len(production),
        "structural_valid_count": structural_valid,
        "structural_valid_rate_all": _ratio(structural_valid, len(records)),
        "production_structural_valid_count": sum(row["structural_valid"] for row in production),
        "production_structural_valid_rate": _ratio(
            sum(row["structural_valid"] for row in production), len(production)),
        "known_component_count": known_parts,
        "component_instance_count": total_parts,
        "known_component_type_rate": _ratio(known_parts, total_parts),
        "kicad_generation_success_count": generated,
        "kicad_generation_success_rate": _ratio(generated, len(records)),
        "official_validation": {
            "status": official_status,
            "cli_detected": bool(cli),
            "cli_path": cli,
            "cli_version": cli_version,
            "erc_attempted": official_attempted,
            "erc_passed": official_erc_passed,
            "erc_pass_rate": _ratio(official_erc_passed, official_attempted),
            "netlist_attempted": netlist_attempted,
            "netlist_passed": netlist_passed,
            "netlist_exact_rate": _ratio(netlist_passed, netlist_attempted),
        },
        "total_elapsed_ms": round((time.perf_counter() - total_start) * 1000.0, 3),
    }
    return {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "benchmark_kind": "built_in_template_export_validation",
        "summary": summary,
        "records": records,
        "metric_boundaries": {
            "known_component_type_rate": (
                "模板中的元件实例能否映射到当前目录，不是自然语言元件识别准确率。"
            ),
            "template_source_share": (
                "本评测故意只输入内置模板，因此不计算或宣称在线生成的模板降级比例。"
            ),
            "official_validation": (
                "仅在 kicad-cli 实际可用且返回报告时计为 pass；skip 不计 pass。"
            ),
        },
        "limitations": [
            "40 个样本来自项目自带模板，不能代表 SchGen、PCBench 或生产分布。",
            "led_no_limit 是故意保留的负例，整体结构合法率必须与 39 个生产候选子集分开报告。",
            "未建立独立标注的自然语言元件识别测试集，因此不报告该项准确率。",
        ],
    }


def _ablation_status(generation_result: dict, ai_result: Optional[dict] = None) -> dict:
    official = generation_result["summary"]["official_validation"]
    kicad_status = "measured" if official["status"] in {"pass", "fail"} else "skip"
    ai_summary = (ai_result or {}).get("summary") or {}
    ai_gate_measured = bool(ai_summary.get("attempted"))
    return {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "skip_is_pass": False,
        "methods": [
            {
                "method": "KiCad原生ERC",
                "status": kicad_status,
                "evidence": "generation_results.json.summary.official_validation",
                "note": "测量导出文件的官方 ERC 与网表；不等同于语义缺陷分类器。",
            },
            {
                "method": "确定性规则",
                "status": "measured_elsewhere",
                "evidence": "compliance_results.json",
                "note": "固定缺陷注入集上的逐规则 TP/FP/FN/TN。",
            },
            {
                "method": "在线AI候选+确定性规则门禁",
                "status": "measured" if ai_gate_measured else "skip",
                "evidence": "ai_requirements_results.json" if ai_gate_measured else None,
                "metrics": ({
                    "attempted": ai_summary.get("attempted"),
                    "llm_completed": ai_summary.get("llm_completed"),
                    "end_to_end_pass": ai_summary.get("end_to_end_pass"),
                    "end_to_end_pass_rate": ai_summary.get("end_to_end_pass_rate"),
                } if ai_gate_measured else None),
                "note": ("固定内部构造需求集的一次在线运行；模板回退不计为AI完成，"
                         "不等同于仅AI审查或泛化准确率。"),
            },
            {
                "method": "仅AI审查",
                "status": "skip",
                "evidence": None,
                "note": "本轮在线实验评估候选生成与规则门禁，没有设计独立的仅AI审查基线。",
            },
            {
                "method": "确定性规则+AI解释",
                "status": "skip",
                "evidence": None,
                "note": "本轮没有运行以AI解释质量为变量的独立对照实验。",
            },
            {
                "method": "确定性规则+AI修复+KiCad复验",
                "status": "skip",
                "evidence": None,
                "note": "当前只实测了确定性 autofix；LLM 修复组合未运行，不能代称该消融项。",
            },
        ],
        "partial_evidence": {
            "deterministic_autofix": "compliance_results.json.repair_metrics",
            "deterministic_pipeline_snapshot": "tests/golden/benchmark_snapshot.json",
            "snapshot_warning": "deterministic-fake 只用于代码回归，不是模型能力指标。",
        },
    }


def _write_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _write_summary_csv(path: str, summary: dict) -> None:
    official = summary["official_validation"]
    rows = [
        ("template_count", summary["template_count"]),
        ("production_candidate_count", summary["production_candidate_count"]),
        ("structural_valid_rate_all", summary["structural_valid_rate_all"]),
        ("production_structural_valid_rate", summary["production_structural_valid_rate"]),
        ("known_component_type_rate", summary["known_component_type_rate"]),
        ("kicad_generation_success_rate", summary["kicad_generation_success_rate"]),
        ("official_erc_pass_rate", official["erc_pass_rate"]),
        ("official_netlist_exact_rate", official["netlist_exact_rate"]),
        ("official_validation_status", official["status"]),
        ("total_elapsed_ms", summary["total_elapsed_ms"]),
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        writer.writerows(rows)


def write_generation_evidence(output_dir: str, *,
                              run_official: bool = True) -> Dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    result = evaluate_templates(run_official=run_official)
    ai_result = None
    ai_result_path = os.path.join(output_dir, "ai_requirements_results.json")
    if os.path.isfile(ai_result_path):
        try:
            with open(ai_result_path, encoding="utf-8") as handle:
                candidate = json.load(handle)
            if isinstance(candidate, dict):
                ai_result = candidate
        except (OSError, json.JSONDecodeError):
            ai_result = None
    paths = {
        "generation_results.json": os.path.join(output_dir, "generation_results.json"),
        "generation_summary.csv": os.path.join(output_dir, "generation_summary.csv"),
        "ablation_status.json": os.path.join(output_dir, "ablation_status.json"),
    }
    _write_json(paths["generation_results.json"], result)
    _write_summary_csv(paths["generation_summary.csv"], result["summary"])
    _write_json(paths["ablation_status.json"], _ablation_status(result, ai_result))
    return paths


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(argv or [])
    output_dir = os.path.abspath(argv[0]) if argv else os.path.join(ROOT, "evidence")
    run_official = "--no-official" not in argv
    paths = write_generation_evidence(output_dir, run_official=run_official)
    with open(paths["generation_results.json"], encoding="utf-8") as handle:
        summary = json.load(handle)["summary"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
