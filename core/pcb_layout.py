# -*- coding: utf-8 -*-
"""
pcb_layout.py —— 旧版 PCB 可视预布局骨架（零第三方依赖）
================================================
教学/原型向：根据 CircuitJSON 生成可打开的 .kicad_pcb 最小工程——
板框 + 网格放置的焊盘占位 + 位号丝印。这里的双焊盘为可视占位，不能用于
制造、DRC 或网络核对；真实封装候选板见 core.pcb_real。不替代专业布线，但让「画完原理图
→ 能立刻看到板子大概长什么样」这条链路走通。

能力：
* auto_board_size(n) —— 按元件数估板框（含 5mm 边距）
* place_parts(circuit) —— 确定性网格布局，返回 [(ref, type, fp, x, y), ...]
* to_kicad_pcb(circuit, name) —— 生成 KiCad 8 .kicad_pcb 文本
* summary(circuit) —— 布局摘要 JSON（板框/元件数/封装覆盖率）

坐标约定：KiCad PCB 坐标 mm，原点左上，Y+ 向下；元件落在 2.54mm 栅格。
"""

from __future__ import annotations

import math
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    from core.kicad_footprints import footprint_for_part
except ImportError:
    import os
    import sys
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    try:
        from core.kicad_footprints import footprint_for_part
    except ImportError:
        def footprint_for_part(p):
            return ""

PCB_VERSION = "20240108"
GENERATOR = "zhihui-dianlu-pcb"
NS_URL = "zhihui-dianlu-pcb"


def _uid(name: str, *keys) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "/".join([NS_URL, name, *(str(k) for k in keys)])))


# 每类元件的占位尺寸 (w, h) mm——粗略但够板框估算
_SIZE = {
    "resistor": (10, 3), "capacitor": (5, 5), "electrolytic": (6, 6),
    "inductor": (8, 4), "led": (5, 5), "diode": (10, 3), "zener": (10, 3),
    "npn": (5, 4), "pnp": (5, 4), "nmos": (5, 4), "pmos": (5, 4),
    "ne555": (10, 8), "regulator_7805": (10, 8), "lm393": (10, 8),
    "lm324": (18, 8), "atmega328": (35, 8), "pushbutton": (6, 6),
    "switch": (6, 6), "battery": (20, 15), "buzzer": (12, 12),
    "relay": (18, 14), "crystal": (11, 5), "opamp_inverting": (10, 8),
    "connector": (8, 5), "screw_terminal": (8, 6),
}
_DEFAULT_SIZE = (8, 6)
_CELL_MM = 15.0     # 网格单元（含间距）
_MARGIN_MM = 5.0    # 板边距


def _part_size(ptype: str) -> Tuple[float, float]:
    return _SIZE.get(ptype, _DEFAULT_SIZE)


def auto_board_size(n: int) -> Tuple[float, float]:
    """按元件数估板框尺寸 (w, h) mm，保证至少 40×30。"""
    n = max(1, n)
    cols = max(2, math.ceil(math.sqrt(n * 1.4)))
    rows = math.ceil(n / cols)
    w = cols * _CELL_MM + 2 * _MARGIN_MM
    h = rows * _CELL_MM + 2 * _MARGIN_MM
    return max(40.0, round(w, 1)), max(30.0, round(h, 1))


def place_parts(circuit: dict) -> List[dict]:
    """确定性网格布局。返回 [{ref, type, fp, x, y, w, h}, ...]。"""
    parts = [p for p in (circuit.get("parts") or []) if isinstance(p, dict)]
    # 锚点不上板
    parts = [p for p in parts if p.get("type") not in ("vcc", "gnd")]
    n = len(parts)
    if not n:
        return []
    cols = max(2, math.ceil(math.sqrt(n * 1.4)))
    placed = []
    for i, p in enumerate(parts):
        col, row = i % cols, i // cols
        x = _MARGIN_MM + col * _CELL_MM + _CELL_MM / 2
        y = _MARGIN_MM + row * _CELL_MM + _CELL_MM / 2
        ptype = str(p.get("type"))
        w, h = _part_size(ptype)
        fp = footprint_for_part(p)
        placed.append({
            "ref": str(p.get("id")),
            "type": ptype,
            "fp": fp,
            "x": round(x, 2),
            "y": round(y, 2),
            "w": w, "h": h,
        })
    return placed


def summary(circuit: dict) -> Dict[str, Any]:
    """布局摘要：板框、元件数、封装覆盖率。"""
    placed = place_parts(circuit)
    n = len(placed)
    w, h = auto_board_size(n)
    with_fp = sum(1 for p in placed if p["fp"])
    return {
        "status": "visual_mockup",
        "routing": "unavailable",
        "drc": "unavailable",
        "manufacturing": "unavailable",
        "board_w_mm": w,
        "board_h_mm": h,
        "parts": n,
        "with_footprint": with_fp,
        "coverage": round(with_fp / n * 100, 1) if n else 0.0,
        "grid_mm": _CELL_MM,
        "placements": placed,
    }


def _edge_cuts(w: float, h: float, name: str) -> str:
    """板框 Edge.Cuts 矩形。"""
    pts = [(0, 0), (w, 0), (w, h), (0, h), (0, 0)]
    lines = []
    for i in range(4):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        lines.append(
            f'\t(gr_line\n'
            f'\t\t(start {x1:g} {y1:g})\n'
            f'\t\t(end {x2:g} {y2:g})\n'
            f'\t\t(stroke (width 0.1) (type default))\n'
            f'\t\t(layer "Edge.Cuts")\n'
            f'\t\t(uuid "{_uid(name, "edge", i)}")\n'
            f'\t)'
        )
    return "\n".join(lines)


def _fp_block(p: dict, name: str, idx: int) -> str:
    """单个封装占位：真实 footprint 名 + 两个简化焊盘 + 位号丝印。

    焊盘按元件类型粗分两脚/多脚；工业级应替换为官方 .kicad_mod，
    此处保证 KiCad 能打开、能看见位置关系。
    """
    ref = p["ref"]
    fp_name = p["fp"] or f"Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
    x, y = p["x"], p["y"]
    uid = _uid(name, "fp", idx, ref)
    # 两个焊盘，间距 2.54mm（水平）
    pads = []
    for pn, dx in ((1, -1.27), (2, 1.27)):
        pads.append(
            f'\t\t(pad "{pn}" thru_hole circle\n'
            f'\t\t\t(at {dx:g} 0)\n'
            f'\t\t\t(size 1.6 1.6) (drill 0.8)\n'
            f'\t\t\t(layers "*.Cu" "*.Mask")\n'
            f'\t\t\t(remove_unused_layers no)\n'
            f'\t\t\t(uuid "{_uid(name, "pad", idx, ref, pn)}")\n'
            f'\t\t)'
        )
    return (
        f'\t(footprint "{fp_name}"\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "{uid}")\n'
        f'\t\t(at {x:g} {y:g})\n'
        f'\t\t(descr "CircuitTutor auto-place {p["type"]}")\n'
        f'\t\t(attr through_hole)\n'
        f'\t\t(property "Reference" "{ref}"\n'
        f'\t\t\t(at 0 -3.5 0)\n'
        f'\t\t\t(layer "F.SilkS")\n'
        f'\t\t\t(uuid "{_uid(name, "fref", idx)}")\n'
        f'\t\t\t(effects (font (size 0.8 0.8) (thickness 0.12)))\n'
        f'\t\t)\n'
        f'\t\t(property "Value" "{p["type"]}"\n'
        f'\t\t\t(at 0 3.5 0)\n'
        f'\t\t\t(layer "F.Fab")\n'
        f'\t\t\t(hide yes)\n'
        f'\t\t\t(uuid "{_uid(name, "fval", idx)}")\n'
        f'\t\t\t(effects (font (size 0.8 0.8) (thickness 0.12)))\n'
        f'\t\t)\n'
        + "\n".join(pads) + "\n"
        f'\t\t(model "${{KICAD8_3DMODEL_DIR}}/Resistor_THT.3dshapes/R_Axial_DIN0207_L6.3mm_D2.5mm.wrl"\n'
        f'\t\t\t(offset (xyz 0 0 0))\n'
        f'\t\t\t(scale (xyz 1 1 1))\n'
        f'\t\t\t(rotate (xyz 0 0 0))\n'
        f'\t\t)\n'
        f'\t)'
    )


def to_kicad_pcb(circuit: dict, name: str = "circuit") -> str:
    """生成 KiCad 8 .kicad_pcb 文本。"""
    placed = place_parts(circuit)
    n = len(placed)
    w, h = auto_board_size(n)
    fp_blocks = "\n".join(_fp_block(p, name, i) for i, p in enumerate(placed))
    return (
        f'(kicad_pcb\n'
        f'\t(version {PCB_VERSION})\n'
        f'\t(generator "{GENERATOR}")\n'
        f'\t(generator_version "1.0")\n'
        f'\t(general\n'
        f'\t\t(thickness 1.6)\n'
        f'\t\t(legacy_teardrops no)\n'
        f'\t)\n'
        f'\t(paper "A4")\n'
        f'\t(title_block\n'
        f'\t\t(title "{name}")\n'
        f'\t\t(comment 1 "VISUAL MOCKUP - TWO-PAD PLACEHOLDERS - NOT FOR FABRICATION")\n'
        f'\t)\n'
        f'\t(layers\n'
        f'\t\t(0 "F.Cu" signal)\n'
        f'\t\t(31 "B.Cu" signal)\n'
        f'\t\t(32 "B.Adhes" user "B.Adhesive")\n'
        f'\t\t(33 "F.Adhes" user "F.Adhesive")\n'
        f'\t\t(34 "B.Paste" user)\n'
        f'\t\t(35 "F.Paste" user)\n'
        f'\t\t(36 "B.SilkS" user "B.Silkscreen")\n'
        f'\t\t(37 "F.SilkS" user "F.Silkscreen")\n'
        f'\t\t(38 "B.Mask" user)\n'
        f'\t\t(39 "F.Mask" user)\n'
        f'\t\t(40 "Dwgs.User" user "User.Drawings")\n'
        f'\t\t(41 "Cmts.User" user "User.Comments")\n'
        f'\t\t(44 "Edge.Cuts" user)\n'
        f'\t\t(45 "Margin" user)\n'
        f'\t)\n'
        f'\t(setup\n'
        f'\t\t(pad_to_mask_clearance 0)\n'
        f'\t\t(allow_soldermask_bridges_in_footprints no)\n'
        f'\t)\n'
        f'\t(net 0 "")\n'
        + _edge_cuts(w, h, name) + "\n"
        + (fp_blocks + "\n" if fp_blocks else "")
        + f')\n'
    )


if __name__ == "__main__":
    c = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "330"}},
            {"type": "led", "id": "LED1", "attrs": {"value": "红"}},
        ],
        "connections": [],
    }
    s = summary(c)
    assert s["parts"] == 3
    assert s["with_footprint"] == 3
    assert s["board_w_mm"] >= 40
    pcb = to_kicad_pcb(c, "test")
    assert "(kicad_pcb" in pcb
    assert "Edge.Cuts" in pcb
    assert "LED1" in pcb
    assert pcb.strip().endswith(")")
    print(f"pcb_layout self-check ok  board={s['board_w_mm']}x{s['board_h_mm']}mm  "
          f"parts={s['parts']}  fp_cov={s['coverage']}%")
