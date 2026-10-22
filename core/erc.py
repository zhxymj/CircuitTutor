# -*- coding: utf-8 -*-
"""
erc.py —— 智绘电路的确定性电气规则校验（ERC，零第三方依赖）
================================================================
核心思路：建“引脚-网络”连通视图，区分“导线(0Ω)”和
“电阻等无源元件(有阻值)”。本文件用 Python 标准库【并查集 DSU + 压缩网络】
实现，不装 networkx，契合智绘电路“解压即跑、零重依赖”。

2026-09-07 规则库升级（检查器对齐商业 EDA 惯例）：
 * 新增 check_duplicate_ids      —— 重复位号 fatal（商业 EDA 均为首查项）
 * 新增 check_no_ground          —— 有源电路未共地 major
 * 新增 check_unconnected_part   —— 元件声明了却一个引脚都没接 minor
 * 新增 check_inductive_flyback  —— 感性负载缺续流二极管 warning（感性保护惯例）
 * 新增 check_decoupling         —— 芯片供电脚缺去耦电容 warning（去耦设计惯例）
 * 新增网络命名 N1/N2...         —— 报错信息可定位到网络，教学讲解更友好
 * 统一 _conn_refs()：所有检查器只取每条连接的前两个引脚引用，修复原先
   check_connection_endpoints 与其他检查器行为不一致的隐患
 * to_repair_prompt()：把 ERC 结果序列化成 LLM 修复指令（错误回灌闭环）

二轮修复（检查器 13 -> 15 条）：
 * 新增 check_polarity          —— LED/蜂鸣器/电解电容极性反接 major、
                                    二极管正向直跨电源 major。此前 LED 反接会被
                                    check_short_circuit 误报成 fatal“电源短路”
                                    （根因是元件目录把 LED 的 “-” 脚记成了 GND，
                                    已在 catalog.py 一并修正为 PASSIVE）
 * 新增 check_passive_values     —— 参数合理性：parse_value 解析 “330Ω/10k/1kΩ/
                                    100nF/10μF” 等工程写法；与 LED 同网的限流电阻
                                    <100Ω 报 warning、0Ω 电阻跨接电源两端报 fatal
 * check_led_current_limit 改为纯“网络构成”判断：+ 脚网络里有 POWER/OUT 电源源头、
   - 脚网络里有 GND 才报“LED缺限流电阻”；中间串了无源元件网络即被隔开，不会误报

核心概念
--------
1. 导线网络(net)：同一条 connection 里的若干引脚被导线短接，电位完全相等，
   用并查集把它们并到同一个集合。
2. 电阻/电容等无源元件【不】并入导线集合 —— 它们两端不是等电位，正是靠这一点，
   “电源-电阻-LED-地”不会被误判成短路，而“电源直接用导线连到地”会被抓出来。
3. 检查器注册表模式：每条规则一个 checker，返回 Issue，按
   fatal/major/minor/warning 四级汇总，方便前端显示绿色“校验通过”或红色问题。

接入方式：run_erc(circuit_json, COMPONENT_KB) -> ERCReport
COMPONENT_KB 描述每种元件每个引脚的电气类型，可直接由你的元件库生成。
"""

import re
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple


# 引脚电气类型
class PT:
    POWER = "POWER"          # 电源正（VCC/5V/+），真正的供电方
    PWR_IN = "PWR_IN"        # 芯片/负载的供电引脚（如 NE555 VCC），只取电不供电，
                             # 与电源正同网络是正常供电，不算多源竞争
    PWR_OUT = "PWR_OUT"      # 供电输出（稳压器/电源芯片输出脚，如 7805 OUT）：
                             # 驱动的是供电轨而非信号，与电源/标记件同网属正常供电
                             #（此前归入 OUT 信号输出，接 VCC 轨
                             #  被“网络竞争”误判 fatal）
    GND = "GND"              # 地
    OUT = "OUT"              # 输出脚
    IN = "IN"                # 输入脚
    PASSIVE = "PASSIVE"      # 无源引脚（电阻/电容脚等）
    UNKNOWN = "UNKNOWN"


# severity 等级（从高到低）
FATAL, MAJOR, MINOR, WARNING = "fatal", "major", "minor", "warning"

# 感性负载类型表（需要续流二极管保护的元件）。
# 2026-09-10：补入 relay/dc_motor——本表注释与 check_inductive_flyback 的
# docstring 一直承诺覆盖“电感/继电器/电机”，此前集合只有 inductor，
# 继电器/电机缺续流二极管会漏检。
INDUCTIVE_TYPES = {"inductor", "relay", "dc_motor"}
# 视为“续流/钳位保护”的元件类型
FLYBACK_TYPES = {"diode", "zener", "schottky"}

# 电流路径回溯时允许穿透的「无源两脚元件」类型。
# 这些元件两端不等电位、不改变电路通断语义，可以当作连接相邻网络的边；
# 有源器件（晶体管/芯片/比较器）【不可】穿透——它们的导通/截止决定电流
# 是否流动，穿透会把“截止”误当通路。用于 check_led_current_limit 的
# 路径遍历（含经器件驱动形态）。
PASSIVE_TRAVERSE_TYPES = {
    "resistor", "capacitor", "inductor", "diode", "zener", "schottky",
    "potentiometer", "thermistor", "photoresistor", "fuse", "crystal",
}

# 有源器件（可作电流开关/驱动）：回溯时不允许穿透（避免把「截止」当通路），
# 但允许调用方显式跨到其【另一侧引脚】去判断是否存在电源源头。
# 用于 check_led_current_limit 的 B 段（经器件驱动形态）。
ACTIVE_DEVICE_TYPES = {
    "npn", "pnp", "nmos", "pmos", "mosfet", "transistor", "optocoupler",
    "ne555", "opamp", "comparator", "driver",
}

# 极性敏感的负载元件（带 “+”/“-” 引脚）：+ 脚应朝高电位、- 脚应朝低电位。
# 移除 capacitor——目录里的电容是无极性/电解通用类型
# （别名含“瓷片电容”），无从判断是否确有极性，按 major 报“极性反接”会把
# 无极性接法误伤；电解电容反接降级为 warning 提示（见 check_polarity）。
# 直流电机（转向相关）与整流桥直流输出同此模式；扬声器/电池不在此表。
POLARITY_PLUS_TYPES = {"led", "buzzer", "dc_motor", "bridge_rectifier"}
# 极性敏感的二极管类（A=阳极 / K=阴极），按 A/K 引脚判断方向
POLARITY_AK_TYPES = {"diode", "zener", "schottky", "photodiode"}

# 悬空输入豁免的“芯片类”类型名关键词（check_floating_input 用）
CHIP_HINTS = ("mcu", "arduino", "esp", "stm32", "芯片", "运放", "opamp", "amp")

# vcc/gnd 电源标记件：网络标签性质（导出时不输出符号、只承担网络改名），
# 不视为真实供电源，check_net_contention 对其豁免
MARKER_TYPES = {"vcc", "gnd"}


def _conn_refs(conn) -> List[str]:
    """统一取一条连接里的引脚引用：只看前两个元素（数据契约为二元组），
    兼容 [a, b, color, path] 四元组。所有检查器一律经由本函数取 refs。"""
    out = []
    if not isinstance(conn, (list, tuple)):
        return out
    for c in conn[:2]:
        if isinstance(c, str) and ":" in c:
            out.append(c)
    return out


# =====================================================================
# 并查集：把被导线短接的引脚并成一个“导线网络”
# =====================================================================
class DSU:
    def __init__(self):
        self.parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # 路径压缩
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


@dataclass
class Issue:
    severity: str
    rule: str
    message: str
    pins: List[str] = field(default_factory=list)


@dataclass
class ERCReport:
    issues: List[Issue] = field(default_factory=list)

    def add(self, severity, rule, message, pins=None):
        self.issues.append(Issue(severity, rule, message, pins or []))

    # —— 汇总接口 ——
    def _of(self, sev): return [i for i in self.issues if i.severity == sev]
    @property
    def fatals(self): return self._of(FATAL)
    @property
    def majors(self): return self._of(MAJOR)
    @property
    def minors(self): return self._of(MINOR)
    @property
    def warnings(self): return self._of(WARNING)

    @property
    def passed(self) -> bool:
        """没有 fatal/major 即视为可渲染（minor/warning 仅提示）。
        单遍短路：原先经由 fatals/majors 属性要完整扫描 issues 两遍。"""
        return not any(i.severity in (FATAL, MAJOR) for i in self.issues)

    def counts(self) -> Dict[str, int]:
        """各严重级条数 {fatal, major, minor, warning}（单遍统计）。

        统一供 server SSE 摘要 / ai2kicad 报告 / kicad 导出标题栏三处
        使用——原先三处各写一遍 sum(... if severity == ...) 的重复逻辑。"""
        out = {"fatal": 0, "major": 0, "minor": 0, "warning": 0}
        for i in self.issues:
            if i.severity in out:
                out[i.severity] += 1
        return out

    def to_dict(self) -> Dict[str, Any]:
        # 单遍按 severity 分组（原先 fatals/majors/minors/warnings + passed
        # 共扫描 issues 6 遍；电路大时这是一处可感知的常数开销）
        msgs: Dict[str, List[str]] = {}
        for i in self.issues:
            msgs.setdefault(i.severity, []).append(i.message)

        def _m(sev: str) -> List[str]:
            return msgs.get(sev, [])

        return {
            "passed": not _m(FATAL) and not _m(MAJOR),
            "failure_count": len(self.issues),
            "fatal_errors": _m(FATAL),
            "major_errors": _m(MAJOR),
            "minor_errors": _m(MINOR),
            "warnings": _m(WARNING),
            # 渲染层错误高亮需要 pins 坐标（render.js 按 “id:pin” 定位画红圈）
            "issues": [{"severity": i.severity, "rule": i.rule,
                        "message": i.message, "pins": i.pins} for i in self.issues],
        }

    def to_repair_prompt(self, graph: Optional["CircuitGraph"] = None) -> str:
        """序列化成给 LLM 的修复指令（错误回灌闭环的输入）。

        graph 可选：传入 CircuitGraph 时为每条 issue
        追加“引脚 -> 所在网络名”上下文——此前只给错误条目本身，模型拿到
        “R1:2 与 LED1:+ 同网”却不知道这是 N3，难以准确定位要改哪条连线。"""
        if self.passed and not self.issues:
            return "（无错误）"

        def _ctx(issue: Issue) -> str:
            if graph is None or not issue.pins:
                return ""
            nets = []
            for ref in issue.pins[:4]:        # 上下文取前 4 个引脚，防提示词膨胀
                nn = graph.net_name_of(ref)
                if nn and nn != "?":
                    nets.append(f"{ref}→{nn}")
            return f"（涉及：{'、'.join(nets)}）" if nets else ""

        lines = []
        for i in self.issues:
            if i.severity in (FATAL, MAJOR):
                lines.append(f"[{i.severity.upper()}] {i.rule}：{i.message}{_ctx(i)}")
        for i in self.issues:
            if i.severity in (MINOR, WARNING) and len(lines) < 12:
                lines.append(f"[{i.severity.upper()}] {i.rule}：{i.message}{_ctx(i)}")
        return "\n".join(lines) or "（无错误）"


# =====================================================================
# 电路图：构建导线网络 + 引脚索引
# =====================================================================
class CircuitGraph:
    def __init__(self, circuit_json: Dict[str, Any], kb: Dict[str, Dict[str, str]]):
        self.raw = circuit_json
        self.kb = kb                                   # {type: {pin: PT.xxx}}
        self.raw_parts: List[Dict[str, Any]] = list(circuit_json.get("parts", []))
        self.parts: Dict[str, Dict[str, Any]] = {p.get("id"): p for p in self.raw_parts}
        self.connections = circuit_json.get("connections", [])
        self.dsu = DSU()
        self.net_members: Dict[str, List[str]] = {}    # 网络根 -> 引脚列表
        self.net_names: Dict[str, str] = {}            # 网络根 -> "N1"/"N2"...
        self.pin_ptype: Dict[str, str] = {}            # "R1:1" -> 电气类型
        self._build()

    def pin_type(self, part_id: str, pin: str) -> str:
        part = self.parts.get(part_id)
        if not part:
            return PT.UNKNOWN
        pins = self.kb.get(part.get("type"), {})
        return pins.get(pin, PT.UNKNOWN)

    def _build(self):
        # 1) 登记所有已声明元件的引脚类型
        for pid, part in self.parts.items():
            for pin in self.kb.get(part.get("type"), {}):
                ref = f"{pid}:{pin}"
                self.pin_ptype[ref] = self.pin_type(pid, pin)
        # 2) 每条 connection 内的引脚用导线短接（统一只取前两个引用）
        for conn in self.connections:
            refs = _conn_refs(conn)
            for r in refs:
                self.dsu.find(r)
                if r not in self.pin_ptype:           # 连接里出现了库中没有的引脚
                    pid, pin = r.split(":", 1)
                    self.pin_ptype[r] = self.pin_type(pid, pin)
            for r in refs[1:]:
                self.dsu.union(refs[0], r)
        # 3) 汇总每个导线网络的成员，并按“最小成员名”排序赋网络名（可复现）
        groups: Dict[str, List[str]] = {}
        for ref in self.dsu.parent:
            groups.setdefault(self.dsu.find(ref), []).append(ref)
        for idx, root in enumerate(sorted(groups, key=lambda r: min(groups[r])), start=1):
            groups[root].sort()
            self.net_names[root] = f"N{idx}"
        self.net_members = groups

    def net_of(self, ref: str) -> Optional[List[str]]:
        if ref not in self.dsu.parent:
            return None
        return self.net_members.get(self.dsu.find(ref))

    def net_name_of(self, ref: str) -> str:
        net = self.net_of(ref)
        if net is None:
            return "?"
        return self.net_names.get(self.dsu.find(ref), "?")

    def net_name(self, net: List[str]) -> str:
        if not net:
            return "?"
        return self.net_names.get(self.dsu.find(net[0]), "?")


# =====================================================================
# 参数解析辅助：把 "330Ω"/"10k"/"1kΩ"/"4.7MΩ"/"100nF"/"10μF" 解析成数值
# =====================================================================
# 数值 + 工程单位（µ/μ 先归一化成 u 再查表）。
# 倍率：p=1e-12, n=1e-9, u=1e-6, m=1e-3, 无=1, k=1e3, M=1e6
_UNIT_TABLE = {
    # 电阻（Ω）：R 是“0R”这类英标零欧写法，E 是欧标同义
    "Ω": ("Ω", 1.0), "R": ("Ω", 1.0), "E": ("Ω", 1.0),
    "k": ("Ω", 1e3), "K": ("Ω", 1e3), "kΩ": ("Ω", 1e3),
    "M": ("Ω", 1e6), "MΩ": ("Ω", 1e6),
    "m": ("Ω", 1e-3), "mΩ": ("Ω", 1e-3),
    # 电容（F）
    "F": ("F", 1.0),
    "pF": ("F", 1e-12), "nF": ("F", 1e-9),
    "uF": ("F", 1e-6), "mF": ("F", 1e-3),
    # 电感（H）
    "H": ("H", 1.0), "nH": ("H", 1e-9), "uH": ("H", 1e-6), "mH": ("H", 1e-3),
    # 电压/电流（V/A）
    "V": ("V", 1.0), "mV": ("V", 1e-3), "kV": ("V", 1e3),
    "A": ("A", 1.0), "mA": ("A", 1e-3), "uA": ("A", 1e-6),
}

# 匹配 “数字[.数字] 单位”，单位字母可含 Ω/µ/μ（不支持科学计数和千分位，教学够用）
_VALUE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-ZµμΩ]*)\s*$")


def parse_value(s: Any) -> Tuple[Optional[float], str]:
    """解析元件 attrs.value 里的工程写法，返回 (数值, 量纲)。

    例： “330Ω”->(330.0,"Ω")  “10k”->(10000.0,"Ω")  “1kΩ”->(1000.0,"Ω")
        “4.7MΩ”->(4.7e6,"Ω")  “100nF”->(1e-7,"F")    “10μF”->(1e-5,"F")
        “47pF”->(4.7e-11,"F") “10mH”->(0.01,"H")      “5V”->(5.0,"V")
    裸数字（如 “470”）量纲未知，返回 (470.0, "")，由调用方按上下文约定解释
    （电阻惯例按欧姆）。解析不了的（“红”“1N4148”“NE555”）返回 (None, "")。
    """
    if not isinstance(s, str):
        return None, ""
    m = _VALUE_RE.match(s.strip())
    if not m:
        return None, ""
    num = float(m.group(1))
    unit = (m.group(2) or "").replace("µ", "u").replace("μ", "u")
    if not unit:
        return num, ""
    hit = _UNIT_TABLE.get(unit)
    if hit is None:
        return None, ""
    dim, mult = hit
    return num * mult, dim


def _fmt_ohm(v: float) -> str:
    """把欧姆值格式化成教学友好的写法：10000 -> “10kΩ”，0 -> “0Ω”。"""
    if v >= 1e6:
        return f"{v / 1e6:g}MΩ"
    if v >= 1e3:
        return f"{v / 1e3:g}kΩ"
    return f"{v:g}Ω"


# 判断“导线网络里是否存在某类电气类型的引脚”的快捷方式
def _net_has_type(g: "CircuitGraph", net: List[str], *types: str) -> bool:
    return any(g.pin_ptype.get(r) in types for r in net)


# =====================================================================
# 各条检查规则（每个都是纯函数：Graph, Report -> None）
# =====================================================================

def check_duplicate_ids(g: CircuitGraph, rep: ERCReport):
    """parts 里出现重复 id：后面的会覆盖前面的，属于结构性错误，fatal。"""
    seen: Dict[str, int] = {}
    for p in g.raw_parts:
        pid = p.get("id")
        if pid:
            seen[pid] = seen.get(pid, 0) + 1
    for pid, n in seen.items():
        if n > 1:
            rep.add(FATAL, "重复位号", f"位号 “{pid}” 出现了 {n} 次，元件 id 必须唯一", [pid])


def check_unknown_component(g: CircuitGraph, rep: ERCReport):
    """使用了元件库中没有的元件类型（OOD），major：提示而不是让它瞎画。"""
    for pid, part in g.parts.items():
        if part.get("type") not in g.kb:
            rep.add(MAJOR, "未知元件", f"元件 {pid} 的类型 “{part.get('type')}” 不在元件库中", [pid])


def check_hallucinated_pins(g: CircuitGraph, rep: ERCReport):
    """连接里引用了元件库不存在的引脚 = LLM 幻觉，直接 fatal（你项目的核心防线）。

    实现：先按元件把全部引脚引用分组（一遍扫描），再逐元件对照白名单 ——
    复杂度 O(引用总数 + 元件数)，替代原先“每元件全量重扫所有连接”的
    O(元件数 × 连接数)；报告的问题顺序与旧实现一致（元件序 × 连接内出现序）。"""
    refs_by_pid: Dict[str, List[str]] = {}
    for conn in g.connections:
        for ref in _conn_refs(conn):
            refs_by_pid.setdefault(ref.split(":", 1)[0], []).append(ref)
    for pid, part in g.parts.items():
        legal = set(g.kb.get(part.get("type"), {}).keys())
        if not legal:
            continue
        for ref in refs_by_pid.get(pid, ()):
            pin = ref.split(":", 1)[1]
            if pin not in legal:
                rep.add(FATAL, "幻觉引脚",
                        f"{part.get('type')} {pid} 不存在引脚 “{pin}”，合法引脚为 {sorted(legal)}",
                        [ref])


def check_connection_endpoints(g: CircuitGraph, rep: ERCReport):
    """每条连接必须恰好引用两个存在的元件引脚。"""
    for idx, conn in enumerate(g.connections):
        refs = _conn_refs(conn)
        if len(refs) < 2:
            rep.add(MAJOR, "连接不完整", f"第 {idx+1} 条连接引脚不足两个：{list(conn)[:2]}")
            continue
        for ref in refs:
            pid = ref.split(":", 1)[0]
            if pid not in g.parts:
                rep.add(FATAL, "悬空引用", f"第 {idx+1} 条连接引用了不存在的元件 “{pid}”", [ref])


def check_short_circuit(g: CircuitGraph, rep: ERCReport):
    """同一导线网络里同时出现电源和地 = 电源短路 fatal。"""
    for net in g.net_members.values():
        types = {r: g.pin_ptype.get(r, PT.UNKNOWN) for r in net}
        pwr = [r for r, t in types.items() if t == PT.POWER]
        gnd = [r for r, t in types.items() if t == PT.GND]
        if pwr and gnd:
            rep.add(FATAL, "电源短路",
                    f"网络 {g.net_name(net)} 中电源 {pwr} 与地 {gnd} 被导线直接短接"
                    f"（中间没有任何元件阻隔）", pwr + gnd)


def _reachable_pins_via_passives(g: CircuitGraph, start_ref: str,
                                 blocked: Optional[set] = None,
                                 max_hops: int = 6) -> set:
    """从 start_ref 出发，沿「导线网络 + 无源元件体」交替前进，收集可达引脚。

    走法（与真实电流路径同构）：
      ① 同网络内的其它引脚（导线直接相连者）——一步直达；
      ② 从这些引脚再穿过其所属的【无源两脚元件】（电阻/电容/二极管等，
         把元件本身当作连接两端网络的边），到达元件另一端的网络。
    有源器件（晶体管/芯片）【不】穿透：电流路径的“方向/通断”由它们决定，
    穿透会把「晶体管截止」也当成通路，造成误报。若需要跨过有源器件去看
    另一侧，由调用方用 _active_device_outer_refs 显式取“外侧引脚”。

    blocked: 不允许穿透的元件 id 集合（通常是 LED 自身，避免自环）。
    max_hops: 穿透层数上限（防环/防病态输入长循环）。
    返回引用的集合（形如 "R1:1"）。"""
    blocked = blocked or set()
    seen_nets = set()
    seen_refs = set()
    frontier = {start_ref}
    hops = 0
    while frontier and hops <= max_hops:
        # ① 展开同网络成员
        net_layer = set()
        for ref in frontier:
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            net = g.net_of(ref)
            if not net:
                continue
            root = g.dsu.find(ref)
            if root in seen_nets:
                continue
            seen_nets.add(root)
            net_layer.update(net)
        seen_refs.update(net_layer)
        # ② 穿过无源元件到另一端
        next_frontier = set()
        for ref in net_layer:
            pid, _, _pin = ref.partition(":")
            if pid in blocked:
                continue
            ptype = g.parts.get(pid, {}).get("type")
            if ptype not in PASSIVE_TRAVERSE_TYPES:
                continue
            for other_pin in g.kb.get(ptype, {}):
                oref = f"{pid}:{other_pin}"
                if oref != ref and oref not in seen_refs:
                    next_frontier.add(oref)
        frontier = next_frontier
        hops += 1
    return seen_refs


def _active_device_outer_refs(g: CircuitGraph, refs: set) -> set:
    """给定一组引脚，找出它们所属的【有源器件】（晶体管/芯片）的【其它】引脚网络。

    用于 check_led_current_limit 的 B 段：LED 由晶体管驱动时，LED 的 + 脚
    网络里只有晶体管的 C/E 脚，真正的供电侧在晶体管的另一侧。此处故意
    【不判断导通方向、只看另一侧是否为电源源头】——因为“LED 被驱动管接在
    电源上却没有任何限流电阻”这一事实与管子是否导通无关：一旦导通即过流。

    只取有源器件的引脚，不含无源元件（后者已由 _reachable_pins_via_passives
    穿透处理，避免重复/环）。
    返回「其它引脚所在网络的全部成员」引用集合。"""
    out = set()
    dev_ids = set()
    for ref in refs:
        pid, _, _pin = ref.partition(":")
        if g.parts.get(pid, {}).get("type") in ACTIVE_DEVICE_TYPES:
            dev_ids.add(pid)
    for pid in dev_ids:
        ptype = g.parts.get(pid, {}).get("type")
        for pin in g.kb.get(ptype, {}):
            oref = f"{pid}:{pin}"
            net = g.net_of(oref)
            if net:
                out.update(net)
    return out


def check_led_current_limit(g: CircuitGraph, rep: ERCReport):
    """LED 无限流电阻检查（major）：沿电流路径遍历，覆盖晶体管/输出脚驱动形态。

    两层判定（宁可少报不误报）：

    A) 直跨形态（原判定，保留）——LED 的 + 脚网络里有 POWER/OUT 电源源头，
       且 - 脚网络里有 GND，说明 LED 被导线直接跨接在电源与地之间。

    B) 经器件驱动形态——LED 由晶体管/输出器件
       驱动时，+ 脚网络里只有驱动管的引脚、没有电源源头，A 判不中。此时：
         b1) 从 LED 的 + 脚沿【导线 + 无源元件】回溯（见
             _reachable_pins_via_passives），若可达集合里有 POWER/PWR_OUT
             电源源头且【无电阻】，报缺限流电阻；
         b2) 再取 + 脚网络里所挂【有源器件】的其它引脚所在网络（见
             _active_device_outer_refs），若那些网络上存在 POWER/PWR_OUT
             且 LED 侧与器件外侧【都没有电阻】，同样报——覆盖
             「电源 → 晶体管C → 晶体管E → LED → 地」这条经典送死电路。

    为什么源头只认 POWER/PWR_OUT、不含信号 OUT：555 OUT、比较器输出等
    信号脚驱动 LED 属正常用法（且常自带内部限流），从严不报以免误伤。

    为什么跨有源器件时不判断导通：本条检查的是「供电路径上没有限流元件」
    这一结构事实，与管子当前是否导通无关——一旦导通即过流烧 LED。

    防误报：只要路径上（LED 侧或器件外侧）存在任意电阻，即认为有限流元件，
    安静通过——“电池-电阻-LED-地”“电源-晶体管C、E-电阻-LED-地”都是
    标准教学电路，必须不报。0Ω 电阻的隐患由 check_passive_values 单独处理。

    （LED 的 “+/−” 脚在元件目录里已改为 PASSIVE，本检查只看网络里【别的】引脚，
    因此 LED 反接不会落进这条规则，反接由 check_polarity 负责。）"""
    for pid, part in g.parts.items():
        if part.get("type") != "led":
            continue
        plus, minus = f"{pid}:+", f"{pid}:-"
        np_, nm = g.net_of(plus), g.net_of(minus)
        if not np_ or not nm:
            continue
        hits_hi = _net_has_type(g, np_, PT.POWER, PT.PWR_OUT, PT.OUT)  # + 脚网络有电源/输出源头
        hits_lo = _net_has_type(g, nm, PT.GND)                          # - 脚网络有地

        # A) 直跨：电源源头与地分别落在 LED 两脚网络上（中间无任何元件）
        if hits_hi and hits_lo:
            rep.add(MAJOR, "LED缺限流电阻",
                    f"LED {pid} 被导线直接跨接在电源/输出与地之间，没有限流电阻会烧毁 LED，"
                    f"应串联 220Ω~1kΩ 限流电阻", [plus, minus])
            continue

        # B) 经器件驱动：两段回溯，任一命中「有电源源头且全程无电阻」即报。
        reach = _reachable_pins_via_passives(g, plus, blocked={pid})
        outer = _active_device_outer_refs(g, reach)

        def _scan(refs):
            """返回 (是否有真电源源头, 是否有电阻)。"""
            src = res = False
            for ref in refs:
                rpid, _, _rp = ref.partition(":")
                if g.parts.get(rpid, {}).get("type") == "resistor":
                    res = True
                    continue
                if g.pin_ptype.get(ref) in (PT.POWER, PT.PWR_OUT):
                    src = True
            return src, res

        src_in, res_in = _scan(reach)
        src_out, res_out = _scan(outer)
        # b1：LED 侧自身即可达电源源头且无电阻
        # b2：跨过驱动管后外侧有电源源头，且 LED 侧与外侧都没有电阻
        if (src_in and not res_in) or (src_out and not res_in and not res_out):
            rep.add(MAJOR, "LED缺限流电阻",
                    f"LED {pid} 的供电路径（经晶体管/输出器件）上没有任何限流电阻，"
                    f"LED 会被过流烧毁，应在 LED 串联 220Ω~1kΩ 限流电阻，"
                    f"或在驱动管基极/栅极侧加限流", [plus, minus])


def check_polarity(g: CircuitGraph, rep: ERCReport):
    """极性反接检查（major）：+/- 型负载（LED/蜂鸣器/电解电容）与 A/K 型二极管。

    判定同样基于网络构成（“电源源头/地源头”只认真正的源 —— 电池、vcc/gnd 符号）：
      * +/- 型：+ 脚网络里有 GND 源头、且 - 脚网络里有 POWER 源头  => 极性反了
      * A/K 型：A(阳极)网络里有 POWER 源头、且 K(阴极)网络里有 GND 源头
                => 二极管正向直跨电源，无限流会立刻烧毁（稳压管正常用法是
                   反向跨接：K 接高电位、A 接地，不会触发本条）
    防误报说明：只有“导线直连”才报 —— 串了电阻之后 +/− 脚网络被无源元件隔开，
    网络里只剩 PASSIVE 引脚，天然不触发；因此【串联电阻后的反接检测不到】是本
    规则的已知局限（教学中先修好直连反接，串阻场景靠 LLM 修复回灌兜底）。
    输出脚（OUT）不算电源源头：比较器/555 低电平驱动、PNP 高边驱动等场景
    语义不明，从严只认 POWER，避免把正常驱动电路误报成反接。
    电容从 major 极性表移出——目录电容是无极性/电解通用
    类型，反接只按 warning 提示（若是电解电容确实危险，但瓷片电容接反无害，
    无从区分时不得用 major 误伤）。"""
    # 每种极性元件的教学提示（错误消息用教学口吻说明怎么改）
    plus_tips = {
        "led": "LED {pid} 正负极接反了：长脚（+）应接高电位一侧、短脚（-）接低电位一侧，"
               "请对调两条连线（本条不亮就是接反了）",
        "buzzer": "蜂鸣器 {pid} 正负极接反了：+ 应接电源正、- 接地，有源蜂鸣器接反了不会响甚至损坏，"
                  "请对调两条连线",
        "dc_motor": "直流电机 {pid} 正负极接反了：+ 应接高电位、- 接低电位，"
                    "接反电机会反转，若与预期转向不符请对调两条连线",
        "bridge_rectifier": "整流桥 {pid} 直流输出接反了：+ 应接负载高电位端、"
                            "- 接低电位端，请对调两条连线",
    }
    for pid, part in g.parts.items():
        ctype = part.get("type")
        if ctype == "capacitor":
            # 电容极性提示（warning，非 major）：+ 脚网络有地源头、- 脚网络有电源
            plus, minus = f"{pid}:+", f"{pid}:-"
            np_, nm = g.net_of(plus), g.net_of(minus)
            if not np_ or not nm:
                continue
            if _net_has_type(g, np_, PT.GND) and _net_has_type(g, nm, PT.POWER):
                rep.add(WARNING, "电容极性提示",
                        f"电容 {pid} 的 + 脚接在低电位、- 脚接在高电位："
                        f"若为电解/钽电容属于反接（发热漏液甚至爆裂），"
                        f"无极性瓷片/薄膜电容可忽略本提示",
                        [plus, minus])
        elif ctype in POLARITY_PLUS_TYPES:
            plus, minus = f"{pid}:+", f"{pid}:-"
            np_, nm = g.net_of(plus), g.net_of(minus)
            if not np_ or not nm:
                continue
            if _net_has_type(g, np_, PT.GND) and _net_has_type(g, nm, PT.POWER):
                rep.add(MAJOR, "极性反接", plus_tips.get(
                    ctype, f"元件 {pid} 正负极接反了，请对调 + / - 两条连线").format(pid=pid),
                    [plus, minus])
        elif ctype in POLARITY_AK_TYPES:
            a, k = f"{pid}:A", f"{pid}:K"
            na, nk = g.net_of(a), g.net_of(k)
            if not na or not nk:
                continue
            if _net_has_type(g, na, PT.POWER) and _net_has_type(g, nk, PT.GND):
                rep.add(MAJOR, "二极管正向直跨电源",
                        f"二极管 {pid} 阳极 A 接电源正、阴极 K 接地，正向导通又没有限流电阻，"
                        f"相当于电源短路会立刻烧毁；请检查方向（稳压管应反向使用：K 接高电位）"
                        f"或串联限流电阻", [a, k])


def check_passive_values(g: CircuitGraph, rep: ERCReport):
    """参数合理性检查：结构合法、但电气上离谱的元件参数。

    a) 与 LED 同一导线网络的电阻阻值 < 100Ω  -> warning 限流电阻过小（烧 LED）
    b) 阻值为 0Ω 的电阻两端网络分别落在电源源头与地源头 -> fatal（等效电源短路；
       导线短路由 check_short_circuit 抓，这里补上“经 0Ω 电阻短路”的漏网场景）
    阻值取自 attrs.value（"330Ω"/"10k"/"1kΩ"/"0R"...，见 parse_value）；
    解析不出来的值静默跳过 —— 宁可漏报，不可误报。"""
    # 先把所有电阻的阻值解析出来：{id: 欧姆值}（裸数字按电阻惯例视为欧姆）
    ohms: Dict[str, float] = {}
    for pid, part in g.parts.items():
        if part.get("type") != "resistor":
            continue
        v, dim = parse_value((part.get("attrs") or {}).get("value"))
        if v is None or v < 0:
            continue
        if dim in ("Ω", ""):
            ohms[pid] = v

    # a) LED 限流电阻过小（限流电阻在 LED 的 + 侧或 - 侧都算，取该侧网络里的电阻）
    for pid, part in g.parts.items():
        if part.get("type") != "led":
            continue
        for pin in ("+", "-"):
            net = g.net_of(f"{pid}:{pin}")
            if not net:
                continue
            for ref in net:
                rid = ref.split(":", 1)[0]
                if rid not in ohms or g.parts.get(rid, {}).get("type") != "resistor":
                    continue
                if ohms[rid] > 0 and ohms[rid] < 100.0:
                    rep.add(WARNING, "LED限流电阻过小",
                            f"与 LED {pid} 串联的电阻 {rid} 阻值只有 {_fmt_ohm(ohms[rid])}，"
                            f"流过的电流过大可能烧毁 LED，建议 220Ω~1kΩ",
                            [f"{rid}:1", f"{rid}:2"])

    # b) 0Ω 电阻跨接电源两端（任一方向：R:1 朝电源 R:2 朝地，或反过来）
    for rid, r in ohms.items():
        if r > 0:
            continue
        n1, n2 = g.net_of(f"{rid}:1"), g.net_of(f"{rid}:2")
        if not n1 or not n2:
            continue
        cross = ((_net_has_type(g, n1, PT.POWER) and _net_has_type(g, n2, PT.GND)) or
                 (_net_has_type(g, n1, PT.GND) and _net_has_type(g, n2, PT.POWER)))
        if cross:
            rep.add(FATAL, "0Ω电阻短路",
                    f"电阻 {rid} 阻值为 0Ω，把电源与地直接短接，等同于电源短路"
                    f"（若只是想连通两个网络，请直接用导线；跳线也应避开电源对地）",
                    [f"{rid}:1", f"{rid}:2"])


def check_net_contention(g: CircuitGraph, rep: ERCReport):
    """同一导线网络里多个“驱动源”打架检查（供电与信号输出分离的对撞矩阵）。

    建模修正：把「信号输出」（OUT）与「供电」（POWER / PWR_OUT）分开判定——
    * vcc/gnd 电源标记件不计入竞争源：它们是网络标签性质（导出时不输出符号，
      只承担网络改名 GND/VCC），电池正极接 VCC 标记属正常画法而非两个真实源。
    * 供电类引脚（POWER 电池正极 / PWR_OUT 稳压器输出）同网合并属正常供电：
      稳压器输出轨、电池直供轨天生就有“多个供电性质引脚”共线，只有【信号
      输出】混进供电轨、或两个真实电源（双电池并联）才构成错误。
    * 晶体管 C/E 已在目录层改为 PASSIVE（方向由电路决定），PNP 高边开关
      （E 接电源轨）不再产生伪 OUT 源，本函数无需特判。
    判定矩阵：
      OUT 撞 POWER/PWR_OUT      -> fatal 网络竞争（信号输出反向驱动供电轨）
      POWER × ≥2                -> fatal 电源冲突（双电池并联仍是真实错误）
      OUT × ≥2                  -> major 输出冲突（推挽输出互顶）
      PWR_OUT × ≥2              -> warning 并联供电输出（两路稳压输出并联，
                                   教学上应避免但非烧毁级错误）
      POWER + PWR_OUT（各 1）    -> 不报（电池直挂稳压输出轨＝旁路稳压器，
                                   非破坏性接线问题，交给教学讲解）"""
    for net in g.net_members.values():
        pwr, pwr_out, outs = [], [], []
        for r in net:
            t = g.pin_ptype.get(r)
            if t not in (PT.POWER, PT.PWR_OUT, PT.OUT):
                continue
            if str(g.parts.get(r.split(":", 1)[0], {}).get("type", "")).lower() in MARKER_TYPES:
                continue   # vcc/gnd 标记件是网络标签，不是真实源
            (pwr if t == PT.POWER else pwr_out if t == PT.PWR_OUT else outs).append(r)
        if not (pwr or pwr_out or outs):
            continue
        if outs and (pwr or pwr_out):
            rep.add(FATAL, "网络竞争",
                    f"网络 {g.net_name(net)} 中信号输出脚 {outs} 与供电脚 "
                    f"{pwr + pwr_out} 接到同一网络，反向驱动时可能损坏",
                    outs + pwr + pwr_out)
        elif len(pwr) > 1:
            rep.add(FATAL, "电源冲突",
                    f"网络 {g.net_name(net)} 中多个电源脚 {pwr} 接到同一网络，"
                    f"请确认电压是否一致", pwr)
        elif len(outs) > 1:
            rep.add(MAJOR, "输出冲突",
                    f"网络 {g.net_name(net)} 中多个输出脚 {outs} 接到同一网络"
                    f"（推挽输出互顶）", outs)
        elif len(pwr_out) > 1:
            rep.add(WARNING, "并联供电输出",
                    f"网络 {g.net_name(net)} 中多个供电输出脚 {pwr_out} 接到同一网络"
                    f"（两路稳压输出并联，输出电压难以一致，建议只用一路供电）", pwr_out)


def check_floating_input(g: CircuitGraph, rep: ERCReport):
    """输入脚所在导线网络只有它自己 = 悬空输入 warning（单片机未用脚可豁免）。"""
    for ref, t in g.pin_ptype.items():
        if t != PT.IN:
            continue
        ctype = str(g.parts.get(ref.split(":", 1)[0], {}).get("type", "")).lower()
        if any(k in ctype for k in CHIP_HINTS):   # 芯片未用脚可豁免
            continue
        net = g.net_of(ref)
        if net is None or len(net) == 1:  # 从未出现在任何连接里，或所在网络只有自己
            rep.add(WARNING, "悬空输入", f"输入脚 {ref} 没有连接到任何网络", [ref])


def check_no_ground(g: CircuitGraph, rep: ERCReport):
    """有源电路（存在电源/芯片供电/输出）必须有地网络，否则电流无回路，major。"""
    has_active = any(t in (PT.POWER, PT.PWR_IN, PT.PWR_OUT, PT.OUT)
                     for t in g.pin_ptype.values())
    if not has_active:
        return
    for net in g.net_members.values():
        if any(g.pin_ptype.get(r) == PT.GND for r in net):
            return
    rep.add(MAJOR, "未共地", "电路中没有任何接地（GND）连接，电流无法构成回路", [])


def check_unconnected_part(g: CircuitGraph, rep: ERCReport):
    """元件声明了却一个引脚都没接（LLM 常见“只摆件不接线”），minor。

    实现：先一遍扫描连接收集“已接线元件 id 集合”，再逐元件查集合 ——
    O(引用总数 + 元件数)，替代原先每元件都全量重扫一遍连接的 O(P×C)。"""
    wired: set = set()
    for conn in g.connections:
        for ref in _conn_refs(conn):
            wired.add(ref.split(":", 1)[0])
    for pid, part in g.parts.items():
        if part.get("type") in ("gnd", "vcc"):
            continue  # 电源标记件允许悬空待连
        if pid not in wired:
            rep.add(MINOR, "未连接元件", f"元件 {pid}（{part.get('type')}）没有连接任何导线", [pid])


def check_inductive_flyback(g: CircuitGraph, rep: ERCReport):
    """感性负载（电感/继电器/电机）被输出脚或晶体管驱动时，应有续流二极管，warning。
    教学场景定为 warning（不阻断渲染），同时保留提示价值。"""
    has_switching = any(
        g.parts[p].get("type") in ("npn", "pnp", "nmos", "ne555", "optocoupler")
        for p in g.parts
    ) or any(t == PT.OUT for t in g.pin_ptype.values())
    # optocoupler 2026-09-10 补入：光耦集电极改为 PASSIVE 后，继电器经光耦
    # 驱动的场景不能再靠 C=OUT 触发本检查，改按元件类型识别开关器件
    if not has_switching:
        return
    has_flyback = any(p.get("type") in FLYBACK_TYPES for p in g.parts.values())
    if has_flyback:
        return
    for pid, part in g.parts.items():
        if part.get("type") in INDUCTIVE_TYPES:
            rep.add(WARNING, "缺续流二极管",
                    f"感性负载 {pid}（{part.get('type')}）由开关/输出驱动，"
                    f"建议并联续流二极管（如 1N4148）保护驱动管", [pid])


def check_decoupling(g: CircuitGraph, rep: ERCReport):
    """带供电脚（PWR_IN）的芯片，其供电网络上建议挂 100nF 去耦电容，warning。

    实现：电容引脚引用集合只构建一次（原先每个芯片都重建一遍
    "电容 id:脚名"列表）；供电网络转集合后遍历网络成员查集合 ——
    "交非空"判定与原方向（遍历电容引用查网络列表）完全等价，
    复杂度从 O(|电容脚| × |网络|) 降为 O(|网络|)。

    多电源脚（2026-09-09 修）：原先用 `next(...)` 只取第一个 PWR_IN 引脚，
    对双电源器件（如运放 V+/V−、双路芯片）会漏检其余供电脚——只要第一个
    脚挂了去耦电容就整颗芯片放行。当前元件库尚无多 PWR_IN 类型，
    故这是潜伏缺陷；改为逐个供电脚独立判定，新增此类元件时自动生效。
    同一条网络只报一次（避免同一芯片多个供电脚接同一网络时重复刷屏）。
    """
    caps = [p for p in g.parts.values() if p.get("type") == "capacitor"]
    cap_refs = {f"{c.get('id')}:{cpin}"
                for c in caps for cpin in g.kb.get("capacitor", {})}
    for pid, part in g.parts.items():
        kb_pins = g.kb.get(part.get("type"), {})
        vcc_pins = [pin for pin, t in kb_pins.items() if t == PT.PWR_IN]
        if not vcc_pins:
            continue
        reported_nets = set()   # 已在本次（同一芯片）报过的网络，防重复
        for vcc_pin in vcc_pins:
            net = g.net_of(f"{pid}:{vcc_pin}")
            if not net:
                continue
            # 供电网络（或经导线直连的网络）上有没有电容脚
            netset = set(net)
            if any(r in cap_refs for r in netset):
                continue        # 该供电网络已有去耦，放行
            key = tuple(sorted(netset))
            if key in reported_nets:
                continue        # 同一网络已报过（多脚共网时不重复）
            reported_nets.add(key)
            rep.add(WARNING, "缺去耦电容",
                    f"{pid}（{part.get('type')}）的供电脚 {vcc_pin} 建议就近加 100nF 去耦电容",
                    [f"{pid}:{vcc_pin}"])


def check_redundant_connection(g: CircuitGraph, rep: ERCReport):
    """同一条连接里把同一个引脚写了多次；以及同一对引脚被重复连接、自己连自己。"""
    for idx, conn in enumerate(g.connections):
        refs = _conn_refs(conn)
        if len(refs) != len(set(refs)):
            dup = sorted({r for r in refs if refs.count(r) > 1})
            rep.add(MINOR, "重复连接", f"第 {idx+1} 条连接中引脚 {dup} 重复出现", dup)
        elif len(refs) == 2 and refs[0] == refs[1]:
            rep.add(MINOR, "自环连接", f"第 {idx+1} 条连接两端是同一个引脚 {refs[0]}", refs)
    seen_pairs = {}
    for idx, conn in enumerate(g.connections):
        refs = _conn_refs(conn)
        if len(refs) == 2:
            key = tuple(sorted(refs))
            if key in seen_pairs:
                rep.add(MINOR, "重复连接",
                        f"第 {idx+1} 条连接与第 {seen_pairs[key]+1} 条重复（{list(key)}）", list(key))
            else:
                seen_pairs[key] = idx




def check_single_node_net(g: CircuitGraph, rep: ERCReport):
    """只挂了一个引脚的网络（工业级：悬空节点/单点网络）。"""
    for net in g.net_members.values():
        if len(net) == 1:
            ref = net[0]
            pid, _, pin = ref.partition(":")
            # 电源/地标记件的单点网络也提示
            rep.add(MINOR, "单点网络",
                    f"网络仅连接 1 个引脚 {ref}，可能是悬空节点或未接完的线", [ref])


def check_ic_power_missing(g: CircuitGraph, rep: ERCReport):
    """IC 的电源脚未接到电源网络（工业级：芯片没供电）。"""
    for pid, part in g.parts.items():
        ptype = part.get("type")
        pins = g.kb.get(ptype) or {}
        if not pins:
            continue
        # 有 PWR_IN 或明确 VCC/VDD 名的芯片
        power_pins = [p for p, t in pins.items()
                      if t == PT.PWR_IN or p in ("VCC", "VDD", "V+")]
        if not power_pins:
            continue
        for pin in power_pins:
            ref = f"{pid}:{pin}"
            net = g.net_of(ref)
            if not net:
                continue
            # 网络里是否有真正的供电源。PWR_IN 只取电，不能把芯片自己的
            # 供电脚（或另一个负载供电脚）反过来当成电源来源；此前把
            # PWR_IN 纳入这里，会让“VCC 接到无源网络”永远无法触发本规则。
            has_src = any(
                g.pin_ptype.get(r) in (PT.POWER, PT.PWR_OUT)
                for r in net
            )
            # 或电池正极
            has_bat = any(
                g.parts.get(r.split(":", 1)[0], {}).get("type") in ("battery", "vcc")
                for r in net
            )
            if not has_src and not has_bat:
                rep.add(MAJOR, "芯片未供电",
                        f"{pid}（{ptype}）供电脚 {pin} 所在网络未见电源来源，芯片可能不工作",
                        [ref])


def check_multiple_battery(g: CircuitGraph, rep: ERCReport):
    """多个电池：正极对正极或负极对负极 → 极性冲突（工业级）。"""
    bats = [pid for pid, p in g.parts.items() if p.get("type") == "battery"]
    if len(bats) < 2:
        return
    pairs = []
    for conn in g.connections:
        refs = [x for x in conn[:2] if isinstance(x, str) and ":" in x]
        if len(refs) == 2:
            pairs.append(set(refs))
    for i, a in enumerate(bats):
        for b in bats[i + 1:]:
            plus_pair = {f"{a}:+", f"{b}:+"}
            minus_pair = {f"{a}:-", f"{b}:-"}
            for s in pairs:
                if s == plus_pair or s == minus_pair:
                    rep.add(MAJOR, "多电池极性冲突",
                            f"电池 {a} 与 {b} 同极相连，可能造成短路，请核对串联/并联意图",
                            [a, b])
                    break

def check_open_collector_output(g: CircuitGraph, rep: ERCReport):
    """集电极开路/漏极开路输出未接上拉（工业级：可能无法输出高电平）。"""
    # 简化：对 npn C 脚所在网络，若无电阻、无电源，则提示
    for pid, part in g.parts.items():
        ptype = part.get("type")
        if ptype not in ("npn", "pnp"):
            continue
        cref = f"{pid}:C" if ptype == "npn" else f"{pid}:E"
        net = g.net_of(cref)
        if not net:
            continue
        # 若网络里只有该引脚和地，无上拉电阻
        has_res = any(
            g.parts.get(r.split(":", 1)[0], {}).get("type") == "resistor"
            for r in net
        )
        has_pwr = any(
            g.pin_ptype.get(r) in (PT.POWER, PT.PWR_OUT, PT.PWR_IN)
            for r in net
        )
        if not has_res and not has_pwr and len(net) == 1:
            # 单独 C 脚悬空
            rep.add(MINOR, "输出脚悬空",
                    f"{pid} 的集电极/发射极 {cref} 悬空，开关电路通常需接负载或上拉",
                    [cref])


def check_diode_reverse_protection_missing(g: CircuitGraph, rep: ERCReport):
    """电池直接接电解电容无防反接（工业级提示）。"""
    # 有电解电容且电池直接并联时提示
    has_bat = any(p.get("type") == "battery" for p in g.parts.values())
    has_elec = any(p.get("type") == "electrolytic" for p in g.parts.values())
    if not (has_bat and has_elec):
        return
    # 找 battery+ 与 electrolytic+ 是否同网
    bat_plus = [f"{pid}:+" for pid, p in g.parts.items() if p.get("type") == "battery"]
    elec_plus = [f"{pid}:+" for pid, p in g.parts.items() if p.get("type") == "electrolytic"]
    for b in bat_plus:
        net = g.net_of(b)
        if not net:
            continue
        for e in elec_plus:
            if e in net:
                rep.add(MINOR, "电解电容无防反接提示",
                        f"电池与电解电容 {e.split(':')[0]} 直接并联，"
                        f"建议串防反接二极管或确认极性一致", [b, e])
                return


def check_connector_unconnected(g: CircuitGraph, rep: ERCReport):
    """接插件/端子悬空（工业级：接口未接）。"""
    for pid, part in g.parts.items():
        ptype = part.get("type")
        if ptype not in ("screw_terminal", "conn_header", "usb_port", "barrel_jack"):
            continue
        pins = g.kb.get(ptype) or {}
        for pin in pins:
            ref = f"{pid}:{pin}"
            net = g.net_of(ref)
            if not net or len(net) == 1:
                rep.add(MINOR, "接插件引脚悬空",
                        f"{pid}（{ptype}）引脚 {pin} 未连接，接口可能未接线", [ref])


# =====================================================================
# 工业级电气规则扩展（2026-09-19：21 -> 33 条）
# 侧重额定值裕量、拓扑完整性与接口规范，教学场景宁可少报不误报。
# =====================================================================

# 电容类（耐压/滤波检查时一并覆盖电解）
_CAP_TYPES = {"capacitor", "electrolytic"}
# 运放类（含专用变体；通用 opamp 一并覆盖）
_OPAMP_TYPES = {
    "opamp", "opamp_inverting", "opamp_noninverting",
    "opamp_comparator", "opamp_follower",
}
# 连接器类（未接地检查）
_CONN_GND_TYPES = {"screw_terminal", "conn_header"}
# 运放电源脚名（目录当前未建模 VCC/GND，但自定义 KB / 后续扩展会用到）
_OPAMP_PWR_PIN_NAMES = ("VCC", "VDD", "V+", "VCC+", "V+IN")
_OPAMP_GND_PIN_NAMES = ("GND", "VSS", "VEE", "V-", "VCC-", "V-IN")


def _detect_supply_v(g: CircuitGraph) -> Optional[float]:
    """从电池/电源标记件的 attrs.value 检测供电电压（伏特）；检测不到返回 None。"""
    for pid, part in g.parts.items():
        if part.get("type") not in ("battery", "vcc", "power", "dc_power"):
            continue
        v, dim = parse_value((part.get("attrs") or {}).get("value"))
        if v is not None and dim == "V" and v > 0:
            return v
    return None


def _parse_explicit_voltage(value: Any) -> Optional[float]:
    """解析明确位于电压字段中的值；不从型号、描述或器件类型猜电压。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    parsed, dimension = parse_value(value)
    if parsed is None or parsed <= 0 or dimension not in ("V", ""):
        return None
    return parsed


def _first_explicit_voltage(attrs: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[float]:
    for key in keys:
        value = _parse_explicit_voltage(attrs.get(key))
        if value is not None:
            return value
    return None


def _i2c_devices_by_bus(g: CircuitGraph) -> Dict[Tuple[str, str], List[str]]:
    """按同一对 SCL/SDA 导线网络归组，只纳入两根线都已连接的器件。"""
    buses: Dict[Tuple[str, str], List[str]] = {}
    for pid, part in g.parts.items():
        pins = g.kb.get(part.get("type"), {})
        if "SCL" not in pins or "SDA" not in pins:
            continue
        scl_ref, sda_ref = f"{pid}:SCL", f"{pid}:SDA"
        scl_net, sda_net = g.net_of(scl_ref), g.net_of(sda_ref)
        if not scl_net or not sda_net:
            continue
        scl_root, sda_root = g.dsu.find(scl_ref), g.dsu.find(sda_ref)
        if scl_root == sda_root:
            continue
        buses.setdefault((scl_root, sda_root), []).append(pid)
    return buses


def _net_has_resistor_pullup(g: CircuitGraph, signal_net: List[str]) -> bool:
    """信号网络是否经正阻值电阻连接到明确 POWER/PWR_OUT 网络。"""
    for ref in signal_net:
        pid, _, pin = ref.partition(":")
        if g.parts.get(pid, {}).get("type") != "resistor":
            continue
        value, dimension = parse_value((g.parts[pid].get("attrs") or {}).get("value"))
        if value is None or value <= 0 or dimension not in ("Ω", ""):
            continue
        for other_pin in g.kb.get("resistor", {}):
            if other_pin == pin:
                continue
            other_net = g.net_of(f"{pid}:{other_pin}")
            if other_net and _net_has_type(g, other_net, PT.POWER, PT.PWR_OUT):
                return True
    return False


def check_i2c_pullups(g: CircuitGraph, rep: ERCReport):
    """共享 I²C 总线缺 SDA/SCL 上拉；单个未接外设不做推断，避免接口误报。"""
    for (scl_root, sda_root), devices in _i2c_devices_by_bus(g).items():
        if len(devices) < 2:
            continue
        missing = []
        if not _net_has_resistor_pullup(g, g.net_members[scl_root]):
            missing.append("SCL")
        if not _net_has_resistor_pullup(g, g.net_members[sda_root]):
            missing.append("SDA")
        if missing:
            pins = [f"{pid}:{line}" for pid in devices for line in missing]
            rep.add(
                WARNING,
                "I2C缺上拉",
                f"I2C 总线上的 {','.join(missing)} 未检测到连接至电源轨的上拉电阻；"
                f"涉及器件 {', '.join(devices)}。请确认板内或外设是否已有 2.2kΩ~10kΩ 上拉",
                pins,
            )


def _parse_i2c_address(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        address = value
    elif isinstance(value, str):
        try:
            address = int(value.strip(), 0)
        except (TypeError, ValueError):
            return None
    else:
        return None
    return address if 0 <= address <= 0x7F else None


def check_i2c_address_conflicts(g: CircuitGraph, rep: ERCReport):
    """同一 SDA/SCL 总线上显式声明的 7 位 I²C 地址重复。未知地址不猜。"""
    for _bus, devices in _i2c_devices_by_bus(g).items():
        addresses: Dict[int, List[str]] = {}
        for pid in devices:
            attrs = g.parts[pid].get("attrs") or {}
            address = _parse_i2c_address(attrs.get("i2c_address", attrs.get("address")))
            if address is not None:
                addresses.setdefault(address, []).append(pid)
        for address, conflicts in addresses.items():
            if len(conflicts) < 2:
                continue
            rep.add(
                MAJOR,
                "总线地址冲突",
                f"同一 I2C 总线上的 {', '.join(conflicts)} 均声明地址 0x{address:02X}；"
                "请修改地址选择脚、器件配置或拆分总线",
                [f"{pid}:SDA" for pid in conflicts] + [f"{pid}:SCL" for pid in conflicts],
            )


def _explicit_net_voltages(g: CircuitGraph, net: List[str]) -> List[Tuple[str, float]]:
    """读取网络上电源源头显式给出的电压；没有明确值就不返回。"""
    values: List[Tuple[str, float]] = []
    seen = set()
    for ref in net:
        if g.pin_ptype.get(ref) not in (PT.POWER, PT.PWR_OUT):
            continue
        pid = ref.split(":", 1)[0]
        if pid in seen:
            continue
        seen.add(pid)
        attrs = g.parts.get(pid, {}).get("attrs") or {}
        voltage = _first_explicit_voltage(
            attrs, ("output_voltage", "vout", "voltage", "value"))
        if voltage is not None:
            values.append((pid, voltage))
    return values


def check_power_rail_compatibility(g: CircuitGraph, rep: ERCReport):
    """按器件显式供电上下限检查其 PWR_IN 所在电源轨；缺参数时保守跳过。"""
    min_keys = ("supply_min_v", "vcc_min", "v_min", "voltage_min")
    max_keys = ("supply_max_v", "vcc_max", "v_max", "voltage_max")
    for pid, part in g.parts.items():
        attrs = part.get("attrs") or {}
        v_min = _first_explicit_voltage(attrs, min_keys)
        v_max = _first_explicit_voltage(attrs, max_keys)
        if v_min is None and v_max is None:
            continue
        supply_pins = [pin for pin, ptype in g.kb.get(part.get("type"), {}).items()
                       if ptype == PT.PWR_IN]
        reported = False
        for pin in supply_pins:
            ref = f"{pid}:{pin}"
            net = g.net_of(ref)
            if not net:
                continue
            for source_id, rail_v in _explicit_net_voltages(g, net):
                below = v_min is not None and rail_v < v_min
                above = v_max is not None and rail_v > v_max
                if not (below or above):
                    continue
                lower = f"{v_min:g}V" if v_min is not None else "未声明"
                upper = f"{v_max:g}V" if v_max is not None else "未声明"
                rep.add(
                    MAJOR,
                    "电源轨不兼容",
                    f"{pid} 供电脚 {pin} 接到 {source_id} 的 {rail_v:g}V 电源轨，"
                    f"超出器件显式供电范围 {lower}~{upper}；请改用兼容电源或电平转换方案",
                    [ref, source_id],
                )
                reported = True
                break
            if reported:
                break


def _cap_on_net(g: CircuitGraph, net: List[str], types=None) -> bool:
    """网络上是否存在电容引脚（默认含电解电容）。"""
    types = types or _CAP_TYPES
    for r in net:
        if g.parts.get(r.split(":", 1)[0], {}).get("type") in types:
            return True
    return False


def check_cap_voltage_rating(g: CircuitGraph, rep: ERCReport):
    """电容耐压不足（warning）：电源电压超过电容额定电压的 80%。

    额定电压取自 attrs.voltage / v_rating / value（须能 parse 出 V 量纲）；
    供电电压取电池 value，缺失时若有 POWER 引脚则按教学默认 5V 估算。
    解析不出额定值时静默跳过——宁可漏报，不可误报。"""
    v_supply = _detect_supply_v(g)
    if v_supply is None:
        if any(t == PT.POWER for t in g.pin_ptype.values()):
            v_supply = 5.0
        else:
            return
    for pid, part in g.parts.items():
        if part.get("type") not in _CAP_TYPES:
            continue
        attrs = part.get("attrs") or {}
        v_rate = None
        for key in ("voltage", "v_rating", "rated_voltage", "v_max", "vmax"):
            v, dim = parse_value(attrs.get(key))
            if v is not None and dim == "V" and v > 0:
                v_rate = v
                break
        if v_rate is None:
            v, dim = parse_value(attrs.get("value"))
            if v is not None and dim == "V" and v > 0:
                v_rate = v
        if v_rate is None:
            continue
        if v_supply > v_rate * 0.8:
            rep.add(WARNING, "电容耐压不足",
                    f"电容 {pid} 额定耐压 {v_rate:g}V，而供电约 {v_supply:g}V"
                    f"（已达额定的 {v_supply / v_rate * 100:.0f}%，超过 80% 安全裕量），"
                    f"建议选用耐压 ≥ {v_supply * 1.5:.0f}V 的电容",
                    [pid])


def check_resistor_power_limit(g: CircuitGraph, rep: ERCReport):
    """电阻功耗超限（warning）：估算 P=I²R，超过额定功耗的 80%。

    教学默认额定 0.25W（1/4W 碳膜）。电流估算（能算才算）：
      a) 电阻两端网络分别落在电源源头与地 → I = V/R（直跨供电）
      b) 电阻与 LED 同网（限流电阻）→ I ≈ (V − Vf)/R，Vf 取 2.0V
    估不出电流则跳过。0Ω 由 check_passive_values 单独 fatal，此处不重复。"""
    p_max_w = 0.25
    v_supply = _detect_supply_v(g) or 5.0
    for pid, part in g.parts.items():
        if part.get("type") != "resistor":
            continue
        ohms, dim = parse_value((part.get("attrs") or {}).get("value"))
        if ohms is None or ohms <= 0 or dim not in ("Ω", ""):
            continue
        n1, n2 = g.net_of(f"{pid}:1"), g.net_of(f"{pid}:2")
        if not n1 or not n2:
            continue
        i_est = None
        cross_pwr = (_net_has_type(g, n1, PT.POWER, PT.PWR_OUT) and _net_has_type(g, n2, PT.GND)) or \
                    (_net_has_type(g, n2, PT.POWER, PT.PWR_OUT) and _net_has_type(g, n1, PT.GND))
        if cross_pwr:
            i_est = v_supply / ohms
        else:
            for net in (n1, n2):
                for ref in net:
                    if g.parts.get(ref.split(":", 1)[0], {}).get("type") == "led":
                        if v_supply > 2.0:
                            i_est = (v_supply - 2.0) / ohms
                        break
                if i_est is not None:
                    break
        if i_est is None:
            continue
        p_w = i_est * i_est * ohms
        if p_w > p_max_w * 0.8:
            rep.add(WARNING, "电阻功耗超限",
                    f"电阻 {pid} 估算功耗约 {p_w * 1000:.0f}mW"
                    f"（I≈{i_est * 1000:.1f}mA，R={_fmt_ohm(ohms)}），"
                    f"超过额定 {p_max_w * 1000:.0f}mW 的 80%，建议换更大阻值或 1/2W 以上功率电阻",
                    [f"{pid}:1", f"{pid}:2"])


def check_led_parallel_shared_resistor(g: CircuitGraph, rep: ERCReport):
    """LED 并联共用电阻（major）：多个 LED 完全并联（+ 同网且 − 同网）却共用限流电阻。

    正确接法应为「每只 LED 串联独立限流电阻再并联」；共用一只电阻时
    因 VF 离散会导致电流分配极不均匀，先亮的抢电流、易烧毁。"""
    plus_groups: Dict[str, List[str]] = {}
    minus_groups: Dict[str, List[str]] = {}
    for pid, part in g.parts.items():
        if part.get("type") != "led":
            continue
        rp, rm = f"{pid}:+", f"{pid}:-"
        if g.net_of(rp):
            plus_groups.setdefault(g.dsu.find(rp), []).append(pid)
        if g.net_of(rm):
            minus_groups.setdefault(g.dsu.find(rm), []).append(pid)
    # 真正的并联：+ 同网 且 − 同网（两组交集）
    for root_p, leds_p in plus_groups.items():
        if len(leds_p) < 2:
            continue
        for root_m, leds_m in minus_groups.items():
            shared = [x for x in leds_p if x in leds_m]
            if len(shared) < 2:
                continue
            # 共享 + / − 网络上的电阻（限流电阻挂在公共节点上）
            net_p = g.net_of(f"{shared[0]}:+")
            net_m = g.net_of(f"{shared[0]}:-")
            shared_res = set()
            for net in (net_p, net_m):
                if not net:
                    continue
                for ref in net:
                    rid = ref.split(":", 1)[0]
                    if g.parts.get(rid, {}).get("type") == "resistor":
                        shared_res.add(rid)
            # 只要存在“挂在公共节点上的电阻”，即可认定共用；无电阻时
            # 由 check_led_current_limit 负责，这里不重复报。
            if shared_res:
                pins = [f"{led}:+" for led in sorted(shared)]
                rep.add(MAJOR, "LED并联共用电阻",
                        f"LED {'、'.join(sorted(shared))} 完全并联却共用电阻 "
                        f"{'、'.join(sorted(shared_res))} 作限流：因管压降离散，"
                        f"电流会集中到其中一只而烧毁；应改为每只 LED 串联独立限流电阻后再并联",
                        pins)
            break


def check_transistor_base_no_resistor(g: CircuitGraph, rep: ERCReport):
    """三极管基极无电阻（major）：NPN/PNP 的 B 极被导线直接接到电源，无限流。

    B-E 结是正向二极管，直连电源会瞬间过流烧毁发射结；教学电路必须
    串基极电阻（典型 1kΩ~10kΩ）。判定：B 所在导线网络里有 POWER 源头
    且网络上没有任何电阻（串了电阻后 B 与电源落在不同网络，天然不触发）。"""
    for pid, part in g.parts.items():
        if part.get("type") not in ("npn", "pnp"):
            continue
        bref = f"{pid}:B"
        net = g.net_of(bref)
        if not net:
            continue
        if not _net_has_type(g, net, PT.POWER):
            continue
        has_res = any(
            g.parts.get(r.split(":", 1)[0], {}).get("type") == "resistor"
            for r in net
        )
        if not has_res:
            rep.add(MAJOR, "基极无电阻",
                    f"{part.get('type').upper()} {pid} 的基极 B 被导线直接接到电源，"
                    f"没有基极限流电阻：B-E 结会过流烧毁，应串联 1kΩ~10kΩ 电阻",
                    [bref])


def check_opamp_power_missing(g: CircuitGraph, rep: ERCReport):
    """运放电源未接（major）：opamp 的 VCC/GND（或 V+/V−）引脚悬空。

    运放不供电则完全不工作；教学原理图常省略电源脚造成误以为“运放坏了”。
    仅当元件库/自定义 KB 为该运放定义了电源/地脚时才检查（当前目录的
    opamp_* 变体只有 +/−/out，本规则对它们自动跳过，不会误报）。"""
    for pid, part in g.parts.items():
        ptype = str(part.get("type") or "")
        if ptype not in _OPAMP_TYPES and "opamp" not in ptype.lower():
            continue
        pins = g.kb.get(ptype) or {}
        if not pins:
            continue
        pwr_pins = [p for p, t in pins.items()
                    if t in (PT.PWR_IN, PT.POWER) or p in _OPAMP_PWR_PIN_NAMES]
        gnd_pins = [p for p, t in pins.items()
                    if t == PT.GND or p in _OPAMP_GND_PIN_NAMES]
        if not pwr_pins and not gnd_pins:
            continue
        for pin in pwr_pins + gnd_pins:
            ref = f"{pid}:{pin}"
            net = g.net_of(ref)
            if not net or len(net) == 1:
                kind = "电源" if pin in pwr_pins else "地"
                rep.add(MAJOR, "运放电源未接",
                        f"运放 {pid} 的{kind}脚 {pin} 悬空，运放无法工作，"
                        f"请将 {pin} 接到 {'VCC/V+' if pin in pwr_pins else 'GND/V−'} 供电网络",
                        [ref])


def check_inductor_reverse_emf(g: CircuitGraph, rep: ERCReport):
    """电感反向电动势（warning）：电感两端没有并联续流二极管。

    与 check_inductive_flyback 的区别：那条按“电路里有没有开关器件 + 任意二极管”
    粗判；本条逐电感检查其【两端网络】上是否真的并了一只二极管（A/K 或 +/−
    分别落在电感两脚所在网络），更贴近工业续流回路的结构要求。"""
    for pid, part in g.parts.items():
        if part.get("type") != "inductor":
            continue
        r1, r2 = f"{pid}:1", f"{pid}:2"
        if not g.net_of(r1) or not g.net_of(r2):
            continue
        root1, root2 = g.dsu.find(r1), g.dsu.find(r2)
        has_flyback = False
        for dpid, dpart in g.parts.items():
            if dpart.get("type") not in FLYBACK_TYPES:
                continue
            for a, k in (("A", "K"), ("+", "-")):
                da, dk = f"{dpid}:{a}", f"{dpid}:{k}"
                if not g.net_of(da) or not g.net_of(dk):
                    continue
                if {g.dsu.find(da), g.dsu.find(dk)} == {root1, root2}:
                    has_flyback = True
                    break
            if has_flyback:
                break
        if not has_flyback:
            rep.add(WARNING, "电感反向电动势",
                    f"电感 {pid} 两端没有续流二极管：断开瞬间会产生反向电动势"
                    f"（V = −L·di/dt）击穿驱动管，应并联一只续流二极管（如 1N4148）",
                    [r1, r2])


def check_crystal_load_caps(g: CircuitGraph, rep: ERCReport):
    """晶振负载电容缺失（minor）：crystal 两端无匹配负载电容。

    皮尔斯振荡器要求晶振两脚各对地接一只负载电容（典型 10~22pF），
    缺失会导致不起振或频率偏移。任一端网络上没有电容即提示一次。"""
    for pid, part in g.parts.items():
        if part.get("type") not in ("crystal", "crystal_gnd"):
            continue
        missing = []
        for pin in ("1", "2"):
            ref = f"{pid}:{pin}"
            net = g.net_of(ref)
            if not net:
                continue
            if not _cap_on_net(g, net):
                missing.append(pin)
        if missing:
            rep.add(MINOR, "晶振负载电容缺失",
                    f"晶振 {pid} 的引脚 {'、'.join(missing)} 一侧未见负载电容，"
                    f"皮尔斯振荡器通常需在晶振两脚各对地接 10~22pF 电容，否则可能不起振",
                    [f"{pid}:{p}" for p in missing])


def check_regulator_filter_caps(g: CircuitGraph, rep: ERCReport):
    """稳压器输入输出电容缺失（warning）：7805 的 IN/OUT 无滤波电容。

    工业惯例：IN 就近 0.33µF、OUT 就近 0.1µF（或电解 + 瓷片组合），
    抑制自激与负载瞬态。逐脚独立判定；与 check_decoupling（只看 PWR_IN）
    互补——本条同时覆盖 OUT（PWR_OUT）供电轨。"""
    for pid, part in g.parts.items():
        if part.get("type") != "regulator_7805":
            continue
        for pin in ("IN", "OUT"):
            ref = f"{pid}:{pin}"
            net = g.net_of(ref)
            if not net:
                continue
            if _cap_on_net(g, net):
                continue
            tip = "输入侧建议就近 0.33µF" if pin == "IN" else "输出侧建议就近 0.1µF（或 10µF 电解）"
            rep.add(WARNING, "稳压器滤波电容缺失",
                    f"稳压器 {pid} 的 {pin} 脚所在网络未见滤波电容，"
                    f"易自激或负载瞬态跌落；{tip}",
                    [ref])


def check_connector_no_gnd(g: CircuitGraph, rep: ERCReport):
    """连接器未接地（minor）：screw_terminal/conn_header 有接线却无 GND 回路。

    外部接口若只引出信号/电源而没有地参考，上电后信号无法形成回路，
    也容易引入共模干扰。完全悬空的连接器由 check_connector_unconnected
    负责，本条只在“至少一脚已接线”的前提下检查是否共地。"""
    for pid, part in g.parts.items():
        if part.get("type") not in _CONN_GND_TYPES:
            continue
        pins = g.kb.get(part.get("type")) or {}
        has_wire = False
        has_gnd = False
        wired_pins = []
        for pin in pins:
            ref = f"{pid}:{pin}"
            net = g.net_of(ref)
            if not net or len(net) == 1:
                continue
            has_wire = True
            wired_pins.append(ref)
            if _net_has_type(g, net, PT.GND) or any(
                g.parts.get(r.split(":", 1)[0], {}).get("type") == "gnd"
                for r in net
            ):
                has_gnd = True
        if has_wire and not has_gnd:
            rep.add(MINOR, "连接器未接地",
                    f"连接器 {pid}（{part.get('type')}）已接线但未连接到 GND，"
                    f"外部接口缺少地参考，信号无法形成回路；请至少将一脚接到地网络",
                    wired_pins)


ALL_CHECKERS = [
    check_duplicate_ids,
    check_unknown_component,
    check_hallucinated_pins,
    check_connection_endpoints,
    check_short_circuit,
    check_led_current_limit,
    check_polarity,
    check_passive_values,
    check_net_contention,
    check_no_ground,
    check_floating_input,
    check_unconnected_part,
    check_inductive_flyback,
    check_decoupling,
    check_redundant_connection,
    check_single_node_net,
    check_ic_power_missing,
    check_multiple_battery,
    check_open_collector_output,
    check_diode_reverse_protection_missing,
    check_connector_unconnected,
    # —— 工业级电气规则扩展（21 -> 33） ——
    check_cap_voltage_rating,
    check_resistor_power_limit,
    check_led_parallel_shared_resistor,
    check_transistor_base_no_resistor,
    check_opamp_power_missing,
    check_inductor_reverse_emf,
    check_crystal_load_caps,
    check_regulator_filter_caps,
    check_connector_no_gnd,
    check_i2c_pullups,
    check_i2c_address_conflicts,
    check_power_rail_compatibility,
]  # 共 33 条检查规则





def run_erc(circuit_json: Dict[str, Any], kb: Dict[str, Dict[str, str]]) -> ERCReport:
    rep = ERCReport()
    try:
        g = CircuitGraph(circuit_json, kb)
        for chk in ALL_CHECKERS:
            chk(g, rep)
    except Exception as e:  # ERC 自身绝不能把服务搞崩，捕获后作为 fatal 返回
        rep.add(FATAL, "ERC异常", f"校验引擎出错：{e}")
    return rep


# =====================================================================
# 自测：python erc.py
# =====================================================================
if __name__ == "__main__":
    # 一个最小演示元件库：type -> {引脚: 电气类型}
    # 注意：led/buzzer 的 “+/−” 已改为 PASSIVE（负载不是信号输入、更不是地源头），
    # 与 catalog.py 的 2026-09-07 修正保持同步。
    KB = {
        "battery":  {"+": PT.POWER, "-": PT.GND},
        "vcc":      {"1": PT.POWER},
        "gnd":      {"1": PT.GND},
        "resistor": {"1": PT.PASSIVE, "2": PT.PASSIVE},
        "capacitor": {"+": PT.PASSIVE, "-": PT.PASSIVE},
        "inductor": {"1": PT.PASSIVE, "2": PT.PASSIVE},
        "led":      {"+": PT.PASSIVE, "-": PT.PASSIVE},
        "buzzer":   {"+": PT.PASSIVE, "-": PT.PASSIVE},
        "diode":    {"A": PT.PASSIVE, "K": PT.PASSIVE, "+": PT.PASSIVE, "-": PT.PASSIVE},
        "switch":   {"1": PT.PASSIVE, "2": PT.PASSIVE},
        "npn":      {"B": PT.IN, "C": PT.OUT, "E": PT.GND},
        "ne555":    {"GND": PT.GND, "TRIG": PT.IN, "OUT": PT.OUT, "RESET": PT.IN,
                     "CTRL": PT.IN, "THR": PT.IN, "DISCH": PT.OUT, "VCC": PT.PWR_IN},
    }

    print("== 正常电路：电池-电阻-LED-地（应通过） ==")
    ok = {
        "parts": [
            {"type": "battery", "id": "BAT1"},
            {"type": "resistor", "id": "R1"},
            {"type": "led", "id": "LED1"},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "R1:1", "red", []],
            ["R1:2", "LED1:+", "red", []],
            ["LED1:-", "GND1:1", "black", []],
            ["BAT1:-", "GND1:1", "black", []],
        ],
    }
    r = run_erc(ok, KB)
    print("passed =", r.passed, "| issues =", [(i.severity, i.rule) for i in r.issues])

    print("\n== 故障电路1：电源直接短路到地（应 fatal） ==")
    short = {"parts": [{"type": "battery", "id": "B1"}],
             "connections": [["B1:+", "B1:-", "red", []]]}
    print(run_erc(short, KB).to_dict()["fatal_errors"])

    print("\n== 故障电路2：LED 没串限流电阻（应 major） ==")
    nolim = {"parts": [{"type": "battery", "id": "B1"}, {"type": "led", "id": "L1"}, {"type": "gnd", "id": "G1"}],
             "connections": [["B1:+", "L1:+", "red", []], ["L1:-", "G1:1", "black", []], ["B1:-", "G1:1", "black", []]]}
    print(run_erc(nolim, KB).to_dict()["major_errors"])

    print("\n== 故障电路3：幻觉引脚（应 fatal） ==")
    hall = {"parts": [{"type": "resistor", "id": "R1"}, {"type": "led", "id": "L1"}],
            "connections": [["R1:999", "L1:+", "red", []]]}
    print(run_erc(hall, KB).to_dict()["fatal_errors"])

    print("\n== 故障电路4：重复位号（应 fatal）+ 未共地 ==")
    dupid = {"parts": [{"type": "battery", "id": "B1"}, {"type": "resistor", "id": "R1"},
                       {"type": "led", "id": "LED1"}, {"type": "resistor", "id": "R1"}],
             "connections": [["B1:+", "R1:1"], ["R1:2", "LED1:+"], ["LED1:-", "B1:-"]]}
    r4 = run_erc(dupid, KB)
    print("fatals =", [i.message for i in r4.fatals])

    print("\n== 故障电路5：感性负载缺续流 + 芯片缺去耦 + 未连接元件 ==")
    ind = {"parts": [{"type": "vcc", "id": "V1"}, {"type": "npn", "id": "Q1"}, {"type": "inductor", "id": "L1"},
                     {"type": "ne555", "id": "U1"}, {"type": "resistor", "id": "R9"}],
           "connections": [["V1:1", "L1:1"], ["L1:2", "Q1:C"], ["Q1:B", "U1:OUT"], ["U1:VCC", "V1:1"],
                           ["Q1:E", "U1:GND"]]}
    r5 = run_erc(ind, KB)
    print("rules =", [(i.severity, i.rule) for i in r5.issues])
    assert any(i.rule == "缺续流二极管" for i in r5.issues), "续流二极管检查未触发"
    assert any(i.rule == "缺去耦电容" for i in r5.issues), "去耦电容检查未触发"
    assert any(i.rule == "未连接元件" for i in r5.issues), "未连接元件检查未触发"

    print("\n== 故障电路6：LED 反接（应报“极性反接” major，绝不能再误报“电源短路” fatal） ==")
    rev = {"parts": [{"type": "battery", "id": "B1"}, {"type": "led", "id": "L1"}],
           "connections": [["B1:+", "L1:-", "red", []], ["L1:+", "B1:-", "black", []]]}
    r6 = run_erc(rev, KB)
    print("rules =", [(i.severity, i.rule) for i in r6.issues])
    assert any(i.rule == "极性反接" and i.severity == "major" for i in r6.issues), "极性反接未触发"
    assert not r6.fatals, "LED 反接不得误报 fatal（尤其“电源短路”），实际：" + \
                          str([i.message for i in r6.fatals])

    print("\n== 故障电路7：LED 串 1Ω 电阻（应 warning“LED限流电阻过小”） ==")
    low = {"parts": [{"type": "battery", "id": "B1"}, {"type": "resistor", "id": "R1", "attrs": {"value": "1Ω"}},
                     {"type": "led", "id": "L1"}, {"type": "gnd", "id": "G1"}],
           "connections": [["B1:+", "R1:1"], ["R1:2", "L1:+"], ["L1:-", "G1:1"], ["B1:-", "G1:1"]]}
    r7 = run_erc(low, KB)
    print("warnings =", [i.message for i in r7.warnings])
    assert any(i.rule == "LED限流电阻过小" for i in r7.warnings), "限流电阻过小未触发"
    assert not r7.fatals and not r7.majors, "串了电阻的 LED 电路不应再报 major/fatal"

    # ---- 盲区修复回归：LED 经晶体管驱动时的限流检查 ----
    print("\n== 故障电路7b：电源→NPN(C/E)→LED→地，全程无电阻（应 major“LED缺限流电阻”） ==")
    led_via_npn = {
        "parts": [{"type": "battery", "id": "B1", "attrs": {"value": "5V"}},
                  {"type": "npn", "id": "Q1"}, {"type": "led", "id": "L1"},
                  {"type": "gnd", "id": "G1"}],
        "connections": [["B1:+", "Q1:C"], ["Q1:E", "L1:+"],
                        ["L1:-", "G1:1"], ["B1:-", "G1:1"]]}
    r7b = run_erc(led_via_npn, KB)
    print("rules =", [(i.severity, i.rule) for i in r7b.issues])
    assert any(i.rule == "LED缺限流电阻" and i.severity == "major" for i in r7b.issues), \
        "经晶体管驱动的 LED 无限流电阻未触发（盲区回归）"

    print("\n== 故障电路7c：同上但 LED 串了 330Ω（正确电路，绝不可误报） ==")
    led_via_npn_ok = {
        "parts": [{"type": "battery", "id": "B1", "attrs": {"value": "5V"}},
                  {"type": "npn", "id": "Q1"},
                  {"type": "resistor", "id": "R1", "attrs": {"value": "330"}},
                  {"type": "led", "id": "L1"}, {"type": "gnd", "id": "G1"}],
        "connections": [["B1:+", "Q1:C"], ["Q1:E", "R1:1"], ["R1:2", "L1:+"],
                        ["L1:-", "G1:1"], ["B1:-", "G1:1"]]}
    r7c = run_erc(led_via_npn_ok, KB)
    assert not any(i.rule == "LED缺限流电阻" for i in r7c.issues), \
        "晶体管驱动 + LED 串限流电阻是标准电路，不得误报缺限流（防误报回归）"

    print("\n== 故障电路8：0Ω 电阻跨接电源两端（应 fatal“0Ω电阻短路”） ==")
    zero = {"parts": [{"type": "battery", "id": "B1"}, {"type": "resistor", "id": "R1", "attrs": {"value": "0Ω"}}],
            "connections": [["B1:+", "R1:1", "red", []], ["R1:2", "B1:-", "black", []]]}
    r8 = run_erc(zero, KB)
    print("fatals =", [i.message for i in r8.fatals])
    assert any(i.rule == "0Ω电阻短路" for i in r8.fatals), "0Ω 电阻跨电源未触发 fatal"

    print("\n== 故障电路9：二极管正向直跨电源（应 major“二极管正向直跨电源”） ==")
    di = {"parts": [{"type": "battery", "id": "B1"}, {"type": "diode", "id": "D1"}, {"type": "gnd", "id": "G1"}],
          "connections": [["B1:+", "D1:A", "red", []], ["D1:K", "G1:1", "black", []], ["B1:-", "G1:1", "black", []]]}
    r9 = run_erc(di, KB)
    print("majors =", [i.message for i in r9.majors])
    assert any(i.rule == "二极管正向直跨电源" for i in r9.majors), "二极管正向直跨电源未触发"

    print("\n== parse_value 参数解析 ==")
    for s in ["330Ω", "10k", "1kΩ", "4.7MΩ", "100nF", "10μF", "10uF", "47pF", "10mH", "0R", "5V", "红", "1N4148"]:
        print(f"  {s:>8} -> {parse_value(s)}")

    def _pv(s, want_v, want_dim):  # 浮点乘倍率有 1ulp 误差，按相对误差比较
        v, d = parse_value(s)
        assert d == want_dim, f"{s} 量纲 {d} != {want_dim}"
        assert v is not None and abs(v - want_v) <= abs(want_v) * 1e-9, f"{s} 数值 {v} != {want_v}"

    _pv("330Ω", 330.0, "Ω")
    _pv("10k", 10000.0, "Ω")
    _pv("1kΩ", 1000.0, "Ω")
    _pv("4.7MΩ", 4.7e6, "Ω")
    _pv("100nF", 1e-7, "F")
    _pv("10μF", 1e-5, "F")
    _pv("10uF", 1e-5, "F")
    _pv("47pF", 4.7e-11, "F")
    _pv("10mH", 0.01, "H")
    _pv("0R", 0.0, "Ω")
    _pv("5V", 5.0, "V")
    assert parse_value("红") == (None, "")
    assert parse_value("1N4148") == (None, "")

    print("\n== 三类正常教学拓扑回归（建模重构后必须全通过） ==")
    KB2 = {  # 与真实 CATALOG 同构的最小库（npn/pnp C/E 双 PASSIVE、7805 供电语义）
        "battery":  {"+": PT.POWER, "-": PT.GND},
        "vcc":      {"1": PT.POWER},
        "gnd":      {"1": PT.GND},
        "resistor": {"1": PT.PASSIVE, "2": PT.PASSIVE},
        "led":      {"+": PT.PASSIVE, "-": PT.PASSIVE},
        "npn":      {"B": PT.IN, "C": PT.PASSIVE, "E": PT.PASSIVE},
        "pnp":      {"B": PT.IN, "C": PT.PASSIVE, "E": PT.PASSIVE},
        "regulator_7805": {"IN": PT.PWR_IN, "GND": PT.GND, "OUT": PT.PWR_OUT},
        "capacitor": {"+": PT.PASSIVE, "-": PT.PASSIVE},
    }
    # 拓扑1：电池直供 + VCC 标记（第一批已修的标记件豁免，保持不回归）
    t1 = {"parts": [{"type": "battery", "id": "BAT1"}, {"type": "vcc", "id": "V1"},
                    {"type": "resistor", "id": "R1"}, {"type": "led", "id": "LED1"},
                    {"type": "gnd", "id": "G1"}],
          "connections": [["BAT1:+", "V1:1"], ["V1:1", "R1:1"], ["R1:2", "LED1:+"],
                          ["LED1:-", "G1:1"], ["BAT1:-", "G1:1"]]}
    r1 = run_erc(t1, KB2)
    assert not r1.fatals, f"电池+VCC 标记不得 fatal：{[i.message for i in r1.fatals]}"
    print("  拓扑1 电池+VCC标记      : 无 fatal ✔")
    # 拓扑2：7805 稳压输出接 VCC 轨（OUT 曾被当信号输出 → “网络竞争”fatal）
    t2 = {"parts": [{"type": "battery", "id": "BAT1"}, {"type": "regulator_7805", "id": "U1"},
                    {"type": "vcc", "id": "V1"}, {"type": "resistor", "id": "R1"},
                    {"type": "led", "id": "LED1"}, {"type": "gnd", "id": "G1"}],
          "connections": [["BAT1:+", "U1:IN"], ["U1:OUT", "V1:1"], ["V1:1", "R1:1"],
                          ["R1:2", "LED1:+"], ["LED1:-", "G1:1"],
                          ["BAT1:-", "G1:1"], ["U1:GND", "G1:1"]]}
    r2 = run_erc(t2, KB2)
    assert not r2.fatals and not r2.majors, \
        f"7805 OUT+VCC 轨不得 fatal/major：{[(i.severity, i.rule) for i in r2.issues]}"
    print("  拓扑2 7805 OUT+VCC 轨   : 无 fatal/major ✔")
    # 拓扑3：PNP 高边开关（E 接电源轨——此前 E=OUT 与电池+ 对撞 fatal）
    t3 = {"parts": [{"type": "battery", "id": "BAT1"}, {"type": "pnp", "id": "Q1"},
                    {"type": "resistor", "id": "R1"}, {"type": "led", "id": "LED1"},
                    {"type": "gnd", "id": "G1"}],
          "connections": [["BAT1:+", "Q1:E"], ["Q1:C", "R1:1"], ["R1:2", "LED1:+"],
                          ["LED1:-", "G1:1"], ["BAT1:-", "G1:1"]]}
    r3 = run_erc(t3, KB2)
    assert not r3.fatals and not r3.majors, \
        f"PNP 高边开关不得 fatal/major：{[(i.severity, i.rule) for i in r3.issues]}"
    print("  拓扑3 PNP 高边开关      : 无 fatal/major ✔")
    # 边界保持：双电池并联（两个真实电源）仍是“电源冲突” fatal —— 防止为放行而放水
    t4 = {"parts": [{"type": "battery", "id": "B1"}, {"type": "battery", "id": "B2"}],
          "connections": [["B1:+", "B2:+"], ["B1:-", "B2:-"]]}
    r4 = run_erc(t4, KB2)
    assert any(i.rule == "电源冲突" and i.severity == "fatal" for i in r4.issues), \
        "双电池并联必须仍报电源冲突 fatal"
    print("  边界  双电池并联        : 电源冲突 fatal 保持 ✔")
    # 无极性电容“接反”（+ 脚接地源头）从 major 降为 warning 提示
    t5 = {"parts": [{"type": "battery", "id": "B1"}, {"type": "capacitor", "id": "C1"},
                    {"type": "gnd", "id": "G1"}],
          "connections": [["B1:+", "C1:-"], ["C1:+", "G1:1"], ["B1:-", "G1:1"]]}
    r5 = run_erc(t5, KB2)
    assert any(i.rule == "电容极性提示" and i.severity == "warning" for i in r5.issues), \
        "电容 + 接低电位应降级为 warning 提示"
    assert not any(i.rule == "极性反接" for i in r5.issues), "电容不得再按 major“极性反接”误伤"
    assert not r5.fatals, "电容反接不得 fatal"
    print("  边界  电容极性提示      : warning 降级 ✔（major 误报消除）")

    print("\n== 检查器注册表 ==")
    print(f"  ALL_CHECKERS 共 {len(ALL_CHECKERS)} 条规则（21 -> 33）")
    assert len(ALL_CHECKERS) == 33, "总线与电压域扩展后应为 33 条检查规则"

    # ---- 工业级新增规则冒烟（2026-09-11） ----
    print("\n== 工业级新增规则冒烟 ==")
    KB3 = {  # 含运放电源脚 / 晶振 / 连接器 / 稳压器 的扩展库
        "battery":  {"+": PT.POWER, "-": PT.GND},
        "vcc":      {"1": PT.POWER},
        "gnd":      {"1": PT.GND},
        "resistor": {"1": PT.PASSIVE, "2": PT.PASSIVE},
        "capacitor": {"+": PT.PASSIVE, "-": PT.PASSIVE},
        "electrolytic": {"+": PT.PASSIVE, "-": PT.PASSIVE},
        "led":      {"+": PT.PASSIVE, "-": PT.PASSIVE},
        "inductor": {"1": PT.PASSIVE, "2": PT.PASSIVE},
        "diode":    {"A": PT.PASSIVE, "K": PT.PASSIVE},
        "npn":      {"B": PT.IN, "C": PT.PASSIVE, "E": PT.PASSIVE},
        "opamp":    {"+": PT.IN, "-": PT.IN, "out": PT.OUT,
                     "VCC": PT.PWR_IN, "GND": PT.GND},
        "crystal":  {"1": PT.PASSIVE, "2": PT.PASSIVE},
        "regulator_7805": {"IN": PT.PWR_IN, "GND": PT.GND, "OUT": PT.PWR_OUT},
        "screw_terminal": {"1": PT.PASSIVE, "2": PT.PASSIVE},
    }
    # a) 电容耐压不足：12V 供电 + 10V 电容
    cap_hi = {"parts": [
        {"type": "battery", "id": "B1", "attrs": {"value": "12V"}},
        {"type": "capacitor", "id": "C1", "attrs": {"value": "100nF", "voltage": "10V"}},
        {"type": "gnd", "id": "G1"}],
        "connections": [["B1:+", "C1:+"], ["C1:-", "G1:1"], ["B1:-", "G1:1"]]}
    ra = run_erc(cap_hi, KB3)
    assert any(i.rule == "电容耐压不足" and i.severity == "warning" for i in ra.issues), \
        "电容耐压不足未触发"
    print("  电容耐压不足           : warning ✔")
    # b) LED 并联共用电阻
    par = {"parts": [
        {"type": "battery", "id": "B1", "attrs": {"value": "5V"}},
        {"type": "resistor", "id": "R1", "attrs": {"value": "330"}},
        {"type": "led", "id": "L1"}, {"type": "led", "id": "L2"},
        {"type": "gnd", "id": "G1"}],
        "connections": [["B1:+", "R1:1"], ["R1:2", "L1:+"], ["R1:2", "L2:+"],
                        ["L1:-", "G1:1"], ["L2:-", "G1:1"], ["B1:-", "G1:1"]]}
    rb = run_erc(par, KB3)
    assert any(i.rule == "LED并联共用电阻" and i.severity == "major" for i in rb.issues), \
        "LED并联共用电阻未触发"
    print("  LED并联共用电阻        : major ✔")
    # c) 三极管基极直连电源
    bas = {"parts": [
        {"type": "battery", "id": "B1", "attrs": {"value": "5V"}},
        {"type": "npn", "id": "Q1"}, {"type": "gnd", "id": "G1"}],
        "connections": [["B1:+", "Q1:B"], ["Q1:E", "G1:1"], ["B1:-", "G1:1"]]}
    rc = run_erc(bas, KB3)
    assert any(i.rule == "基极无电阻" and i.severity == "major" for i in rc.issues), \
        "基极无电阻未触发"
    print("  三极管基极无电阻       : major ✔")
    # d) 运放电源悬空
    oa = {"parts": [
        {"type": "opamp", "id": "U1"}, {"type": "gnd", "id": "G1"}],
        "connections": [["U1:out", "G1:1"]]}
    rd = run_erc(oa, KB3)
    assert any(i.rule == "运放电源未接" and i.severity == "major" for i in rd.issues), \
        "运放电源未接未触发"
    print("  运放电源未接           : major ✔")
    # e) 电感无续流二极管
    ind2 = {"parts": [
        {"type": "battery", "id": "B1", "attrs": {"value": "5V"}},
        {"type": "inductor", "id": "L1"}, {"type": "gnd", "id": "G1"}],
        "connections": [["B1:+", "L1:1"], ["L1:2", "G1:1"], ["B1:-", "G1:1"]]}
    re_ = run_erc(ind2, KB3)
    assert any(i.rule == "电感反向电动势" and i.severity == "warning" for i in re_.issues), \
        "电感反向电动势未触发"
    print("  电感反向电动势         : warning ✔")
    # f) 晶振缺负载电容
    xt = {"parts": [
        {"type": "crystal", "id": "Y1"}, {"type": "vcc", "id": "V1"},
        {"type": "resistor", "id": "R1", "attrs": {"value": "1M"}}],
        "connections": [["Y1:1", "V1:1"], ["Y1:2", "R1:1"]]}
    rf = run_erc(xt, KB3)
    assert any(i.rule == "晶振负载电容缺失" and i.severity == "minor" for i in rf.issues), \
        "晶振负载电容缺失未触发"
    print("  晶振负载电容缺失       : minor ✔")
    # g) 7805 无输入输出电容
    reg = {"parts": [
        {"type": "battery", "id": "B1", "attrs": {"value": "12V"}},
        {"type": "regulator_7805", "id": "U1"}, {"type": "gnd", "id": "G1"},
        {"type": "vcc", "id": "V1"}],
        "connections": [["B1:+", "U1:IN"], ["U1:OUT", "V1:1"],
                        ["U1:GND", "G1:1"], ["B1:-", "G1:1"]]}
    rg = run_erc(reg, KB3)
    assert any(i.rule == "稳压器滤波电容缺失" and i.severity == "warning" for i in rg.issues), \
        "稳压器滤波电容缺失未触发"
    print("  稳压器滤波电容缺失     : warning ✔")
    # h) 连接器已接线但未接地
    cn = {"parts": [
        {"type": "screw_terminal", "id": "J1"}, {"type": "battery", "id": "B1"},
        {"type": "vcc", "id": "V1"}],
        "connections": [["J1:1", "B1:+"], ["J1:2", "V1:1"]]}
    rh = run_erc(cn, KB3)
    assert any(i.rule == "连接器未接地" and i.severity == "minor" for i in rh.issues), \
        "连接器未接地未触发"
    print("  连接器未接地           : minor ✔")
    # i) 电阻功耗超限：5V 直跨 10Ω → P=2.5W >> 0.2W
    pw = {"parts": [
        {"type": "battery", "id": "B1", "attrs": {"value": "5V"}},
        {"type": "resistor", "id": "R1", "attrs": {"value": "10Ω"}},
        {"type": "gnd", "id": "G1"}],
        "connections": [["B1:+", "R1:1"], ["R1:2", "G1:1"], ["B1:-", "G1:1"]]}
    ri = run_erc(pw, KB3)
    assert any(i.rule == "电阻功耗超限" and i.severity == "warning" for i in ri.issues), \
        "电阻功耗超限未触发"
    print("  电阻功耗超限           : warning ✔")

    print("\n== 修复提示词示例 ==")
    print(run_erc(rev, KB).to_repair_prompt())
