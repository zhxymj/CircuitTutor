# -*- coding: utf-8 -*-
"""
autofix.py —— ERC 警告自动修复：附挂去耦/旁路电容（Python 标准库，零依赖）
==========================================================================
基准实测（2026-09-08，30 用例）中 4 条 warning 全部是“加个电容”级别的
工程惯例问题：3× 缺去耦电容（NE555 供电脚）+ 1× 悬空输入（555 CTRL）。
与其让每条 warning 都留给用户手工处理，不如在服务端流水线出图后自动补上 ——
这也是真实 EDA（KiCad ERC → 自动 Annotate/填充）的常规做法。

设计原则：
* 只修“警告级”且修法无争议的两类问题，fatal/major 仍走 LLM 回灌修复闭环；
* 幂等：已存在等效电容（同网络已有电容脚相连）时绝不重复添加，可安全重放；
* 透明：每处修复生成一条人话说明，server.py 经 SSE 推给前端展示；
* 无地不修：找不到可用的接地参考点（gnd 符号/电池负极）时放弃修复，
  绝不凭空造地。

修复规则：
1) 缺去耦电容 —— 芯片 PWR_IN 供电脚网络上没有电容脚时，附挂 100nF
   去耦电容（电容 + 接供电脚网络，- 接地）；
2) 悬空输入（仅 NE555 CTRL）—— 555 的 CTRL 脚悬空时按数据手册惯例
   附挂 10nF 旁路电容到地（其他芯片的悬空输入交由 ERC 修复/用户处理）。
"""

import copy
from typing import Any, Dict, List, Tuple

# 与 erc.py 的严重级别常量保持同值（字符串），避免循环导入
_WARNING = "warning"
# 与 erc.py 各规则名保持一致（rule 字段精确匹配）
_RULE_DECOUPLING = "缺去耦电容"
_RULE_FLOATING = "悬空输入"


def _warn_issues(rep) -> List[Any]:
    """取出报告里的 warning 级 Issue（ERCReport.issues 的 severity 过滤）。"""
    return [i for i in rep.issues if i.severity == _WARNING]


def _conn_refs(circuit: dict) -> List[Tuple[str, str]]:
    """把 connections 压成 [(端点A, 端点B)]，只取前两个合法字符串端点。"""
    pairs = []
    for conn in circuit.get("connections", []):
        if isinstance(conn, (list, tuple)) and len(conn) >= 2 \
                and isinstance(conn[0], str) and isinstance(conn[1], str):
            pairs.append((conn[0], conn[1]))
    return pairs


def _find_ground_ref(circuit: dict, erc_kb: dict) -> str:
    """找一个接地参考引脚引用（“id:pin”）。
    优先级：gnd 符号 > 电池负极 > 任意 GND 型引脚；找不到返回 ""。"""
    for p in circuit.get("parts", []):
        if isinstance(p, dict) and p.get("type") == "gnd":
            pins = erc_kb.get("gnd", {})
            if pins:
                return f"{p.get('id')}:{next(iter(pins))}"
    for p in circuit.get("parts", []):
        if isinstance(p, dict) and p.get("type") == "battery":
            if erc_kb.get("battery", {}).get("-") == "GND":
                return f"{p.get('id')}:-"
    for p in circuit.get("parts", []):
        if not isinstance(p, dict):
            continue
        for pin, ptype in erc_kb.get(p.get("type"), {}).items():
            if ptype == "GND":
                return f"{p.get('id')}:{pin}"
    return ""


def _has_decoupling(ref: str, parts_by_id: dict, pairs) -> bool:
    """ref 引脚所在的网络上是否已经挂了电容（与电容任一引脚有直连）。"""
    for a, b in pairs:
        if a == ref and str(parts_by_id.get(b.split(":", 1)[0], {}).get("type")) == "capacitor":
            return True
        if b == ref and str(parts_by_id.get(a.split(":", 1)[0], {}).get("type")) == "capacitor":
            return True
    return False


def _next_cap_id(circuit: dict) -> str:
    """分配一个不冲突的电容位号 C1、C2、…（交互层手动插入的电容也避开）。"""
    used = {p.get("id") for p in circuit.get("parts", []) if isinstance(p, dict)}
    n = 1
    while f"C{n}" in used:
        n += 1
    return f"C{n}"


def _add_cap(circuit: dict, erc_kb: dict, ref: str, gnd_ref: str,
             value: str, fixes: List[str], desc: str) -> None:
    """向电路附挂一颗 ref→gnd 的电容，并记录修复说明。"""
    cap_pins = list(erc_kb.get("capacitor", {}).keys())
    if len(cap_pins) < 2 or not gnd_ref:
        return                      # 元件目录异常 / 无地可接：放弃（幂等安全）
    cid = _next_cap_id(circuit)
    circuit["parts"].append({"type": "capacitor", "id": cid,
                             "attrs": {"value": value}})
    circuit["connections"].append([ref, f"{cid}:{cap_pins[0]}", "blue", []])
    circuit["connections"].append([f"{cid}:{cap_pins[1]}", gnd_ref, "black", []])
    fixes.append(f"{desc}：已附挂 {value} 电容 {cid}（{ref} → {gnd_ref}）")


def apply(circuit: dict, erc_kb: dict, rep=None) -> Tuple[dict, List[str]]:
    """依据 ERC 警告自动附挂电容。返回 (修复后的新电路, 修复说明列表)。

    rep 可传入已算好的 ERCReport（省一次重算）；不传则内部现算。
    输入电路不被修改（deepcopy），失败/无警告时原样返回（fixes 为空）。
    """
    if not isinstance(circuit, dict) or not circuit.get("parts"):
        return circuit, []
    if rep is None:
        from core.erc import run_erc   # 延迟导入，保持模块解耦
        rep = run_erc(circuit, erc_kb)

    warns = _warn_issues(rep)
    if not warns:
        return circuit, []

    fixed = copy.deepcopy(circuit)
    parts_by_id = {p.get("id"): p for p in fixed.get("parts", []) if isinstance(p, dict)}
    gnd_ref = _find_ground_ref(fixed, erc_kb)
    fixes: List[str] = []

    # —— 规则 1：缺去耦电容（供电脚网络无电容）——
    for issue in warns:
        if issue.rule != _RULE_DECOUPLING or not issue.pins:
            continue
        ref = issue.pins[0]
        if _has_decoupling(ref, parts_by_id, _conn_refs(fixed)):
            continue                            # 已有电容：幂等跳过
        if not gnd_ref:
            fixes.append(f"{ref} 建议加去耦电容，但电路中没有接地参考点，已跳过自动修复")
            continue
        pid = ref.split(":", 1)[0]
        ptype = str(parts_by_id.get(pid, {}).get("type", ""))
        _add_cap(fixed, erc_kb, ref, gnd_ref, "100nF", fixes,
                 f"{pid}（{ptype}）供电脚 {ref.split(':', 1)[1]} 缺去耦电容")
        parts_by_id = {p.get("id"): p for p in fixed.get("parts", []) if isinstance(p, dict)}

    # —— 规则 2：NE555 CTRL 悬空（数据手册惯例：10nF 旁路到地）——
    for issue in warns:
        if issue.rule != _RULE_FLOATING or not issue.pins:
            continue
        ref = issue.pins[0]
        if ":" not in ref:
            continue
        pid, pin = ref.split(":", 1)
        if str(parts_by_id.get(pid, {}).get("type", "")).lower() != "ne555" \
                or pin.upper() != "CTRL":
            continue                            # 其他悬空输入不越俎代庖
        if _has_decoupling(ref, parts_by_id, _conn_refs(fixed)):
            continue
        if not gnd_ref:
            continue
        _add_cap(fixed, erc_kb, ref, gnd_ref, "10nF", fixes,
                 f"{pid} 的 CTRL 脚悬空")
        parts_by_id = {p.get("id"): p for p in fixed.get("parts", []) if isinstance(p, dict)}

    if not fixes:
        return circuit, []
    return fixed, fixes


if __name__ == "__main__":
    # 自测：NE555 电路无任何电容 → 自动附挂去耦 + CTRL 旁路 → ERC 警告清零
    from core.erc import run_erc
    from core.catalog import build_erc_kb
    kb = build_erc_kb()
    circuit = {"version": 1,
               "parts": [{"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
                         {"type": "ne555", "id": "U1"},
                         {"type": "resistor", "id": "R1"},
                         {"type": "led", "id": "LED1"},
                         {"type": "gnd", "id": "GND1"}],
               "connections": [["BAT1:+", "U1:VCC", "red", []],
                               ["U1:OUT", "R1:1", "red", []],
                               ["R1:2", "LED1:+", "red", []],
                               ["LED1:-", "GND1:1", "black", []],
                               ["BAT1:-", "GND1:1", "black", []],
                               ["U1:GND", "GND1:1", "black", []]]}
    rep0 = run_erc(circuit, kb)
    print("修复前警告数：", len([i for i in rep0.issues if i.severity == "warning"]))
    fixed, fixes = apply(circuit, kb)
    for f in fixes:
        print(" -", f)
    rep1 = run_erc(fixed, kb)
    print("修复后警告数：", len([i for i in rep1.issues if i.severity == "warning"]))
    _, fixes2 = apply(fixed, kb)
    print("重放幂等（应无新增修复）：", fixes2 == [])
