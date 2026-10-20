# -*- coding: utf-8 -*-
"""轻量电路块启发式识别（教学解释用，保守措辞）。

只做「疑似/常见用法」提示，不替代 ERC；失败不得影响主流程。
"""

from __future__ import annotations

from typing import Any, Dict, List


def _parts(circuit: dict) -> List[dict]:
    return [p for p in (circuit or {}).get("parts") or [] if isinstance(p, dict)]


def _types(circuit: dict) -> set:
    return {str(p.get("type") or "") for p in _parts(circuit)}


def _has(circuit: dict, *types: str) -> bool:
    ts = _types(circuit)
    return any(t in ts for t in types)


def analyze_blocks(circuit: dict) -> List[Dict[str, Any]]:
    """返回 [{key,title,parts,explain}]，文案保守。"""
    out: List[Dict[str, Any]] = []
    if not circuit:
        return out
    parts = _parts(circuit)
    ids = [str(p.get("id")) for p in parts if p.get("id")]

    if _has(circuit, "led") and _has(circuit, "resistor") and _has(circuit, "battery", "vcc"):
        out.append({
            "key": "led_limit",
            "title": "LED 指示 + 限流（常见用法）",
            "parts": [i for i in ids if i.upper().startswith(("LED", "R", "BAT"))][:6],
            "explain": "电池/电源经限流电阻驱动 LED，是教学中最常见的指示灯接法；电流需落在器件允许范围。",
        })
    if _has(circuit, "capacitor", "capacitor_ceramic") and _has(circuit, "atmega328", "esp32", "ne555", "regulator_7805", "lm393", "lm324"):
        out.append({
            "key": "decoupling",
            "title": "疑似电源去耦",
            "parts": [i for i in ids if i.upper().startswith(("C", "U", "IC"))][:6],
            "explain": "芯片附近出现电容时，常见用途是 VCC 去耦（如 100nF 就近放置）；具体容值与位置请对照数据手册。",
        })
    if _has(circuit, "bmp280", "mpu6050", "dht11", "ds18b20", "i2c_header") or any(
        "SCL" in str((p.get("attrs") or {})) or "SDA" in str((p.get("attrs") or {})) for p in parts
    ):
        # I2C 上拉：resistor + sensor/mcu
        if _has(circuit, "resistor") and _has(circuit, "bmp280", "mpu6050", "atmega328", "esp32", "i2c_header"):
            out.append({
                "key": "i2c_pullup",
                "title": "疑似 I2C 上拉/接口（常见用法）",
                "parts": [i for i in ids if i.upper().startswith(("R", "U", "J", "MOD"))][:8],
                "explain": "I2C 总线通常在 SDA/SCL 各接上拉电阻到逻辑电源（常见 2.2k～10k）；本提示为启发式，不是 ERC 判定。",
            })
        else:
            out.append({
                "key": "i2c_bus",
                "title": "疑似 I2C 传感/接口场景",
                "parts": ids[:8],
                "explain": "需求或元件出现 I2C 传感/接口特征；请确认上拉、去耦与连接器是否齐备。",
            })
    if _has(circuit, "regulator_7805", "buck_module", "boost_module", "ldo_3v3", "lm317"):
        out.append({
            "key": "power_stage",
            "title": "电源变换级（常见用法）",
            "parts": [i for i in ids if any(x in i.upper() for x in ("U", "REG", "PS", "MOD"))][:6],
            "explain": "检测到稳压/DC-DC 类器件，常见用途是产生系统电源轨；输入输出电容与散热需按手册确认。",
        })
    if _has(circuit, "conn_header", "screw_terminal", "usb_port", "barrel_jack", "uart_header", "spi_header", "i2c_header"):
        out.append({
            "key": "connector",
            "title": "外部接口/连接器",
            "parts": [i for i in ids if i.upper().startswith(("J", "CN", "USB", "P"))][:6],
            "explain": "板级连接器用于对外供电或信号引出；引脚定义与防呆/ESD 需在详细设计中确认。",
        })
    return out
