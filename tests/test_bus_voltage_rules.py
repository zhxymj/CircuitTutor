# -*- coding: utf-8 -*-
"""I²C 总线与电源轨兼容性 ERC 行为回归。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.catalog import build_erc_kb  # noqa: E402
from core.erc import run_erc  # noqa: E402


KB = build_erc_kb()


def _rules(circuit):
    return [issue.rule for issue in run_erc(circuit, KB).issues]


def _i2c_circuit(address1="0x76", address2="0x77", pullups=False):
    parts = [
        {"type": "battery", "id": "BAT1", "attrs": {"value": "3.3V"}},
        {"type": "gnd", "id": "GND1"},
        {"type": "i2c_header", "id": "J1"},
        {"type": "bmp280", "id": "U1", "attrs": {"i2c_address": address1}},
        {"type": "bmp280", "id": "U2", "attrs": {"i2c_address": address2}},
    ]
    connections = [
        ["BAT1:+", "J1:VCC"],
        ["J1:VCC", "U1:VCC"],
        ["J1:VCC", "U2:VCC"],
        ["BAT1:-", "GND1:1"],
        ["GND1:1", "J1:GND"],
        ["GND1:1", "U1:GND"],
        ["GND1:1", "U2:GND"],
        ["J1:SCL", "U1:SCL"],
        ["J1:SCL", "U2:SCL"],
        ["J1:SDA", "U1:SDA"],
        ["J1:SDA", "U2:SDA"],
    ]
    if pullups:
        parts.extend([
            {"type": "resistor", "id": "R_SCL", "attrs": {"value": "4.7kΩ"}},
            {"type": "resistor", "id": "R_SDA", "attrs": {"value": "4.7kΩ"}},
        ])
        connections.extend([
            ["J1:SCL", "R_SCL:1"],
            ["R_SCL:2", "BAT1:+"],
            ["J1:SDA", "R_SDA:1"],
            ["R_SDA:2", "BAT1:+"],
        ])
    return {"title": "I2C 总线", "parts": parts, "connections": connections}


def _voltage_circuit(supply="5V", with_limits=True):
    attrs = {}
    if with_limits:
        attrs.update({"supply_min_v": "1.7V", "supply_max_v": "3.6V"})
    return {
        "title": "显式电压域样本",
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": supply}},
            {"type": "gnd", "id": "GND1"},
            {"type": "bmp280", "id": "U1", "attrs": attrs},
        ],
        "connections": [
            ["BAT1:+", "U1:VCC"],
            ["BAT1:-", "GND1:1"],
            ["GND1:1", "U1:GND"],
        ],
    }


def test_i2c_bus_without_pullups_is_reported():
    assert "I2C缺上拉" in _rules(_i2c_circuit(pullups=False))


def test_i2c_bus_with_two_real_pullups_is_not_reported():
    assert "I2C缺上拉" not in _rules(_i2c_circuit(pullups=True))


def test_same_explicit_address_on_same_bus_is_reported():
    assert "总线地址冲突" in _rules(
        _i2c_circuit(address1="0x76", address2="0x76", pullups=True)
    )


def test_different_or_unknown_addresses_are_not_reported():
    assert "总线地址冲突" not in _rules(
        _i2c_circuit(address1="0x76", address2="0x77", pullups=True)
    )
    assert "总线地址冲突" not in _rules(
        _i2c_circuit(address1="auto", address2=None, pullups=True)
    )


def test_explicit_out_of_range_supply_is_reported():
    assert "电源轨不兼容" in _rules(_voltage_circuit("5V", with_limits=True))


def test_in_range_or_unspecified_limits_are_not_reported():
    assert "电源轨不兼容" not in _rules(_voltage_circuit("3.3V", with_limits=True))
    assert "电源轨不兼容" not in _rules(_voltage_circuit("5V", with_limits=False))


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"[PASS] {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {test.__name__}: {exc}")
    print(f"结果：{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
