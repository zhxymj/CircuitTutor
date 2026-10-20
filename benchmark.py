# -*- coding: utf-8 -*-
"""
benchmark.py —— LLM 生成成功率基准工具（答辩核心数据来源）
============================================================
对 40 条多样化中文真实需求逐条跑完整流水线：
    元件抽取（本地短路 → LLM 兜底）
      → 电气规划（PLAN_PROMPT，文本模式）
      → 结构化生成（GEN_PROMPT，JSON mode + few-shot 正负例）
      → 结构校验 / STRUCT_REPAIR_PROMPT 修复
      → ERC 校验 / ERC_REPAIR_PROMPT 修复（注入规划上下文）
全程受 CallBudget 预算封顶（5 次/条），逐条记录：

  * 命中元件（本地命中 or LLM 抽取）
  * 结构是否通过（validate_circuit_json）
  * ERC 是否通过（fatal/major/minor/warning 四级计数）
  * LLM 调用次数（budget.used）、耗时（ms）、经历的阶段序列
  * 最终 verdict：成功 = 结构通过 且 无 fatal / major

产出：
  * 控制台汇总表：总成功率 / 结构通过率 / ERC 通过率 / 平均耗时 /
    平均调用次数 / 失败清单
  * 报告 JSON（默认 benchmark_report.json）：含每条需求的完整明细
    （命中元件、阶段序列、ERC 问题原文、最终 CircuitJSON），答辩时
    可直接拿去做错误分析与成功率图表。

零依赖（Python 标准库 only），代码风格与 core/llm.py 一致。

用法：
  python benchmark.py                      # 跑全部 40 条（30 结构化 + 10 自由表达）
  python benchmark.py --limit 5            # 只跑前 5 条（快速冒烟）
  python benchmark.py --out my_report.json # 指定报告输出路径
  python benchmark.py --snapshot           # 离线快照：注入确定性假模型，无需 API Key
  python benchmark.py --snapshot --out golden.json   # 重新生成黄金快照

配置（依赖真实 API Key，未配置时打印设置方法后优雅退出）：
  Windows CMD        : set DEEPSEEK_API_KEY=sk-xxxxxxxx
  Windows PowerShell : $env:DEEPSEEK_API_KEY = 'sk-xxxxxxxx'
  Linux / macOS      : export DEEPSEEK_API_KEY=sk-xxxxxxxx

离线快照（--snapshot）：
  真实基准依赖外部模型，同一份代码换个模型/日期结论就会漂移，评审时无法复现。
  --snapshot 用 DeterministicClient 顶替 DeepSeekClient：对给定需求返回固定的
  合法电路（按需求关键词路由到模板），全程不联网、零 API Key、结果逐字节可复现。
  快照报告在 meta 里记下 model="deterministic-fake" 与 suite_hash（40 条需求的
  内容指纹），因此任何一次套件改动都会改变哈希、让对比失效——这正是我们要的
  「测试用例变了要显式知道」。tests/test_benchmark_snapshot.py 消费该机制。
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time

from core import catalog
from core import config
from core import llm
from core.erc import run_erc

# =====================================================================
# 1. 40 条多样中文需求：30 条结构化 + 10 条自由表达压力测试
#    （LED 点灯/限流、555 闪烁/定时、反相/同相放大器、电压跟随器、
#     比较器、NPN 开关驱动 LED/蜂鸣器、7805 稳压、分压、RC 充放电、
#     按键去抖、续流二极管保护感性负载、稳压二极管稳压，
#     以及保险丝/电位器/晶振/LC 滤波等补充场景；
#     另含 10 条刻意不含元件关键词的口语化需求，压力测试 LLM 抽取链路）
# =====================================================================
BENCHMARK_CASES = [
    # ---- LED 点灯 / 限流 ----
    "用5V电池经330Ω限流电阻点亮一个红色LED",
    "3V纽扣电池点亮一个绿色发光二极管，串联220Ω限流电阻",
    "拨动开关控制5V电池给LED供电，LED串联470Ω限流电阻",
    "5V电池经100Ω限流电阻驱动红外LED发射电路，负极接地",
    # ---- 555 闪烁 / 定时 ----
    "用NE555做一个1Hz的LED闪烁灯，电源用5V电池，定时电阻10k、"
    "定时电容100µF，LED串330Ω限流电阻，再加100nF去耦电容",
    "用555定时器做单稳态定时电路，电源用电池，按下按钮后LED亮1秒再熄灭，"
    "需要电阻、电容、按钮和LED",
    "NE555无稳态电路驱动蜂鸣器间歇鸣叫，电源用电池，"
    "包含定时电阻、定时电容和蜂鸣器",
    "NE555输出经330Ω限流电阻驱动LED，电源用电池，定时电容10µF，"
    "用电位器调节占空比",
    # ---- 反相 / 同相放大器 ----
    "设计一个反相比例放大器，输入电阻10k、反馈电阻100k，"
    "输入信号用电池提供，同相端接地",
    "画一个增益11倍的同相比例放大器，信号由电池提供，输出经10k负载电阻接地",
    "反相放大器输出经330Ω限流电阻接LED到地做指示，"
    "输入电阻10k、反馈电阻100k，信号源用电池",
    # ---- 电压跟随器 ----
    "做一个电压跟随器缓冲电路，输入来自两个10k电阻对电池分压的中点",
    "电位器接电池两端做可调电压，滑片经电压跟随器缓冲后输出",
    # ---- 比较器 ----
    "做一个过零电压比较器，输入信号用电池，比较器同相端接地，"
    "输出经1kΩ限流电阻接LED到地",
    "用电压比较器监测电池电压是否超过稳压二极管基准，"
    "输出经限流电阻驱动LED报警",
    # ---- NPN 开关驱动 LED / 蜂鸣器 ----
    "用NPN三极管驱动LED：基极经10k电阻接电池正极，"
    "集电极经330Ω电阻和LED接电池正极，发射极接地",
    "NPN三极管驱动有源蜂鸣器，基极经1kΩ电阻接电池正极，"
    "蜂鸣器接在电池正极和集电极之间，发射极接电池负极",
    "上电延时电路：电池经10k电阻给100µF电容充电，"
    "经NPN三极管缓冲后点亮LED，LED带330Ω限流电阻",
    # ---- 续流二极管保护感性负载（继电器/电机模型） ----
    "电池经NPN三极管驱动电感线圈，线圈两端反向并联1N4148续流二极管，"
    "基极经10k电阻驱动",
    "N沟道MOS管驱动电感负载，电感两端并联1N4148续流二极管，"
    "栅极经10k电阻接电池",
    # ---- 7805 稳压 ----
    "12V电池经7805稳压到5V，输入端加100µF电解电容、输出端加100nF瓷片电容，"
    "输入输出共地",
    "用LM7805和电容搭一个5V线性稳压电源，输入用9V电池，输出接100nF滤波电容",
    # ---- 分压 / 按键去抖 ----
    "用两个10k电阻对5V电池分压得到2.5V基准，分压下端接电池负极",
    "电位器两端接电池正负极做可调分压，滑片输出0到5V连续可调电压",
    "10k上拉电阻把按键一端拉到电池正极，按键另一端接电池负极，"
    "按键两端并联0.1µF电容去抖",
    # ---- RC 充放电 ----
    "RC充放电实验电路：5V电池经10k电阻给100µF电容充电，电容负极接电池负极",
    # ---- 稳压二极管稳压 ----
    "用12V电池经470Ω电阻接5.1V稳压二极管到地，组成简单并联稳压电路",
    # ---- 其他补充场景（保险丝 / LC 滤波 / 晶振） ----
    "用保险丝保护5V电池供电回路，输出经220Ω限流电阻接LED指示通电",
    "电感和电容组成LC低通滤波，接在电池输出与地之间",
    "晶振两端各接一个22pF电容到地组成时钟振荡网络，电源用电池",
    # =================================================================
    # 自由表达压力测试（10 条口语化需求，刻意不含显式元件关键词）
    #   与前 30 条"结构化"用例互补：前 30 条题面已点名元件（电阻/LED/555
    #   等），本地匹配器秒命中、零 LLM 抽取（local_extract_rate=1.0），
    #   测的是"已知元件怎么连"。这 10 条用大白话描述功能意图（"一闪一闪"
    #   "放大""稳定成5伏"），刻意回避元件名词，真实压力测试"自然语言理解
    #   →元件识别→生成"全链路；需配 Key 实测，预期成功率低于结构化用例，
    #   暴露的失败正是改进方向——诚实的数据比注水的 96% 更值钱。
    # =================================================================
    "做个让指示灯一闪一闪的电路",
    "把传感器出来的微弱信号放大到能点亮一个指示灯",
    "按下按钮灯就亮，松开就灭的简单控制",
    "做一个把较高直流电压稳定成5伏给单片机供电的电源",
    "做一个通电后会延时几秒才让负载动作的电路",
    "用一节电池同时点亮两个指示灯",
    "当输入电压超过某个设定值时点亮报警灯",
    "给数字芯片的供电脚旁边加一个消除高频干扰的小元件",
    "做一个旋钮转一转就能连续改变输出电压高低的电路",
    "做一个电池快没电时指示灯会亮起来的提醒电路",
    # ---- 2026-09-11 扩充：更多口语化压力用例（无元件关键词） ----
    "天黑了灯会自动亮起来的那种小夜灯电路",
    "用手摸一下风扇就开始转的感应开关",
    "做一个能数数的东西，每按一下前进一位",
    "把家里插座的高压交流变成安全的低压直流",
    "做一个小喇叭，按一下就发出滴滴声",
    "让小车轮子能正转也能反转的驱动电路",
    "做一个测体温的小装置，温度高了就报警",
    "烟雾大的时候自动报警的电路",
    "让两个设备之间隔离干扰的信号传递电路",
    "做一个能记忆刚才按钮按没按过的电路",
    "光照强弱变化时输出电压跟着变的电路",
    "做一个可调亮度的小台灯驱动电路",
    "把不稳定的输入电压变成很稳的输出电压",
    "用旋钮调速的小风扇控制电路",
    "做一个一键开关机的延时断电电路",
]


# =====================================================================
# 1b. 离线快照：确定性假模型 + 套件指纹
# =====================================================================
#
# 真实基准的结论依赖外部模型（同一份代码，换个模型版本/换个日期就可能不同），
# 评审与回归都无法复现。离线快照用确定性假模型把「流水线代码」本身的行为
# 固定下来：跑出的成功/失败分布一旦变化，就说明是我们的代码改了，而不是
# 模型漂移。它是「代码回归护栏」，不是「模型能力指标」——两者互补：
#   * benchmark.py            → 模型能力（需 Key，人工/答辩时跑）
#   * benchmark.py --snapshot → 代码回归（零 Key，可进 CI 与 run_tests）
#
# DeterministicClient 的实现原则：
#   1) 不联网、不读 Key、不用随机数——同输入必得同输出（逐字节可复现）；
#   2) 复用 core.templates 的既有模板作为「假模型输出」，保证电路是合法的
#      CircuitJSON（否则结构校验必然全红，快照就失去意义）；
#   3) 按调用阶段（system_prompt 特征串）返回对应内容，模拟真实交互序列，
#      使 stages / llm_calls 等指标同样被固定。

_SNAPSHOT_MODEL = "deterministic-fake"

# 快照「剧本」：按用例序号（1 基）指定假模型的行为，让快照既有成功也有失败，
# 从而真正钉住流水线的分支（否则 40 条全 success，等于只测了 happy path）。
# 取值：
#   ("ok",)            —— 正常返回该需求的模板电路（→ 成功）
#   ("no_extract",)    —— 抽取阶段返回空数组（→ "元件抽取未命中" 失败）
#   ("always_bad",)    —— 生成阶段返回缺冒号的非法电路（→ 结构校验失败）
#   ("no_fix_erc",)    —— 生成一份有 fatal 的电路且修复也修不好（→ ERC 失败）
# 序号与用例内容绑定在 _SNAPSHOT_SCRIPT，改动后 suite_hash 不变但脚本变，
# 因此 test_benchmark_snapshot 会同时比对「脚本覆盖率」与「结果」。
_SNAPSHOT_SCRIPT = {
    # 只有本地匹配器完全没命中的用例才会走 LLM 抽取（其余在 extract 阶段
    # 就被本地短路拦下，脚本给 no_extract 也到不了）。40 条里仅 #35/#39 符合。
    35: ("no_extract",),    # 通电延时后负载动作 —— 唯一的 LLM 抽取路径之一
    39: ("no_extract",),    # 旋钮连续改变输出电压 —— 唯一的 LLM 抽取路径之二
    7:  ("always_bad",),    # NE555 无稳态驱动蜂鸣器 —— 结构非法分支
    12: ("always_bad",),    # 电压跟随器缓冲电路 —— 结构非法（耗尽重试）
    17: ("no_fix_erc",),    # NPN 驱动有源蜂鸣器 —— ERC 修复失败分支
}


def suite_hash(cases=None) -> str:
    """40 条需求的内容指纹（用于快照对比时判断用例集是否变过）。"""
    cases = BENCHMARK_CASES if cases is None else cases
    blob = "\n".join(cases).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def script_hash() -> str:
    """快照剧本指纹：剧本改了也应被察觉（与 suite_hash 分离，各管一件事）。"""
    blob = json.dumps({str(k): list(v) for k, v in sorted(_SNAPSHOT_SCRIPT.items())},
                      sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _bare_led_circuit() -> dict:
    """一份结构合法但 ERC 有 fatal 的电路（LED 直接跨电源，无限流）。"""
    return {"version": 1,
            "parts": [{"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
                      {"type": "led", "id": "LED1"},
                      {"type": "gnd", "id": "GND1"}],
            "connections": [["BAT1:+", "LED1:+", "red", []],
                            ["LED1:-", "GND1:1", "black", []],
                            ["BAT1:-", "GND1:1", "black", []]]}


def _bad_struct_circuit() -> dict:
    """一份结构非法的电路（端点缺冒号 + 引脚白名单外），必触发结构修复。"""
    return {"version": 1,
            "parts": [{"type": "battery", "id": "BAT1"},
                      {"type": "led", "id": "LED1"}],
            "connections": [["BAT1+", "LED1:+", "red", []]]}


class DeterministicClient:
    """离线确定性假客户端：按阶段与脚本返回固定内容，零网络/Key/随机。

    * 无脚本的用例：返回 core.templates 里按关键词命中的合法模板（→ 成功）；
    * 有脚本的用例：按 _SNAPSHOT_SCRIPT 制造确定的失败路径（→ 覆盖分支）。
    因此快照同时钉住成功与失败两类结果，流水线任一分支行为漂移都会被比对出来。
    """

    def __init__(self, script=None):
        self.model = _SNAPSHOT_MODEL
        self.calls = []                    # [(stage, json_mode)] 便于断言调用序列
        self._templates = self._load_templates()
        self._script = dict(_SNAPSHOT_SCRIPT) if script is None else dict(script)
        self._current_index = 0            # run_case 在每条开始前设置（见 run_case）

    def set_case(self, index: int) -> None:
        """由 run_case 在每条用例开始时调用，让 chat 知道当前处理哪一条。

        用「外部显式告知」而不是「解析 user_prompt」——后者会要求我们往真实
        prompt 里塞序号，改动生产代码的输入（本轮的约束是不改行为）。"""
        self._current_index = int(index)

    @staticmethod
    def _load_templates() -> dict:
        from core import templates
        return dict(templates.FALLBACK_TEMPLATES)

    def _pick_template_key(self, user_prompt: str) -> str:
        """按需求关键词挑一个模板键（与 templates 别名表同源，保证可命中）。"""
        key = catalog.match_template(user_prompt, self._templates)
        return key or "led_limit"

    def _circuit_for(self, user_prompt: str) -> dict:
        """按需求关键词返回一份该模板的电路（用 server.BUILTIN_TEMPLATES）。"""
        import server
        key = self._pick_template_key(user_prompt)
        tpl = server.BUILTIN_TEMPLATES.get(key) or server.BUILTIN_TEMPLATES["led_limit"]
        return json.loads(json.dumps(tpl))   # 深拷贝：绝不让流水线改到共享模板

    def chat(self, system_prompt, user_prompt, temperature=0.0, max_tokens=None,
             timeout=None, retries=None, json_mode=False, cancel_event=None):
        """模仿 DeepSeekClient.chat 的签名与阶段语义。"""
        if cancel_event is not None and cancel_event.is_set():
            raise llm.OperationCancelled("快照：cancel_event 已置位")
        idx = self._current_index
        act = (self._script.get(idx) or ("ok",))[0]

        if "抽取需要用到的元件类型名称" in system_prompt:          # EXTRACT
            self.calls.append(("extract", json_mode))
            if act == "no_extract":
                return "[]"                     # 空数组 → 抽取未命中
            key = self._pick_template_key(user_prompt)
            return json.dumps(self._template_types(key), ensure_ascii=False)
        if "电子电路规划助手" in system_prompt:                     # PLAN
            self.calls.append(("plan", json_mode))
            return "快照规划：电源 → 限流/定时网络 → 负载 → 回地"
        if "转换成一个合法的 CircuitJSON" in system_prompt:         # GEN
            self.calls.append(("generate", json_mode))
            if act == "always_bad":
                return json.dumps(_bad_struct_circuit(), ensure_ascii=False)
            if act == "no_fix_erc":
                return json.dumps(_bare_led_circuit(), ensure_ascii=False)
            return json.dumps(self._circuit_for(user_prompt), ensure_ascii=False)
        if "格式 / 结构】上有误" in system_prompt:                   # STRUCT_REPAIR
            self.calls.append(("repair_struct", json_mode))
            if act == "always_bad":
                return json.dumps(_bad_struct_circuit(), ensure_ascii=False)  # 修不好
            return json.dumps(self._circuit_for(user_prompt), ensure_ascii=False)
        if "电气规则检查" in system_prompt:                          # ERC_REPAIR
            self.calls.append(("repair_erc", json_mode))
            if act == "no_fix_erc":
                return json.dumps(_bare_led_circuit(), ensure_ascii=False)    # 修不好
            return json.dumps(self._circuit_for(user_prompt), ensure_ascii=False)
        self.calls.append(("other", json_mode))
        return json.dumps(self._circuit_for(user_prompt), ensure_ascii=False)

    def _template_types(self, key: str) -> list:
        """从模板电路里抽取元件类型（供 EXTRACT 阶段返回，模拟真实识别）。"""
        import server
        tpl = server.BUILTIN_TEMPLATES.get(key) or server.BUILTIN_TEMPLATES["led_limit"]
        seen, out = set(), []
        for p in tpl.get("parts", []):
            t = p.get("type")
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def close(self):
        """与 DeepSeekClient 接口对齐（快照无需释放连接）。"""



# 快照对比时忽略的字段：时间戳与耗时天然不可复现，不参与逐字段比对
_TIMING_FIELDS = frozenset({"time", "elapsed_ms", "avg_elapsed_ms",
                            "total_elapsed_ms"})


def strip_timing(obj):
    """递归剔除时间/耗时字段，只留下可复现的行为结果（快照对比用）。"""
    if isinstance(obj, dict):
        return {k: strip_timing(v) for k, v in obj.items()
                if k not in _TIMING_FIELDS}
    if isinstance(obj, list):
        return [strip_timing(x) for x in obj]
    return obj


def diff_snapshots(golden: dict, actual: dict) -> list:
    """比对两份快照（已剔时间字段），返回差异描述列表；一致则空列表。

    逐条按 index 对齐，便于把「第 N 条从 success 变 fail」直接说清楚。"""
    diffs = []
    gsum, asum = golden.get("summary", {}), actual.get("summary", {})
    for k in sorted(set(gsum) | set(asum)):
        if gsum.get(k) != asum.get(k):
            diffs.append(f"summary.{k}: {gsum.get(k)!r} → {asum.get(k)!r}")
    gc = {c.get("index"): c for c in golden.get("cases", [])}
    ac = {c.get("index"): c for c in actual.get("cases", [])}
    for idx in sorted(set(gc) | set(ac)):
        g, a = gc.get(idx), ac.get(idx)
        if g is None:
            diffs.append(f"case[{idx}]: 新增（快照中不存在）")
            continue
        if a is None:
            diffs.append(f"case[{idx}]: 缺失（实际运行未产生）")
            continue
        for f in ("verdict", "structure_ok", "erc_passed", "fatal", "major",
                  "minor", "warning", "llm_calls", "components",
                  "component_source", "stages", "error", "prompt"):
            if g.get(f) != a.get(f):
                diffs.append(f"case[{idx}].{f}: {g.get(f)!r} → {a.get(f)!r}")
    return diffs


# =====================================================================
# 2. 单条需求：跑完整流水线并记录全部指标
# =====================================================================

def run_case(idx: int, prompt: str, client, erc_kb: dict) -> dict:
    """跑一条需求的完整流水线（抽取 → 规划 → 生成 → 结构修复 → ERC 修复），
    返回该条的全部记录。绝不让单条失败中断整轮基准。"""
    t0 = time.monotonic()
    budget = llm.CallBudget(max_calls=config.LLM_BUDGET)  # 与 server.py 线上流水线保持一致
    stages: list = []
    # 快照模式专用：把当前用例序号告知确定性假客户端（真实客户端无此方法，
    # hasattr 探测保证对线上零影响）。
    if hasattr(client, "set_case"):
        client.set_case(idx)
    rec = {
        "index": idx,
        "prompt": prompt,
        "components": [],            # 命中的元件类型
        "component_source": "none",  # local=本地短路 / llm=LLM 抽取 / none
        "has_circuit": False,        # 是否生成出了 CircuitJSON
        "structure_ok": False,       # 结构校验（含引脚白名单）是否通过
        "erc_passed": False,         # ERC 是否无 fatal/major
        "fatal": 0, "major": 0, "minor": 0, "warning": 0,
        "erc_issues": [],            # ERC 问题原文（severity + message）
        "llm_calls": 0,              # 本条消耗的 LLM 调用次数
        "elapsed_ms": 0,
        "stages": [],                # 经历的阶段序列（plan/generate/repair_*…）
        "error": "",                 # 失败原因（成功为空）
        "verdict": "fail",           # success = 结构通过 且 无 fatal/major
        "circuit": None,             # 最终产物（便于人工复核）
    }
    try:
        # ① 元件抽取：本地命中即免第一次 LLM 往返；未命中走 LLM 兜底
        types, source = llm.extract_components(prompt, client, budget)
        rec["components"] = types
        rec["component_source"] = source
        if not types:
            rec["error"] = "元件抽取未命中任何元件（本地与 LLM 均未识别）"
            return rec

        # ② 规划 → 生成 → 结构校验修复 → ERC 校验修复（预算封顶 5 次）
        matched_lib = catalog.build_matched_lib(types)
        circuit = llm.generate_circuit_json(
            prompt, matched_lib, client, max_retry=config.LLM_STRUCT_RETRIES,
            budget=budget, erc_kb=erc_kb,
            on_progress=lambda s, d: stages.append(s))
        rec["has_circuit"] = True
        rec["circuit"] = circuit

        # ③ 以“最终产物”复核结构 + ERC（generate 可能 best-effort 带错返回）
        struct_errs = llm.validate_circuit_json(circuit, matched_lib)
        rec["structure_ok"] = not struct_errs
        rep = run_erc(circuit, erc_kb)
        rec["erc_passed"] = rep.passed
        rec["fatal"], rec["major"] = len(rep.fatals), len(rep.majors)
        rec["minor"], rec["warning"] = len(rep.minors), len(rep.warnings)
        rec["erc_issues"] = [f"[{i.severity}] {i.rule}：{i.message}"
                             for i in rep.issues]
        if struct_errs:
            rec["error"] = "结构校验未通过：" + "；".join(struct_errs[:3])
        elif rep.fatals or rep.majors:
            rec["error"] = f"ERC 未通过：fatal={len(rep.fatals)}，major={len(rep.majors)}"
    except Exception as e:            # 预算耗尽 / 网络错误 / 非法结构等
        rec["error"] = f"{type(e).__name__}: {e}"
    finally:
        rec["llm_calls"] = budget.used
        rec["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
        rec["stages"] = stages
        # 判定标准：结构通过 且 无 fatal / major（minor/warning 不判失败）
        rec["verdict"] = ("success" if rec["structure_ok"]
                          and rec["fatal"] == 0 and rec["major"] == 0 else "fail")
    return rec


# =====================================================================
# 3. 汇总与报告
# =====================================================================

def summarize(records: list, total_cases: int) -> dict:
    """控制台汇总表用（同时也是报告 JSON 的 summary 字段）。"""
    n = len(records)
    if n == 0:
        # 一条都没跑完（如启动后立刻 Ctrl+C）也要返回完整键结构，
        # 否则 print_summary / write_report 取 summary['success'] 会 KeyError
        return {"total": 0, "cases_in_suite": total_cases, "success": 0,
                "success_rate": 0.0, "structure_pass": 0, "structure_pass_rate": 0.0,
                "circuit_produced": 0, "erc_pass": 0, "erc_pass_rate": 0.0,
                "local_extract_rate": 0.0, "avg_llm_calls": 0, "max_llm_calls": 0,
                "avg_elapsed_ms": 0, "total_elapsed_ms": 0,
                "fatal_total": 0, "major_total": 0, "minor_total": 0,
                "warning_total": 0}
    ok = sum(1 for r in records if r["verdict"] == "success")
    struct = sum(1 for r in records if r["structure_ok"])
    produced = sum(1 for r in records if r["has_circuit"])
    erc_ok = sum(1 for r in records if r["erc_passed"])
    local = sum(1 for r in records if r["component_source"] == "local")
    calls = [r["llm_calls"] for r in records]
    elapsed = [r["elapsed_ms"] for r in records]
    return {
        "total": n,
        "cases_in_suite": total_cases,
        "success": ok,
        "success_rate": round(ok / n, 4),
        "structure_pass": struct,
        "structure_pass_rate": round(struct / n, 4),
        "circuit_produced": produced,                     # 生成出电路的数量
        "erc_pass": erc_ok,
        # ERC 通过率按“生成出电路”的条数算才有意义（没生成出电路无从谈 ERC）
        "erc_pass_rate": round(erc_ok / produced, 4) if produced else 0.0,
        "local_extract_rate": round(local / n, 4),        # 本地短路占比（免 LLM 抽取）
        "avg_llm_calls": round(sum(calls) / n, 2),
        "max_llm_calls": max(calls),
        "avg_elapsed_ms": round(sum(elapsed) / n),
        "total_elapsed_ms": sum(elapsed),
        "fatal_total": sum(r["fatal"] for r in records),
        "major_total": sum(r["major"] for r in records),
        "minor_total": sum(r["minor"] for r in records),
        "warning_total": sum(r["warning"] for r in records),
    }


def print_summary(summary: dict, records: list) -> None:
    """控制台汇总表 + 失败清单（答辩时直接截图用）。"""
    n = summary.get("total", 0)
    print("\n" + "=" * 66)
    print("基准汇总（共 %d 条需求）" % n)
    print("=" * 66)
    print(f"  总成功率      ：{summary['success']}/{n}"
          f"（{summary['success_rate']:.1%}）")
    print(f"  结构通过率    ：{summary['structure_pass']}/{n}"
          f"（{summary['structure_pass_rate']:.1%}）")
    print(f"  ERC 通过率    ：{summary['erc_pass']}/{summary['circuit_produced']} 条已生成电路"
          f"（{summary['erc_pass_rate']:.1%}）")
    print(f"  平均耗时      ：{summary['avg_elapsed_ms'] / 1000:.1f} s"
          f" / 条（总计 {summary['total_elapsed_ms'] / 1000:.1f} s）")
    print(f"  平均调用次数  ：{summary['avg_llm_calls']} 次 / 条"
          f"（最多 {summary['max_llm_calls']} 次，预算封顶 {config.LLM_BUDGET}）")
    print(f"  本地抽取短路  ：{summary['local_extract_rate']:.1%}"
          f"（免第一次 LLM 往返）")
    print(f"  ERC 问题计数  ：fatal {summary['fatal_total']} / major {summary['major_total']}"
          f" / minor {summary['minor_total']} / warning {summary['warning_total']}")
    fails = [r for r in records if r["verdict"] == "fail"]
    if fails:
        print(f"\n  失败清单（{len(fails)} 条）：")
        for r in fails:
            print(f"   [{r['index']:>2}] {r['prompt'][:34]}…")
            print(f"        原因：{r['error'][:80]}")
    else:
        print("\n  失败清单：无（全部通过）")
    print("=" * 66)


def write_report(path: str, summary: dict, records: list,
                 limit: int, model: str) -> None:
    """把完整明细写成 JSON（失败时另存，绝不让报告环节崩掉基准数据）。"""
    snapshot = model == _SNAPSHOT_MODEL
    report = {
        "meta": {
            "tool": "benchmark.py（智绘电路 CircuitTutor LLM 生成成功率基准）",
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
            "model": model,
            "suite_size": len(BENCHMARK_CASES),
            "suite_hash": suite_hash(),          # 40 条需求内容指纹
            "script_hash": script_hash(),        # 快照剧本指纹（仅快照模式有意义）
            "snapshot": snapshot,                # 是否为离线快照（零 Key / 可复现）
            "ran": len(records),
            "limit": limit,
            "budget_per_case": config.LLM_BUDGET,
            "verdict_rule": "成功 = 结构校验通过 且 ERC 无 fatal/major",
        },
        "summary": summary,
        "cases": records,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        if snapshot:
            print(f"\n离线快照已写入：{path}"
                  f"（model={model}，suite_hash={report['meta']['suite_hash']}，"
                  f"script_hash={report['meta']['script_hash']}，逐字节可复现）")
        else:
            print(f"\n完整报告已写入：{path}（含每条明细与最终 CircuitJSON）")
    except OSError as e:
        alt = f"benchmark_report_{int(time.time())}.json"
        with open(alt, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n写入 {path} 失败（{e}），已改存 {alt}")


# =====================================================================
# 4. 入口：环境检查 → 逐条跑 → 汇总 → 写报告
# =====================================================================

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(
        description="智绘电路 CircuitTutor —— LLM 生成成功率基准工具（40 条中文需求：30 结构化 + 10 自由表达）")
    ap.add_argument("--limit", type=int, default=0,
                    help="只跑前 N 条需求（默认全部 30 条）")
    ap.add_argument("--out", default="benchmark_report.json",
                    help="报告 JSON 输出路径（默认 benchmark_report.json）")
    ap.add_argument("--snapshot", action="store_true",
                    help="离线快照模式：注入确定性假模型，零 API Key、"
                         "结果逐字节可复现，用于 CI 代码回归")
    args = ap.parse_args()

    cases = BENCHMARK_CASES[:args.limit] if args.limit > 0 else BENCHMARK_CASES

    # 离线快照：不检查 Key，直接用确定性假客户端
    if args.snapshot:
        client = DeterministicClient()
        erc_kb = catalog.build_erc_kb()
        print(f"智绘电路 · 离线快照（model={client.model}，共 {len(cases)} 条，"
              f"零 API Key / 零网络 / 可复现，suite_hash={suite_hash()}，"
              f"script_hash={script_hash()}）")
        print("-" * 66)
        records = []
        for i, prompt in enumerate(cases, 1):
            rec = run_case(i, prompt, client, erc_kb)
            records.append(rec)
            mark = "✔ 成功" if rec["verdict"] == "success" else "✘ 失败"
            print(f"[{i:>2}/{len(cases)}] {mark}  {rec['llm_calls']} 次调用  "
                  f"{prompt[:36]}")
        summary = summarize(records, len(BENCHMARK_CASES))
        print_summary(summary, records)
        write_report(args.out, summary, records, args.limit, client.model)
        return 0

    # 环境检查：真实基准必须真实调用 LLM，缺 API Key 时给出友好指引后退出
    if not config.api_key_configured():
        print("=" * 62)
        print("未检测到环境变量 DEEPSEEK_API_KEY，无法运行 LLM 生成基准。")
        print("=" * 62)
        print()
        print("本工具需真实调用 DeepSeek 接口（40 条需求 × 规划/生成/修复），")
        print("请先设置 API Key（任选其一）后重新运行：")
        print()
        print("  Windows CMD        : set DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx")
        print("  Windows PowerShell : $env:DEEPSEEK_API_KEY = 'sk-xxxxxxxxxxxxxxxx'")
        print("  Linux / macOS      : export DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx")
        print()
        print("配置完成后：")
        print("  python benchmark.py                       # 跑全部 40 条")
        print("  python benchmark.py --limit 5             # 只跑前 5 条冒烟")
        print("  python benchmark.py --out my_report.json  # 指定报告路径")
        print()
        print("提示：无需 Key 的离线代码回归可直接跑快照模式——")
        print("  python benchmark.py --snapshot")
        print("（无需联网即可跑的离线回归：python test_llm_runtime.py）")
        return 0   # 优雅退出：环境未配置，不算基准失败

    client = llm.DeepSeekClient()           # 复用 keep-alive 连接，跑得更快
    erc_kb = catalog.build_erc_kb()

    print(f"智绘电路 · LLM 生成成功率基准（模型 {client.model}，共 {len(cases)} 条，"
          f"预算 {config.LLM_BUDGET} 次/条）")
    print("-" * 66)

    records = []
    try:
        for i, prompt in enumerate(cases, 1):
            print(f"[{i:>2}/{len(cases)}] {prompt[:40]}")
            rec = run_case(i, prompt, client, erc_kb)
            records.append(rec)
            mark = "✔ 成功" if rec["verdict"] == "success" else "✘ 失败"
            print(f"         {mark}  {rec['elapsed_ms'] / 1000:.1f}s "
                  f"{rec['llm_calls']} 次调用  "
                  f"ERC(f{rec['fatal']}/M{rec['major']}/m{rec['minor']}/w{rec['warning']})"
                  + (f"  ← {rec['error'][:56]}" if rec["error"] else ""))
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C：已中断，将基于已完成的条目输出部分报告…")

    summary = summarize(records, len(BENCHMARK_CASES))
    print_summary(summary, records)
    write_report(args.out, summary, records, args.limit, client.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
