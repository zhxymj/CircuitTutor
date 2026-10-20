# -*- coding: utf-8 -*-
"""SPICE 仿真骨架测试（deck 注入 / 解析解 / AC 幅频 / ngspice 可选）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.spice_enhanced import (  # noqa: E402
    build_analysis_deck,
    ac_response,
    analyze_simple,
    simulate,
    find_ngspice,
    parse_ngspice_op,
    ANALYSIS_KINDS,
)


def _led():
    return {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "330"}},
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


def _rc():
    return {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "1k"}},
            {"type": "capacitor", "id": "C1", "attrs": {"value": "100nF"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "R1:1", "red", []],
            ["R1:2", "C1:+", "red", []],
            ["C1:-", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }


def test_kinds_constant():
    assert set(ANALYSIS_KINDS) == {"op", "ac", "tran", "dc"}


def test_op_deck():
    deck = build_analysis_deck(_led(), kind="op")
    assert ".op" in deck
    assert deck.strip().endswith(".end")
    assert "V1" in deck and "R1" in deck


def test_ac_deck_injects_stimulus():
    deck = build_analysis_deck(_led(), kind="ac")
    assert ".ac" in deck
    assert "AC 1" in deck
    # 不应残留纯 DC 源行（已改写）
    lines = [ln for ln in deck.splitlines() if ln.startswith("V1 ")]
    assert lines and "AC 1" in lines[0]


def test_tran_deck_injects_pulse():
    deck = build_analysis_deck(_led(), kind="tran")
    assert ".tran" in deck
    assert "PULSE" in deck


def test_unknown_kind_falls_back_op():
    deck = build_analysis_deck(_led(), kind="bogus")
    assert ".op" in deck


def test_analytic_led_current():
    r = analyze_simple(_led())
    kinds = {i["kind"] for i in r["items"]}
    assert "led_current" in kinds
    led = next(i for i in r["items"] if i["kind"] == "led_current")
    # (5-2)/330 ≈ 9.1 mA
    assert 8 < led["i_ma"] < 11


def test_ac_response_rc_lowpass():
    acr = ac_response(_rc())
    assert acr["engine"] == "analytic_rc"
    assert acr["topology"] == "lowpass"
    # fc = 1/(2π·1000·100e-9) ≈ 1591.5
    assert 1400 < acr["fc_hz"] < 1800
    # 低频增益接近 0 dB，高频衰减
    pts = acr["points"]
    assert pts[0]["gain_db"] > -1
    assert pts[-1]["gain_db"] < -10


def test_simulate_unified():
    r = simulate(_led(), kind="op")
    assert r["kind"] == "op"
    assert r["engine"] in ("ngspice", "analytic")
    assert "deck" in r and "analytic" in r
    assert isinstance(r["ngspice_available"], bool)


def test_parse_ngspice_op():
    out = "v(n1) = 2.500000e+00\nv(n2) = 4.700000e+00\nnoise"
    v = parse_ngspice_op(out)
    assert abs(v["n1"] - 2.5) < 1e-9
    assert abs(v["n2"] - 4.7) < 1e-9


def test_find_ngspice_no_crash():
    # 不要求本机装了 ngspice，只要不抛
    p = find_ngspice()
    assert p is None or isinstance(p, str)


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
    print(f"spice_enhanced: {len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
