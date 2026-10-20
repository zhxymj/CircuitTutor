# -*- coding: utf-8 -*-
"""
erc_ratings.py —— ERC 额定值告警（工业级：能算且能拦）
=========================================================
用 core/specs.py 的额定值对照电路实际工况：

* LED 电流 > i_max_ma        → danger（烧毁风险）
* LED 电流 > 20mA 且 ≤ max   → warn（偏大）
* 电阻功耗 > p_max_mw        → danger / warn
* 7805 输入压差与功耗         → warn
* 电容耐压不足               → warn（若 specs 给了 v_max）

接入：erc.run_erc 之后或 design_rules.apply_precision 之后调用
run_rating_checks(circuit, erc_kb) → List[SimIssue]
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

try:
    from core.design_rules import (
        detect_supply_v,
        led_vf,
        parse_ohm,
        _pin_pairs,
    )
    from core.specs import get_spec
    from core.simulate import (
        LED_I_DANGER_MA,
        LED_I_MAX_MA,
        _parts_by_type,
    )
except ImportError:
    detect_supply_v = None
    led_vf = lambda x: 2.0
    parse_ohm = None
    _pin_pairs = None

    def get_spec(t):
        return {}

    def _parts_by_type(c):
        return {}

    LED_I_DANGER_MA = 30.0
    LED_I_MAX_MA = 20.0


try:
    from core.simulate import SimIssue
except ImportError:
    class SimIssue:
        def __init__(self, severity, rule, message, pins=None):
            self.severity = severity
            self.rule = rule
            self.message = message
            self.pins = pins or []


def _parse_r(p: dict) -> Optional[float]:
    return parse_ohm((p.get("attrs") or {}).get("value")) if parse_ohm else None


def check_led_ratings(circuit: dict) -> List[SimIssue]:
    """LED 额定电流检查。"""
    issues: List[SimIssue] = []
    if detect_supply_v is None or _pin_pairs is None:
        return issues
    by = _parts_by_type(circuit)
    pairs = _pin_pairs(circuit)
    vcc, _ = detect_supply_v(circuit) or (5.0, "")
    leds = by.get("led") or []
    resistors = {str(r.get("id")): r for r in (by.get("resistor") or [])}
    spec = get_spec("led")
    i_max = float(spec.get("i_max_ma") or LED_I_MAX_MA)

    for led in leds:
        lid = str(led.get("id"))
        rid = None
        for a, b in pairs:
            for x, y in ((a, b), (b, a)):
                px, py = x.split(":", 1)[0], y.split(":", 1)[0]
                if px == lid and py in resistors:
                    rid = py
                    break
            if rid:
                break
        if not rid:
            continue
        r = _parse_r(resistors[rid])
        if not r or r <= 0:
            continue
        vf = led_vf((led.get("attrs") or {}).get("value"))
        if vcc <= vf:
            continue
        i_ma = (vcc - vf) / r * 1000.0
        if i_ma >= LED_I_DANGER_MA or i_ma > i_max:
            issues.append(SimIssue(
                "danger", "LED超额定电流",
                f"LED {lid} 电流约 {i_ma:.1f}mA，超过额定 {i_max:.0f}mA，存在烧毁风险；"
                f"建议增大 {rid}（目标约 {(vcc - vf) / 0.01:.0f}Ω @10mA）",
                [lid, rid]))
        elif i_ma > LED_I_MAX_MA:
            issues.append(SimIssue(
                "warn", "LED电流偏大",
                f"LED {lid} 电流约 {i_ma:.1f}mA，高于教学常用 {LED_I_MAX_MA:.0f}mA 上限",
                [lid, rid]))
    return issues


def check_resistor_power(circuit: dict) -> List[SimIssue]:
    """电阻功耗：P = I²R；无电流测量时按 LED 支路电流或 10mA 保守估算。"""
    issues: List[SimIssue] = []
    if detect_supply_v is None or parse_ohm is None:
        return issues
    by = _parts_by_type(circuit)
    pairs = _pin_pairs(circuit)
    vcc, _ = detect_supply_v(circuit) or (5.0, "")
    spec_r = get_spec("resistor")
    p_max_mw = float(spec_r.get("p_max_mw") or 250)

    # 与 LED 串联的电阻用真实电流
    leds = by.get("led") or []
    resistors = {str(r.get("id")): r for r in (by.get("resistor") or [])}
    checked = set()
    for led in leds:
        lid = str(led.get("id"))
        rid = None
        for a, b in pairs:
            for x, y in ((a, b), (b, a)):
                px, py = x.split(":", 1)[0], y.split(":", 1)[0]
                if px == lid and py in resistors:
                    rid = py
                    break
            if rid:
                break
        if not rid:
            continue
        r = _parse_r(resistors[rid])
        if not r:
            continue
        vf = led_vf((led.get("attrs") or {}).get("value"))
        if vcc <= vf:
            continue
        i_a = (vcc - vf) / r
        p_mw = i_a * i_a * r * 1000.0
        checked.add(rid)
        if p_mw > p_max_mw * 1.5:
            issues.append(SimIssue(
                "danger", "电阻功耗超额定",
                f"电阻 {rid} 估算功耗 {p_mw:.0f}mW，超过额定 {p_max_mw:.0f}mW，可能烧毁",
                [rid]))
        elif p_mw > p_max_mw * 0.8:
            issues.append(SimIssue(
                "warn", "电阻功耗偏高",
                f"电阻 {rid} 估算功耗 {p_mw:.0f}mW，接近额定 {p_max_mw:.0f}mW",
                [rid]))

    # 其余电阻按最坏 10mA 粗估（教学安全提示）
    for rid, rpart in resistors.items():
        if rid in checked:
            continue
        r = _parse_r(rpart)
        if not r or r <= 0:
            continue
        i_a = 0.01
        p_mw = i_a * i_a * r * 1000.0
        if p_mw > p_max_mw * 2:
            issues.append(SimIssue(
                "warn", "大阻值电阻功耗提示",
                f"电阻 {rid}（{r:g}Ω）在约 10mA 下功耗可达 {p_mw:.0f}mW，"
                f"请确认额定功率 ≥{p_max_mw:.0f}mW",
                [rid]))
    return issues


def check_regulator_ratings(circuit: dict) -> List[SimIssue]:
    """7805 输入压差与功耗。"""
    issues: List[SimIssue] = []
    if detect_supply_v is None:
        return issues
    by = _parts_by_type(circuit)
    regs = by.get("regulator_7805") or []
    if not regs:
        return issues
    vin, _ = detect_supply_v(circuit) or (9.0, "")
    spec = get_spec("regulator_7805")
    v_in_min = float(spec.get("v_in_min") or 7.0)
    v_out = float(spec.get("v_out") or 5.0)
    for reg in regs:
        rid = str(reg.get("id"))
        if vin < v_in_min:
            issues.append(SimIssue(
                "warn", "稳压器输入偏低",
                f"{rid} 输入约 {vin:g}V，低于典型最低 {v_in_min:g}V，可能无法稳压到 {v_out:g}V",
                [rid]))
        # 假设 100mA 负载
        p_w = max(0.0, (vin - v_out) * 0.1)
        if p_w >= 0.5:
            issues.append(SimIssue(
                "warn", "线性稳压功耗偏大",
                f"{rid} 按 100mA 估算功耗约 {p_w:.2f}W（{(vin - v_out):.1f}V 压差），"
                f"注意散热或改用开关电源",
                [rid]))
    return issues


def check_cap_voltage(circuit: dict) -> List[SimIssue]:
    """电容耐压：若供电电压接近/超过额定 v_max 则提示。"""
    issues: List[SimIssue] = []
    if detect_supply_v is None:
        return issues
    vcc, _ = detect_supply_v(circuit) or (5.0, "")
    by = _parts_by_type(circuit)
    for ctype in ("capacitor", "electrolytic", "capacitor_ceramic",
                  "capacitor_film", "capacitor_tantalum"):
        for cap in by.get(ctype) or []:
            spec = get_spec(ctype) or get_spec("capacitor")
            v_max = spec.get("v_max")
            if not v_max:
                continue
            # 教学经验：工作电压不超过额定的 70%
            if vcc > float(v_max) * 0.7:
                cid = str(cap.get("id"))
                issues.append(SimIssue(
                    "warn", "电容耐压可能不足",
                    f"{cid} 供电约 {vcc:g}V，接近额定耐压 {v_max}V（建议工作电压≤70% 额定）",
                    [cid]))
    return issues


def run_rating_checks(circuit: dict) -> List[SimIssue]:
    """额定值检查入口。"""
    if not isinstance(circuit, dict) or not circuit.get("parts"):
        return []
    issues: List[SimIssue] = []
    try:
        issues.extend(check_led_ratings(circuit))
    except Exception:
        pass
    try:
        issues.extend(check_resistor_power(circuit))
    except Exception:
        pass
    try:
        issues.extend(check_regulator_ratings(circuit))
    except Exception:
        pass
    try:
        issues.extend(check_cap_voltage(circuit))
    except Exception:
        pass
    return issues


if __name__ == "__main__":
    c = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "10Ω"}},
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
    iss = run_rating_checks(c)
    print("issues", [(i.severity, i.rule) for i in iss])
    assert any(i.severity == "danger" for i in iss), "10Ω LED 应触发 danger"
    print("erc_ratings self-check ok")
