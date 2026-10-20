# -*- coding: utf-8 -*-
"""
kicad_batch.py —— KiCad 官方库批量导入（中文草稿清单）
======================================================
从官方符号库批量扫描，产出「待人工补中文」的目录草稿与评审清单。
**禁止一键灌入运行时 CATALOG** —— 教学目录必须保持可解释、可测试。

输出：
* drafts.json     结构草稿（与 symbol_import.draft_entry 同构）
* review.md       中文评审清单（按库分组，含引脚表）
* summary.json    统计：已入库 / 新候选 / 跳过

用法：
    python -m core.kicad_batch                 # 教学优先库
    python -m core.kicad_batch --all           # 全部官方库（慢）
    python -m core.kicad_batch --out DIR       # 指定输出目录
    python -m core.kicad_batch --limit 50      # 最多导出 N 个新符号
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

try:
    from core import catalog as catalog_mod
    from core import kicad_lib
    from core import symbol_import as si
except ImportError:
    catalog_mod = None
    kicad_lib = None
    si = None

# 中文占位名：按 lib 前缀给出可人工替换的建议
_LIB_ZH = {
    "Device": "器件",
    "Diode": "二极管",
    "Transistor_BJT": "三极管",
    "Transistor_FET": "场效应管",
    "Amplifier_Operational": "运放",
    "Regulator_Linear": "线性稳压",
    "Regulator_Switching": "开关电源",
    "Timer": "定时器",
    "Sensor_Temperature": "温度传感",
    "Sensor_Optical": "光学传感",
    "Switch": "开关",
    "Connector": "接插件",
    "power": "电源",
    "LED": "发光器件",
    "Reference_Voltage": "基准源",
    "MCU_Microchip_ATmega": "单片机",
    "RF_Module": "射频模块",
    "Display_Controller": "显示驱动",
    "Memory_EEPROM": "存储器",
    "Interface_UART": "串口",
    "Interface_USB": "USB接口",
    "Isolator": "隔离器",
    "Converter_ADC": "ADC",
    "Converter_DAC": "DAC",
    "Filter": "滤波器",
    "Oscillator": "振荡器",
    "Transformer": "变压器",
    "Motor": "电机",
    "Relay": "继电器",
    "Crystal": "晶振",
    "Device": "无源器件",
}


def _zh_suggest(lib: str, name: str) -> str:
    zh = _LIB_ZH.get(lib, lib)
    # 把 CamelCase 名拆成词，便于人工翻译
    import re
    words = re.findall(r"[A-Z][a-z0-9]+|[A-Z]+(?![a-z])", name)
    tail = " ".join(words) if words else name
    return f"{zh}-{tail}"


def batch_scan(lib_dir: Optional[Path] = None,
               all_libs: bool = False,
               limit: int = 80,
               out_dir: Optional[Path] = None) -> dict:
    """批量扫描并生成草稿。"""
    if kicad_lib is None or si is None or catalog_mod is None:
        raise RuntimeError("依赖模块缺失")

    d = lib_dir or kicad_lib.default_symbol_dir()
    if d is None or not Path(d).is_dir():
        raise FileNotFoundError(
            "未找到 KiCad 官方符号库目录。请设置 KICAD_SYMBOL_DIR 或安装 KiCad。")

    existing = set(catalog_mod.CATALOG.keys())
    # 常见官方名 → 已有类型（跳过）
    already = {
        "R", "C", "C_Polarized", "L", "R_Potentiometer", "D", "D_Zener",
        "LED", "Q_NPN_BEC", "Q_PNP_BEC", "Q_NMOS_DGS", "Fuse", "Crystal",
        "Battery", "Buzzer", "Speaker", "SW_Push", "NE555", "L7805",
        "Opamp3", "PWR_FLAG", "D_Schottky", "D_Photo", "Q_PMOS_DGS",
        "R_Photo", "R_Thermistor", "AC_Source", "Motor_DC", "Servo",
        "Relay", "Optocoupler", "Bridge_Rectifier", "Screw_Terminal",
        "Thermistor", "LDR", "Varistor",
    }

    lib_names = kicad_lib.list_libs(d) if all_libs else kicad_lib.TEACHING_LIBS
    lib_names = [x for x in lib_names if (Path(d) / f"{x}.kicad_sym").is_file()]

    drafts: Dict[str, dict] = {}
    skipped = []
    scanned = 0

    for lib in lib_names:
        try:
            idx = kicad_lib.load_symbol_index(d, libs=[lib])
        except Exception:
            continue
        for sname, meta in idx.items():
            scanned += 1
            if sname in already or sname in drafts:
                continue
            if sname.endswith(tuple(f"_{i}_{j}" for i in range(1, 5) for j in range(1, 5))):
                continue
            if len(drafts) >= limit:
                break
            try:
                one = si.draft_from_lib(Path(d) / f"{lib}.kicad_sym", [sname])
                entry = one[sname]
            except Exception:
                continue
            entry["kicad_lib"] = lib
            entry["zh_suggest"] = _zh_suggest(lib, sname)
            entry["pin_count"] = len(entry.get("pins") or {})
            entry["status"] = "pending_review"
            drafts[sname] = entry
        if len(drafts) >= limit:
            break

    out = Path(out_dir) if out_dir else (Path.cwd() / "kicad_drafts")
    out.mkdir(parents=True, exist_ok=True)
    (out / "drafts.json").write_text(
        json.dumps(drafts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 评审清单 Markdown
    lines = [
        "# KiCad 官方库批量导入 · 中文草稿评审清单",
        "",
        f"- 扫描库：{len(lib_names)} 个（{'全部' if all_libs else '教学优先'}）",
        f"- 扫描符号数：{scanned}",
        f"- 新候选（待补中文）：{len(drafts)}",
        f"- 运行时已入库类型：{len(existing)}",
        "",
        "> 人工补完中文名与别名后，按扩展协议写入 catalog / symbols / kicad / app.js，",
        "> 再跑 `python -B tests/run_tests.py`。",
        "",
    ]
    by_lib: Dict[str, List[str]] = {}
    for sname, e in drafts.items():
        by_lib.setdefault(e.get("kicad_lib", "?"), []).append(sname)
    for lib in sorted(by_lib):
        lines.append(f"## {lib}")
        lines.append("")
        lines.append("| 符号 | 建议中文 | 引脚数 | 引脚 |")
        lines.append("|---|---|---|---|")
        for sname in sorted(by_lib[lib]):
            e = drafts[sname]
            pins = list((e.get("pins") or {}).keys())
            lines.append(
                f"| {sname} | {e.get('zh_suggest','')} | {e.get('pin_count',0)} | "
                f"{', '.join(pins[:8])}{'…' if len(pins) > 8 else ''} |"
            )
        lines.append("")
    (out / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "libs_scanned": len(lib_names),
        "symbols_scanned": scanned,
        "new_candidates": len(drafts),
        "existing_types": len(existing),
        "out_dir": str(out),
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="KiCad 官方库批量导入（中文草稿清单）")
    ap.add_argument("--all", action="store_true", help="扫描全部官方库（默认教学优先）")
    ap.add_argument("--limit", type=int, default=80, help="最多导出 N 个新符号")
    ap.add_argument("--out", default="kicad_drafts", help="输出目录")
    ap.add_argument("--lib-dir", default=None, help="覆盖符号库目录")
    args = ap.parse_args(argv)
    try:
        lib_dir = Path(args.lib_dir) if args.lib_dir else None
        s = batch_scan(lib_dir=lib_dir, all_libs=args.all,
                       limit=args.limit, out_dir=Path(args.out))
    except Exception as e:
        print(f"错误：{e}")
        return 1
    print("批量导入草稿完成")
    print(f"  扫描库：{s['libs_scanned']}")
    print(f"  扫描符号：{s['symbols_scanned']}")
    print(f"  新候选：{s['new_candidates']}")
    print(f"  输出目录：{s['out_dir']}")
    print(f"  评审清单：{s['out_dir']}/review.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
