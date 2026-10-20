# -*- coding: utf-8 -*-
"""
test_pipeline.py —— 智绘电路“准确度”全链路测试（Python 侧）
=============================================================
对 circuit_templates.json 里的每份电路跑确定性 ERC（erc_engine），
断言预期结果，并把结果写成 erc_results.json 交给前端渲染测试
（test_render.js）做“ERC 标错渲染”。同时导出元件目录前端清单
frontend_manifest.json，供 Node 侧校验“目录-符号同步”。

运行：python test_pipeline.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.catalog import CATALOG, build_erc_kb, to_frontend_manifest
from core.erc import run_erc

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    with open(os.path.join(os.path.dirname(HERE), "data", "circuit_templates.json"), encoding="utf-8") as f:
        circuits = json.load(f)
    kb = build_erc_kb(CATALOG)

    results = {}
    ok = True
    for name, c in circuits.items():
        rep = run_erc(c, kb)
        results[name] = rep.to_dict()
        rules = [(i.severity, i.rule) for i in rep.issues]
        print(f"== {name}: passed={rep.passed} issues={rules}")

    # ---- 断言预期（每条都对应一类真实错误场景） ----
    def rules_of(name):
        return [i["rule"] for i in results[name]["issues"]]

    assert results["led_limit"]["passed"], "LED 限流电路应通过 ERC"
    assert not rules_of("led_limit"), f"LED 限流电路不应有问题，实际 {rules_of('led_limit')}"

    assert results["ne555_blink"]["passed"], f"555 电路应通过 ERC：{rules_of('ne555_blink')}"
    assert not rules_of("ne555_blink"), f"555 电路不应有问题，实际 {rules_of('ne555_blink')}"

    assert results["opamp_inv"]["passed"], f"反相放大器应通过 ERC：{rules_of('opamp_inv')}"

    # ---- 扩容模板（2026-09-08）：全部必须零问题通过，保证兜底质量 ----
    for name in ("pot_divider", "button_buzzer", "diode_protect",
                 "parallel_led", "npn_switch", "zener_reg"):
        assert results[name]["passed"], f"{name} 应通过 ERC：{rules_of(name)}"
        assert not rules_of(name), f"{name} 应零问题，实际 {rules_of(name)}"

    bad = results["led_no_limit"]
    assert not bad["passed"], "错误示范电路必须不通过"
    assert "重复位号" in rules_of("led_no_limit"), "应抓到重复位号 fatal"
    assert "LED缺限流电阻" in rules_of("led_no_limit"), "应抓到 LED 缺限流 major"
    assert rules_of("led_no_limit").count("未连接元件") == 2, "应抓到 2 个未连接元件"

    # ================================================================
    # 2026-09-07 二轮修复的验收用例（极性检查 + 参数合理性检查）。
    # 这些电路内联在测试里（circuit_templates.json 不动，保持渲染测试稳定）。
    # ================================================================
    print("\n== 二轮修复验收用例：极性 / 参数合理性 ==")

    # 用例 A（核心验收）：LED 直连反接 —— 必须报“极性反接” major，
    # 且绝不能再误报 fatal“电源短路”（旧目录把 LED “-” 记成 GND 时的缺陷）
    led_reverse = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "led", "id": "LED1", "attrs": {"value": "红"}},
        ],
        "connections": [
            ["BAT1:+", "LED1:-", "red", []],
            ["LED1:+", "BAT1:-", "black", []],
        ],
    }
    r = run_erc(led_reverse, kb)
    rules = [(i.severity, i.rule) for i in r.issues]
    print(f"  LED 直连反接    : {rules}")
    assert ("major", "极性反接") in rules, f"LED 反接必须报出“极性反接” major，实际 {rules}"
    assert not r.fatals, f"LED 反接绝不能出现 fatal（尤其“电源短路”），实际 {[i.message for i in r.fatals]}"
    assert all(i.rule != "电源短路" for i in r.issues), "LED 反接不得触发“电源短路”规则"
    # 极性反接的 issue 必须带两个引脚（渲染层据此画红圈）
    pol = next(i for i in r.issues if i.rule == "极性反接")
    assert sorted(pol.pins) == ["LED1:+", "LED1:-"], f"极性反接应把两脚放进 pins，实际 {pol.pins}"

    # 用例 B：LED 正确接法（电池+ -> 电阻 -> LED+ -> LED- -> 电池-）—— 无 major/fatal
    led_ok = {
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
    r = run_erc(led_ok, kb)
    print(f"  LED 正确接法    : passed={r.passed} issues={[(i.severity, i.rule) for i in r.issues]}")
    assert r.passed, f"标准限流电路必须通过，实际 {[(i.severity, i.rule, i.message) for i in r.issues]}"
    assert not r.issues, f"标准限流电路连 warning 都不该有，实际 {[(i.severity, i.rule) for i in r.issues]}"

    # 用例 C：LED 直连电源（无电阻）—— 仍报“LED缺限流电阻”，且不是极性/短路问题
    led_direct = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "led", "id": "LED1", "attrs": {"value": "红"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "LED1:+", "red", []],
            ["LED1:-", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }
    r = run_erc(led_direct, kb)
    rules = [(i.severity, i.rule) for i in r.issues]
    print(f"  LED 直连电源    : {rules}")
    assert ("major", "LED缺限流电阻") in rules, f"LED 直连电源必须报“LED缺限流电阻”，实际 {rules}"
    assert all(i.rule != "极性反接" for i in r.issues), "正向直连不是极性问题，不得误报"

    # 用例 D：LED 串 1Ω 电阻 —— warning“LED限流电阻过小”，且不升级成 major/fatal
    led_1ohm = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "1Ω"}},
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
    r = run_erc(led_1ohm, kb)
    rules = [(i.severity, i.rule) for i in r.issues]
    print(f"  LED 串 1Ω 电阻  : {rules}")
    assert ("warning", "LED限流电阻过小") in rules, f"1Ω 限流电阻必须报 warning，实际 {rules}"
    assert "1Ω" in next(i.message for i in r.issues if i.rule == "LED限流电阻过小"), "提示里应写明实际阻值"
    assert not r.fatals and not r.majors, f"串了电阻属于参数问题，不应报 major/fatal，实际 {rules}"

    # 用例 E：0Ω 电阻跨接电源两端 —— fatal“0Ω电阻短路”
    r_zero = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "0Ω"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "R1:1", "red", []],
            ["R1:2", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }
    r = run_erc(r_zero, kb)
    rules = [(i.severity, i.rule) for i in r.issues]
    print(f"  0Ω 电阻跨电源   : {rules}")
    assert ("fatal", "0Ω电阻短路") in rules, f"0Ω 电阻跨电源必须报 fatal，实际 {rules}"
    assert not r.passed

    # 用例 F：555 闪烁灯（结构与 demo 的 ne555_blink 相同）—— DISCH 改 OUT 后仍通过
    ne555_copy = {
        "parts": [
            {"type": "vcc", "id": "V1", "attrs": {"value": "5V"}},
            {"type": "ne555", "id": "U1", "attrs": {"value": "NE555"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "1k"}},
            {"type": "resistor", "id": "R2", "attrs": {"value": "10k"}},
            {"type": "resistor", "id": "R3", "attrs": {"value": "330Ω"}},
            {"type": "capacitor", "id": "C1", "attrs": {"value": "10µF"}},
            {"type": "capacitor", "id": "C2", "attrs": {"value": "100nF"}},
            {"type": "capacitor", "id": "C3", "attrs": {"value": "100nF"}},
            {"type": "led", "id": "LED1", "attrs": {"value": "红"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["V1:1", "R1:1", "red", []],
            ["R1:2", "U1:DISCH", "red", []],
            ["U1:DISCH", "R2:1", "red", []],
            ["R2:2", "U1:THR", "red", []],
            ["U1:THR", "U1:TRIG", "red", []],
            ["U1:TRIG", "C1:+", "red", []],
            ["C1:-", "GND1:1", "black", []],
            ["U1:CTRL", "C2:+", "blue", []],
            ["C2:-", "GND1:1", "black", []],
            ["V1:1", "U1:RESET", "red", []],
            ["V1:1", "U1:VCC", "red", []],
            ["V1:1", "C3:+", "red", []],
            ["C3:-", "GND1:1", "black", []],
            ["U1:OUT", "R3:1", "green", []],
            ["R3:2", "LED1:+", "green", []],
            ["LED1:-", "GND1:1", "black", []],
            ["U1:GND", "GND1:1", "black", []],
        ],
    }
    r = run_erc(ne555_copy, kb)
    rules = [(i.severity, i.rule) for i in r.issues]
    print(f"  555 闪烁灯副本   : passed={r.passed} issues={rules}")
    assert not r.fatals and not r.majors, f"DISCH 改 OUT 后 555 电路仍应通过，实际 {rules}"
    assert not r.issues, f"555 电路连 warning 都不该有，实际 {rules}"

    # ================================================================
    # 对撞矩阵重构回归（三类正常教学拓扑 + 两个边界）。
    # 对应目录层建模：晶体管 C/E 双 PASSIVE、7805 IN=PWR_IN/OUT=PWR_OUT、
    # vcc/gnd 标记件不计竞争源、电容反接降级 warning。
    # ================================================================
    print("\n== 建模重构回归：供电/输出分离的对撞矩阵 ==")

    # 拓扑 1：电池正极接 VCC 标记（标记件是网络标签，不算第二个电源源）
    bat_vcc = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "vcc", "id": "V1", "attrs": {"value": "5V"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "330Ω"}},
            {"type": "led", "id": "LED1", "attrs": {"value": "红"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "V1:1", "red", []],
            ["V1:1", "R1:1", "red", []],
            ["R1:2", "LED1:+", "red", []],
            ["LED1:-", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }
    r = run_erc(bat_vcc, kb)
    print(f"  电池+VCC 标记   : passed={r.passed} issues={[(i.severity, i.rule) for i in r.issues]}")
    assert not r.fatals and not r.majors, \
        f"电池+VCC 标记是正常画法，不得报 fatal/major，实际 {[(i.severity, i.rule, i.message) for i in r.issues]}"

    # 拓扑 2：7805 稳压输出挂 VCC 轨（OUT=PWR_OUT 供电，与 VCC 标记共网正常）
    reg_7805 = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "9V"}},
            {"type": "regulator_7805", "id": "U1"},
            {"type": "capacitor", "id": "C1", "attrs": {"value": "100nF"}},
            {"type": "vcc", "id": "V1", "attrs": {"value": "5V"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "330Ω"}},
            {"type": "led", "id": "LED1", "attrs": {"value": "红"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "U1:IN", "red", []],
            ["U1:OUT", "V1:1", "red", []],
            ["U1:GND", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
            ["C1:+", "U1:IN", "red", []],
            ["C1:-", "GND1:1", "black", []],
            ["V1:1", "R1:1", "red", []],
            ["R1:2", "LED1:+", "red", []],
            ["LED1:-", "GND1:1", "black", []],
        ],
    }
    r = run_erc(reg_7805, kb)
    print(f"  7805 输出挂轨   : passed={r.passed} issues={[(i.severity, i.rule) for i in r.issues]}")
    assert not r.fatals and not r.majors, \
        f"7805 输出接 VCC 轨是正常供电拓扑，实际 {[(i.severity, i.rule, i.message) for i in r.issues]}"

    # 拓扑 3：PNP 高边开关（E 直挂电源轨）—— 旧目录 E=OUT 会伪报“网络竞争”
    # fatal；C/E 改双 PASSIVE 后方向由电路决定，必须零 fatal/major
    pnp_high_side = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "pnp", "id": "Q1"},
            {"type": "resistor", "id": "R1", "attrs": {"value": "1k"}},
            {"type": "resistor", "id": "R2", "attrs": {"value": "330Ω"}},
            {"type": "pushbutton", "id": "SW1"},
            {"type": "led", "id": "LED1", "attrs": {"value": "红"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "Q1:E", "red", []],
            ["Q1:C", "R2:1", "red", []],
            ["R2:2", "LED1:+", "red", []],
            ["LED1:-", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
            ["SW1:1", "BAT1:-", "black", []],
            ["SW1:2", "R1:1", "black", []],
            ["R1:2", "Q1:B", "black", []],
        ],
    }
    r = run_erc(pnp_high_side, kb)
    print(f"  PNP 高边开关    : passed={r.passed} issues={[(i.severity, i.rule) for i in r.issues]}")
    assert not r.fatals and not r.majors, \
        f"PNP 高边开关（E 接电源轨）不得报 fatal/major，实际 {[(i.severity, i.rule, i.message) for i in r.issues]}"

    # 边界 1：双电池正极并联 —— 真实错误，fatal“电源冲突”必须保持
    dual_battery = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "battery", "id": "BAT2", "attrs": {"value": "5V"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "BAT2:+", "red", []],
            ["BAT1:-", "GND1:1", "black", []],
            ["BAT2:-", "GND1:1", "black", []],
        ],
    }
    r = run_erc(dual_battery, kb)
    rules = [(i.severity, i.rule) for i in r.issues]
    print(f"  双电池并联      : {rules}")
    assert ("fatal", "电源冲突") in rules, f"双电池并联必须报 fatal 电源冲突，实际 {rules}"
    assert not r.passed

    # 边界 2：电容反接 —— 降级为 warning“电容极性提示”，
    # 绝不再按 major“极性反接”误伤无极性电容
    cap_reverse = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "capacitor", "id": "C1", "attrs": {"value": "100µF"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["C1:+", "GND1:1", "black", []],
            ["C1:-", "BAT1:+", "red", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }
    r = run_erc(cap_reverse, kb)
    rules = [(i.severity, i.rule) for i in r.issues]
    print(f"  电容反接        : {rules}")
    assert ("warning", "电容极性提示") in rules, f"电容反接应报 warning 提示，实际 {rules}"
    assert ("major", "极性反接") not in rules, \
        f"电容已移出 major 极性表，不得按“极性反接”误伤，实际 {rules}"

    # ---- 导出给前端 ----
    with open(os.path.join(HERE, "erc_results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    with open(os.path.join(HERE, "frontend_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(to_frontend_manifest(), f, ensure_ascii=False, indent=2)
    print(f"\nerc_results.json / frontend_manifest.json 已写出（{len(CATALOG)} 种元件）")
    print("test_pipeline.py 全部断言通过 ✔")


if __name__ == "__main__":
    main()
