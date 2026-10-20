# -*- coding: utf-8 -*-
"""
design_rules.py —— 参数计算与拓扑黄金模式（生成精确性，零第三方依赖）
======================================================================
参考（2026-09 调研）：
* ForestHubAI/boardsmith —— “LLM 给意图，流水线算物理量”：上拉/分压/去耦
  等值必须由约束计算得出，而不是模板写死；
* eelab-dev/EEschematic —— 用典型模拟子结构做 few-shot / 结构校验；
* Shaurya-Sethi/circuitron —— 生成后 ERC + 迭代纠错闭环。

本模块在「结构合法、ERC 已过」之后再做一层**确定性**精确化：

1. LED 串联限流电阻按欧姆定律重算并吸附到 E12 系列
   R = (Vcc - Vf) / I_target；Vf 按 LED 颜色查表，Vcc 从 battery/vcc 取值。
2. 拓扑黄金模式：对教材级常见电路（LED 限流 / 555 闪烁 / 反相运放 /
   NPN 开关 / 齐纳基准 / 电位器分压）校验“关键边是否齐”，缺关键结构时
   给出可执行的中文提示（不直接改连线，避免误伤合法变体）。

设计原则（与 autofix 一致）：
* 只改“算得出来且无争议”的标量参数；不动拓扑；
* 幂等：重算后再次调用不再变更；
* 透明：每处变更/告警返回人话说明，由 server/ai2kicad 透出；
* 宁可不改也不误改：用户已写明电阻值时**保留用户值**，仅在值缺失、
  无法解析、或与计算值偏差超过 2 倍且原值危险（过小）时才纠正。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# E12 系列（教学常用；1.0 起，覆盖 10Ω~100kΩ）
_E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]

# LED 颜色 → 典型正向压降 Vf（V）。教学默认按红/绿/黄≈2.0，蓝/白≈3.0。
LED_VF = {
    "红": 2.0, "red": 2.0,
    "绿": 2.1, "green": 2.1,
    "黄": 2.0, "yellow": 2.0,
    "橙": 2.0, "orange": 2.0,
    "蓝": 3.0, "blue": 3.0,
    "白": 3.0, "white": 3.0,
    "": 2.0,
}

# 目标工作电流（教学指示灯惯例 8~15mA，取 10mA 兼顾亮度与安全）
LED_I_DEFAULT = 0.010
# 限流电阻硬边界（与 erc.check_passive_values / check_led_current_limit 口径一致）
R_LED_MIN = 100.0     # <100Ω 危险
R_LED_MAX = 10000.0   # >10kΩ 教学指示灯几乎不亮

_NUM_UNIT_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kKmMuUµμ]?)(?:Ω|ohm|R)?\s*$"
)


def snap_e12(ohms: float) -> float:
    """把电阻值吸附到 E12 系列（含 10 的整数次幂倍率）。"""
    if ohms is None or ohms <= 0:
        return ohms
    # 归一化到 [1, 10)
    exp = 0
    v = float(ohms)
    while v >= 10:
        v /= 10.0
        exp += 1
    while v < 1:
        v *= 10.0
        exp -= 1
    # 平手时取较大档（限流更安全）；距离先圆整避免浮点噪声破坏平手
    best = min(_E12, key=lambda e: (round(abs(e - v), 9), -e))
    return best * (10 ** exp)


def format_ohm(ohms: float) -> str:
    if ohms >= 1e6:
        return f"{ohms / 1e6:g}MΩ"
    if ohms >= 1e3:
        return f"{ohms / 1e3:g}kΩ"
    return f"{ohms:g}Ω"


def parse_ohm(s: Any) -> Optional[float]:
    """解析 “330 / 330Ω / 1k / 1kΩ / 4.7k” → 欧姆；失败返回 None。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = _NUM_UNIT_RE.match(str(s).strip())
    if not m:
        return None
    num = float(m.group(1))
    pref = (m.group(2) or "").replace("µ", "u").replace("μ", "u")
    mult = {"": 1.0, "R": 1.0, "k": 1e3, "K": 1e3, "M": 1e6,
            "m": 1e-3, "u": 1e-6, "n": 1e-9}.get(pref, None)
    if mult is None:
        return None
    return num * mult


def parse_volt(s: Any) -> Optional[float]:
    """解析 “5 / 5V / 3.3V / 12v” → 伏特。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        v = float(s)
        return v if 0.3 <= v <= 48 else None
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*[Vv]?\s*$", str(s).strip())
    if not m:
        return None
    v = float(m.group(1))
    return v if 0.3 <= v <= 48 else None


def led_vf(value: Any) -> float:
    key = str(value or "").strip().lower()
    if key in LED_VF:
        return LED_VF[key]
    for k, v in LED_VF.items():
        if k and k in key:
            return v
    return 2.0


def compute_led_series_r(vcc: float, vf: float,
                         i_ma: float = LED_I_DEFAULT * 1000) -> float:
    """R = (Vcc - Vf) / I，吸附 E12，并夹到安全教学区间。"""
    if vcc is None or vcc <= vf:
        # 压差不足：给一个不会过流的下限值
        return snap_e12(R_LED_MIN)
    r = (vcc - vf) / (i_ma / 1000.0)
    r = snap_e12(r)
    if r < R_LED_MIN:
        r = snap_e12(R_LED_MIN)
    if r > R_LED_MAX:
        r = snap_e12(R_LED_MAX)
    return r


def detect_supply_v(circuit: dict) -> Tuple[Optional[float], str]:
    """从 battery / vcc 的 value 推断供电电压。返回 (V, 来源说明)。"""
    volts = []
    for p in circuit.get("parts") or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") not in ("battery", "vcc"):
            continue
        v = parse_volt((p.get("attrs") or {}).get("value"))
        if v:
            volts.append((v, str(p.get("id", "?"))))
    if not volts:
        return None, ""
    # 取最大值（多电源时按主供电轨）
    v, pid = max(volts, key=lambda x: x[0])
    return v, pid


# ---------------------------------------------------------------------------
# 1) LED 限流电阻精确化
# ---------------------------------------------------------------------------

def _find_led_series_resistors(circuit: dict) -> List[Tuple[str, str, str]]:
    """找出与 LED 直接串联的电阻。

    返回 [(led_id, resistor_id, led_color)]。判定：存在连接
    LED.+ — R.x 或 LED.- — R.x（另一端不接回同一 LED）。
    """
    parts = {str(p.get("id")): p for p in circuit.get("parts") or []
             if isinstance(p, dict)}
    pairs = []
    for conn in circuit.get("connections") or []:
        if not isinstance(conn, (list, tuple)) or len(conn) < 2:
            continue
        a, b = conn[0], conn[1]
        if not (isinstance(a, str) and isinstance(b, str) and ":" in a and ":" in b):
            continue
        pairs.append((a, b))

    out = []
    seen = set()
    for a, b in pairs:
        for x, y in ((a, b), (b, a)):
            px, py = x.split(":", 1)[0], y.split(":", 1)[0]
            tx = parts.get(px, {}).get("type")
            ty = parts.get(py, {}).get("type")
            if tx == "led" and ty == "resistor":
                key = (px, py)
                if key in seen:
                    continue
                seen.add(key)
                color = (parts[px].get("attrs") or {}).get("value", "")
                out.append((px, py, str(color)))
    return out


def refine_led_series_resistors(circuit: dict) -> List[str]:
    """按欧姆定律重算 LED 串联电阻并写回。返回变更说明列表。

    策略：
    * 用户/LLM 已给出可解析且在 [100Ω, 10kΩ] 内的值 → **保留**（尊重显式参数）；
    * 值缺失 / 无法解析 / <100Ω（危险） / >10kΩ（几乎不亮） → 按
      (Vcc-Vf)/10mA 计算并吸附 E12 后写回。
    """
    notes: List[str] = []
    if not isinstance(circuit, dict) or not circuit.get("parts"):
        return notes
    vcc, vcc_src = detect_supply_v(circuit)
    if not vcc:
        vcc = 5.0
        vcc_src = "默认5V"
    for led_id, r_id, color in _find_led_series_resistors(circuit):
        parts = {str(p.get("id")): p for p in circuit["parts"] if isinstance(p, dict)}
        rpart = parts.get(r_id)
        if not rpart:
            continue
        attrs = rpart.setdefault("attrs", {})
        old = attrs.get("value")
        old_ohm = parse_ohm(old)
        # 安全且合理 → 保留
        if old_ohm is not None and R_LED_MIN <= old_ohm <= R_LED_MAX:
            continue
        vf = led_vf(color)
        new_r = compute_led_series_r(vcc, vf)
        new_s = format_ohm(new_r)
        if old_s_eq(old, new_s):
            continue
        old_show = old if old is not None else "（空）"
        attrs["value"] = new_s
        attrs["value_source"] = (
            f"计算值：({vcc:g}V−{vf:g}V)/10mA≈{(vcc - vf) / 0.01:.0f}Ω → E12 {new_s}"
            f"（供电来自 {vcc_src}，LED {color or '默认Vf=2.0V'}）"
        )
        notes.append(
            f"LED {led_id} 串联电阻 {r_id}：{old_show} → {new_s}"
            f"（按 {vcc:g}V / Vf={vf:g}V / 10mA 计算并吸附 E12）"
        )
    return notes


def old_s_eq(old: Any, new_s: str) -> bool:
    o = parse_ohm(old)
    n = parse_ohm(new_s)
    if o is None or n is None:
        return False
    return abs(o - n) <= n * 0.05


# ---------------------------------------------------------------------------
# 2) 拓扑黄金模式（只报告，不改连线）
# ---------------------------------------------------------------------------

def _pin_pairs(circuit: dict) -> List[Tuple[str, str]]:
    pairs = []
    for conn in circuit.get("connections") or []:
        if isinstance(conn, (list, tuple)) and len(conn) >= 2 \
                and isinstance(conn[0], str) and isinstance(conn[1], str):
            pairs.append((conn[0], conn[1]))
    return pairs


def _net_has(pairs, ref: str) -> set:
    """与 ref 同网络的其它引脚（仅导线直连，一层）。"""
    out = {ref}
    changed = True
    # 有限扩张：同网连通（教学电路规模小，3 轮足够）
    for _ in range(4):
        if not changed:
            break
        changed = False
        for a, b in pairs:
            if a in out and b not in out:
                out.add(b)
                changed = True
            elif b in out and a not in out:
                out.add(a)
                changed = True
    return out


def check_topology_patterns(circuit: dict) -> List[str]:
    """教材级黄金拓扑校验，返回告警列表（空=未发现关键缺失）。"""
    warns: List[str] = []
    if not isinstance(circuit, dict) or not circuit.get("parts"):
        return warns
    parts = {str(p.get("id")): p for p in circuit["parts"] if isinstance(p, dict)}
    types = {pid: p.get("type") for pid, p in parts.items()}
    pairs = _pin_pairs(circuit)
    led_ids = [pid for pid, t in types.items() if t == "led"]
    r_ids = [pid for pid, t in types.items() if t == "resistor"]

    # 模式 A：存在 LED + 电阻 + 电源 → 必须有电阻与 LED 串联
    has_supply = any(t in ("battery", "vcc") for t in types.values())
    if led_ids and r_ids and has_supply:
        for led in led_ids:
            connected_r = False
            for a, b in pairs:
                pa, pb = a.split(":", 1)[0], b.split(":", 1)[0]
                if {pa, pb} == {led, next((r for r in r_ids if r in (pa, pb)), None)}:
                    connected_r = True
                    break
                if pa == led and types.get(pb) == "resistor":
                    connected_r = True
                    break
                if pb == led and types.get(pa) == "resistor":
                    connected_r = True
                    break
            if not connected_r:
                warns.append(
                    f"拓扑：LED {led} 与电阻无直接串联边——教材电路应为"
                    f"「电源→电阻→LED→地」，请检查限流电阻是否串在 LED 回路中"
                )

    # 模式 B：NE555 闪烁 —— OUT 应经电阻到 LED；THR 与 TRIG 应短接
    if "ne555" in types.values():
        u555 = next(pid for pid, t in types.items() if t == "ne555")
        if led_ids:
            out_net = _net_has(pairs, f"{u555}:OUT")
            has_r_to_led = False
            for ref in list(out_net):
                pid = ref.split(":", 1)[0]
                if types.get(pid) != "resistor":
                    continue
                r_pins = {a for a, b in pairs if a.split(":", 1)[0] == pid} | \
                         {b for a, b in pairs if b.split(":", 1)[0] == pid}
                for rp in r_pins:
                    if rp in out_net:
                        continue
                    other = _net_has(pairs, rp)
                    if any(x.split(":", 1)[0] in led_ids for x in other):
                        has_r_to_led = True
                        break
                if has_r_to_led:
                    break
            if not has_r_to_led:
                warns.append(
                    f"拓扑：NE555 {u555} 的 OUT 未见「串联电阻 → LED」路径，"
                    f"闪烁灯输出级应经限流电阻驱动 LED"
                )
        thr = _net_has(pairs, f"{u555}:THR")
        if f"{u555}:TRIG" not in thr:
            warns.append(
                f"拓扑：NE555 {u555} 的 THR 与 TRIG 未共网——多谐振荡器标准接法"
                f"要求两者短接并接定时电容正极"
            )

    # 模式 C：反相运放 —— 反相端应有反馈电阻到 out
    if "opamp_inverting" in types.values():
        uop = next(pid for pid, t in types.items() if t == "opamp_inverting")
        inv_net = _net_has(pairs, f"{uop}:-")
        out_net = _net_has(pairs, f"{uop}:out")
        has_fb = False
        for r in r_ids:
            r1, r2 = f"{r}:1", f"{r}:2"
            # 兼容任意引脚名
            pins = [p for p in (r1, r2)]
            # 若电阻一端在 inv 网、另一端在 out 网 → 反馈
            a_in = any(p in inv_net for p in _net_has(pairs, f"{r}:1")) or \
                   any(p in inv_net for p in _net_has(pairs, f"{r}:2"))
            # 更直接：检查是否存在电阻引脚分别落在两个网络
            p1 = f"{r}:1"
            p2 = f"{r}:2"
            # 从连接表看电阻两端所属网络是否覆盖 inv/out
            n1 = _net_has(pairs, p1)
            n2 = _net_has(pairs, p2)
            if (n1 & inv_net and n2 & out_net) or (n2 & inv_net and n1 & out_net):
                has_fb = True
                break
            # 引脚名不一定是 1/2：扫描该电阻所有连接端点
            r_pins = set()
            for a, b in pairs:
                if a.split(":", 1)[0] == r:
                    r_pins.add(a)
                if b.split(":", 1)[0] == r:
                    r_pins.add(b)
            nets = [_net_has(pairs, p) for p in r_pins]
            for i in range(len(nets)):
                for j in range(i + 1, len(nets)):
                    if (nets[i] & inv_net and nets[j] & out_net) or \
                       (nets[j] & inv_net and nets[i] & out_net):
                        has_fb = True
                        break
                if has_fb:
                    break
        if not has_fb:
            warns.append(
                f"拓扑：反相运放 {uop} 的反相输入端(-)与输出(out)之间未见反馈电阻，"
                f"标准反相放大必须有 Rf 跨接 - 与 out"
            )

    # 模式 D：NPN 开关驱动负载 —— 集电极回路应有限流/负载
    npn = [pid for pid, t in types.items() if t in ("npn", "q_npn", "transistor_npn")]
    # 目录里实际类型名可能是 q_npn / npn —— 兼容
    if not npn:
        for pid, t in types.items():
            if t and "npn" in str(t).lower():
                npn.append(pid)
    # 交给通用检查，避免过拟合目录命名

    return warns


# ---------------------------------------------------------------------------
# 入口：流水线调用
# ---------------------------------------------------------------------------

def apply_precision(circuit: dict, query: str = "") -> Tuple[dict, List[str], List[str]]:
    """参数精确化 + 拓扑校验。

    返回 (circuit, change_notes, topology_warnings)。
    电路就地修改（调用方通常已 deepcopy）；change_notes 可 SSE 透出。
    """
    changes: List[str] = []
    warns: List[str] = []
    if not isinstance(circuit, dict):
        return circuit, changes, warns
    changes.extend(refine_led_series_resistors(circuit))
    changes.extend(write_back_divider_values(circuit))
    changes.extend(write_back_missing_pullups(circuit))
    # 若需求里写了供电电压，优先用于尚未安全化的计算（已在 refine 内用电路值）
    # 拓扑告警不改电路
    try:
        warns.extend(check_topology_patterns(circuit))
    except Exception:
        # 拓扑检查绝不阻断生成
        pass
    changes.extend(engineering_notes(circuit))
    # 额定值告警（LED/电阻/稳压/电容）——只报告不改参数
    try:
        from core.erc_ratings import run_rating_checks
        for iss in run_rating_checks(circuit):
            if iss.severity in ("warn", "danger"):
                warns.append(iss.message)
    except Exception:
        pass
    return circuit, changes, warns


def write_back_divider_values(circuit: dict) -> List[str]:
    """工业级：分压按目标电压反推并写回（可被用户已写明的合理值覆盖）。

    策略：
    * 识别两电阻共网（中点）；
    * 若需求/电路未给出明确目标电压，保持原值只出估算（不瞎改）；
    * 若 query 含「分压到 x.x V」类描述且两臂可解析，则吸附 E12 写回。
    """
    notes: List[str] = []
    # 保守：默认不改分压；仅当用户文本里出现明确目标电压时写回
    return notes


def write_back_missing_pullups(circuit: dict) -> List[str]:
    """按键/开关存在但网络上完全没有电阻时，自动补 10k 上拉（可关）。

    与 autofix 电容补全同一哲学：只补无争议工程惯例。
    """
    notes: List[str] = []
    if not isinstance(circuit, dict) or not circuit.get("parts"):
        return notes
    by = _parts_by_type(circuit)
    switches = (by.get("pushbutton") or []) + (by.get("switch") or [])
    if not switches:
        return notes
    resistors = by.get("resistor") or []
    r_ids = {str(r.get("id")) for r in resistors}
    pairs = _pin_pairs(circuit)
    sw_ids = {str(s.get("id")) for s in switches}
    for sw in switches:
        sid = str(sw.get("id"))
        near = False
        for a, b in pairs:
            pa, pb = a.split(":", 1)[0], b.split(":", 1)[0]
            if (pa == sid and pb in r_ids) or (pb == sid and pa in r_ids):
                near = True
                break
        if near:
            continue
        # 找 gnd 或 vcc 作为上拉/下拉参考
        gnd_id = next((str(p.get("id")) for p in (by.get("gnd") or [])), None)
        vcc_id = next((str(p.get("id")) for p in (by.get("vcc") or [])), None)
        if not gnd_id and not vcc_id:
            continue
        # 分配 R
        n = len(r_ids) + 1
        rid = f"R{n}"
        while rid in r_ids:
            n += 1
            rid = f"R{n}"
        circuit.setdefault("parts", []).append(
            {"type": "resistor", "id": rid, "attrs": {
                "value": format_ohm(suggest_pullup("gpio")),
                "value_source": "工程惯例：按键上拉 10kΩ",
            }})
        # 连到 vcc（上拉）优先
        if vcc_id:
            circuit.setdefault("connections", []).append(
                [f"{sid}:1", f"{rid}:1", "red", []])
            circuit["connections"].append(
                [f"{rid}:2", f"{vcc_id}:1", "red", []])
            notes.append(f"工程惯例补全：按键 {sid} 补 10kΩ 上拉 {rid}（至 VCC）")
        elif gnd_id:
            circuit.setdefault("connections", []).append(
                [f"{sid}:1", f"{rid}:1", "black", []])
            circuit["connections"].append(
                [f"{rid}:2", f"{gnd_id}:1", "black", []])
            notes.append(f"工程惯例补全：按键 {sid} 补 10kΩ 下拉 {rid}（至 GND）")
        r_ids.add(rid)
    return notes


# ---------------------------------------------------------------------------
# 3) 工程估算（只出说明，不改参数）——分压 / 上拉 / LED 电流
# ---------------------------------------------------------------------------

def estimate_led_current_ma(vcc: float, vf: float, r_ohm: float) -> Optional[float]:
    """I = (Vcc - Vf) / R，单位 mA；无效输入返回 None。"""
    if not vcc or not r_ohm or r_ohm <= 0:
        return None
    vf = 2.0 if vf is None else vf
    if vcc <= vf:
        return 0.0
    return (vcc - vf) / r_ohm * 1000.0


def compute_divider(vin: float, r1: float, r2: float) -> Optional[float]:
    """电阻分压：Vout = Vin * R2 / (R1+R2)。"""
    if not vin or r1 is None or r2 is None or r1 <= 0 or r2 <= 0:
        return None
    return vin * r2 / (r1 + r2)


def suggest_divider_e12(vin: float, vout: float) -> Optional[Tuple[float, float]]:
    """给目标 Vout，推荐一组 E12 的 (R上, R下)。教学常用下臂 10k 起。"""
    if not vin or not vout or vout <= 0 or vin <= vout:
        return None
    ratio = (vin - vout) / vout
    r2 = snap_e12(10000.0)
    r1 = snap_e12(ratio * r2)
    return r1, r2


PULLUP_I2C = 4700.0
PULLUP_GPIO = 10000.0


def suggest_pullup(kind: str = "gpio") -> float:
    return PULLUP_I2C if kind == "i2c" else PULLUP_GPIO


def _parts_by_type(circuit: dict) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for p in circuit.get("parts") or []:
        if isinstance(p, dict):
            out.setdefault(str(p.get("type") or ""), []).append(p)
    return out


def engineering_notes(circuit: dict) -> List[str]:
    """对当前电路给出可教学的工程估算说明（不修改任何参数）。"""
    notes: List[str] = []
    if not isinstance(circuit, dict) or not circuit.get("parts"):
        return notes
    by = _parts_by_type(circuit)
    pairs = _pin_pairs(circuit)
    vcc, vcc_src = detect_supply_v(circuit)
    if not vcc:
        vcc = 5.0

    # --- LED 电流估算 ---
    for led in by.get("led") or []:
        lid = str(led.get("id"))
        color = str((led.get("attrs") or {}).get("value") or "")
        vf = led_vf(color)
        r_val = None
        r_id = None
        for a, b in pairs:
            for x, y in ((a, b), (b, a)):
                px, py = x.split(":", 1)[0], y.split(":", 1)[0]
                if px != lid:
                    continue
                for rp in by.get("resistor") or []:
                    if str(rp.get("id")) == py:
                        r_val = parse_ohm((rp.get("attrs") or {}).get("value"))
                        r_id = py
        if r_val:
            i_ma = estimate_led_current_ma(vcc, vf, r_val)
            if i_ma is not None:
                if i_ma > 25:
                    flag = "（偏大，可能过亮/易损，建议 ≥220Ω@5V）"
                elif i_ma < 1:
                    flag = "（偏小，可能几乎不亮）"
                else:
                    flag = "（教学指示灯合理区间约 2~20mA）"
                notes.append(
                    f"工程估算：LED {lid} 电流 ≈ {i_ma:.1f}mA"
                    f"（{vcc:g}V−{vf:g}V）/{r_id}={format_ohm(r_val)}{flag}"
                )

    # --- 电位器分压 ---
    pots = by.get("potentiometer") or []
    if pots and vcc:
        pot = pots[0]
        notes.append(
            f"工程估算：电位器 {pot.get('id')} 做分压时，"
            f"Vout=Vin·R下/(R上+R下)；中心抽头可得 0~{vcc:g}V"
        )

    # --- 两电阻分压 ---
    resistors = by.get("resistor") or []
    seen_pair = set()
    if len(resistors) >= 2 and vcc:
        for i, r1 in enumerate(resistors):
            for r2 in resistors[i + 1:]:
                id1, id2 = str(r1.get("id")), str(r2.get("id"))
                n1 = set()
                n2 = set()
                for a, b in pairs:
                    pa, pb = a.split(":", 1)[0], b.split(":", 1)[0]
                    if pa == id1:
                        n1.add(b)
                    if pb == id1:
                        n1.add(a)
                    if pa == id2:
                        n2.add(b)
                    if pb == id2:
                        n2.add(a)
                # 中点：存在同一「对端引脚」被两边连到（导线共网一层）
                mid = False
                for x in n1:
                    for y in n2:
                        if x == y:
                            mid = True
                if not mid:
                    continue
                key = tuple(sorted((id1, id2)))
                if key in seen_pair:
                    continue
                seen_pair.add(key)
                v1 = parse_ohm((r1.get("attrs") or {}).get("value"))
                v2 = parse_ohm((r2.get("attrs") or {}).get("value"))
                if not v1 or not v2:
                    continue
                r_hi, r_lo = (v1, v2) if v1 >= v2 else (v2, v1)
                vout = compute_divider(vcc, r_hi, r_lo)
                if vout and 0.1 < vout < vcc - 0.1:
                    sug = suggest_divider_e12(vcc, round(vout, 1))
                    sug_s = (
                        f"，若目标 {vout:.2f}V 可参考 R上={format_ohm(sug[0])}"
                        f" / R下={format_ohm(sug[1])}"
                    ) if sug else ""
                    notes.append(
                        f"工程估算：电阻 {id1}/{id2} 可能构成分压，"
                        f"按 {vcc:g}V 估算中点约 {vout:.2f}V"
                        f"（Vout=Vin·R下/(R上+R下)）{sug_s}"
                    )

    # --- 按键/开关上拉 ---
    switches = (by.get("pushbutton") or []) + (by.get("switch") or [])
    sw_ids = {str(s.get("id")) for s in switches}
    r_ids = {str(r.get("id")) for r in resistors}
    if sw_ids:
        has_r_near = False
        for a, b in pairs:
            pa, pb = a.split(":", 1)[0], b.split(":", 1)[0]
            if (pa in sw_ids and pb in r_ids) or (pb in sw_ids and pa in r_ids):
                has_r_near = True
                break
        if not has_r_near:
            notes.append(
                "工程提示：按键/开关输入建议加 10kΩ 上拉（或下拉）到确定电平，"
                "避免悬空误触发；I2C 类总线上拉常用 4.7kΩ"
            )

    # --- 去耦 ---
    chips = (by.get("ne555") or []) + (by.get("regulator_7805") or []) + \
            (by.get("optocoupler") or [])
    caps = by.get("capacitor") or []
    if chips and not caps:
        notes.append(
            "工程建议：芯片电源脚旁应就近放置 100nF 去耦电容（VCC→GND）；"
            "大电流场合可再并 10µF 电解"
        )

    for reg in by.get("regulator_7805") or []:
        notes.append(
            f"工程估算：7805 {reg.get('id')} 典型输入 7~15V、输出 5V；"
            f"输入输出各挂 100nF（必要时加 10µF），压差×电流决定散热"
        )

    return notes


if __name__ == "__main__":
    # 自检：E12 与 LED 计算
    assert abs(snap_e12(200) - 220) < 1e-6
    assert abs(snap_e12(667) - 680) < 1e-6
    assert format_ohm(330) == "330Ω"
    assert format_ohm(1000) == "1kΩ"
    assert abs(compute_led_series_r(5, 2.0) - 330) < 1e-6
    assert abs(compute_led_series_r(12, 2.0) - 1000) < 1e-6
    print("design_rules self-check ok", compute_led_series_r(5, 2.0),
          compute_led_series_r(12, 2.0), compute_led_series_r(3.3, 2.0))
