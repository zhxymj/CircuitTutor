# -*- coding: utf-8 -*-
"""原理图合规检查的固定缺陷注入评测。

本模块只做三件事：

1. 从现有模板/元件库构造可追溯的合法对照与单点缺陷样本；
2. 同时运行确定性 ERC 与额定值检查；
3. 从逐样本记录自动计算指标并写出 JSON/CSV 证据。

它不是公开数据集，也不把构造样本包装成“真实生产数据”。公开数据的接入状态
由 coverage/limitations 明确说明。运行时不调用 LLM、不联网，结果可离线重放。
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import platform
import random
import runpy
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from core import autofix
from core.catalog import CATALOG, build_erc_kb
from core.erc import ALL_CHECKERS, run_erc
from core.erc_ratings import run_rating_checks


DEFAULT_SEED = 1550
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_PATH = os.path.join(ROOT, "data", "circuit_templates.json")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _load_templates() -> Dict[str, dict]:
    with open(TEMPLATE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _template(name: str) -> dict:
    return copy.deepcopy(_load_templates()[name])


def _direct_buzzer() -> dict:
    return {
        "title": "5V 有源蜂鸣器极性对照",
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "buzzer", "id": "BZ1", "attrs": {"value": "5V"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "BZ1:+", "red", []],
            ["BZ1:-", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }


def _resistive_load(value: str = "330Ω") -> dict:
    return {
        "title": "5V 电阻负载功耗对照",
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": value}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "R1:1", "red", []],
            ["R1:2", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }


def _rated_capacitor(voltage: str = "25V") -> dict:
    return {
        "title": "12V 电容耐压对照",
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "12V"}},
            {"type": "capacitor", "id": "C1",
             "attrs": {"value": "100uF", "voltage": voltage}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "C1:+", "red", []],
            ["C1:-", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }


def _dual_output_driver() -> dict:
    """两个独立信号输出；不建模供电脚，专用于输出竞争的结构对照。"""
    return {
        "title": "双输出隔离对照",
        "parts": [
            {"type": "opamp_follower", "id": "U1"},
            {"type": "opamp_follower", "id": "U2"},
            {"type": "resistor", "id": "R1", "attrs": {"value": "330Ω"}},
            {"type": "resistor", "id": "R2", "attrs": {"value": "330Ω"}},
            {"type": "led", "id": "LED1", "attrs": {"value": "红"}},
            {"type": "led", "id": "LED2", "attrs": {"value": "绿"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["U1:+", "GND1:1", "black", []],
            ["U1:-", "GND1:1", "black", []],
            ["U2:+", "GND1:1", "black", []],
            ["U2:-", "GND1:1", "black", []],
            ["U1:out", "R1:1", "green", []],
            ["R1:2", "LED1:+", "green", []],
            ["LED1:-", "GND1:1", "black", []],
            ["U2:out", "R2:1", "green", []],
            ["R2:2", "LED2:+", "green", []],
            ["LED2:-", "GND1:1", "black", []],
        ],
    }


def _nmos_gate_control() -> dict:
    return {
        "title": "NMOS 栅极下拉对照",
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "1kΩ"}},
            {"type": "nmos", "id": "Q1"},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "R1:1", "red", []],
            ["R1:2", "Q1:D", "green", []],
            ["Q1:S", "GND1:1", "black", []],
            ["Q1:G", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }


def _relay_with_flyback() -> dict:
    circuit = _template("relay_driver")
    circuit["parts"].append(
        {"type": "diode", "id": "D1", "attrs": {"value": "1N4148"}})
    circuit["connections"].extend([
        ["D1:K", "BAT1:+", "red", []],
        ["D1:A", "Q1:C", "black", []],
    ])
    circuit["title"] = "继电器驱动（含续流二极管对照）"
    return circuit


def _i2c_bus(address1: str = "0x76", address2: str = "0x77",
             pullups: bool = True) -> dict:
    parts = [
        {"type": "battery", "id": "BAT1", "attrs": {"value": "3.3V"}},
        {"type": "gnd", "id": "GND1"},
        {"type": "i2c_header", "id": "J1"},
        {"type": "bmp280", "id": "U1", "attrs": {"i2c_address": address1}},
        {"type": "bmp280", "id": "U2", "attrs": {"i2c_address": address2}},
        {"type": "capacitor", "id": "C1", "attrs": {"value": "100nF"}},
        {"type": "capacitor", "id": "C2", "attrs": {"value": "100nF"}},
    ]
    connections = [
        ["BAT1:+", "J1:VCC", "red", []],
        ["J1:VCC", "U1:VCC", "red", []],
        ["J1:VCC", "U2:VCC", "red", []],
        ["BAT1:-", "GND1:1", "black", []],
        ["GND1:1", "J1:GND", "black", []],
        ["GND1:1", "U1:GND", "black", []],
        ["GND1:1", "U2:GND", "black", []],
        ["U1:SDO", "GND1:1", "black", []],
        ["U2:SDO", "GND1:1", "black", []],
        ["U1:CSB", "BAT1:+", "red", []],
        ["U2:CSB", "BAT1:+", "red", []],
        ["C1:+", "BAT1:+", "red", []],
        ["C1:-", "GND1:1", "black", []],
        ["C2:+", "BAT1:+", "red", []],
        ["C2:-", "GND1:1", "black", []],
        ["J1:SCL", "U1:SCL", "green", []],
        ["J1:SCL", "U2:SCL", "green", []],
        ["J1:SDA", "U1:SDA", "blue", []],
        ["J1:SDA", "U2:SDA", "blue", []],
    ]
    if pullups:
        parts.extend([
            {"type": "resistor", "id": "R_SCL", "attrs": {"value": "4.7kΩ"}},
            {"type": "resistor", "id": "R_SDA", "attrs": {"value": "4.7kΩ"}},
        ])
        connections.extend([
            ["J1:SCL", "R_SCL:1", "green", []],
            ["R_SCL:2", "BAT1:+", "red", []],
            ["J1:SDA", "R_SDA:1", "blue", []],
            ["R_SDA:2", "BAT1:+", "red", []],
        ])
    return {"title": "3.3V I2C 多设备总线", "parts": parts,
            "connections": connections}


def _voltage_domain(supply: str = "3.3V") -> dict:
    return {
        "title": "显式供电范围传感器",
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": supply}},
            {"type": "gnd", "id": "GND1"},
            {"type": "i2c_header", "id": "J1"},
            {"type": "bmp280", "id": "U1", "attrs": {
                "supply_min_v": "1.7V", "supply_max_v": "3.6V",
            }},
            {"type": "resistor", "id": "R_SCL", "attrs": {"value": "4.7kΩ"}},
            {"type": "resistor", "id": "R_SDA", "attrs": {"value": "4.7kΩ"}},
            {"type": "capacitor", "id": "C1", "attrs": {"value": "100nF"}},
        ],
        "connections": [
            ["BAT1:+", "J1:VCC", "red", []],
            ["BAT1:+", "U1:VCC", "red", []],
            ["BAT1:-", "GND1:1", "black", []],
            ["GND1:1", "J1:GND", "black", []],
            ["GND1:1", "U1:GND", "black", []],
            ["U1:SDO", "GND1:1", "black", []],
            ["U1:CSB", "BAT1:+", "red", []],
            ["J1:SCL", "U1:SCL", "green", []],
            ["J1:SDA", "U1:SDA", "blue", []],
            ["J1:SCL", "R_SCL:1", "green", []],
            ["R_SCL:2", "BAT1:+", "red", []],
            ["J1:SDA", "R_SDA:1", "blue", []],
            ["R_SDA:2", "BAT1:+", "red", []],
            ["C1:+", "BAT1:+", "red", []],
            ["C1:-", "GND1:1", "black", []],
        ],
    }


def _replace_connection(circuit: dict, old_a: str, old_b: str,
                        new_a: str, new_b: str) -> None:
    for index, connection in enumerate(circuit.get("connections", [])):
        if len(connection) < 2:
            continue
        if {connection[0], connection[1]} == {old_a, old_b}:
            rest = list(connection[2:])
            circuit["connections"][index] = [new_a, new_b] + rest
            return
    raise ValueError(f"未找到待替换连接：{old_a} <-> {old_b}")


def _remove_part_and_connections(circuit: dict, part_id: str) -> None:
    circuit["parts"] = [part for part in circuit.get("parts", [])
                        if part.get("id") != part_id]
    circuit["connections"] = [
        connection for connection in circuit.get("connections", [])
        if not any(isinstance(item, str) and item.startswith(part_id + ":")
                   for item in connection[:2])
    ]


def _make_pair(*, slug: str, base_id: str, source: str, defect_type: str,
               location: str, expected_rules: Sequence[str], severity: str,
               auto_fixable: bool, base: dict, injected: dict,
               note: str = "") -> List[dict]:
    control_id = f"{slug}__control"
    injected_id = f"{slug}__injected"
    common = {
        "base_circuit_id": base_id,
        "source": source,
        "defect_type": defect_type,
        "injection_location": location,
        "expected_severity": severity,
        "note": note,
    }
    control = {
        **common,
        "case_id": control_id,
        "paired_case_id": injected_id,
        "sample_role": "control",
        "expected_rules": [],
        "auto_fixable": False,
        "expected_after_repair": {"status": "not_applicable", "remaining_rules": []},
        "circuit": copy.deepcopy(base),
    }
    positive = {
        **common,
        "case_id": injected_id,
        "paired_case_id": control_id,
        "sample_role": "injected",
        "expected_rules": list(expected_rules),
        "auto_fixable": bool(auto_fixable),
        "expected_after_repair": {
            "status": "target_rules_cleared" if auto_fixable else "manual_review",
            "remaining_rules": [],
        },
        "circuit": copy.deepcopy(injected),
    }
    return [control, positive]


def _build_pairs() -> List[List[dict]]:
    pairs: List[List[dict]] = []

    base = _template("led_limit")
    bad = copy.deepcopy(base)
    bad["connections"].append(["BAT1:+", "GND1:1", "red", []])
    pairs.append(_make_pair(
        slug="power_ground_short", base_id="template:led_limit",
        source="existing_template+deterministic_injection", defect_type="电源与地短路",
        location="BAT1:+ -> GND1:1", expected_rules=["电源短路"], severity="fatal",
        auto_fixable=False, base=base, injected=bad))

    base = _dual_output_driver()
    bad = copy.deepcopy(base)
    bad["connections"].append(["U1:out", "U2:out", "green", []])
    pairs.append(_make_pair(
        slug="output_contention", base_id="constructed:dual_output_driver",
        source="catalog_constructed+deterministic_injection", defect_type="输出脚互连",
        location="U1:out <-> U2:out", expected_rules=["输出冲突"], severity="major",
        auto_fixable=False, base=base, injected=bad))

    base = _nmos_gate_control()
    bad = copy.deepcopy(base)
    bad["connections"] = [connection for connection in bad["connections"]
                          if {connection[0], connection[1]} != {"Q1:G", "GND1:1"}]
    pairs.append(_make_pair(
        slug="floating_input", base_id="constructed:nmos_gate_control",
        source="catalog_constructed+deterministic_injection", defect_type="输入悬空",
        location="remove Q1:G -> GND1:1", expected_rules=["悬空输入"], severity="warning",
        auto_fixable=False, base=base, injected=bad))

    base = _template("led_limit")
    bad = copy.deepcopy(base)
    _remove_part_and_connections(bad, "R1")
    bad["connections"].append(["BAT1:+", "LED1:+", "red", []])
    pairs.append(_make_pair(
        slug="led_missing_resistor", base_id="template:led_limit",
        source="existing_template+deterministic_injection", defect_type="LED缺限流电阻",
        location="remove R1; BAT1:+ -> LED1:+", expected_rules=["LED缺限流电阻"],
        severity="major", auto_fixable=False, base=base, injected=bad))

    base = _template("led_limit")
    bad = copy.deepcopy(base)
    next(part for part in bad["parts"] if part["id"] == "R1")["attrs"]["value"] = "50Ω"
    pairs.append(_make_pair(
        slug="led_resistor_too_small", base_id="template:led_limit",
        source="existing_template+deterministic_injection", defect_type="电阻阻值不合理",
        location="R1.value: 330Ω -> 50Ω",
        expected_rules=["LED限流电阻过小", "LED超额定电流"], severity="warning+danger",
        auto_fixable=False, base=base, injected=bad,
        note="一个参数注入同时产生规则告警和额定值越限，二者均计为预期。"))

    base = _resistive_load("330Ω")
    bad = _resistive_load("100Ω")
    pairs.append(_make_pair(
        slug="resistor_power_limit", base_id="constructed:resistive_load",
        source="catalog_constructed+deterministic_injection", defect_type="电阻功耗越限",
        location="R1.value: 330Ω -> 100Ω", expected_rules=["电阻功耗超限"],
        severity="warning", auto_fixable=False, base=base, injected=bad))

    base = _direct_buzzer()
    bad = copy.deepcopy(base)
    bad["connections"][0][1] = "BZ1:-"
    bad["connections"][1][0] = "BZ1:+"
    pairs.append(_make_pair(
        slug="reversed_polarity", base_id="constructed:direct_buzzer",
        source="catalog_constructed+deterministic_injection", defect_type="极性元件反接",
        location="swap BZ1:+ and BZ1:-", expected_rules=["极性反接"], severity="major",
        auto_fixable=False, base=base, injected=bad))

    base = _rated_capacitor("25V")
    bad = _rated_capacitor("10V")
    pairs.append(_make_pair(
        slug="capacitor_underrated", base_id="constructed:rated_capacitor",
        source="catalog_constructed+deterministic_injection", defect_type="器件耐压不足",
        location="C1.voltage: 25V -> 10V", expected_rules=["电容耐压不足"],
        severity="warning", auto_fixable=False, base=base, injected=bad))

    base = _template("ne555_blink")
    bad = copy.deepcopy(base)
    _remove_part_and_connections(bad, "C3")
    pairs.append(_make_pair(
        slug="missing_decoupling", base_id="template:ne555_blink",
        source="existing_template+deterministic_injection", defect_type="芯片缺去耦电容",
        location="remove C3 on U1:VCC", expected_rules=["缺去耦电容"],
        severity="warning", auto_fixable=True, base=base, injected=bad))

    base = _template("ne555_blink")
    bad = copy.deepcopy(base)
    _replace_connection(bad, "V1:1", "C3:+", "R1:2", "C3:+")
    pairs.append(_make_pair(
        slug="misplaced_decoupling", base_id="template:ne555_blink",
        source="existing_template+deterministic_injection", defect_type="去耦位置或连接不合理",
        location="move C3:+ from VCC net to R1:2", expected_rules=["缺去耦电容"],
        severity="warning", auto_fixable=True, base=base, injected=bad))

    base = _relay_with_flyback()
    bad = copy.deepcopy(base)
    _remove_part_and_connections(bad, "D1")
    pairs.append(_make_pair(
        slug="missing_flyback", base_id="template:relay_driver+flyback",
        source="existing_template_corrected+deterministic_injection",
        defect_type="继电器/电机缺续流二极管", location="remove D1 across K1 coil",
        expected_rules=["缺续流二极管"], severity="warning", auto_fixable=False,
        base=base, injected=bad))

    base = _template("ne555_blink")
    bad = copy.deepcopy(base)
    _replace_connection(bad, "V1:1", "U1:VCC", "U1:VCC", "R2:2")
    pairs.append(_make_pair(
        slug="undriven_power_net", base_id="template:ne555_blink",
        source="existing_template+deterministic_injection", defect_type="电源网络缺驱动",
        location="move U1:VCC from V1 to passive timing net",
        expected_rules=["芯片未供电"], severity="major", auto_fixable=False,
        base=base, injected=bad,
        note="该样本用于暴露供电来源判定是否把芯片自身 PWR_IN 误当成电源。"))

    base = _template("led_limit")
    bad = copy.deepcopy(base)
    bad["parts"].append({"type": "invented_sensor_xyz", "id": "X1"})
    pairs.append(_make_pair(
        slug="unknown_component", base_id="template:led_limit",
        source="existing_template+deterministic_injection", defect_type="错误器件映射",
        location="append X1:invented_sensor_xyz", expected_rules=["未知元件"],
        severity="major", auto_fixable=False, base=base, injected=bad))

    base = _template("led_limit")
    bad = copy.deepcopy(base)
    bad["connections"].append(["R1:99", "R1:1", "green", []])
    pairs.append(_make_pair(
        slug="hallucinated_pin", base_id="template:led_limit",
        source="existing_template+deterministic_injection", defect_type="错误引脚映射",
        location="append invalid R1:99 reference", expected_rules=["幻觉引脚"],
        severity="fatal", auto_fixable=False, base=base, injected=bad))

    base = _i2c_bus(pullups=True)
    bad = _i2c_bus(pullups=False)
    pairs.append(_make_pair(
        slug="i2c_missing_pullups", base_id="constructed:i2c_bus",
        source="catalog_constructed+deterministic_injection", defect_type="I2C缺上拉",
        location="remove R_SCL/R_SDA pull-ups", expected_rules=["I2C缺上拉"],
        severity="warning", auto_fixable=False, base=base, injected=bad,
        note="仅在至少两个器件共享 SDA/SCL 时检查；外部上拉需由用户显式建模。"))

    base = _i2c_bus(address1="0x76", address2="0x77", pullups=True)
    bad = _i2c_bus(address1="0x76", address2="0x76", pullups=True)
    pairs.append(_make_pair(
        slug="i2c_address_conflict", base_id="constructed:i2c_bus",
        source="catalog_constructed+deterministic_injection", defect_type="总线地址冲突",
        location="U2.i2c_address: 0x77 -> 0x76", expected_rules=["总线地址冲突"],
        severity="major", auto_fixable=False, base=base, injected=bad,
        note="只比较同一 SDA/SCL 总线上的显式 7 位地址；未知地址不推断。"))

    base = _voltage_domain("3.3V")
    bad = _voltage_domain("5V")
    pairs.append(_make_pair(
        slug="incompatible_power_rail", base_id="constructed:voltage_domain",
        source="catalog_constructed+deterministic_injection", defect_type="电源轨不兼容",
        location="BAT1.value: 3.3V -> 5V", expected_rules=["电源轨不兼容"],
        severity="major", auto_fixable=False, base=base, injected=bad,
        note="仅使用器件 attrs 中显式供电上下限；缺少范围时不猜测。"))

    return pairs


def _coverage() -> List[dict]:
    implemented = {
        "电源与地短路": "power_ground_short__injected",
        "输出脚互连": "output_contention__injected",
        "输入悬空": "floating_input__injected",
        "LED缺限流电阻": "led_missing_resistor__injected",
        "电阻阻值不合理": "led_resistor_too_small__injected",
        "电阻功耗越限": "resistor_power_limit__injected",
        "极性元件反接": "reversed_polarity__injected",
        "器件耐压不足": "capacitor_underrated__injected",
        "芯片缺去耦电容": "missing_decoupling__injected",
        "去耦位置或连接不合理": "misplaced_decoupling__injected",
        "继电器/电机缺续流二极管": "missing_flyback__injected",
        "电源网络缺驱动": "undriven_power_net__injected",
        "错误器件映射": "unknown_component__injected",
        "错误引脚映射": "hallucinated_pin__injected",
        "I2C缺上拉": "i2c_missing_pullups__injected",
        "总线地址冲突": "i2c_address_conflict__injected",
        "电源轨不兼容": "incompatible_power_rail__injected",
    }
    rows = [{"defect_type": name, "status": "implemented",
             "case_id": case_id, "reason": "固定构造样本与成对合法对照"}
            for name, case_id in implemented.items()]
    return rows


def build_case_dataset(seed: int = DEFAULT_SEED) -> dict:
    """构造固定评测集；同一 seed 返回字节级等价的逻辑内容。"""
    pair_groups = _build_pairs()
    random.Random(seed).shuffle(pair_groups)
    cases = [case for pair in pair_groups for case in pair]
    coverage = _coverage()
    fingerprint_payload = {"seed": seed, "coverage": coverage, "cases": cases}
    metadata = {
        "schema_version": "1.0",
        "dataset_name": "CircuitTutor deterministic compliance benchmark",
        "dataset_kind": "constructed_fault_injection",
        "seed": seed,
        "case_count": len(cases),
        "positive_count": sum(c["sample_role"] == "injected" for c in cases),
        "control_count": sum(c["sample_role"] == "control" for c in cases),
        "source_statement": (
            "基于项目内置模板与当前元件知识库构造；不是 SchGen 全量数据，"
            "不代表生产现场分布。"
        ),
        "fingerprint": _fingerprint(fingerprint_payload),
    }
    return {"metadata": metadata, "coverage": coverage, "cases": cases}


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def _stat(tp: int, fp: int, fn: int, tn: int) -> dict:
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = None
    if precision is not None and recall is not None and precision + recall:
        f1 = round(2 * precision * recall / (precision + recall), 6)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_positive_rate": _ratio(fp, fp + tn),
    }


def compute_metrics(records: Sequence[dict], rules: Sequence[str]) -> dict:
    """按 case×rule 二元矩阵统计；空分母返回 null，而不是伪造 0 或 1。"""
    per_rule: Dict[str, dict] = {}
    for rule in sorted(set(rules)):
        tp = fp = fn = tn = 0
        for record in records:
            expected = rule in set(record.get("expected_rules") or [])
            detected = rule in set(record.get("detected_rules") or [])
            if expected and detected:
                tp += 1
            elif not expected and detected:
                fp += 1
            elif expected and not detected:
                fn += 1
            else:
                tn += 1
        per_rule[rule] = _stat(tp, fp, fn, tn)

    aggregate = _stat(
        sum(row["tp"] for row in per_rule.values()),
        sum(row["fp"] for row in per_rule.values()),
        sum(row["fn"] for row in per_rule.values()),
        sum(row["tn"] for row in per_rule.values()),
    )
    exact = sum(
        set(record.get("expected_rules") or []) == set(record.get("detected_rules") or [])
        for record in records
    )
    aggregate["case_exact_match_rate"] = _ratio(exact, len(records))
    aggregate["case_count"] = len(records)
    return {"aggregate": aggregate, "per_rule": per_rule}


def _issue_dict(issue: Any, source: str) -> dict:
    return {
        "source": source,
        "severity": str(getattr(issue, "severity", "unknown")),
        "rule": str(getattr(issue, "rule", "unknown")),
        "message": str(getattr(issue, "message", "")),
        "pins": list(getattr(issue, "pins", []) or []),
    }


def _detect(circuit: dict, kb: dict) -> tuple[Any, List[dict]]:
    report = run_erc(circuit, kb)
    issues = [_issue_dict(issue, "deterministic_erc") for issue in report.issues]
    issues.extend(_issue_dict(issue, "rating_check")
                  for issue in run_rating_checks(circuit))
    return report, issues


def evaluate_dataset(dataset: dict) -> dict:
    """运行离线评测。elapsed_ms 仅作观测，不进入可重复性指纹与指标。"""
    kb = build_erc_kb()
    rules = sorted({rule for case in dataset["cases"]
                    for rule in case.get("expected_rules", [])})
    records: List[dict] = []
    repair_records: List[dict] = []

    for case in dataset["cases"]:
        start = time.perf_counter()
        report, all_issues = _detect(case["circuit"], kb)
        elapsed_ms = round((time.perf_counter() - start) * 1000.0, 3)
        detected_rules = sorted({issue["rule"] for issue in all_issues
                                 if issue["rule"] in rules})
        expected_rules = sorted(set(case.get("expected_rules") or []))
        record = {
            "case_id": case["case_id"],
            "sample_role": case["sample_role"],
            "defect_type": case["defect_type"],
            "expected_rules": expected_rules,
            "detected_rules": detected_rules,
            "all_detected_rules": sorted({issue["rule"] for issue in all_issues}),
            "matched_rules": sorted(set(expected_rules) & set(detected_rules)),
            "missed_rules": sorted(set(expected_rules) - set(detected_rules)),
            "unexpected_scoped_rules": sorted(set(detected_rules) - set(expected_rules)),
            "issues": all_issues,
            "erc_passed": report.passed,
            "elapsed_ms": elapsed_ms,
            "status": "pass" if set(expected_rules) == set(detected_rules) else "fail",
        }

        if case.get("auto_fixable") and case["sample_role"] == "injected":
            repair_start = time.perf_counter()
            fixed, fixes = autofix.apply(case["circuit"], kb, report)
            _fixed_report, fixed_issues = _detect(fixed, kb)
            repair_ms = round((time.perf_counter() - repair_start) * 1000.0, 3)
            fixed_rules = sorted({issue["rule"] for issue in fixed_issues
                                  if issue["rule"] in rules})
            target_cleared = not (set(expected_rules) & set(fixed_rules))
            new_rules = sorted(set(fixed_rules) - set(detected_rules))
            success = bool(fixes) and target_cleared and not new_rules
            repair = {
                "case_id": case["case_id"],
                "attempted": True,
                "fix_count": len(fixes),
                "fixes": fixes,
                "target_rules_cleared": target_cleared,
                "remaining_scoped_rules": fixed_rules,
                "new_scoped_rules": new_rules,
                "success": success,
                "repair_rounds": 1,
                "elapsed_ms": repair_ms,
                "llm_calls": 0,
            }
            record["repair"] = repair
            repair_records.append(repair)
        records.append(record)

    metrics = compute_metrics(records, rules)
    eligible = len(repair_records)
    repaired = sum(row["success"] for row in repair_records)
    introduced = sum(bool(row["new_scoped_rules"]) for row in repair_records)
    repair_metrics = {
        "eligible_cases": eligible,
        "attempted_cases": eligible,
        "successful_cases": repaired,
        "top1_repair_success_rate": _ratio(repaired, eligible),
        "post_repair_target_clear_rate": _ratio(
            sum(row["target_rules_cleared"] for row in repair_records), eligible),
        "new_defect_rate": _ratio(introduced, eligible),
        "average_repair_rounds": _ratio(
            sum(row["repair_rounds"] for row in repair_records), eligible),
        "average_repair_ms": (
            round(sum(row["elapsed_ms"] for row in repair_records) / eligible, 3)
            if eligible else None
        ),
        "llm_calls": sum(row["llm_calls"] for row in repair_records),
        "refused_or_failed_cases": eligible - repaired,
    }
    return {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "run_status": "pass",
        "run_status_definition": "pass 表示全部样本完成执行；检测质量见 metrics，不等于零漏报。",
        "dataset_fingerprint": dataset["metadata"]["fingerprint"],
        "evaluation_rules": rules,
        "metrics": metrics,
        "repair_metrics": repair_metrics,
        "records": records,
        "limitations": [
            "数据为固定构造缺陷注入集，不代表公开数据集或生产分布。",
            "指标只覆盖 evaluation_rules；未实现规则不进入分母并在 coverage 单列。",
            "额定值检查为工程估算，最终结论仍需 SPICE、器件手册与 KiCad 复验。",
            "本评测未调用仅 AI 审查基线，因此不报告该项对照数据。",
        ],
    }


def _test_suite_counts() -> dict:
    runner = runpy.run_path(os.path.join(ROOT, "tests", "run_tests.py"))
    return {
        "python": len(runner.get("PY_SUITES", [])),
        "node": len(runner.get("JS_SUITES", [])),
        "total": len(runner.get("PY_SUITES", [])) + len(runner.get("JS_SUITES", [])),
    }


def project_facts(dataset: dict) -> dict:
    templates = _load_templates()
    return {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "source_of_truth": {
            "component_types": "core.catalog.CATALOG",
            "erc_rules": "core.erc.ALL_CHECKERS",
            "templates": "data/circuit_templates.json",
            "test_suites": "tests/run_tests.py",
        },
        "counts": {
            "component_types": len(CATALOG),
            "erc_rules": len(ALL_CHECKERS),
            "templates": len(templates),
            "benchmark_cases": len(dataset["cases"]),
            "benchmark_positive_cases": dataset["metadata"]["positive_count"],
            "benchmark_control_cases": dataset["metadata"]["control_count"],
            "test_suites": _test_suite_counts(),
        },
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "test_execution_status": "not_run_by_benchmark",
        "note": "测试通过数由最终验收单独记录；此文件不把套件数量冒充通过数量。",
    }


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _write_summary_csv(path: str, result: dict) -> None:
    fields = ["scope", "rule", "tp", "fp", "fn", "tn", "precision",
              "recall", "f1", "false_positive_rate", "case_exact_match_rate"]
    rows = []
    aggregate = result["metrics"]["aggregate"]
    rows.append({"scope": "aggregate", "rule": "ALL", **aggregate})
    for rule, metrics in result["metrics"]["per_rule"].items():
        rows.append({"scope": "rule", "rule": rule, **metrics,
                     "case_exact_match_rate": ""})
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fields})


def _write_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def write_evidence(output_dir: str, seed: int = DEFAULT_SEED) -> Dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    dataset = build_case_dataset(seed=seed)
    result = evaluate_dataset(dataset)
    facts = project_facts(dataset)
    paths = {
        "compliance_cases.json": os.path.join(output_dir, "compliance_cases.json"),
        "compliance_results.json": os.path.join(output_dir, "compliance_results.json"),
        "compliance_summary.csv": os.path.join(output_dir, "compliance_summary.csv"),
        "project_facts.json": os.path.join(output_dir, "project_facts.json"),
    }
    _write_json(paths["compliance_cases.json"], dataset)
    _write_json(paths["compliance_results.json"], result)
    _write_summary_csv(paths["compliance_summary.csv"], result)
    _write_json(paths["project_facts.json"], facts)
    return paths


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(argv or sys.argv[1:])
    output_dir = os.path.abspath(argv[0]) if argv else os.path.join(ROOT, "evidence")
    paths = write_evidence(output_dir, DEFAULT_SEED)
    with open(paths["compliance_results.json"], encoding="utf-8") as handle:
        result = json.load(handle)
    summary = result["metrics"]["aggregate"]
    print("合规评测完成")
    print(f"  数据指纹：{result['dataset_fingerprint']}")
    print(f"  TP/FP/FN/TN：{summary['tp']}/{summary['fp']}/{summary['fn']}/{summary['tn']}")
    print(f"  Precision/Recall/F1：{summary['precision']}/{summary['recall']}/{summary['f1']}")
    print(f"  证据目录：{output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
