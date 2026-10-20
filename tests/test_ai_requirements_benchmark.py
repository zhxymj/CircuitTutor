# -*- coding: utf-8 -*-
"""固定中文需求集的 AI 候选生成评测合同。"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.ai_requirements_benchmark import (  # noqa: E402
    evaluate_case,
    load_cases,
    summarize_records,
    write_results,
)


GOOD_CIRCUIT = {
    "version": 1,
    "parts": [
        {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
        {"type": "resistor", "id": "R1", "attrs": {"value": "330Ω"}},
        {"type": "led", "id": "LED1", "attrs": {"value": "red"}},
    ],
    "connections": [
        ["BAT1:+", "R1:1", "red", []],
        ["R1:2", "LED1:+", "red", []],
        ["LED1:-", "BAT1:-", "black", []],
    ],
}


def _case():
    return {
        "case_id": "req_001",
        "prompt": "5V供电的红色LED指示灯，使用330Ω限流电阻",
        "required_types": ["battery", "resistor", "led"],
        "forbidden_types": ["dc_motor"],
    }


def test_llm_success_is_scored_separately_from_template_fallback():
    llm_record = evaluate_case(_case(), GOOD_CIRCUIT,
                               {"path": "llm", "types": ["battery", "resistor", "led"]},
                               elapsed_ms=12.5)
    fallback_record = evaluate_case(_case(), GOOD_CIRCUIT,
                                    {"path": "template", "types": [],
                                     "note": "本地模板兜底（led_limit 模板；原因: ValueError: 结构校验失败）"},
                                    elapsed_ms=3.0)

    assert llm_record["generation_path"] == "llm"
    assert llm_record["llm_completed"] is True
    assert llm_record["structure_valid"] is True
    assert llm_record["required_type_recall"] == 1.0
    assert llm_record["forbidden_types_found"] == []
    assert llm_record["end_to_end_pass"] is True

    assert fallback_record["generation_path"] == "template"
    assert fallback_record["llm_completed"] is False
    assert fallback_record["fallback_reason_type"] == "ValueError"
    assert fallback_record["structure_valid"] is True
    assert fallback_record["end_to_end_pass"] is False

    summary = summarize_records([llm_record, fallback_record])
    assert summary["attempted"] == 2
    assert summary["llm_completed"] == 1
    assert summary["template_fallbacks"] == 1
    assert summary["llm_completion_rate"] == 0.5
    assert summary["end_to_end_pass_rate"] == 0.5


def test_missing_and_forbidden_types_are_machine_readable():
    bad = {
        "version": 1,
        "parts": [
            {"type": "battery", "id": "BAT1"},
            {"type": "dc_motor", "id": "M1"},
        ],
        "connections": [["BAT1:+", "M1:+"], ["M1:-", "BAT1:-"]],
    }
    record = evaluate_case(_case(), bad, {"path": "llm", "types": []}, elapsed_ms=1)
    assert record["missing_required_types"] == ["led", "resistor"]
    assert record["forbidden_types_found"] == ["dc_motor"]
    assert record["required_type_recall"] == 0.333333
    assert record["end_to_end_pass"] is False


def test_cases_and_results_have_traceable_schema():
    payload = {
        "metadata": {
            "dataset_kind": "fixed_constructed_requirements",
            "scenario": "通信设备低压 MCU 与 I2C 传感接口子板",
        },
        "cases": [_case()],
    }
    with tempfile.TemporaryDirectory() as tmp:
        case_path = os.path.join(tmp, "cases.json")
        with open(case_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        loaded = load_cases(case_path)
        assert loaded["metadata"]["dataset_kind"] == "fixed_constructed_requirements"
        assert loaded["cases"][0]["case_id"] == "req_001"

        record = evaluate_case(_case(), GOOD_CIRCUIT, {"path": "llm", "types": []}, 5)
        paths = write_results(tmp, loaded, [record], {
            "model_alias": "test-model",
            "temperature_policy": "initial_0_retry_0.3",
            "run_started_at": "2026-09-19T00:00:00+08:00",
        })
        assert set(paths) == {"json", "csv"}
        with open(paths["json"], encoding="utf-8") as fh:
            result = json.load(fh)
        assert result["run_metadata"]["model_alias"] == "test-model"
        assert result["dataset_metadata"]["dataset_kind"] == "fixed_constructed_requirements"
        assert result["summary"]["attempted"] == 1


def test_cli_online_path_does_not_fall_back_on_component_extraction_call_signature():
    """捕获 understand() 重复传 on_progress 导致在线路径静默回退的回归。"""
    import ai2kicad

    class FakeClient:
        def close(self):
            return None

    original_configured = ai2kicad.config.api_key_configured
    original_client = ai2kicad.llm.DeepSeekClient
    original_generate = ai2kicad.llm.generate_circuit_json
    try:
        ai2kicad.config.api_key_configured = lambda: True
        ai2kicad.llm.DeepSeekClient = FakeClient
        ai2kicad.llm.generate_circuit_json = lambda *args, **kwargs: GOOD_CIRCUIT
        circuit, meta = ai2kicad.understand(
            "5V电池、330Ω电阻和红色LED串联指示灯",
            offline=False,
            verbose=False,
        )
        assert circuit == GOOD_CIRCUIT
        assert meta["path"] == "llm"
    finally:
        ai2kicad.config.api_key_configured = original_configured
        ai2kicad.llm.DeepSeekClient = original_client
        ai2kicad.llm.generate_circuit_json = original_generate


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
