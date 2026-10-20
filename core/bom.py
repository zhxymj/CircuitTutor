# -*- coding: utf-8 -*-
"""
bom.py —— BOM（物料清单）导出（零第三方依赖）
=============================================
工业交付刚需：一份电路不仅要有图，还要能变成采购/装配清单。

输出：
* to_rows(circuit) -> List[dict]  合并同值同类型
* to_csv(circuit) -> str          UTF-8 BOM + CSV（Excel 可开）
* to_json(circuit) -> dict        结构化，便于前端/接口
"""

from __future__ import annotations

import csv
import io
import json
from collections import OrderedDict
from typing import Any, Dict, List

try:
    from core.specs import package_of, get_spec
except ImportError:
    def package_of(t, fallback=""):
        return fallback or str(t)

    def get_spec(t):
        return {}

try:
    from core.kicad_footprints import footprint_of
except ImportError:
    def footprint_of(t, hint=""):
        return ""


def _spec_rating(ptype: str) -> str:
    """额定值摘要：电压/电流/功耗/极性。"""
    sp = get_spec(ptype)
    bits = []
    if sp.get("v_max"):
        bits.append(f"≤{sp['v_max']}V")
    if sp.get("i_max_ma"):
        bits.append(f"≤{sp['i_max_ma']}mA")
    if sp.get("p_max_mw"):
        bits.append(f"≤{sp['p_max_mw']}mW")
    if sp.get("polarized"):
        bits.append("有极性")
    if sp.get("v_out"):
        bits.append(f"Vout={sp['v_out']}V")
    return "；".join(bits)


def _value(p: dict) -> str:
    attrs = p.get("attrs") or {}
    v = attrs.get("value")
    return str(v) if v not in (None, "") else ""


def to_rows(circuit: dict) -> List[Dict[str, Any]]:
    """按 (type, value) 合并，输出 BOM 行。"""
    parts = (circuit or {}).get("parts") or []
    buckets: "OrderedDict[tuple, dict]" = OrderedDict()
    for p in parts:
        if not isinstance(p, dict):
            continue
        ptype = str(p.get("type") or "?")
        if ptype in ("gnd", "vcc"):
            continue  # 电源标记不是实物
        val = _value(p)
        key = (ptype, val)
        if key not in buckets:
            buckets[key] = {
                "type": ptype,
                "value": val,
                "package": package_of(ptype),
                "footprint": footprint_of(ptype, (p.get("attrs") or {}).get("package") or ""),
                "rating": _spec_rating(ptype),
                "note": "教学默认值；请按器件手册/封装选型",
                "qty": 0,
                "refs": [],
            }
        buckets[key]["qty"] += 1
        buckets[key]["refs"].append(str(p.get("id") or "?"))
    rows = []
    for i, b in enumerate(buckets.values(), 1):
        rows.append({
            "no": i,
            "type": b["type"],
            "value": b["value"],
            "package": b["package"],
            "footprint": b.get("footprint", ""),
            "rating": b["rating"],
            "note": b.get("note") or "教学默认值；请按器件手册/封装选型",
            "qty": b["qty"],
            "refs": "、".join(b["refs"]),
        })
    return rows


def to_csv(circuit: dict) -> str:
    rows = to_rows(circuit)
    buf = io.StringIO()
    # UTF-8 BOM 便于 Excel 识别中文
    w = csv.writer(buf)
    w.writerow(["序号", "类型", "参数值", "封装", "KiCad封装", "额定", "备注", "数量", "位号"])
    for r in rows:
        w.writerow([r["no"], r["type"], r["value"], r["package"],
                    r.get("footprint", ""), r.get("rating", ""),
                    r.get("note", ""), r["qty"], r["refs"]])
    return "﻿" + buf.getvalue()


def to_json(circuit: dict) -> dict:
    rows = to_rows(circuit)
    return {
        "app": "CircuitTutor",
        "kind": "bom",
        "items": rows,
        "total_line_items": len(rows),
        "total_parts": sum(r["qty"] for r in rows),
    }


if __name__ == "__main__":
    c = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "330Ω"}},
            {"type": "resistor", "id": "R2", "attrs": {"value": "330Ω"}},
            {"type": "led", "id": "LED1", "attrs": {"value": "红"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [],
    }
    rows = to_rows(c)
    assert len(rows) == 3  # battery + 2x330 merged + led
    assert rows[1]["qty"] == 2
    csv_text = to_csv(c)
    assert "330Ω" in csv_text
    print("bom self-check ok", rows)
