"""Conservative text preflight for explicit low-voltage design contradictions.

These rules inspect requested actions, not a netlist or physical circuit. An empty
result never certifies electrical correctness. Isolation, level conversion, current
drivers and addressed broadcasts have valid uses, so explicit exceptions are kept.

Primary references checked for the contract:
https://www.nxp.com/docs/en/user-guide/UM10204.pdf (separate SDA/SCL, addressing)
https://documentation.espressif.com/esp32-wroom-32_datasheet_en.html (EN active high)
https://www.ti.com/lit/ds/symlink/lm1117.pdf (LM1117 output capacitor)
"""
from __future__ import annotations

import re


class IntentConstraintError(ValueError):
    """A terminal requirement rejection; callers must not use template fallback."""

    def __init__(self, blockers: list[dict]):
        self.blockers = blockers
        super().__init__("需求存在明确矛盾，需先修正：" +
                         "；".join(item["message"] for item in blockers))


_I2C = re.compile(r"i\s*2\s*c|iic", re.I)
_SIGNAL = r"(?:SDA|SCL|GPIO\d+|PC\d+)"
_GROUND = r"(?:GND|公共地|地)"
_SUPPLY = r"(?:\d+(?:\.\d+)?\s*V(?:电源)?|VCC|电源轨|电源)"
_NEGATED_ACTION = re.compile(r"(?:不要|禁止|严禁|不得|不能|不应|避免|不允许|不让|没有|未)[^，。；;,]{0,24}$", re.I)
_FEATURE_NEGATION = re.compile(r"(?:不加|不使用|不用|无|没有|禁止|不要|不采用)[^，。；;,]{0,8}$", re.I)


def _positive_feature(query: str, pattern: str) -> bool:
    for match in re.finditer(pattern, query, re.I):
        if not _FEATURE_NEGATION.search(query[max(0, match.start() - 14):match.start()]):
            return True
    return False


def _affirmative(text: str, action: str) -> bool:
    """Check that a harmful action is requested rather than explicitly forbidden."""
    for match in re.finditer(action, text, re.I):
        if not _NEGATED_ACTION.search(text[max(0, match.start() - 32):match.start()]):
            return True
    return False


def _requested_omission(text: str, component: str) -> bool:
    # "禁止删除输出电容" asks us to keep it; "禁止添加输出电容" asks
    # us to omit it. Do not treat the first prohibition as an omission.
    action = (r"(?:(?:不允许|禁止)(?!.{0,4}(?:删除|去掉|移除|省略))|"
              r"不加|不串|无需|不用|省略|去掉|删除|移除).{0,12}" + component)
    return _affirmative(text, action)


def detect_requirement_blockers(query: str) -> list[dict]:
    """Return structured blockers for explicit contradictions in supported scope.

    Fields: rule_id, severity='error', message, evidence, span=[start,end],
    source='explicit_requirement_rule'. Rule matching is intentionally bounded and
    not a universal language or electrical validator. The supported 1117 plan
    contract includes output capacitance; capacitor-free regulator families are
    not rejected merely for omitting an output capacitor.
    """
    if not isinstance(query, str) or not query.strip():
        return []
    blockers = []
    seen = set()
    i2c = bool(_I2C.search(query))
    has_isolation = _positive_feature(query, r"(?:数字)?隔离器|隔离芯片|光耦")
    has_level_conversion = _positive_feature(query, r"电平转换(?:器)?|电平移位(?:器)?|双向转换器")
    has_mux = _positive_feature(query, r"多路复用器|总线复用器|I2C\s*mux")
    has_current_driver = _positive_feature(query, r"恒流(?:驱动|源)|电流调节驱动器")
    ldo = bool(re.search(r"LDO|(?:AMS|LM)1117|稳压器", query, re.I))
    protection = bool(re.search(r"TVS|ESD|保护阵列", query, re.I))

    def add(rule_id: str, message: str, text: str, start: int):
        if rule_id not in seen:
            blockers.append({"rule_id":rule_id, "severity":"error", "message":message,
                             "evidence":text, "span":[start, start + len(text)],
                             "source":"explicit_requirement_rule"})
            seen.add(rule_id)

    for clause in re.finditer(r"[^，。；;,\n]+", query):
        text = clause.group().strip()
        start = clause.start() + len(clause.group()) - len(clause.group().lstrip())
        if not text:
            continue
        if i2c and re.search(r"SDA.{0,18}SCL|SCL.{0,18}SDA", text, re.I) and _affirmative(
                text, r"短接|短路|(?:合并|连成).{0,8}一条(?:线|导线)"):
            add("SDA_SCL_SHORT", "SDA 和 SCL 必须保留为不同信号网络，不能按要求短接。", text, start)

        signal_must_switch = bool(re.search(r"SDA|SCL", text, re.I) or
                                  re.search(r"(?:GPIO|PC)\d+.{0,18}(?:切换|通信|输出脉冲|输出时钟)", query, re.I))
        if signal_must_switch and re.search(_SIGNAL + r".{0,24}(?:直接(?:接|焊)|短接|固定(?:接|到)|永久(?:接|到)).{0,12}" + _SUPPLY, text, re.I):
            if _affirmative(text, r"直接(?:接|焊)|短接|固定(?:接|到)|永久(?:接|到)") and not re.search(r"上拉|(?:经|通过).{0,12}(?:电阻|恒流)", text):
                add("SIGNAL_TIED_TO_SUPPLY", "信号脚被要求固定到电源，无法保持正常信号切换；请明确驱动或上拉路径。", text, start)

        no_common_ground = r"(?:禁止|不允许|不要).{0,10}(?:接|连接).{0,8}公共地|不共地|无共地|各自隔离地|地.{0,8}(?:彼此|互相)不连接"
        if i2c and not has_isolation and not re.search(r"阵列|TVS|ESD", text, re.I) and _affirmative(text, no_common_ground):
            add("NO_COMMON_GROUND", "I2C 两侧被要求不共地且未声明有效隔离器，需明确公共参考地或隔离方案。", text, start)

        if i2c and not has_mux and re.search(r"地址.{0,12}(?:都|均).{0,10}(?:0x[0-9a-f]+|相同)|地址.{0,5}(?:相同|一致)", text, re.I):
            independent_read = bool(re.search(r"分别(?:读取|读|访问)|单独(?:读取|访问)|独立(?:读取|访问)", query))
            if independent_read and _affirmative(text, r"(?:都|均).{0,10}(?:0x[0-9a-f]+|相同)|相同|一致"):
                add("I2C_ADDRESS_COLLISION", "同一总线的相同地址目标不能按要求独立访问；需不同地址或选通方案。", text, start)

        if i2c and re.search(r"3\s*\.\s*3\s*V|(?:AMS|LM)1117\s*-\s*3\.3", query, re.I) and not has_level_conversion and re.search(
                r"上拉(?:电阻)?.{0,32}(?:接|用|到).{0,12}5\s*V", text, re.I):
            if _affirmative(text, r"(?:接|用|到).{0,12}5\s*V"):
                add("I2C_PULLUP_WRONG_DOMAIN", "3.3V 总线被要求上拉到 5V 且无电平转换说明，需先核对输入容限并修正供电域。", text, start)

        if re.search(r"(?:LED|发光二极管)", query, re.I) and not has_current_driver and _requested_omission(text, "限流电阻"):
            add("LED_CURRENT_LIMIT_OMITTED", "LED 被要求取消限流且未声明恒流驱动，需补充有效限流路径。", text, start)

        output_cap_required = bool(re.search(r"(?:AMS|LM)1117", query, re.I) or
                                   re.search(r"(?:必须|必需|要求).{0,10}输出电容", query))
        if ldo and output_cap_required and _requested_omission(text, "输出电容"):
            add("LDO_OUTPUT_CAP_OMITTED", "当前稳压方案要求输出电容，不能同时要求删除并禁止补回。", text, start)

        shorted_output = (re.search(r"OUT.{0,12}GND|GND.{0,12}OUT", text, re.I) and
                          _affirmative(text, r"短接|短路"))
        grounded_output = (re.search(r"(?:LDO.{0,8})?(?:OUT|输出).{0,12}(?:短接|直接接).{0,4}(?:地|GND)", text, re.I) and
                           _affirmative(text, r"短接|直接接"))
        if ldo and (shorted_output or grounded_output):
            add("REGULATED_SUPPLY_SHORT", "稳压器正输出被要求短接到地，需修正输出电源网络。", text, start)

        active_controller_required = _positive_feature(query, r"保持运行|主控运行|正常运行|可靠通信|仍要.{0,4}运行|仍需.{0,4}运行")
        if active_controller_required and re.search(r"ESP32", query, re.I) and re.search(r"(?<![A-Za-z0-9])EN.{0,8}(?:永久|始终|一直).{0,8}(?:接|短接).{0,6}(?:GND|地)", text, re.I):
            if _affirmative(text, r"(?:接|短接)"):
                add("MCU_ENABLE_HELD_INACTIVE", "ESP32 EN 为高有效，永久接地与主控保持运行的要求矛盾。", text, start)

        ground_floating = bool(re.search(_GROUND + r".{0,8}(?:悬空|不连接)|(?:禁止|不允许).{0,8}(?:接|连接).{0,6}(?:公共地|GND)", text, re.I))
        if ground_floating and _affirmative(text, r"悬空|不连接|(?:禁止|不允许).{0,8}(?:接|连接).{0,6}(?:公共地|GND)"):
            if protection and re.search(r"阵列|保护|TVS|ESD", text, re.I):
                add("PROTECTION_GROUND_OMITTED", "保护器件的地端被要求悬空，需明确有效泄放回路。", text, start)
            elif i2c and not has_isolation and re.search(r"排针|接口|外部口", query):
                add("EXTERNAL_GROUND_OMITTED", "外部 I2C 接口的参考地被要求悬空，需接公共地或提供明确隔离方案。", text, start)

        if protection and re.search(r"(?:阵列|TVS|ESD)?.{0,6}GND.{0,14}(?:接|短接).{0,8}" + _SUPPLY, text, re.I):
            if _affirmative(text, r"(?:接|短接)"):
                add("PROTECTION_GROUND_WRONG", "保护器件 GND 被要求接到电源，需核对保护端口并修正泄放网络。", text, start)

        if re.search(r"不存在.{0,12}(?:目录|引脚)|目录外.{0,8}引脚", text) and _affirmative(text, r"必须|强制|要求"):
            add("UNSUPPORTED_LOGICAL_PIN", "需求强制使用不存在的逻辑引脚，需先补齐器件引脚合同或改用已支持引脚。", text, start)

        if i2c and re.search(r"多主|两个主控|两只主控|双主", query) and re.search(r"推挽.{0,12}(?:强制相连|直接相连|短接)", text) and _affirmative(text, r"相连|短接"):
            add("I2C_PUSH_PULL_CONTENTION", "多主 I2C 不能要求互连推挽驱动；需有效开漏和仲裁方案。", text, start)
    return blockers
