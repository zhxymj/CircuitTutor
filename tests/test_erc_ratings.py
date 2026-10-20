# -*- coding: utf-8 -*-
"""ERC 额定值告警测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.erc_ratings import (  # noqa: E402
    check_led_ratings,
    check_regulator_ratings,
    check_resistor_power,
    run_rating_checks,
)


def _led_circuit(r_value, vcc="5V"):
    return {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": vcc}},
            {"type": "resistor", "id": "R1", "attrs": {"value": r_value}},
            {"type": "led", "id": "LED1", "attrs": {"value": "红"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "R1:1", "red", []],
            ["R1:2", "LED1:+", "red", []],
            ["LED1:-", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }


def test_led_overcurrent_danger():
    iss = check_led_ratings(_led_circuit("10Ω"))
    assert any(i.severity == "danger" for i in iss), iss


def test_led_safe_no_alert():
    iss = check_led_ratings(_led_circuit("330Ω"))
    assert not any(i.severity == "danger" for i in iss), iss


def test_led_12v_danger():
    iss = check_led_ratings(_led_circuit("10Ω", vcc="12V"))
    assert any(i.severity == "danger" for i in iss)


def test_regulator_low_vin():
    c = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "regulator_7805", "id": "U1"},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "U1:IN", "red", []],
            ["U1:GND", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }
    iss = check_regulator_ratings(c)
    assert any("输入偏低" in i.rule or "低于" in i.message for i in iss), iss


def test_resistor_power_danger():
    # 5V LED + 10Ω → I≈0.3A, P≈0.9W >> 0.25W
    iss = check_resistor_power(_led_circuit("10Ω"))
    assert any(i.severity == "danger" for i in iss), iss


def test_run_all():
    iss = run_rating_checks(_led_circuit("10Ω"))
    assert len(iss) >= 1


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
