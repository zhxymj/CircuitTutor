# -*- coding: utf-8 -*-
"""
spice_enhanced.py —— SPICE 仿真骨架（直流/交流/瞬态 + ngspice 可选）
==================================================================
在 spice.py 网表导出之上，补「能算」能力：

* build_analysis_deck(circuit, kind, **kw) -> str
    kind: 'dc' | 'ac' | 'tran' | 'op'
    生成含 .op/.ac/.tran/.dc 控制卡的完整网表；
    AC/自动注入交流激励，tran 自动注入脉冲激励。
* run_ngspice(deck) -> Optional[str]
    若本机有 ngspice CLI，真正跑一次仿真并返回输出文本
* analyze_simple(circuit) -> dict
    零依赖教学级解析解（RC 时间常数 / 分压 / LED 电流 / 7805 功耗）
* ac_response(circuit) -> dict
    纯 Python RC 一阶低通/高通幅频（ngspice 不可用时的教学兜底）
* simulate(circuit, kind, **kw) -> dict
    统一入口：优先 ngspice，失败回退解析解

用法：
    python -m core.spice_enhanced --kind tran
    python -m core.spice_enhanced --kind ac --circuit path.json
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from core.spice import to_cir, _build_pin_net_map, _spice_value
    from core.design_rules import detect_supply_v, led_vf, parse_ohm
    from core.simulate import _parts_by_type
except ImportError:
    to_cir = None
    _build_pin_net_map = None
    _spice_value = None
    detect_supply_v = None
    led_vf = lambda x: 2.0
    parse_ohm = None
    _parts_by_type = lambda c: {}

ANALYSIS_KINDS = ("op", "ac", "tran", "dc")


def _inject_source_stimulus(lines: List[str], kind: str) -> None:
    """为第一个电压源注入 AC/瞬态激励（教学默认）。"""
    kind = (kind or "").lower()
    if kind not in ("ac", "tran"):
        return
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.startswith("*") or s.startswith("."):
            continue
        # V1 node+ node- DC 5  /  V1 node+ node- 5
        m = re.match(r"^(V\d+)\s+(\S+)\s+(\S+)\s+(?:DC\s+)?(\S+)\s*$", s)
        if not m:
            continue
        name, nplus, nminus, dc = m.group(1), m.group(2), m.group(3), m.group(4)
        if kind == "ac":
            # .ac 扫频需要源带 AC 1
            lines[i] = f"{name} {nplus} {nminus} DC {dc} AC 1"
        else:
            # 瞬态：0→dc 的 1ms 上升沿方波，周期 20ms（50Hz 教学默认）
            lines[i] = (
                f"{name} {nplus} {nminus} PULSE(0 {dc} 1m 1u 1u 10m 20m)")
        return


def build_analysis_deck(circuit: dict, kind: str = "op",
                        title: str = "CircuitTutor",
                        ac_start: float = 1.0, ac_stop: float = 1e6,
                        ac_points: int = 11,
                        tran_step: float = 1e-4, tran_stop: float = 0.05,
                        dc_source: str = "V1", dc_start: float = 0.0,
                        dc_stop: float = 5.0, dc_step: float = 0.1) -> str:
    """生成带分析卡的 SPICE 网表。"""
    if to_cir is None:
        raise RuntimeError("spice.py 不可用")
    base = to_cir(circuit, title=title)
    lines = base.splitlines()
    # 去掉 .op/.end，后面重新加
    lines = [ln for ln in lines if not ln.strip().startswith((".op", ".end"))]
    kind = (kind or "op").lower()
    if kind not in ANALYSIS_KINDS:
        kind = "op"
    _inject_source_stimulus(lines, kind)
    if kind == "op":
        lines += [".op"]
    elif kind == "ac":
        lines += [f".ac dec {ac_points} {ac_start} {ac_stop}"]
    elif kind == "tran":
        lines += [f".tran {tran_step} {tran_stop}"]
    elif kind == "dc":
        lines += [f".dc {dc_source} {dc_start} {dc_stop} {dc_step}"]
    lines += [".end", ""]
    return "\n".join(lines)


def find_ngspice() -> Optional[str]:
    """查找 ngspice 可执行文件。"""
    env = os.environ.get("NGSPICE_PATH")
    if env and Path(env).is_file():
        return env
    for name in ("ngspice", "ngspice.exe"):
        p = shutil.which(name)
        if p:
            return p
    candidates = [
        r"C:\Program Files\ngspice\bin\ngspice.exe",
        r"C:\ngspice\bin\ngspice.exe",
        "/usr/bin/ngspice",
        "/usr/local/bin/ngspice",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    return None


def run_ngspice(deck: str, timeout_s: float = 30.0) -> Optional[str]:
    """若本机有 ngspice，跑仿真并返回输出；否则 None。"""
    exe = find_ngspice()
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as td:
        net = Path(td) / "deck.cir"
        net.write_text(deck, encoding="utf-8")
        try:
            r = subprocess.run(
                [exe, "-b", str(net)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout_s,
            )
            return (r.stdout or "") + (r.stderr or "")
        except Exception:
            return None


def parse_ngspice_op(output: str) -> Dict[str, float]:
    """从 ngspice 文本输出中粗提节点电压（教学用途）。"""
    volts: Dict[str, float] = {}
    if not output:
        return volts
    # 常见：v(n1) = 2.500000e+00
    for m in re.finditer(
            r"v\(([^)]+)\)\s*=\s*([-+0-9.eE]+)", output, re.IGNORECASE):
        try:
            volts[m.group(1).lower()] = float(m.group(2))
        except ValueError:
            pass
    return volts


def analyze_simple(circuit: dict) -> Dict[str, Any]:
    """零依赖教学级解析解（ngspice 不可用时兜底）。"""
    result: Dict[str, Any] = {"engine": "analytic", "items": []}
    if detect_supply_v is None:
        return result
    by = _parts_by_type(circuit)
    vcc, vcc_src = detect_supply_v(circuit) or (5.0, "默认5V")
    items = result["items"]

    # LED 电流
    for led in by.get("led") or []:
        lid = str(led.get("id"))
        rid = None
        pairs = []
        try:
            from core.design_rules import _pin_pairs
            pairs = _pin_pairs(circuit)
        except Exception:
            pass
        resistors = {str(r.get("id")): r for r in by.get("resistor") or []}
        for a, b in pairs:
            for x, y in ((a, b), (b, a)):
                px, py = x.split(":", 1)[0], y.split(":", 1)[0]
                if px == lid and py in resistors:
                    rid = py
                    break
            if rid:
                break
        if not rid or parse_ohm is None:
            continue
        r = parse_ohm((resistors[rid].get("attrs") or {}).get("value"))
        if not r:
            continue
        vf = led_vf((led.get("attrs") or {}).get("value"))
        if vcc <= vf:
            continue
        i_a = (vcc - vf) / r
        items.append({
            "kind": "led_current",
            "led": lid, "resistor": rid,
            "vcc": vcc, "vf": vf, "r": r,
            "i_ma": round(i_a * 1000, 2),
            "p_mw": round(i_a * i_a * r * 1000, 1),
        })

    # RC 时间常数
    caps = by.get("capacitor") or []
    res = by.get("resistor") or []
    if caps and res and parse_ohm is not None:
        try:
            r0 = parse_ohm((res[0].get("attrs") or {}).get("value"))
            c_raw = (caps[0].get("attrs") or {}).get("value")
            c_val = _parse_cap(c_raw)
            if r0 and c_val:
                tau = r0 * c_val
                items.append({
                    "kind": "rc_time_constant",
                    "r": r0, "c": c_val,
                    "tau_s": tau,
                    "tau_ms": round(tau * 1000, 4),
                    "fc_hz": round(1.0 / (2 * math.pi * tau), 1) if tau > 0 else None,
                })
        except Exception:
            pass

    # 分压
    if len(res) >= 2 and parse_ohm is not None:
        v1 = parse_ohm((res[0].get("attrs") or {}).get("value"))
        v2 = parse_ohm((res[1].get("attrs") or {}).get("value"))
        if v1 and v2:
            vout = vcc * min(v1, v2) / (v1 + v2)
            items.append({
                "kind": "divider",
                "r1": v1, "r2": v2, "vin": vcc,
                "vout": round(vout, 3),
            })

    # 7805
    for reg in by.get("regulator_7805") or []:
        items.append({
            "kind": "linear_regulator",
            "reg": str(reg.get("id")),
            "vin": vcc, "vout_nom": 5.0,
            "dropout": round(max(0, vcc - 5.0), 2),
            "p_w_at_100ma": round(max(0, vcc - 5.0) * 0.1, 3),
        })

    return result


def _parse_cap(c_raw: Any) -> Optional[float]:
    m = re.match(r"^\s*([0-9.]+)\s*([unpµμ]?)F?\s*$", str(c_raw or ""))
    if not m:
        return None
    num = float(m.group(1))
    pref = m.group(2) or ""
    mult = {"": 1.0, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "p": 1e-12}.get(pref, 1.0)
    return num * mult


def ac_response(circuit: dict, points: int = 11,
                f_start: float = 1.0, f_stop: float = 1e6
                ) -> Dict[str, Any]:
    """纯 Python 一阶 RC 幅频（低通/高通），ngspice 缺席时的教学仿真。

    拓扑启发式：
    * R 串入 + C 对地 → 低通 |H|=1/sqrt(1+(2πfRC)²)
    * C 串入 + R 对地 → 高通 |H|=(2πfRC)/sqrt(1+(2πfRC)²)
    返回扫频表 + fc。
    """
    if detect_supply_v is None or parse_ohm is None:
        return {"engine": "none", "kind": "ac", "points": []}
    by = _parts_by_type(circuit)
    res = by.get("resistor") or []
    caps = (by.get("capacitor") or []) + (by.get("electrolytic") or []) + \
           (by.get("capacitor_ceramic") or []) + (by.get("capacitor_film") or []) + \
           (by.get("capacitor_tantalum") or [])
    if not res or not caps:
        return {"engine": "none", "kind": "ac", "points": [],
                "note": "未检测到 R+C，无法做一阶 RC 幅频估算"}
    r = parse_ohm((res[0].get("attrs") or {}).get("value"))
    c = _parse_cap((caps[0].get("attrs") or {}).get("value"))
    if not r or not c:
        return {"engine": "none", "kind": "ac", "points": [],
                "note": "R/C 值不可解析"}
    tau = r * c
    fc = 1.0 / (2 * math.pi * tau) if tau > 0 else 0.0
    # 简单拓扑判断：电容第二端是否更靠近地
    topology = "lowpass"
    try:
        gnd_ids = {str(p.get("id")) for p in by.get("gnd") or []}
        cap_id = str(caps[0].get("id"))
        for conn in circuit.get("connections") or []:
            if not isinstance(conn, (list, tuple)) or len(conn) < 2:
                continue
            for a, b in ((conn[0], conn[1]), (conn[1], conn[0])):
                if not (isinstance(a, str) and isinstance(b, str)):
                    continue
                if a.startswith(cap_id + ":") and b.split(":", 1)[0] in gnd_ids:
                    topology = "lowpass"
                    break
                # 电阻对地 → 高通
                rid0 = str(res[0].get("id"))
                if a.startswith(rid0 + ":") and b.split(":", 1)[0] in gnd_ids:
                    topology = "highpass"
    except Exception:
        pass

    decades = max(1, min(8, int(points)))
    freqs: List[float] = []
    lo = math.log10(max(f_start, 1e-3))
    hi = math.log10(max(f_stop, f_start * 10))
    for i in range(decades + 1):
        freqs.append(10 ** (lo + (hi - lo) * i / max(1, decades)))
    pts = []
    for f in freqs:
        wrc = 2 * math.pi * f * tau
        mag = (1.0 / math.sqrt(1 + wrc * wrc)) if topology == "lowpass" \
            else (wrc / math.sqrt(1 + wrc * wrc))
        pts.append({
            "f_hz": round(f, 2),
            "gain_db": round(20 * math.log10(mag), 2) if mag > 0 else -120.0,
            "gain": round(mag, 5),
        })
    return {
        "engine": "analytic_rc",
        "kind": "ac",
        "topology": topology,
        "r": r, "c": c, "tau_s": tau,
        "fc_hz": round(fc, 2),
        "points": pts,
    }


# ------------------------------------------------------------------ 瞬态时间序列
# 供前端时序图（第三方 wavedrom 渲染器）使用的真实 .tran 数据。
# 批处理模式下 .tran 默认不打印波形，必须显式加 .print 控制卡；本模块只读取
# ngspice 的实际输出，不插值、不合成、不用解析解替代波形：跑不出来就返回
# ran=False 与具体原因，由调用方显示“未运行”，避免把示意波形当成仿真结果。

def _batch_node_names(output: str) -> List[str]:
    """从批处理输出的 Node Voltage 表提取节点名（排除支路电流）。"""
    names: List[str] = []
    in_table = False
    for raw in (output or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            in_table = False
            continue
        if re.match(r"^Node\s+Voltage$", stripped, re.IGNORECASE):
            in_table = True
            continue
        if not in_table:
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_#$]*)\s+([-+0-9.eE]+)$", stripped)
        if not m:
            continue
        name = m.group(1).lower()
        if "#branch" in name or name == "node":
            continue
        if name not in names:
            names.append(name)
    return names


def _parse_tran_table(output: str) -> tuple:
    """从 .print tran 输出解析 (列名, 数据行)；列名为节点名，行首列为时间。

    批处理输出每满一屏会重复打印一次表头（实测每 55 行），因此不能在第一处
    断行就停止：跳过空行与重复表头，并要求索引严格递增以排除其它分析段的行。
    """
    columns: List[str] = []
    rows: List[List[float]] = []
    lines = (output or "").splitlines()
    start = None
    for i, raw in enumerate(lines):
        if re.match(r"^\s*Index\s+time\b", raw, re.IGNORECASE):
            found = re.findall(r"v\(([^)]+)\)", raw)
            if found:
                columns, start = found, i + 1
                break
    if start is None:
        return [], []
    width = 2 + len(columns)
    last_index = -1
    for row in lines[start:]:
        parts = row.split()
        if len(parts) != width:
            continue
        try:
            index = int(parts[0])
            values = [float(x) for x in parts[1:]]
        except ValueError:
            continue
        if index <= last_index:      # 重复表头后的重排或其它分析段，跳过
            continue
        last_index = index
        rows.append(values)
    return columns, rows


def transient_series(circuit: dict, *, step: float = 1e-4, stop: float = 0.05,
                     max_nodes: int = 4, max_points: int = 240) -> Dict[str, Any]:
    """跑一次真实 .tran，返回节点电压时间序列（教学时序图数据源）。

    返回 dict：ran / engine / reason / nodes / time / series / rows / point_count / deck。
    任何一步失败都保持 ran=False 并写明 reason，不生成替代波形。
    """
    exe = find_ngspice()
    result: Dict[str, Any] = {
        "ran": False, "engine": "ngspice" if exe else "none", "kind": "tran",
        "step_s": step, "stop_s": stop, "nodes": [], "time": [], "series": {},
        "rows": 0, "point_count": 0, "reason": "", "deck": "",
    }
    if not exe:
        result["reason"] = "ngspice_unavailable"
        return result
    try:
        nodes = _batch_node_names(run_ngspice(build_analysis_deck(circuit, "op")) or "")
    except Exception:
        nodes = []
    nodes = [n for n in nodes if n != "0"][:max_nodes]
    if not nodes:
        result["reason"] = "no_voltage_nodes"
        return result
    lines = [ln for ln in build_analysis_deck(circuit, "tran", tran_step=step,
                                             tran_stop=stop).splitlines()
             if not ln.strip().startswith(".end")]
    lines.append(".print tran " + " ".join("v(%s)" % n for n in nodes))
    lines += [".end", ""]
    deck = "\n".join(lines)
    result["deck"] = deck
    out = run_ngspice(deck)
    if not out:
        result["reason"] = "tran_run_failed"
        return result
    cols, rows = _parse_tran_table(out)
    if len(rows) < 2:
        result["reason"] = "no_transient_table"
        return result
    keep = cols[:max_nodes]
    stride = max(1, len(rows) // max_points)
    picked = rows[::stride][:max_points]
    result.update({
        "ran": True, "nodes": keep, "rows": len(rows),
        "point_count": len(picked),
        "time": [round(r[0], 9) for r in picked],
        "series": {c: [round(r[1 + i], 6) for r in picked]
                   for i, c in enumerate(keep)},
    })
    return result


def simulate(circuit: dict, kind: str = "op", **kw) -> Dict[str, Any]:
    """统一入口：优先 ngspice，失败回退解析解。"""
    deck = build_analysis_deck(circuit, kind=kind, **kw)
    ng_out = run_ngspice(deck)
    analytic = analyze_simple(circuit)
    ac = None
    if kind == "ac":
        ac = ac_response(circuit)
    node_v = parse_ngspice_op(ng_out or "") if kind == "op" else {}
    return {
        "kind": kind,
        "deck": deck,
        "engine": "ngspice" if ng_out else "analytic",
        "ngspice_available": bool(find_ngspice()),
        "ngspice_output": ng_out,
        "node_voltages": node_v,
        "analytic": analytic,
        "ac_response": ac,
    }


if __name__ == "__main__":
    import sys
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    # 重新绑定包内导入（直接 python core/spice_enhanced.py 时）
    from core.spice import to_cir as _to_cir  # noqa: F401
    from core.simulate import _parts_by_type as _pbt  # noqa: F401
    globals()["to_cir"] = _to_cir
    globals()["_parts_by_type"] = _pbt
    try:
        from core.design_rules import detect_supply_v as _dsv, led_vf as _lvf, parse_ohm as _po
        globals()["detect_supply_v"] = _dsv
        globals()["led_vf"] = _lvf
        globals()["parse_ohm"] = _po
    except Exception:
        pass

    c = {
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
    r = simulate(c, kind="op")
    assert r["deck"].strip().endswith(".end")
    assert ".op" in r["deck"]
    assert r["analytic"]["items"], r["analytic"]

    ac_deck = build_analysis_deck(c, kind="ac")
    assert ".ac" in ac_deck
    assert "AC 1" in ac_deck, ac_deck

    tran_deck = build_analysis_deck(c, kind="tran")
    assert ".tran" in tran_deck
    assert "PULSE" in tran_deck, tran_deck

    rc = {
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
    acr = ac_response(rc)
    assert acr["engine"] == "analytic_rc"
    # fc = 1/(2π·1000·1e-7) ≈ 1591.5 Hz
    assert 1400 < acr["fc_hz"] < 1800, acr["fc_hz"]
    assert acr["topology"] == "lowpass"
    assert len(acr["points"]) >= 2
    print("spice_enhanced self-check ok  fc=%.1fHz" % acr["fc_hz"])
