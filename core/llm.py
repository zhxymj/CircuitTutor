# -*- coding: utf-8 -*-
"""
core/llm.py —— 智绘电路 CircuitTutor 的 LLM 生成层
=========================================================
按智绘电路「Flask + Python 标准库、零第三方重依赖、中文输入」的技术栈
实现：

* 只用标准库 http.client 调 DeepSeek（OpenAI 兼容 /chat/completions），不装 openai SDK；
  连接 keep-alive 复用（每次调用省一次 TCP+TLS 握手），线程私有连接在 threaded
  Flask 下安全；网络错误 / 429 / 5xx 自动指数退避重试，超时与重试次数均可配
  （LLM_TIMEOUT_S / LLM_RETRIES）；
* prompt 改为中文，并把【元件引脚白名单】注入进去 —— 这是防止 LLM 编造
  引脚 / 元件（幻觉）最关键的一招；GEN_PROMPT 在单个示例之外补充
  “反相比例放大器”正例（演示 + / - / out 引脚配对与反馈回路怎么连）和
  “编造引脚 / 位号重复 / 未共地”负例，few-shot 正负对照抑制幻觉；
* temperature=0 保证可复现（答辩稳定）；
* 内置健壮 JSON 提取，兼容 LLM 返回 ```json 代码块、// 注释、尾逗号、单引号；
* 运行时护栏：CallBudget 全局调用预算封顶（杜绝“规划+生成+重试+双修复”
  最坏 7 次串行调用）；CallBudget 是流水线唯一的终止上限——max_retry /
  LLM_ERC_RETRIES 等重试配置只决定“把预算花在哪几轮”，不产生第二个上限，
  预算耗尽即 best-effort 返回已有结果或回落模板；extract_components 本地
  匹配短路（命中即免第一次 LLM 往返，实测 0.15ms / 30 用例全对）；
  generate_circuit_json 支持 on_progress 回调，供 SSE 把“规划中→生成中→
  校验→完成”实时推给前端。

2026-09-07 LLM 生成层四项改进落地：
* chat() 新增 json_mode 参数：生成 / 修复 / 抽取调用启用 DeepSeek JSON 输出
  模式（response_format={"type":"json_object"}），结构化输出从“文本约束”
  升级为“接口层强约束”；规划调用保持纯文本（思维链不宜被 JSON 格式束缚）；
* chat() 新增 cancel_event 参数：SSE 客户端断开后由 server.py 置位，
  每次 HTTP 尝试前 / 重试退避前检查，置位即抛 OperationCancelled ——
  最多浪费“当前正在进行的这一次”调用，后续调用全部取消，不再烧钱；
* REPAIR_PROMPT 拆分为 STRUCT_REPAIR_PROMPT（只修结构错误）与
  ERC_REPAIR_PROMPT（只修电气错误，并注入规划上下文 design_advice，
  修复阶段不丢失设计意图）。

2026-09-10 LLM 健壮性五条落地：
* 重试加温：生成/结构修复/ERC 修复的重试轮次 temperature 0→0.3，
  并在 user 文本注入“上次输出无法解析”的具体提示——零温重试往往精确
  复现同一错误，微升温帮助模型跳出局部失败；
* 规划失败不炸管线：PLAN 调用失败（网络最终失败等）只记录告警、
  以无规划模式继续生成；取消（OperationCancelled）与预算耗尽
  （BudgetExhausted）仍按各自语义传播，不被误吞；
* 空 content 重试：思维链烧尽 max_tokens 导致的空响应按可重试
  处理（chat() 内实现，见 200 分支）；
* ERC 修复多轮化：config.LLM_ERC_RETRIES（默认 1）控制修复轮数，
  每轮基于【最新电路】重新回灌错误清单；有改善才续轮，问题数不减即停
  （轮数 + CallBudget 双上限，不会无限循环）；
* 修复上下文增强：ERC_REPAIR_PROMPT 的错误清单追加“引脚→网络名”
  上下文（to_repair_prompt(graph)），模型能看见每个问题引脚所在网络，
  不再对着裸错误文本猜连线。

CircuitJSON 数据契约（前后端统一，和你现有模板保持同一结构）：
{
  "version": 1,
  "parts": [ {"type":"resistor","id":"R1","top":120,"left":200,
              "rotate":0,"attrs":{"value":"1k"}} ],
  # 每条连接 = [起点引脚, 终点引脚, 颜色, 相对路径指令[]]
  # 引脚引用格式 "元件id:引脚名"；路径指令用不到时给 []，交给前端自动走线。
  "connections": [ ["R1:1","LED1:+","red",[]] ]
}
"""

import json
import re
import ast
import functools
import logging
import os
import sys
import ssl
import time
import threading
import http.client
import urllib.parse
from collections import Counter
from typing import Optional, Dict, Any, List, Callable, Tuple

try:
    from core import config as app_config      # 全局配置（超时/重试等环境变量统一入口）
except ImportError:                  # 允许脱离项目目录单文件使用，退回内置默认
    app_config = None

try:
    from core.catalog import CATALOG           # 标准元件目录（防幻觉封闭集 + 生成引脚表）
except ImportError:                  # 脱离项目目录单文件使用时，退回仅用传入的白名单
    CATALOG = {}

log = logging.getLogger(__name__)    # 可观测性：重试告警等（级别由入口 setup_logging 决定）

from core.intent_constraints import IntentConstraintError, detect_requirement_blockers


@functools.lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """构造 HTTPS 连接用的 SSL 上下文（兼容教学机房网络环境），进程内缓存。

    两个现实问题（2026-09-08 实测定位）：
    1) 本项目自带的 Python 发行版 OpenSSL 默认 CA 路径不存在（无 cert.pem），
       证书验证会报 self signed certificate —— 从 Windows 证书库补齐受信 CA；
    2) 部分校园网/机房网关会干扰 OpenSSL TLS 1.3 握手（表现为超时或 10054
       连接重置，curl/Schannel 却正常）—— 设 DEEPSEEK_TLS12=1 可回退 TLS 1.2。

    缓存说明：Windows 证书库可能有上百张证书，此前每次新建连接都重新
    枚举加载一遍（每次可达数百毫秒）；上下文本身不可变，lru_cache 进程内
    只构建一次，新建连接直接复用。"""
    ctx = ssl.create_default_context()
    if sys.platform == "win32":
        try:
            for store in ("ROOT", "CA"):
                for der, enc, _trust in ssl.enum_certificates(store):
                    if enc == "x509_asn":
                        try:
                            ctx.load_verify_locations(
                                cadata=ssl.DER_cert_to_PEM_cert(der))
                        except ssl.SSLError as cert_err:
                            # 个别证书编码异常跳过即可，但要留下痕迹可排查
                            log.debug("跳过无法解析的 Windows 证书：%s", cert_err)
        except Exception as store_err:  # noqa: BLE001 —— 证书库缺失回退默认 CA 路径
            log.warning("Windows 证书库加载失败，回退 OpenSSL 默认 CA 路径：%s: %s",
                        type(store_err).__name__, store_err)
    # TLS 1.2 回退开关统一走 config（0/false/off/no 显式关闭，仅 1/true/yes/on
    # 开启，语义与 bool(非空即真) 相反的旧写法已废弃）；脱离项目单文件使用
    # （app_config 不可用）时按同一语义读环境变量。
    _tls12 = getattr(app_config, "TLS12", None)
    if _tls12 is None:
        _tls12 = os.environ.get("DEEPSEEK_TLS12", "").strip().lower() in ("1", "true", "yes", "on")
    if _tls12:
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


# =====================================================================
# 1. Prompt 模板（中文）。{xxx} 是运行时填充位，用 .format() 时注意转义大括号。
# =====================================================================

# 1.1 先让模型做一次“电气规划”（思维链），只出规划、不出图，降低致命错误
#     2026-09-11 对齐 boardsmith「先算物理再连线」、circuitron「先定网络再接脚」
PLAN_PROMPT = """你是一名电子电路规划助手。根据用户的中文需求，先完成规划，不要输出最终电路图。

按下面顺序输出规划（总条数不超过 8 条，每条用简短中文）：

1. 【网络】先命名网络：电源轨（如 VCC/5V）、地 GND、以及主要信号节点；
2. 【供电】电源/地从哪里来，工作电压是多少；
3. 【保护】LED 是否串联限流、感性负载是否续流二极管、按键是否上下拉、芯片是否就近去耦；
4. 【信号方向】信号从哪个引脚输出、到哪个引脚输入，禁止输出脚对输出脚；
5. 【引脚配对】用【可用元件及其引脚】写出关键连接（元件id:引脚 → 元件id:引脚）；
6. 【参数计算】关键阻容按工程公式算出推荐值（写出算式），例如：
   - LED 限流：R=(Vcc−Vf)/10mA，Vf 红/绿约 2V、蓝/白约 3V，结果吸附 E12（220/330/470/1k…）；
   - 分压：Vout=Vin·R下/(R上+R下)；
   - 芯片去耦：电源脚旁 100nF。
7. 【拓扑骨架】用 3~5 句话描述整体骨架（从电源到地的电流路径）。

可用元件与引脚：
{available_components}"""

# 1.2 元件抽取：先把需求里用到的元件名抽出来，便于后端用本地元件库做匹配/拦截
EXTRACT_PROMPT = ("从下面的中文电路需求中，抽取需要用到的元件类型名称，"
                  "只输出一个 JSON 一维字符串数组，不要任何解释。\n"
                  "常见别名对照（可用中文或英文名）：LED/发光二极管→LED；电阻→resistor；"
                  "电容→capacitor；电池/电源→battery；地→gnd；三极管 NPN→npn；"
                  "555/定时器→ne555；运放→opamp；电位器→potentiometer；按键→pushbutton；"
                  "蜂鸣器→buzzer；齐纳/稳压二极管→zener；7805→regulator_7805。\n"
                  "需求：{user_prompt}")

# 1.3 核心：生成 CircuitJSON。{component_pins} 注入“本次允许使用的元件 + 合法引脚”，
#     {design_advice} 注入上一步的规划（仅作指导，避免跑偏）
GEN_PROMPT = """你要把用户的中文电路需求转换成一个合法的 CircuitJSON，用于渲染电路原理图。

标准元件目录（全部可用）及其【合法引脚白名单】如下（引脚名只能从这里取，严禁编造）。
优先使用与用户需求直接相关的元件，不要堆砌目录里用不上的元件：
{component_pins}

类型定义：
- Part = {{type:字符串, id:字符串, top:数字, left:数字, rotate?:0/90/180/270, attrs:{{value?:字符串}}}}
- Connection = ["元件id:引脚名", "元件id:引脚名", "导线颜色", [相对走线指令]]
- CircuitJSON = {{version:1, parts:Part[], connections:Connection[]}}

硬性要求：
1. 只输出 CircuitJSON 本身，不要 markdown、不要解释、不要注释。
2. connections 里出现的每个“引脚名”必须能在该元件的引脚白名单里找到，绝不允许臆造引脚。
   例：ATmega328P 的 I2C 在 PC4/PC5，应写 “芯片:PC4” / “芯片:PC5”，不能写不存在的 “芯片:SDA”。
3. 每个连接端点必须是完整的字符串“元件id:引脚名”（半角冒号，冒号两侧都不可省略）。
   非法示范：“BAT1”（缺引脚）、“R1:”（缺引脚名）、裸数字、嵌套数组，均会被结构校验拒绝。
4. 三引脚及以上的元件必须按白名单逐脚接线，不可漏脚、不可复用同一引脚充当两个角色。
   例：电位器 1/2 是两端、3 是滑片；三极管 B/C/E；7805 IN/GND/OUT。
5. 元件不要重叠，彼此至少留 50 像素；相关引脚尽量对齐，让连线短而清晰。
6. 走线只允许水平/垂直正交（L 形或阶梯形），从引脚先垂直引出再转向；距离取 5/10 的整数倍；
   导线不要穿过元件本体。若不想手算路径，第 4 个元素直接给 []，由前端自动布线。
7. 不允许悬空导线：每条连接两端都必须是真实存在的引脚。
8. 电气安全：
   - LED / 数码管必须串联限流电阻（典型 220Ω~1kΩ）；
   - 输出脚（GPIO/OUT）不许直接接到电源轨或另一个输出脚；
   - 所有有源器件必须共地；
9. 元件参数写进 attrs.value，如 {{"value":"10k"}}、{{"value":"100nF"}}。
10. 画布约 1000x600，元件摆放在中部。id 用“类型前缀+序号”，如 R1、C1、LED1、U1。

规划参考（仅作指导，以用户需求为准）：
{design_advice}

示例一（电池-电阻-LED 基础回路）：
{{
  "version": 1,
  "parts": [
    {{"type":"battery","id":"BAT1","top":80,"left":60,"attrs":{{"value":"5V"}}}},
    {{"type":"resistor","id":"R1","top":80,"left":220,"attrs":{{"value":"330Ω"}}}},
    {{"type":"led","id":"LED1","top":80,"left":380,"attrs":{{"value":"红"}}}}
  ],
  "connections": [
    ["BAT1:+","R1:1","red",[]],
    ["R1:2","LED1:+","red",[]],
    ["LED1:-","BAT1:-","black",[]]
  ]
}}

示例二（反相比例放大器：输入经 R1 到反相端“-”，反馈电阻 R2 从 out 接回反相端，同相端“+”接地）：
{{
  "version": 1,
  "parts": [
    {{"type":"battery","id":"BAT1","top":60,"left":60,"attrs":{{"value":"0.1V"}}}},
    {{"type":"opamp_inverting","id":"U1","top":120,"left":320}},
    {{"type":"resistor","id":"R1","top":60,"left":180,"attrs":{{"value":"10k"}}}},
    {{"type":"resistor","id":"R2","top":260,"left":240,"attrs":{{"value":"100k"}}}},
    {{"type":"gnd","id":"GND1","top":340,"left":60}}
  ],
  "connections": [
    ["BAT1:+","R1:1","red",[]],
    ["R1:2","U1:-","red",[]],
    ["U1:-","R2:1","red",[]],
    ["R2:2","U1:out","red",[]],
    ["U1:+","GND1:1","black",[]],
    ["BAT1:-","GND1:1","black",[]]
  ]
}}

反例（下面这段输出是【错误示范】，其中每一类问题都严禁出现在你的输出里）：
{{
  "version": 1,
  "parts": [
    {{"type":"opamp_inverting","id":"U1"}},
    {{"type":"resistor","id":"R1","attrs":{{"value":"10k"}}}},
    {{"type":"resistor","id":"R1","attrs":{{"value":"100k"}}}},
    {{"type":"battery","id":"BAT1"}}
  ],
  "connections": [
    ["BAT1:+","U1:VCC","red",[]],
    ["R1:2","U1:IN+","red",[]]
  ]
}}
错误原因：
- “U1:VCC”“U1:IN+” 是编造的引脚——opamp_inverting 的合法引脚只有 +、-、out；
- 输入电阻和反馈电阻位号都写了 R1，id 必须唯一（应写成 R1、R2）；
- 电池负极悬空、整个电路未共地，电流没有回路。

示例三（电位器调光：三引脚元件电位器，1/2 是两端、3 是滑片——两端跨接电源，
滑片经限流电阻接 LED，转动电位器即可调亮度）：
{{
  "version": 1,
  "parts": [
    {{"type":"battery","id":"BAT1","top":80,"left":60,"attrs":{{"value":"5V"}}}},
    {{"type":"potentiometer","id":"RV1","top":80,"left":240,"attrs":{{"value":"10k"}}}},
    {{"type":"resistor","id":"R1","top":80,"left":420,"attrs":{{"value":"330Ω"}}}},
    {{"type":"led","id":"LED1","top":80,"left":560,"attrs":{{"value":"红"}}}},
    {{"type":"gnd","id":"GND1","top":280,"left":60}}
  ],
  "connections": [
    ["BAT1:+","RV1:1","red",[]],
    ["RV1:2","GND1:1","black",[]],
    ["RV1:3","R1:1","red",[]],
    ["R1:2","LED1:+","red",[]],
    ["LED1:-","GND1:1","black",[]],
    ["BAT1:-","GND1:1","black",[]]
  ]
}}

示例四（NE555 多谐振荡器闪烁灯——教学最常见骨架，接法固定请背下来）：
- 电源：VCC→VCC/RESET/去耦电容；GND→芯片 GND；
- 定时：VCC→R1→DISCH；DISCH→R2→THR；THR 与 TRIG 短接并接定时电容正极，电容负极接地；
- CTRL：旁路电容到地；OUT→限流电阻→LED+，LED−→GND。
{{
  "version": 1,
  "parts": [
    {{"type":"vcc","id":"V1","top":40,"left":60,"attrs":{{"value":"5V"}}}},
    {{"type":"ne555","id":"U1","top":140,"left":280,"attrs":{{"value":"NE555"}}}},
    {{"type":"resistor","id":"R1","top":40,"left":180,"attrs":{{"value":"1k"}}}},
    {{"type":"resistor","id":"R2","top":140,"left":180,"attrs":{{"value":"10k"}}}},
    {{"type":"resistor","id":"R3","top":220,"left":520,"attrs":{{"value":"330Ω"}}}},
    {{"type":"capacitor","id":"C1","top":240,"left":180,"attrs":{{"value":"10µF"}}}},
    {{"type":"capacitor","id":"C2","top":140,"left":420,"attrs":{{"value":"100nF"}}}},
    {{"type":"capacitor","id":"C3","top":40,"left":420,"attrs":{{"value":"100nF"}}}},
    {{"type":"led","id":"LED1","top":220,"left":680,"attrs":{{"value":"红"}}}},
    {{"type":"gnd","id":"GND1","top":340,"left":280}}
  ],
  "connections": [
    ["V1:1","R1:1","red",[]],
    ["R1:2","U1:DISCH","red",[]],
    ["U1:DISCH","R2:1","red",[]],
    ["R2:2","U1:THR","red",[]],
    ["U1:THR","U1:TRIG","red",[]],
    ["U1:TRIG","C1:+","red",[]],
    ["C1:-","GND1:1","black",[]],
    ["U1:CTRL","C2:+","blue",[]],
    ["C2:-","GND1:1","black",[]],
    ["V1:1","U1:RESET","red",[]],
    ["V1:1","U1:VCC","red",[]],
    ["V1:1","C3:+","red",[]],
    ["C3:-","GND1:1","black",[]],
    ["U1:OUT","R3:1","green",[]],
    ["R3:2","LED1:+","green",[]],
    ["LED1:-","GND1:1","black",[]],
    ["U1:GND","GND1:1","black",[]]
  ]
}}

输出前自检（必须全部满足，再吐出 JSON）：
- 每个有源器件电源脚都接到电源网络，GND 脚都接到地；
- 每个 LED 的电流路径上有限流电阻；
- 无重复 id；无编造引脚；无悬空导线；
- 阻容值已按需求/欧姆定律写好 attrs.value（如 330Ω、100nF、10k）；
- 布局：电源在上、地在下、信号大致从左向右，元件间距 ≥50px。
只输出 CircuitJSON 对象本身。"""

# 1.4 修复提示词：错误回灌闭环。
#     结构修复和电气修复关注点完全不同，拆成两个模板各司其职 ——
#     结构修复解决“格式/引脚/位号”层面的问题，ERC 修复解决“电气安全”层面的
#     问题，并且注入规划上下文，避免修复时把设计意图一起改没了。
# 1.4a 结构修复：输出格式/结构有误（编造引脚、未知元件、重复位号、悬空导线等）
STRUCT_REPAIR_PROMPT = """你上一次输出的 CircuitJSON 在【格式 / 结构】上有误，没有通过结构校验。错误清单：
{struct_errors}

端点格式强约束（最常见的结构错误）：
- 每个连接端点必须是完整字符串“元件id:引脚名”，半角冒号两侧都不可省略；
- 正确示范："BAT1:+"、"RV1:3"；非法示范："BAT1"（缺引脚）、"R1:"（缺引脚名）、
  3、["BAT1:+"]（嵌套数组）；
- 三引脚元件（电位器 1/2/3、三极管 B/C/E、7805 IN/GND/OUT）必须按白名单逐脚接线。

请修正以上全部结构错误，重新输出【完整的】CircuitJSON（一个 json 对象）。
要求：
1. 只输出 CircuitJSON 本身，不要 markdown、不要解释、不要注释；
2. 引脚名只能取自下面的白名单，严禁编造：
{component_pins}
3. 不得增删用户需求要求的元件、不得改变连接意图，只修结构错误；
4. 保持 Part/Connection 结构与字段定义不变；
5. 修完后自检：id 唯一、端点均为“元件id:引脚名”、三引脚元件脚位齐全、无悬空端点。

用户原始需求：{user_prompt}"""

# 1.4b ERC 修复：结构合法但有电气错误。{design_advice} 注入规划上下文，
#     修复时“心中有图纸”，只修电气问题、不丢设计意图
ERC_REPAIR_PROMPT = """你上一次生成的电路图结构合法，但没有通过电气规则检查（ERC）。电气错误清单：
{erc_errors}

规划阶段给出的电气规划（修复时保持这一设计意图，不要推翻重来）：
{design_advice}

请修复以上全部 fatal/major 错误（minor/warning 尽量修复），重新输出【完整的】CircuitJSON（一个 json 对象）。
要求：
1. 只输出 CircuitJSON 本身，不要 markdown、不要解释、不要注释；
2. 引脚名只能取自下面的白名单，严禁编造：
{component_pins}
3. 不得改变用户的原始需求和电路拓扑意图，只修电气错误
   （如补限流电阻、补共地、消除输出对输出短接、补续流二极管、芯片电源脚旁补 100nF 去耦）；
4. 若需新增电阻/电容，参数按工程计算写入 attrs.value：
   - LED 限流：R=(Vcc−Vf)/10mA 并吸附 E12（5V 红光常用 330Ω，12V 常用 1kΩ）；
   - 去耦电容：100nF；定时/滤波电容按规划；
5. 保持 Part/Connection 结构与字段定义不变；
6. 改完后再对照错误清单逐条确认已消除。

用户原始需求：{user_prompt}"""


# =====================================================================
# 2. DeepSeek 客户端（OpenAI 兼容，仅用 http.client 标准库）
#    运行时优化：keep-alive 连接复用 / 指数退避重试 / 线程安全
# =====================================================================

# 可重试的 HTTP 状态：限流与服务端瞬时故障；400/401/403 等重试无意义，立即失败
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class OperationCancelled(RuntimeError):
    """外部取消信号（如 SSE 客户端已断开）。调用方把 cancel_event 置位后，
    chat() 在下一次 HTTP 尝试前 / 重试退避前感知并抛出本异常，立即止损，
    不再为已经没人看的响应继续烧 token。"""


class DeepSeekClient:
    DEFAULT_BASE = "https://api.deepseek.com"
    # 默认模型（2026-09-10 对齐）：模型 ID 是滚动别名，官方后台已指向
    # DeepSeek-V4-Flash-0731 正式版（1M 上下文 / JSON Output / 支持思考开关）。
    # 选 Flash 作默认的理由：本项目是「结构化 JSON 生成」而非长链推理，
    # Flash 的首次合格率与 Pro 相当而成本约 1/3、延迟更低；Pro（0813 GA）
    # 保留在预设表供强推理场景（复杂模拟电路拓扑）手动切换。
    DEFAULT_MODEL = "deepseek-flash"

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None, timeout: Optional[float] = None,
                 retries: Optional[int] = None, backoff: float = 1.0):
        """
        model   : 模型 ID。默认 None，按「环境变量 DEEPSEEK_MODEL → 类常量
                  DEFAULT_MODEL」逐级回退。⚠️ 这里**不能**写成 `model=DEFAULT_MODEL`：
                  形参默认值会在调用时直接顶掉 env，导致 DEEPSEEK_MODEL 永远失效
                  （踩过一次，见 tests/test_llm_pipeline.py 的模型接线断言）。
        timeout : 单次请求超时（秒）。原 60s 会把一次网络抖动拖成整分钟黑屏，
                  默认取 config.LLM_TIMEOUT_S（环境变量 LLM_TIMEOUT_S，默认 25），
                  25s + 重试的总耗时上限更有保障。
        retries : 网络错误 / 429 / 5xx 的重试次数（默认取 config.LLM_RETRIES，
                  即环境变量 LLM_RETRIES，默认 2，共最多 3 次尝试）。
        backoff : 退避基数秒，实际等待 backoff * 2^attempt（默认 1s、2s）。
        """
        self.api_key = (api_key
                        or (app_config.API_KEY if app_config
                            else os.environ.get("DEEPSEEK_API_KEY", "")))
        # base_url 默认接通 config.BASE_URL（环境变量 DEEPSEEK_BASE_URL 在
        # config 文档中一直存在，此前客户端未接线——现补上；单文件脱离项目
        # 使用时仍回退 DEFAULT_BASE）
        self.base_url = (base_url
                         or (app_config.BASE_URL if app_config else self.DEFAULT_BASE)
                         ).rstrip("/")
        # model 接线优先级：显式入参 > 环境变量 DEEPSEEK_MODEL > 类默认（滚动别名）。
        # 空串一律视作「未指定」，避免 env 留空反而把默认模型顶掉。
        self.model = (model or "").strip() or (
            (app_config.MODEL if app_config else "").strip()
            or os.environ.get("DEEPSEEK_MODEL", "").strip()
            or self.DEFAULT_MODEL)
        self.timeout = float(timeout) if timeout is not None else (
            app_config.LLM_TIMEOUT_S if app_config else 25.0)
        self.retries = int(retries) if retries is not None else (
            app_config.LLM_RETRIES if app_config else 2)
        self.backoff = backoff
        u = urllib.parse.urlparse(self.base_url)
        self._scheme = u.scheme or "https"
        self._host = u.hostname or "api.deepseek.com"
        self._port = u.port or (443 if self._scheme == "https" else 80)
        self._path_prefix = (u.path or "").rstrip("/")
        # 连接是线程私有的：threaded Flask / 多线程调用下复用安全
        self._local = threading.local()

    def _get_conn(self, timeout: float) -> http.client.HTTPConnection:
        conn = getattr(self._local, "conn", None)
        if conn is not None and getattr(self._local, "timeout", None) == timeout:
            return conn
        if conn is not None:
            self._drop_conn()
        cls = (http.client.HTTPSConnection if self._scheme == "https"
               else http.client.HTTPConnection)
        if cls is http.client.HTTPSConnection:
            conn = cls(self._host, self._port, timeout=timeout,
                       context=_ssl_context())
        else:
            conn = cls(self._host, self._port, timeout=timeout)
        self._local.conn = conn
        self._local.timeout = timeout
        return conn

    def _drop_conn(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception as close_err:  # noqa: BLE001 —— 关闭失败不阻断重试
                log.debug("丢弃 LLM 连接时关闭失败（忽略）：%s", close_err)
            self._local.conn = None

    def close(self) -> None:
        """关闭当前线程的持久连接（长驻进程退出前可调用）。"""
        self._drop_conn()

    def chat(self, system_prompt: str, user_prompt: str,
             temperature: float = 0.0, max_tokens: Optional[int] = None,
             timeout: Optional[float] = None, retries: Optional[int] = None,
             json_mode: bool = False,
             cancel_event: Optional[threading.Event] = None) -> str:
        """单轮对话，返回模型文本。temperature=0 让结果尽量确定、可复现。
        网络层错误 / 429 / 5xx 指数退避重试；其余错误立即抛出。

        json_mode    : True 时启用 DeepSeek 的 JSON 输出模式
                       （response_format={"type":"json_object"}，OpenAI 兼容），
                       结构化输出由接口层强约束。注意 JSON mode 要求 prompt
                       中含 "json" 字样，GEN/EXTRACT/两套修复模板均已满足。
        cancel_event : 外部取消信号（如 SSE 客户端已断开）。每次 HTTP 尝试前、
                       重试退避前检查；已置位则抛 OperationCancelled，不再发请求。
        """
        if not self.api_key:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY，请先设置环境变量或传入 api_key。")

        # 链路日志：request_id 由 ContextVar 注入（core.trace），不改签名
        _t0 = None
        try:
            from core import trace as _trace
            _t0 = _trace.llm_call_started(self.model, stage="chat")
        except Exception:
            _trace = None

        def _check_cancel() -> None:
            """断开止损：检查取消信号，已置位直接抛出，避免无谓的 LLM 花费。"""
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelled(
                    "LLM 调用已被取消（客户端断开或上层中止），不再发起新请求")

        _check_cancel()   # 进入即检查：连第一次请求都不必发
        timeout = self.timeout if timeout is None else timeout
        retries = self.retries if retries is None else retries
        if max_tokens is None:
            # 未显式指定时取全局配置（默认 4096，与原行为一致）
            max_tokens = getattr(app_config, "LLM_MAX_TOKENS", 4096) if app_config else 4096
        req = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,   # 生成电路用 0，最稳定
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            # DeepSeek JSON mode：接口层保证输出是合法 JSON 对象
            req["response_format"] = {"type": "json_object"}
        # V4 / deepseek-flash / v4.1 起思考模式默认开启：思维链 token
        # 计入 max_tokens，实测 4096 可被思维链烧光、content 为空串
        # （finish_reason=length）。对 DeepSeek 官方端点【默认一律关闭思考】。
        # 规划质量靠提示词要点约束即可，不靠烧 token 的长思维链；
        # 若网关不认识 thinking 字段，下面 400 会立刻抛出，不会静默。
        # 非 DeepSeek 兼容端点不下发该字段，避免拒收。
        def _payload(disable_think: bool) -> bytes:
            r = dict(req)
            if disable_think and "deepseek" in (self._host or ""):
                r["thinking"] = {"type": "disabled"}
            return json.dumps(r).encode("utf-8")

        payload = _payload(disable_think=True)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        path = self._path_prefix + "/chat/completions"
        last_err: Optional[Exception] = None
        # 空 content 后是否已尝试过“再抬 max_tokens 并保持关思考”
        bumped_tokens = False
        for attempt in range(retries + 1):
            _check_cancel()               # 每次 HTTP 尝试前检查：断开就不再发起
            try:
                conn = self._get_conn(timeout)
                conn.request("POST", path, body=payload, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                if resp.status == 200:
                    # 响应形状校验：网关劫持 / 服务端异常时可能返回 200 + 非
                    # JSON 或缺字段的 body，直接下标取值会抛 KeyError/IndexError
                    # 这类难定位的错，这里统一转成带上下文的业务错误
                    try:
                        data = json.loads(body.decode("utf-8"))
                        choice0 = (data.get("choices") or [None])[0] or {}
                        message = choice0.get("message") or {}
                        content = message.get("content")
                        finish = choice0.get("finish_reason")
                    except (json.JSONDecodeError, UnicodeDecodeError) as shape_err:
                        raise RuntimeError(
                            "DeepSeek 响应格式异常（HTTP 200 但 body 非 JSON）："
                            f"{body[:200]!r}，根因 {type(shape_err).__name__}：{shape_err}")
                    if content is None and not message:
                        raise RuntimeError(
                            "DeepSeek 响应格式异常（HTTP 200 但缺少 choices/"
                            f"message/content）：{body[:200]!r}")
                    if isinstance(content, str) and content.strip():
                        if _trace is not None and _t0 is not None:
                            _trace.llm_call_finished(self.model, _t0, ok=True,
                                                     chars=len(content))
                        return content
                    # 空/纯思维链：关思考后仍空 → 抬 max_tokens 再试一次
                    if not bumped_tokens and finish == "length":
                        bumped_tokens = True
                        req["max_tokens"] = min(int(req["max_tokens"]) * 2, 16384)
                        payload = _payload(disable_think=True)
                        last_err = RuntimeError(
                            "DeepSeek 空 content（finish_reason=length），"
                            f"已将 max_tokens 提至 {req['max_tokens']} 并保持关思考后重试")
                    else:
                        last_err = RuntimeError(
                            "DeepSeek 返回空 content（思维链烧尽 max_tokens 或纯思考"
                            f"无正文），将重试；finish={finish!r} 头 {body[:120]!r}")
                else:
                    detail = body.decode("utf-8", "ignore")[:500]
                    err = RuntimeError(f"DeepSeek 接口错误 {resp.status}: {detail}")
                    if resp.status in RETRYABLE_STATUS:
                        last_err = err           # 429/5xx：可重试
                    else:
                        raise err                # 400/401/403 等：重试无意义
            except RuntimeError:
                raise                         # 业务错误（含不可重试状态码）直接抛
            except (http.client.HTTPException, OSError) as e:
                # 含连接被服务端闲置断开（RemoteDisconnected）、超时、拒绝连接
                last_err = RuntimeError(f"DeepSeek 网络错误（第 {attempt + 1} 次尝试）：{e}")
            # 走到这里都是可重试失败：丢掉连接，退避后换新连接重试
            self._drop_conn()
            if attempt < retries:
                wait = self.backoff * (2 ** attempt)
                log.warning("DeepSeek 调用失败（第 %d/%d 次），%.1fs 后重试：%s",
                            attempt + 1, retries + 1, wait, last_err)
                _check_cancel()               # 退避前检查：客户端已断开就别等了
                time.sleep(wait)
        log.error("DeepSeek 调用最终失败（共 %d 次尝试）：%s", retries + 1, last_err)
        if _trace is not None and _t0 is not None:
            _trace.llm_call_finished(self.model, _t0, ok=False, chars=0,
                                     error=str(last_err)[:200])
        raise last_err  # type: ignore[misc]


# =====================================================================
# 2.5 全局调用预算：封顶整条流水线的 LLM 串行调用次数
#     （规划1 + 生成重试2 + 结构修复2 + ERC修复1，最坏 6~7 次 → 默认封顶 5 次）
# =====================================================================

class BudgetExhausted(RuntimeError):
    """LLM 调用预算耗尽。上层捕获后应返回 best-effort 结果或回落模板。"""


class CallBudget:
    def __init__(self, max_calls: Optional[int] = None):
        # 默认预算取 config.LLM_BUDGET（环境变量 LLM_BUDGET，默认 5）
        self.max_calls = int(max_calls) if max_calls is not None else (
            app_config.LLM_BUDGET if app_config else 5)
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_calls

    def spend(self) -> None:
        if self.exhausted:
            raise BudgetExhausted(f"LLM 调用预算已用尽（上限 {self.max_calls} 次）")
        self.used += 1


# =====================================================================
# 3. 健壮 JSON 提取（清洗 LLM 常见脏输出）
# =====================================================================

# 清洗 LLM 脏输出的 4 条正则，模块加载时编译一次（此前每次调用都走
# re 模块的模式字符串缓存查找，预编译省掉查表开销，也免每调用重编译）
_RE_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_RE_LINE_COMMENT = re.compile(r"//[^\n]*")
_RE_TRAILING_OBJ_COMMA = re.compile(r",\s*}")
_RE_TRAILING_ARR_COMMA = re.compile(r",\s*]")
_RE_ADJACENT_OBJECT_FIELDS = re.compile(
    r'(?P<value>"(?:\\.|[^"\\])*"|true|false|null|-?(?:0|[1-9]\d*)'
    r'(?:\.\d+)?(?:[eE][+-]?\d+)?|[}\]])'
    r'(?P<gap>\s*)(?P<key>"(?:\\.|[^"\\])*"\s*:)' 
)


def _first_json_object(t: str) -> Optional[str]:
    """从文本中截出第一个**完整**的 JSON 对象子串；找不到返回 None。

    为什么不能用 find('{') + rfind('}')：模型常在 JSON 前后带说明文字，
    而说明里又会提到花括号（"其中 {key} 表示位号"）。此时 rfind 会一直
    找到最后一个花括号，把散文一起吃进来，得到 `{"a":1}（其中 {key}`
    这种既非合法 JSON 也非原始文本的脏串——既解析失败又丢失了原始信息。

    改用花括号计数扫描，并跳过字符串字面量内的花括号（否则
    {"note":"含有 } 的字符串"} 会被提前截断）。以第一个 '{' 为起点，
    深度归零处即为该对象的结尾，天然支持"对象后面还有散文"的常见情形。
    """
    start = t.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return t[start:i + 1]
    return None          # 括号不配对（输出被截断）——交给上层重试


def _json_container_stack(text: str, limit: int) -> List[str]:
    """返回 ``text[:limit]`` 扫描后的容器栈，忽略字符串里的括号。"""
    stack: List[str] = []
    in_str = False
    escaped = False
    for ch in text[:limit]:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            stack.append(ch)
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
    return stack


def _repair_adjacent_object_fields(text: str) -> str:
    """仅在对象容器中给相邻字段补逗号，不触碰数组元素或字段语义。

    模型常把 ``"version": 1 "parts": []`` 写成少一个逗号。候选位置必须
    满足“JSON 值后紧接着一个带冒号的字段名”，且值与字段名位于同一对象；
    因此不会凭空增加元件、连接或其它字段。
    """
    matches = list(_RE_ADJACENT_OBJECT_FIELDS.finditer(text))
    inserts: List[int] = []
    for match in matches:
        value = match.group("value")
        # 闭合嵌套对象/数组后，栈已经弹出，父级对象才是待修复容器。
        end = match.end("value")
        stack = _json_container_stack(text, end)
        if value == "}" and stack and stack[-1] == "{":
            pass
        elif value == "]" and stack and stack[-1] == "{":
            pass
        elif not stack or stack[-1] != "{":
            continue
        inserts.append(match.start("key"))
    if not inserts:
        return text
    out = text
    for pos in reversed(inserts):
        out = out[:pos] + "," + out[pos:]
    return out


def _trace_error(exc: BaseException) -> Dict[str, Any]:
    """将解析异常压缩为不含模型原文的安全摘要。"""
    if isinstance(exc, json.JSONDecodeError):
        return {"kind": "json_decode", "message": str(exc.msg)[:120],
                "position": int(exc.pos)}
    return {"kind": type(exc).__name__, "message": str(exc)[:120]}


def _flatten_connection_paths(obj: Any) -> Any:
    """保留历史兼容行为：将连接路径的嵌套片段拍平成点序列。"""
    if isinstance(obj, dict) and isinstance(obj.get("connections"), list):
        for conn in obj["connections"]:
            if isinstance(conn, list) and len(conn) >= 4 and isinstance(conn[3], list):
                flat = []
                for seg in conn[3]:
                    flat.extend(seg) if isinstance(seg, list) else flat.append(seg)
                conn[3] = flat
    return obj


def extract_json_object_with_trace(text: str) -> Tuple[Any, Dict[str, Any]]:
    """解析第一个对象，并返回安全的解析策略轨迹。

    轨迹只包含策略名、结果类型和压缩后的错误摘要，不保存完整模型输出，
    也不读取或记录 API Key。返回值第一项与 ``extract_json_object`` 兼容。
    """
    trace: Dict[str, Any] = {"strategies": [], "errors": []}
    if not isinstance(text, str):
        trace["strategies"].append("non_string")
        trace["result_type"] = type(text).__name__
        return text, trace
    t = text.replace("```json\n", "").replace("```JSON\n", "").replace("```", "")
    trace["strategies"].append("extract_object")
    s = _first_json_object(t)
    if s is None:
        trace["strategies"].append("no_object")
        trace["result_type"] = "str"
        return text, trace
    cleaned = _RE_BLOCK_COMMENT.sub("", s)        # 去块注释
    cleaned = _RE_LINE_COMMENT.sub("", cleaned)   # 去行注释
    cleaned = _RE_TRAILING_OBJ_COMMA.sub("}", cleaned)
    cleaned = _RE_TRAILING_ARR_COMMA.sub("]", cleaned)
    if cleaned != s:
        trace["strategies"].append("cleanup")
    s = cleaned
    try:
        obj = json.loads(s)
        trace["strategies"].append("json_loads")
        trace["result_type"] = type(obj).__name__
        return _flatten_connection_paths(obj), trace
    except json.JSONDecodeError as first_err:
        trace["errors"].append(_trace_error(first_err))
    try:
        obj = ast.literal_eval(s)                  # 兜底：模型偶尔输出单引号
        trace["strategies"].append("literal_eval")
        trace["result_type"] = type(obj).__name__
        return _flatten_connection_paths(obj), trace
    except Exception as literal_err:               # noqa: BLE001
        trace["errors"].append(_trace_error(literal_err))

    repaired = _repair_adjacent_object_fields(s)
    if repaired != s:
        trace["strategies"].append("missing_comma")
        try:
            obj = json.loads(repaired)
            trace["strategies"].append("json_loads_repaired")
            trace["result_type"] = type(obj).__name__
            return _flatten_connection_paths(obj), trace
        except json.JSONDecodeError as repaired_err:
            trace["errors"].append(_trace_error(repaired_err))
        try:
            obj = ast.literal_eval(repaired)
            trace["strategies"].append("literal_eval_repaired")
            trace["result_type"] = type(obj).__name__
            return _flatten_connection_paths(obj), trace
        except Exception as repaired_literal_err:  # noqa: BLE001
            trace["errors"].append(_trace_error(repaired_literal_err))
    trace["strategies"].append("unparsed")
    trace["result_type"] = "str"
    # 与旧接口保持一致：找到对象但仍失败时返回清洗后的片段，供上层重试。
    return repaired if repaired != s else s, trace


def extract_json_object(text: str) -> Any:
    """从模型输出里抠出第一个完整 JSON 对象；失败时返回原字符串便于上层重试。"""
    obj, _trace = extract_json_object_with_trace(text)
    return obj


# 语义更短的别名，供需要显式解析轨迹的调用方使用。
parse_json_with_trace = extract_json_object_with_trace


def extract_json_array(text: str) -> List[str]:
    """抽取 JSON 字符串数组（元件名列表），失败时按行兜底。"""
    if not isinstance(text, str):
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            v = ast.literal_eval(m.group(0))
            if isinstance(v, list):
                return [str(x) for x in v]
        except Exception as parse_err:  # noqa: BLE001 —— 坏 JSON 落行级兜底
            log.debug("元件抽取 JSON 解析失败，按行兜底：%s: %.80r",
                      type(parse_err).__name__, text)
    return [ln.strip("- *\n") for ln in text.splitlines() if ln.strip()]


# =====================================================================
# 4. 结构校验（CircuitJSON 全字段校验，纯标准库实现）
#    只查“形状”，不查电气（电气交给 erc.py）。
# =====================================================================

def validate_circuit_json(circuit: Any, matched_lib: Optional[Dict[str, Any]] = None) -> List[str]:
    """校验 CircuitJSON 结构，返回错误列表（空列表 = 合法）。
    matched_lib 传入时额外做防幻觉校验：元件类型必须是标准目录内的已知类型
    （matched_lib 之外、目录之内的类型同样放行，避免本地识别漏判被误杀），
    引脚必须在白名单内（目录内其余元件以目录引脚表为准）。"""
    errs: List[str] = []
    if not isinstance(circuit, dict):
        return [f"顶层必须是 JSON 对象，实际是 {type(circuit).__name__}"]
    if not isinstance(circuit.get("parts"), list):
        errs.append("缺少 parts 数组")
        return errs
    if not isinstance(circuit.get("connections"), list):
        errs.append("缺少 connections 数组")
        return errs

    ids = []
    for i, p in enumerate(circuit["parts"]):
        if not isinstance(p, dict):
            errs.append(f"parts[{i}] 必须是对象")
            continue
        pid, ptype = p.get("id"), p.get("type")
        if not isinstance(pid, str) or not pid.strip():
            errs.append(f"parts[{i}].id 必须是非空字符串")
            continue
        if not isinstance(ptype, str) or not ptype.strip():
            errs.append(f"{pid}.type 必须是非空字符串")
        ids.append(pid)
        for k in ("top", "left"):
            if k in p and not isinstance(p.get(k), (int, float)):
                errs.append(f"{pid}.{k} 必须是数字")
        if "rotate" in p and p.get("rotate") not in (0, 90, 180, 270):
            errs.append(f"{pid}.rotate 只能是 0/90/180/270")
        if "attrs" in p and not isinstance(p.get("attrs"), dict):
            errs.append(f"{pid}.attrs 必须是对象")
        # 防幻觉校验按标准元件目录的封闭集判定：目录内的元件一律放行
        # （即使本地识别漏判、不在 matched_lib 里），目录外的臆造类型仍拒绝。
        if (matched_lib is not None and isinstance(ptype, str)
                and ptype not in matched_lib and ptype not in CATALOG):
            errs.append(f"{pid} 的元件类型 “{ptype}” 不在标准元件目录中")

    # 重复位号：Counter 一次统计（原先 ids.count(x) 对每个元素都全表重扫，
    # O(n²)；元件多的电路里是平方级浪费）
    dup = sorted(x for x, n in Counter(ids).items() if n > 1)
    if dup:
        errs.append(f"元件 id 重复：{dup}")
    idset = set(ids)

    # id -> type 索引（保留首个同 id 元件的 type，与原先 next() 的取首个语义一致），
    # 连接循环里 O(1) 查表，替代原先每个端点都全量重扫 parts 的 O(P×C)
    id2type: Dict[Any, Any] = {}
    for p in circuit["parts"]:
        if isinstance(p, dict):
            id2type.setdefault(p.get("id"), p.get("type"))

    seen_pairs = set()
    for i, conn in enumerate(circuit["connections"]):
        if not isinstance(conn, (list, tuple)) or len(conn) < 2:
            errs.append(f"connections[{i}] 至少要有 [起点引脚, 终点引脚] 两个元素")
            continue
        refs = []
        for j in (0, 1):
            c = conn[j]
            if not isinstance(c, str) or ":" not in c:
                errs.append(f"connections[{i}] 第 {j+1} 个端点必须是 “元件id:引脚名” 格式的字符串")
                continue
            refs.append(c)
        for ref in refs:
            pid, pin = ref.split(":", 1)
            if pid not in idset:
                errs.append(f"connections[{i}] 引用了不存在的元件 “{pid}”")
            elif matched_lib is not None:
                ptype = id2type.get(pid)
                # matched_lib 内的元件沿用传入白名单；目录内其余元件退回目录引脚表
                if ptype in matched_lib:
                    pins = matched_lib[ptype].get("pins", [])
                else:
                    pins = CATALOG.get(ptype, {}).get("pins", [])
                if pins and pin not in pins:
                    errs.append(f"connections[{i}] 引脚 “{ref}” 不在 {pid}（{ptype}）的白名单 {pins} 内")
        if len(refs) == 2:
            if refs[0] == refs[1]:
                errs.append(f"connections[{i}] 两端是同一个引脚 {refs[0]}")
            key = tuple(sorted(refs))
            if key in seen_pairs:
                errs.append(f"connections[{i}] 与前文连接重复：{list(key)}")
            seen_pairs.add(key)

    connected = set()
    for c in circuit["connections"]:
        if isinstance(c, (list, tuple)):
            for endpoint in c:
                if isinstance(endpoint, str) and ":" in endpoint:
                    connected.add(endpoint.split(":", 1)[0])
    for pid in ids:
        if pid not in connected:
            errs.append(f"元件 {pid} 没有参与任何连接")
    errs.extend(_check_pin_reuse(circuit, matched_lib, id2type))
    errs.extend(_check_power_markers(circuit, id2type))
    return errs


# 单引脚电源标记件类型（只能接一个网络，被复用即矛盾）
_MARKER_TYPES = frozenset({"vcc", "gnd"})


def _pin_reuse_hint(conn_nos: List[int]) -> str:
    """把「同一引脚被多条连接复用」的连接序号拼成人话，便于定位改哪条线。"""
    return "、".join(str(i) for i in conn_nos[:4])


def _check_pin_reuse(circuit: Dict[str, Any], matched_lib: Optional[Dict[str, Any]],
                     id2type: Dict[Any, Any]) -> List[str]:
    """同一【功能引脚】被 3 条以上连接引用 = 连接错误（2026-09-10 新增）。

    为什么单列这条：validate_circuit_json 此前只查「引脚是否存在」「位号是否
    重复」「两端是否自环/重复」，但**不查一个引脚被几条连接引用**。三线汇于
    单引脚是 LLM 的典型连线错误形态——模型把本该「串联/分叉」的节点全部
    汇到一个引脚上。

    ⚠️ 阈值与豁免范围（重要，改前必读）
    ------------------------------------
    * 阈值是 3，不是 2：「同一引脚接 2 条线」是合法分叉（导线从一点分出两路）。
    * 电源/地标记件（vcc/gnd）【整体豁免】，无论被引用多少次都不报。
      这不是放宽标准，而是本项目的既定约定：标记件只有 1 个引脚，其唯一作用
      就是「挂起一整条电源轨/地轨」。实测 data/circuit_templates.json 的 10 套
      内置模板中，NE555 模板的 GND1:1 被引用 5 次、V1:1 被引用 4 次，其余
      模板普遍 2~3 次——这是标准原理图画法（所有需要接地的元件汇总到同一个
      GND 符号），若纳入本规则会把 4/10 套模板判为非法。
      带多引脚的芯片、含子部件的连接器同理在各自引脚上分叉，也不受影响。

    因此本规则只针对【单一功能引脚被三线以上汇接】这一种在任何拓扑下都
    说不通的形态。"""
    errs: List[str] = []
    ref_counts: Dict[str, List[int]] = {}
    for idx, conn in enumerate(circuit.get("connections", [])):
        if not isinstance(conn, (list, tuple)):
            continue
        for j in (0, 1):
            if j >= len(conn):
                continue
            c = conn[j]
            if not isinstance(c, str) or ":" not in c:
                continue
            ref_counts.setdefault(c, []).append(idx + 1)

    for ref, conn_nos in ref_counts.items():
        if len(conn_nos) < 3:
            continue
        pid = ref.split(":", 1)[0]
        # 电源/地标记件豁免：它们的唯一引脚就是用来汇总整条轨的（见 docstring）
        if id2type.get(pid) in _MARKER_TYPES:
            continue
        errs.append(
            f"引脚 {ref} 被 {len(conn_nos)} 条连接引用（第 "
            f"{_pin_reuse_hint(conn_nos)} 条）：一个引脚最多只应接 2 条线；"
            f"如需在多个元件间共用一个节点，请把线接成「串联链」"
            f"（A→B→C）而不是三条线全部汇到同一个引脚上")
    return errs


def _check_power_markers(circuit: Dict[str, Any],
                         id2type: Dict[Any, Any]) -> List[str]:
    """电源/地标记件必须真的接出去（2026-09-10 新增）。

    标记件（vcc/gnd）的语义是「给所在网络起名」，本身不耗电也不导电。
    一个完全没接线的 gnd 标记件不等于「电路接地了」——它是空符号，
    画出来会让人误以为已共地，而 ERC 的 check_no_ground 只看网络里有没有
    GND 引脚，悬空标记件不构成网络，于是「未共地」会照常报出（这条是有
    兜底的）。但**悬空标记件本身**属于结构性冗余，应在结构阶段就提示，
    让模型把它删掉或接上，而不是留一个看不懂的空符号在图上。
    """
    errs: List[str] = []
    wired: set = set()
    for conn in circuit.get("connections", []):
        if isinstance(conn, (list, tuple)):
            for endpoint in conn[:2]:
                if isinstance(endpoint, str) and ":" in endpoint:
                    wired.add(endpoint.split(":", 1)[0])
    for p in circuit.get("parts", []):
        if not isinstance(p, dict):
            continue
        pid, ptype = p.get("id"), p.get("type")
        if ptype in _MARKER_TYPES and pid not in wired:
            errs.append(
                f"电源标记件 {pid}（{ptype}）没有连接任何网络："
                f"标记件必须接在电路上才有意义，否则只是一个空符号。"
                f"请把它接到对应网络，或直接删除该标记件")
    return errs


# =====================================================================
# 4.5 元件抽取（本地短路优化）
#     本地 match_components 实测 ~0.15ms、30 用例 30/30 全对 —— 命中即免掉
#     第一次 LLM 往返（省 5~10s）；本地无命中（罕见措辞/新元件）才问 LLM。
# =====================================================================

def extract_components(user_prompt: str, client: Optional[DeepSeekClient] = None,
                       budget: Optional[CallBudget] = None,
                       on_progress: Optional[Callable[[str, str], None]] = None,
                       cancel_event: Optional[threading.Event] = None
                       ) -> Tuple[List[str], str]:
    """抽取需求涉及的元件类型。返回 (types, source)，
    source ∈ {'local'（本地命中，零 LLM 调用）, 'llm'（LLM 抽取后本地匹配）, 'none'}。
    cancel_event 由上层（SSE 断开止损）透传：客户端已断开时不再发起 LLM 抽取。

    无论走本地还是 LLM 路径，都把「需求词 → 命中类型（得分、
    次优候选）」映射写入日志，让匹配可审计；多候选接近阈值（歧义）时告警——
    歧义的澄清交互由 clarify 链路负责（catalog.match_one_detail 与
    clarify.rule_analyze 共用同一份分数）。"""
    from core.catalog import match_components, match_one_detail

    local = match_components(user_prompt)
    if local:
        detail = match_one_detail(user_prompt)
        log.info("元件匹配映射（本地）：%r -> %s（best=%s score=%.2f runner=%s %.2f）",
                 user_prompt[:60], local, detail["type"], detail["score"],
                 detail["runner_up"], detail["runner_up_score"])
        if detail["ambiguous"]:
            log.warning("元件匹配存在多候选歧义：%r 同时接近 %s(%s) 与 %s(%s)，"
                        "已按高分取 %s——如与意图不符请在需求中写明具体元件名",
                        user_prompt[:60], detail["type"], round(detail["score"], 2),
                        detail["runner_up"], round(detail["runner_up_score"], 2),
                        detail["type"])
        return local, "local"
    if client is None or (budget is not None and budget.exhausted):
        return [], "none"
    # 取消优先于记账：本路径直连 client.chat()（不走 call() 包装），
    # 若不在此前置检查，已取消的请求仍会先 budget.spend() 扣一次额度，
    # 再依赖 client 内部抛 OperationCancelled 才发现——白扣预算。
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled(
            "LLM 调用已被取消（客户端断开或上层中止），不再发起新请求")
    if budget is not None:
        budget.spend()
    if on_progress:
        on_progress("extract", "本地未命中，LLM 抽取元件中")
    raw = client.chat(EXTRACT_PROMPT.format(user_prompt=user_prompt), user_prompt,
                      json_mode=True, cancel_event=cancel_event)
    matched: List[str] = []
    for name in extract_json_array(raw):
        d = match_one_detail(name)
        t = d["type"]
        if t and t not in matched:
            matched.append(t)
        if d["ambiguous"]:
            log.warning("LLM 抽取词 %r 匹配歧义：%s(%s) ≈ %s(%s)，取 %s",
                        name, d["type"], round(d["score"], 2),
                        d["runner_up"], round(d["runner_up_score"], 2), t)
        elif t:
            log.info("元件匹配映射（LLM 抽取）：%r -> %s (score=%.2f)",
                     name, t, d["score"])
    return matched, "llm"


def build_generation_brief(user_prompt: str,
                           matched_lib: Dict[str, Any]) -> Dict[str, Any]:
    """确定性预规划：把词面证据、总线网络和目录缺口交给生成阶段。

    此记录独立于 CircuitJSON，可用于逐例审计；suggested_types 是工程建议，
    不会被计为需求命中，也不代表已核对具体封装或焊盘。
    """
    from core.catalog import (BASE_ALLOWED_TYPES, retrieve_candidates,
                              unsupported_part_numbers)

    candidates = retrieve_candidates(user_prompt)
    explicit = list(dict.fromkeys(row["type"] for row in candidates))
    selected = [t for t in matched_lib if t in CATALOG]
    suggested = []
    checks = []
    q = user_prompt.lower()
    if re.search(r"i\s*2\s*c|iic", q):
        checks.extend(["SDA 与 SCL 各自同网且分别上拉", "总线电压与主从器件兼容",
                       "同一条总线每根信号线只需一组上拉"])
        suggested.extend(["resistor", "capacitor"])
    if "ldo_3v3" in explicit:
        checks.append("LDO 输入和输出分别按器件手册配置电容")
        suggested.append("capacitor")
    if "led" in explicit:
        checks.append("LED 电流路径串联限流电阻")
        suggested.append("resistor")
    for t in selected:
        if t not in explicit and t not in suggested and t not in BASE_ALLOWED_TYPES:
            suggested.append(t)
    bus_pins = {
        "atmega328": {"SDA": "PC4", "SCL": "PC5"},
        "esp32": {"SDA": "GPIO21", "SCL": "GPIO22"},
        "bmp280": {"SDA": "SDA", "SCL": "SCL"},
        "mpu6050": {"SDA": "SDA", "SCL": "SCL"},
        "oled_ssd1306": {"SDA": "SDA", "SCL": "SCL"},
        "i2c_header": {"SDA": "SDA", "SCL": "SCL"},
    }
    nets = {}
    if re.search(r"i\s*2\s*c|iic", q):
        for net in ("SDA", "SCL"):
            nets[net] = [{"type": t, "pin": bus_pins[t][net]}
                         for t in explicit if t in bus_pins]
    return {
        "stage": "catalog_preflight", "explicit_candidates": candidates,
        "required_types": explicit, "suggested_types": suggested,
        "signal_nets": nets, "checks": checks,
        "unsupported_part_numbers": unsupported_part_numbers(user_prompt),
        "blockers": detect_requirement_blockers(user_prompt),
    }


# =====================================================================
# 5. 串起完整生成流程（模板兜底 -> 规划 -> 生成 -> 结构校验 -> ERC 回灌修复）
# =====================================================================

def generate_circuit_json(user_prompt: str, matched_lib: Dict[str, Any],
                          client: DeepSeekClient, max_retry: int = 2,
                          with_plan: bool = True, erc_kb: Optional[Dict] = None,
                          erc_repair: bool = True,
                          budget: Optional[CallBudget] = None,
                          on_progress: Optional[Callable[[str, str], None]] = None,
                          cancel_event: Optional[threading.Event] = None
                          ) -> Dict[str, Any]:
    """
    user_prompt : 用户中文需求
    matched_lib : 本地元件库中本次命中的元件，结构 {type: {pins:[...], ...}}
    client      : DeepSeekClient
    max_retry   : 结构非法/误返回数组时的额外重试次数（默认 2，即最多 3 次
                  生成/修复尝试，对“返回数组”这类高发故障留足重试余地。
                  服务端经 config.LLM_STRUCT_RETRIES 注入，可环境变量调整；
                  总调用始终受 budget 封顶，不会因调大重试而失控）
    erc_kb      : {type: {pin: 电气类型}}，由 catalog.build_erc_kb() 生成；
                  传入后启用“ERC 错误回灌修复”闭环（fatal/major 才触发修复）
    budget      : CallBudget 全局调用预算（默认新建，上限 5 次）。
                  规划+生成+结构修复+ERC 修复全串起来也封顶，超限即 best-effort：
                  已有合法电路则带错误返回，没有则抛 ValueError 让上层回落模板。
    on_progress : 进度回调 (stage, detail)，stage ∈
                  plan/generate/repair_struct/repair_erc/done —— 供 SSE 实时推送。
    cancel_event: 外部取消信号（SSE 断开止损）。透传给内部每次 client.chat()，
                  置位后下一次调用前抛 OperationCancelled 向上传播。
    返回合法 CircuitJSON dict。
    明确需求矛盾会在任何模型调用前抛 IntentConstraintError；调用者必须
    将该拒绝返回用户，不能继续使用模板兜底。
    """
    brief = build_generation_brief(user_prompt, matched_lib)
    if brief["blockers"]:
        raise IntentConstraintError(brief["blockers"])
    if brief["unsupported_part_numbers"]:
        raise ValueError("需求包含目录外器件型号，需先核对型号、引脚与符号库："
                         + ", ".join(brief["unsupported_part_numbers"]))
    budget = budget if budget is not None else CallBudget()
    brief_text = json.dumps(brief, ensure_ascii=False, sort_keys=True)

    def report(stage: str, detail: str = "") -> None:
        if on_progress:
            on_progress(stage, detail)

    def call(system_prompt: str, user_text: str, stage: str, detail: str = "",
             json_mode: bool = True, temperature: float = 0.0) -> str:
        """统一 LLM 调用入口：预算计数 + 进度上报 + JSON 模式 / 取消信号透传。
        生成/修复走 JSON mode（结构化输出接口层强约束）；
        规划保持纯文本（思维链不宜被 JSON 格式束缚）。
        temperature：默认 0 保证可复现（答辩稳定）；重试轮次可传 0.3 加温
        ——同一 prompt 零温重试往往精确复现同一错误，微升温帮模型跳出
        局部失败。

        取消优先于记账：cancel_event 已置位时先抛 OperationCancelled，不再
        消耗预算。此前先 budget.spend() 后由 client.chat() 内部检查取消，
        等于「已取消的请求仍扣掉 1 次额度」——SSE 断开止损场景下会把本可
        省下的预算白扣，取消得越晚越亏。"""
        # 取消信号前置检查：已取消则直接上抛，不占用预算、不发请求。
        # client.chat() 内部仍会再查一次（防检查后到发请求前的竞态窗口）。
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled(
                "LLM 调用已被取消（客户端断开或上层中止），不再发起新请求")
        budget.spend()               # 预算耗尽抛 BudgetExhausted，由各调用点决定策略
        report(stage, detail)
        return client.chat(system_prompt, user_text, temperature=temperature,
                           json_mode=json_mode, cancel_event=cancel_event)

    # 引脚白名单注入完整标准目录而非仅本次命中元件：本地识别偶尔漏判措辞时
    # （如「三极管开关」只命中「开关」），LLM 仍能按目录正确补上漏判元件，
    # 识别缺口不再被窄白名单放大成结构错误→兜底。与 validate_circuit_json
    # 的目录级防幻觉放行配套；matched_lib 继续用于规划聚焦与进度展示，接口不变。
    pin_table = ({t: {"pins": e.get("pins", [])} for t, e in CATALOG.items()}
                 if CATALOG else
                 {t: {"pins": info.get("pins", [])} for t, info in matched_lib.items()})
    comp_pins = json.dumps(pin_table, ensure_ascii=False, indent=2)

    advice = ""
    if with_plan:
        try:
            avail = "\n".join(f"{t}: 引脚 {v.get('pins', [])}" for t, v in matched_lib.items())
            avail += "\n确定性预规划（只作为核查清单，未核对物理焊盘）：" + brief_text
            advice = call(PLAN_PROMPT.format(available_components=avail), user_prompt,
                          "plan", "电气规划中（思维链）", json_mode=False)
        except OperationCancelled:
            raise                    # 取消必须传播：客户端已断开，继续生成纯属烧钱
        except BudgetExhausted:
            advice = ""               # 规划只是建议，预算紧张时跳过不致命
        except Exception as plan_err:  # noqa: BLE001 —— 网络最终失败等：跳过不致命
            # 规划失败不再炸整条流水线——此前 PLAN 一步网络失败会把
            # 整次请求打回模板兜底；现在降级为“无规划直接生成”，生成主路径
            # 仍有引脚白名单与 few-shot 兜着。
            log.warning("电气规划失败，跳过规划直接生成：%s: %s",
                        type(plan_err).__name__, plan_err)
            advice = ""

    gen_sys = GEN_PROMPT.format(component_pins=comp_pins,
                                design_advice=(advice or "（无）") +
                                "\n确定性预规划：" + brief_text)

    circuit = None
    for attempt in range(max_retry + 1):
        # 重试轮次加温 + 注入“上次无法解析”的明确提示。
        # 零温重试在确定性解码下常常逐字复现上一次的坏输出；升温 0.3 与
        # 明确指令双管齐下，让重试真正产生“不同的下一次”。
        retry_hint = ("" if attempt == 0 else
                      "\n\n注意：你上一次的回复无法解析出 CircuitJSON 对象"
                      "（可能是夹带了 markdown/解释文字，或输出被截断）。"
                      "这次请直接输出一个完整、合法的 CircuitJSON 对象。")
        try:
            raw = call(gen_sys, f"用户需求：{user_prompt}{retry_hint}", "generate",
                       "结构化生成中" if attempt == 0 else "结构非法，重新生成中",
                       temperature=0.0 if attempt == 0 else 0.3)
        except BudgetExhausted:
            raise ValueError("LLM 调用预算耗尽且尚无合法结果，请回落到模板兜底")
        circuit = extract_json_object(raw)
        if isinstance(circuit, dict) and "parts" in circuit and "connections" in circuit:
            break
        circuit = None
    if circuit is None:
        # 全部失败交给上层；仅匹配且覆盖需求的模板可回退，其余明确拒绝。
        raise ValueError("LLM 多次返回非法结构，请回落到模板兜底")

    # 结构校验：字段/引脚白名单（全字段校验）
    struct_errs = validate_circuit_json(circuit, matched_lib)
    if struct_errs:
        for attempt in range(max_retry + 1):
            try:
                # 结构修复专用模板：明确“你上次的输出格式/结构有误”，逐条列出错误
                # （重试轮次加温，理由同生成循环）
                fix_sys = STRUCT_REPAIR_PROMPT.format(
                    struct_errors="\n".join(f"- {e}" for e in struct_errs),
                    component_pins=comp_pins, user_prompt=user_prompt)
                raw = call(fix_sys, f"你上一次的输出：\n{json.dumps(circuit, ensure_ascii=False)}",
                           "repair_struct", f"结构修复中（{len(struct_errs)} 处问题）",
                           temperature=0.0 if attempt == 0 else 0.3)
            except BudgetExhausted:
                raise ValueError(f"预算耗尽仍未通过结构校验：{struct_errs[:3]}，请回落到模板兜底")
            fixed = extract_json_object(raw)
            if isinstance(fixed, dict) and "parts" in fixed and "connections" in fixed \
                    and not validate_circuit_json(fixed, matched_lib):
                circuit = fixed
                break
            struct_errs = validate_circuit_json(fixed, matched_lib) if isinstance(fixed, dict) else ["非对象"]
            circuit = fixed if isinstance(fixed, dict) else circuit
        else:
            raise ValueError(f"LLM 输出始终无法通过结构校验：{struct_errs[:3]}")

    # 电气校验 + ERC 错误回灌修复（生成→校验→回灌修复的闭环）
    # 修复从“单发一枪”升级为多轮循环（config.LLM_ERC_RETRIES，默认 1）
    # ——每轮基于【最新电路】重新跑 ERC、重新回灌错误清单与引脚→网络上下文，
    # 上一轮已修复的问题不再出现，修复方向可逐轮收敛；
    # 问题数不减即停在较优版本，总调用始终受 CallBudget 封顶，不会失控。
    if erc_kb and erc_repair:
        from core.erc import run_erc, CircuitGraph  # 同目录模块，延迟导入避免硬耦合
        _erc_rounds = int(getattr(app_config, "LLM_ERC_RETRIES", 1) if app_config else 1)
        rep = run_erc(circuit, erc_kb)
        for attempt in range(max(0, _erc_rounds) + 1):
            if rep.passed:
                break
            try:
                # ERC 修复专用模板：明确“结构合法但有电气错误”，注入规划上下文
                # design_advice（修复阶段不丢失设计意图）与引脚→网络上下文
                fix_sys = ERC_REPAIR_PROMPT.format(
                    erc_errors=rep.to_repair_prompt(CircuitGraph(circuit, erc_kb)),
                    design_advice=advice or "（无）",
                    component_pins=comp_pins, user_prompt=user_prompt)
                raw = call(fix_sys,
                           f"你上一次的输出：\n{json.dumps(circuit, ensure_ascii=False)}",
                           "repair_erc",
                           f"ERC 修复中（第 {attempt + 1} 轮，{len(rep.issues)} 处电气问题）",
                           temperature=0.0 if attempt == 0 else 0.3)
            except OperationCancelled:
                raise                    # 客户端已断开：止损向上传播，不再返回结果
            except BudgetExhausted:
                # 已有结构合法的电路：best-effort 返回，ERC 错误照常标注在图上
                report("done", f"预算耗尽，返回带 {len(rep.issues)} 处 ERC 标注的结果")
                return circuit
            fixed = extract_json_object(raw)
            if isinstance(fixed, dict) and not validate_circuit_json(fixed, matched_lib):
                rep2 = run_erc(fixed, erc_kb)
                if rep2.passed:
                    report("done", "完成（ERC 修复成功）")
                    return fixed
                # 本轮毫无改善甚至变差：停在问题较少的版本，不再空烧预算续轮
                if len(rep2.issues) >= len(rep.issues):
                    report("done", "完成（best-effort）")
                    return fixed if len(rep2.issues) < len(rep.issues) else circuit
                # 有实质改善：以修复版为起点进入下一轮继续收敛
                circuit, rep = fixed, rep2
            # 修复输出结构非法：不更新电路，下一轮按同一错误清单重试
        if rep.passed:
            report("done", "完成")
            return circuit
        report("done", f"完成（best-effort，仍有 {len(rep.issues)} 处 ERC 标注）")
        return circuit
    report("done", "完成")
    return circuit


if __name__ == "__main__":
    # 简单自测：不联网，只验证 JSON 清洗与结构校验逻辑
    demo = '''好的，这是结果：
    ```json
    {
      "version": 1,
      "parts": [{"type":"resistor","id":"R1","top":0,"left":0,"attrs":{"value":"1k"},}],
      "connections": [["R1:1","R1:2","red",[]]], // 尾逗号和注释都要被清掉
    }
    ```'''
    print(json.dumps(extract_json_object(demo), ensure_ascii=False, indent=2))
    print(extract_json_array('用到的元件：["led","resistor","battery"]'))

    print("\n== 结构校验：正常电路（应无错误） ==")
    good = {"version": 1,
            "parts": [{"type": "battery", "id": "BAT1"},
                      {"type": "resistor", "id": "R1"},
                      {"type": "led", "id": "LED1"}],
            "connections": [["BAT1:+", "R1:1", "red", []],
                            ["R1:2", "LED1:+", "red", []],
                            ["LED1:-", "BAT1:-", "black", []]]}
    lib = {"battery": {"pins": ["+", "-"]}, "resistor": {"pins": ["1", "2"]}, "led": {"pins": ["+", "-"]}}
    print(validate_circuit_json(good, lib))

    print("\n== 结构校验：坏电路（应列出多条错误） ==")
    bad = {"version": 1,
           "parts": [{"type": "resistor", "id": "R1"},
                     {"type": "led", "id": "R1"},          # 重复 id
                     {"type": "neo", "id": "N1"}],          # 未知类型
           "connections": [["R1:9", "LED1:+"],               # 白名单外引脚 + 不存在元件
                            ["R1:1", "R1:1"],                # 自环
                            ["R1:1", "R1:2", "red", []],
                            ["R1:1", "R1:2", "red", []]]}    # 重复连接
    for e in validate_circuit_json(bad, lib):
        print(" -", e)

    print("\n== CallBudget 调用预算 ==")
    b = CallBudget(2)
    b.spend(); b.spend()
    print("  exhausted =", b.exhausted, "| remaining =", b.remaining)
    try:
        b.spend()
    except BudgetExhausted as e:
        print("  超限正确拦截 [OK] ", e)

    print("\n== Prompt 模板自检（新 few-shot 与拆分后的修复模板） ==")
    # format() 本身就是校验：占位符写错 / 大括号忘转义都会直接抛 KeyError
    _g = GEN_PROMPT.format(component_pins='{"resistor": {"pins": ["1","2"]}}',
                           design_advice="（无）")
    _s = STRUCT_REPAIR_PROMPT.format(struct_errors="测试错误",
                                     component_pins="{}", user_prompt="测试需求")
    _e = ERC_REPAIR_PROMPT.format(erc_errors="测试错误", design_advice="测试规划",
                                  component_pins="{}", user_prompt="测试需求")
    print("  GEN_PROMPT 含反相放大器正例 few-shot：", "opamp_inverting" in _g)
    print("  GEN_PROMPT 含编造引脚负例警示：", "错误示范" in _g and "错误原因" in _g)
    print("  GEN_PROMPT 含三引脚元件正例（电位器）：", "potentiometer" in _g and "RV1:3" in _g)
    print("  GEN_PROMPT 含端点格式强约束：", "元件id:引脚名" in _g)
    print("  STRUCT_REPAIR_PROMPT 聚焦结构错误：", "结构校验" in _s)
    print("  ERC_REPAIR_PROMPT 注入设计意图：", "设计意图" in _e and "测试规划" in _e)
    print("  三套模板 format 全部通过 [OK]")

    print("\n== extract_components 本地短路（不联网） ==")
    for q in ["用555定时器做一个LED闪烁灯，带按键和电容", "画一个反相比例放大器", "随便画点什么"]:
        types, source = extract_components(q)   # client=None：本地无命中即返回 none
        print(f"  “{q}” -> {types} （来源 {source}）")
