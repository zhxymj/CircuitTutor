# -*- coding: utf-8 -*-
"""
kicad_batch_import.py —— KiCad 官方库批量导入草稿
===============================================
从本机 KiCad 官方符号库批量筛选教学高频件，生成：
1. 中文对照草稿清单（Markdown，人工补别名后落库）
2. JSON 目录草稿（与 symbol_import 协议兼容）

用法：
  python -m core.kicad_batch_import                 # 默认扫教学库，出 md+json
  python -m core.kicad_batch_import --all           # 扫全部官方库（慢）
  python -m core.kicad_batch_import --lib Device --out drafts
  python -m core.kicad_batch_import --only LED,R,C  # 指定符号名

输出目录默认：data/kicad_drafts/
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from core.kicad_lib import (
        TEACHING_LIBS,
        default_symbol_dir,
        draft,
        list_libs,
        load_symbol_index,
        search,
    )
except ImportError:
    TEACHING_LIBS = []
    default_symbol_dir = lambda: None
    draft = None
    list_libs = lambda d=None: []
    load_symbol_index = lambda *a, **k: {}
    search = lambda *a, **k: []


# 教学高频官方符号名 → 建议中文名 / 类型 key / 引脚中文对照
# （键为 KiCad 符号名；值为待人工确认的草稿）
CN_NAME_HINTS = {
    "R": ("电阻", "resistor"),
    "R_Potentiometer": ("电位器", "potentiometer"),
    "R_Variable": ("可变电阻", "potentiometer"),
    "R_Pack04": ("排阻4", "resistor_network"),
    "R_Pack08": ("排阻8", "resistor_network"),
    "C": ("电容", "capacitor"),
    "C_Polarized": ("极性电容", "capacitor"),
    "C_Variable": ("可变电容", "capacitor"),
    "L": ("电感", "inductor"),
    "L_Core_Ferrite": ("磁珠", "ferrite_bead"),
    "L_Coupled": ("耦合电感", "inductor"),
    "D": ("二极管", "diode"),
    "D_Zener": ("齐纳二极管", "zener"),
    "D_Schottky": ("肖特基二极管", "schottky"),
    "D_TVS": ("TVS二极管", "tvs"),
    "D_Bridge": ("整流桥", "bridge_rectifier"),
    "D_Photo": ("光电二极管", "photodiode"),
    "LED": ("发光二极管", "led"),
    "LED_Small": ("小尺寸LED", "led"),
    "LED_RGB": ("RGB灯珠", "led_rgb"),
    "LED_KingBright_HighPower_Horizontal_SmallLED_A-100-Y0V0-AA": ("高亮LED", "led"),
    "Q_NPN_BEC": ("NPN三极管", "npn"),
    "Q_NPN_CBE": ("NPN三极管CBE", "npn"),
    "Q_PNP_BEC": ("PNP三极管", "pnp"),
    "Q_NMOS_DGS": ("N沟道MOS管", "nmos"),
    "Q_PMOS_DGS": ("P沟道MOS管", "pmos"),
    "Q_NMOS_GDS": ("N沟道MOS管GDS", "nmos"),
    "Q_Phototransistor": ("光敏三极管", "phototransistor"),
    "Q_IGBT": ("IGBT", "igbt"),
    "Battery": ("电池", "battery"),
    "Battery_Cell": ("电池单体", "battery"),
    "Buzzer": ("蜂鸣器", "buzzer"),
    "Speaker": ("扬声器", "speaker"),
    "Fuse": ("保险丝", "fuse"),
    "Thermistor": ("热敏电阻", "thermistor"),
    "Thermistor_NTC": ("NTC热敏电阻", "thermistor"),
    "LDR": ("光敏电阻", "photocell"),
    "Crystal": ("晶振", "crystal"),
    "Crystal_Small": ("小尺寸晶振", "crystal"),
    "Motor": ("电机", "dc_motor"),
    "Motor_DC": ("直流电机", "dc_motor"),
    "Servo_Motor": ("舵机", "servo"),
    "Relay_SPDT": ("单刀双掷继电器", "relay"),
    "Transformer_1P_1S": ("变压器", "transformer"),
    "Transformer_1P_2S": ("双次级变压器", "transformer"),
    "Varistor": ("压敏电阻", "varistor"),
    "FerriteBead": ("磁珠", "ferrite_bead"),
    "Antenna": ("天线", "antenna"),
    "Microphone": ("话筒", "microphone"),
    "Solar_Cell": ("太阳能板", "solar_cell"),
    "Opto-Isolator": ("光耦", "optocoupler"),
    "Comparator": ("比较器", "opamp_comparator"),
    "Amplifier_Operational": ("通用运放", "opamp"),
    "Opamp_Amplifier_Inverting": ("反相放大器", "opamp_inverting"),
    "Opamp_Amplifier_NonInverting": ("同相放大器", "opamp_noninverting"),
    "Timer_555": ("555定时器", "ne555"),
    "Timer_556": ("双555定时器", "ne556"),
    "Regulator_Linear": ("线性稳压器", "regulator_7805"),
    "Regulator_Switching": ("开关稳压器", "buck_module"),
    "Connector_Generic": ("通用连接器", "conn_header"),
    "Connector_PinHeader": ("排针", "conn_header"),
    "Connector_PinSocket": ("排母", "conn_header"),
    "Connector_Jack_Barrel": ("DC电源座", "barrel_jack"),
    "Connector_USB": ("USB接口", "usb_port"),
    "Connector_Wire": ("接线端子", "screw_terminal"),
    "Switch": ("开关", "switch"),
    "Switch_Push": ("按钮", "pushbutton"),
    "Switch_Slide": ("拨动开关", "switch_toggle"),
    "Switch_Reed": ("干簧管", "switch_reed"),
    "Display_7Segment": ("数码管", "sevenseg"),
    "Display_LCD": ("LCD屏", "lcd1602"),
    "Display_OLED": ("OLED屏", "oled_ssd1306"),
    "Sensor_Temperature": ("温度传感器", "ds18b20"),
    "Sensor_Pressure": ("压力传感器", "bmp280"),
    "Sensor_Motion": ("运动传感器", "mpu6050"),
    "Sensor_Proximity": ("接近传感器", "hc_sr04"),
    "Sensor_Gas": ("气体传感器", "mq2"),
    "Sensor_Current": ("电流传感器", "ammeter"),
    "Sensor_Voltage": ("电压传感器", "voltmeter"),
    "MCU_Microchip_ATmega": ("AVR单片机", "atmega328"),
    "MCU_Espressif": ("ESP模块", "esp32"),
    "LogicGate": ("逻辑门", "ic_and"),
    "LogicGates": ("逻辑门组", "ic_and"),
    "74xx": ("74系列逻辑", "ic_and"),
    "4xxx": ("4000系列逻辑", "cd4017"),
    "Memory_EEPROM": ("EEPROM", "atmega328"),
    "Isolator": ("隔离器", "optocoupler"),
}

# 优先教学库（与 kicad_lib.TEACHING_LIBS 一致，可覆盖）
DEFAULT_LIBS = TEACHING_LIBS or [
    "Device", "Transistor_BJT", "Transistor_FET", "Regulator_Linear",
    "Regulator_Switching", "Timer", "Amplifier_Operational",
    "Sensor_Temperature", "Sensor_Optical", "Switch", "Connector",
    "power", "Diode", "LED", "Reference_Voltage",
]


def _cn_hint(name: str) -> tuple:
    if name in CN_NAME_HINTS:
        return CN_NAME_HINTS[name]
    # 前缀启发
    for k, v in CN_NAME_HINTS.items():
        if name.startswith(k) or k.startswith(name):
            return v
    # 粗译：下划线分词
    parts = name.replace("-", "_").split("_")
    return (" / ".join(parts[:3]), name.lower())


def batch_draft(lib_dir: Optional[Path] = None,
                lib_names: Optional[List[str]] = None,
                only: Optional[List[str]] = None,
                teaching_only: bool = True) -> List[dict]:
    """扫描官方库，返回草稿行列表。

    teaching_only=True（默认）时只保留教学高频符号，避免 Device 库
    上千个冷门型号淹没清单。
    """
    d = Path(lib_dir) if lib_dir else default_symbol_dir()
    if not d or not d.is_dir():
        raise FileNotFoundError(
            "未找到 KiCad 官方符号库。请安装 KiCad 或设置 KICAD_SYMBOL_DIR。")
    libs = lib_names or DEFAULT_LIBS
    idx = load_symbol_index(d, libs=libs)
    rows = []
    for name, meta in sorted(idx.items()):
        if only and name not in only:
            continue
        known = name in CN_NAME_HINTS
        if teaching_only and not known:
            # 默认只导出已知教学高频（CN_NAME_HINTS）
            continue
        cn, typ = _cn_hint(name)
        pins = meta.get("pins") or []
        pin_names = [p.get("name") or p.get("number") for p in pins]
        rows.append({
            "kicad_name": name,
            "lib_id": meta.get("lib_id"),
            "lib": meta.get("lib"),
            "pin_count": meta.get("pin_count"),
            "pins": pin_names[:12],
            "suggest_cn": cn,
            "suggest_type": typ,
            "status": "draft" if known else "candidate",
        })
    return rows


def to_markdown(rows: List[dict], out_path: Path) -> Path:
    lines = [
        "# KiCad 官方库批量导入草稿清单",
        "",
        f"> 生成符号数：{len(rows)}（草稿，**须人工补中文别名与负向词后**再写入 catalog）",
        "> 协议：草稿 → 补中文 → catalog / symbols / kicad TYPE_MAP / app.js → 跑 25 套件",
        "",
        "| KiCad 符号 | 建议中文 | 建议类型 | 引脚数 | 引脚 | 状态 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        pins = "、".join(str(x) for x in r["pins"][:6])
        if len(r["pins"]) > 6:
            pins += "…"
        lines.append(
            f"| `{r['kicad_name']}` | {r['suggest_cn']} | `{r['suggest_type']}` "
            f"| {r['pin_count']} | {pins} | {r['status']} |"
        )
    lines += [
        "",
        "## 落库步骤（每新增一种）",
        "1. 核对引脚名与电气类型（PASSIVE/IN/OUT/POWER/GND）",
        "2. 写 core/catalog.py：name / pins / aliases / negative / default",
        "3. 写 web/symbols.js LIB 条目（或复用 icBox）",
        "4. 写 web/interact.js PART_INFO 默认值",
        "5. 写 kicad/sch_writer.py TYPE_MAP",
        "6. python core/catalog.py --manifest tests/frontend_manifest.json",
        "7. python -B tests/run_tests.py",
        "",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def to_json(rows: List[dict], out_path: Path) -> Path:
    payload = {
        "source": "kicad_official",
        "count": len(rows),
        "items": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="KiCad 官方库批量导入草稿")
    ap.add_argument("--lib", action="append", help="指定库名，可重复")
    ap.add_argument("--all", action="store_true", help="扫描全部官方库（慢）")
    ap.add_argument("--only", help="仅导出指定符号名，逗号分隔")
    ap.add_argument("--full", action="store_true",
                    help="关闭教学过滤，导出全部符号（含冷门型号）")
    ap.add_argument("--out", default=None, help="输出目录（默认 data/kicad_drafts）")
    args = ap.parse_args(argv)

    root = Path(__file__).resolve().parent.parent
    out_dir = Path(args.out) if args.out else root / "data" / "kicad_drafts"

    lib_names = args.lib
    if args.all:
        lib_names = list_libs() or None
    only = [x.strip() for x in args.only.split(",")] if args.only else None

    try:
        rows = batch_draft(lib_names=lib_names, only=only,
                           teaching_only=not args.full)
    except FileNotFoundError as e:
        print(f"错误：{e}")
        return 1

    if not rows:
        print("未扫描到符号。")
        return 1

    md = to_markdown(rows, out_dir / "kicad_drafts_zh.md")
    js = to_json(rows, out_dir / "kicad_drafts.json")
    print(f"草稿 {len(rows)} 条")
    print(f"  Markdown: {md}")
    print(f"  JSON:     {js}")
    print("请人工补中文别名后按文档末尾步骤落库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
