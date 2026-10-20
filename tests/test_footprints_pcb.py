# -*- coding: utf-8 -*-
"""KiCad 封装映射 + PCB 预布局测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.kicad_footprints import (  # noqa: E402
    footprint_of, footprint_for_part, FOOTPRINTS,
)
from core.pcb_layout import (  # noqa: E402
    auto_board_size, place_parts, summary, to_kicad_pcb,
)


def _circuit():
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


def test_footprint_map_basic():
    assert footprint_of("resistor").startswith("Resistor_THT:")
    assert footprint_of("led").startswith("LED_THT:")
    assert footprint_of("ne555") == "Package_DIP:DIP-8_W7.62mm"
    assert footprint_of("regulator_7805").startswith("Package_TO_SOT_THT:")


def test_footprint_smd_override():
    assert footprint_of("resistor", "SMD 0805") == "Resistor_SMD:R_0805_2012Metric"
    assert footprint_of("capacitor", "0603") == "Capacitor_SMD:C_0805_2012Metric"


def test_footprint_unknown_empty():
    assert footprint_of("nonexistent_type") == ""
    assert footprint_of("vcc") == ""
    assert footprint_of("gnd") == ""


def test_footprint_for_part():
    p = {"type": "led", "attrs": {"value": "红"}}
    assert footprint_for_part(p).startswith("LED_THT:")
    p2 = {"type": "resistor", "attrs": {"package": "SMD 0805"}}
    assert "SMD" in footprint_for_part(p2)


def test_footprint_map_coverage():
    # 至少 100 种有效映射
    n = sum(1 for v in FOOTPRINTS.values() if v)
    assert n >= 100, n


def test_auto_board_size():
    w, h = auto_board_size(1)
    assert w >= 40 and h >= 30
    w2, h2 = auto_board_size(20)
    assert w2 > w or h2 > h


def test_place_parts_skips_anchors():
    placed = place_parts(_circuit())
    refs = {p["ref"] for p in placed}
    assert "GND1" not in refs
    assert "BAT1" in refs and "R1" in refs and "LED1" in refs


def test_place_parts_has_footprints():
    placed = place_parts(_circuit())
    for p in placed:
        assert p["fp"], f"{p['ref']} 缺封装"


def test_summary():
    s = summary(_circuit())
    assert s["parts"] == 3
    assert s["with_footprint"] == 3
    assert s["coverage"] == 100.0
    assert s["board_w_mm"] >= 40


def test_to_kicad_pcb():
    pcb = to_kicad_pcb(_circuit(), "test")
    assert "(kicad_pcb" in pcb
    assert "Edge.Cuts" in pcb
    assert "LED1" in pcb
    assert "R1" in pcb
    assert pcb.strip().endswith(")")
    # 版本号
    assert "20240108" in pcb


def test_to_kicad_pcb_empty():
    pcb = to_kicad_pcb({"parts": [], "connections": []}, "empty")
    assert "(kicad_pcb" in pcb
    assert "Edge.Cuts" in pcb


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
    print(f"footprints+pcb: {len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
