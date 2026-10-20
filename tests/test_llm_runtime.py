# -*- coding: utf-8 -*-
"""
test_llm_runtime.py —— 运行时优化项的离线回归测试（不联网、不依赖 API Key）
======================================================================
覆盖：
1. CallBudget           预算计数 / 超限拦截
2. extract_components   本地匹配短路（命中免 LLM / 无命中不误报）
3. DeepSeekClient       429/5xx 指数退避重试；400 不重试；keep-alive 连接复用；
                        json_mode（response_format）；cancel_event 取消止损
4. generate_circuit_json 预算封顶（耗尽→ValueError 模板兜底信号）+ 进度回调顺序
5. 修复模板拆分          STRUCT_REPAIR_PROMPT / ERC_REPAIR_PROMPT 各司其职；
                        json_mode / cancel_event 在流水线内正确透传
6. 数组响应重试语义      误返回数组时按 max_retry（默认 2）重试，耗尽走模板兜底

用法：python test_llm_runtime.py
"""


# 测试位于 tests/ 子目录：先把项目根插入 sys.path 再 import 项目模块
import os as _os
import sys as _sys
_PROJECT_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _PROJECT_ROOT)
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import llm

PASS = 0


def check(name, cond):
    global PASS
    if not cond:
        raise AssertionError(f"失败：{name}")
    PASS += 1
    print(f"  ✔ {name}")


# =====================================================================
# 1. CallBudget
# =====================================================================
print("== CallBudget 调用预算 ==")
b = llm.CallBudget(3)
check("初始 remaining=3", b.remaining == 3 and not b.exhausted)
b.spend(); b.spend(); b.spend()
check("花满 3 次后 exhausted", b.exhausted and b.remaining == 0)
try:
    b.spend()
    check("超限应抛 BudgetExhausted", False)
except llm.BudgetExhausted:
    check("超限抛 BudgetExhausted", True)

# =====================================================================
# 2. extract_components 本地短路（client=None，全程零 LLM 调用）
# =====================================================================
print("== extract_components 本地短路 ==")
types, source = llm.extract_components("用555定时器做一个LED闪烁灯，带按键和电容")
check("多元件句本地命中", source == "local" and
      set(types) == {"capacitor", "led", "ne555", "pushbutton"})
types, source = llm.extract_components("画一个同相比例放大器，增益11倍")
check("负向关键词不误伤（同相≠反相）", source == "local" and types == ["opamp_noninverting"])
types, source = llm.extract_components("帮我写一段排序算法")
check("无关输入返回 none", source == "none" and types == [])


# =====================================================================
# 3. DeepSeekClient 重试 / 连接复用（本地假 OpenAI 兼容服务）
# =====================================================================
class FakeAPI(BaseHTTPRequestHandler):
    """可编程的假 LLM 服务：fail_status 连续返回 N 次后恢复 200。
    last_body  记录最近一次请求体（供 json_mode 的 response_format 断言）；
    auto_cancel 若非 None：收到请求即置位（模拟 SSE 客户端在请求进行中断开）。"""
    protocol_version = "HTTP/1.1"      # 支持 keep-alive
    fail_status = 0                    # 0 = 不失败
    fail_count = 0
    requests = 0
    connections = 0
    last_body = None
    auto_cancel = None

    def setup(self):
        super().setup()
        type(self).connections += 1

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        cls = type(self)
        cls.requests += 1
        try:                           # 记录最近一次请求体，供 json_mode 断言
            cls.last_body = json.loads(raw.decode("utf-8"))
        except Exception:
            cls.last_body = None
        if cls.auto_cancel is not None:   # 模拟：请求进行中客户端断开（SSE 写失败）
            cls.auto_cancel.set()
        if cls.fail_status and cls.requests <= cls.fail_count:
            body = json.dumps({"error": "boom"}).encode()
            self.send_response(cls.fail_status)
        else:
            body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


print("== DeepSeekClient 重试与连接复用（本地假服务） ==")
srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
client = llm.DeepSeekClient(api_key="test-key", base_url=base,
                            retries=2, backoff=0)   # backoff=0：测试不等待

def reset_fake(fail_status=0, fail_count=0):
    FakeAPI.fail_status = fail_status
    FakeAPI.fail_count = fail_count
    FakeAPI.requests = 0
    FakeAPI.connections = 0
    FakeAPI.last_body = None
    FakeAPI.auto_cancel = None

# 3.1 正常路径：两次调用复用一条连接（keep-alive 生效）
reset_fake()
check("首次调用成功", client.chat("s", "u") == "ok")
check("二次调用成功", client.chat("s", "u") == "ok")
check("keep-alive：2 次请求仅 1 条连接",
      FakeAPI.requests == 2 and FakeAPI.connections == 1)

# 3.2 503 两次后成功：指数退避重试生效
reset_fake(503, 2)
client2 = llm.DeepSeekClient(api_key="test-key", base_url=base, retries=2, backoff=0)
check("503×2 后重试成功", client2.chat("s", "u") == "ok")
check("总共发出 3 次请求（1+2 重试）", FakeAPI.requests == 3)

# 3.3 400 不可重试：立即抛错且只发 1 次请求
reset_fake(400, 99)
try:
    client2.chat("s", "u")
    check("400 应立即抛错", False)
except RuntimeError as e:
    check("400 立即抛错不重试", "400" in str(e) and FakeAPI.requests == 1)

# 3.4 重试次数耗尽：抛最后一次错误
reset_fake(503, 99)
try:
    client2.chat("s", "u")
    check("重试耗尽应抛错", False)
except RuntimeError as e:
    check("重试耗尽抛错（共 3 次尝试）", FakeAPI.requests == 3 and "503" in str(e))

# 3.5 json_mode：DeepSeek JSON 输出模式（response_format 写进请求体）
reset_fake()
client.chat("把结果输出为 json 对象", "u", json_mode=True)
check("json_mode=True 时请求体含 response_format",
      FakeAPI.last_body.get("response_format") == {"type": "json_object"})
client.chat("把结果输出为 json 对象", "u")   # 默认 json_mode=False
check("默认 json_mode=False 不带 response_format",
      "response_format" not in FakeAPI.last_body)

# 3.6 cancel_event 已置位：不发请求直接抛 OperationCancelled（断开止损）
reset_fake()
ev = threading.Event()
ev.set()
try:
    client.chat("s", "u", cancel_event=ev)
    check("取消应抛 OperationCancelled", False)
except llm.OperationCancelled:
    check("cancel_event 置位后抛 OperationCancelled 且未发请求",
          FakeAPI.requests == 0)

# 3.7 第 1 次尝试失败后、退避重试前取消：不再发起第 2 次请求
reset_fake(503, 99)
ev2 = threading.Event()
FakeAPI.auto_cancel = ev2    # 假服务收到第 1 个请求即置位（模拟客户端断开）
try:
    client2.chat("s", "u", cancel_event=ev2)
    check("退避前取消应抛 OperationCancelled", False)
except llm.OperationCancelled:
    check("退避重试前感知取消（只发了 1 次请求）", FakeAPI.requests == 1)
finally:
    FakeAPI.auto_cancel = None   # 复位，避免影响后续用例


# =====================================================================
# 3.8 取消优先于记账（T1/T2 回归）：已取消时不得消耗预算、不得进入 chat
# =====================================================================
# 背景：llm.call() 与 extract_components() 此前都是“先 budget.spend()
# 再让 client.chat() 内部检查取消”，导致已取消的请求仍白扣 1 次额度。
# 修复后取消检查前置，下面两条把该语义钉死，防止回归。
class _SpyClient:
    """记录 chat 是否被进入（取消应使其不被进入）。"""

    def __init__(self):
        self.entered = 0

    def chat(self, *a, **kw):
        self.entered += 1
        return "[]"


print("== 取消优先于记账（已取消不扣预算 / 不进 chat） ==")
_spy = _SpyClient()
_b2 = llm.CallBudget(max_calls=5)
_ev3 = threading.Event()
_ev3.set()
try:
    llm.extract_components("一个稀有的没听说过的元件需求", _spy, _b2,
                           lambda s, d: None, cancel_event=_ev3)
    check("抽取路径取消应抛 OperationCancelled", False)
except llm.OperationCancelled:
    check("抽取路径：已取消抛 OperationCancelled 且未扣预算（used=0）",
          _b2.used == 0)
    check("抽取路径：已取消不进入 client.chat（entered=0）",
          _spy.entered == 0)

# generate_circuit_json 走 llm.call() 包装：同样应取消优先于记账
_spy2 = _SpyClient()
_b3 = llm.CallBudget(max_calls=5)
try:
    llm.generate_circuit_json("点亮LED", {"resistor": {}, "led": {}, "battery": {}},
                              _spy2, budget=_b3,
                              on_progress=lambda s, d: None,
                              cancel_event=_ev3)
    check("生成路径取消应抛 OperationCancelled", False)
except llm.OperationCancelled:
    check("生成路径：已取消不扣预算（used=0）", _b3.used == 0)
    check("生成路径：已取消不进入 client.chat（entered=0）",
          _spy2.entered == 0)


# =====================================================================
# 4. generate_circuit_json 预算封顶 + 进度回调
# =====================================================================
class FakeClient:
    """离线假客户端：任何调用都返回同一份合法电路 JSON。"""
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def chat(self, system_prompt, user_prompt, **kw):
        self.calls += 1
        return self.text


GOOD = json.dumps({
    "version": 1,
    "parts": [{"type": "battery", "id": "BAT1"},
              {"type": "resistor", "id": "R1"},
              {"type": "led", "id": "LED1"}],
    "connections": [["BAT1:+", "R1:1", "red", []],
                    ["R1:2", "LED1:+", "red", []],
                    ["LED1:-", "BAT1:-", "black", []]],
}, ensure_ascii=False)
LIB = {"battery": {"pins": ["+", "-"]},
       "resistor": {"pins": ["1", "2"]},
       "led": {"pins": ["+", "-"]}}

print("== generate_circuit_json 预算与进度 ==")
# 4.1 预算恰好够（plan+generate=2）
fc = FakeClient(GOOD)
stages = []
out = llm.generate_circuit_json("点亮一个LED", LIB, fc,
                                budget=llm.CallBudget(2),
                                on_progress=lambda s, d: stages.append(s))
check("预算=2 时正常返回电路", out["parts"][0]["id"] == "BAT1")
check("实际 LLM 调用 2 次（规划+生成）", fc.calls == 2)
check("进度回调按序触发", stages == ["plan", "generate", "done"])

# 4.2 预算=1：规划花光预算，生成被拦截 → ValueError（模板兜底信号）
fc = FakeClient(GOOD)
try:
    llm.generate_circuit_json("点亮一个LED", LIB, fc, budget=llm.CallBudget(1))
    check("预算不足应抛 ValueError", False)
except ValueError:
    check("预算=1 时抛 ValueError（模板兜底信号）", True)

# 4.3 预算=2 且电路带 ERC 错误：ERC 修复被预算拦截 → best-effort 返回原电路
BAD_ERC = json.dumps({
    "version": 1,
    "parts": [{"type": "battery", "id": "BAT1"},
              {"type": "led", "id": "LED1"}],
    "connections": [["BAT1:+", "LED1:+", "red", []],
                    ["LED1:-", "BAT1:-", "black", []]],
}, ensure_ascii=False)
from core import catalog
kb = catalog.build_erc_kb()
fc = FakeClient(BAD_ERC)
out = llm.generate_circuit_json("LED直接接电池", LIB, fc,
                                budget=llm.CallBudget(2), erc_kb=kb)
check("ERC 修复被预算拦截时 best-effort 返回", out["parts"][1]["id"] == "LED1")
check("预算封顶后不再追加调用", fc.calls == 2)


# =====================================================================
# 5. 修复模板拆分 / json_mode 透传 / cancel_event 透传回归
# =====================================================================
print("== 修复模板拆分与调用模式 ==")
check("STRUCT_REPAIR_PROMPT 聚焦结构错误（struct_errors 占位）",
      "struct_errors" in llm.STRUCT_REPAIR_PROMPT
      and "component_pins" in llm.STRUCT_REPAIR_PROMPT
      and "没有通过结构校验" in llm.STRUCT_REPAIR_PROMPT)
check("ERC_REPAIR_PROMPT 注入规划上下文（erc_errors + design_advice 占位）",
      "erc_errors" in llm.ERC_REPAIR_PROMPT
      and "design_advice" in llm.ERC_REPAIR_PROMPT
      and "没有通过电气规则检查" in llm.ERC_REPAIR_PROMPT)
check("GEN_PROMPT 含正负例 few-shot（反相放大器 + 编造引脚警示）",
      "opamp_inverting" in llm.GEN_PROMPT and "错误原因" in llm.GEN_PROMPT)


class ScriptClient:
    """脚本化假客户端：按调用次序返回预设文本，实现与 DeepSeekClient 相同的
    取消语义（cancel_event 置位即抛 OperationCancelled），并记录每次调用的
    json_mode / cancel_event / system_prompt，供透传断言。"""
    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = 0
        self.json_modes = []
        self.cancel_events = []
        self.systems = []

    def chat(self, system_prompt, user_prompt, json_mode=False,
             cancel_event=None, **kw):
        if cancel_event is not None and cancel_event.is_set():
            raise llm.OperationCancelled("已取消（假客户端）")
        self.calls += 1
        self.json_modes.append(json_mode)
        self.cancel_events.append(cancel_event)
        self.systems.append(system_prompt)
        return self.texts[min(self.calls - 1, len(self.texts) - 1)]


# 5.1 结构修复路径：生成坏结构（位号重复）→ STRUCT_REPAIR_PROMPT 修复为好结构
BAD_STRUCT = json.dumps({
    "version": 1,
    "parts": [{"type": "battery", "id": "BAT1"},
              {"type": "resistor", "id": "R1"},
              {"type": "led", "id": "LED1"},
              {"type": "led", "id": "LED1"}],      # 位号重复：触发结构修复
    "connections": [["BAT1:+", "R1:1", "red", []],
                    ["R1:2", "LED1:+", "red", []],
                    ["LED1:-", "BAT1:-", "black", []]],
}, ensure_ascii=False)
sc = ScriptClient(BAD_STRUCT, BAD_STRUCT, GOOD)
stages = []
out = llm.generate_circuit_json("点亮一个LED", LIB, sc,
                                budget=llm.CallBudget(5),
                                on_progress=lambda s, d: stages.append(s))
check("结构修复后返回合法电路（位号不再重复）",
      [p["id"] for p in out["parts"]] == ["BAT1", "R1", "LED1"])
check("结构修复阶段使用 STRUCT_REPAIR_PROMPT",
      "没有通过结构校验" in sc.systems[2])
check("进度回调含 repair_struct 阶段", "repair_struct" in stages)
check("规划文本模式、生成/结构修复 JSON 模式",
      sc.json_modes == [False, True, True])

# 5.2 ERC 修复路径：结构合法但 LED 缺限流 → ERC_REPAIR_PROMPT 修复（注入规划上下文）
sc2 = ScriptClient("规划：LED 必须串联限流电阻", BAD_ERC, GOOD)
stages2 = []
out2 = llm.generate_circuit_json("LED直接接电池", LIB, sc2,
                                 budget=llm.CallBudget(5), erc_kb=kb,
                                 on_progress=lambda s, d: stages2.append(s))
check("ERC 修复后返回通过校验的电路",
      out2 == json.loads(GOOD) and "repair_erc" in stages2)
check("ERC 修复阶段使用 ERC_REPAIR_PROMPT",
      "没有通过电气规则检查" in sc2.systems[2])
check("ERC 修复注入规划上下文 design_advice",
      "规划：LED 必须串联限流电阻" in sc2.systems[2])

# 5.3 cancel_event 透传：generate_circuit_json 取消后 0 次调用即中止
ev3 = threading.Event()
ev3.set()
sc3 = ScriptClient(GOOD)
try:
    llm.generate_circuit_json("点亮一个LED", LIB, sc3, cancel_event=ev3)
    check("取消应向上传播", False)
except llm.OperationCancelled:
    check("generate_circuit_json 透传取消信号（0 次 LLM 调用）", sc3.calls == 0)

# 5.4 extract_components 的 LLM 兜底：json_mode / cancel_event 透传
sc4 = ScriptClient('["蜂鸣器"]')
types, source = llm.extract_components("搞个会响的报警玩意", sc4)
check("LLM 兜底抽取正常工作（本地无命中才走 LLM）",
      source == "llm" and types == ["buzzer"])
check("抽取调用启用 json_mode", sc4.json_modes == [True])
ev4 = threading.Event()
ev4.set()
try:
    llm.extract_components("搞个会响的报警玩意", ScriptClient('["蜂鸣器"]'),
                            cancel_event=ev4)
    check("抽取取消应抛 OperationCancelled", False)
except llm.OperationCancelled:
    check("extract_components 透传取消信号", True)

# 5.5 数组响应重试（“返回数组”是 LLM 高发故障，最多 3 次生成尝试）：
#     默认 max_retry=2 → 最多 3 次生成尝试；仍非法则 ValueError 走模板兜底
print("== 数组响应重试语义 ==")
ARRAY_OUT = '["电阻", "LED"]'   # 典型坏输出：把需求抽成了元件名数组而非电路对象
sc5 = ScriptClient("规划：略", ARRAY_OUT, ARRAY_OUT, GOOD)
stages5 = []
out5 = llm.generate_circuit_json("点亮一个LED", LIB, sc5,
                                 budget=llm.CallBudget(6),
                                 on_progress=lambda s, d: stages5.append(s))
check("数组×2 后第 3 次生成成功", out5["parts"][0]["id"] == "BAT1")
check("共 4 次调用（规划 + 3 次生成）", sc5.calls == 4)
check("generate 阶段共出现 3 次、plan 在最前",
      stages5.count("generate") == 3 and stages5[0] == "plan")

sc6 = ScriptClient("规划：略", ARRAY_OUT, ARRAY_OUT, ARRAY_OUT)
try:
    llm.generate_circuit_json("点亮一个LED", LIB, sc6, budget=llm.CallBudget(6))
    check("数组×3 应抛 ValueError", False)
except ValueError as e:
    check("数组×3 耗尽重试抛 ValueError（模板兜底信号）", "非法结构" in str(e))
check("失败路径共 4 次调用（规划 + 3 次生成）", sc6.calls == 4)

# 显式 max_retry=0 的调用方保持“一次尝试”语义（参数覆盖默认值）
sc7 = ScriptClient("规划：略", ARRAY_OUT)
try:
    llm.generate_circuit_json("点亮一个LED", LIB, sc7, max_retry=0,
                              budget=llm.CallBudget(6))
    check("max_retry=0 应立即抛 ValueError", False)
except ValueError:
    check("显式 max_retry=0 保持一次尝试语义", sc7.calls == 2)

srv.shutdown()

# =====================================================================
# 7. 模板兜底路由（server.FALLBACK_TEMPLATES）
#    覆盖 parallel_led 分流（2026-09-08）与既有路由不回归
# =====================================================================
print("== 模板兜底路由 ==")
from core import catalog
from server import FALLBACK_TEMPLATES

def _route(q):
    return catalog.match_template(q, FALLBACK_TEMPLATES)

# 新分流：并联类需求应命中 parallel_led，而不是被 led_limit 抢走
check("两个LED并联 -> parallel_led", _route("两个LED并联") == "parallel_led")
check("LED并联电路 -> parallel_led", _route("LED并联电路") == "parallel_led")
check("多路LED指示灯 -> parallel_led", _route("多路LED指示灯") == "parallel_led")
# 负向词边界：串联类不进并联模板；与既有模板同分时既有模板优先
check("两个LED串联不进并联模板", _route("两个LED串联") is None)
check("并联电阻分压仍归 pot_divider", _route("并联电阻分压") == "pot_divider")
check("双LED闪烁仍归 ne555_blink", _route("双LED闪烁") == "ne555_blink")
# 既有路由不回归：led_limit 的负向词不误伤普通 LED 需求
check("LED限流电阻 -> led_limit", _route("LED限流电阻") == "led_limit")
check("发光二极管电路 -> led_limit", _route("发光二极管电路") == "led_limit")
check("555闪烁灯 -> ne555_blink", _route("做一个555闪烁灯电路") == "ne555_blink")
check("反相放大器 -> opamp_inv", _route("反相放大器") == "opamp_inv")
check("三极管开关 -> npn_switch", _route("三极管开关电路") == "npn_switch")
check("稳压二极管 -> zener_reg", _route("稳压二极管电路") == "zener_reg")
# parallel_led 路由到的模板确实存在且通过 ERC（test_pipeline 已验证其 ERC 通过）
check("parallel_led 是内置模板合法键", "parallel_led" in
      json.load(open(_os.path.join(_PROJECT_ROOT, "data", "circuit_templates.json"),
                     encoding="utf-8")))

print(f"\ntest_runtime.py 全部 {PASS} 项断言通过 ✔")
