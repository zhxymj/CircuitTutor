#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DXF 兼容性优化器（零依赖，面向 CAD 导入场景）。

针对 kicad-cli 导出的 DXF（也适用于一般 R12 风格 DXF）做四类无损优化：

1. 头变量补全 —— 补 $ACADVER（部分第三方解析器据此判定版本）、
   $DWGCODEPAGE、$EXTMIN/$EXTMAX（轻量查看器靠它确定初始视野，
   缺失时"打开一片空白"）；
2. --mm 单位重标定 —— kicad-cli 以英寸存坐标（KiCad mm ÷25.4，出现
   6.9685905511811024 这类 16 位小数尾巴），统一 ×25.4 换算为毫米，
   $INSUNITS→4（mm）、$MEASUREMENT→1，坐标规整 ≤6 位小数
   （0.000001mm，视觉零差异），文件更小、符合国内 CAD 使用习惯；
3. --shift-origin 平移到第一象限 —— 负坐标合法，但部分简陋导入器
   对负坐标处理不佳；
4. 表完整性 —— 实体引用了未定义图层时补 LAYER 条目；STYLE 表缺
   STANDARD 时克隆补齐。

所有几何变换均为"等比缩放 + 等距平移"：线段长度/角度、连通关系、
元件相对位置严格不变。角度(50)、标志(70)等无量纲组码永不参与变换。

CLI:
    python dxf.py in.dxf [-o out.dxf] [--mm] [--shift-origin] [--report r.md]

已知边界：INSERT/BLOCK 的块变换语义复杂（基点/比例因子无量纲），
本工具不做块展开；KiCad 原理图 DXF 为全平铺实体（无块），不受影响。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ACADVER_R12 = "AC1009"          # 最低公共版本：AutoCAD R12，兼容性最广
INCH_TO_MM = 25.4

# 实体类型 → 随单位缩放/平移的组码集合。
# 10/11/12/13 = x 类坐标；20/21/22/23 = y 类；40 = 半径/字高/线宽（有量纲）。
# 50(角度)、70(标志)、62(颜色) 等无量纲组码不在此列。
_XY = {10, 11, 12, 13, 20, 21, 22, 23}
_SCALE_BY_TYPE = {
    "LINE": _XY,
    "POINT": {10, 20},
    "CIRCLE": {10, 20, 40},
    "ARC": {10, 20, 40},
    "SOLID": _XY,
    "TRACE": _XY,
    "TEXT": {10, 20, 40, 11, 21},
    "ATTRIB": {10, 20, 40, 11, 21},
    "MTEXT": {10, 20, 40, 41, 11, 21, 12, 22},
    "LWPOLYLINE": {10, 20, 40, 41, 43},
    "VERTEX": {10, 20, 40, 41},
    "INSERT": {10, 20},          # 仅平移插入点；比例因子(41/42)无量纲不可缩放
}


# ---------------------------------------------------------------- 解析/序列化

def _parse(text: str) -> list:
    """把 DXF 文本解析为 (组码int, 值str) 对列表；格式损坏时抛 ValueError。"""
    lines = text.splitlines()
    if len(lines) % 2 != 0:
        raise ValueError("DXF 行数为奇数：组码/值未成对，文件可能已损坏")
    pairs = []
    for i in range(0, len(lines), 2):
        code = lines[i].strip()
        if not (code.lstrip("-").isdigit()):
            raise ValueError(f"第 {i + 1} 行不是合法组码：{lines[i]!r}")
        pairs.append((int(code), lines[i + 1]))
    return pairs


def _dump(pairs) -> str:
    """规范化输出：组码行顶格 + 值行（DXF 对缩进无要求，顶格最通用）。"""
    return "".join(f"{c}\n{v}\n" for c, v in pairs)


# ---------------------------------------------------------------- 区段定位

def _find_section(pairs, name: str):
    """返回 (SECTION对索引, ENDSEC对索引)；不存在返回 None。"""
    for i, (c, v) in enumerate(pairs):
        if c == 0 and v.strip() == "SECTION" and i + 1 < len(pairs) \
                and pairs[i + 1] == (2, name):
            for j in range(i + 2, len(pairs)):
                if pairs[j][0] == 0 and pairs[j][1].strip() == "ENDSEC":
                    return i, j
            return i, len(pairs) - 1
    return None


def _iter_entities(pairs):
    """产出 (实体类型, [(组码, 值)...])，只遍历 ENTITIES 区。"""
    sec = _find_section(pairs, "ENTITIES")
    if not sec:
        return
    cur = None
    for i in range(sec[0] + 2, sec[1]):
        c, v = pairs[i]
        if c == 0:
            if cur:
                yield cur
            cur = (v.strip(), [])
        elif cur is not None:
            cur[1].append((c, v))
    if cur:
        yield cur


def _find_table(pairs, name: str):
    """返回 (TABLE对索引, ENDTAB对索引)；不存在返回 None。"""
    for i, (c, v) in enumerate(pairs):
        if c == 0 and v.strip() == "TABLE" and i + 1 < len(pairs) \
                and pairs[i + 1] == (2, name):
            for j in range(i + 2, len(pairs)):
                if pairs[j][0] == 0 and pairs[j][1].strip() == "ENDTAB":
                    return i, j
    return None


# ---------------------------------------------------------------- 头变量

def _header_var_index(pairs, name: str):
    for i, (c, v) in enumerate(pairs):
        if c == 9 and v.strip() == name:
            return i
    return None


def _header_ensure(pairs, name: str, code: int, value: str) -> bool:
    """缺则补该头变量（插到 HEADER 区 ENDSEC 之前）；返回是否插入。"""
    if _header_var_index(pairs, name) is not None:
        return False
    sec = _find_section(pairs, "HEADER")
    at = sec[1] if sec else len(pairs)
    pairs[at:at] = [(9, name), (code, value)]
    return True


def _header_set(pairs, name: str, code: int, value: str) -> bool:
    """已存在则改值；返回是否修改。"""
    i = _header_var_index(pairs, name)
    if i is not None and i + 1 < len(pairs) and pairs[i + 1][0] == code:
        pairs[i + 1] = (code, value)
        return True
    return False


def _header_ensure_point(pairs, name: str, x: str, y: str, z: str = "0.0") -> bool:
    """缺则补三维点头变量（$EXTMIN/$EXTMAX：组码 10/20/30 一次写全）。"""
    if _header_var_index(pairs, name) is not None:
        return False
    sec = _find_section(pairs, "HEADER")
    at = sec[1] if sec else len(pairs)
    pairs[at:at] = [(9, name), (10, x), (20, y), (30, z)]
    return True


# ---------------------------------------------------------------- 几何

def _fnum(v: str):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _entity_boxes(etype: str, props) -> list:
    """单实体的包围盒列表 [(x0,y0,x1,y1)...]；无几何返回 []。"""
    d = {}
    for c, v in props:
        if c in (10, 11, 12, 13, 20, 21, 22, 23, 40):
            n = _fnum(v)
            if n is not None:
                d.setdefault(c, []).append(n)
    if etype in ("LINE", "POINT"):
        if not d.get(10) or not d.get(20):
            return []
        x0, y0 = d[10][0], d[20][0]
        x1 = d.get(11, [x0])[0]
        y1 = d.get(21, [y0])[0]
        return [(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))]
    if etype in ("CIRCLE", "ARC"):
        return [(x - r, y - r, x + r, y + r) for x in d.get(10, [])
                for y in d.get(20, []) for r in d.get(40, [])]
    if etype in ("SOLID", "TRACE"):
        return [(x0, y0, x1, y1) for x0 in d.get(10, []) for y0 in d.get(20, [])
                for x1 in d.get(11, [x0]) for y1 in d.get(21, [y0])
                for x2 in d.get(12, [x0]) for y2 in d.get(22, [y0])
                for x3 in d.get(13, [x0]) for y3 in d.get(23, [y0])]
    if etype in ("TEXT", "ATTRIB", "MTEXT"):
        boxes = [(x, y, x1, y1) for x in d.get(10, []) for y in d.get(20, [])
                 for x1 in d.get(11, [x]) for y1 in d.get(21, [y])]
        # 文字向上延展一个字高，避免范围框裁掉顶部
        if d.get(40) and d.get(10) and d.get(20):
            h = max(d[40])
            boxes.append((min(d[10]), min(d[20]), min(d[10]) + 0.0,
                          max(d[20]) + h))
        return boxes
    if etype in ("LWPOLYLINE", "VERTEX"):
        xs, ys = d.get(10, []), d.get(20, [])
        return [(x, y, x, y) for x, y in zip(xs, ys)]
    return []


def _collect_extents(pairs):
    """全图包围盒 (minx, miny, maxx, maxy)；无实体几何返回 None。"""
    lo_x = lo_y = hi_x = hi_y = None
    for etype, props in _iter_entities(pairs):
        for x0, y0, x1, y1 in _entity_boxes(etype, props):
            lo_x = x0 if lo_x is None else min(lo_x, x0)
            lo_y = y0 if lo_y is None else min(lo_y, y0)
            hi_x = x1 if hi_x is None else max(hi_x, x1)
            hi_y = y1 if hi_y is None else max(hi_y, y1)
    if lo_x is None:
        return None
    return lo_x, lo_y, hi_x, hi_y


def _fmt(v: float, precision: int = 6) -> str:
    """坐标规整：最多 precision 位小数，去掉尾零；-0 归 0。"""
    s = f"{v:.{precision}f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-", "-0") else s


def _transform_entities(pairs, scale: float, dx: float, dy: float) -> int:
    """ENTITIES 区做 x'=x*scale+dx / y'=y*scale+dy；返回改动实体数。"""
    sec = _find_section(pairs, "ENTITIES")
    if not sec:
        return 0
    changed = 0
    cur_type = None
    for i in range(sec[0] + 2, sec[1]):
        c, v = pairs[i]
        if c == 0:
            cur_type = v.strip()
            continue
        if cur_type not in _SCALE_BY_TYPE or c not in _SCALE_BY_TYPE[cur_type]:
            continue
        n = _fnum(v)
        if n is None:
            continue
        if c in (10, 11, 12, 13):
            n = n * scale + dx
        elif c in (20, 21, 22, 23):
            n = n * scale + dy
        else:                       # 40 半径/字高/线宽：只缩放不平移
            n = n * scale
        pairs[i] = (c, _fmt(n))
        changed += 1
    return changed


# ---------------------------------------------------------------- 表完整性

def _layer_names_defined(pairs) -> set:
    tbl = _find_table(pairs, "LAYER")
    if not tbl:
        return set()
    names, cur = set(), None
    for i in range(tbl[0] + 2, tbl[1]):
        c, v = pairs[i]
        if c == 0:
            cur = None
            if v.strip() == "LAYER":
                cur = True
        elif cur and c == 2:
            names.add(v.strip())
            cur = None
    return names


def _layers_referenced(pairs) -> set:
    return {v.strip() for _t, props in _iter_entities(pairs)
            for c, v in props if c == 8}


def _add_missing_layers(pairs, missing: set) -> int:
    """给 LAYER 表补缺失图层（颜色 7 白 / CONTINUOUS 线型）。"""
    if not missing:
        return 0
    add = []
    for n in sorted(missing):
        add += [(0, "LAYER"), (2, n), (70, 0), (62, 7), (6, "CONTINUOUS")]
    tbl = _find_table(pairs, "LAYER")
    if tbl:
        pairs[tbl[1]:tbl[1]] = add          # 插到 ENDTAB 前
        return len(missing)
    sec = _find_section(pairs, "TABLES")    # 极端情况：无 LAYER 表则整体补一张
    if sec:
        block = ([(0, "TABLE"), (2, "LAYER"), (70, len(missing))]
                 + add + [(0, "ENDTAB")])
        pairs[sec[1]:sec[1]] = block
        return len(missing)
    return 0


def _add_standard_style(pairs) -> bool:
    """STYLE 表缺 STANDARD 时，克隆第一个样式条目改名补齐。"""
    tbl = _find_table(pairs, "STYLE")
    if not tbl:
        return False
    entries, cur = [], None
    for i in range(tbl[0] + 2, tbl[1]):
        c, v = pairs[i]
        if c == 0:
            if cur:
                entries.append(cur)
            cur = (v.strip(), []) if v.strip() == "STYLE" else None
        elif cur is not None:
            cur[1].append((c, v))
    if cur:
        entries.append(cur)
    names = {next((v.strip() for c, v in props if c == 2), "")
             for _t, props in entries}
    if "STANDARD" in names or not entries:
        return False
    clone = [(0, "STYLE")]
    for c, v in entries[0][1]:
        if c == 2:
            clone.append((2, "STANDARD"))
        elif c == 3:                # 字体文件指向 AutoCAD 内置 txt.shx
            clone.append((3, "txt"))
        else:
            clone.append((c, v))
    pairs[tbl[1]:tbl[1]] = clone
    return True


# ---------------------------------------------------------------- 主流程

def optimize_file(src, dst=None, mm: bool = False, shift_origin: bool = False,
                  report_path=None) -> dict:
    """优化 DXF 文件，返回审计报告 dict。几何关系严格保持。"""
    src, dst = Path(src), Path(dst) if dst else Path(src).with_name(
        Path(src).stem + "_optimized.dxf")
    text = src.read_text(encoding="utf-8", errors="replace")
    pairs = _parse(text)

    rep = {"source": str(src), "output": str(dst), "warnings": [],
           "entities": dict(Counter(t for t, _p in _iter_entities(pairs)))}

    # ---- 1) 头变量补全（$ACADVER 置于 HEADER 首位）----
    sec = _find_section(pairs, "HEADER")
    if sec and _header_var_index(pairs, "$ACADVER") is None:
        pairs[sec[0] + 2:sec[0] + 2] = [(9, "$ACADVER"), (1, ACADVER_R12)]
        rep["acadver"] = ACADVER_R12
    if _header_ensure(pairs, "$DWGCODEPAGE", 3, "ANSI_936"):
        rep["codepage"] = "ANSI_936"

    # ---- 2) 图层引用完整性 ----
    refs = {v.strip() for _t, props in _iter_entities(pairs)
            for c, v in props if c == 8}
    defined = _layer_names_defined(pairs)
    missing = refs - defined
    rep["layers_referenced"] = sorted(refs)
    rep["layers_defined"] = sorted(defined)
    if missing:
        n = _add_missing_layers(pairs, missing)
        rep["layers_added"] = sorted(missing) if n else []
        if not n:
            rep["warnings"].append(f"存在未定义图层但无法补表: {sorted(missing)}")

    # ---- 3) 单位重标定 + 正象限平移 ----
    if mm:
        if not (_header_set(pairs, "$INSUNITS", 70, "4")
                or _header_ensure(pairs, "$INSUNITS", 70, "4")):
            rep["warnings"].append("$INSUNITS 组码异常，未修改")
        if not (_header_set(pairs, "$MEASUREMENT", 70, "1")
                or _header_ensure(pairs, "$MEASUREMENT", 70, "1")):
            rep["warnings"].append("$MEASUREMENT 组码异常，未修改")
    scale = INCH_TO_MM if mm else 1.0
    dx = dy = 0.0
    if mm or shift_origin:
        ext = _collect_extents(pairs)
        if ext and shift_origin:
            dx, dy = -ext[0] * scale, -ext[1] * scale
        if ext:
            n = _transform_entities(pairs, scale, dx, dy)
            rep["scale"] = scale
            rep["shift"] = (round(dx, 6), round(dy, 6))
            rep["transformed_values"] = n
        else:
            rep["warnings"].append("无可识别几何，跳过变换")

    # ---- 4) 范围框（基于变换后的最终几何，三元组一次写全）----
    ext = _collect_extents(pairs)
    if ext:
        _header_ensure_point(pairs, "$EXTMIN", _fmt(ext[0]), _fmt(ext[1]))
        _header_ensure_point(pairs, "$EXTMAX", _fmt(ext[2]), _fmt(ext[3]))
        rep["extents"] = tuple(round(e, 6) for e in ext)

    # ---- 5) STYLE 表 STANDARD ----
    if _add_standard_style(pairs):
        rep["standard_style"] = True

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(_dump(pairs), encoding="utf-8")
    rep["size_before"] = src.stat().st_size
    rep["size_after"] = dst.stat().st_size

    if report_path:
        _write_report(rep, Path(report_path))
    return rep


def _write_report(rep: dict, path: Path) -> None:
    """把审计报告写成 Markdown。"""
    ent = "、".join(f"{t}×{n}" for t, n in sorted(rep["entities"].items())) or "无"
    L = ["# DXF 兼容性优化报告", "",
         f"- 源文件：`{rep['source']}`",
         f"- 输出文件：`{rep['output']}`",
         f"- 文件大小：{rep['size_before']:,} B → {rep['size_after']:,} B",
         f"- 实体构成：{ent}",
         f"- 图层：引用 {len(rep['layers_referenced'])} 个 / 定义 "
         f"{len(rep['layers_defined'])} 个"
         + (f"，补定义 {rep['layers_added']}" if rep.get("layers_added") else ""),
         f"- 版本标记：{'补 $ACADVER=' + rep['acadver'] if rep.get('acadver') else '原有 $ACADVER'}",
         f"- 代码页：{'补 $DWGCODEPAGE=' + rep['codepage'] if rep.get('codepage') else '原有'}",
         f"- 单位：{'重标定为毫米（×25.4），$INSUNITS=4' if rep.get('scale') == 25.4 else '保持原单位'}",
         f"- 平移：{rep.get('shift', (0.0, 0.0))}",
         f"- 范围框：{rep.get('extents', '无几何')}",
         f"- STANDARD 文字样式：{'已补' if rep.get('standard_style') else '已存在'}",
         "", "## 兼容性说明", "",
         "- 所有变换为等比缩放 + 等距平移，几何关系（长度/角度/连通/比例）不变；",
         "- 头变量为纯元数据补全，不影响任何已有读取器；",
         "- 坐标规整到 0.000001mm，远高于打印/描图精度。"]
    if rep["warnings"]:
        L += ["", "## 警告", ""] + [f"- {w}" for w in rep["warnings"]]
    path.write_text("\n".join(L), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="DXF 兼容性优化器：头变量补全 / mm 重标定 / 正象限平移 / 表完整性")
    ap.add_argument("input", help="输入 DXF 文件")
    ap.add_argument("-o", "--out", default=None,
                    help="输出路径（默认 <输入名>_optimized.dxf）")
    ap.add_argument("--mm", action="store_true",
                    help="英寸→毫米重标定（坐标 ×25.4，$INSUNITS=4）")
    ap.add_argument("--shift-origin", action="store_true",
                    help="平移到第一象限（消除负坐标）")
    ap.add_argument("--report", default=None, help="审计报告输出路径（.md）")
    args = ap.parse_args(argv)

    src = Path(args.input)
    if not src.is_file():
        print(f"错误：输入文件不存在：{src}", file=sys.stderr)
        return 2
    try:
        rep = optimize_file(src, args.out, mm=args.mm,
                            shift_origin=args.shift_origin, report_path=args.report)
    except ValueError as e:
        print(f"错误：DXF 解析失败 —— {e}", file=sys.stderr)
        return 2

    print(f"✔ 已优化：{rep['output']}")
    print(f"  实体 {sum(rep['entities'].values())} 个 | "
          f"大小 {rep['size_before']:,} → {rep['size_after']:,} B")
    if rep.get("scale") == 25.4:
        print("  单位：英寸 → 毫米（×25.4），$INSUNITS=4")
    if rep.get("shift", (0, 0)) != (0.0, 0.0):
        print(f"  平移：dx={rep['shift'][0]}, dy={rep['shift'][1]}（正象限）")
    for w in rep["warnings"]:
        print(f"  ⚠ {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
