# -*- coding: utf-8 -*-
"""
symbol_import.py —— KiCad 官方符号库(.kicad_sym) → 智绘电路目录草稿（零依赖）
=====================================================================
元件库扩展协议（"新增一种元件 = 一段 JSON + 一组测试"）：
  1) 运行本工具从 KiCad 官方库解析目标符号：
       python core/symbol_import.py "D:/KICAD/share/kicad/symbols/Device.kicad_sym" \
           --names Thermistor,LDR --json draft.json
  2) 对照 draft.json 补齐四处（前两处可由草稿直接誊写）：
       core/catalog.py   CATALOG 条目（pins 电气类型已按映射表给出）
       web/symbols.js    LIB SVG 符号（ports 与草稿 pins 一一对应）
       kicad/symbols.py  嵌入符号定义 + sch_writer.TYPE_MAP 映射
       web/app.js        PART_INFO 插入信息表
  3) 重新生成 frontend_manifest.json（python core/catalog.py --manifest …），
     跑 tests/run_tests.py —— test_render 校验目录↔符号同步，新元件全链路生效。
协议要点：CATALOG 是单一数据源，匹配/ERC/manifest 自动派生；符号库与
TYPE_MAP 是"类型集合"的两端，任一端新增都必须同步（两侧测试会拦截）。

解析器：手写 S 表达式 tokenizer（括号/引号字符串/原子），零第三方依赖，
可处理官方库中的中文描述、嵌套 unit（symbol "X_1_1"）与富文本 property。

电气类型映射（KiCad etype -> 本项目 ERC 类型，与 erc.py 词汇一致）：
  power_out -> POWER    power_in -> PWR_IN    input -> IN    output -> OUT
  passive / bidirectional / tri_state / unspecified / open_* / no_connect -> PASSIVE
  （负载件统一 PASSIVE 的纪律见 catalog.py 里 LED/蜂鸣器的修复注释；
    开漏 open_collector 语义上更接近 OUT——NE555 DISCH 即如此定义——
    但保守起见导入草稿一律 PASSIVE，由人工评审后改判，避免自动误判。）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------
# S 表达式解析（零依赖）：token 流 -> 嵌套 [原子...] 列表
# ---------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r'"(?:[^"\\]|\\.)*"'      # 双引号字符串（支持 \" 转义）
    r'|[()]'                   # 括号
    r'|[^\s()"]+'              # 裸原子（数字/符号名）
)


def _tokenize(text: str):
    # 去掉行注释（KiCad 库文件偶有 ; 注释）
    for line in text.splitlines():
        code = line.split(";", 1)[0] if line.lstrip().startswith(";") else line
        yield from _TOKEN_RE.findall(code)


def _atom(tok: str) -> str:
    """字符串 token 去引号并反转义；裸原子原样返回。
    反转义用单遍正则（\\x -> x），避免\"先处理 \\" 再处理 \\\\\"的顺序歧义。"""
    if len(tok) >= 2 and tok.startswith('"') and tok.endswith('"'):
        return re.sub(r"\\(.)", r"\1", tok[1:-1])
    return tok


def parse_sexpr(text: str):
    """解析 .kicad_sym 全文为嵌套列表；字符串节点为 str，结构为 list。
    语法错误（括号不配对/多余右括号）抛 ValueError。"""
    stack, root = [], None
    for tok in _tokenize(text):
        if tok == "(":
            node = []
            if stack:
                stack[-1].append(node)
            else:
                if root is not None:
                    raise ValueError("多余的顶层表达式（括号不配对？）")
                root = node
            stack.append(node)
        elif tok == ")":
            if not stack:
                raise ValueError("右括号多余（括号不配对）")
            stack.pop()
        else:
            if not stack:
                raise ValueError(f"括号外的裸原子：{tok[:40]}")
            stack[-1].append(_atom(tok))
    if stack:
        raise ValueError("括号未闭合")
    if root is None:
        raise ValueError("空文档")
    return root


# ---------------------------------------------------------------------
# 符号遍历与引脚提取
# ---------------------------------------------------------------------

def _children(node, head):
    """node 的直接子表达式中，首元素等于 head 的那些。"""
    return [c for c in node if isinstance(c, list) and c and c[0] == head]


def iter_symbol_nodes(ast):
    """顶层 (kicad_symbol_lib ...) 下的全部 (symbol "NAME" ...) 节点。
    兼容两种输入形态：ast 即 lib 节点（parse_sexpr 对单顶层文档的返回值），
    或 ast 是包含 lib 节点的更高层列表。"""
    if isinstance(ast, list) and ast and ast[0] == "kicad_symbol_lib":
        libs = [ast]
    else:
        libs = _children(ast, "kicad_symbol_lib")
    out = []
    for lib in libs:
        out.extend(_children(lib, "symbol"))
    return out


def symbol_name(node) -> str:
    for c in node[1:]:
        if isinstance(c, str):
            return c
    return "?"


def _walk_symbols(node):
    """深度优先展开符号及其 unit 子符号（X_0_1 / X_1_1 等）。"""
    yield node
    for sub in _children(node, "symbol"):
        yield from _walk_symbols(sub)


def symbol_pins(node) -> list:
    """收集符号（含全部 unit 子符号）的引脚 ->
    [{number, name, etype, at:[x,y,angle], length}]，按引脚号排序。"""
    pins = []
    for sym in _walk_symbols(node):
        for pin in _children(sym, "pin"):
            if len(pin) < 3 or not isinstance(pin[1], str):
                continue
            etype, shape = pin[1], pin[2]
            at, length, name, number = None, None, "~", ""
            for c in pin[3:]:
                if isinstance(c, list) and c:
                    if c[0] == "at" and len(c) >= 4:
                        at = c[1:4]
                    elif c[0] == "length" and len(c) >= 2:
                        length = c[1]
                    elif c[0] == "name" and len(c) >= 2:
                        name = c[1]
                    elif c[0] == "number" and len(c) >= 2:
                        number = c[1]
            pins.append({"number": number, "name": name, "etype": etype,
                         "shape": shape, "at": at, "length": length})
    pins.sort(key=lambda p: (len(p["number"]), p["number"]))
    return pins


# ---------------------------------------------------------------------
# 电气类型映射（扩展协议的核心知识，见模块 docstring）
# ---------------------------------------------------------------------

ETYPE_MAP = {
    "power_out": "POWER",
    "power_in": "PWR_IN",
    "input": "IN",
    "output": "OUT",
    # 保守归类：以下类型默认 PASSIVE，人工评审后可改判
    #（如 open_collector 输出可改 OUT，参照 CATALOG 里 ne555.DISCH 的先例）
    "passive": "PASSIVE",
    "bidirectional": "PASSIVE",
    "tri_state": "PASSIVE",
    "unspecified": "PASSIVE",
    "no_connect": "PASSIVE",
    "open_collector": "PASSIVE",
    "open_emitter": "PASSIVE",
    "open_collector_lo": "PASSIVE",
    "open_emitter_hi": "PASSIVE",
}


def mapped_etype(kicad_etype: str) -> str:
    """KiCad 电气类型 -> 项目 ERC 类型；未知类型保守归 PASSIVE（不抛错，
    官方库偶有非标准值，草稿阶段宁松勿错，评审环节兜底）。"""
    return ETYPE_MAP.get(kicad_etype, "PASSIVE")


# 符号名 -> 建议位号前缀（教学常用件直查，其余默认 U；
# 与 CATALOG 既有前缀保持一致，前缀冲突由人工评审调整）
_REF_PREFIX = {
    "R": "R", "R_Potentiometer": "RV", "C": "C", "C_Polarized": "C", "D": "D",
    "D_Zener": "D", "D_TVS": "D", "LED": "LED", "L": "L", "Q": "Q",
    "Q_NPN_BEC": "Q", "Q_PNP_BEC": "Q", "Q_NMOS_DGS": "Q", "Q_PMOS_DGS": "Q",
    "SW_Push": "SW", "SW_SPST": "SW", "Battery": "BAT", "Battery_Cell": "BAT",
    "Fuse": "F", "Crystal": "Y", "Resonator": "Y", "Buzzer": "BZ",
    "Speaker": "LS", "Thermistor": "RT", "LDR": "R", "Varistor": "RV",
    "Ferrite_Bead": "FB", "PWR_FLAG": "#FLG", "VCC": "VCC", "GND": "GND",
}


def _ref_prefix(name: str) -> str:
    if name in _REF_PREFIX:
        return _REF_PREFIX[name]
    head = name.split("_", 1)[0]
    return _REF_PREFIX.get(head, "U")


def draft_entry(node) -> dict:
    """单个符号节点 -> 目录草稿（字段命名与 CATALOG 条目对齐）。"""
    name = symbol_name(node)
    pins = symbol_pins(node)
    return {
        "kicad_lib_id": name,
        "type": name.lower(),                     # 建议类型 key（人工复核）
        "ref": _ref_prefix(name),
        "name": name,                             # 中文显示名待人工翻译
        "pins": {p["number"]: mapped_etype(p["etype"]) for p in pins},
        "pin_detail": [{k: p[k] for k in ("number", "name", "etype")}
                       for p in pins],
        "aliases": [],                            # 中文别名必须人工补充
        "negative": [],                           # 消歧义词必须人工补充
        "default": "",
    }


def draft_from_lib(lib_path, names=None) -> dict:
    """解析 .kicad_sym 文件，返回 {符号名: 草稿}。
    names 给定时只导出指定符号；未命中任何符号抛 KeyError 提示。"""
    text = Path(lib_path).read_text(encoding="utf-8")
    ast = parse_sexpr(text)
    wanted = set(names or [])
    out = {}
    for node in iter_symbol_nodes(ast):
        name = symbol_name(node)
        if wanted and name not in wanted:
            continue
        out[name] = draft_entry(node)
    if wanted:
        missing = wanted - set(out)
        if missing:
            raise KeyError(f"库中未找到符号：{', '.join(sorted(missing))}")
    return out


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def main(argv) -> int:
    args = list(argv)
    if not args or args[0].startswith("--"):
        print("用法：python symbol_import.py <lib.kicad_sym> "
              "[--names A,B] [--json out.json]")
        return 2
    lib_path = args[0]
    names = None
    out_json = None
    i = 1
    while i < len(args):
        if args[i] == "--names" and i + 1 < len(args):
            names = [n.strip() for n in args[i + 1].split(",") if n.strip()]
            i += 2
        elif args[i] == "--json" and i + 1 < len(args):
            out_json = args[i + 1]
            i += 2
        else:
            print(f"错误：未知参数 {args[i]}")
            return 2
    try:
        drafts = draft_from_lib(lib_path, names)
    except (OSError, ValueError, KeyError) as e:
        print(f"错误：{e}")
        return 1
    payload = json.dumps(drafts, ensure_ascii=False, indent=2)
    if out_json:
        Path(out_json).write_text(payload + "\n", encoding="utf-8")
        print(f"草稿已写入 {out_json}（{len(drafts)} 个符号）")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
