# -*- coding: utf-8 -*-
"""SPICE 导出测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.spice import to_cir, to_subckt  # noqa: E402


def _led():
    return {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "330"}},
            {"type": "led", "id": "LED1", "attrs": {"value": "red"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "R1:1", "red", []],
            ["R1:2", "LED1:+", "red", []],
            ["LED1:-", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }


def test_cir_basic():
    cir = to_cir(_led())
    assert "V1" in cir
    assert "R1" in cir
    assert "DLED1" in cir
    assert ".end" in cir
    assert ".model LEDMOD" in cir


def test_cir_ohm_value():
    cir = to_cir(_led())
    # 330 should appear as 330 (not 330Ω)
    assert "330" in cir
    assert "330Ω" not in cir


def test_subckt():
    sc = to_subckt(_led(), "LEDCIR")
    assert ".subckt LEDCIR" in sc
    assert ".ends LEDCIR" in sc
    # .ends 含 ".end" 子串，故检查独立 .end 行
    assert not any(ln.strip() == ".end" for ln in sc.splitlines())


def test_cap_value():
    c = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "9V"}},
            {"type": "capacitor", "id": "C1", "attrs": {"value": "100nF"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "10k"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "R1:1", "red", []],
            ["R1:2", "C1:+", "red", []],
            ["C1:-", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }
    cir = to_cir(c)
    assert "C1" in cir
    assert "100n" in cir  # 100nF -> 100n


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
