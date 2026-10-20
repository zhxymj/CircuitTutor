# -*- coding: utf-8 -*-
"""KiCad 嵌入符号定义（自绘紧凑版，零第三方依赖）。

本模块只负责"符号长什么样"：S 表达式图元助手 + 各元件符号定义 +
build_symbol_defs() 注册表。原理图正文生成见 kicad.sch_writer，
kicad-cli 联动见 kicad.cli_runner（kicad.export 为兼容门面）。

坐标约定：文件坐标 Y+ 为屏幕上方；引脚 (at x y 角度) 的角度 =
引脚从连接点指向元件体的方向：0=+x右 90=+y上 180=左 270=下。
"""

from __future__ import annotations

import uuid

# 画布：A4 横向 297x210mm；KiCad 原理图画布 Y 轴向下为正
STUB = 2.54          # 引脚短走线长度（0.1 inch，KiCad 标准栅距）
FONT_SZ = 1.27

NS_URL = "zhihui-dianlu"


def _uid(name: str, *keys) -> str:
    """确定性 UUID（uuid5），保证同一电路导出结果逐字节一致。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "/".join([NS_URL, name, *(str(k) for k in keys)])))


def _esc(s: str) -> str:
    """S 表达式字符串转义（引号与反斜杠）。"""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _def_head(lib_id: str, ref: str, value: str, *, hide_numbers: bool) -> str:
    num = " (pin_numbers hide)" if hide_numbers else ""
    return (f'(symbol "{lib_id}"{num} (pin_names (offset 0.254)) '
            f'(in_bom yes) (on_board yes)\n'
            f'    (property "Reference" "{ref}" (at 0 0 0) (effects (font (size {FONT_SZ} {FONT_SZ}))))\n'
            f'    (property "Value" "{value}" (at 0 0 0) (effects (font (size {FONT_SZ} {FONT_SZ}))))\n'
            f'    (property "Footprint" "" (at 0 0 0) (effects (font (size {FONT_SZ} {FONT_SZ})) (hide yes)))\n'
            f'    (property "Datasheet" "" (at 0 0 0) (effects (font (size {FONT_SZ} {FONT_SZ})) (hide yes)))')


def _pin(etype: str, x: float, y: float, ang: int, number: str, name: str = "~",
         length: float = STUB) -> str:
    return (f'    (pin {etype} line (at {x:g} {y:g} {ang}) (length {length:g})\n'
            f'      (name "{_esc(name)}" (effects (font (size {FONT_SZ} {FONT_SZ}))))\n'
            f'      (number "{_esc(number)}" (effects (font (size {FONT_SZ} {FONT_SZ})))))')


def _wire(pts, width: float = 0.254, fill: str = "none") -> str:
    p = " ".join(f"(xy {x:g} {y:g})" for x, y in pts)
    return f'      (polyline (pts {p}) (stroke (width {width:g}) (type default)) (fill (type {fill})))'


def _rect(x1, y1, x2, y2, width: float = 0.254) -> str:
    return (f'      (rectangle (start {x1:g} {y1:g}) (end {x2:g} {y2:g})\n'
            f'        (stroke (width {width:g}) (type default)) (fill (type none)))')


def _circle(cx, cy, r, width: float = 0.254) -> str:
    return (f'      (circle (center {cx:g} {cy:g}) (radius {r:g})\n'
            f'        (stroke (width {width:g}) (type default)) (fill (type none)))')


def _arc(x1, y1, xm, ym, x2, y2, width: float = 0.254) -> str:
    return (f'      (arc (start {x1:g} {y1:g}) (mid {xm:g} {ym:g}) (end {x2:g} {y2:g})\n'
            f'        (stroke (width {width:g}) (type default)) (fill (type none)))')


def _text(s, x, y) -> str:
    return (f'      (text "{_esc(s)}" (at {x:g} {y:g} 0)\n'
            f'        (effects (font (size {FONT_SZ} {FONT_SZ}))))')


def _two_pin_vert(base: str, body: str, top=("1", "~"), bottom=("2", "~"),
                  ref_prefix: str = "REF") -> str:
    """竖直双引脚模板：默认 1=上(0,3.81,270) 2=下(0,-3.81,90)。

    二极管族传 top=("2","A"), bottom=("1","K")：图形阳极在上，
    引脚编号遵循 KiCad 标准库惯例（D/LED/D_Zener: 1=K 2=A）。
    """
    return (_def_head(f"CircuitTutor:{base}", ref_prefix, base, hide_numbers=True) + "\n" +
            f'  (symbol "{base}_0_1"\n{body}\n  )\n' +
            f'  (symbol "{base}_1_1"\n' +
            _pin("passive", 0, 3.81, 270, top[0], top[1]) + "\n" +
            _pin("passive", 0, -3.81, 90, bottom[0], bottom[1]) + "\n  )\n)")


def _sym_r() -> str:
    return _two_pin_vert("R", _rect(-1.016, -2.54, 1.016, 2.54), ref_prefix="R")


def _sym_c_pol() -> str:
    body = (_wire([(-1.016, 2.54), (1.016, 2.54)]) + "\n" +
            _arc(-1.016, -2.54, 0, -3.175, 1.016, -2.54) + "\n" +
            _text("+", 2.286, 3.048))
    return _two_pin_vert("C_Polarized", body, top=("1", "+"), bottom=("2", "-"),
                         ref_prefix="C")


def _sym_l() -> str:
    arcs = "\n".join(
        _arc(x, 0, x + 0.4233, 0.847, x + 0.8466, 0)
        for x in (-2.54, -0.8466, 0.8466))
    return _two_pin_vert("L", arcs, ref_prefix="L")


def _sym_diode(base: str, zener: bool = False, schottky: bool = False) -> str:
    body = (_wire([(-1.016, 1.27), (1.016, 1.27), (0, -1.016), (-1.016, 1.27)], fill="outline") + "\n" +
            _wire([(-1.016, -1.016), (1.016, -1.016)]))
    if zener:
        body += ("\n" + _wire([(-1.016, -1.016), (-1.778, -1.778)]) + "\n" +
                 _wire([(1.016, -1.016), (1.778, -0.254)]))
    if schottky:
        # 肖特基：阴极杆两端 S 形钩（左上钩 / 右下钩），与稳压管斜翼区分
        body += ("\n" + _wire([(-1.016, -1.016), (-1.524, -1.016), (-1.524, -0.508)]) + "\n" +
                 _wire([(1.016, -1.016), (1.524, -1.016), (1.524, -1.524)]))
    # 图形阳极在上（三角底边在上、阴极杆在下）；编号遵循 KiCad 惯例 1=K 2=A
    return _two_pin_vert(base, body, top=("2", "A"), bottom=("1", "K"),
                         ref_prefix="D")


def _sym_led() -> str:
    body = (_wire([(-1.016, 1.27), (1.016, 1.27), (0, -1.016), (-1.016, 1.27)], fill="outline") + "\n" +
            _wire([(-1.016, -1.016), (1.016, -1.016)]) + "\n" +
            _wire([(1.27, 0.381), (2.286, 1.397), (2.286, 0.889), (2.286, 1.397), (1.778, 1.397)]) + "\n" +
            _wire([(0.889, -0.508), (1.905, 0.508), (1.905, 0.0), (1.905, 0.508), (1.397, 0.508)]))
    return _two_pin_vert("LED", body, top=("2", "A"), bottom=("1", "K"),
                         ref_prefix="LED")


def _sym_battery() -> str:
    body = (_wire([(-1.27, 1.778), (1.27, 1.778)]) + "\n" +
            _wire([(-0.635, 0.762), (0.635, 0.762)], width=0.508) + "\n" +
            _wire([(-1.27, -0.254), (1.27, -0.254)]) + "\n" +
            _wire([(-0.635, -1.27), (0.635, -1.27)], width=0.508) + "\n" +
            _text("+", 2.286, 2.54))
    return _two_pin_vert("Battery", body, top=("1", "+"), bottom=("2", "-"),
                         ref_prefix="BAT")


def _sym_pwr_flag() -> str:
    """电源标志（与官方 power:PWR_FLAG 同语义）：(power) 标记 +
    power_out 零长引脚。用于告知官方 ERC “该网络是外部供电的”。"""
    return ('(symbol "CircuitTutor:PWR_FLAG" (power) (pin_numbers hide) '
            '(pin_names (offset 0) hide) (exclude_from_sim no) '
            '(in_bom yes) (on_board yes)\n'
            '    (property "Reference" "#FLG" (at 0 1.905 0) '
            '(effects (font (size 1.27 1.27)) (hide yes)))\n'
            '    (property "Value" "PWR_FLAG" (at 0 3.81 0) '
            '(effects (font (size 1.27 1.27))))\n'
            '    (property "Footprint" "" (at 0 0 0) '
            '(effects (font (size 1.27 1.27)) (hide yes)))\n'
            '    (property "Datasheet" "~" (at 0 0 0) '
            '(effects (font (size 1.27 1.27)) (hide yes)))\n'
            '  (symbol "PWR_FLAG_0_1"\n'
            '    (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) '
            '(xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27)) '
            '(stroke (width 0) (type default)) (fill (type none)))\n'
            '  )\n'
            '  (symbol "PWR_FLAG_0_0"\n'
            '    (pin power_out line (at 0 0 90) (length 0)\n'
            '      (name "~" (effects (font (size 1.27 1.27))))\n'
            '      (number "1" (effects (font (size 1.27 1.27)))))\n'
            '  )\n'
            ')')


def _sym_pot() -> str:
    body = (_rect(-1.016, -2.54, 1.016, 2.54) + "\n" +
            _wire([(3.175, 0), (2.032, 0.635), (2.032, -0.635), (3.175, 0)], fill="outline"))
    s = (_def_head("CircuitTutor:R_Potentiometer", "RV", "R_Potentiometer", hide_numbers=True) + "\n" +
         f'  (symbol "R_Potentiometer_0_1"\n{body}\n  )\n' +
         f'  (symbol "R_Potentiometer_1_1"\n' +
         _pin("passive", 0, 3.81, 270, "1") + "\n" +
         _pin("passive", 0, -3.81, 90, "2") + "\n" +
         _pin("passive", 5.08, 0, 180, "3") + "\n  )\n)")
    return s


def _sym_sw_push() -> str:
    body = (_circle(-2.794, 0, 0.508) + "\n" +
            _circle(2.794, 0, 0.508) + "\n" +
            _wire([(-3.302, 1.016), (3.302, 1.016)]))
    s = (_def_head("CircuitTutor:SW_Push", "SW", "SW_Push", hide_numbers=True) + "\n" +
         f'  (symbol "SW_Push_0_1"\n{body}\n  )\n' +
         f'  (symbol "SW_Push_1_1"\n' +
         _pin("passive", -5.08, 0, 0, "1") + "\n" +
         _pin("passive", 5.08, 0, 180, "2") + "\n  )\n)")
    return s


def _sym_fuse() -> str:
    body = _wire([(-2.54, 0), (2.54, 0)], width=0.508)
    s = (_def_head("CircuitTutor:Fuse", "F", "Fuse", hide_numbers=True) + "\n" +
         f'  (symbol "Fuse_0_1"\n{body}\n  )\n' +
         f'  (symbol "Fuse_1_1"\n' +
         _pin("passive", -5.08, 0, 0, "1") + "\n" +
         _pin("passive", 5.08, 0, 180, "2") + "\n  )\n)")
    return s


def _sym_crystal() -> str:
    body = (_wire([(-1.27, 1.524), (1.27, 1.524)]) + "\n" +
            _rect(-0.762, -1.016, 0.762, 1.016) + "\n" +
            _wire([(-1.27, -1.524), (1.27, -1.524)]))
    return _two_pin_vert("Crystal", body, ref_prefix="Y")


def _sym_buzzer() -> str:
    body = (_rect(-2.54, -2.54, 0.508, 2.54) + "\n" +
            _arc(0.508, 2.54, 1.27, 3.302, 2.032, 2.54) + "\n" +
            _arc(0.508, -2.54, 1.27, -3.302, 2.032, -2.54))
    s = (_def_head("CircuitTutor:Buzzer", "BZ", "Buzzer", hide_numbers=True) + "\n" +
         f'  (symbol "Buzzer_0_1"\n{body}\n  )\n' +
         f'  (symbol "Buzzer_1_1"\n' +
         _pin("passive", -5.08, 0, 0, "1", "+") + "\n" +
         _pin("passive", 5.08, 0, 180, "2", "-") + "\n  )\n)")
    return s


def _sym_speaker() -> str:
    # 扬声器：梯形喇叭口朝右（闭合轮廓）+ 两条右凸声波弧；1=+(左) 2=-(右)。
    # 引脚几何与 Buzzer 一致（±5.08 水平双引脚），便于统一 horiz 版式。
    body = (_wire([(-2.54, -2.54), (0.0, -2.54), (2.54, 0), (0.0, 2.54),
                   (-2.54, 2.54), (-2.54, -2.54)]) + "\n" +
            _arc(4.064, -2.54, 5.842, 0, 4.064, 2.54) + "\n" +
            _arc(5.334, -3.302, 7.62, 0, 5.334, 3.302))
    s = (_def_head("CircuitTutor:Speaker", "LS", "Speaker", hide_numbers=True) + "\n" +
         f'  (symbol "Speaker_0_1"\n{body}\n  )\n' +
         f'  (symbol "Speaker_1_1"\n' +
         _pin("passive", -5.08, 0, 0, "1", "+") + "\n" +
         _pin("passive", 5.08, 0, 180, "2", "-") + "\n  )\n)")
    return s


def _sym_bjt(base: str, pnp: bool) -> str:
    # 1=B(左) 2=C(上右) 3=E(下右)；NPN 箭头在 E 指外，PNP 指向基极
    body = (_circle(0.254, 0, 3.175) + "\n" +
            _wire([(-1.27, -2.032), (-1.27, 2.032)], width=0.508) + "\n" +
            _wire([(-1.27, 1.016), (2.54, 4.318), (2.54, 5.08)]) + "\n" +
            _wire([(-1.27, -1.016), (2.54, -4.318), (2.54, -5.08)]))
    if pnp:
        body += "\n" + _wire([(0.0, -1.905), (-0.889, -1.016), (-0.127, -0.889), (0.0, -1.905)], fill="outline")
    else:
        body += "\n" + _wire([(0.0, -1.016), (0.889, -1.905), (0.127, -2.032), (0.0, -1.016)], fill="outline")
    s = (_def_head(f"CircuitTutor:{base}", "Q", base, hide_numbers=False) + "\n" +
         f'  (symbol "{base}_0_1"\n{body}\n  )\n' +
         f'  (symbol "{base}_1_1"\n' +
         _pin("input", -5.08, 0, 0, "1", "B") + "\n" +
         _pin("passive", 2.54, 5.08, 270, "2", "C") + "\n" +
         _pin("passive", 2.54, -5.08, 90, "3", "E") + "\n  )\n)")
    return s


def _sym_nmos() -> str:
    body = (_circle(0.254, 0, 3.175) + "\n" +
            _wire([(-1.27, -2.032), (-1.27, 2.032)], width=0.508) + "\n" +
            _wire([(0.0, 2.032), (0.0, 1.27), (2.54, 1.27), (2.54, 5.08)]) + "\n" +
            _wire([(0.0, -2.032), (0.0, -1.27), (2.54, -1.27), (2.54, -5.08)]) + "\n" +
            _wire([(0.0, 1.27), (0.0, -1.27)], width=0.508) + "\n" +
            _wire([(0.0, -1.27), (0.889, -2.032), (0.127, -2.159), (0.0, -1.27)], fill="outline"))
    s = (_def_head("CircuitTutor:Q_NMOS_DGS", "Q", "Q_NMOS_DGS", hide_numbers=False) + "\n" +
         '  (symbol "Q_NMOS_DGS_0_1"\n' + body + '\n  )\n' +
         '  (symbol "Q_NMOS_DGS_1_1"\n' +
         _pin("input", -5.08, 0, 0, "2", "G") + "\n" +
         _pin("passive", 2.54, 5.08, 270, "1", "D") + "\n" +
         _pin("passive", 2.54, -5.08, 90, "3", "S") + "\n  )\n)")
    return s


def _sym_opamp3() -> str:
    body = (_wire([(-5.08, 5.08), (-5.08, -5.08), (5.08, 0), (-5.08, 5.08)], fill="outline") + "\n" +
            _text("+", -4.064, 3.175) + "\n" +
            _text("-", -4.064, -3.175))
    s = (_def_head("CircuitTutor:Opamp3", "U", "Opamp3", hide_numbers=False) + "\n" +
         '  (symbol "Opamp3_0_1"\n' + body + '\n  )\n' +
         '  (symbol "Opamp3_1_1"\n' +
         _pin("input", -7.62, 2.54, 180, "1", "+") + "\n" +
         _pin("input", -7.62, -2.54, 180, "2", "-") + "\n" +
         _pin("output", 7.62, 0, 0, "3", "out") + "\n  )\n)")
    return s


def _sym_ne555() -> str:
    body = _rect(-7.62, -7.62, 7.62, 7.62, width=0.508)
    s = (_def_head("CircuitTutor:NE555", "U", "NE555", hide_numbers=False) + "\n" +
         '  (symbol "NE555_0_1"\n' + body + '\n  )\n' +
         '  (symbol "NE555_1_1"\n' +
         _pin("power_in", 0, -10.16, 90, "1", "GND") + "\n" +
         _pin("input", -10.16, 5.08, 180, "2", "TRIG") + "\n" +
         _pin("output", 10.16, 5.08, 0, "3", "OUT") + "\n" +
         _pin("input", -10.16, 2.54, 180, "4", "RESET") + "\n" +
         _pin("input", -10.16, -2.54, 180, "5", "CTRL") + "\n" +
         _pin("input", -10.16, -5.08, 180, "6", "THR") + "\n" +
         _pin("open_collector", 10.16, 2.54, 0, "7", "DISCH") + "\n" +
         _pin("power_in", 0, 10.16, 270, "8", "VCC") + "\n  )\n)")
    return s


def _sym_7805() -> str:
    body = _rect(-5.08, -3.81, 5.08, 3.81, width=0.508)
    s = (_def_head("CircuitTutor:L7805", "U", "L7805", hide_numbers=False) + "\n" +
         '  (symbol "L7805_0_1"\n' + body + '\n  )\n' +
         '  (symbol "L7805_1_1"\n' +
         _pin("power_in", -7.62, 2.54, 180, "1", "IN") + "\n" +
         _pin("power_in", 0, -6.35, 90, "2", "GND") + "\n" +
         _pin("power_out", 7.62, 0, 0, "3", "OUT") + "\n  )\n)")
    return s


# =====================================================================
# 2026-09-09 常用元件扩展批（与 catalog.CATALOG 同步，
# 引脚电气类型仅影响官方 ERC；本项目内置 ERC 用 CATALOG 的 pins 表）
# =====================================================================

def _sym_photodiode() -> str:
    # 光电二极管：二极管图形 + 两个指向管体的箭头（光照进来，与 LED 相反）
    body = (_wire([(-1.016, 1.27), (1.016, 1.27), (0, -1.016), (-1.016, 1.27)], fill="outline") + "\n" +
            _wire([(-1.016, -1.016), (1.016, -1.016)]) + "\n" +
            _wire([(2.286, 1.397), (1.27, 0.381), (1.27, 0.889), (1.27, 0.381), (1.778, 0.381)]) + "\n" +
            _wire([(1.905, 0.508), (0.889, -0.508), (0.889, 0.0), (0.889, -0.508), (1.397, -0.508)]))
    return _two_pin_vert("D_Photo", body, top=("2", "A"), bottom=("1", "K"),
                         ref_prefix="D")


def _sym_pmos() -> str:
    # 与 _sym_nmos 对偶：箭头绕 x=0 镜像（朝外 = P 沟道），引脚编号同 NMOS 惯例
    body = (_circle(0.254, 0, 3.175) + "\n" +
            _wire([(-1.27, -2.032), (-1.27, 2.032)], width=0.508) + "\n" +
            _wire([(0.0, 2.032), (0.0, 1.27), (2.54, 1.27), (2.54, 5.08)]) + "\n" +
            _wire([(0.0, -2.032), (0.0, -1.27), (2.54, -1.27), (2.54, -5.08)]) + "\n" +
            _wire([(0.0, 1.27), (0.0, -1.27)], width=0.508) + "\n" +
            _wire([(0.0, -1.27), (-0.889, -2.032), (-0.127, -2.159), (0.0, -1.27)], fill="outline"))
    s = (_def_head("CircuitTutor:Q_PMOS_DGS", "Q", "Q_PMOS_DGS", hide_numbers=False) + "\n" +
         '  (symbol "Q_PMOS_DGS_0_1"\n' + body + '\n  )\n' +
         '  (symbol "Q_PMOS_DGS_1_1"\n' +
         _pin("input", -5.08, 0, 0, "2", "G") + "\n" +
         _pin("passive", 2.54, 5.08, 270, "1", "D") + "\n" +
         _pin("passive", 2.54, -5.08, 90, "3", "S") + "\n  )\n)")
    return s


def _sym_photocell() -> str:
    # 光敏电阻（LDR）：电阻框 + 两个射入箭头
    body = (_rect(-1.016, -2.54, 1.016, 2.54) + "\n" +
            _wire([(2.794, 2.794), (1.778, 1.778), (2.286, 1.778), (1.778, 1.778), (1.778, 2.286)]) + "\n" +
            _wire([(2.032, 3.302), (1.016, 2.286), (1.524, 2.286), (1.016, 2.286), (1.016, 2.794)]))
    return _two_pin_vert("R_Photo", body, ref_prefix="RG")


def _sym_thermistor() -> str:
    # 热敏电阻：电阻框 + IEC 斜杠（下端带短横脚）
    body = (_rect(-1.016, -2.54, 1.016, 2.54) + "\n" +
            _wire([(-1.524, -3.048), (1.524, 3.048)]) + "\n" +
            _wire([(-1.524, -3.048), (-0.508, -3.048)]))
    return _two_pin_vert("R_Thermistor", body, ref_prefix="TH")


def _sym_ac_source() -> str:
    # 交流源：圆圈 + 正弦波（竖放，1=+ 上 2=- 下）
    body = (_circle(0, 0, 2.54) + "\n" +
            _wire([(-1.27, 0), (-0.635, 0.889), (0, -0.889), (0.635, 0.889), (1.27, 0)]))
    return _two_pin_vert("AC_Source", body, top=("1", "+"), bottom=("2", "-"),
                         ref_prefix="VAC")


def _sym_motor() -> str:
    # 直流电机：圆圈 + M，水平双引脚（1=+ 左 2=- 右），引脚几何与 Buzzer 一致
    body = (_circle(0, 0, 2.54) + "\n" +
            _text("M", -0.889, 0.508))
    s = (_def_head("CircuitTutor:Motor_DC", "M", "Motor_DC", hide_numbers=True) + "\n" +
         '  (symbol "Motor_DC_0_1"\n' + body + '\n  )\n' +
         '  (symbol "Motor_DC_1_1"\n' +
         _pin("passive", -5.08, 0, 0, "1", "+") + "\n" +
         _pin("passive", 5.08, 0, 180, "2", "-") + "\n  )\n)")
    return s


def _sym_servo() -> str:
    # 舵机：方框 + 三线（上 VCC 下 GND 左 SIG）；引脚全 passive/input，
    # 不引入 power_in —— 爱好舵机直接挂电池网络是常态，避免官方 ERC
    # “电源引脚未驱动”误报（内置 ERC 的 PWR_IN 语义由 CATALOG 承载）
    body = _rect(-3.81, -2.54, 3.81, 2.54, width=0.508)
    s = (_def_head("CircuitTutor:Servo", "M", "Servo", hide_numbers=False) + "\n" +
         '  (symbol "Servo_0_1"\n' + body + '\n  )\n' +
         '  (symbol "Servo_1_1"\n' +
         _pin("passive", 0, 5.08, 270, "1", "VCC") + "\n" +
         _pin("passive", 0, -5.08, 90, "2", "GND") + "\n" +
         _pin("input", -6.35, 0, 180, "3", "SIG") + "\n  )\n)")
    return s


def _sym_relay() -> str:
    # 继电器（常开触点）：方框，左线圈 1=coil+/2=coil-，右触点 3=com/4=no
    body = _rect(-5.08, -3.81, 5.08, 3.81, width=0.508)
    s = (_def_head("CircuitTutor:Relay", "K", "Relay", hide_numbers=False) + "\n" +
         '  (symbol "Relay_0_1"\n' + body + '\n  )\n' +
         '  (symbol "Relay_1_1"\n' +
         _pin("passive", -7.62, 2.54, 180, "1", "coil+") + "\n" +
         _pin("passive", -7.62, -2.54, 180, "2", "coil-") + "\n" +
         _pin("passive", 7.62, 2.54, 0, "3", "com") + "\n" +
         _pin("passive", 7.62, -2.54, 0, "4", "no") + "\n  )\n)")
    return s


def _sym_optocoupler() -> str:
    # 光耦（PC817 脚号惯例）：左 LED 侧 1=A/2=K，右三极管侧 3=E/4=C；
    # 全 passive（与 KiCad Device 库光耦惯例一致，避免输出冲突误报）
    body = _rect(-5.08, -3.81, 5.08, 3.81, width=0.508)
    s = (_def_head("CircuitTutor:Optocoupler", "U", "Optocoupler", hide_numbers=False) + "\n" +
         '  (symbol "Optocoupler_0_1"\n' + body + '\n  )\n' +
         '  (symbol "Optocoupler_1_1"\n' +
         _pin("passive", -7.62, 2.54, 180, "1", "A") + "\n" +
         _pin("passive", -7.62, -2.54, 180, "2", "K") + "\n" +
         _pin("passive", 7.62, -2.54, 0, "3", "E") + "\n" +
         _pin("passive", 7.62, 2.54, 0, "4", "C") + "\n  )\n)")
    return s


def _sym_bridge() -> str:
    # 整流桥：方框，左交流 1=AC1/2=AC2，右直流 3=+/4=-
    body = _rect(-5.08, -3.81, 5.08, 3.81, width=0.508)
    s = (_def_head("CircuitTutor:Bridge_Rectifier", "BR", "Bridge_Rectifier", hide_numbers=False) + "\n" +
         '  (symbol "Bridge_Rectifier_0_1"\n' + body + '\n  )\n' +
         '  (symbol "Bridge_Rectifier_1_1"\n' +
         _pin("passive", -7.62, 2.54, 180, "1", "AC1") + "\n" +
         _pin("passive", -7.62, -2.54, 180, "2", "AC2") + "\n" +
         _pin("passive", 7.62, 2.54, 0, "3", "+") + "\n" +
         _pin("passive", 7.62, -2.54, 0, "4", "-") + "\n  )\n)")
    return s


def _sym_screw_terminal() -> str:
    # 接线端子：两个接线孔圆，水平双引脚（几何与 Fuse 一致）
    body = (_circle(-1.27, 0, 0.762) + "\n" +
            _circle(1.27, 0, 0.762))
    s = (_def_head("CircuitTutor:Screw_Terminal", "J", "Screw_Terminal", hide_numbers=True) + "\n" +
         '  (symbol "Screw_Terminal_0_1"\n' + body + '\n  )\n' +
         '  (symbol "Screw_Terminal_1_1"\n' +
         _pin("passive", -5.08, 0, 0, "1") + "\n" +
         _pin("passive", 5.08, 0, 180, "2") + "\n  )\n)")
    return s


# lib_id -> 嵌入符号定义（惰性构建，模块导入零开销）


# =====================================================================
# 2026-09-11 新件专用 KiCad 符号（工业级精修）
# =====================================================================

def _sym_ic_box(lib_id: str, ref: str, value: str, pins: list) -> str:
    """通用矩形 IC：pins=[(number, name, etype, side)] side=L/R/T/B。
    side 决定引脚朝向与坐标，用于模块/多脚芯片。"""
    # 计算尺寸
    left = [x for x in pins if x[3] == "L"]
    right = [x for x in pins if x[3] == "R"]
    top = [x for x in pins if x[3] == "T"]
    bot = [x for x in pins if x[3] == "B"]
    pitch = 2.54
    n = max(len(left), len(right), 1)
    hh = (n + 1) * pitch
    ww = max(12.7, (max(len(top), len(bot), 1) + 1) * pitch)
    x0, x1 = -ww / 2, ww / 2
    y0, y1 = -hh / 2, hh / 2
    name = lib_id.split(":")[-1]
    parts = [_def_head(lib_id, ref, value, hide_numbers=False)]
    parts.append(f'  (symbol "{name}_0_1"\n' + _rect(x0, y0, x1, y1, width=0.508) + "\n  )\n")
    pin_lines = []
    for i, (num, pname, et, side) in enumerate(left):
        y = y1 - pitch * (i + 1)
        pin_lines.append(_pin(et, x0 - 2.54, y, 180, num, pname))
    for i, (num, pname, et, side) in enumerate(right):
        y = y1 - pitch * (i + 1)
        pin_lines.append(_pin(et, x1 + 2.54, y, 0, num, pname))
    for i, (num, pname, et, side) in enumerate(top):
        x = x0 + pitch * (i + 1)
        pin_lines.append(_pin(et, x, y1 + 2.54, 270, num, pname))
    for i, (num, pname, et, side) in enumerate(bot):
        x = x0 + pitch * (i + 1)
        pin_lines.append(_pin(et, x, y0 - 2.54, 90, num, pname))
    parts.append(f'  (symbol "{name}_1_1"\n' + "\n".join(pin_lines) + "\n  )\n)")
    return "".join(parts)


def _sym_logic(lib_id: str, value: str, pins: list) -> str:
    """2 输入 1 输出逻辑门（ANSI 三角/与或简化为方框+引脚）。"""
    return _sym_ic_box(lib_id, "U", value, pins)


def _build_new_kicad_symbols() -> dict:
    d = {}
    # 电容细分：复用极性电容
    d["CircuitTutor:C_Ceramic"] = _sym_ic_box("CircuitTutor:C_Ceramic", "C", "C_Ceramic",
        [("1", "+", "passive", "L"), ("2", "-", "passive", "R")])
    d["CircuitTutor:C_Film"] = _sym_ic_box("CircuitTutor:C_Film", "C", "C_Film",
        [("1", "+", "passive", "L"), ("2", "-", "passive", "R")])
    d["CircuitTutor:C_Tantalum"] = _sym_ic_box("CircuitTutor:C_Tantalum", "C", "C_Tantalum",
        [("1", "+", "passive", "L"), ("2", "-", "passive", "R")])
    d["CircuitTutor:R_Network"] = _sym_ic_box("CircuitTutor:R_Network", "RN", "R_Network",
        [("1", "1", "passive", "L"), ("2", "2", "passive", "L"), ("3", "3", "passive", "L"), ("4", "com", "passive", "R")])
    d["CircuitTutor:L_Toroid"] = _sym_ic_box("CircuitTutor:L_Toroid", "L", "L_Toroid",
        [("1", "1", "passive", "L"), ("2", "2", "passive", "R")])
    d["CircuitTutor:Transformer"] = _sym_ic_box("CircuitTutor:Transformer", "T", "Transformer",
        [("1", "p1", "passive", "L"), ("2", "p2", "passive", "L"), ("3", "s1", "passive", "R"), ("4", "s2", "passive", "R")])
    d["CircuitTutor:LED_RGB"] = _sym_ic_box("CircuitTutor:LED_RGB", "LED", "LED_RGB",
        [("1", "R", "passive", "L"), ("2", "G", "passive", "L"), ("3", "B", "passive", "L"), ("4", "com", "passive", "R")])
    d["CircuitTutor:TRIAC"] = _sym_ic_box("CircuitTutor:TRIAC", "VS", "TRIAC",
        [("1", "A1", "passive", "L"), ("2", "G", "passive", "L"), ("3", "A2", "passive", "R")])
    d["CircuitTutor:DIAC"] = _sym_ic_box("CircuitTutor:DIAC", "DB", "DIAC",
        [("1", "1", "passive", "L"), ("2", "2", "passive", "R")])
    d["CircuitTutor:PPTC"] = _sym_ic_box("CircuitTutor:PPTC", "F", "PPTC",
        [("1", "1", "passive", "L"), ("2", "2", "passive", "R")])
    d["CircuitTutor:IGBT"] = _sym_ic_box("CircuitTutor:IGBT", "Q", "IGBT",
        [("1", "G", "input", "L"), ("2", "C", "passive", "T"), ("3", "E", "passive", "B")])
    d["CircuitTutor:UJT"] = _sym_ic_box("CircuitTutor:UJT", "Q", "UJT",
        [("1", "E", "passive", "L"), ("2", "B2", "passive", "T"), ("3", "B1", "passive", "B")])
    d["CircuitTutor:Phototriac"] = _sym_ic_box("CircuitTutor:Phototriac", "U", "Phototriac",
        [("1", "A", "passive", "L"), ("2", "K", "passive", "L"), ("3", "A1", "passive", "R"), ("4", "A2", "passive", "R")])
    # 逻辑门
    for gid, gname in [("AND","AND"),("OR","OR"),("NAND","NAND"),("NOR","NOR"),("XOR","XOR"),("BUF","BUF")]:
        pins = [("1", "A", "input", "L"), ("2", "B", "input", "L"), ("3", "Y", "output", "R")]
        if gid in ("BUF",):
            pins = [("1", "A", "input", "L"), ("3", "Y", "output", "R")]
        d[f"CircuitTutor:Gate_{gid}"] = _sym_logic(f"CircuitTutor:Gate_{gid}", gname, pins)
    d["CircuitTutor:FF_D"] = _sym_ic_box("CircuitTutor:FF_D", "U", "D_FF",
        [("1", "D", "input", "L"), ("2", "CLK", "input", "L"), ("3", "Q", "output", "R"), ("4", "Qn", "output", "R"), ("5", "RST", "input", "B")])
    d["CircuitTutor:Counter4"] = _sym_ic_box("CircuitTutor:Counter4", "U", "Counter4",
        [("1", "CLK", "input", "L"), ("2", "RST", "input", "L"), ("3", "Q0", "output", "R"), ("4", "Q1", "output", "R"), ("5", "Q2", "output", "R"), ("6", "Q3", "output", "R")])
    d["CircuitTutor:Shift595"] = _sym_ic_box("CircuitTutor:Shift595", "U", "74HC595",
        [("1", "DS", "input", "L"), ("2", "SHCP", "input", "L"), ("3", "STCP", "input", "L"), ("4", "Q7S", "output", "R"), ("5", "OE", "input", "B"), ("6", "MR", "input", "B")])
    d["CircuitTutor:LM393"] = _sym_ic_box("CircuitTutor:LM393", "U", "LM393",
        [("1", "1IN+", "input", "L"), ("2", "1IN-", "input", "L"), ("3", "1OUT", "output", "R"), ("4", "2IN+", "input", "L"), ("5", "2IN-", "input", "L"), ("6", "2OUT", "output", "R"), ("7", "GND", "power_in", "B"), ("8", "VCC", "power_in", "T")])
    d["CircuitTutor:LM324"] = _sym_ic_box("CircuitTutor:LM324", "U", "LM324",
        [("1", "1IN+", "input", "L"), ("2", "1IN-", "input", "L"), ("3", "1OUT", "output", "R"),
         ("4", "2IN+", "input", "L"), ("5", "2IN-", "input", "L"), ("6", "2OUT", "output", "R"),
         ("7", "GND", "power_in", "B"), ("8", "VCC", "power_in", "T")])
    d["CircuitTutor:NE556"] = _sym_ic_box("CircuitTutor:NE556", "U", "NE556",
        [("1", "GND", "power_in", "B"), ("2", "TRIG1", "input", "L"), ("3", "OUT1", "output", "R"), ("4", "RESET", "input", "L"), ("5", "CTRL1", "input", "L"), ("6", "THR1", "input", "L"), ("7", "DIS1", "open_collector", "R"), ("8", "VCC", "power_in", "T")])
    d["CircuitTutor:CD4017"] = _sym_ic_box("CircuitTutor:CD4017", "U", "CD4017",
        [("1", "CLK", "input", "L"), ("2", "RST", "input", "L"), ("3", "EN", "input", "L"), ("4", "Q0", "output", "R"), ("5", "Q1", "output", "R"), ("6", "Q2", "output", "R"), ("7", "VSS", "power_in", "B"), ("8", "VDD", "power_in", "T")])
    d["CircuitTutor:ATmega328"] = _sym_ic_box("CircuitTutor:ATmega328", "U", "ATmega328",
        [("1", "VCC", "power_in", "T"), ("2", "GND", "power_in", "B"),
         ("3", "PB0", "bidirectional", "L"), ("4", "PB1", "bidirectional", "L"),
         ("5", "PC0", "bidirectional", "L"), ("6", "PC1", "bidirectional", "R"),
         ("7", "PD0", "bidirectional", "R"), ("8", "PD1", "bidirectional", "R"),
         ("9", "AREF", "passive", "R"), ("10", "AVCC", "power_in", "T"),
         ("11", "PC4", "bidirectional", "L"), ("12", "PC5", "bidirectional", "R"),
         ("13", "RESET", "input", "L"), ("14", "XTAL1", "input", "L"),
         ("15", "XTAL2", "output", "L"), ("16", "GND2", "power_in", "B"),
         ("17", "PB2", "bidirectional", "L"), ("18", "PB3", "bidirectional", "L"),
         ("19", "PB4", "bidirectional", "L"), ("20", "PB5", "bidirectional", "L"),
         ("21", "PD2", "bidirectional", "R"), ("22", "PD3", "bidirectional", "R"),
         ("23", "PD4", "bidirectional", "R"), ("24", "PD5", "bidirectional", "R"),
         ("25", "PD6", "bidirectional", "R"), ("26", "PD7", "bidirectional", "R"),
         ("27", "PC2", "bidirectional", "R"), ("28", "PC3", "bidirectional", "R")])
    d["CircuitTutor:ESP32"] = _sym_ic_box("CircuitTutor:ESP32", "U", "ESP32",
        [("1", "3V3", "power_in", "L"), ("2", "GND", "power_in", "L"), ("3", "EN", "input", "L"), ("4", "GPIO0", "bidirectional", "L"), ("5", "GPIO2", "bidirectional", "R"), ("6", "TX", "output", "R"), ("7", "RX", "input", "R"), ("8", "GPIO21", "bidirectional", "L"), ("9", "GPIO22", "bidirectional", "R")])
    # 引脚号与 KiCad 官方 Sensor_Humidity:SHT4x 符号及其 DFN-4 封装一致
    d["CircuitTutor:SHT4x"] = _sym_ic_box("CircuitTutor:SHT4x", "U", "SHT4x",
        [("1", "SDA", "bidirectional", "L"), ("2", "SCL", "input", "L"),
         ("3", "VDD", "power_in", "R"), ("4", "VSS", "power_in", "R")])
    d["CircuitTutor:SPI_Header6"] = _sym_ic_box("CircuitTutor:SPI_Header6", "J", "SPI header",
        [("1", "SCK", "passive", "L"), ("2", "MOSI", "passive", "L"),
         ("3", "MISO", "passive", "L"), ("4", "CS", "passive", "R"),
         ("5", "GND", "passive", "R"), ("6", "VCC", "passive", "R")])
    # 传感器/显示模块（统一模块盒）
    modules = {
        "DHT11": [("1","VCC","power_in","L"),("2","DATA","bidirectional","L"),("3","NC","passive","R"),("4","GND","power_in","R")],
        "DS18B20": [("1","VDD","power_in","L"),("2","DQ","bidirectional","L"),("3","GND","power_in","R")],
        "BMP280": [("1","VCC","power_in","L"),("2","GND","power_in","L"),("3","SCL","input","L"),("4","SDA","bidirectional","R"),("5","SDO","bidirectional","R"),("6","CSB","input","R")],
        "MPU6050": [("1","VCC","power_in","L"),("2","GND","power_in","L"),("3","SCL","input","L"),("4","SDA","bidirectional","R"),("5","AD0","passive","R"),("6","INT","output","R")],
        "HC_SR04": [("1","VCC","power_in","L"),("2","TRIG","input","L"),("3","ECHO","output","R"),("4","GND","power_in","R")],
        "PIR": [("1","VCC","power_in","L"),("2","OUT","output","L"),("3","GND","power_in","R")],
        "MQ2": [("1","VCC","power_in","L"),("2","GND","power_in","L"),("3","DO","output","R"),("4","AO","passive","R")],
        "Sound": [("1","VCC","power_in","L"),("2","GND","power_in","L"),("3","DO","output","R"),("4","AO","passive","R")],
        "IR_RX": [("1","VCC","power_in","L"),("2","OUT","output","L"),("3","GND","power_in","R")],
        "Hall": [("1","VCC","power_in","L"),("2","OUT","output","L"),("3","GND","power_in","R")],
        "LoadCell": [("1","E+","power_in","L"),("2","E-","power_in","L"),("3","A+","passive","R"),("4","A-","passive","R")],
        "MAX7219": [("1","DIN","input","L"),("2","CS","input","L"),("3","CLK","input","L"),("4","DOUT","output","R"),("5","VCC","power_in","T"),("6","GND","power_in","B")],
        "TM1637": [("1","CLK","input","L"),("2","DIO","bidirectional","L"),("3","VCC","power_in","R"),("4","GND","power_in","R")],
        "LM317": [("1","IN","power_in","L"),("2","OUT","power_out","R"),("3","ADJ","passive","B")],
        "Buck": [("1","IN+","power_in","L"),("2","IN-","power_in","L"),("3","OUT+","power_out","R"),("4","OUT-","power_in","R")],
        "Boost": [("1","IN+","power_in","L"),("2","IN-","power_in","L"),("3","OUT+","power_out","R"),("4","OUT-","power_in","R")],
        "USB": [("1","VBUS","power_out","L"),("2","D-","bidirectional","L"),("3","D+","bidirectional","L"),("4","ID","passive","T"),("5","GND","power_in","R")],
        "DCJack": [("1","tip","passive","L"),("2","sleeve","power_in","L"),("3","switch","passive","R")],
        "Header4": [("1","1","passive","B"),("2","2","passive","B"),("3","3","passive","B"),("4","4","passive","B")],
        "ToggleSW": [("1","1","passive","L"),("2","2","passive","T"),("3","3","passive","R")],
        "DIP6": [("1","1","passive","B"),("2","2","passive","B"),("3","3","passive","B"),("4","4","passive","B"),("5","5","passive","B"),("6","6","passive","B")],
        "Reed": [("1","1","passive","L"),("2","2","passive","R")],
        "TrimRV": [("1","1","passive","L"),("2","2","passive","R"),("3","3","passive","T")],
        "SevenSeg": [("1","com","passive","R"),("2","a","passive","T"),("3","b","passive","R"),("4","c","passive","R"),("5","d","passive","B"),("6","e","passive","L"),("7","f","passive","L"),("8","g","passive","L")],
        "LCD1602": [("1","VSS","power_in","L"),("2","VDD","power_in","L"),("3","VO","passive","L"),("4","RS","input","L"),("5","RW","input","L"),("6","E","input","L"),("7","D4","bidirectional","B"),("8","D5","bidirectional","B"),("9","D6","bidirectional","B"),("10","D7","bidirectional","B"),("11","A","power_in","T"),("12","K","power_in","T")],
        "OLED": [("1","VCC","power_in","L"),("2","GND","power_in","L"),("3","SCL","input","T"),("4","SDA","bidirectional","T")],
        "Stepper": [("1","A+","passive","L"),("2","A-","passive","L"),("3","B+","passive","R"),("4","B-","passive","R")],
        "Solenoid": [("1","+","passive","L"),("2","-","passive","R")],
        "Heater": [("1","+","passive","L"),("2","-","passive","R")],
        "LiPo": [("1","+","passive","T"),("2","-","passive","B")],
        "Solar": [("1","+","passive","T"),("2","-","passive","B")],
        "Antenna": [("1","feed","passive","B")],
        "Jumper": [("1","1","passive","L"),("2","2","passive","R")],
        "OptoInt": [("1","VCC","power_in","L"),("2","GND","power_in","L"),("3","OUT","output","R")],
    }
    for mid, mpins in modules.items():
        d[f"CircuitTutor:Mod_{mid}"] = _sym_ic_box(f"CircuitTutor:Mod_{mid}", "U", mid, mpins)
    return d



def build_symbol_defs() -> dict:
    d = {
        "CircuitTutor:R": _sym_r(),
        "CircuitTutor:C_Polarized": _sym_c_pol(),
        "CircuitTutor:L": _sym_l(),
        "CircuitTutor:R_Potentiometer": _sym_pot(),
        "CircuitTutor:D": _sym_diode("D"),
        "CircuitTutor:D_Zener": _sym_diode("D_Zener", zener=True),
        "CircuitTutor:LED": _sym_led(),
        "CircuitTutor:Q_NPN_BEC": _sym_bjt("Q_NPN_BEC", pnp=False),
        "CircuitTutor:Q_PNP_BEC": _sym_bjt("Q_PNP_BEC", pnp=True),
        "CircuitTutor:Q_NMOS_DGS": _sym_nmos(),
        "CircuitTutor:Fuse": _sym_fuse(),
        "CircuitTutor:Crystal": _sym_crystal(),
        "CircuitTutor:Battery": _sym_battery(),
        "CircuitTutor:Buzzer": _sym_buzzer(),
        "CircuitTutor:Speaker": _sym_speaker(),
        "CircuitTutor:SW_Push": _sym_sw_push(),
        "CircuitTutor:NE555": _sym_ne555(),
        "CircuitTutor:L7805": _sym_7805(),
        "CircuitTutor:Opamp3": _sym_opamp3(),
        "CircuitTutor:PWR_FLAG": _sym_pwr_flag(),
        # 2026-09-09 常用元件扩展批
        "CircuitTutor:D_Schottky": _sym_diode("D_Schottky", schottky=True),
        "CircuitTutor:D_Photo": _sym_photodiode(),
        "CircuitTutor:Q_PMOS_DGS": _sym_pmos(),
        "CircuitTutor:R_Photo": _sym_photocell(),
        "CircuitTutor:R_Thermistor": _sym_thermistor(),
        "CircuitTutor:AC_Source": _sym_ac_source(),
        "CircuitTutor:Motor_DC": _sym_motor(),
        "CircuitTutor:Servo": _sym_servo(),
        "CircuitTutor:Relay": _sym_relay(),
        "CircuitTutor:Optocoupler": _sym_optocoupler(),
        "CircuitTutor:Bridge_Rectifier": _sym_bridge(),
        "CircuitTutor:Screw_Terminal": _sym_screw_terminal(),
    }
    d.update(_build_new_kicad_symbols())
    return d
