# -*- coding: utf-8 -*-
"""
test_autofix.py —— autofix.py 自动附挂电容的离线单元测试
=========================================================
不联网、不调 LLM。运行：python test_autofix.py
覆盖：警告识别、修复效果、幂等性、输入不可变、已有电容去重、
无接地参考点放弃、非 555 悬空输入不越权修复。
"""


# 测试位于 tests/ 子目录：先把项目根插入 sys.path 再 import 项目模块
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from core.erc import run_erc
from core.catalog import build_erc_kb
from core.autofix import apply
from core.llm import validate_circuit_json

PASS = 0


def check(name, cond):
    global PASS
    if not cond:
        raise AssertionError(f"失败：{name}")
    PASS += 1
    print(f"  ✔ {name}")


def warnings_of(rep):
    return [i for i in rep.issues if i.severity == "warning"]


def ne555_bare():
    """NE555 裸电路：有供电与输出负载，但无任何电容、TRIG/THR/RESET 悬空。"""
    return {"version": 1,
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


KB = build_erc_kb()

print("== 1. 修复前：两类目标警告都存在 ==")
c = ne555_bare()
rep0 = run_erc(c, KB)
w0 = warnings_of(rep0)
check("存在“缺去耦电容”警告", any(i.rule == "缺去耦电容" for i in w0))
check("存在 555 CTRL“悬空输入”警告",
      any(i.rule == "悬空输入" and "CTRL" in i.message for i in w0))

print("== 2. 修复后：两类警告清零、其余不受影响 ==")
fixed, fixes = apply(c, KB)
check("生成了至少两条修复说明", len(fixes) >= 2)
rep1 = run_erc(fixed, KB)
w1 = warnings_of(rep1)
check("“缺去耦电容”已清零", not any(i.rule == "缺去耦电容" for i in w1))
check("555 CTRL“悬空输入”已清零",
      not any(i.rule == "悬空输入" and "CTRL" in i.message for i in w1))
check("其余警告数量不受影响（TRIG/THR/RESET 仍如实提示）",
      len(w1) == len(w0) - 2)
check("修复后的电路通过结构校验", validate_circuit_json(fixed) == [])
check("新增位号不与原有位号冲突",
      {p["id"] for p in fixed["parts"]}.isdisjoint({"C0"}) and
      len([p for p in fixed["parts"] if p["type"] == "capacitor"]) == 2)

print("== 3. 幂等与不可变 ==")
_, fixes2 = apply(fixed, KB)
check("重放无新增修复（幂等）", fixes2 == [])
check("输入电路未被就地修改",
      "capacitor" not in {p.get("type") for p in c["parts"]})

print("== 4. 已有去耦电容时不重复添加 ==")
c2 = ne555_bare()
c2["parts"].append({"type": "capacitor", "id": "C1", "attrs": {"value": "100nF"}})
c2["connections"] += [["U1:VCC", "C1:+", "blue", []],
                      ["C1:-", "GND1:1", "black", []]]
f2, fx2 = apply(c2, KB)
check("不再补去耦电容", not any("缺去耦" in s for s in fx2))
check("位号从 C1 之后顺延（CTRL 旁路占 C2）",
      any(p["id"] == "C2" for p in f2["parts"]))

print("== 5. 无地符号/电池时：回退用芯片自身 GND 引脚作参考 ==")
c3 = {"version": 1,
      "parts": [{"type": "vcc", "id": "V1"},
                {"type": "ne555", "id": "U1"},
                {"type": "resistor", "id": "R1"},
                {"type": "led", "id": "LED1"}],
      "connections": [["V1:1", "U1:VCC", "red", []],
                      ["U1:OUT", "R1:1", "red", []],
                      ["R1:2", "LED1:+", "red", []]]}
f3, fx3 = apply(c3, KB)
check("去耦电容挂到芯片自身 GND 引脚（教科书接法）",
      any("U1:GND" in s for s in fx3))
check("确实附挂了电容",
      "capacitor" in {p["type"] for p in f3["parts"]})
rep3 = run_erc(f3, KB)
check("“缺去耦电容”警告清零",
      not any(i.rule == "缺去耦电容" for i in warnings_of(rep3)))


def _no_ground_ref():
    """彻底无任何 GND 型引脚的元件组合（防御分支的直接单元验证）。"""
    from core import autofix
    parts = [{"type": "resistor", "id": "R1"}, {"type": "led", "id": "LED1"}]
    check("纯无源组合找不到接地参考点（返回空串）",
          autofix._find_ground_ref({"parts": parts}, KB) == "")


_no_ground_ref()

print("== 6. 非 555 的悬空输入不越权修复 ==")
c4 = {"version": 1,
      "parts": [{"type": "battery", "id": "BAT1"},
                {"type": "opamp_inverting", "id": "U1"},
                {"type": "gnd", "id": "GND1"}],
      "connections": [["BAT1:+", "U1:-", "red", []],
                      ["U1:out", "GND1:1", "black", []],
                      ["BAT1:-", "GND1:1", "black", []]]}
f4, fx4 = apply(c4, KB)
check("未自动附挂电容（留给 ERC 修复/用户处理）",
      "capacitor" not in {p["type"] for p in f4["parts"]} and fx4 == [])

print(f"\nautofix 测试：{PASS} 项全部通过 ✔")
