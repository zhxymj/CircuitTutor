"""Offline requirement blocking, safe exceptions and zero-call rejection."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import llm
from core.intent_constraints import IntentConstraintError, detect_requirement_blockers
from evaluation.requirements_v2 import load_dataset


class IntentConstraintTests(unittest.TestCase):
    def assert_rule(self, prompt, rule):
        blockers = detect_requirement_blockers(prompt)
        self.assertIn(rule, {item["rule_id"] for item in blockers}, prompt)
        for item in blockers:
            start, end = item["span"]
            self.assertEqual(item["evidence"], prompt[start:end])
            self.assertEqual(item["severity"], "error")
            self.assertEqual(item["source"], "explicit_requirement_rule")
        return blockers

    def test_corpus_labels_are_text_rules_not_erc(self):
        for task in load_dataset()["tasks"]:
            with self.subTest(task=task["task_id"]):
                observed = {b["rule_id"] for b in detect_requirement_blockers(task["prompt"])}
                if task["category"] == "negative":
                    self.assertTrue(set(task["negative_rule_ids"]).issubset(observed))
                else:
                    self.assertFalse(observed)

    def test_fresh_explicit_actions_and_evidence_spans(self):
        examples = [
            ("ESP32 的 IIC 走线：让 SCL 与 SDA 短路。", "SDA_SCL_SHORT"),
            ("3.3V I2C 接口中 SDA 永久接到 VCC。", "SIGNAL_TIED_TO_SUPPLY"),
            ("I2C 主从地互相不连接，必须能通信。", "NO_COMMON_GROUND"),
            ("I2C 两传感器地址相同，要求独立读取。", "I2C_ADDRESS_COLLISION"),
            ("3.3V IIC 上拉电阻接到 5V。", "I2C_PULLUP_WRONG_DOMAIN"),
            ("发光二极管亮灭指示：删除限流电阻。", "LED_CURRENT_LIMIT_OMITTED"),
            ("LM1117 稳压器：去掉输出电容。", "LDO_OUTPUT_CAP_OMITTED"),
            ("LDO 的输出直接接地。", "REGULATED_SUPPLY_SHORT"),
            ("ESP32 主控 EN 始终接 GND，还要保持运行。", "MCU_ENABLE_HELD_INACTIVE"),
            ("IIC 接口的 TVS 保护 GND 不连接。", "PROTECTION_GROUND_OMITTED"),
            ("I2C 外部口 GND 不连接。", "EXTERNAL_GROUND_OMITTED"),
            ("ESD 阵列 GND 短接 VCC。", "PROTECTION_GROUND_WRONG"),
            ("要求使用不存在于目录的引脚。", "UNSUPPORTED_LOGICAL_PIN"),
            ("I2C 两个主控的推挽输出直接相连。", "I2C_PUSH_PULL_CONTENTION"),
        ]
        for prompt, rule in examples:
            with self.subTest(rule=rule):
                self.assert_rule(prompt, rule)

    def test_forbidden_bad_action_does_not_become_requested_bad_action(self):
        safe = [
            "3.3V I2C ESP32：不要将 SDA 和 SCL 短接。",
            "SDA 走 I2C，不允许 SDA 直接焊到 3.3V 电源。",
            "I2C 主从必须共地，不要不共地。",
            "LED 不要删除限流电阻。",
            "LED 禁止删除限流电阻。",
            "LM1117 禁止删除输出电容。",
            "LDO 输出和 GND 严禁短接。",
            "ESP32 禁止 EN 永久接地。",
            "TVS 保护 GND 不要悬空。",
            "I2C 排针不要让 GND 不连接。",
            "ESD 阵列禁止 GND 接 VCC。",
            "不要要求不存在于目录的引脚。",
            "多主 I2C 禁止推挽输出直接相连。",
        ]
        for prompt in safe:
            with self.subTest(prompt=prompt):
                self.assertEqual(detect_requirement_blockers(prompt), [])

    def test_declared_electrical_exceptions_and_non_signal_pins(self):
        safe = [
            "I2C 两侧不共地，使用数字隔离器实现隔离。",
            "3.3V I2C 使用双向电平转换器，5V 侧上拉接 5V。",
            "LED 使用恒流驱动，不加限流电阻。",
            "I2C 两传感器地址相同，用多路复用器分别读取。",
            "I2C 两个器件地址相同，仅广播相同配置并且不读取。",
            "I2C 的 SDA 通过 4.7k 电阻接 3.3V 电源。",
            "I2C 的 SDA 上拉电阻直接接 3.3V 电源。",
            "GPIO0 固定到电源，用作输入启动配置。",
            "采用允许无输出电容的 LDO，不加输出电容。",
            "I2C 外部口 GND 悬空，两侧使用数字隔离器。",
            "I2C 单主 UFm 总线推挽输出直接相连。",
            "ESP32 EN 永久接地，本板不要求主控运行。",
        ]
        for prompt in safe:
            with self.subTest(prompt=prompt):
                self.assertEqual(detect_requirement_blockers(prompt), [])

    def test_explicitly_forbidden_exception_does_not_clear_blocker(self):
        examples = [
            ("I2C 不共地且禁止隔离器。", "NO_COMMON_GROUND"),
            ("3.3V I2C 上拉接 5V，不用电平转换器。", "I2C_PULLUP_WRONG_DOMAIN"),
            ("LED 不用恒流驱动且禁止限流电阻。", "LED_CURRENT_LIMIT_OMITTED"),
            ("I2C 两设备地址相同，禁止多路复用器但必须分别读取。", "I2C_ADDRESS_COLLISION"),
        ]
        for prompt, rule in examples:
            with self.subTest(rule=rule):
                self.assert_rule(prompt, rule)

    def test_blocker_is_terminal_before_budget_model_or_fallback(self):
        class NeverCall:
            def chat(self, *args, **kwargs):
                raise AssertionError("blocked requirements must not invoke a model")

        prompt = "3.3V ESP32 接 BMP280 走 I2C，SDA 和 SCL 短接"
        brief = llm.build_generation_brief(prompt, {})
        self.assertEqual(brief["blockers"][0]["rule_id"], "SDA_SCL_SHORT")
        for planning in (False, True):
            with self.subTest(planning=planning), patch.object(llm, "CallBudget") as budget:
                with self.assertRaises(IntentConstraintError) as raised:
                    llm.generate_circuit_json(prompt, {}, NeverCall(), with_plan=planning)
                self.assertEqual(raised.exception.blockers, brief["blockers"])
                self.assertIn("需求存在明确矛盾", str(raised.exception))
                budget.assert_not_called()

    def test_empty_input_and_rule_deduplication(self):
        self.assertEqual(detect_requirement_blockers(""), [])
        self.assertEqual(detect_requirement_blockers(None), [])
        blockers = detect_requirement_blockers("I2C SDA SCL 短接；SDA SCL 短接")
        self.assertEqual([item["rule_id"] for item in blockers], ["SDA_SCL_SHORT"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
