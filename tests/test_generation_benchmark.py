# -*- coding: utf-8 -*-
"""模板生成/导出有效性评测回归；单元测试不启动外部 KiCad。"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.generation_benchmark import (  # noqa: E402
    evaluate_templates,
    write_generation_evidence,
)


def test_template_evaluation_scope_and_negative_fixture():
    result = evaluate_templates(run_official=False)
    summary = result["summary"]
    assert summary["template_count"] == 40
    assert summary["production_candidate_count"] == 39
    assert summary["structural_valid_count"] == 39
    assert summary["production_structural_valid_rate"] == 1.0
    assert summary["kicad_generation_success_count"] == 40
    assert summary["kicad_generation_success_rate"] == 1.0
    assert summary["known_component_type_rate"] == 1.0
    assert summary["official_validation"]["status"] == "skip"
    negative = next(row for row in result["records"]
                    if row["template_id"] == "led_no_limit")
    assert negative["fixture_role"] == "intentional_negative"
    assert negative["structural_valid"] is False
    assert any("重复" in error for error in negative["structure_errors"])


def test_records_distinguish_pass_fail_skip():
    result = evaluate_templates(run_official=False)
    assert all(row["export_status"] in {"pass", "fail"}
               for row in result["records"])
    assert all(row["official_erc_status"] == "skip"
               for row in result["records"])
    assert all(row["official_netlist_status"] == "skip"
               for row in result["records"])


def test_evidence_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        manifest = write_generation_evidence(tmp, run_official=False)
        assert set(manifest) == {
            "generation_results.json",
            "generation_summary.csv",
            "ablation_status.json",
        }
        with open(os.path.join(tmp, "generation_results.json"), encoding="utf-8") as handle:
            result = json.load(handle)
        with open(os.path.join(tmp, "ablation_status.json"), encoding="utf-8") as handle:
            ablation = json.load(handle)
        assert result["summary"]["template_count"] == 40
        methods = {row["method"]: row["status"] for row in ablation["methods"]}
        assert methods["仅AI审查"] == "skip"
        assert methods["确定性规则"] == "measured_elsewhere"
        assert ablation["skip_is_pass"] is False


def test_ablation_reports_online_candidate_gate_without_relabeling_it_as_ai_review():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "ai_requirements_results.json"), "w", encoding="utf-8") as handle:
            json.dump({
                "run_metadata": {"model_alias": "test-model"},
                "summary": {"attempted": 8, "llm_completed": 6,
                            "end_to_end_pass": 5, "end_to_end_pass_rate": 0.625},
            }, handle)
        write_generation_evidence(tmp, run_official=False)
        with open(os.path.join(tmp, "ablation_status.json"), encoding="utf-8") as handle:
            ablation = json.load(handle)
        methods = {row["method"]: row for row in ablation["methods"]}
        gate = methods["在线AI候选+确定性规则门禁"]
        assert gate["status"] == "measured"
        assert gate["evidence"] == "ai_requirements_results.json"
        assert gate["metrics"]["end_to_end_pass"] == 5
        assert methods["仅AI审查"]["status"] == "skip"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"[PASS] {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {test.__name__}: {exc}")
    print(f"结果：{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
