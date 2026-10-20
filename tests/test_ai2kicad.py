#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_ai2kicad.py —— 自然语言→KiCad 端到端测试
=============================================================
覆盖 ai2kicad.py 的四点承诺：
  1) 需求理解: 无 Key 时自动回落模板路由且来源透明（离线确定性）;
  2) 格式合规: run() 产物为完整 KiCad 工程（sch/pro/库/表四件套）;
  3) 默认值:  参数来源标注（用户指定/约定默认/AI 补充）准确，报告明示;
  4) 校验:    偏差清单能抓到“描述参数未体现/重复位号/类型缺失”，
              装有 kicad-cli 时官方 ERC 归零 + 网表连通一致。

运行：python -B tests/test_ai2kicad.py   （项目根执行；全程离线、零 LLM 调用）
"""

from __future__ import annotations

# 测试位于 tests/ 子目录：先把项目根插入 sys.path 再 import 项目模块
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import copy
import json
import tempfile
from pathlib import Path

import ai2kicad as A
from core import catalog
from core import erc as erc_mod
from core import templates
from kicad import export as K

PASS = 0


def check(name, cond):
    global PASS
    if not cond:
        raise AssertionError(f"失败：{name}")
    PASS += 1
    print(f"  ✔ {name}")


def main() -> int:
    kb = catalog.build_erc_kb()

    print("== 参数提取（中文省略单位的写法） ==")
    check("完整单位", A._query_params("5V供电、330Ω、100nF") ==
          [(5.0, "V"), (330.0, "Ω"), A._value_params("100nF")[0]])
    check("单位归一（微法数值等价）",
          abs(A._value_params("100nF")[0][0] - 1e-7) < 1e-18)
    check("裸 k 前缀归 Ω（1k电阻）",
          A._query_params("用1k电阻") == [(1000.0, "Ω")])
    check("裸 M 前缀归 Ω", A._query_params("1M的下拉") == [(1e6, "Ω")])
    check("不误伤 555/两个", A._query_params("做一个555闪烁灯电路，两个LED") == [])
    check("值侧同规则", A._value_params("10k") == [(10000.0, "Ω")]
          and A._value_params("330Ω") == [(330.0, "Ω")])

    print("== 类型提及判定 ==")
    check("led 别名“灯”命中", A._type_mentioned("led", "做一个555闪烁灯电路"))
    check("负向词排除（数码管不算 led）",
          not A._type_mentioned("led", "做一个数码管显示"))
    check("未提及类型", not A._type_mentioned("resistor", "做一个555闪烁灯电路"))

    print("== 需求理解回落（无 Key 离线确定性） ==")
    with tempfile.TemporaryDirectory() as td:
        circuit, src = A.understand("做一个555闪烁灯电路", offline=True, verbose=False)
        check("回落到模板路径", src["path"] == "template")
        check("路由命中 ne555_blink", "ne555_blink" in src["note"])
        check("电路结构合法", isinstance(circuit.get("parts"), list)
              and circuit["parts"])
        circuit2, src2 = A.understand("两个LED并联", offline=True, verbose=False)
        check("parallel_led 分流", "parallel_led" in src2["note"])
        check("模板互不污染（deepcopy 隔离）",
              circuit2 is not server_demo("两个LED并联"))

    print("== 模板参数注入（用户显式参数被采纳） ==")
    circuit3, src3 = A.understand("5V电源，LED串联470Ω限流电阻后接地",
                                  offline=True, verbose=False)
    vals3 = {p["id"]: (p.get("attrs") or {}).get("value") for p in circuit3["parts"]}
    check("470Ω 注入到电阻", vals3.get("R1") == "470Ω")
    check("注入记录透明（R1 330Ω→470Ω）",
          any(r == "R1" and n == "470Ω" for r, _o, n in src3.get("injected", [])))
    check("注入后偏差报告不再报该参数",
          not any("470" in d for d in A.consistency_check(circuit3, "5V电源，LED串联470Ω限流电阻后接地")))
    circuit4, _ = A.understand("用1k电阻点亮红色LED", offline=True, verbose=False)
    vals4 = {p["id"]: (p.get("attrs") or {}).get("value") for p in circuit4["parts"]}
    check("裸 k 前缀注入（1k→1kΩ）", vals4.get("R1") == "1kΩ")
    check("颜色词注入（红色LED→红）", vals4.get("LED1") == "红")
    # 歧义保护：两个不同阻值时不注入，交给偏差报告提示核对
    circuit5, _ = A.understand("LED电路，用470Ω和1k两个电阻值",
                               offline=True, verbose=False)
    vals5 = {p["id"]: (p.get("attrs") or {}).get("value") for p in circuit5["parts"]}
    check("歧义多值不注入（保持模板默认）", vals5.get("R1") == "330Ω")
    # 共享模板本体不被注入污染
    check("全局模板本体未被污染",
          templates.BUILTIN_TEMPLATES["led_limit"]["parts"][1]["attrs"]["value"] == "330Ω")

    print("== 参数来源标注 ==")
    demo = json.loads((Path(__file__).resolve().parent.parent / "data" / "circuit_templates.json")
                      .read_text(encoding="utf-8"))["led_limit"]
    rows = {r[0]: r for r in A.analyze_parts(demo, "5V供电的LED电路，用330Ω电阻")}
    check("用户指定值识别（5V 电池）",
          rows["BAT1"][3] == "用户指定")
    check("用户指定值识别（330Ω 电阻）", rows["R1"][3] == "用户指定")
    check("约定默认值（LED 颜色）", rows["LED1"][3] == "约定默认值")
    check("AI 补充（地参考）", rows["GND1"][3] == "AI 补充")
    rows2 = {r[0]: r for r in A.analyze_parts(demo, "LED电路")}
    check("类型未提及 → AI 补充", rows2["BAT1"][3] == "AI 补充"
          and rows2["R1"][3] == "AI 补充")
    rows3 = {r[0]: r for r in
             A.analyze_parts(demo, "电池供电的LED限流电阻电路")}
    check("提及类型但未给值 → 约定默认值", rows3["BAT1"][3] == "约定默认值"
          and rows3["R1"][3] == "约定默认值")

    print("== 一致性偏差 ==")
    devs = A.consistency_check(demo, "5V供电的LED电路，用1k电阻")
    check("抓到“1k 未体现在电路值”", any("1kΩ" in d for d in devs))
    check("无虚报（5V/LED/电阻类型均覆盖，不再报缺失）",
          not any("未包含" in d for d in devs))
    dup = {"parts": [{"type": "resistor", "id": "R1"},
                     {"type": "resistor", "id": "R1"}],
           "connections": []}
    check("抓到重复位号", any("重复位号" in d for d in A.consistency_check(dup, "电阻")))
    dev_t = A.consistency_check(demo, "5V供电的LED电路，加一个蜂鸣器")
    check("抓到“描述提到但电路缺失的类型”", any("蜂鸣器" in d for d in dev_t))

    print("== 期望网络独立重算 ==")
    nets = A.expected_nets(demo)
    check("引脚级网络数（led_limit=7 引脚）", len(nets) == 7)
    check("锚点改名 GND", nets.get("BAT1:-") == "GND"
          and nets.get("LED1:-") == "GND")
    check("电源网络对齐", nets.get("BAT1:+") == nets.get("R1:1") == "N1")

    print("== 端到端 run()（产物四件套 + 官方校验） ==")
    with tempfile.TemporaryDirectory() as td:
        rc = A.run("做一个555闪烁灯电路", Path(td) / "e1", offline=True,
                   verbose=False)
        d1 = Path(td) / "e1"
        schs = list(d1.glob("*.kicad_sch"))
        pros = list(d1.glob("*.kicad_pro"))
        check("退出码 0（官方校验全过）", rc == 0)
        check("产物：.kicad_sch", len(schs) == 1)
        check("产物：.kicad_pro", len(pros) == 1)
        check("产物：共享符号库与库表",
              (d1 / "CircuitTutor.kicad_sym").is_file()
              and (d1 / "sym-lib-table").is_file())
        check("产物：生成报告.md", (d1 / "生成报告.md").is_file())
        report = (d1 / "生成报告.md").read_text(encoding="utf-8")
        for section in ("识别结果", "连接关系", "工程惯例补全", "完整性与一致性校验",
                        "一致性偏差", "产物与使用"):
            check(f"报告含章节「{section}」", section in report)

        rc2 = A.run("5V供电的LED电路，用1k电阻", Path(td) / "e2", offline=True,
                    verbose=False)
        report2 = ((Path(td) / "e2") / "生成报告.md").read_text(encoding="utf-8")
        gate2 = json.loads(((Path(td) / "e2") / "校验门禁.json").read_text(encoding="utf-8"))
        from core.spice_enhanced import find_ngspice
        if find_ngspice():
            check("端到端退出码 0（官方校验不受描述偏差影响）", rc2 == 0)
        else:
            check("无 ngspice 时端到端保留未完成门禁", rc2 == 1
                  and gate2["verdict"] == "incomplete"
                  and gate2["blocking_gates"] == ["spice_runtime"]
                  and gate2["gates"]["spice_runtime"]["status"] == "unavailable")
            print("  （跳过端到端官方全通过断言：本机未找到 ngspice CLI）")
        check("端到端报告仍指出 1kΩ 偏差", "1kΩ" in report2)

    print("== 真机联动（kicad-cli 官方校验，若本机装有 KiCad） ==")
    cli = K.find_kicad_cli()
    if cli:
        with tempfile.TemporaryDirectory() as td:
            circuit, _ = A.understand("两个LED并联", offline=True, verbose=False)
            fp, _w = K.export_to_dir(circuit, "case", Path(td), kb=kb)
            erc = K.run_official_erc(fp)
            check("官方 ERC 归零", erc is not None and erc["total"] == 0)
            nets = K.export_official_netlist(fp)
            check("官方网表可导出", nets is not None)
            check("网表与期望一致", nets is not None
                  and A.net_diffs(circuit, nets) == [])
        print(f"  （kicad-cli: {cli}，KiCad {K.kicad_cli_version()}）")
    else:
        print("  （本机未安装 KiCad，跳过）")

    print("== autofix 开关两态 ==")
    # 开关语义：run() 内 `if not no_autofix and config.AUTOFIX:` 二者是与关系，
    # 任一为假即关闭修复。此前无任何用例覆盖「关闭后是否真的不修」，
    # 开关一旦接线错误（如被写成恒真）无人察觉。
    #
    # 注意：9 套内置离线模板本身已自带去耦电容/CTRL 旁路，跑它们走不到修复
    # 分支（fixes 恒空）——开关开着与关着产物一样，测不出开关死活。故这里
    # 用 understand() 注入一个「裸 555」电路（无任何电容），确保修复分支
    # 真的会被触发，开关的两态才有可观测差异。
    bare_555 = {"version": 1,
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
    rep0 = erc_mod.run_erc(bare_555, kb)
    check("注入的裸 555 电路确实触发两类待修警告（用例前提成立）",
          any(i.rule == "缺去耦电容" for i in rep0.issues)
          and any(i.rule == "悬空输入" and "CTRL" in i.message for i in rep0.issues))

    saved_understand = A.understand
    saved_auto = A.config.AUTOFIX
    A.understand = lambda q, offline=False, verbose=True: (
        copy.deepcopy(bare_555), {"note": "测试注入：裸 555（无去耦电容）", "source": "test"})
    try:
        with tempfile.TemporaryDirectory() as td:
            # —— 态一：开关开启（默认）→ 应写入补全条目 ——
            rc_on = A.run("555测试", Path(td) / "on", offline=True,
                          no_clarify=True, verbose=False)
            rep_on = ((Path(td) / "on") / "生成报告.md").read_text(encoding="utf-8")
            sec_on = (rep_on.split("## 二、工程惯例补全（确定性）")[1].split("## 三")[0]
                      if "## 二、工程惯例补全（确定性）" in rep_on else "")
            check("开关开启：退出码 0", rc_on == 0)
            check("开关开启：报告「工程惯例补全」节有条目（非“- 无”）",
                  "- 无" not in sec_on and "电容" in sec_on)

            # —— 态二：--no-autofix 关闭 → 同名电路应无任何补全条目 ——
            rc_off = A.run("555测试", Path(td) / "off", offline=True,
                           no_autofix=True, no_clarify=True, verbose=False)
            rep_off = ((Path(td) / "off") / "生成报告.md").read_text(encoding="utf-8")
            sec_off = (rep_off.split("## 二、工程惯例补全（确定性）")[1].split("## 三")[0]
                       if "## 二、工程惯例补全（确定性）" in rep_off else "")
            check("开关关闭：退出码仍为 0（不因不补全而失败）", rc_off == 0)
            check("开关关闭：报告「工程惯例补全」节为“- 无”", "- 无" in sec_off)
            check("两态报告确实不同（开关真的改变了产物）", rep_on != rep_off)

            # —— 态三：config.AUTOFIX=False 全局关闭（与 CLI 开关的与逻辑等效） ——
            A.config.AUTOFIX = False
            rc_cfg = A.run("555测试", Path(td) / "cfg", offline=True,
                           no_clarify=True, verbose=False)
            rep_cfg = ((Path(td) / "cfg") / "生成报告.md").read_text(encoding="utf-8")
            sec_cfg = (rep_cfg.split("## 二、工程惯例补全（确定性）")[1].split("## 三")[0]
                       if "## 二、工程惯例补全（确定性）" in rep_cfg else "")
            check("config.AUTOFIX=False：等效关闭补全（报告为“- 无”）",
                  rc_cfg == 0 and "- 无" in sec_cfg)

            # —— 态四：关闭时产物应为「未修复」的原始电路（不应改输出却不写报告） ——
            check("开关关闭：产物电路未含电容器件（真的没修，而非只是没写说明）",
                  "capacitor" not in
                  ((Path(td) / "off") / (list((Path(td) / "off").glob("*.kicad_sch"))[0].stem
                                          + ".kicad_sch")).read_text(encoding="utf-8"))
    finally:
        A.understand = saved_understand
        A.config.AUTOFIX = saved_auto
    check("开关用例后 understand 与 config.AUTOFIX 均已还原",
          A.understand is saved_understand and A.config.AUTOFIX is saved_auto)

    # —— 底层 apply() 与开关解耦核对：修复函数本身纯且幂等，开关由调用方判定 ——
    from core import autofix as _af
    _fixed, _fixes = _af.apply(copy.deepcopy(bare_555), kb)
    check("底层 apply() 不受开关影响（始终执行修复，开关由调用方判定）",
          len(_fixes) >= 2 and "capacitor" in {p["type"] for p in _fixed["parts"]})
    check("底层 apply() 幂等（重放无新增修复）", _af.apply(_fixed, kb)[1] == [])

    print(f"\ntest_ai2kicad.py 全部 {PASS} 项断言通过 ✔")
    return 0


def server_demo(query):
    """模板原件（用于验证 deepcopy 隔离）。"""
    import server
    key = catalog.match_template(query, server.FALLBACK_TEMPLATES)
    return server.BUILTIN_TEMPLATES[key or "led_limit"]


if __name__ == "__main__":
    raise SystemExit(main())
