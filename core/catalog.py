# -*- coding: utf-8 -*-
"""
catalog.py —— 智绘电路元件目录与匹配器（Python 标准库，零依赖）
=====================================================================
设计要点：
 * 每个元件带 aliases 别名表供模糊匹配 + OOD（分布外）拦截思想：
   相似度不足不硬凑，交给上层兜底/拦截，宁缺毋滥；
 * 数据驱动：位号前缀、符号尺寸、引脚端口全部收敛在 CATALOG 一处。
并针对开发中遇到的真实问题做了强化 ——
「同相放大器 / 电压比较器 被误匹配到 反相运放模板」：
本文件用【正向别名 aliases + 负向关键词 negative】双表机制，匹配时先做负向排除，
从根上避免宽关键词误伤。匹配只用标准库 difflib（你项目本来就用 difflib，不引新依赖）。

一份 CATALOG 同时供：
  1) 后端中文需求 -> 元件类型匹配（match_components）
  2) 模板路由（match_template，含负向排除）
  3) 生成 ERC 的引脚电气类型表（build_erc_kb）
  4) 下发给前端做符号/引脚单一数据源（to_frontend_manifest）
"""

import functools
import re
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable

# 引脚电气类型（与 erc.py 保持一致）
POWER, GND, OUT, IN, PASSIVE = "POWER", "GND", "OUT", "IN", "PASSIVE"
PWR_IN = "PWR_IN"    # 芯片供电引脚（只取电不供电），与电源正同网络不算多源竞争
PWR_OUT = "PWR_OUT"  # 供电输出（稳压器/电源芯片的输出脚）：驱动的是供电轨而非
                     # 信号，与电源正/标记件同网络属正常供电，不参与信号对撞
                     # （此前 7805 OUT 记为 OUT 信号输出，接 VCC 轨
                     #  被判“网络竞争 fatal”）


# =====================================================================
# 元件目录：新增元件 = 在这里加一条记录即可，匹配/ERC/前端都会自动带上
# pins: {引脚名: 电气类型}；aliases: 中文/英文/口语别名；negative: 出现时排除本元件
# =====================================================================
CATALOG = {
    # ---------- 无源 ----------
    "resistor": {
        "ref": "R", "name": "电阻", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["电阻", "电阻器", "resistor", "限流电阻", "分压电阻", "上拉电阻", "下拉电阻"],
        "negative": ["光敏", "热敏", "压敏", "可变", "可调", "电位器"], "default": "10k",
    },
    "capacitor": {
        "ref": "C", "name": "电容", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["电容", "电容器", "capacitor", "cap", "电解电容", "瓷片电容", "滤波电容", "旁路电容", "去耦电容", "滤波"],
        "negative": [], "default": "100nF",
    },
    "inductor": {
        "ref": "L", "name": "电感", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["电感", "电感器", "线圈", "inductor", "扼流圈"],
        "negative": [], "default": "10mH",
    },
    "potentiometer": {
        "ref": "RV", "name": "电位器", "pins": {"1": PASSIVE, "2": PASSIVE, "3": PASSIVE},
        "aliases": ["电位器", "可变电阻", "滑动变阻器", "potentiometer", "pot", "可调电阻"],
        "negative": [], "default": "10k",
    },
    # ---------- 半导体 ----------
    # 注（已知不忠实，本轮不修以免改变输出）：
    # 整流二极管把 1N4148（小信号/续流，100V/200mA）与 1N4007（整流，1000V/1A）
    # 收进同一类型的 aliases，default 固定为 "1N4148"。因此用户写“1N4007”时，
    # 生成电路的器件值仍会落到 1N4148——类型判定正确，但具体型号不忠实。两者
    # 在教学中常互作续流二极管（本项目主要用途），影响有限；要真正区分需把
    # default 改为「按需求回填」并在 net/报告里透传，那会改变现有网表与快照
    # 输出（属行为变更），留作后续单独立项。
    "diode": {
        "ref": "D", "name": "二极管", "pins": {"A": PASSIVE, "K": PASSIVE, "+": PASSIVE, "-": PASSIVE},
        "aliases": ["二极管", "diode", "整流管", "1n4148", "1n4007"],
        "negative": ["发光", "稳压", "齐纳", "led", "zener",
                     "肖特基", "光电", "光敏", "整流桥"], "default": "1N4148",
    },
    "zener": {
        "ref": "D", "name": "稳压二极管", "pins": {"A": PASSIVE, "K": PASSIVE, "+": PASSIVE, "-": PASSIVE},
        "aliases": ["稳压二极管", "齐纳二极管", "稳压管", "齐纳管", "齐纳", "zener", "1n4733"],
        "negative": ["发光", "led", "整流", "三端稳压", "7805",
                     "肖特基", "光电", "光敏"], "default": "5.1V",
    },
    "led": {
        # 2026-09-07 修复：原来把 "+" 记成 IN、"-" 记成 GND，导致 LED 反接（"+" 接电池负、
        # "-" 接电池正）时，"-" 脚被当成“地源头”跟电源正同网络，ERC 误报 fatal“电源短路”。
        # LED 是负载不是信号输入，也不是地源头 —— 两个脚都用 PASSIVE；
        # 极性对错交给 erc.check_polarity 基于网络构成去判（引脚名 "+"/"-" 保持不变，
        # 前端 symbols.js 的 manifest 一致性测试依赖引脚名集合）。
        "ref": "LED", "name": "发光二极管", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["发光二极管", "led", "灯", "指示灯", "发光管"],
        "negative": ["数码管", "点阵"], "default": "红",
    },
    "npn": {
        # C/E 改双 PASSIVE——晶体管的 C/E 电气方向由电路决定
        # （低边开关 C 下拉、高边/跟随器 E 输出、发射极电阻等），把「角色」
        # 预设成 GND/OUT 会把 PNP 高边开关（E 接电源轨）误判“网络竞争 fatal”、
        # 在发射极电阻场景伪造地源头。基极 B 是高阻输入，保持 IN。
        "ref": "Q", "name": "NPN三极管", "pins": {"B": IN, "C": PASSIVE, "E": PASSIVE},
        "aliases": ["npn", "npn三极管", "npn晶体管", "三极管", "晶体管", "9013", "2n3904", "s8050"],
        "negative": ["pnp"], "default": "2N3904",
    },
    "pnp": {
        # 与 npn 同理：C/E 双 PASSIVE（此前 E=OUT 与电源轨同网即 fatal）
        "ref": "Q", "name": "PNP三极管", "pins": {"B": IN, "C": PASSIVE, "E": PASSIVE},
        "aliases": ["pnp", "pnp三极管", "pnp晶体管", "三极管", "晶体管", "9012", "2n3906", "s8550"],
        "negative": ["npn"], "default": "2N3906",
    },
    "nmos": {
        "ref": "Q", "name": "N沟道MOS管", "pins": {"G": IN, "D": PASSIVE, "S": PASSIVE},
        "aliases": ["nmos", "n沟道mos", "nmos管", "mos管", "场效应管", "mosfet"],
        "negative": ["pmos", "p沟道"], "default": "IRF540",
    },
    "fuse": {
        "ref": "F", "name": "保险丝", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["保险丝", "熔断器", "自恢复保险丝", "fuse"],
        "negative": [], "default": "1A",
    },
    "crystal": {
        "ref": "Y", "name": "晶振", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["晶振", "晶体振荡器", "石英晶振", "crystal", "xtal"],
        "negative": [], "default": "12MHz",
    },
    # ---------- 电源 / 源 ----------
    "battery": {
        "ref": "BAT", "name": "电池/直流源", "pins": {"+": POWER, "-": GND},
        "aliases": ["电池", "电源", "直流源", "电压源", "battery", "dc", "供电"],
        "negative": ["交流", "ac"], "default": "5V",
    },
    "vcc": {
        "ref": "VCC", "name": "电源正", "pins": {"1": POWER},
        "aliases": ["vcc", "vcc电源", "电源正", "电源正极", "正电源", "+5v"],
        "negative": [], "default": "5V",
    },
    "gnd": {
        "ref": "GND", "name": "接地", "pins": {"1": GND},
        "aliases": ["地", "接地", "gnd", "ground", "公共地"], "negative": [], "default": "",
    },
    # ---------- 运放（关键：用 negative 区分三种放大器，解决误匹配） ----------
    "opamp_inverting": {
        "ref": "U", "name": "反相比例放大器", "pins": {"+": IN, "-": IN, "out": OUT},
        "aliases": ["反相放大器", "反相比例放大器", "反相比例运算", "反相放大", "inverting amplifier", "inverting",
                    "运放", "运算放大器", "opamp", "operational amplifier"],
        # 只要出现“同相/比较器/电压跟随”就绝不能匹配到反相模板
        "negative": ["同相", "非反相", "比较器", "比较", "跟随", "non-inverting", "noninverting", "comparator", "buffer"],
        "default": "",
    },
    "opamp_noninverting": {
        "ref": "U", "name": "同相比例放大器", "pins": {"+": IN, "-": IN, "out": OUT},
        "aliases": ["同相放大器", "同相比例放大器", "同相放大", "非反相放大器", "non-inverting", "noninverting amplifier"],
        "negative": ["反相", "比较器", "跟随"], "default": "",
    },
    "opamp_comparator": {
        "ref": "U", "name": "电压比较器", "pins": {"+": IN, "-": IN, "out": OUT},
        "aliases": ["电压比较器", "比较器", "比较电路", "comparator", "过零比较"],
        "negative": ["放大", "比例", "跟随"], "default": "",
    },
    "opamp_follower": {
        "ref": "U", "name": "电压跟随器", "pins": {"+": IN, "-": IN, "out": OUT},
        "aliases": ["电压跟随器", "跟随器", "缓冲器", "buffer", "unity gain", "射极跟随"],
        "negative": ["反相", "同相比例", "比较"], "default": "",
    },
    # ---------- 功能电路 / 芯片（9.5 前要补的方向） ----------
    "buzzer": {
        # 2026-09-07 修复：与 LED 同理，蜂鸣器是有极性的负载（不是信号输入、不是地源头），
        # 原来 "-" 记成 GND 会把蜂鸣器反接误报成“电源短路”，改双 PASSIVE，
        # 反接交给 erc.check_polarity 判定。
        "ref": "BZ", "name": "蜂鸣器", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["蜂鸣器", "喇叭", "buzzer", "beep", "报警器"], "negative": ["扬声器", "speaker"], "default": "有源",
    },
    "speaker": {
        # 2026-09-09 新增（元件库扩展协议首个示范，工具链见 symbol_import.py）：
        # 扬声器与蜂鸣器同为无极性语义的负载件，引脚双 PASSIVE（反接交给
        # check_polarity）。“喇叭”一词两义：留在 buzzer 别名表（先到先得），
        # speaker 用“扬声器/speaker”专用词 + 负向词避免误伤。
        "ref": "LS", "name": "扬声器", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["扬声器", "speaker", "音箱", "喇叭音响"],
        "negative": ["蜂鸣器", "beep", "有源蜂鸣", "报警器"], "default": "8Ω",
    },
    "regulator_7805": {
        # 引脚电气类型按真实器件校准——IN 是供电输入
        # （PWR_IN，去耦电容检查据此定位供电网）、OUT 是供电输出（PWR_OUT，
        # 驱动 VCC 轨属正常供电而非信号输出）、GND 不变。
        "ref": "U", "name": "7805稳压", "pins": {"IN": PWR_IN, "GND": GND, "OUT": PWR_OUT},
        "aliases": ["7805", "稳压器", "三端稳压", "lm7805", "5v稳压", "线性稳压", "稳压芯片", "三端稳压器"],
        "negative": ["开关电源", "buck", "boost", "lm2596"], "default": "LM7805",
    },
    "ne555": {
        # 2026-09-07 修复：DISCH（放电脚）是集电极开路【输出】，原来误记为 IN，
        # 会让“定时电阻下端接 DISCH”的正常多谐振荡器被当成输入冲突场景。
        "ref": "U", "name": "555定时器",
        "pins": {"GND": GND, "TRIG": IN, "OUT": OUT, "RESET": IN, "CTRL": IN, "THR": IN, "DISCH": OUT, "VCC": PWR_IN},
        "aliases": ["555", "ne555", "555定时器", "555振荡", "多谐振荡器", "单稳态", "astable", "定时器芯片"],
        "negative": [], "default": "NE555",
    },
    "switch": {
        "ref": "SW", "name": "开关", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["开关", "switch", "拨动开关", "spst"],
        "negative": ["按钮", "按键", "button", "轻触"], "default": "SPST",
    },
    "pushbutton": {
        "ref": "SW", "name": "按钮", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["按钮", "按键", "轻触开关", "button", "push button", "pushbutton", "微动"],
        "negative": ["拨动"], "default": "NO",
    },
    # ---------- 2026-09-09 常用元件扩展批（扩展协议见 symbol_import.py；
    # 每种元件六处同步：CATALOG / PART_INFO / web/symbols.js / kicad/symbols.py /
    # TYPE_MAP / manifest，测试对全部同步点做一致性断言） ----------
    "pmos": {
        # 与 nmos 对偶：PMOS 导通逻辑相反，别名带 p 前缀，负向词互斥
        "ref": "Q", "name": "P沟道MOS管", "pins": {"G": IN, "D": PASSIVE, "S": PASSIVE},
        "aliases": ["pmos", "p沟道mos", "pmos管", "p沟道场效应管", "irf9540"],
        "negative": ["nmos", "n沟道"], "default": "IRF9540",
    },
    "schottky": {
        # 肖特基二极管：低压降快恢复，常用于续流/防反接；引脚命名与 diode 一致
        "ref": "D", "name": "肖特基二极管", "pins": {"A": PASSIVE, "K": PASSIVE, "+": PASSIVE, "-": PASSIVE},
        "aliases": ["肖特基二极管", "肖特基", "肖特基管", "schottky", "1n5819", "ss34"],
        "negative": ["发光", "稳压", "齐纳", "led", "zener", "光电", "光敏", "整流桥"],
        "default": "1N5819",
    },
    "photodiode": {
        # 光电二极管：正常用法是反偏（K 接高电位），"电阻"作负向词与光敏电阻区分
        "ref": "D", "name": "光电二极管", "pins": {"A": PASSIVE, "K": PASSIVE, "+": PASSIVE, "-": PASSIVE},
        "aliases": ["光电二极管", "光敏二极管", "photodiode", "光电池"],
        "negative": ["发光", "led", "稳压", "齐纳", "电阻"], "default": "BPW34",
    },
    "photocell": {
        # 光敏电阻（LDR）：无极性双引脚，"二极管"作负向词与光敏二极管区分
        "ref": "RG", "name": "光敏电阻", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["光敏电阻", "光敏电阻器", "ldr", "photocell", "光敏传感器", "光控", "光感"],
        "negative": ["二极管", "发光", "led"], "default": "GL5516",
    },
    "thermistor": {
        "ref": "TH", "name": "热敏电阻", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["热敏电阻", "ntc", "ptc", "thermistor", "温度传感器", "测温电阻", "温度"],
        "negative": [], "default": "10k",
    },
    "ac_source": {
        # 交流源与 battery（直流）互为负向；POWER/GND 引脚语义与 battery 相同
        "ref": "VAC", "name": "交流电源", "pins": {"+": POWER, "-": GND},
        "aliases": ["交流电源", "交流源", "交流电", "市电", "ac", "ac source"],
        "negative": ["直流", "电池", "dc"], "default": "220V",
    },
    "dc_motor": {
        # 直流电机是有极性的负载（转向相关），双 PASSIVE + check_polarity 判定，
        # 与 led/buzzer 同一处理模式；"舵机/伺服"作负向词与 servo 区分
        "ref": "M", "name": "直流电机", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["直流电机", "马达", "直流马达", "电机", "dc motor", "motor", "小电机"],
        "negative": ["舵机", "servo", "步进", "stepper", "伺服"], "default": "3V",
    },
    "servo": {
        # 舵机三线的电气语义与 ne555 同模式：VCC 取电（PWR_IN）、GND 回流、SIG 输入
        "ref": "M", "name": "舵机", "pins": {"VCC": PWR_IN, "GND": GND, "SIG": IN},
        "aliases": ["舵机", "servo", "sg90", "mg996r", "伺服电机", "伺服马达"],
        "negative": ["步进", "stepper"], "default": "SG90",
    },
    "relay": {
        # 单刀单掷继电器（线圈 + com/no 触点），引脚全 PASSIVE：
        # 线圈不是信号输入、触点不是输出，反接/方向交给具体电路语义
        "ref": "K", "name": "继电器",
        "pins": {"coil+": PASSIVE, "coil-": PASSIVE, "com": PASSIVE, "no": PASSIVE},
        "aliases": ["继电器", "relay", "电磁继电器"],
        "negative": [], "default": "5V",
    },
    "optocoupler": {
        # 光耦（PC817 类）：LED 侧 A/K 无源；光敏三极管侧 C 改 PASSIVE
        # （2026-09-10 与 npn/pnp 同一建模修正：集电极开路输出允许多路
        #  wired-AND 共线，预设 OUT 会把合法共集电极接法误判“输出冲突”）
        "ref": "U", "name": "光耦", "pins": {"A": PASSIVE, "K": PASSIVE, "E": PASSIVE, "C": PASSIVE},
        "aliases": ["光耦", "光电耦合器", "光耦隔离", "optocoupler", "pc817", "el817"],
        "negative": [], "default": "PC817",
    },
    "bridge_rectifier": {
        "ref": "BR", "name": "整流桥", "pins": {"AC1": PASSIVE, "AC2": PASSIVE, "+": PASSIVE, "-": PASSIVE},
        "aliases": ["整流桥", "桥式整流", "整流桥堆", "桥堆", "bridge rectifier", "整流"],
        "negative": [], "default": "KBP210",
    },
    "screw_terminal": {
        "ref": "J", "name": "接线端子", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["接线端子", "接线柱", "端子台", "terminal", "screw terminal", "接插件"],
        "negative": [], "default": "2P",
    },
    # ---------- 2026-09-11 工业级器件库扩容（10 种教学高频件） ----------
    "electrolytic": {
        # 电解电容有极性：+ 接高电位，- 接低电位；与 capacitor 区分（无极性）
        "ref": "C", "name": "电解电容",
        "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["电解电容", "电解", "有极电容", "铝电解", "electrolytic", "电解电容器"],
        "negative": ["无极", "陶瓷电容", "瓷片"], "default": "10µF",
    },
    "tvs": {
        # TVS 瞬态抑制：双极性保护，A/K 与二极管同，负向与普通二极管/齐纳区分
        "ref": "D", "name": "TVS二极管",
        "pins": {"A": PASSIVE, "K": PASSIVE, "+": PASSIVE, "-": PASSIVE},
        "aliases": ["tvs", "TVS", "瞬态抑制", "瞬态抑制二极管", "tvs管", "防浪涌"],
        "negative": ["齐纳", "稳压二极管", "普通二极管"], "default": "SMBJ5.0A",
    },
    "phototransistor": {
        # 光敏三极管：C/E 无源（与 npn/pnp 同建模纪律），B 可选悬空或接
        "ref": "Q", "name": "光敏三极管",
        "pins": {"C": PASSIVE, "E": PASSIVE},
        "aliases": ["光敏三极管", "光敏管", "光电三极管", "phototransistor"],
        "negative": ["光敏电阻", "光耦", "photodiode", "光电二极管"], "default": "3DU5",
    },
    "opamp": {
        # 通用运放：与四个专用变体同引脚，便于 LLM 直接生成未指定拓扑的运放
        "ref": "U", "name": "通用运放",
        "pins": {"+": IN, "-": IN, "out": OUT},
        # 「运算放大器」含「放大器」，会使负向词「放大器」被 _neg_list 剔除；
        # 故别名只保留「运放/通用运放」，避免抢走反相/同相的澄清候选
        "aliases": ["运放", "通用运放", "opamp", "op-amp", "lm358", "ua741"],
        "negative": ["比较器", "跟随器", "同相", "反相", "放大器"], "default": "LM358",
    },
    "ferrite_bead": {
        "ref": "FB", "name": "磁珠",
        "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["磁珠", "铁氧体磁珠", "ferrite", "ferrite bead", "滤波磁珠"],
        "negative": ["电感", "电阻"], "default": "600Ω",
    },
    "varistor": {
        "ref": "RV", "name": "压敏电阻",
        "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["压敏电阻", "压敏", "varistor", "mov", "防雷压敏"],
        "negative": ["热敏", "电位器", "光敏"], "default": "10D471K",
    },
    "scr": {
        # 单向晶闸管：A 阳极 / K 阴极 / G 门极，全 PASSIVE（触发方向由电路决定）
        "ref": "VS", "name": "晶闸管",
        "pins": {"A": PASSIVE, "K": PASSIVE, "G": PASSIVE},
        "aliases": ["晶闸管", "可控硅", "scr", "单向可控硅", "thyristor"],
        "negative": ["三极管", "mos", "二极管"], "default": "BT151",
    },
    "buzzer_passive": {
        # 无源蜂鸣器需方波驱动；与有源 buzzer 区分
        "ref": "BZ", "name": "无源蜂鸣器",
        "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["无源蜂鸣器", "无源蜂鸣", "passive buzzer", "无源喇叭"],
        "negative": ["有源蜂鸣器", "扬声器", "speaker"], "default": "无源",
    },
    "ammeter": {
        # 电流表串联：双 PASSIVE，教学展示用
        "ref": "PA", "name": "电流表",
        "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["电流表", "电流计", "ammeter", "安培表", "测电流"],
        "negative": ["电压表", "multimeter"], "default": "mA",
    },
    "stepper": {
        "ref": "M", "name": "步进电机",
        "pins": {"A+": PASSIVE, "A-": PASSIVE, "B+": PASSIVE, "B-": PASSIVE},
        "aliases": ["步进电机", "步进", "stepper", "28byj", "四相步进", "减速步进"],
        "negative": ["直流电机", "舵机", "servo"], "default": "28BYJ-48",
    },
        "capacitor_trim": {
        "ref": "C", "name": "微调电容", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["微调电容", "半可变电容", "可调电容", "trimmer", "微调"],
        "negative": ["可变电容", "电解电容"], "default": "5-30pF",
    },
    "capacitor_variable": {
        "ref": "C", "name": "可变电容", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["可变电容", "调谐电容", "空气可变电容", "variable capacitor"],
        "negative": ["微调电容", "电解电容"], "default": "365pF",
    },
    "capacitor_feedthrough": {
        "ref": "C", "name": "穿心电容", "pins": {"1": PASSIVE, "2": PASSIVE, "3": PASSIVE},
        "aliases": ["穿心电容", "穿芯电容", "feedthrough", "滤波穿心"],
        "negative": ["普通电容", "电解"], "default": "100pF",
    },
    "crystal_gnd": {
        "ref": "Y", "name": "接地晶振", "pins": {"1": PASSIVE, "2": PASSIVE, "3": GND, "4": GND},
        "aliases": ["接地晶振", "屏蔽晶振", "四脚晶振", "带地晶振"],
        "negative": ["两脚晶振", "谐振器"], "default": "12MHz",
    },
    "circuit_breaker_1p": {
        "ref": "QF", "name": "单极断路器", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["单极断路器", "1p空开", "微型断路器", "空开", "微断"],
        "negative": ["双极", "三极", "保险丝"], "default": "C16",
    },
    "circuit_breaker_2p": {
        "ref": "QF", "name": "双极断路器", "pins": {"1": PASSIVE, "2": PASSIVE, "3": PASSIVE, "4": PASSIVE},
        "aliases": ["双极断路器", "2p空开", "双极空开"],
        "negative": ["单极", "三极"], "default": "C16/2P",
    },
    "circuit_breaker_3p": {
        "ref": "QF", "name": "三极断路器", "pins": {"1": PASSIVE, "2": PASSIVE, "3": PASSIVE, "4": PASSIVE, "5": PASSIVE, "6": PASSIVE},
        "aliases": ["三极断路器", "3p空开", "三相空开", "三相断路"],
        "negative": ["单极", "双极"], "default": "C16/3P",
    },
    "capacitor_network": {
        "ref": "CN", "name": "电容排", "pins": {"1": PASSIVE, "2": PASSIVE, "3": PASSIVE, "4": PASSIVE, "com": PASSIVE},
        "aliases": ["电容排", "电容阵列", "cap network", "电容网络"],
        "negative": ["排阻", "单电容"], "default": "100nF×4",
    },
    "antenna_dipole": {
        "ref": "AE", "name": "偶极天线", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["偶极天线", "半波天线", "dipole", "偶极子天线"],
        "negative": ["棒状天线", "环形天线"], "default": "λ/2",
    },
    "battery_cell_1v5": {
        "ref": "BT", "name": "1.5V电池", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["1.5v电池", "五号电池", "七号电池", "aa电池", "aaa电池", "干电池"],
        "negative": ["锂电池", "9v电池"], "default": "1.5V",
    },
    "voltmeter": {
        "ref": "PV", "name": "电压表",
        "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["电压表", "电压计", "voltmeter", "伏特表", "测电压"],
        "negative": ["电流表", "multimeter"], "default": "V",
    },
    "capacitor_ceramic": {
        "ref": "C", "name": "瓷片电容", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["瓷片电容", "陶瓷电容", "瓷片", "ceramic capacitor", "陶瓷"],
        "negative": ["电解", "钽电容"], "default": "100nF",
    },
    "capacitor_film": {
        "ref": "C", "name": "薄膜电容", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["薄膜电容", "涤纶电容", "film capacitor", "薄膜"],
        "negative": ["电解", "瓷片"], "default": "1µF",
    },
    "capacitor_tantalum": {
        "ref": "C", "name": "钽电容", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["钽电容", "钽", "tantalum", "贴片钽电容"],
        "negative": ["铝电解", "瓷片"], "default": "10µF",
    },
    "resistor_network": {
        "ref": "RN", "name": "排阻", "pins": {"1": PASSIVE, "2": PASSIVE, "3": PASSIVE, "com": PASSIVE},
        "aliases": ["排阻", "电阻排", "resistor network", "排阻网络", "阻排"],
        "negative": [], "default": "10k×8",
    },
    "inductor_toroid": {
        "ref": "L", "name": "环形电感", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["环形电感", "磁环电感", "toroid", "环形线圈"],
        "negative": ["磁珠", "变压器"], "default": "100µH",
    },
    "transformer": {
        "ref": "T", "name": "变压器", "pins": {"p1": PASSIVE, "p2": PASSIVE, "s1": PASSIVE, "s2": PASSIVE},
        "aliases": ["变压器", "电源变压器", "transformer", "工频变压器"],
        "negative": ["电感", "互感器"], "default": "220V-12V",
    },
    "led_rgb": {
        "ref": "LED", "name": "RGB灯珠", "pins": {"R": PASSIVE, "G": PASSIVE, "B": PASSIVE, "com": PASSIVE},
        "aliases": ["rgb led", "RGB灯", "全彩LED", "rgb灯珠", "三色灯"],
        "negative": ["单色", "白光"], "default": "共阴",
    },
    "diode_rectifier": {
        "ref": "D", "name": "整流二极管", "pins": {"A": PASSIVE, "K": PASSIVE, "+": PASSIVE, "-": PASSIVE},
        "aliases": ["整流二极管", "整流管", "1n4007", "rectifier diode", "硅整流"],
        "negative": ["肖特基", "齐纳", "LED"], "default": "1N4007",
    },
    "triac": {
        "ref": "VS", "name": "双向可控硅", "pins": {"A1": PASSIVE, "A2": PASSIVE, "G": PASSIVE},
        "aliases": ["双向可控硅", "triac", "可控硅双向", "交流开关"],
        "negative": ["单向", "晶闸管", "二极管"], "default": "BTA16",
    },
    "diac": {
        "ref": "DB", "name": "双向触发二极管", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["双向触发二极管", "diac", "触发管"],
        "negative": ["TVS", "齐纳"], "default": "DB3",
    },
    "fuse_resettable": {
        "ref": "F", "name": "自恢复保险丝", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["自恢复保险丝", "自恢复", "pptc", "resettable fuse", "polyfuse"],
        "negative": ["一次性", "玻璃管"], "default": "500mA",
    },
    "igbt": {
        "ref": "Q", "name": "IGBT", "pins": {"G": IN, "C": PASSIVE, "E": PASSIVE},
        "aliases": ["igbt", "绝缘栅双极管", "绝缘栅晶体管"],
        "negative": ["mos", "三极管"], "default": "IRG4PC40U",
    },
    "ujt": {
        "ref": "Q", "name": "单结晶体管", "pins": {"E": PASSIVE, "B1": PASSIVE, "B2": PASSIVE},
        "aliases": ["单结晶体管", "ujt", "单结管", "弛张振荡"],
        "negative": ["三极管", "mos"], "default": "2N2646",
    },
    "phototriac": {
        "ref": "U", "name": "光耦可控硅", "pins": {"A": PASSIVE, "K": PASSIVE, "A1": PASSIVE, "A2": PASSIVE},
        "aliases": ["光耦可控硅", "光 triac", "phototriac", "光隔离开关"],
        "negative": ["光耦三极管", "光敏电阻"], "default": "MOC3021",
    },
    "ic_and": {
        "ref": "U", "name": "与门", "pins": {"A": IN, "B": IN, "Y": OUT},
        "aliases": ["与门", "and gate", "与非门输入", "逻辑与"],
        "negative": ["或门", "非门", "异或"], "default": "74HC08",
    },
    "ic_or": {
        "ref": "U", "name": "或门", "pins": {"A": IN, "B": IN, "Y": OUT},
        "aliases": ["或门", "or gate", "逻辑或"],
        "negative": ["与门", "非门"], "default": "74HC32",
    },
    "ic_not": {
        "ref": "U", "name": "非门", "pins": {"A": IN, "Y": OUT},
        "aliases": ["非门", "not gate", "反相器", "逻辑非"],
        "negative": ["与门", "或门"], "default": "74HC04",
    },
    "ic_xor": {
        "ref": "U", "name": "异或门", "pins": {"A": IN, "B": IN, "Y": OUT},
        "aliases": ["异或门", "xor", "异或", "半加器"],
        "negative": ["同或", "与门"], "default": "74HC86",
    },
    "ic_nand": {
        "ref": "U", "name": "与非门", "pins": {"A": IN, "B": IN, "Y": OUT},
        "aliases": ["与非门", "nand", "nand gate"],
        "negative": ["与门", "或非"], "default": "74HC00",
    },
    "ic_nor": {
        "ref": "U", "name": "或非门", "pins": {"A": IN, "B": IN, "Y": OUT},
        "aliases": ["或非门", "nor", "nor gate"],
        "negative": ["或门", "与非"], "default": "74HC02",
    },
    "ic_buffer": {
        "ref": "U", "name": "缓冲器", "pins": {"A": IN, "Y": OUT},
        "aliases": ["缓冲器", "buffer", "三态缓冲", "驱动器"],
        "negative": ["非门", "与门"], "default": "74HC244",
    },
    "flipflop_d": {
        "ref": "U", "name": "D触发器", "pins": {"D": IN, "CLK": IN, "Q": OUT, "Qn": OUT, "RST": IN},
        "aliases": ["d触发器", "d flip flop", "触发器", "寄存器", "边沿触发"],
        "negative": ["jk", "sr触发器"], "default": "74HC74",
    },
    "counter_4bit": {
        "ref": "U", "name": "4位计数器", "pins": {"CLK": IN, "RST": IN, "Q0": OUT, "Q1": OUT, "Q2": OUT, "Q3": OUT},
        "aliases": ["4位计数器", "计数器", "counter", "分频器", "4017类似"],
        "negative": ["移位寄存器"], "default": "74HC93",
    },
    "shiftreg_8bit": {
        "ref": "U", "name": "8位移位寄存器", "pins": {"DS": IN, "SHCP": IN, "STCP": IN, "Q7S": OUT, "OE": IN, "MR": IN},
        "aliases": ["移位寄存器", "74hc595", "595", "shift register", "串转并"],
        "negative": ["计数器", "锁存器"], "default": "74HC595",
    },
    "lm393": {
        "ref": "U", "name": "双电压比较器", "pins": {"1IN+": IN, "1IN-": IN, "1OUT": OUT, "2IN+": IN, "2IN-": IN, "2OUT": OUT, "VCC": PWR_IN, "GND": GND},
        "aliases": ["lm393", "双比较器", "电压比较器ic", "393"],
        "negative": ["运放", "单比较器"], "default": "LM393",
    },
    "lm324": {
        "ref": "U", "name": "四运放", "pins": {"1IN+": IN, "1IN-": IN, "1OUT": OUT, "2IN+": IN, "2IN-": IN, "2OUT": OUT, "VCC": PWR_IN, "GND": GND},
        "aliases": ["lm324", "四运放", "324", "quad opamp"],
        "negative": ["单运放", "比较器"], "default": "LM324",
    },
    "ne556": {
        "ref": "U", "name": "双555定时器", "pins": {"GND": GND, "TRIG1": IN, "OUT1": OUT, "RESET": IN, "CTRL1": IN, "THR1": IN, "DIS1": OUT, "VCC": PWR_IN},
        "aliases": ["ne556", "双555", "556", "双定时器"],
        "negative": ["单555", "ne555"], "default": "NE556",
    },
    "cd4017": {
        "ref": "U", "name": "十进制计数器", "pins": {"CLK": IN, "RST": IN, "EN": IN, "Q0": OUT, "Q1": OUT, "Q2": OUT, "VDD": PWR_IN, "VSS": GND},
        "aliases": ["cd4017", "4017", "十进制计数", "流水灯芯片", "环形计数"],
        "negative": ["74hc93"], "default": "CD4017",
    },
    "atmega328": {
        # PC4=SDA、PC5=SCL；这里只是原理图逻辑引脚名，制造阶段仍需
        # 按具体 MPN/封装核对物理焊盘号。
        "ref": "U", "name": "ATmega328", "pins": {
            "VCC": PWR_IN, "GND": GND, "PB0": PASSIVE, "PB1": PASSIVE,
            "PC0": PASSIVE, "PC1": PASSIVE, "PC4": PASSIVE, "PC5": PASSIVE,
            "PD0": PASSIVE, "PD1": PASSIVE, "AREF": PASSIVE, "AVCC": PWR_IN,
            "RESET": IN, "XTAL1": IN, "XTAL2": OUT, "GND2": GND,
            "PB2": PASSIVE, "PB3": PASSIVE, "PB4": PASSIVE, "PB5": PASSIVE,
            "PD2": PASSIVE, "PD3": PASSIVE, "PD4": PASSIVE, "PD5": PASSIVE,
            "PD6": PASSIVE, "PD7": PASSIVE, "PC2": PASSIVE, "PC3": PASSIVE,
        },
        "aliases": ["atmega328p", "atmega328", "atmega", "avr", "arduino芯片"],
        "negative": ["esp32", "树莓派"], "default": "ATmega328P",
    },
    "esp32": {
        # 常用 I2C 默认脚 GPIO21=SDA、GPIO22=SCL；ESP32 GPIO 可重映射。
        # 这里不表示已核对具体模块/封装的焊盘编号。
        "ref": "U", "name": "ESP32模块", "pins": {"3V3": PWR_IN, "GND": GND, "EN": IN, "GPIO0": PASSIVE, "GPIO2": PASSIVE, "GPIO21": PASSIVE, "GPIO22": PASSIVE, "TX": OUT, "RX": IN},
        "aliases": ["esp32", "esp32模块", "wifi模块", "物联网模块"],
        "negative": ["arduino", "树莓派"], "default": "ESP32-WROOM",
    },
    "dht11": {
        "ref": "U", "name": "温湿度传感器", "pins": {"VCC": PWR_IN, "DATA": PASSIVE, "GND": GND, "NC": PASSIVE},
        "aliases": ["dht11", "温湿度", "温湿度传感器", "湿度传感器"],
        "negative": ["ds18b20", "热敏"], "default": "DHT11",
    },
    "ds18b20": {
        "ref": "U", "name": "数字温度传感器", "pins": {"VDD": PWR_IN, "DQ": PASSIVE, "GND": GND},
        "aliases": ["ds18b20", "数字温度", "18b20", "单总线温度"],
        "negative": ["dht11", "热敏电阻"], "default": "DS18B20",
    },
    "bmp280": {
        "ref": "U", "name": "气压传感器", "pins": {"VCC": PWR_IN, "GND": GND, "SCL": IN, "SDA": PASSIVE, "SDO": PASSIVE, "CSB": IN},
        "aliases": ["bmp280", "气压传感器", "气压计", "海拔传感器"],
        "negative": ["陀螺仪"], "default": "BMP280",
    },
    "mpu6050": {
        "ref": "U", "name": "陀螺仪加速度计", "pins": {"VCC": PWR_IN, "GND": GND, "SCL": IN, "SDA": PASSIVE, "AD0": PASSIVE, "INT": OUT},
        "aliases": ["mpu6050", "陀螺仪", "加速度计", "六轴", "姿态传感器"],
        "negative": ["气压计"], "default": "MPU6050",
    },
    "hc_sr04": {
        "ref": "U", "name": "超声波测距", "pins": {"VCC": PWR_IN, "TRIG": IN, "ECHO": OUT, "GND": GND},
        "aliases": ["超声波", "hc-sr04", "测距", "超声波测距", "sr04"],
        "negative": ["红外"], "default": "HC-SR04",
    },
    "pir": {
        "ref": "U", "name": "人体红外", "pins": {"VCC": PWR_IN, "OUT": OUT, "GND": GND},
        "aliases": ["人体红外", "pir", "热释电", "人体感应", "hc-sr501"],
        "negative": ["红外接收"], "default": "HC-SR501",
    },
    "mq2": {
        "ref": "U", "name": "气体传感器", "pins": {"VCC": PWR_IN, "GND": GND, "DO": OUT, "AO": PASSIVE},
        "aliases": ["mq2", "气体传感器", "烟雾传感器", "燃气传感器", "酒精传感器"],
        "negative": ["温湿度"], "default": "MQ-2",
    },
    "sound_sensor": {
        "ref": "U", "name": "声音传感器", "pins": {"VCC": PWR_IN, "GND": GND, "DO": OUT, "AO": PASSIVE},
        "aliases": ["声音传感器", "声控", "咪头模块", "分贝模块", "声音检测"],
        "negative": ["麦克风裸件"], "default": "KY-037",
    },
    "ir_receiver": {
        "ref": "U", "name": "红外接收头", "pins": {"VCC": PWR_IN, "OUT": OUT, "GND": GND},
        "aliases": ["红外接收", "红外接收头", "vs1838", "1838", "遥控接收"],
        "negative": ["红外发射", "光敏"], "default": "VS1838B",
    },
    "ir_led": {
        "ref": "D", "name": "红外发射管", "pins": {"A": PASSIVE, "K": PASSIVE, "+": PASSIVE, "-": PASSIVE},
        "aliases": ["红外发射", "红外发射管", "ir led", "红外灯"],
        "negative": ["红外接收", "可见光LED"], "default": "TSAL6200",
    },
    "hall_sensor": {
        "ref": "U", "name": "霍尔传感器", "pins": {"VCC": PWR_IN, "OUT": OUT, "GND": GND},
        "aliases": ["霍尔", "霍尔传感器", "hall", "磁场传感器", "49e"],
        "negative": ["电流互感"], "default": "49E",
    },
    "i2c_rh_sensor": {
        # 引脚名/编号与 KiCad 官方 Sensor_Humidity:SHT4x 符号及其
        # Sensirion DFN-4 封装一致（1=SDA 2=SCL 3=VDD 4=VSS），不另造引脚序。
        "ref": "U", "name": "I2C温湿度传感器",
        "pins": {"SDA": PASSIVE, "SCL": PASSIVE, "VDD": PWR_IN, "VSS": GND},
        "aliases": ["sht4x", "sht40", "sht41", "sht45", "温湿度传感器", "i2c温湿度",
                    "湿度传感器", "rh sensor"],
        "negative": ["气压计", "imu", "光照", "二氧化碳"], "default": "SHT41-AD1B",
    },
    "load_cell": {
        "ref": "U", "name": "称重传感器", "pins": {"E+": PWR_IN, "E-": GND, "A+": PASSIVE, "A-": PASSIVE},
        "aliases": ["称重传感器", "称重", "load cell", "压力传感器", "电子秤"],
        "negative": ["气压计"], "default": "5kg",
    },
    "microphone": {
        "ref": "MK", "name": "驻极体话筒", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["话筒", "麦克风", "驻极体", "咪头", "microphone", "拾音器"],
        "negative": ["扬声器", "蜂鸣器"], "default": "驻极体",
    },
    "sevenseg": {
        "ref": "DS", "name": "数码管", "pins": {"com": PASSIVE, "a": PASSIVE, "b": PASSIVE, "c": PASSIVE, "d": PASSIVE, "e": PASSIVE, "f": PASSIVE, "g": PASSIVE},
        "aliases": ["数码管", "七段数码管", "7段", "seven segment", "共阴数码管", "共阳数码管"],
        "negative": ["点阵", "lcd"], "default": "共阴0.56",
    },
    "lcd1602": {
        "ref": "LCD", "name": "LCD1602", "pins": {"VSS": GND, "VDD": PWR_IN, "VO": PASSIVE, "RS": IN, "RW": IN, "E": IN, "D4": PASSIVE, "D5": PASSIVE, "D6": PASSIVE, "D7": PASSIVE, "A": PWR_IN, "K": GND},
        "aliases": ["lcd1602", "1602", "液晶屏", "lcd", "字符液晶", "液晶显示"],
        "negative": ["oled", "数码管"], "default": "LCD1602",
    },
    "oled_ssd1306": {
        "ref": "OLED", "name": "OLED屏", "pins": {"VCC": PWR_IN, "GND": GND, "SCL": IN, "SDA": PASSIVE},
        "aliases": ["oled", "oled屏", "ssd1306", "0.96寸oled", "oled显示"],
        "negative": ["lcd1602", "数码管"], "default": "SSD1306",
    },
    "max7219": {
        "ref": "U", "name": "LED驱动芯片", "pins": {"DIN": IN, "CS": IN, "CLK": IN, "DOUT": OUT, "VCC": PWR_IN, "GND": GND},
        "aliases": ["max7219", "7219", "led驱动", "点阵驱动", "数码管驱动"],
        "negative": ["595"], "default": "MAX7219",
    },
    "tm1637": {
        "ref": "U", "name": "四位数码管模块", "pins": {"CLK": IN, "DIO": PASSIVE, "VCC": PWR_IN, "GND": GND},
        "aliases": ["tm1637", "四位数码管", "时钟模块", "数码管模块"],
        "negative": ["lcd", "oled"], "default": "TM1637",
    },
    "solenoid": {
        "ref": "Y", "name": "电磁铁", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["电磁铁", "solenoid", "电磁阀", "推杆电磁铁"],
        "negative": ["继电器", "变压器"], "default": "5V",
    },
    "heater": {
        "ref": "EH", "name": "加热片", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["加热片", "加热", "ptc加热", "heater", "发热片"],
        "negative": ["电阻负载"], "default": "5V 5W",
    },
    "lm317": {
        "ref": "U", "name": "可调稳压", "pins": {"IN": PWR_IN, "ADJ": PASSIVE, "OUT": PWR_OUT},
        "aliases": ["lm317", "可调稳压", "317", "可调电源"],
        "negative": ["7805", "降压模块"], "default": "LM317",
    },
    "buck_module": {
        "ref": "U", "name": "DC-DC降压模块", "pins": {"IN+": PWR_IN, "IN-": GND, "OUT+": PWR_OUT, "OUT-": GND},
        "aliases": ["降压模块", "dcdc降压", "buck", "开关降压", "lm2596模块", "mp1584"],
        "negative": ["升压", "7805"], "default": "LM2596",
    },
    "boost_module": {
        "ref": "U", "name": "DC-DC升压模块", "pins": {"IN+": PWR_IN, "IN-": GND, "OUT+": PWR_OUT, "OUT-": GND},
        "aliases": ["升压模块", "dcdc升压", "boost", "mt3608"],
        "negative": ["降压", "稳压"], "default": "MT3608",
    },
    "lipo_battery": {
        "ref": "BT", "name": "锂电池", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["锂电池", "lipo", "3.7v电池", "锂电", "聚合物电池"],
        "negative": ["干电池", "充电宝"], "default": "3.7V 1000mAh",
    },
    "solar_cell": {
        "ref": "BT", "name": "太阳能板", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["太阳能板", "太阳能", "光伏板", "solar", "光伏"],
        "negative": ["锂电池"], "default": "5V 1W",
    },
    "usb_port": {
        "ref": "J", "name": "USB接口", "pins": {"VBUS": PWR_OUT, "D-": PASSIVE, "D+": PASSIVE, "GND": GND, "ID": PASSIVE},
        "aliases": ["usb", "usb接口", "type-c", "microusb", "usb座"],
        "negative": ["排针", "电源座"], "default": "Micro-USB",
    },
    "barrel_jack": {
        "ref": "J", "name": "DC电源座", "pins": {"tip": PASSIVE, "sleeve": GND, "switch": PASSIVE},
        "aliases": ["dc电源座", "电源座", "dc座", "5.5*2.1", "电源插头"],
        "negative": ["usb", "接线端子"], "default": "5.5×2.1",
    },
    "conn_header": {
        "ref": "J", "name": "排针", "pins": {"1": PASSIVE, "2": PASSIVE, "3": PASSIVE, "4": PASSIVE},
        "aliases": ["排针", "排母", "杜邦线", "header", "pin header", "排针座"],
        "negative": ["接线端子"], "default": "1×4",
    },
    "switch_toggle": {
        "ref": "SW", "name": "拨动开关", "pins": {"1": PASSIVE, "2": PASSIVE, "3": PASSIVE},
        "aliases": ["拨动开关", "拨动", "toggle", "拨码开关单路"],
        "negative": ["按钮", "船型开关"], "default": "SPDT",
    },
    "switch_dip": {
        "ref": "SW", "name": "拨码开关", "pins": {"1": PASSIVE, "2": PASSIVE, "3": PASSIVE, "4": PASSIVE, "5": PASSIVE, "6": PASSIVE},
        "aliases": ["拨码开关", "dip开关", "dip switch", "指拨开关"],
        "negative": ["拨动开关"], "default": "6位",
    },
    "switch_reed": {
        "ref": "SW", "name": "干簧管", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["干簧管", "干簧", "reed", "磁簧开关", "门磁"],
        "negative": ["继电器", "霍尔"], "default": "Φ3×14",
    },
    "connector_screw2": {
        "ref": "J", "name": "两位接线端子", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["两位端子", "两位接线端子", "2p端子", "螺丝端子2位"],
        "negative": [], "default": "5.08mm",
    },
    "potentiometer_trim": {
        "ref": "RV", "name": "微调电位器", "pins": {"1": PASSIVE, "2": PASSIVE, "3": PASSIVE},
        "aliases": ["微调电位器", "可调电阻", "精密电位器", "trimmer", "3296"],
        "negative": ["电位器旋钮"], "default": "10k",
    },
    "varactor": {
        "ref": "D", "name": "变容二极管", "pins": {"A": PASSIVE, "K": PASSIVE, "+": PASSIVE, "-": PASSIVE},
        "aliases": ["变容二极管", "变容", "varactor", "调谐二极管"],
        "negative": ["齐纳", "tvs"], "default": "BB910",
    },
    "optointerrupter": {
        "ref": "U", "name": "光电开关", "pins": {"VCC": PWR_IN, "GND": GND, "OUT": OUT},
        "aliases": ["光电开关", "槽型光耦", "光电门", "对射开关", "optointerrupter"],
        "negative": ["光耦隔离"], "default": "ITR9606",
    },
    "buzzer_magnetic": {
        "ref": "BZ", "name": "电磁蜂鸣器", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["电磁蜂鸣器", "电磁式", "magnetic buzzer"],
        "negative": ["无源蜂鸣器", "压电"], "default": "5V",
    },
    "speaker_8ohm": {
        "ref": "LS", "name": "8欧扬声器", "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["8欧扬声器", "喇叭8欧", "小喇叭"],
        "negative": ["蜂鸣器"], "default": "8Ω 0.5W",
    },
    "antenna": {
        "ref": "AE", "name": "天线", "pins": {"feed": PASSIVE},
        "aliases": ["天线", "antenna", "棒状天线", "wifi天线"],
        "negative": [], "default": "2.4G",
    },
    "jumper_wire": {
        "ref": "W", "name": "跳线", "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["跳线", "跳线帽", "杜邦头", "jumper wire", "飞线"],
        "negative": ["导线原理图"], "default": "M-M 20cm",
    },
    # ---------- 2026 电源管理 + 通信接口 + 保护器件 + 模拟前端扩容（24 种） ----------
    # 电源管理
    "ldo_3v3": {
        # LDO：IN 取电（PWR_IN）、OUT 供电轨（PWR_OUT），与 regulator_7805 同建模
        "ref": "U", "name": "LDO3.3V稳压",
        "pins": {"IN": PWR_IN, "GND": GND, "OUT": PWR_OUT},
        "aliases": ["ldo", "ldo稳压", "3.3v ldo", "3.3v稳压", "ams1117", "低压差稳压", "ldo3.3", "三端ldo"],
        "negative": ["1.8v", "7805", "开关电源", "buck", "boost", "升压"],
        "default": "AMS1117-3.3",
    },
    "ldo_1v8": {
        "ref": "U", "name": "LDO1.8V稳压",
        "pins": {"IN": PWR_IN, "GND": GND, "OUT": PWR_OUT},
        "aliases": ["ldo1.8", "1.8v ldo", "1.8v稳压", "低压差1.8v", "1.8v ldoregulator"],
        "negative": ["3.3v", "7805", "开关电源", "buck", "boost"],
        "default": "AMS1117-1.8",
    },
    "charge_pump": {
        # 电荷泵：开关电容式电压变换，飞跨电容 C+/C- 为无源脚
        "ref": "U", "name": "电荷泵",
        "pins": {"VIN": PWR_IN, "GND": GND, "VOUT": PWR_OUT, "C+": PASSIVE, "C-": PASSIVE},
        "aliases": ["电荷泵", "charge pump", "开关电容", "极性反转电源", "负压生成", "电压反转"],
        "negative": ["ldo", "7805", "buck", "boost", "变压器"],
        "default": "ICL7660",
    },
    "voltage_monitor": {
        # 电压监控/复位监控：RESET 开漏输出，MR 手动复位输入
        "ref": "U", "name": "电压监控器",
        "pins": {"VCC": PWR_IN, "GND": GND, "RESET": OUT, "MR": IN},
        "aliases": ["电压监控", "电压监控器", "电源监控", "复位监控", "欠压检测", "supervisor", "电压监测", "看门狗监控"],
        "negative": ["比较器", "运放", "adc"],
        "default": "MAX809",
    },
    "power_sequencer": {
        # 电源时序：EN 使能输入、PG 电源好输出、VOUT 受控供电输出
        "ref": "U", "name": "电源时序器",
        "pins": {"VIN": PWR_IN, "GND": GND, "EN": IN, "PG": OUT, "VOUT": PWR_OUT},
        "aliases": ["电源时序", "上电时序", "电源时序器", "power sequencer", "时序控制", "上电顺序"],
        "negative": ["ldo", "稳压器", "7805"],
        "default": "TPS3808",
    },
    # 通信接口（排针按 MCU 侧语义：TX/SCK 等为输出，RX/MISO 为输入）
    "uart_header": {
        "ref": "J", "name": "UART排针",
        "pins": {"TX": OUT, "RX": IN, "GND": GND, "VCC": PWR_OUT},
        "aliases": ["uart", "uart排针", "串口排针", "串口座", "调试口", "uart header", "ttl串口", "串口接口"],
        "negative": ["spi", "i2c", "can", "rs485"],
        "default": "1×4",
    },
    "spi_header": {
        "ref": "J", "name": "SPI排针",
        "pins": {"SCK": OUT, "MOSI": OUT, "MISO": IN, "CS": OUT, "GND": GND, "VCC": PWR_OUT},
        "aliases": ["spi", "spi排针", "spi接口", "spi header", "spi座", "spi总线"],
        "negative": ["uart", "i2c", "can"],
        "default": "1×6",
    },
    "i2c_header": {
        # I2C 开漏总线：SCL/SDA 双 PASSIVE，允许多主/多从共线
        "ref": "J", "name": "I2C排针",
        "pins": {"SCL": PASSIVE, "SDA": PASSIVE, "GND": GND, "VCC": PWR_OUT},
        "aliases": ["i2c", "i2c排针", "i2c接口", "i2c header", "iic排针", "iic接口", "i2c总线"],
        "negative": ["uart", "spi", "can"],
        "default": "1×4",
    },
    "can_transceiver": {
        "ref": "U", "name": "CAN收发器",
        "pins": {"TXD": IN, "RXD": OUT, "VCC": PWR_IN, "GND": GND, "CANH": PASSIVE, "CANL": PASSIVE},
        "aliases": ["can收发器", "can transceiver", "can总线", "tja1050", "can通信", "can接口"],
        "negative": ["rs485", "uart", "spi"],
        "default": "TJA1050",
    },
    "rs485_transceiver": {
        "ref": "U", "name": "RS485收发器",
        "pins": {"RO": OUT, "DI": IN, "DE": IN, "RE": IN, "A": PASSIVE, "B": PASSIVE, "VCC": PWR_IN, "GND": GND},
        "aliases": ["rs485", "rs485收发器", "485收发", "max485", "rs-485", "差分总线", "485通信"],
        "negative": ["can", "rs232", "uart", "spi"],
        "default": "MAX485",
    },
    "usb_c": {
        # USB Type-C：VBUS 可对外供电（PWR_OUT），CC 为配置通道
        "ref": "J", "name": "USB Type-C",
        "pins": {"VBUS": PWR_OUT, "GND": GND, "D+": PASSIVE, "D-": PASSIVE, "CC1": PASSIVE, "CC2": PASSIVE},
        "aliases": ["type-c", "typec", "usb-c", "usbc", "c口", "type c接口", "usb type-c", "type-c座"],
        "negative": ["micro", "microusb", "mini", "type-a"],
        "default": "USB-C 16P",
    },
    # 保护器件
    "tvs_array": {
        # TVS 阵列：多路 ESD/浪涌保护，IO 脚无源、GND 泄放
        "ref": "D", "name": "TVS阵列",
        "pins": {"IO1": PASSIVE, "IO2": PASSIVE, "GND": GND, "VCC": PASSIVE},
        "aliases": ["tvs阵列", "tvs array", "esd阵列", "多路tvs", "保护阵列", "esd保护阵列"],
        "negative": ["单路tvs", "tvs二极管"],
        "default": "PESD5V0L4",
    },
    "esd_diode": {
        # ESD 保护二极管：与普通 TVS 区分（更低电容、信号线专用）
        "ref": "D", "name": "ESD保护二极管",
        "pins": {"A": PASSIVE, "K": PASSIVE, "+": PASSIVE, "-": PASSIVE},
        "aliases": ["esd", "esd二极管", "esd保护", "静电保护", "esd diode", "防静电", "静电二极管"],
        "negative": ["整流", "发光", "稳压管", "齐纳"],
        "default": "PESD5V0S1BA",
    },
    "common_mode_choke": {
        # 共模扼流圈：双绕组 4 脚，抑制共模噪声
        "ref": "L", "name": "共模扼流圈",
        "pins": {"1": PASSIVE, "2": PASSIVE, "3": PASSIVE, "4": PASSIVE},
        "aliases": ["共模扼流圈", "共模电感", "共模电感器", "common mode choke", "共模滤波", "共模线圈"],
        "negative": ["差模", "普通电感", "磁珠", "变压器"],
        "default": "10mH 1A",
    },
    "gas_discharge_tube": {
        "ref": "GDT", "name": "气体放电管",
        "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["气体放电管", "放电管", "gdt", "gas discharge", "陶瓷放电管", "防雷管", "放电间隙"],
        "negative": ["tvs", "压敏", "氖泡"],
        "default": "2R75-230",
    },
    "resettable_fuse_1206": {
        # 1206 贴片 PPTC：与插件 fuse_resettable 区分封装
        "ref": "F", "name": "1206自恢复保险丝",
        "pins": {"1": PASSIVE, "2": PASSIVE},
        "aliases": ["1206自恢复", "1206保险丝", "贴片自恢复", "pptc 1206", "1206 fuse", "贴片保险丝"],
        "negative": ["插件自恢复", "玻璃管", "一次性"],
        "default": "1206 500mA",
    },
    # 模拟前端
    "instrumentation_amp": {
        # 仪表放大器：高阻差分输入 + REF 基准。
        # 名称/别名避开与「放大器」的 0.75 模糊分（否则会挤进
        # 裸词“放大器”的澄清候选，破坏反相/同相二选一回归）；
        # 用户说「仪表放大器」时靠 name 模糊匹配（ratio≈0.77）仍可命中。
        "ref": "U", "name": "高精度仪表放大器",
        "pins": {"IN+": IN, "IN-": IN, "REF": PASSIVE, "OUT": OUT, "V+": PWR_IN, "V-": GND},
        "aliases": ["仪放", "精密仪放", "仪表运放", "instrumentation amp", "ina128", "ad620", "高精度仪放"],
        "negative": ["普通运放", "比较器", "跟随器", "反相放大", "同相放大"],
        "default": "INA128",
    },
    "current_sense_amp": {
        "ref": "U", "name": "电流检测放大器",
        "pins": {"IN+": IN, "IN-": IN, "OUT": OUT, "VCC": PWR_IN, "GND": GND},
        "aliases": ["电流检测", "电流检测放大器", "电流采样放大", "current sense", "ina219", "分流放大", "检流放大"],
        "negative": ["仪表放大器", "电流表", "ammeter"],
        "default": "INA219",
    },
    "digital_pot": {
        # 数字电位器：A/B 电阻两端、W 滑动端；SPI 三线控制
        "ref": "U", "name": "数字电位器",
        "pins": {"A": PASSIVE, "W": PASSIVE, "B": PASSIVE, "CS": IN, "SCK": IN, "SI": IN, "VCC": PWR_IN, "GND": GND},
        "aliases": ["数字电位器", "数控电位器", "digital pot", "digipot", "mcp41010", "数字可调电阻", "数位电位器"],
        "negative": ["机械电位器", "电位器旋钮", "dac"],
        "default": "MCP41010",
    },
    "dac_8bit": {
        "ref": "U", "name": "8位DAC",
        "pins": {"CS": IN, "SCK": IN, "DIN": IN, "OUT": OUT, "VREF": PASSIVE, "VCC": PWR_IN, "GND": GND},
        "aliases": ["dac", "8位dac", "数模转换", "dac0808", "mcp4725", "数模转换器", "d/a转换"],
        "negative": ["adc", "数字电位器", "模数"],
        "default": "DAC0808",
    },
    "adc_8bit": {
        "ref": "U", "name": "8位ADC",
        "pins": {"AIN": PASSIVE, "DOUT": OUT, "CLK": IN, "CS": IN, "VREF": PASSIVE, "VCC": PWR_IN, "GND": GND},
        "aliases": ["adc", "8位adc", "模数转换", "adc0804", "模数转换器", "a/d转换"],
        "negative": ["dac", "数模"],
        "default": "ADC0804",
    },
    # 其他执行器 / 隔离
    "buzzer_magnetic_5v": {
        # 与 buzzer_magnetic 区分：明确 5V 供电规格
        "ref": "BZ", "name": "5V电磁蜂鸣器",
        "pins": {"+": PASSIVE, "-": PASSIVE},
        "aliases": ["5v电磁蜂鸣器", "5v蜂鸣器", "电磁蜂鸣器5v", "磁式蜂鸣器5v", "5v有源蜂鸣"],
        "negative": ["无源蜂鸣器", "压电蜂鸣器", "扬声器"],
        "default": "5V",
    },
    "relay_solid_state": {
        # 固态继电器：控制侧 IN±、负载侧 OUT1/OUT2，全 PASSIVE（无触点）
        "ref": "K", "name": "固态继电器",
        "pins": {"IN+": PASSIVE, "IN-": PASSIVE, "OUT1": PASSIVE, "OUT2": PASSIVE},
        "aliases": ["固态继电器", "ssr", "solid state relay", "无触点继电器", "固态开关", "光耦继电器"],
        "negative": ["电磁继电器", "机械继电器", "relay线圈"],
        "default": "SSR-25DA",
    },
    "optocoupler_ac": {
        # 交流输入光耦：双向 LED 输入，可直接检测交流信号
        "ref": "U", "name": "交流输入光耦",
        "pins": {"A": PASSIVE, "K": PASSIVE, "E": PASSIVE, "C": PASSIVE},
        "aliases": ["交流光耦", "ac光耦", "交流输入光耦", "h11aa1", "过零光耦", "双向光耦"],
        "negative": ["直流光耦", "phototriac", "光耦可控硅"],
        "default": "H11AA1",
    },
}


# =====================================================================
# 允许元件库构建（命中类型 ∪ 无源/电源基件）
# =====================================================================
# 任何真实电路几乎必然包含、但用户的措辞往往不会明说的基件：例如
# 「用555做一个LED闪烁灯」字面只提 555 和 LED，本地抽取结果就是
# {led, ne555}——而 LLM 生成可用的 555 电路必然要输出限流/定时电阻、
# 定时/去耦电容和电源符号。若允许库只含命中类型，正确输出会被结构校验
# 整批误杀（实录：R1~R3/C1/V1/GND1 共 6 处「不在允许库」→ 3 轮修复
# 无解 → 模板兜底）。故这些基件始终放行；引脚白名单照常生效，乱接连脚
# 仍会被校验拦截。仅收录 CATALOG 中渲染无忧的通用件，不放开 MCU 类。
BASE_ALLOWED_TYPES = ("resistor", "capacitor", "vcc", "gnd", "battery")


def build_matched_lib(types: Iterable[str]) -> Dict[str, Dict[str, list]]:
    """命中元件类型 ∪ BASE_ALLOWED_TYPES → 本次允许的元件库。

    结构与既有约定一致：{type: {"pins": [引脚名字列表]}}（引脚取自
    CATALOG 白名单）。types 中的未知类型静默剔除（与原推导行为一致），
    去重保序：命中类型在前，基件补充在后。"""
    merged = list(dict.fromkeys([*types, *BASE_ALLOWED_TYPES]))
    return {t: {"pins": list(CATALOG[t]["pins"])}
            for t in merged if t in CATALOG}


# =====================================================================
# 匹配器
# =====================================================================
FUZZY_CUTOFF = 0.55  # difflib 相似度阈值，低于此值视为没匹配上（OOD，交给上层兜底/拦截）

@functools.lru_cache(maxsize=1024)
def _norm(s: str) -> str:
    """别名/需求归一化（去空白连字符+小写）。

    进程内缓存：目录里 ~150 条别名是固定字符串，每条只需真的归一化一次；
    此前每次匹配请求都对全部别名重跑正则（单次 match_one 约 300+ 次正则调用），
    本地短路路径在每条需求上都会触发，这里是最热的纯函数。
    maxsize=None → 1024——查询字符串来自用户输入，
    无界缓存会让长驻进程内存随请求数缓慢增长；目录别名是有限集，命中率
    不受影响，只有冷门查询会重复归一化（微秒级）。"""
    return re.sub(r"[\s\-_]+", "", str(s).lower())


@functools.lru_cache(maxsize=512)
def _word_re(alias: str) -> "re.Pattern":
    """纯英文/缩写别名的词边界匹配正则，按别名缓存（避免每次匹配重建模式串）。
    同上加上限（别名是有限集，512 绰绰有余）。"""
    token = r"[\s\-_]*".join(re.escape(char) for char in alias)
    return re.compile(r"(?<![a-z0-9])" + token + r"(?![a-z0-9])")


def _neg_list(entry):
    """返回真正生效的负向关键词表：若某负向词本身包含在某条别名之内
    （如“非反相放大器”包含“反相”），它描述的就是本元件自身，跳过之，
    否则会把“非反相放大器”这类正当需求误杀。"""
    names = [entry["name"]] + entry.get("aliases", [])
    return [neg for neg in entry.get("negative", [])
            if not any(_norm(neg) in _norm(a) for a in names if a)]


# ---------------------------------------------------------------------
# 导入期派生视图（数据驱动实践：
# 目录数据只写一份，匹配用的派生表在模块加载时算好一次，请求热路径
# 不再对 ~150 条别名反复跑“负向词×别名”的过滤循环）。
# 显式传入自定义 catalog 时不走视图、现场计算，结果与逐次推导一致。
# ---------------------------------------------------------------------
# match_one 用：生效负向词 (原文, 归一化) 对（原文供 q_raw 快速判断）
_NEG_PAIRS = {t: tuple((n, _norm(n)) for n in _neg_list(e))
              for t, e in CATALOG.items()}
# match_components 用：(归一化别名序列, 归一化负向词序列)
_NORM_VIEW = {t: (tuple(_norm(a) for a in [e["name"]] + e.get("aliases", []) if a),
                  tuple(_norm(n) for n in e.get("negative", [])))
              for t, e in CATALOG.items()}


def _alias_hit(q: str, alias: str) -> bool:
    """别名是否在需求中命中。
    中文别名用子串包含（如“灯”“地”）；纯英文/缩写用词边界，避免单字母
    别名 l 误命中 “led”、r 误命中 “resistor” 这类问题。"""
    a = _norm(alias)
    if not a:
        return False
    if a == q:
        return True
    if all(ord(ch) < 128 for ch in a):  # 纯 ASCII：要求前后不是字母数字
        return _word_re(a).search(q) is not None
    return a in q  # 含中文：子串包含


def _score(query: str, entry: dict) -> float:
    """别名命中给最高分；否则用 difflib 算模糊相似度。

    预筛采用标准库 difflib.get_close_matches 的同款实践：quick_ratio()
    是 ratio() 的快速上界（恒有 ratio ≤ quick_ratio），上界都追不上当前
    最优时整条跳过 —— max 结果与全额计算完全一致，省掉 O(n²) 的 ratio()。"""
    q = _norm(query)
    best = 0.0
    for alias in [entry["name"]] + entry.get("aliases", []):
        if _alias_hit(q, alias):
            return 1.0
        sm = SequenceMatcher(None, q, _norm(alias))
        if sm.real_quick_ratio() <= best or sm.quick_ratio() <= best:
            continue
        r = sm.ratio()
        if r > best:
            best = r
    return best


def match_one(query: str, catalog=CATALOG, cutoff=FUZZY_CUTOFF):
    """返回 (type, score)；被负向关键词排除或相似度不足时返回 (None, 0)。"""
    best_type, best_score = None, 0.0
    q_raw = str(query).lower()   # 需求的两种形态只算一次（原先每个负向词
    q_norm = _norm(query)        # 都重复调用 str().lower() 与 _norm(query)）
    neg_view = _NEG_PAIRS if catalog is CATALOG else None
    for ctype, entry in catalog.items():
        negs = neg_view[ctype] if neg_view is not None else \
            tuple((n, _norm(n)) for n in _neg_list(entry))
        if any(raw in q_raw or nrm in q_norm for raw, nrm in negs):
            continue  # 负向排除：例如“同相放大器”不允许命中反相模板
        sc = _score(query, entry)
        if sc > best_score:
            best_type, best_score = ctype, sc
            if best_score >= 1.0:
                # 满分即分数上限：后续条目不可能严格更大，提前收尾。
                # 与原“严格大于才替换”语义一致 —— 仍是首个满分条目当选。
                break
    if best_score >= cutoff:
        return best_type, best_score
    return None, best_score


# 多候选歧义判定边距：次优候选与最优分差小于该值（且双双过阈值）时，
# 认为“多个候选都可能是用户想要的”——由 clarify 链路提问，而不是静默择一
# （相近词命中相邻类型会产生结构合法但语义错误的电路，
#  例如口语“放大器”对反相/同相放大器的模糊分同为 0.75，此前静默取首个）
AMBIGUITY_MARGIN = 0.08


def match_one_detail(query: str, catalog=CATALOG, cutoff=FUZZY_CUTOFF) -> Dict[str, Any]:
    """match_one 的审计增强版：返回完整匹配画像（不改变 match_one 行为）。

    返回 dict：
      type/score        最优候选及得分（未过阈值时 type=None）
      name              最优候选中文名（type 为 None 时空串）
      runner_up/score   次优候选及得分（不足两个候选时为 None/0.0）
      runner_up_name    次优候选中文名
      ambiguous         多候选歧义：双方都过 cutoff 且分差 <= AMBIGUITY_MARGIN
      candidates        得分>0 的前 3 个 (type, 中文名, score)，日志回显用
    供 clarify 规则判歧与 extract_components 日志回显共用，
    保证“判定歧义”与“静默匹配”看到的是同一份分数。"""
    scored = []
    q_raw = str(query).lower()
    q_norm = _norm(query)
    neg_view = _NEG_PAIRS if catalog is CATALOG else None
    for ctype, entry in catalog.items():
        negs = neg_view[ctype] if neg_view is not None else \
            tuple((n, _norm(n)) for n in _neg_list(entry))
        if any(raw in q_raw or nrm in q_norm for raw, nrm in negs):
            continue
        sc = _score(query, entry)
        if sc > 0:
            scored.append((sc, ctype, entry.get("name", ctype)))
    scored.sort(key=lambda x: (-x[0], x[1]))   # 分数降序、同分按类型名稳定排序
    best_score = scored[0][0] if scored else 0.0
    best_type = scored[0][1] if scored and best_score >= cutoff else None
    runner = scored[1] if len(scored) > 1 else None
    runner_ok = runner is not None and runner[0] >= cutoff
    return {
        "type": best_type,
        "score": best_score if best_type else 0.0,
        "name": scored[0][2] if best_type else "",
        "runner_up": runner[1] if runner_ok else None,
        "runner_up_score": runner[0] if runner_ok else 0.0,
        "runner_up_name": runner[2] if runner_ok else "",
        "ambiguous": bool(best_type and runner_ok
                          and best_score - runner[0] <= AMBIGUITY_MARGIN),
        "candidates": [(t, n, s) for s, t, n in scored[:3]],
    }


def _affirmative_alias_positions(text, alias, negs_all, window=6):
    """仅返回完整且未被否定的别名；匹配和证据检索共享这一合同。"""
    ascii_name = all(ord(char) < 128 for char in alias)
    pattern = _word_re(alias) if ascii_name else re.compile(re.escape(alias))
    for match in pattern.finditer(text):
        idx = match.start()
        prefix = _norm(text[max(0, idx - 24):idx])
        if re.search(r"(?:不要|无需|无须|不需要|不(?:添加|加|使用|包含|连接|接入))[^，。；;,]{0,10}$", prefix):
            continue
        seg = _norm(text[max(0, idx - window):match.end() + window])
        if not ascii_name and any(neg in seg for neg in negs_all if neg not in alias):
            continue
        yield idx


def match_components(query: str, catalog=CATALOG, cutoff=FUZZY_CUTOFF, window=6):
    """从一句中文需求里找出所有提到的元件类型（去重，保序）。
    多元件同句时，负向排除只看“命中别名前后 window 字”的局部窗口，
    避免句尾的 LED 负向词误杀句中的稳压管。"""
    q = _norm(query)
    q_raw = str(query).lower()
    norm_view = _NORM_VIEW if catalog is CATALOG else None
    found = []
    for ctype, entry in catalog.items():
        if norm_view is not None:
            names, negs_all = norm_view[ctype]   # 导入期已归一化
        else:
            names = tuple(_norm(a) for a in
                          [entry["name"]] + entry.get("aliases", []) if a)
            negs_all = tuple(_norm(n) for n in entry.get("negative", []))
        ok = False
        for a in names:
            # 英文缩写必须按完整词命中；否则 OLED 会误命中 LED。
            # 原文边界保证 OLED 不被当成 LED；规范化文本补全带空格/连字符
            # 的别名（例如 "3.3V LDO"）。两条路径都必须满足完整词边界。
            if not (_alias_hit(q_raw, a) or _alias_hit(q, a)):
                continue
            positions = list(_affirmative_alias_positions(q, a, negs_all, window))
            if not positions:
                positions = list(_affirmative_alias_positions(q_raw, a, negs_all, window))
            for idx in positions:
                ok = True
                break
            if ok:
                break
        if ok and ctype not in found:
            found.append(ctype)
    # I2C 是总线名称，不等于存在一个物理排针。仅有明确连接器词，或没有
    # 可识别的总线端点器件时，才把 i2c_header 保留为候选。
    if "i2c_header" in found and (any(t in found for t in
                                         ("atmega328", "esp32", "bmp280", "mpu6050",
                                          "oled_ssd1306")) or
                                    any(word in q for word in ("单片机", "传感器"))) and not any(
            word in q for word in ("排针", "排母", "针座", "接插件", "外接接口", "外部接口")):
        found.remove("i2c_header")
    if "i2c_header" in found and "conn_header" in found:
        found.remove("conn_header")
    return found


def retrieve_candidates(query: str, catalog=CATALOG) -> list:
    """返回带词面证据的目录候选，供规划与逐例评测使用。

    不用整句模糊相似度猜器件；仅返回 match_components 已接受的目录类型。
    每条记录都能追溯到需求中的别名及其起始位置。未提及的电源/阻容支持件
    不在此列，应由规划器单独列为 suggested，而不能伪装成用户要求。
    """
    q = _norm(query)
    q_raw = str(query).lower()
    accepted = set(match_components(query, catalog=catalog))
    rows = []
    for ctype, entry in catalog.items():
        if ctype not in accepted:
            continue
        hits = []
        negs = tuple(_norm(n) for n in entry.get("negative", []))
        for alias in [entry["name"]] + entry.get("aliases", []):
            a = _norm(alias)
            if not a:
                continue
            positions = list(_affirmative_alias_positions(q_raw, a, negs))
            basis = "original"
            if not positions:
                positions = list(_affirmative_alias_positions(q, a, negs))
                basis = "normalized"
            for pos in positions:
                hits.append((len(a), pos, alias, basis))
        if not hits:
            continue
        _, pos, alias, basis = max(hits, key=lambda hit: (hit[0], -hit[1]))
        rows.append({"type": ctype, "name": entry["name"], "alias": alias,
                     "position": pos, "position_basis": basis,
                     "score": 1.0, "source": "catalog_exact"})
    return sorted(rows, key=lambda row: (row["position"], row["type"]))


_PART_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*", re.I)
_NON_PART_TOKEN_RE = re.compile(
    r"(?:i2c|iic|gpio\d+|p[a-d]\d+|sda\d*|scl\d*|[urcjdq]\d+|led\d+)", re.I)
_SOURCE_HASH_RE = re.compile(
    r"(?:固定提交|提交|commit|sha(?:-?(?:1|256))?|hash|指纹)\s*[:=：]?\s*"
    r"(?<![A-Za-z0-9])([0-9a-f]{64}|[0-9a-f]{40})(?![A-Za-z0-9])", re.I)
_SOURCE_REPO_RE = re.compile(
    r"参考(?:仓库)?\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s*(?=固定提交|提交|commit)", re.I)


def unsupported_part_numbers(query: str, catalog=CATALOG) -> list:
    """找出需求中明显的目录外器件型号；不把型号悄悄改成近似目录件。

    只审计包含英文字母及数字的型号。自然语言中的未收录中文器件名仍需
    由需求澄清/人工审核判断；此函数不宣称通用命名实体识别能力。
    """
    known = set()
    for entry in catalog.values():
        for label in [entry.get("default", ""), *entry.get("aliases", [])]:
            if "类似" in label:
                continue
            known.update(token.lower() for token in _PART_TOKEN_RE.findall(label))
        known.update(str(pin).lower() for pin in entry.get("pins", {})
                     if _PART_TOKEN_RE.fullmatch(str(pin)))
    # Mask only explicit provenance metadata, preserving offsets and actual MPNs.
    # Hex-looking tokens outside a commit/hash declaration remain auditable.
    text = str(query)
    for pattern in (_SOURCE_HASH_RE, _SOURCE_REPO_RE):
        text = pattern.sub(lambda m: m.group(0)[:m.start(1)-m.start()]
                           + " " * len(m.group(1))
                           + m.group(0)[m.end(1)-m.start():], text)
    unknown = []
    for token in _PART_TOKEN_RE.findall(text):
        if _NON_PART_TOKEN_RE.fullmatch(token) or token.lower() in known:
            continue
        if token not in unknown:
            unknown.append(token)
    return unknown


def match_template(query: str, templates, catalog=CATALOG):
    """
    模板路由：templates = {模板key: {"aliases":[...], "negative":[...]}}。
    先负向排除再算正向得分，返回最佳模板 key 或 None（None 表示走 LLM）。
    """
    q_norm = _norm(query)   # 需求归一化只算一次（原先每个别名重复计算）
    best_key, best_score = None, 0.0
    for key, rule in templates.items():
        if any(_norm(n) in q_norm for n in rule.get("negative", [])):
            continue
        sc = 0.0
        for a in rule.get("aliases", []):
            if _norm(a) in q_norm:
                sc = 1.0
                break               # 子串命中已是满分，其余别名无需再看
            sm = SequenceMatcher(None, q_norm, _norm(a))
            if sm.real_quick_ratio() <= best_score or sm.quick_ratio() <= best_score:
                continue            # 上界都追不上当前最优：与全额计算等价
            r = sm.ratio()
            if r > sc:
                sc = r
        if sc > best_score:
            best_key, best_score = key, sc
            if best_score >= 1.0:
                break               # 满分封顶，先到先得语义不变
    return best_key if best_score >= FUZZY_CUTOFF else None


# =====================================================================
# 导出：给 ERC / 前端
# =====================================================================
def build_erc_kb(catalog=CATALOG):
    """生成 erc.run_erc 需要的 {type: {pin: 电气类型}}。"""
    return {t: dict(e["pins"]) for t, e in catalog.items()}


def to_frontend_manifest(catalog=CATALOG):
    """下发给前端的精简清单（不含匹配用别名，只含渲染/交互需要的字段）。"""
    return {t: {"ref": e["ref"], "name": e["name"],
                "pins": list(e["pins"].keys()), "default": e.get("default", "")}
            for t, e in catalog.items()}


# =====================================================================
# 参数值解析与模板注入（模板兜底采纳用户显式参数）
# ---------------------------------------------------------------------
# 解析思路参考开源项目 PcbDraw（github.com/yaqwsx/PcbDraw）unit.py 的
# read_resistance()：数字 + SI 倍率前缀（k/K/M/m…）+ 单位 三段式归一。
# 结合中文书写习惯扩展：单位可省略（“1k电阻”按电阻语义归 Ω）、
# 中文单位别名（欧/欧姆/伏/法/亨）。与 ai2kicad 的偏差检测共用同一套
# 解析，保证“注入了什么”与“报告核对了什么”严格同源。
# =====================================================================

PARAM_UNIT_MAP = {"欧": "Ω", "欧姆": "Ω", "Ω": "Ω", "R": "Ω",
                  "V": "V", "伏": "V",
                  "F": "F", "法": "F",
                  "H": "H", "亨": "H"}

# 数字 + 倍率前缀 + 单位（完整形式，如 330Ω / 5V / 100nF）
PARAM_FULL = re.compile(
    r"(\d+(?:\.\d+)?)\s*([kKmMμunp]?)\s*(Ω|欧|欧姆|V|伏|F|法|H)")
# 数字 + 裸倍率前缀（省略单位的中文写法，如 “1k电阻”——按电阻语义归 Ω）；
# 负向断言排除其后紧跟任何单位的情况（1kΩ、1mF、1kH 等由完整形式负责）
PARAM_BARE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(k|K|m|M)(?![a-zA-ZΩ欧伏法亨V])")
PARAM_MULT = {"k": 1e3, "K": 1e3, "M": 1e6, "m": 1e-3,
              "μ": 1e-6, "u": 1e-6, "n": 1e-9, "p": 1e-12, "": 1.0}


def extract_params(text: str):
    """从文本提取 [(数值, 归一单位)]，如 '5V供电、1k电阻' -> [(5,'V'),(1000,'Ω')]。"""
    out = []
    for num, prefix, unit in PARAM_FULL.findall(str(text)):
        out.append((float(num) * PARAM_MULT[prefix],
                    PARAM_UNIT_MAP.get(unit, unit)))
    for num, prefix in PARAM_BARE.findall(str(text)):
        out.append((float(num) * PARAM_MULT[prefix], "Ω"))
    return out


def format_value(num: float, unit: str) -> str:
    """数值 -> 工程紧凑写法：470->'470Ω'，1000->'1kΩ'，1e6->'1MΩ'，5->'5V'。
    与一致性偏差报告的展示口径一致（同一函数，避免两处格式化漂移）。"""
    if unit == "Ω" and num >= 1e6:
        return f"{num / 1e6:g}MΩ"
    if unit == "Ω" and num >= 1000:
        return f"{num / 1e3:g}kΩ"
    return f"{num:g}{unit}"


# 可注入单位 -> 目标元件类型（只覆盖模板兜底实际含有的带值元件）
_INJECT_TYPES = {"V": ("battery", "vcc"), "Ω": ("resistor",),
                 "F": ("capacitor",), "H": ("inductor",)}
_LED_COLORS = ("红", "绿", "黄", "蓝", "白")


def inject_query_values(circuit: dict, query: str):
    """把用户描述里显式给出的参数值注入模板电路（就地修改，返回注入记录）。

    规则（保守，宁可不注也不误注）：
    - 某单位在描述中恰有 1 个不同取值时，注入到所有对应类型的元件
      （多个不同取值 = 有歧义，跳过并留给偏差报告提示核对）；
    - 仅覆盖已有 value 属性的元件（不改电路结构）；
    - LED 颜色词（红/绿/黄/蓝/白 + 色/灯/LED）注入到 led 元件。

    返回 [(位号, 旧值, 新值)]，供调用方写入来源说明/报告。"""
    notes = []
    parts = circuit.get("parts") or []
    # 数值注入：按单位分组去重
    by_unit = {}
    for num, unit in extract_params(query):
        by_unit.setdefault(unit, set()).add(num)
    for unit, types in _INJECT_TYPES.items():
        vals = by_unit.get(unit) or set()
        if len(vals) != 1:
            continue
        new_value = format_value(next(iter(vals)), unit)
        for p in parts:
            if p.get("type") in types and "value" in (p.get("attrs") or {}):
                old = p["attrs"]["value"]
                # 数值等价视为已采纳（如模板 1000Ω vs 描述 1k → 无需改动）
                if set(extract_params(str(old))) == set(extract_params(new_value)):
                    continue
                notes.append((str(p.get("id", "?")), old, new_value))
                p["attrs"]["value"] = new_value
    # LED 颜色注入
    leds = [p for p in parts if p.get("type") == "led"
            and "value" in (p.get("attrs") or {})]
    if leds:
        for c in _LED_COLORS:
            if f"{c}色" in query or f"{c}灯" in query or f"{c}LED" in query.upper():
                for p in leds:
                    if p["attrs"]["value"] != c:
                        notes.append((str(p.get("id", "?")), p["attrs"]["value"], c))
                        p["attrs"]["value"] = c
                break
    return notes


if __name__ == "__main__":
    import sys, json, os

    # --manifest <path>：把前端清单写成 JSON，供 test_frontend.js 做目录↔符号同步校验
    if len(sys.argv) >= 3 and sys.argv[1] == "--manifest":
        with open(sys.argv[2], "w", encoding="utf-8") as f:
            json.dump(to_frontend_manifest(), f, ensure_ascii=False, indent=2)
        print(f"manifest 已写入 {sys.argv[2]}（{len(CATALOG)} 种元件）")
        sys.exit(0)

    print("元件总数：", len(CATALOG))

    print("\n== 负向关键词：三种放大器必须各归各位 ==")
    for q in ["反相比例放大器", "同相放大器", "电压比较器", "电压跟随器", "运放", "运算放大器"]:
        print(f"  {q:>8} -> {match_one(q)[0]}")

    print("\n== 二极管家族消歧 ==")
    for q in ["一个普通二极管", "发光二极管LED", "稳压二极管齐纳"]:
        print(f"  {q} -> {match_one(q)[0]}")

    print("\n== 新增条目（与符号库同步）==")
    for q in ["保险丝", "晶振", "mos管", "vcc", "pmos"]:
        print(f"  {q} -> {match_one(q)[0]}")

    print("\n== 从整句需求抽取元件 ==")
    print(" ", match_components("用555定时器和电阻电容LED做一个闪烁灯，加个按钮复位"))
    print(" ", match_components("vcc经保险丝给mos管供电"))

    print("\n== ERC 知识库示例 ==")
    kb = build_erc_kb()
    print("  ne555 引脚电气类型：", kb["ne555"])

    print("\n== 引脚电气类型回归（负载件不得自带 GND/POWER；DISCH 是开漏输出） ==")
    print("  led    引脚类型：", kb["led"])
    print("  buzzer 引脚类型：", kb["buzzer"])
    assert kb["led"] == {"+": PASSIVE, "-": PASSIVE}, "LED 是负载，引脚不得记为 IN/GND（会误报电源短路）"
    assert kb["buzzer"] == {"+": PASSIVE, "-": PASSIVE}, "蜂鸣器同 LED，负载件引脚应为 PASSIVE"
    assert kb["ne555"]["DISCH"] == OUT, "NE555 的 DISCH 是集电极开路输出，应为 OUT"
    assert kb["battery"] == {"+": POWER, "-": GND}, "电池才是真正的电源/地源头，定义不得改动"
    assert kb["vcc"] == {"1": POWER} and kb["gnd"] == {"1": GND}, "vcc/gnd 符号定义不得改动"
    # 引脚名集合不能变：前端 symbols.js 的 manifest 一致性测试依赖它
    assert list(CATALOG["led"]["pins"]) == ["+", "-"], "LED 引脚名集合不得增删改名"
    assert list(CATALOG["buzzer"]["pins"]) == ["+", "-"], "蜂鸣器引脚名集合不得增删改名"

    print("\n== 引脚电气类型回归（建模重构） ==")
    assert kb["npn"] == {"B": IN, "C": PASSIVE, "E": PASSIVE}, "npn C/E 方向由电路决定，不得预设 OUT/GND"
    assert kb["pnp"] == {"B": IN, "C": PASSIVE, "E": PASSIVE}, "pnp C/E 同 npn（E=OUT 曾致高边开关 fatal）"
    assert kb["optocoupler"]["C"] == PASSIVE, "光耦集电极开路允许多路共线，C 应为 PASSIVE"
    assert kb["regulator_7805"] == {"IN": PWR_IN, "GND": GND, "OUT": PWR_OUT}, \
        "7805 IN/OUT 应为供电输入/供电输出"
    print("  npn/pnp C/E 双 PASSIVE、光耦 C PASSIVE、7805 PWR_IN/PWR_OUT ✔")

    print("\n== match_one_detail 多候选歧义 ==")
    d = match_one_detail("放大器")   # 反相/同相放大器模糊分相同 → 必须判歧
    print("  “放大器” ->", d["type"], d["score"], "次优", d["runner_up"],
          d["runner_up_score"], "歧义", d["ambiguous"])
    assert d["ambiguous"], "模糊分相同的相近候选必须判歧"
    assert d["runner_up"] is not None and d["runner_up_score"] >= FUZZY_CUTOFF
    d2 = match_one_detail("反相比例放大器")
    print("  “反相比例放大器” ->", d2["type"], d2["score"], "歧义", d2["ambiguous"])
    assert d2["type"] == "opamp_inverting" and not d2["ambiguous"], "满分命中不该判歧"

    print("\n== 二极管双别名现状锁定（已知不忠实但本轮不改行为） ==")
    # 1N4148 与 1N4007 属同一 diode 类型、default 固定 1N4148 → 用户写 1N4007
    # 时器件值仍落到 1N4148。类型判定是正确的，只是具体型号不忠实。这里把
    # 「当前行为」钉住：将来若真按需求回填型号，这条断言会变红，提醒作者这是
    # 有意的行为变更（需同步更新 benchmark 黄金快照），而不是无声漂移。
    assert CATALOG["diode"]["default"] == "1N4148", "二极管默认型号变更需同步快照与文档"
    assert "1n4007" in CATALOG["diode"]["aliases"], "1N4007 仍应收录为二极管别名"
    assert match_one("1N4007")[0] == "diode" and match_one("1N4148")[0] == "diode", \
        "两个型号都归 diode 类型（现状锁定）"
    print("  1N4148/1N4007 同归 diode，default=1N4148（型号不忠实，见 catalog 注释）")
    print("  match_one_detail 歧义判定 ✔")
    print("  引脚电气类型回归 ✔  （LED/蜂鸣器双 PASSIVE、DISCH=OUT、电池/vcc/gnd 不变）")
