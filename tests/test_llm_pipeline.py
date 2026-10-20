# -*- coding: utf-8 -*-
"""
test_llm_pipeline.py —— LLM 生成链路离线回归（mock 注入，第 17 套件）
=====================================================================
补齐 16 套件的结构性测试盲区：此前 823 条断言全测确定性外围（KiCad 导出 /
几何 / ERC / 符号解析），而核心链路——LLM prompt→CircuitJSON→parse
→repair 循环——零自动化回归（benchmark 是独立脚本、需 Key、不在 run_tests、
CI 也无 Key 不跑）。本套件用 FakeClient 注入预设响应，离线覆盖：

  1. 元件抽取的 LLM 路径（本地未命中 → LLM 兜底，source == "llm"）
     —— 这正是 30 条结构化用例从未触达的链路（local_extract_rate=1.0）
  2. 生成 happy path（GEN 返回合法 JSON → 结构校验通过）
  3. 结构修复闭环（GEN 返回非法 → STRUCT_REPAIR 返回合法 → 通过）
  4. 预算耗尽（始终非法 → 预算封顶 → ValueError，budget.used == 上限）
  5. 取消信号（cancel_event 置位 → OperationCancelled）

零依赖、零网络、零 Key，CI 可直接运行。
"""


# 测试位于 tests/ 子目录：先把项目根插入 sys.path 再 import 项目模块
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import json
import threading

from core import llm
from core.catalog import CATALOG, build_erc_kb
from core.llm import (CallBudget, OperationCancelled, extract_components,
                             generate_circuit_json, validate_circuit_json)

# ---- 一条合法的 LED 限流电路（与 benchmark case 1 同构） ----
GOOD_CIRCUIT = {
    "version": 1,
    "parts": [
        {"type": "battery", "id": "BAT1", "top": 80, "left": 60, "attrs": {"value": "5V"}},
        {"type": "resistor", "id": "R1", "top": 80, "left": 220, "attrs": {"value": "330Ω"}},
        {"type": "led", "id": "LED1", "top": 80, "left": 380, "attrs": {"value": "红"}},
    ],
    "connections": [
        ["BAT1:+", "R1:1", "red", []],
        ["R1:2", "LED1:+", "red", []],
        ["LED1:-", "BAT1:-", "black", []],
    ],
}
# 引脚白名单外（端点缺 ":"）的非法电路，触发结构修复
BAD_CIRCUIT = {
    "version": 1,
    "parts": [
        {"type": "battery", "id": "BAT1", "top": 80, "left": 60},
        {"type": "resistor", "id": "R1", "top": 80, "left": 220},
        {"type": "led", "id": "LED1", "top": 80, "left": 380},
    ],
    "connections": [["BAT1+", "R1:1", "red", []]],   # "BAT1+" 缺冒号 → 结构错误
}
LED_LIB = {t: {"pins": list(CATALOG[t]["pins"])} for t in ("battery", "resistor", "led")}


class FakeClient:
    """伪造 DeepSeekClient：按 system_prompt 的阶段标记返回预设响应，
    零网络、零 Key。mode 控制行为：
      "ok"      —— GEN/REPAIR 一律返回合法电路
      "bad_gen" —— GEN 返回非法，STRUCT_REPAIR 返回合法（测修复闭环）
      "always_bad" —— 全程非法（测预算耗尽）"""

    def __init__(self, mode="ok", extract_names=None):
        self.mode = mode
        self.extract_names = extract_names or ["resistor", "led", "battery"]
        self.calls = []          # [(stage, json_mode)] 记录每次调用

    def chat(self, system_prompt, user_prompt, temperature=0.0, max_tokens=None,
             timeout=None, retries=None, json_mode=False, cancel_event=None):
        # 取消信号优先：置位即抛，模拟 SSE 客户端断开
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled("测试：cancel_event 已置位")
        # 阶段识别（与 deepseek_prompt 各 *_PROMPT 常量的特征串对齐）
        if "抽取需要用到的元件类型名称" in system_prompt:        # EXTRACT
            self.calls.append(("extract", json_mode))
            return json.dumps(self.extract_names, ensure_ascii=False)
        if "电子电路规划助手" in system_prompt:                   # PLAN（纯文本）
            self.calls.append(("plan", json_mode))
            return "规划：电源→限流电阻→LED→回地"
        if "转换成一个合法的 CircuitJSON" in system_prompt:       # GEN
            self.calls.append(("generate", json_mode))
            if self.mode == "ok":
                return json.dumps(GOOD_CIRCUIT, ensure_ascii=False)
            if self.mode in ("bad_gen", "always_bad"):
                return json.dumps(BAD_CIRCUIT, ensure_ascii=False)
        if "格式 / 结构】上有误" in system_prompt:                 # STRUCT_REPAIR
            self.calls.append(("repair_struct", json_mode))
            return json.dumps(GOOD_CIRCUIT, ensure_ascii=False)   # ok/bad_gen 均修好
        if "电气规则检查" in system_prompt:                        # ERC_REPAIR
            self.calls.append(("repair_erc", json_mode))
            return json.dumps(GOOD_CIRCUIT, ensure_ascii=False)
        return json.dumps(GOOD_CIRCUIT, ensure_ascii=False)       # 兜底


# =====================================================================
# 测试用例
# =====================================================================

def test_extract_via_llm():
    """① LLM 元件抽取路径：本地未命中的口语化需求 → 走 LLM → source=='llm'。
    这是 30 条结构化用例从未触达的链路（它们 local_extract_rate=1.0）。"""
    client = FakeClient(extract_names=["buzzer", "battery"])
    budget = CallBudget(max_calls=5)
    # "做个能发出声音的装置" 本地匹配器返回空 → 必须问 LLM
    types, source = extract_components("做个能发出声音的装置", client, budget)
    assert source == "llm", f"应走 LLM 抽取，实际 source={source}"
    assert "buzzer" in types and "battery" in types, f"应识别 buzzer/battery，实际 {types}"
    assert budget.used == 1, f"抽取应耗 1 次预算，实际 {budget.used}"
    # 阶段记录里有 extract
    assert any(s[0] == "extract" for s in client.calls), "应有 extract 调用"
    return "LLM 抽取路径：source=llm，识别 buzzer/battery，耗 1 次预算"


def test_generate_happy_path():
    """② 生成 happy path：GEN 返回合法 JSON → 结构校验通过。"""
    client = FakeClient(mode="ok")
    budget = CallBudget(max_calls=5)
    stages = []
    circuit = generate_circuit_json("点亮一个LED", LED_LIB, client,
                                    max_retry=2, budget=budget,
                                    on_progress=lambda s, d: stages.append(s))
    errs = validate_circuit_json(circuit, LED_LIB)
    assert not errs, f"合法电路不应有结构错误：{errs}"
    assert "generate" in stages, "应经历 generate 阶段"
    assert "done" in stages, "应正常收尾 done"
    # PLAN + GEN 至少 2 次调用
    assert budget.used >= 2, f"应至少 plan+gen 两次调用，实际 {budget.used}"
    return f"生成 happy path：结构校验通过，阶段={stages}，调用={budget.used}次"


def test_generate_struct_repair():
    """③ 结构修复闭环：GEN 非法 → STRUCT_REPAIR 返回合法 → 最终通过。"""
    client = FakeClient(mode="bad_gen")
    budget = CallBudget(max_calls=5)
    stages = []
    circuit = generate_circuit_json("点亮LED", LED_LIB, client,
                                    max_retry=2, budget=budget,
                                    on_progress=lambda s, d: stages.append(s))
    errs = validate_circuit_json(circuit, LED_LIB)
    assert not errs, f"修复后应无结构错误：{errs}"
    assert "repair_struct" in stages, f"应经历结构修复，实际阶段={stages}"
    # 有 generate + repair_struct 两次
    gen_calls = [s for s in client.calls if s[0] == "generate"]
    repair_calls = [s for s in client.calls if s[0] == "repair_struct"]
    assert gen_calls and repair_calls, f"应有 generate 和 repair_struct 调用：{client.calls}"
    return f"结构修复闭环：GEN 非法→REPAIR 合法，阶段含 repair_struct，调用={budget.used}次"


def test_budget_exhaustion():
    """④ 预算耗尽：始终非法 + 低预算 → 抛 ValueError，budget.used 命中上限。"""
    client = FakeClient(mode="always_bad")  # GEN/REPAIR 全返回非法 BAD_CIRCUIT
    budget = CallBudget(max_calls=2)        # 极低预算：plan+gen 即耗尽
    try:
        generate_circuit_json("点亮LED", LED_LIB, client, max_retry=2,
                              budget=budget, on_progress=lambda s, d: None)
        raise AssertionError("预算耗尽应抛 ValueError")
    except ValueError as e:
        assert "预算" in str(e) or "结构" in str(e), f"错误信息应提及预算/结构：{e}"
    assert budget.used == 2, f"预算应耗尽到上限 2，实际 {budget.used}"
    return f"预算耗尽：抛 ValueError，budget.used={budget.used} 命中上限"


def test_cancel_event():
    """⑤ 取消信号：cancel_event 置位 → chat 抛 OperationCancelled 向上传播。"""
    client = FakeClient(mode="ok")
    budget = CallBudget(max_calls=5)
    cancel = threading.Event()
    cancel.set()   # 进入即取消
    raised = None
    try:
        generate_circuit_json("点亮LED", LED_LIB, client, max_retry=2,
                              budget=budget, cancel_event=cancel,
                              on_progress=lambda s, d: None)
        # plan 阶段调用 chat 时会因 cancel_event 抛 OperationCancelled；
        # 但 plan 有 try/except BudgetExhausted 不接 OperationCancelled，
        # 故应直接向上抛出
        pass
    except OperationCancelled as e:
        raised = e
    except ValueError as e:
        # 某些路径下 OperationCancelled 被外层转 ValueError——此时必须
        # 真的携带取消语义才算生效（此前 `or True` 使该分支恒真、
        # 断言形同虚设，任何 ValueError 都会被当作“取消生效”放行）
        assert ("取消" in str(e) or "Cancelled" in str(e)), \
            f"ValueError 分支仅当错误信息含取消语义才算生效，实际：{e}"
        raised = e
    assert raised is not None, "cancel_event 置位应阻断生成（未抛任何异常）"
    # 取消发生在首个 chat 调用之前/之中，不应有任何成功的 LLM 调用被记账
    assert budget.used == 0, \
        f"取消后不应消耗任何 LLM 调用预算，实际 budget.used={budget.used}"
    assert client.calls == [], \
        f"取消后不应发出任何 LLM 请求，实际调用序列={client.calls}"
    return "取消信号：cancel_event 置位后生成被阻断，未发任何 LLM 请求"


# =====================================================================
# 连接正确性护栏（2026-09-10 新增）：引脚复用检测 + 内置模板零误报
# =====================================================================
import json as _json
import pathlib as _pathlib


def test_builtin_templates_clean():
    """全部内置模板必须零结构错误 —— 这是新护栏的【防误报基线】。

    背景：引脚复用检测曾把「电源/地标记件被多条连接引用」判为非法，
    结果 10 套内置模板里 4 套（NE555/反相放大器/电位器调光/两路LED并联）
    被误判 —— 而这些模板正是 LLM 失败时的兜底产物，一旦被判非法，
    兜底路径会连带崩掉。本用例把「内置模板必须全部合法」钉成硬约束。
    """
    tp = _pathlib.Path(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))) \
        / "data" / "circuit_templates.json"
    raw = _json.loads(tp.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else list(raw.values())
    lib = {t: {"pins": list(e["pins"].keys())} for t, e in CATALOG.items()}
    checked, bad = 0, []
    for t in items:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or t.get("title") or "?"
        if "错误示范" in name:          # 负例模板本就非法，跳过
            continue
        circ = t.get("circuit") if isinstance(t.get("circuit"), dict) else t
        parts, conns = circ.get("parts"), circ.get("connections")
        if not parts or not conns:
            continue
        checked += 1
        errs = validate_circuit_json(
            {"version": 1, "parts": parts, "connections": conns}, lib)
        if errs:
            bad.append(f"{name}: {errs[:1]}")
    assert checked >= 8, f"内置模板数量异常（只检到 {checked} 套）"
    assert not bad, "内置模板出现结构误报（护栏过严）：" + " | ".join(bad)
    return f"{checked} 套内置模板零结构错误"


def test_pin_reuse_detected():
    """功能引脚被 3 条以上连接汇接必须被检出（先红后绿：喂缺陷必须报）。"""
    lib = {t: {"pins": list(e["pins"].keys())} for t, e in CATALOG.items()}
    circ = {"version": 1,
            "parts": [{"type": "battery", "id": "B1"},
                      {"type": "resistor", "id": "R1"},
                      {"type": "resistor", "id": "R2"},
                      {"type": "resistor", "id": "R3"},
                      {"type": "led", "id": "L1"},
                      {"type": "gnd", "id": "G1"}],
            "connections": [["B1:+", "R1:1"], ["R1:2", "R2:1"], ["R2:2", "R3:1"],
                            ["R3:2", "L1:+"], ["L1:-", "G1:1"], ["B1:-", "G1:1"],
                            # 下面三条把 R1:2 变成 4 条引用 —— 三线汇于功能引脚
                            ["R1:2", "R2:2"], ["R1:2", "R3:2"], ["R1:2", "L1:+"]]}
    errs = validate_circuit_json(circ, lib)
    assert any("R1:2" in e and "条连接引用" in e for e in errs), \
        f"引脚复用未被检出，实际错误：{errs}"
    return "功能引脚 4 线汇接被正确检出"


def test_marker_fanout_allowed():
    """电源/地标记件的多路扇出必须放行（合法画法，不得误报）。"""
    lib = {t: {"pins": list(e["pins"].keys())} for t, e in CATALOG.items()}
    circ = {"version": 1,
            "parts": [{"type": "battery", "id": "BAT1"}, {"type": "vcc", "id": "V1"},
                      {"type": "resistor", "id": "R1"}, {"type": "resistor", "id": "R2"},
                      {"type": "led", "id": "LED1"}, {"type": "gnd", "id": "G1"}],
            "connections": [["BAT1:+", "V1:1"], ["V1:1", "R1:1"], ["V1:1", "R2:1"],
                            ["R1:2", "LED1:+"], ["LED1:-", "G1:1"],
                            ["BAT1:-", "G1:1"], ["R2:2", "G1:1"]]}
    errs = validate_circuit_json(circ, lib)
    assert not any("条连接引用" in e for e in errs), \
        f"标记件扇出被误报：{errs}"
    return "GND/VCC 标记件多路扇出未被误报"


def test_unwired_marker_flagged():
    """悬空的 vcc/gnd 标记件应被提示（空符号无意义）。"""
    lib = {t: {"pins": list(e["pins"].keys())} for t, e in CATALOG.items()}
    circ = {"version": 1,
            "parts": [{"type": "battery", "id": "B1"}, {"type": "resistor", "id": "R1"},
                      {"type": "gnd", "id": "G1"}, {"type": "gnd", "id": "G2"}],
            "connections": [["B1:+", "R1:1"], ["R1:2", "G1:1"], ["B1:-", "G1:1"]]}
    errs = validate_circuit_json(circ, lib)
    assert any("G2" in e and "没有连接任何网络" in e for e in errs), \
        f"悬空标记件未被提示：{errs}"
    return "悬空 gnd 标记件被正确提示"


# =====================================================================
# JSON 抽取鲁棒性（extract_json_object）
# =====================================================================

def test_json_extract_robustness():
    """模型输出前后带散文时，必须抠出**完整且干净**的 JSON 对象。

    回归由来（2026-09-09）：原实现用 find('{') + rfind('}') 划界，前提是
    "最后一个 } 就是 JSON 的结尾"。但模型常在 JSON 后附解释，解释里又会
    提到花括号（如"其中 {key} 表示位号"），rfind 便会把散文一起吞进来，
    得到 `{"a":1}（其中 {key}` 这种脏串——既解析不了又丢了原文。
    改为花括号计数扫描（跳过字符串字面量内的括号）后，深度归零处即结尾。
    """
    from core.llm import extract_json_object
    cases = [
        ('{"a":1}', {"a": 1}, '纯 JSON'),
        ('{"a":1}', {"a": 1}, '前后无散文'),
        ('好的，这是电路：{"a":1} 希望有帮助！', {"a": 1}, '前后有散文'),
        ('电路如下：{"a":1}（其中 {key} 表示位号）', {"a": 1}, '尾注含花括号'),
        ('{"a":1} 还有 {"b":2}', {"a": 1}, '多个对象只取第一个'),
        ('```json\n{"a":1}\n```\n说明：{见上}', {"a": 1}, '代码块+尾注含花括号'),
        ('{"note":"含 } 的串","b":2}', {"note": "含 } 的串", "b": 2},
         '字符串字面量内的 } 不能提前截断'),
        ('{"a":{"b":1}} 完毕}', {"a": {"b": 1}}, '嵌套对象+尾注多余 }'),
        ("{'a':1}", {"a": 1}, '单引号兜底'),
        ('{"a":1,}', {"a": 1}, '尾逗号清洗'),
    ]
    for text, expect, label in cases:
        got = extract_json_object(text)
        assert got == expect, f"{label}：期望 {expect!r}，实际 {got!r}"
    # 截断/无 JSON 时应原样返回字符串（上层据此重试，而非把脏串当对象用）
    for text, label in [('{"a":1', '截断不配对'), ('纯文本无 JSON', '无花括号')]:
        got = extract_json_object(text)
        assert isinstance(got, str), f"{label}：应原样返回字符串，实际 {got!r}"
    return f"{len(cases)} 条脏输出全部正确抽取，2 条异常原样返回"


def test_circuitjson_schema_contract():
    """Schema 必须声明 Draft 2020-12，并覆盖当前 CircuitJSON 数据合同。"""
    from core.circuit_schema import CIRCUIT_JSON_SCHEMA

    assert CIRCUIT_JSON_SCHEMA["$schema"] == \
        "https://json-schema.org/draft/2020-12/schema"
    assert CIRCUIT_JSON_SCHEMA["properties"]["version"]["const"] == 1
    # 现有内置模板只有 parts/connections，version 不能误设为必填。
    assert set(("parts", "connections")).issubset(
        set(CIRCUIT_JSON_SCHEMA["required"]))
    assert "version" not in CIRCUIT_JSON_SCHEMA["required"]
    part = CIRCUIT_JSON_SCHEMA["$defs"]["part"]
    assert set(("type", "id")).issubset(set(part["required"]))
    assert part["properties"]["rotate"]["enum"] == [0, 90, 180, 270]
    connection = CIRCUIT_JSON_SCHEMA["$defs"]["connection"]
    assert connection["type"] == "array"
    assert connection["minItems"] == 2
    assert connection["maxItems"] == 4
    path_schema = connection["prefixItems"][3]
    path_variants = path_schema["oneOf"]
    assert {variant.get("type") for variant in path_variants} == {"array"}
    assert any(variant.get("items") == {"type": "number"}
               for variant in path_variants), \
        "路由点必须允许扁平数字数组"
    assert any(variant.get("items") == {"$ref": "#/$defs/path_point"}
               for variant in path_variants), \
        "路由点必须允许二维点数组"


def test_json_extract_repairs_adjacent_fields_with_trace():
    """漏掉确定性字段逗号时可修复，且解析轨迹不泄露完整输出。"""
    from core.llm import extract_json_object_with_trace

    raw = ('{"version":1 "parts":[{"type":"resistor" "id":"R1" '
           '"top":0 "left":0 "attrs":{"value":"1k"}}] '
           '"connections":[]}')
    parsed, trace = extract_json_object_with_trace(raw)
    assert parsed["version"] == 1
    assert parsed["parts"][0]["id"] == "R1"
    assert parsed["connections"] == []
    assert "missing_comma" in trace["strategies"]
    assert "output" not in trace and "raw" not in trace
    assert all(len(str(value)) < 200 for value in trace.values())


# =====================================================================
# 入口
# =====================================================================

def test_model_env_wiring():
    """DEEPSEEK_MODEL 环境变量必须真的生效（防形参默认值顶掉 env）。

    回归由来（2026-09-10）：`__init__` 曾把形参写成 `model: str = DEFAULT_MODEL`，
    形参默认值在调用时即被填充，永远不会落到 env / 类常量回退分支，
    于是 `DEEPSEEK_MODEL=deepseek-v4-pro` 静默失效。
    本测试直接调用（不经 env 子进程）验证默认态，再临时改 env 验证接线。
    """
    import os
    from core import llm as _llm

    key = "DEEPSEEK_MODEL"
    old = os.environ.get(key)

    try:
        # 1) 修好之后：形参默认必须是 None，否则 env 分支不可达
        dflt = _llm.DeepSeekClient.__init__.__defaults__
        assert dflt[2] is None, (
            f"__init__ 的 model 形参默认值应为 None，实测 {dflt[2]!r}；"
            f"非 None 会把 DEEPSEEK_MODEL 环境变量顶掉")

        # 2) env 生效（config.MODEL 在 import 期已固化，故走显式回退分支验证）
        assert getattr(_llm.app_config, "MODEL", None) is not None, \
            "core/config.py 未定义 MODEL，DEEPSEEK_MODEL 无处落地"

        # 3) 显式入参优先于一切
        c = _llm.DeepSeekClient(api_key="sk-x", model="deepseek-v4-pro")
        assert c.model == "deepseek-v4-pro", c.model

        # 4) None / 空白串一律回退（不得把模型名置空）
        for blank in (None, "", "   "):
            c = _llm.DeepSeekClient(api_key="sk-x", model=blank)
            assert c.model == _llm.DeepSeekClient.DEFAULT_MODEL, \
                f"model={blank!r} 未回退到默认，实测 {c.model!r}"

        # 5) 默认态就是滚动别名，不得写成带日期的快照名
        c = _llm.DeepSeekClient(api_key="sk-x")
        assert c.model == "deepseek-flash", c.model
        assert "0731" not in c.model and "0813" not in c.model, \
            "默认模型不应写死带日期的快照名（第三方网关支持不一致）"
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old
    return "DEEPSEEK_MODEL 接线与回退层级正确（形参默认 None）"


# =====================================================================
# 入口
# =====================================================================

def main():
    tests = [
        ("LLM 抽取路径", test_extract_via_llm),
        ("生成 happy path", test_generate_happy_path),
        ("结构修复闭环", test_generate_struct_repair),
        ("预算耗尽", test_budget_exhaustion),
        ("取消信号", test_cancel_event),
        ("内置模板零误报", test_builtin_templates_clean),
        ("引脚复用检出", test_pin_reuse_detected),
        ("标记件扇出放行", test_marker_fanout_allowed),
        ("悬空标记件提示", test_unwired_marker_flagged),
        ("模型 env 接线", test_model_env_wiring),
        ("JSON 抽取鲁棒性", test_json_extract_robustness),
        ("CircuitJSON Schema 合同", test_circuitjson_schema_contract),
        ("漏逗号解析轨迹", test_json_extract_repairs_adjacent_fields_with_trace),
    ]
    print("=" * 64)
    print(" test_llm_pipeline · LLM 生成链路离线回归（mock 注入）")
    print("=" * 64)
    passed = 0
    for name, fn in tests:
        try:
            msg = fn()
            print(f"  [PASS] {name} —— {msg}")
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {name} —— {type(e).__name__}: {e}")
    print("-" * 64)
    print(f" 结果：{passed}/{len(tests)} 通过")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
