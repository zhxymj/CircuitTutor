# -*- coding: utf-8 -*-
"""design_rules 测试：E12 吸附 / LED 欧姆定律 / 拓扑黄金模式。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.design_rules import (  # noqa: E402
    check_topology_patterns,
    compute_led_series_r,
    format_ohm,
    led_vf,
    parse_ohm,
    refine_led_series_resistors,
    snap_e12,
)


def test_snap_e12():
    assert abs(snap_e12(200) - 220) < 1e-9
    assert abs(snap_e12(667) - 680) < 1e-9
    assert abs(snap_e12(1000) - 1000) < 1e-9
    assert abs(snap_e12(87) - 82) < 1e-9 or abs(snap_e12(87) - 100) < 1e-9
    # 87 归一 8.7 → 最近 E12 是 8.2
    assert abs(snap_e12(87) - 82) < 1e-9


def test_parse_and_format():
    assert parse_ohm("330Ω") == 330
    assert parse_ohm("1k") == 1000
    assert parse_ohm("1kΩ") == 1000
    assert parse_ohm("4.7k") == 4700
    assert format_ohm(330) == "330Ω"
    assert format_ohm(1000) == "1kΩ"


def test_led_compute():
    assert led_vf("红") == 2.0
    assert led_vf("蓝") == 3.0
    # 5V 红光：(5-2)/0.01 = 300 → E12 330
    assert abs(compute_led_series_r(5, 2.0) - 330) < 1e-9
    # 12V 红光：1000 → 1kΩ
    assert abs(compute_led_series_r(12, 2.0) - 1000) < 1e-9
    # 3.3V 红光：130 → E12 120 或 150；(3.3-2)/0.01=130 → 120
    r33 = compute_led_series_r(3.3, 2.0)
    assert abs(r33 - 120) < 1e-9


def _led_circuit(resistor_value=None, vcc="5V"):
    parts = [
        {"type": "battery", "id": "BAT1", "attrs": {"value": vcc}},
        {"type": "resistor", "id": "R1", "attrs": {"value": resistor_value} if resistor_value else {}},
        {"type": "led", "id": "LED1", "attrs": {"value": "红"}},
        {"type": "gnd", "id": "GND1"},
    ]
    return {
        "parts": parts,
        "connections": [
            ["BAT1:+", "R1:1", "red", []],
            ["R1:2", "LED1:+", "red", []],
            ["LED1:-", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }


def test_refine_keeps_safe_user_value():
    c = _led_circuit("470Ω")
    notes = refine_led_series_resistors(c)
    assert notes == []
    assert c["parts"][1]["attrs"]["value"] == "470Ω"


def test_refine_fixes_dangerous_value():
    c = _led_circuit("10Ω")  # 危险
    notes = refine_led_series_resistors(c)
    assert len(notes) == 1
    val = c["parts"][1]["attrs"]["value"]
    ohm = parse_ohm(val)
    assert ohm >= 100
    assert "value_source" in c["parts"][1]["attrs"]


def test_refine_uses_battery_voltage():
    c = _led_circuit("10Ω", vcc="12V")
    refine_led_series_resistors(c)
    assert parse_ohm(c["parts"][1]["attrs"]["value"]) == 1000


def test_refine_idempotent():
    c = _led_circuit("10Ω")
    n1 = refine_led_series_resistors(c)
    n2 = refine_led_series_resistors(c)
    assert n1 and n2 == []


def test_topology_led_missing_series():
    c = _led_circuit()
    # 删掉电阻相关连接，模拟 LED 直跨电源
    c["connections"] = [
        ["BAT1:+", "LED1:+", "red", []],
        ["LED1:-", "GND1:1", "black", []],
        ["BAT1:-", "GND1:1", "black", []],
    ]
    # 电阻还在 parts 但无连接——拓扑检查看的是串联边
    warns = check_topology_patterns(c)
    assert any("串联" in w or "限流" in w for w in warns)


def test_topology_555_ok_template():
    import json
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "circuit_templates.json")
    with open(path, encoding="utf-8") as f:
        tpl = json.load(f)
    c = tpl["ne555_blink"]
    warns = check_topology_patterns(c)
    # 内置模板应无关键拓扑缺失
    assert warns == [], warns


def test_self_check_module():
    # 运行模块自检
    import core.design_rules as dr
    # __main__ 不自动跑；手动断言已在上面覆盖
    assert dr.snap_e12(150) in (150, 180)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[ERR ] {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n结果：{len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
