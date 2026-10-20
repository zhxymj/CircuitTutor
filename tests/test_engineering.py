# -*- coding: utf-8 -*-
"""工程估算与 KiCad 官方库检索测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.design_rules import (  # noqa: E402
    compute_divider,
    engineering_notes,
    estimate_led_current_ma,
    suggest_divider_e12,
    suggest_pullup,
)


def test_led_current():
    # 5V 红光 330Ω → (5-2)/330*1000 ≈ 9.09 mA
    i = estimate_led_current_ma(5, 2.0, 330)
    assert 8.5 < i < 9.5
    assert estimate_led_current_ma(5, 2.0, 0) is None


def test_divider():
    assert abs(compute_divider(5, 10000, 10000) - 2.5) < 1e-9
    sug = suggest_divider_e12(5, 1.65)
    assert sug is not None
    r1, r2 = sug
    v = compute_divider(5, r1, r2)
    assert abs(v - 1.65) < 0.15  # E12 近似


def test_pullup():
    assert suggest_pullup("i2c") == 4700
    assert suggest_pullup() == 10000


def test_engineering_notes_led_template():
    import json
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "circuit_templates.json")
    with open(path, encoding="utf-8") as f:
        c = json.load(f)["led_limit"]
    notes = engineering_notes(c)
    assert any("工程估算" in n and "LED" in n for n in notes), notes


def test_engineering_notes_pot():
    circuit = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "potentiometer", "id": "RV1", "attrs": {"value": "10k"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "RV1:1", "red", []],
            ["RV1:3", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }
    notes = engineering_notes(circuit)
    assert any("电位器" in n for n in notes), notes


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {fn.__name__}: {e}")
    print(f"结果：{len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
