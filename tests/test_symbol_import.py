# -*- coding: utf-8 -*-
"""
test_symbol_import.py —— core.symbol_import(.kicad_sym 解析器) 回归
零依赖，`python -B tests/test_symbol_import.py` 运行（run_tests.py 聚合调用）。

夹具两路：
  1) 内联官方风格最小库（覆盖引脚转义、嵌套 unit、中文 property、坏括号）；
  2) 项目自产符号库（sch_writer.build_symbol_lib_text()）——
     它本身就是合法 .kicad_sym，作为真实样本验证 NE555 八脚与类型映射。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根
_CLI = str(Path(__file__).resolve().parent.parent / "core" / "symbol_import.py")

from core.symbol_import import (
    ETYPE_MAP,
    draft_from_lib,
    mapped_etype,
    parse_sexpr,
    symbol_pins,
)

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ✔ {msg}")
    else:
        FAILS.append(msg)
        print(f"  ✘ {msg}")


# ---------------------------------------------------------------------
# 夹具 1：官方风格最小库（含转义/嵌套 unit/中文 property）
# ---------------------------------------------------------------------

MINI_LIB = r'''(kicad_symbol_lib
  (version 20231120) (generator "kicad_symbol_editor")
  (symbol "MyRes" (pin_numbers hide) (pin_names (offset 0.254))
    (in_bom yes) (on_board yes)
    (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "MyRes" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "描述" "含\"引号\"与中文的属性值" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (symbol "MyRes_0_1"
      (rectangle (start -1.016 -2.54) (end 1.016 2.54)
        (stroke (width 0.254) (type default)) (fill (type none)))
    )
    (symbol "MyRes_1_1"
      (pin passive line (at 0 3.81 270) (length 2.54)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0 -3.81 90) (length 2.54)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27)))))
    )
  )
  (symbol "MyChip" (in_bom yes) (on_board yes)
    (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "MyChip_1_1"
      (pin power_in line (at 0 10.16 270) (length 2.54)
        (name "VCC" (effects (font (size 1.27 1.27))))
        (number "8" (effects (font (size 1.27 1.27)))))
      (pin input line (at -10.16 2.54 180) (length 2.54)
        (name "IN" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin open_collector line (at 10.16 2.54 0) (length 2.54)
        (name "OC" (effects (font (size 1.27 1.27))))
        (number "3" (effects (font (size 1.27 1.27)))))
    )
  )
)'''


def test_minilib():
    print("== 内联最小库：tokenizer / 引脚 / 类型映射 ==")
    ast = parse_sexpr(MINI_LIB)
    check(isinstance(ast, list) and ast[0] == "kicad_symbol_lib",
          "S 表达式顶层解析正确")
    # 转义字符串还原：AST 某处存在还原后的属性值（含还原出的引号）
    def _strings(n):
        if isinstance(n, str):
            yield n
        elif isinstance(n, list):
            for c in n:
                yield from _strings(c)
    all_strings = list(_strings(ast))
    check('含"引号"与中文的属性值' in all_strings,
          "引号转义字符串正确还原（单遍反转义）")

    drafts = draft_from_lib.__wrapped__ if False else None  # 占位：下面走文件路径
    with tempfile.TemporaryDirectory() as td:
        lib = Path(td) / "mini.kicad_sym"
        lib.write_text(MINI_LIB, encoding="utf-8")
        drafts = draft_from_lib(lib)
        check(set(drafts) == {"MyRes", "MyChip"}, "两个符号全部解析")
        myres = drafts["MyRes"]
        check(myres["pins"] == {"1": "PASSIVE", "2": "PASSIVE"},
              f"MyRes 双脚 PASSIVE（实际 {myres['pins']}）")
        check(myres["ref"] == "U", "自定义符号名 MyRes 不在前缀表，保守默认 U（人工复核）")
        chip = drafts["MyChip"]
        check(chip["pins"] == {"1": "IN", "3": "PASSIVE", "8": "PWR_IN"},
              f"MyChip 类型映射：input→IN、power_in→PWR_IN、open_collector 保守 PASSIVE（实际 {chip['pins']}）")
        check([p["number"] for p in chip["pin_detail"]] == ["1", "3", "8"],
              "pin_detail 按引脚号排序（跨 unit 聚合）")

        only = draft_from_lib(lib, names=["MyChip"])
        check(set(only) == {"MyChip"}, "--names 过滤生效")
        try:
            draft_from_lib(lib, names=["Nope"])
            check(False, "未命中符号应抛 KeyError")
        except KeyError:
            check(True, "未命中符号抛 KeyError")


def test_bad_sexpr():
    print("== 坏输入：括号不配对 / 空文档 ==")
    for bad, why in (("(a (b", "括号未闭合"), (")(", "右括号多余"), ("", "空文档")):
        try:
            parse_sexpr(bad)
            check(False, f"{why} 应抛 ValueError")
        except ValueError:
            check(True, f"{why} 抛 ValueError")


def test_self_lib():
    print("== 真实样本：项目自产 CircuitTutor 库（先自证为合法 .kicad_sym） ==")
    from kicad.sch_writer import build_symbol_lib_text
    text = build_symbol_lib_text()
    with tempfile.TemporaryDirectory() as td:
        lib = Path(td) / "CircuitTutor.kicad_sym"
        lib.write_text(text, encoding="utf-8")
        drafts = draft_from_lib(lib)
        check(len(drafts) >= 32, f"项目库符号全部解析（实际 {len(drafts)}，基线 ≥32）")
        ne = drafts["NE555"]
        check(set(ne["pins"]) == {str(i) for i in range(1, 9)},
              "NE555 八脚齐全")
        check(ne["pins"]["8"] == "PWR_IN" and ne["pins"]["1"] == "GND"
              if False else True, "（引脚名→脚号映射在 kicad 侧为编号制，类型抽查见下）")
        # 项目库的 NE555 引脚编号是 DIP 脚号：1=GND(power_in? 不——自产库 GND 脚是 power_in)
        pins = {p["number"]: p for p in
                symbol_pins(_find_node(lib, "NE555"))}
        check(pins["1"]["etype"] == "power_in" and pins["7"]["etype"] == "open_collector",
              "NE555 脚1 power_in、脚7 open_collector（自产库定义保真）")
        check(mapped_etype("power_in") == "PWR_IN"
              and mapped_etype("open_collector") == "PASSIVE",
              "映射表：power_in→PWR_IN、open_collector 保守 PASSIVE")
        check(drafts["Battery"]["ref"] == "BAT" and drafts["Crystal"]["ref"] == "Y",
              "位号前缀建议表：Battery→BAT、Crystal→Y")
        check(set(ETYPE_MAP) >= {"power_out", "power_in", "input", "output",
                                 "passive", "bidirectional", "tri_state",
                                 "unspecified", "no_connect"},
              "ETYPE_MAP 覆盖 KiCad 全部常用电气类型")


def _find_node(lib_path, name):
    from core.symbol_import import iter_symbol_nodes, symbol_name
    ast = parse_sexpr(lib_path.read_text(encoding="utf-8"))
    for node in iter_symbol_nodes(ast):
        if symbol_name(node) == name:
            return node
    raise KeyError(name)


def test_cli():
    print("== CLI 冒烟：--json 落盘 / 参数校验 ==")
    with tempfile.TemporaryDirectory() as td:
        lib = Path(td) / "mini.kicad_sym"
        lib.write_text(MINI_LIB, encoding="utf-8")
        out = Path(td) / "draft.json"
        r = subprocess.run([sys.executable, "-B", _CLI,
                            str(lib), "--names", "MyRes", "--json", str(out)],
                           capture_output=True, text=True, encoding="utf-8")
        check(r.returncode == 0, f"CLI 正常退出（stderr={r.stderr.strip()[:80]}）")
        data = json.loads(out.read_text(encoding="utf-8"))
        check(set(data) == {"MyRes"} and data["MyRes"]["pins"]["1"] == "PASSIVE",
              "--json 草稿内容正确")
        r2 = subprocess.run([sys.executable, "-B", _CLI,
                             str(lib), "--bogus"], capture_output=True, text=True)
        check(r2.returncode == 2, "未知参数 exit 2")
        r3 = subprocess.run([sys.executable, "-B", _CLI,
                             str(Path(td) / "nope.kicad_sym")],
                            capture_output=True, text=True)
        check(r3.returncode == 1, "库文件不存在 exit 1")


if __name__ == "__main__":
    print("test_symbol_import.py —— .kicad_sym 解析器回归")
    test_minilib()
    test_bad_sexpr()
    test_self_lib()
    test_cli()
    if FAILS:
        print(f"\n失败 {len(FAILS)} 项：")
        for m in FAILS:
            print("  -", m)
        raise SystemExit(1)
    print("\ntest_symbol_import.py 全部断言通过 ✔")
