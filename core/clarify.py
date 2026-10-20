# -*- coding: utf-8 -*-
"""
clarify.py —— 需求澄清（判歧 → 提问 → 合并），零第三方依赖
===========================================================
设计动机：此前自然语言流水线对模糊需求直接“凭猜测”生成——“做一个电机
控制电路”会被默默按直流电机 + 默认电压生成，可能偏离用户真实意图。
本模块在生成前加一道“歧义检测”闸门：

* 两条判歧路径（LLM 判歧 + 规则兜底）：
  - analyze(client!=None)：DeepSeek 输出结构化 JSON（clear / clarify+questions），
    json_mode 接口层强约束；坏 JSON / 网络异常 / 预算耗尽 —— 自动落规则兜底；
  - rule_analyze()：纯 Python 规则，离线/无 Key 也可用。刻意保守——
    能按工程惯例取默认值的（限流电阻、5V 供电、555 频率≈1Hz）不问，
    只问四类实质影响电路结构的问题：功能不明 / 一词多义 / 功率负载缺供电 /
    目录多候选歧义（与 catalog.match_one_detail 共用同一份分数）；
* 判定结果统一为 ClarifyResult dict：
    {"status": "clear",   "questions": [], "assumptions": [], "source": ...}
    {"status": "clarify", "questions": [ClarifyQuestion...], ...}
  ClarifyQuestion = {"question": str, "options": [str...], "reason": str}
* merge_clarifications()：用户作答后把问答对合并成“增强需求”文本，
  交给既有生成流水线（不擅自假设：未作答的问题不会出现在合并文本里）；
  合并后超长自动截断，保证 /api/generate 的 MAX_QUERY_LEN 上限不被击穿。

入口：server.py /api/clarify（Web）与 ai2kicad.py maybe_clarify（CLI）。
"""

import logging
import re
from typing import Any, Dict, List, Optional

from core import catalog
from core import llm

log = logging.getLogger(__name__)

MAX_QUESTIONS = 3          # 一次最多问几个问题（多问易劝退用户）
MAX_OPTIONS = 4            # 每个问题最多候选数
_QUESTION_LEN = 120        # 单条问题/原因文本上限
_OPTION_LEN = 60           # 单个候选选项上限
_ANSWER_LEN = 120          # 用户答案合并进需求时的上限


# =====================================================================
# 1. LLM 判歧提示词（json_mode：prompt 内含 "JSON" 字样满足接口约束；
#    用户需求经 user message 传入，不做 .format 注入，避免大括号注入）
# =====================================================================

CLARIFY_PROMPT = """你是电路需求分析助手。判断用户的中文电路需求是否清晰到可以直接生成电路原理图，只输出一个 JSON 对象。

输出格式（二选一）：
{{"status": "clear"}}
{{"status": "clarify", "questions": [{{"question": "...", "options": ["...", "..."], "reason": "..."}}]}}

判定规则：
1. 只有当歧义或缺失会实质改变电路结构或关键参数时才提问；能按工程惯例取默认值的
   （如限流电阻阻值、常见 5V 供电、555 闪烁频率约 1Hz）不要问。
2. 一词多义必须问（例如"电机"可能是直流电机或舵机，"开关"可能是按钮/拨动开关/继电器）。
3. 完全看不出要做什么电路时必须问功能。
4. 最多 {max_q} 个问题；每个问题给 2~4 个具体可执行的候选选项；
   reason 用一句话说明为什么要问。
5. 不要问与电路设计无关的问题，不要索要需求中已经给出的信息。"""


# =====================================================================
# 2. LLM 结果规范化：任何形状异常都安全降级（返回 None = 落规则兜底）
# =====================================================================

def _clean_str(v: Any, limit: int) -> str:
    """裁剪为单行字符串并限长；非字符串返回 ''。"""
    if not isinstance(v, str):
        return ""
    return " ".join(v.split())[:limit].strip()


def normalize_llm_result(obj: Any, max_questions: int = MAX_QUESTIONS
                         ) -> Optional[Dict[str, Any]]:
    """把 LLM 原始输出规范化为 ClarifyResult；无法采用时返回 None。

    宽容剔除、严格兜底：单个问题非法只剔该问，全部非法 / status 非法 → None，
    由调用方落规则兜底（绝不把脏数据透传给前端/CLI）。"""
    if not isinstance(obj, dict):
        return None
    status = obj.get("status")
    if status == "clear":
        return {"status": "clear", "questions": [], "assumptions": []}
    if status != "clarify":
        return None
    raw_qs = obj.get("questions")
    if not isinstance(raw_qs, list):
        return None
    questions: List[Dict[str, Any]] = []
    for item in raw_qs:
        if not isinstance(item, dict):
            continue
        q = _clean_str(item.get("question"), _QUESTION_LEN)
        if not q:
            continue
        raw_opts = item.get("options")
        options: List[str] = []
        if isinstance(raw_opts, list):
            for o in raw_opts:
                s = _clean_str(o, _OPTION_LEN)
                if s and s not in options:
                    options.append(s)
        reason = _clean_str(item.get("reason"), _QUESTION_LEN)
        questions.append({"question": q,
                          "options": options[:MAX_OPTIONS],
                          "reason": reason})
        if len(questions) >= max_questions:
            break
    if not questions:
        return None
    return {"status": "clarify", "questions": questions, "assumptions": []}


# =====================================================================
# 3. 规则兜底判歧（离线可用，刻意保守）
# =====================================================================

# 供电线索：出现任一即认为"供电已说明"，不再追问
_POWER_RE = re.compile(r"\d+\s*[vV伏]|\d+\s*毫?安|电池|市电|usb|USB|供电|电源|充电宝|稳压")

# 需求里已出现这些类型 = 供电意识已体现，不问供电
_POWER_TYPES = frozenset({"battery", "ac_source", "vcc", "gnd"})

# 只有功率型负载才对供电电压敏感（LED/555/运放类默认 5V 惯例即可，不问）
_ASK_POWER_LOADS = frozenset({"dc_motor", "servo", "relay"})

_I2C_RE = re.compile(r"i\s*2\s*c|iic|两线总线", re.IGNORECASE)
_I2C_CONTROLLER_RE = re.compile(r"单片机|微控制器|主控|\bmcu\b", re.IGNORECASE)
_I2C_SENSOR_RE = re.compile(r"传感器|传感模块")
_I2C_DISPLAY_RE = re.compile(r"显示模块|显示屏|oled", re.IGNORECASE)
_I2C_KNOWN_CONTROLLER_RE = re.compile(r"atmega328p?|esp32", re.IGNORECASE)
_I2C_KNOWN_DEVICE_RE = re.compile(r"bmp280|mpu6050|ssd1306|oled", re.IGNORECASE)
_I2C_VOLTAGE_RE = re.compile(r"(?:3\s*\.\s*3|5)\s*(?:v|伏)", re.IGNORECASE)

# 一词多义表：term 命中且无任何 refined 限定词时才提问
# （match_components 的消歧解决"匹配到哪个目录类型"，这里解决更上层的
#  “用户到底想要哪种东西”——两者互补不冲突）
_AMBIGUOUS_TERMS = [
    {"term": "电机", "refined": ("直流", "舵机", "伺服", "步进", "马达"),
     "question": "需求中的“电机”指哪一种？",
     "options": ["直流电机（连续转动）", "舵机（角度控制）", "继电器开关控制的电机"],
     "reason": "“电机”有多种理解，驱动方式与电路结构差别很大"},
    {"term": "开关", "refined": ("按钮", "按键", "轻触", "拨动", "继电器"),
     "question": "“开关”想用哪种实现？",
     "options": ["按钮/轻触按键", "拨动开关", "继电器（电控开关）"],
     "reason": "“开关”可能是手按或电控，电路接法不同"},
    {"term": "灯", "refined": ("led", "LED", "发光", "灯泡", "照明", "数码"),
     "question": "“灯”具体指什么？",
     "options": ["LED 指示灯", "多个 LED（并联/流水）", "LED + 蜂鸣器声光提示"],
     "reason": "“灯”的种类与数量直接影响电路结构"},
]

# 功能完全不明时给出的功能候选（与 templates.py 兜底模板一一对应）
_FUNCTION_OPTIONS = ["LED 限流点灯", "555 闪烁灯", "运放反相放大", "电位器分压/调光"]


def rule_analyze(query: str, max_questions: int = MAX_QUESTIONS
                 ) -> Dict[str, Any]:
    """纯规则歧义检测。返回 ClarifyResult（不含 source 字段，调用方补）。

    保守原则：能按工程惯例取默认值的不问（与 ai2kicad“约定默认值 + 报告
    注明来源”的既有理念一致），只问实质影响电路结构的四类问题。"""
    q = query.strip()
    questions: List[Dict[str, Any]] = []
    types = set(catalog.match_components(q))

    def add(question: str, options: List[str], reason: str) -> None:
        # merge_clarifications 保留原需求中的“未定”文字；已回答的问题
        # 不能因为原句仍在而被下一轮再次提出。
        if question + "——" in q:
            answer = q.split(question + "——", 1)[1].split("\n", 1)[0].strip()
            if answer and not re.search(r"不知道|未定|没确定|尚未|不清楚", answer):
                return
        if len(questions) < max_questions:
            questions.append({"question": question,
                              "options": options[:MAX_OPTIONS],
                              "reason": reason})

    # ① 功能不明：本地元件匹配与模板路由双双落空 —— 不问清楚只能瞎画
    if not types and not _I2C_RE.search(q) and not catalog.match_template(
            q, _fallback_templates()):
        add("您想做一个什么功能的电路？", list(_FUNCTION_OPTIONS),
            "需求中没有识别出具体元件或功能，无法确定电路拓扑")

    # ② 一词多义：命中泛化词且没有任何限定词
    for entry in _AMBIGUOUS_TERMS:
        if entry["term"] in q and not any(r in q for r in entry["refined"]):
            add(entry["question"], list(entry["options"]), entry["reason"])

    # ③ 功率负载缺供电：电机/舵机/继电器对电压敏感（3V/6V/12V 结构差异大），
    #    且无电源类元件、无供电线索时才问（LED/555 等默认 5V 惯例不问）
    if (types & _ASK_POWER_LOADS and not types & _POWER_TYPES
            and not _POWER_RE.search(q)):
        add("电路用什么供电？",
            ["5V（USB/稳压电源）", "9V 电池", "12V 直流", "220V 市电（需整流降压）"],
            "功率负载对供电电压敏感，电压决定驱动与取值")

    # 低压 MCU/I2C 子板：泛称“单片机”不能直接等同 ATmega328P，
    # 泛称“传感器”也不能静默选 BMP280。总线电压影响上拉和电平兼容。
    if _I2C_RE.search(q):
        if _I2C_CONTROLLER_RE.search(q) and not _I2C_KNOWN_CONTROLLER_RE.search(q):
            add("I2C 主控具体使用哪一型号？",
                ["ATmega328P", "ESP32-WROOM", "其他型号（请写明）"],
                "主控型号决定 SDA/SCL 的真实引脚与供电范围")
        if ((_I2C_SENSOR_RE.search(q) and not re.search(r"bmp280|mpu6050", q, re.I))
                or (_I2C_DISPLAY_RE.search(q) and not re.search(r"ssd1306", q, re.I))):
            add("I2C 从设备具体使用哪一型号？",
                ["BMP280", "MPU6050", "SSD1306 OLED", "其他型号（请写明）"],
                "不同从设备的引脚、供电和地址配置不同")
        voltage_unset = re.search(
            r"(?:电压|供电)[^，。；;\n]{0,16}(?:未定|未确定|没确定|未选择|待定|尚未|后续|未决定|没选)", q)
        if not _I2C_VOLTAGE_RE.search(q) or voltage_unset:
            add("I2C 总线和器件使用什么电压？",
                ["全部 3.3V", "全部 5V（器件均须支持）", "5V 主控与 3.3V 从设备（需电平转换）"],
                "总线电压决定上拉电源及器件输入是否兼容")
        if re.search(r"(?:地址|SDO|AD0)[^，。；;\n]{0,18}(?:没决定|未定|未确定|尚未|待定)", q, re.I):
            add("同一 I2C 总线各从设备的地址与地址选择引脚如何配置？",
                ["分别配置不同地址（请写明）", "分开总线或使用多路复用器", "待核对具体模块"],
                "重复地址的同型号从设备无法在同一总线上分别寻址")
        if re.search(r"时序[^，。；;\n]{0,12}(?:尚未|未定|没确定|未确定)", q):
            add("电源好信号与 EN 的使能时序是什么？",
                ["电源稳定后释放 EN（请给延迟）", "由指定时序器控制（请写明）", "待核对器件手册"],
                "上电时序会影响主控复位和稳定启动")
        if re.search(r"(?:仲裁|主控分工)[^，。；;\n]{0,16}(?:尚未|未定|没确定|未确定)", q):
            add("多主 I2C 的仲裁协议和两颗主控分工是什么？",
                ["指定一个主控，另一个从设备", "使用支持多主仲裁的协议（请写明）", "待确定协议"],
                "两颗主控同时驱动总线需要明确的仲裁与冲突处理")

    # ④ 目录多候选歧义：需求词与目录中两个类型同时高度接近
    #    （双方过模糊阈值且分差在歧义阈值内），流水线只能“按高分猜一个”——
    #    与 catalog.match_one_detail / llm.extract_components 共用同一份
    #    分数，判歧口径全链路一致。把得分前二的候选作为选项让用户拍板。
    #    前置 not types：子串已命中至少一个元件（如“用555做一个LED闪烁灯”）
    #    说明需求已明确提及元件，长句 difflib 的中等相似度不再升级成提问，
    #    避免把清晰需求误判成歧义（实测「LED 闪烁灯」整句会凑出假歧义）。
    if not types:
        try:
            detail = catalog.match_one_detail(q)
        except Exception:  # noqa: BLE001 —— 打分异常不拖垮其余判歧规则
            detail = None
        if detail and detail.get("ambiguous") and detail.get("runner_up"):
            best_name = detail.get("name") or detail["type"]
            runner_name = detail.get("runner_up_name") or detail["runner_up"]
            opts: List[str] = []
            for o in (best_name, runner_name):
                if o and o not in opts:
                    opts.append(o)
            if len(opts) >= 2:
                add("需求可能对应多种元件，你想用哪一种？", opts,
                    f"“{q[:40]}”与 {best_name}（{detail['score']:.2f}）和 "
                    f"{runner_name}（{detail['runner_up_score']:.2f}）都很接近")

    status = "clarify" if questions else "clear"
    return {"status": status, "questions": questions, "assumptions": []}


def _fallback_templates() -> dict:
    """兜底模板路由表（templates 单一数据源）；导入失败时退化为空表，
    功能不明规则随之退化为只看元件匹配，不影响其余规则。"""
    try:
        from core import templates
        return templates.FALLBACK_TEMPLATES
    except Exception as tpl_err:  # noqa: BLE001 —— 兜底数据缺失不应拖垮判歧
        # 静默吞异常会掩盖模板表损坏（改名/语法错误），debug 留痕可排查
        log.debug("templates 兜底路由表加载失败，退化为空表：%s: %s",
                  type(tpl_err).__name__, tpl_err)
        return {}


# =====================================================================
# 4. 统一入口：LLM 判歧 → 规范化 → 异常/坏形状自动落规则兜底
# =====================================================================

def analyze(query: str, client: Optional[llm.DeepSeekClient] = None,
            budget: Optional[llm.CallBudget] = None,
            cancel_event=None, max_questions: int = MAX_QUESTIONS
            ) -> Dict[str, Any]:
    """判歧统一入口。返回 ClarifyResult + source ∈ {"llm", "rules"}。

    client=None（无 Key/离线）直接走规则；LLM 路径任何失败都落规则——
    判歧是“增强”，永远不得阻塞生成主流程。"""
    if client is not None:
        try:
            if budget is not None:
                budget.spend()
            raw = client.chat(
                CLARIFY_PROMPT.format(max_q=max_questions), query,
                json_mode=True, cancel_event=cancel_event)
            norm = normalize_llm_result(llm.extract_json_object(raw),
                                        max_questions)
            if norm is not None:
                # 关键 I2C 型号/电压缺失时，模型的 clear 不能绕过确定性闸门。
                if norm["status"] == "clear" and _I2C_RE.search(query):
                    required = rule_analyze(query, max_questions)
                    if required["status"] == "clarify":
                        required["source"] = "rules"
                        return required
                norm["source"] = "llm"
                return norm
            log.warning("LLM 判歧输出形状非法，落规则兜底：%.120r", raw)
        except Exception as e:  # noqa: BLE001 —— 网络/预算/解析失败均落规则
            log.info("LLM 判歧不可用，落规则兜底：%s: %s", type(e).__name__, e)
    result = rule_analyze(query, max_questions)
    result["source"] = "rules"
    return result


# =====================================================================
# 5. 问答合并：用户确认后的答案并回需求文本（未作答 = 不擅自假设）
# =====================================================================

def merge_clarifications(query: str, qa_pairs,
                         max_len: int = 500) -> str:
    """把澄清问答合并成增强需求。qa_pairs: [{"question":..., "answer":...}]。

    * 空答案 / 非 dict 项直接跳过——未作答的问题不补任何假设；
    * 合并文本超 max_len（默认对齐 config.MAX_QUERY_LEN）从尾部截断，
      保证下游 /api/generate 的长度上限不被问答合并击穿。"""
    lines: List[str] = []
    if isinstance(qa_pairs, list):
        for qa in qa_pairs:
            if not isinstance(qa, dict):
                continue
            q_text = _clean_str(qa.get("question"), 80)
            a_text = _clean_str(qa.get("answer"), _ANSWER_LEN)
            if not a_text:
                continue
            lines.append(f"{q_text}——{a_text}" if q_text else a_text)
    if not lines:
        return query
    merged = query.rstrip() + "\n补充说明：" + "；".join(lines)
    return merged[:max_len] if len(merged) > max_len else merged
