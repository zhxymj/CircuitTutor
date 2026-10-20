# -*- coding: utf-8 -*-
"""
simulate.py —— 准仿真引擎（教学向直流工作点，零第三方依赖）
================================================================
工业级产品需要「画得对」之外再回答：电流多大、电压多少、会不会过热。
本模块提供**确定性、可测试、不调用 SPICE 引擎**的直流近似：

* LED 串联支路电流：I = (Vcc − Vf) / R
* 电阻分压：Vout = Vin · R2 / (R1+R2)
* 线性稳压（7805 类）：P ≈ (Vin − Vout) · Iout
* 功耗与安全裕量：对照器件规格库（catalog 或 design_rules 默认表）

输出 SimulationReport，供 server / 前端 / CLI 展示。
这不是 SPICE 替代品；目标是教学与设计阶段的快速自检。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    from core.design_rules import (
        detect_supply_v,
        led_vf,
        parse_ohm,
        _pin_pairs,
    )
except ImportError:  # 允许脱离项目单测
    detect_supply_v = None
    led_vf = lambda x: 2.0
    parse_ohm = None
    _pin_pairs = None


@dataclass
class SimIssue:
    severity: str  # info | warn | danger
    rule: str
    message: str
    pins: List[str] = field(default_factory=list)


@dataclass
class SimulationReport:
    passed: bool
    issues: List[SimIssue]
    led_currents: List[Dict[str, Any]]
    dividers: List[Dict[str, Any]]
    regulator: List[Dict[str, Any]]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "issues": [
                {"severity": i.severity, "rule": i.rule, "message": i.message, "pins": i.pins}
                for i in self.issues
            ],
            "led_currents": self.led_currents,
            "dividers": self.dividers,
            "regulator": self.regulator,
        }


# LED 教学安全区间（mA）
LED_I_MIN_MA = 1.0
LED_I_MAX_MA = 20.0
LED_I_TYP_MA = 10.0
LED_I_DANGER_MA = 30.0


def _parts_by_type(circuit: dict) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for p in circuit.get("parts") or []:
        if isinstance(p, dict):
            out.setdefault(str(p.get("type") or ""), []).append(p)
    return out


def _parse_vf_and_r(led: dict, rpart: dict) -> Tuple[float, Optional[float]]:
    color = str((led.get("attrs") or {}).get("value") or "")
    vf = led_vf(color)
    r = parse_ohm((rpart.get("attrs") or {}).get("value")) if parse_ohm else None
    return vf, r


def simulate_led_branches(circuit: dict) -> Tuple[List[dict], List[SimIssue]]:
    """扫描 LED—串联电阻，计算工作电流。"""
    if detect_supply_v is None or _pin_pairs is None:
        return [], []
    notes: List[SimIssue] = []
    results: List[dict] = []
    by = _parts_by_type(circuit)
    pairs = _pin_pairs(circuit)
    vcc, vcc_src = detect_supply_v(circuit)
    if not vcc:
        vcc = 5.0
        vcc_src = "默认5V"
    leds = by.get("led") or []
    resistors = {str(r.get("id")): r for r in (by.get("resistor") or [])}
    for led in leds:
        lid = str(led.get("id"))
        # 找直接串联电阻
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
            notes.append(SimIssue(
                "warn", "LED无串联电阻",
                f"LED {lid} 未检测到直接串联电阻，无法计算工作电流（请检查限流）",
                [lid]))
            continue
        vf, r = _parse_vf_and_r(led, resistors[rid])
        if not r or r <= 0:
            notes.append(SimIssue(
                "warn", "限流电阻不可解析",
                f"LED {lid} 的限流电阻 {rid} 值无效，跳过电流估算", [lid, rid]))
            continue
        i_ma = (vcc - vf) / r * 1000.0 if vcc > vf else 0.0
        entry = {
            "led": lid, "resistor": rid, "vcc": vcc, "vcc_src": vcc_src,
            "vf": vf, "r": r, "i_ma": round(i_ma, 2),
            "status": "ok",
        }
        if i_ma >= LED_I_DANGER_MA:
            entry["status"] = "danger"
            notes.append(SimIssue(
                "danger", "LED过流",
                f"LED {lid} 电流约 {i_ma:.1f}mA，超过 {LED_I_DANGER_MA:.0f}mA，可能烧毁；"
                f"建议增大 {rid} 至 {(vcc - vf) / 0.01:.0f}Ω 量级",
                [lid, rid]))
        elif i_ma > LED_I_MAX_MA:
            entry["status"] = "warn"
            notes.append(SimIssue(
                "warn", "LED电流偏大",
                f"LED {lid} 电流约 {i_ma:.1f}mA，高于教学常用上限 {LED_I_MAX_MA:.0f}mA",
                [lid, rid]))
        elif i_ma < LED_I_MIN_MA:
            entry["status"] = "warn"
            notes.append(SimIssue(
                "warn", "LED电流偏小",
                f"LED {lid} 电流约 {i_ma:.2f}mA，可能几乎不亮", [lid, rid]))
        results.append(entry)
    return results, notes


def simulate_dividers(circuit: dict) -> Tuple[List[dict], List[SimIssue]]:
    """检测两电阻共网构成的分压并估算中点电压。"""
    if detect_supply_v is None or parse_ohm is None:
        return [], []
    notes: List[SimIssue] = []
    results: List[dict] = []
    by = _parts_by_type(circuit)
    resistors = by.get("resistor") or []
    if len(resistors) < 2:
        return [], []
    vcc, _ = detect_supply_v(circuit)
    if not vcc:
        vcc = 5.0
    # 两电阻是否有公共对端（中点）
    def endpoints(r):
        rid = str(r.get("id"))
        return {a for a, b in _pin_pairs(circuit) if a.startswith(rid + ":")} | \
               {b for a, b in _pin_pairs(circuit) if b.startswith(rid + ":")}

    seen = set()
    for i, r1 in enumerate(resistors):
        for r2 in resistors[i + 1:]:
            id1, id2 = str(r1.get("id")), str(r2.get("id"))
            e1, e2 = endpoints(r1), endpoints(r2)
            mid = e1 & e2
            if not mid:
                continue
            key = tuple(sorted((id1, id2)))
            if key in seen:
                continue
            seen.add(key)
            v1 = parse_ohm((r1.get("attrs") or {}).get("value"))
            v2 = parse_ohm((r2.get("attrs") or {}).get("value"))
            if not v1 or not v2:
                continue
            r_hi, r_lo = (v1, v2) if v1 >= v2 else (v2, v1)
            vout = vcc * r_lo / (r_hi + r_lo)
            results.append({
                "r_top": id1 if v1 >= v2 else id2,
                "r_bot": id2 if v1 >= v2 else id1,
                "r_top_ohm": r_hi, "r_bot_ohm": r_lo,
                "vin": vcc, "vout": round(vout, 3),
            })
            notes.append(SimIssue(
                "info", "分压工作点",
                f"分压 {results[-1]['r_top']}/{results[-1]['r_bot']}："
                f"{vcc:g}V → 中点约 {vout:.2f}V",
                [results[-1]["r_top"], results[-1]["r_bot"]]))
    return results, notes


def simulate_regulators(circuit: dict) -> Tuple[List[dict], List[SimIssue]]:
    """7805 类线性稳压：压差与功耗提示（Iout 需用户场景，默认 100mA 估算）。"""
    if detect_supply_v is None:
        return [], []
    notes: List[SimIssue] = []
    results: List[dict] = []
    by = _parts_by_type(circuit)
    regs = by.get("regulator_7805") or []
    if not regs:
        return [], []
    vin, _ = detect_supply_v(circuit)
    if not vin:
        vin = 9.0
    vout_nom = 5.0
    iout_typ = 0.1  # 教学估算
    for reg in regs:
        rid = str(reg.get("id"))
        drop = max(0.0, vin - vout_nom)
        p_w = drop * iout_typ
        entry = {
            "reg": rid, "vin": vin, "vout_nom": vout_nom,
            "dropout_v": round(drop, 2), "iout_a": iout_typ,
            "p_w": round(p_w, 3),
        }
        results.append(entry)
        if vin < vout_nom + 2:
            notes.append(SimIssue(
                "warn", "稳压器压差不足",
                f"{rid} 输入约 {vin:g}V，7805 通常需要 ≥7V 才能稳到 5V", [rid]))
        elif p_w >= 0.5:
            notes.append(SimIssue(
                "warn", "线性稳压功耗偏大",
                f"{rid} 估算功耗约 {p_w:.2f}W（{(vin - vout_nom):.1f}V×{iout_typ * 1000:.0f}mA），"
                f"教学场景建议加散热或改开关电源", [rid]))
        else:
            notes.append(SimIssue(
                "info", "稳压工作点",
                f"{rid}：Vin≈{vin:g}V → 5V，压差 {drop:.1f}V，"
                f"功耗约 {p_w:.2f}W @ {iout_typ * 1000:.0f}mA", [rid]))
    return results, notes


def run_simulation(circuit: dict) -> SimulationReport:
    """直流准仿真入口。"""
    if not isinstance(circuit, dict) or not circuit.get("parts"):
        return SimulationReport(True, [], [], [], [])
    led_r, led_i = simulate_led_branches(circuit)
    div_r, div_i = simulate_dividers(circuit)
    reg_r, reg_i = simulate_regulators(circuit)
    issues = led_i + div_i + reg_i
    danger = [i for i in issues if i.severity == "danger"]
    return SimulationReport(
        passed=not danger,
        issues=issues,
        led_currents=led_r,
        dividers=div_r,
        regulator=reg_r,
    )


if __name__ == "__main__":
    # 自检
    c = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "330Ω"}},
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
    rep = run_simulation(c)
    print("passed", rep.passed)
    print("led", rep.led_currents)
    assert rep.led_currents and 8 < rep.led_currents[0]["i_ma"] < 11
    print("simulate self-check ok")
