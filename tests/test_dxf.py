#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_dxf.py —— DXF 兼容性优化器测试（第 14 套件）
===========================================================
覆盖 dxf.py 的四点承诺：
  1) 头变量补全: $ACADVER/$DWGCODEPAGE/$EXTMIN/$EXTMAX 缺则补齐且数值正确;
  2) 单位重标定: --mm 时坐标/半径 ×25.4、$INSUNITS=4、$MEASUREMENT=1、
     长度保形（等比缩放的数学保证）;
  3) 正象限平移: --shift-origin 后无负坐标、$EXTMIN≈原点、平移只改位置不改形状;
  4) 表完整性:   实体引用未定义图层时自动补 LAYER 条目；STYLE 补 STANDARD;
     几何数据在默认模式下逐字节保持。

运行：python -B tests/test_dxf.py   （项目根执行；全程离线，不依赖 kicad-cli）
"""

from __future__ import annotations

# 测试位于 tests/ 子目录：先把项目根插入 sys.path 再 import 项目模块
import os as _os
import sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT)

import math
import subprocess
import sys
import tempfile
from pathlib import Path

from core import dxf as D

PASS = 0


def check(name, cond):
    global PASS
    assert cond, f"失败: {name}"
    PASS += 1
    print(f"  ✔ {name}")


# ---- 合成 R12 风格 fixture：kicad-cli 导出的关键缺陷样样俱全 ----
# 头部只有 2 个变量（缺 ACADVER/CODEPAGE/EXTMIN/EXTMAX）、INSUNITS=1(英寸)、
# MEASUREMENT=0；STYLE 表无 STANDARD；LINE 坐标为带 16 位小数尾巴的英寸值。
FIXTURE = """0
SECTION
2
HEADER
9
$INSUNITS
70
1
9
$MEASUREMENT
70
0
0
ENDSEC
0
SECTION
2
TABLES
0
TABLE
2
LAYER
70
2
0
LAYER
2
WIRING
70
0
62
7
6
CONTINUOUS
0
LAYER
2
PARTS
70
0
62
3
6
CONTINUOUS
0
ENDTAB
0
TABLE
2
STYLE
70
1
0
STYLE
2
KICAD
70
0
40
0.0
41
1.0
42
1.0
50
0.0
71
0
3
kicadfont
4

0
ENDTAB
0
ENDSEC
0
SECTION
2
ENTITIES
0
LINE
8
WIRING
6
CONTINUOUS
10
0.5
20
-1.0
11
2.5
21
-1.0
0
CIRCLE
8
PARTS
6
CONTINUOUS
10
1.0
20
0.5
40
0.25
0
ENDSEC
0
EOF
"""


def _write_fixture(tmp: Path) -> Path:
    p = tmp / "fixture.dxf"
    p.write_text(FIXTURE, encoding="utf-8")
    return p


def _trio(pairs, name):
    i = D._header_var_index(pairs, name)
    assert i is not None, f"缺头变量 {name}"
    vals = {}
    for c, v in pairs[i + 1:i + 8]:
        if c == 9:
            break
        if c in (10, 20, 30):
            vals[c] = float(v)
    return vals


def _lines(pairs):
    out = []
    for t, props in D._iter_entities(pairs):
        if t != "LINE":
            continue
        d = {c: float(v) for c, v in props if c in (10, 11, 20, 21)}
        out.append(((d[10], d[20]), (d[11], d[21])))
    return out


def _length(seg):
    (x0, y0), (x1, y1) = seg
    return math.hypot(x1 - x0, y1 - y0)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src = _write_fixture(tmp)
        base = D._parse(FIXTURE)

        print("— 默认模式：头补全 + 表完整性，几何逐字节不变 —")
        out1 = tmp / "d1.dxf"
        rep = D.optimize_file(src, out1)
        p1 = D._parse(out1.read_text(encoding="utf-8"))
        i = D._header_var_index(p1, "$ACADVER")
        check("$ACADVER 补齐且为 AC1009", i is not None and p1[i + 1] == (1, "AC1009"))
        sec = D._find_section(p1, "HEADER")
        check("$ACADVER 位于 HEADER 首位", p1[sec[0] + 2] == (9, "$ACADVER"))
        i = D._header_var_index(p1, "$DWGCODEPAGE")
        check("$DWGCODEPAGE 补齐为 ANSI_936",
              i is not None and p1[i + 1] == (3, "ANSI_936"))
        ext = _trio(p1, "$EXTMIN")
        ext2 = _trio(p1, "$EXTMAX")
        # 几何: LINE (0.5,-1)-(2.5,-1)，CIRCLE (1,0.5) r=0.25
        check("$EXTMIN 三元组=(0.5,-1,0)",
              ext == {10: 0.5, 20: -1.0, 30: 0.0})
        check("$EXTMAX 三元组=(2.5,0.75,0)",
              ext2 == {10: 2.5, 20: 0.75, 30: 0.0})
        check("STYLE 表补 STANDARD",
              "STANDARD" in {next((v for c, v in pr if c == 2), "")
                             for t, pr in D._iter_entities(p1)} | _style_names(p1))
        check("实体数不变", len(list(D._iter_entities(base)))
              == len(list(D._iter_entities(p1))) == 2)
        geo_before = [(c, v) for t, pr in _ent(base, "LINE") for c, v in pr]
        geo_after = [(c, v) for t, pr in _ent(p1, "LINE") for c, v in pr]
        check("默认模式几何值逐字节不变", geo_before == geo_after)
        check("INSUNITS 默认模式不动", _hv(p1, "$INSUNITS") == "1")
        check("审计报告: 无警告", not rep["warnings"])

        print("— --mm：×25.4 重标定，长度保形 —")
        out2 = tmp / "d2.dxf"
        D.optimize_file(src, out2, mm=True)
        p2 = D._parse(out2.read_text(encoding="utf-8"))
        check("$INSUNITS → 4 (mm)", _hv(p2, "$INSUNITS") == "4")
        check("$MEASUREMENT → 1 (公制)", _hv(p2, "$MEASUREMENT") == "1")
        segs = _lines(p2)
        check("坐标 ×25.4（0.5→12.7, -1→-25.4）",
              segs[0][0] == (12.7, -25.4) and segs[0][1] == (63.5, -25.4))
        r = [float(v) for t, pr in _ent(p2, "CIRCLE") for c, v in pr if c == 40][0]
        check("半径 ×25.4（0.25→6.35）", abs(r - 6.35) < 1e-9)
        L0, L1_ = _length(_lines(base)[0]), _length(segs[0])
        check("长度保形: |L₂ − 25.4·L₀| < 1e-9", abs(L1_ - 25.4 * L0) < 1e-9)
        check("无负坐标容忍（未开平移时保持原符号）", segs[0][0][1] == -25.4)

        print("— --mm --shift-origin：正象限，形状不变 —")
        out3 = tmp / "d3.dxf"
        D.optimize_file(src, out3, mm=True, shift_origin=True)
        p3 = D._parse(out3.read_text(encoding="utf-8"))
        segs3 = _lines(p3)
        allv = [v for s in segs3 for pt in s for v in pt]
        check("平移后无负坐标", all(v >= -1e-9 for v in allv))
        ext3 = _trio(p3, "$EXTMIN")
        check("$EXTMIN ≈ (0,0,0)", ext3[10] == 0 and ext3[20] == 0)
        check("平移不改长度（与 mm 版等长）",
              abs(_length(segs3[0]) - _length(segs[0])) < 1e-9)
        r3 = [float(v) for t, pr in _ent(p3, "CIRCLE") for c, v in pr if c == 40][0]
        check("平移不改半径", abs(r3 - 6.35) < 1e-9)

        print("— 表完整性：未定义图层自动补齐 —")
        fx2 = FIXTURE.replace("8\nWIRING", "8\nNEWLAYER", 1)
        src2 = tmp / "fixture2.dxf"
        src2.write_text(fx2, encoding="utf-8")
        out4 = tmp / "d4.dxf"
        rep4 = D.optimize_file(src2, out4)
        p4 = D._parse(out4.read_text(encoding="utf-8"))
        check("NEWLAYER 补入 LAYER 表", "NEWLAYER" in D._layer_names_defined(p4))
        check("补表后无未定义引用",
              not (D._layers_referenced(p4) - D._layer_names_defined(p4)))
        check("审计报告记录 layers_added",
              rep4.get("layers_added") == ["NEWLAYER"])

        print("— CLI 与健壮性 —")
        r = subprocess.run([sys.executable, "-B", _os.path.join(_ROOT, "core", "dxf.py"), str(src),
                            "-o", str(tmp / "cli.dxf"), "--mm", "--shift-origin"],
                           capture_output=True, text=True, encoding="utf-8")
        check("CLI exit 0 且产出文件", r.returncode == 0
              and (tmp / "cli.dxf").is_file())
        bad = tmp / "bad.dxf"
        bad.write_text("0\nSECTION\n2\nHEADER\n残缺", encoding="utf-8")
        r2 = subprocess.run([sys.executable, "-B", _os.path.join(_ROOT, "core", "dxf.py"), str(bad),
                             "-o", str(tmp / "bad_out.dxf")],
                            capture_output=True, text=True, encoding="utf-8")
        check("损坏文件 exit 2 + 中文报错", r2.returncode == 2
              and "解析失败" in (r2.stderr or ""))

        print("— 真实 kicad-cli 产物（存在时）：整图保形抽检 —")
        real = Path("demo_out/led_limit_v2/cad_dxf")
        orig = next((p for p in real.glob("*.dxf")
                     if "_CAD导入版" not in p.stem), None) \
            if real.is_dir() else None
        if orig is not None:
            po = D._parse(orig.read_text(encoding="utf-8"))
            opt = tmp / "real_opt.dxf"
            repr_ = D.optimize_file(orig, opt, mm=True, shift_origin=True)
            pp = D._parse(opt.read_text(encoding="utf-8"))
            check("真实产物: 实体数一致",
                  len(list(D._iter_entities(po))) == len(list(D._iter_entities(pp))))
            check("真实产物: 图层引用一致",
                  sorted(D._layers_referenced(po)) == sorted(D._layers_referenced(pp)))
            s0, s1 = _lines(po), _lines(pp)
            dev = max((abs(_length(a) * 25.4 - _length(b)) for a, b in zip(s0, s1)),
                      default=0.0)
            check(f"真实产物: {len(s0)} 条线段长度保形（最大偏差 {dev:.2e} mm）",
                  dev < 1e-6)
            check("真实产物: mm 头变量就位",
                  _hv(pp, "$INSUNITS") == "4" and _hv(pp, "$MEASUREMENT") == "1")
        else:
            print("  （跳过：demo_out 真实 DXF 不存在，AI 生成后可复跑）")

    print(f"\n✔ DXF 优化器测试：{PASS} 项断言全部通过")
    return 0


# ---- 小工具 ----

def _hv(pairs, name):
    i = D._header_var_index(pairs, name)
    return pairs[i + 1][1] if i is not None else None


def _ent(pairs, etype):
    return [(t, pr) for t, pr in D._iter_entities(pairs) if t == etype]


def _style_names(pairs):
    tbl = D._find_table(pairs, "STYLE")
    names = set()
    if tbl:
        cur = None
        for i in range(tbl[0] + 2, tbl[1]):
            c, v = pairs[i]
            if c == 0:
                cur = v.strip() == "STYLE"
            elif cur and c == 2:
                names.add(v.strip())
                cur = False
    return names


if __name__ == "__main__":
    raise SystemExit(main())
