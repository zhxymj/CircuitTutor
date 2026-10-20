# -*- coding: utf-8 -*-
"""BOM 与规格库测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import bom, specs  # noqa: E402


def _sample():
    return {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "330Ω"}},
            {"type": "resistor", "id": "R2", "attrs": {"value": "330Ω"}},
            {"type": "resistor", "id": "R3", "attrs": {"value": "10k"}},
            {"type": "led", "id": "LED1", "attrs": {"value": "红"}},
            {"type": "gnd", "id": "GND1"},
            {"type": "vcc", "id": "V1"},
        ],
        "connections": [],
    }


def test_specs():
    assert specs.package_of("led") == "LED 5mm"
    assert specs.get_spec("ne555")["v_max"] >= 12
    assert specs.get_spec("regulator_7805")["v_out"] == 5.0
    assert specs.get_spec("unknown_xyz") == {}


def test_bom_merge():
    rows = bom.to_rows(_sample())
    # gnd/vcc 排除；330Ω 两颗合并
    types = {r["type"]: r for r in rows}
    assert "gnd" not in types and "vcc" not in types
    assert types["resistor"]["qty"] >= 2 or any(
        r["qty"] == 2 and r["value"] == "330Ω" for r in rows)


def test_bom_csv():
    text = bom.to_csv(_sample())
    assert text.startswith("﻿") or "序号" in text
    assert "330Ω" in text
    assert "LED" in text.lower() or "led" in text


def test_bom_json():
    d = bom.to_json(_sample())
    assert d["kind"] == "bom"
    assert d["total_parts"] >= 4


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
