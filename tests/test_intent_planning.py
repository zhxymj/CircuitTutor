"""目标场景的离线规划回归；运行：python tests/test_intent_planning.py。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import catalog, clarify, llm
from evaluation import ai_requirements_benchmark, intent_benchmark


def test_i2c_pin_names_and_candidate_scope():
    prompt = "3.3V ATmega328P 与 BMP280 的 I2C 接口，SDA/SCL 各加 4.7k 上拉"
    matched = catalog.match_components(prompt)
    assert {"atmega328", "bmp280"}.issubset(matched)
    assert "i2c_header" not in matched, "总线描述不能凭空生成排针"
    brief = llm.build_generation_brief(prompt, catalog.build_matched_lib(matched))
    assert "battery" not in brief["suggested_types"]
    assert brief["signal_nets"]["SDA"] == [
        {"type": "atmega328", "pin": "PC4"}, {"type": "bmp280", "pin": "SDA"}]
    assert brief["signal_nets"]["SCL"][0]["pin"] == "PC5"
    assert {"PC4", "PC5"}.issubset(catalog.CATALOG["atmega328"]["pins"])
    assert {"GPIO21", "GPIO22"}.issubset(catalog.CATALOG["esp32"]["pins"])
    assert "ldo_3v3" in catalog.match_components("用 3.3V LDO 稳压")
    assert "led" not in catalog.match_components("只接 OLED 显示屏")
    assert "atmega328" not in catalog.match_components("给单片机供电"), \
        "未指定型号的单片机不能默认成 ATmega328"
    assert "usb_c" in catalog.match_components("Type-C 5V 输入")
    assert "led" not in catalog.match_components("OLED 显示，不要添加独立 LED")
    assert {"atmega328", "esp32"}.issubset(catalog.match_components("ATmega328P 与 ESP32 共用 I2C"))
    assert catalog.unsupported_part_numbers("MPU6050 的 AD0 接地") == []


def test_named_unknown_rejected_before_model_call():
    class NeverCall:
        def chat(self, *args, **kwargs):
            raise AssertionError("目录外型号不应触发在线模型")

    prompt = "STM32F103 接 BME680 做 3.3V I2C"
    assert catalog.unsupported_part_numbers(prompt) == ["STM32F103", "BME680"]
    try:
        llm.generate_circuit_json(prompt, {}, NeverCall())
    except ValueError as exc:
        assert "目录外" in str(exc) and "BME680" in str(exc)
    else:
        raise AssertionError("未拒绝目录外器件")


def test_source_metadata_does_not_become_a_part_number():
    commit = "abcdef0123456789abcdef0123456789abcdef01"
    prompt = "参考 sparkfun/Qwiic_BMP581_Breakout 固定提交 " + commit + " 的接口，实际使用 BME680。"
    assert catalog.unsupported_part_numbers(prompt) == ["BME680"]
    assert catalog.unsupported_part_numbers("器件型号 " + commit) == [commit]
    assert catalog.unsupported_part_numbers("commit: " + commit + "，使用 STM32F103") == ["STM32F103"]


def test_clarification_after_answer():
    prompt = "用单片机连接一个 I2C 传感器"
    first = clarify.rule_analyze(prompt)
    assert first["status"] == "clarify"
    assert len(first["questions"]) == 3
    answer = clarify.merge_clarifications(prompt, [
        {"question": first["questions"][0]["question"], "answer": "ESP32-WROOM"},
        {"question": first["questions"][1]["question"], "answer": "BMP280"},
        {"question": first["questions"][2]["question"], "answer": "全部 3.3V"},
    ])
    second = clarify.rule_analyze(answer)
    assert second["status"] == "clear", second["questions"]

    class IncorrectClear:
        def chat(self, *args, **kwargs):
            return '{"status":"clear"}'

    guarded = clarify.analyze(prompt, client=IncorrectClear())
    assert guarded["status"] == "clarify" and guarded["source"] == "rules"


def test_offline_report_is_explicitly_non_model():
    result = intent_benchmark.run(intent_benchmark.load_cases())
    assert result["summary"]["all"]["count"] == 12
    assert result["summary"]["holdout"]["count"] == 4
    assert all(row["generation_path"] == "not_run_offline" and
               row["structure_valid"] is None and row["template_fallback"] is None
               for row in result["records"])
    assert all(row["candidate_selection_correct"] and row["clarify_correct"] and
               row["unsupported_detection_correct"] for row in result["records"])


def test_online_record_keeps_preflight_separate_from_fallback():
    case = {"case_id": "audit", "prompt": "3.3V ATmega328P 接 BMP280 走 I2C",
            "required_types": ["atmega328", "bmp280"], "forbidden_types": []}
    row = ai_requirements_benchmark.evaluate_case(
        case, None, {"path": "template"}, elapsed_ms=2.5,
        error="模型未运行")
    assert row["catalog_preflight"]["required_types"] == ["atmega328", "bmp280"]
    assert row["llm_completed"] is False
    assert row["end_to_end_pass"] is False


if __name__ == "__main__":
    for test in (test_i2c_pin_names_and_candidate_scope,
                 test_named_unknown_rejected_before_model_call,
                 test_source_metadata_does_not_become_a_part_number,
                 test_clarification_after_answer,
                 test_offline_report_is_explicitly_non_model,
                 test_online_record_keeps_preflight_separate_from_fallback):
        test()
        print("[PASS]", test.__name__)
