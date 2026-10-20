# -*- coding: utf-8 -*-
"""CircuitJSON -> KiCad 原理图 (.kicad_sch) 写入器（零第三方依赖）。

本模块负责"原理图正文与工程文件怎么写"：类型映射、网络计算、
export_kicad_sch 正文生成、三件套工程文件（.kicad_sym / sym-lib-table /
.kicad_pro）与命令行入口。符号定义见 kicad.symbols，kicad-cli 联动见
kicad.cli_runner（kicad.export 为兼容门面）。

设计要点
========
1. 输出为 KiCad 8 原理图格式（kicad_sch version 20231120，S 表达式纯文本），
   符号定义内嵌于文件的 (lib_symbols ...) 节——打开不需要安装任何符号库，
   KiCad 8/9 可直接打开并继续编辑（ERC / 网表导出 / 更新 PCB 均可用）。
2. 连通性方案：每个引脚引出 2.54mm 短走线，线端放网络标签。
   KiCad 中同名标签在本图纸内并入同一网络，因此无需任何长走线布线，
   连通性与引脚摆放位置解耦（对自动生成电路最稳健的方案）。
3. 网络命名与 erc.CircuitGraph 同源（按网络最小引脚引用名排序赋
   N1/N2...，与 ERC 报文互相可定位）；含 gnd/vcc 锚点的网络改名为 GND/VCC，
   对应的 vcc/gnd 锚点元件不输出为符号（其作用已由网络名承载）。
4. 引脚编号对齐 KiCad 标准库惯例，便于日后把嵌入式符号替换为官方库符号：
   二极管/LED/D_Zener: 1=K 2=A；Battery: 1=+ 2=-；Q_NPN/Q_PNP_BEC: 1=B 2=C 3=E；
   Q_NMOS_DGS: 1=D 2=G 3=S；NE555: 通用 DIP-8 引脚号；L7805: 1=IN 2=GND 3=OUT。
5. 输出确定性：uuid 用 uuid5(名字空间, 电路名+键) 生成，同一电路反复导出
   逐字节一致（与渲染管线“重复渲染确定性”的质量要求一致）。

已知限制
========
- 符号图形为自绘紧凑版（矩形/三角/圆等基本图元），非官方库原版图形；
- 元件一律 0° 摆放（原 JSON 中的 rotate 不映射），布局为确定性网格，
  不复用 SVG 布局算法——KiCad 侧重可编辑性与连通性，美观可后续手工调整；
- 引脚标签水平排布（90° 走线的标签也保持 0°），个别标签可能与走线接近。
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

from core import catalog
from core.erc import CircuitGraph, PT, run_erc
from kicad.symbols import (
    FONT_SZ,
    NS_URL,              # noqa: F401  （经门面对外暴露，保持兼容）
    STUB,
    _esc,
    _uid,
    build_symbol_defs,
    _sym_ic_box,
)

# 导出格式常量（KiCad 8.0 原理图）
SCH_VERSION = "20231120"
GENERATOR = "zhihui-dianlu"
GENERATOR_VERSION = "1.0"


# =====================================================================
# 类型映射：CircuitJSON 元件类型 -> KiCad lib_id / 引脚号 / 位号前缀
# =====================================================================

# (lib_id, {本目录引脚名: KiCad 引脚号}, 版式类 vert|horiz|ic|opamp)
TYPE_MAP = {
    "resistor":          ("CircuitTutor:R", {"1": "1", "2": "2"}, "vert"),
    "capacitor":         ("CircuitTutor:C_Polarized", {"+": "1", "-": "2"}, "vert"),
    "inductor":          ("CircuitTutor:L", {"1": "1", "2": "2"}, "vert"),
    "potentiometer":     ("CircuitTutor:R_Potentiometer", {"1": "1", "2": "2", "3": "3"}, "vert"),
    "diode":             ("CircuitTutor:D", {"A": "2", "K": "1", "+": "2", "-": "1"}, "vert"),
    "zener":             ("CircuitTutor:D_Zener", {"A": "2", "K": "1", "+": "2", "-": "1"}, "vert"),
    "led":               ("CircuitTutor:LED", {"+": "2", "-": "1"}, "vert"),
    "npn":               ("CircuitTutor:Q_NPN_BEC", {"B": "1", "C": "2", "E": "3"}, "horiz"),
    "pnp":               ("CircuitTutor:Q_PNP_BEC", {"B": "1", "C": "2", "E": "3"}, "horiz"),
    "nmos":              ("CircuitTutor:Q_NMOS_DGS", {"D": "1", "G": "2", "S": "3"}, "horiz"),
    "fuse":              ("CircuitTutor:Fuse", {"1": "1", "2": "2"}, "horiz"),
    "crystal":           ("CircuitTutor:Crystal", {"1": "1", "2": "2"}, "vert"),
    "battery":           ("CircuitTutor:Battery", {"+": "1", "-": "2"}, "vert"),
    "buzzer":            ("CircuitTutor:Buzzer", {"+": "1", "-": "2"}, "horiz"),
    "speaker":           ("CircuitTutor:Speaker", {"+": "1", "-": "2"}, "horiz"),
    "switch":            ("CircuitTutor:SW_Push", {"1": "1", "2": "2"}, "horiz"),
    "pushbutton":        ("CircuitTutor:SW_Push", {"1": "1", "2": "2"}, "horiz"),
    "ne555":             ("CircuitTutor:NE555", {"GND": "1", "TRIG": "2", "OUT": "3", "RESET": "4",
                                          "CTRL": "5", "THR": "6", "DISCH": "7", "VCC": "8"}, "ic"),
    "regulator_7805":    ("CircuitTutor:L7805", {"IN": "1", "GND": "2", "OUT": "3"}, "ic"),
    "opamp_inverting":   ("CircuitTutor:Opamp3", {"+": "1", "-": "2", "out": "3"}, "opamp"),
    "opamp_noninverting": ("CircuitTutor:Opamp3", {"+": "1", "-": "2", "out": "3"}, "opamp"),
    "opamp_comparator":  ("CircuitTutor:Opamp3", {"+": "1", "-": "2", "out": "3"}, "opamp"),
    "opamp_follower":    ("CircuitTutor:Opamp3", {"+": "1", "-": "2", "out": "3"}, "opamp"),
    # 2026-09-09 常用元件扩展批（符号定义见 symbols.py 扩展批区块）
    "schottky":          ("CircuitTutor:D_Schottky", {"A": "2", "K": "1", "+": "2", "-": "1"}, "vert"),
    "photodiode":        ("CircuitTutor:D_Photo", {"A": "2", "K": "1", "+": "2", "-": "1"}, "vert"),
    "pmos":              ("CircuitTutor:Q_PMOS_DGS", {"D": "1", "G": "2", "S": "3"}, "horiz"),
    "photocell":         ("CircuitTutor:R_Photo", {"1": "1", "2": "2"}, "vert"),
    "thermistor":        ("CircuitTutor:R_Thermistor", {"1": "1", "2": "2"}, "vert"),
    "ac_source":         ("CircuitTutor:AC_Source", {"+": "1", "-": "2"}, "vert"),
    "dc_motor":          ("CircuitTutor:Motor_DC", {"+": "1", "-": "2"}, "horiz"),
    "servo":             ("CircuitTutor:Servo", {"VCC": "1", "GND": "2", "SIG": "3"}, "ic"),
    "relay":             ("CircuitTutor:Relay", {"coil+": "1", "coil-": "2", "com": "3", "no": "4"}, "ic"),
    "optocoupler":       ("CircuitTutor:Optocoupler", {"A": "1", "K": "2", "E": "3", "C": "4"}, "ic"),
    "bridge_rectifier":  ("CircuitTutor:Bridge_Rectifier", {"AC1": "1", "AC2": "2", "+": "3", "-": "4"}, "ic"),
    "screw_terminal":    ("CircuitTutor:Screw_Terminal", {"1": "1", "2": "2"}, "horiz"),
        # 2026-09-11 专用符号精修（80 种）
    "capacitor_ceramic": ("CircuitTutor:C_Ceramic", {"+": "1", "-": "2"}, "vert"),
    "capacitor_film": ("CircuitTutor:C_Film", {"+": "1", "-": "2"}, "vert"),
    "capacitor_tantalum": ("CircuitTutor:C_Tantalum", {"+": "1", "-": "2"}, "vert"),
    "resistor_network": ("CircuitTutor:R_Network", {"1": "1", "2": "2", "3": "3", "com": "4"}, "ic"),
    "inductor_toroid": ("CircuitTutor:L_Toroid", {"1": "1", "2": "2"}, "vert"),
    "transformer": ("CircuitTutor:Transformer", {"p1": "1", "p2": "2", "s1": "3", "s2": "4"}, "ic"),
    "led_rgb": ("CircuitTutor:LED_RGB", {"R": "1", "G": "2", "B": "3", "com": "4"}, "ic"),
    "triac": ("CircuitTutor:TRIAC", {"A1": "1", "G": "2", "A2": "3"}, "ic"),
    "diac": ("CircuitTutor:DIAC", {"1": "1", "2": "2"}, "vert"),
    "fuse_resettable": ("CircuitTutor:PPTC", {"1": "1", "2": "2"}, "vert"),
    "igbt": ("CircuitTutor:IGBT", {"G": "1", "C": "2", "E": "3"}, "horiz"),
    "ujt": ("CircuitTutor:UJT", {"E": "1", "B2": "2", "B1": "3"}, "horiz"),
    "phototriac": ("CircuitTutor:Phototriac", {"A": "1", "K": "2", "A1": "3", "A2": "4"}, "ic"),
    "ic_and": ("CircuitTutor:Gate_AND", {"A": "1", "B": "2", "Y": "3"}, "ic"),
    "ic_or": ("CircuitTutor:Gate_OR", {"A": "1", "B": "2", "Y": "3"}, "ic"),
    "ic_nand": ("CircuitTutor:Gate_NAND", {"A": "1", "B": "2", "Y": "3"}, "ic"),
    "ic_nor": ("CircuitTutor:Gate_NOR", {"A": "1", "B": "2", "Y": "3"}, "ic"),
    "ic_xor": ("CircuitTutor:Gate_XOR", {"A": "1", "B": "2", "Y": "3"}, "ic"),
    "ic_not": ("CircuitTutor:Gate_BUF", {"A": "1", "Y": "3"}, "ic"),
    "ic_buffer": ("CircuitTutor:Gate_BUF", {"A": "1", "Y": "3"}, "ic"),
    "flipflop_d": ("CircuitTutor:FF_D", {"D": "1", "CLK": "2", "Q": "3", "Qn": "4", "RST": "5"}, "ic"),
    "counter_4bit": ("CircuitTutor:Counter4", {"CLK": "1", "RST": "2", "Q0": "3", "Q1": "4", "Q2": "5", "Q3": "6"}, "ic"),
    "shiftreg_8bit": ("CircuitTutor:Shift595", {"DS": "1", "SHCP": "2", "STCP": "3", "Q7S": "4", "OE": "5", "MR": "6"}, "ic"),
    "lm393": ("CircuitTutor:LM393", {"1IN+": "1", "1IN-": "2", "1OUT": "3", "2IN+": "4", "2IN-": "5", "2OUT": "6", "GND": "7", "VCC": "8"}, "ic"),
    "lm324": ("CircuitTutor:LM324", {"1IN+": "1", "1IN-": "2", "1OUT": "3", "2IN+": "4", "2IN-": "5", "2OUT": "6", "GND": "7", "VCC": "8"}, "ic"),
    "ne556": ("CircuitTutor:NE556", {"GND": "1", "TRIG1": "2", "OUT1": "3", "RESET": "4", "CTRL1": "5", "THR1": "6", "DIS1": "7", "VCC": "8"}, "ic"),
    "cd4017": ("CircuitTutor:CD4017", {"CLK": "1", "RST": "2", "EN": "3", "Q0": "4", "Q1": "5", "Q2": "6", "VSS": "7", "VDD": "8"}, "ic"),
    # MCU 为本项目自绘概念符号；编号只保证本原理图/网表自洽，绝不可用作实际封装焊盘号。
    # Keep v1 abstract pin numbers stable; manufacturing uses component.pin_map.
    "atmega328": ("CircuitTutor:ATmega328", {
        "VCC": "1", "GND": "2", "PB0": "3", "PB1": "4", "PC0": "5", "PC1": "6",
        "PD0": "7", "PD1": "8", "AREF": "9", "AVCC": "10", "PC4": "11", "PC5": "12",
        "RESET": "13", "XTAL1": "14", "XTAL2": "15", "GND2": "16",
        "PB2": "17", "PB3": "18", "PB4": "19", "PB5": "20", "PD2": "21", "PD3": "22",
        "PD4": "23", "PD5": "24", "PD6": "25", "PD7": "26", "PC2": "27", "PC3": "28",
    }, "ic"),
    "esp32": ("CircuitTutor:ESP32", {"3V3": "1", "GND": "2", "EN": "3", "GPIO0": "4", "GPIO2": "5", "TX": "6", "RX": "7", "GPIO21": "8", "GPIO22": "9"}, "ic"),
    "dht11": ("CircuitTutor:Mod_DHT11", {"VCC": "1", "DATA": "2", "NC": "3", "GND": "4"}, "ic"),
    "ds18b20": ("CircuitTutor:Mod_DS18B20", {"VDD": "1", "DQ": "2", "GND": "3"}, "ic"),
    "bmp280": ("CircuitTutor:Mod_BMP280", {"VCC": "1", "GND": "2", "SCL": "3", "SDA": "4", "SDO": "5", "CSB": "6"}, "ic"),
    "mpu6050": ("CircuitTutor:Mod_MPU6050", {"VCC": "1", "GND": "2", "SCL": "3", "SDA": "4", "AD0": "5", "INT": "6"}, "ic"),
    "hc_sr04": ("CircuitTutor:Mod_HC_SR04", {"VCC": "1", "TRIG": "2", "ECHO": "3", "GND": "4"}, "ic"),
    "pir": ("CircuitTutor:Mod_PIR", {"VCC": "1", "OUT": "2", "GND": "3"}, "ic"),
    "mq2": ("CircuitTutor:Mod_MQ2", {"VCC": "1", "GND": "2", "DO": "3", "AO": "4"}, "ic"),
    "sound_sensor": ("CircuitTutor:Mod_Sound", {"VCC": "1", "GND": "2", "DO": "3", "AO": "4"}, "ic"),
    "ir_receiver": ("CircuitTutor:Mod_IR_RX", {"VCC": "1", "OUT": "2", "GND": "3"}, "ic"),
    "hall_sensor": ("CircuitTutor:Mod_Hall", {"VCC": "1", "OUT": "2", "GND": "3"}, "ic"),
    "load_cell": ("CircuitTutor:Mod_LoadCell", {"E+": "1", "E-": "2", "A+": "3", "A-": "4"}, "ic"),
    "max7219": ("CircuitTutor:Mod_MAX7219", {"DIN": "1", "CS": "2", "CLK": "3", "DOUT": "4", "VCC": "5", "GND": "6"}, "ic"),
    "tm1637": ("CircuitTutor:Mod_TM1637", {"CLK": "1", "DIO": "2", "VCC": "3", "GND": "4"}, "ic"),
    "lm317": ("CircuitTutor:Mod_LM317", {"IN": "1", "OUT": "2", "ADJ": "3"}, "ic"),
    "buck_module": ("CircuitTutor:Mod_Buck", {"IN+": "1", "IN-": "2", "OUT+": "3", "OUT-": "4"}, "ic"),
    "boost_module": ("CircuitTutor:Mod_Boost", {"IN+": "1", "IN-": "2", "OUT+": "3", "OUT-": "4"}, "ic"),
    "usb_port": ("CircuitTutor:Mod_USB", {"VBUS": "1", "D-": "2", "D+": "3", "ID": "4", "GND": "5"}, "ic"),
    "barrel_jack": ("CircuitTutor:Mod_DCJack", {"tip": "1", "sleeve": "2", "switch": "3"}, "ic"),
    "conn_header": ("CircuitTutor:Mod_Header4", {"1": "1", "2": "2", "3": "3", "4": "4"}, "ic"),
    "switch_toggle": ("CircuitTutor:Mod_ToggleSW", {"1": "1", "2": "2", "3": "3"}, "ic"),
    "switch_dip": ("CircuitTutor:Mod_DIP6", {"1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6"}, "ic"),
    "switch_reed": ("CircuitTutor:Mod_Reed", {"1": "1", "2": "2"}, "vert"),
    "potentiometer_trim": ("CircuitTutor:Mod_TrimRV", {"1": "1", "2": "2", "3": "3"}, "ic"),
    "sevenseg": ("CircuitTutor:Mod_SevenSeg", {"com": "1", "a": "2", "b": "3", "c": "4", "d": "5", "e": "6", "f": "7", "g": "8"}, "ic"),
    "lcd1602": ("CircuitTutor:Mod_LCD1602", {"VSS": "1", "VDD": "2", "VO": "3", "RS": "4", "RW": "5", "E": "6", "D4": "7", "D5": "8", "D6": "9", "D7": "10", "A": "11", "K": "12"}, "ic"),
    "oled_ssd1306": ("CircuitTutor:Mod_OLED", {"VCC": "1", "GND": "2", "SCL": "3", "SDA": "4"}, "ic"),
    "stepper": ("CircuitTutor:Mod_Stepper", {"A+": "1", "A-": "2", "B+": "3", "B-": "4"}, "ic"),
    "solenoid": ("CircuitTutor:Mod_Solenoid", {"+": "1", "-": "2"}, "horiz"),
    "heater": ("CircuitTutor:Mod_Heater", {"+": "1", "-": "2"}, "horiz"),
    "lipo_battery": ("CircuitTutor:Mod_LiPo", {"+": "1", "-": "2"}, "vert"),
    "solar_cell": ("CircuitTutor:Mod_Solar", {"+": "1", "-": "2"}, "vert"),
    "antenna": ("CircuitTutor:Mod_Antenna", {"feed": "1"}, "vert"),
    "jumper_wire": ("CircuitTutor:Mod_Jumper", {"1": "1", "2": "2"}, "horiz"),
    "optointerrupter": ("CircuitTutor:Mod_OptoInt", {"VCC": "1", "GND": "2", "OUT": "3"}, "ic"),
    "ir_led": ("CircuitTutor:LED", {"A": "2", "K": "1", "+": "2", "-": "1"}, "vert"),
    "phototransistor": ("CircuitTutor:Q_NPN_BEC", {"C": "2", "E": "3"}, "horiz"),
    "tvs": ("CircuitTutor:D_Zener", {"A": "2", "K": "1", "+": "2", "-": "1"}, "vert"),
    "scr": ("CircuitTutor:NE555", {"A": "1", "K": "2", "G": "3"}, "ic"),
    "diode_rectifier": ("CircuitTutor:D", {"A": "2", "K": "1", "+": "2", "-": "1"}, "vert"),
    "varactor": ("CircuitTutor:D_Zener", {"A": "2", "K": "1", "+": "2", "-": "1"}, "vert"),
    "ferrite_bead": ("CircuitTutor:L", {"1": "1", "2": "2"}, "horiz"),
    "varistor": ("CircuitTutor:R", {"1": "1", "2": "2"}, "vert"),
    "electrolytic": ("CircuitTutor:C_Polarized", {"+": "1", "-": "2"}, "vert"),
    "buzzer_passive": ("CircuitTutor:Buzzer", {"+": "1", "-": "2"}, "horiz"),
    "buzzer_magnetic": ("CircuitTutor:Buzzer", {"+": "1", "-": "2"}, "horiz"),
    "speaker_8ohm": ("CircuitTutor:Speaker", {"+": "1", "-": "2"}, "horiz"),
    "microphone": ("CircuitTutor:R", {"+": "1", "-": "2"}, "vert"),
    "ammeter": ("CircuitTutor:R", {"+": "1", "-": "2"}, "horiz"),
    "voltmeter": ("CircuitTutor:R", {"+": "1", "-": "2"}, "vert"),
    "capacitor_trim": ("CircuitTutor:C_Polarized", {"+": "1", "-": "2"}, "vert"),
    "capacitor_variable": ("CircuitTutor:C_Polarized", {"+": "1", "-": "2"}, "vert"),
    "capacitor_feedthrough": ("CircuitTutor:NE555", {"1": "1", "2": "2", "3": "3"}, "ic"),
    "crystal_gnd": ("CircuitTutor:NE555", {"1": "1", "2": "2", "3": "3", "4": "4"}, "ic"),
    "circuit_breaker_1p": ("CircuitTutor:R", {"1": "1", "2": "2"}, "vert"),
    "circuit_breaker_2p": ("CircuitTutor:NE555", {"1": "1", "2": "2", "3": "3", "4": "4"}, "ic"),
    "circuit_breaker_3p": ("CircuitTutor:NE555", {"1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6"}, "ic"),
    "capacitor_network": ("CircuitTutor:NE555", {"1": "1", "2": "2", "3": "3", "4": "4", "com": "5"}, "ic"),
    "antenna_dipole": ("CircuitTutor:Fuse", {"1": "1", "2": "2"}, "vert"),
    "battery_cell_1v5": ("CircuitTutor:Battery", {"+": "1", "-": "2"}, "vert"),
    "connector_screw2": ("CircuitTutor:Screw_Terminal", {"1": "1", "2": "2"}, "horiz"),
    "opamp": ("CircuitTutor:Opamp3", {"+": "1", "-": "2", "out": "3"}, "opamp"),
    # 2026 电源管理 + 通信接口 + 保护器件 + 模拟前端扩容（复用既有符号，引脚号唯一）
    "ldo_3v3": ("CircuitTutor:L7805", {"IN": "1", "GND": "2", "OUT": "3"}, "ic"),
    "ldo_1v8": ("CircuitTutor:L7805", {"IN": "1", "GND": "2", "OUT": "3"}, "ic"),
    "charge_pump": ("CircuitTutor:NE555", {"VIN": "1", "GND": "2", "VOUT": "3", "C+": "4", "C-": "5"}, "ic"),
    "voltage_monitor": ("CircuitTutor:NE555", {"VCC": "1", "GND": "2", "RESET": "3", "MR": "4"}, "ic"),
    "power_sequencer": ("CircuitTutor:NE555", {"VIN": "1", "GND": "2", "EN": "3", "PG": "4", "VOUT": "5"}, "ic"),
    "uart_header": ("CircuitTutor:Mod_Header4", {"TX": "1", "RX": "2", "GND": "3", "VCC": "4"}, "ic"),
    "spi_header": ("CircuitTutor:SPI_Header6", {"SCK": "1", "MOSI": "2", "MISO": "3", "CS": "4", "GND": "5", "VCC": "6"}, "ic"),
    "i2c_header": ("CircuitTutor:Mod_Header4", {"SCL": "1", "SDA": "2", "GND": "3", "VCC": "4"}, "ic"),
    "i2c_rh_sensor": ("CircuitTutor:SHT4x", {"SDA": "1", "SCL": "2", "VDD": "3", "VSS": "4"}, "ic"),
    "can_transceiver": ("CircuitTutor:NE555", {"TXD": "1", "RXD": "2", "VCC": "3", "GND": "4", "CANH": "5", "CANL": "6"}, "ic"),
    "rs485_transceiver": ("CircuitTutor:NE555", {"RO": "1", "DI": "2", "DE": "3", "RE": "4", "A": "5", "B": "6", "VCC": "7", "GND": "8"}, "ic"),
    "usb_c": ("CircuitTutor:NE555", {"VBUS": "1", "GND": "2", "D+": "3", "D-": "4", "CC1": "5", "CC2": "6"}, "ic"),
    "tvs_array": ("CircuitTutor:NE555", {"IO1": "1", "IO2": "2", "GND": "3", "VCC": "4"}, "ic"),
    "esd_diode": ("CircuitTutor:D", {"A": "2", "K": "1", "+": "2", "-": "1"}, "vert"),
    "common_mode_choke": ("CircuitTutor:NE555", {"1": "1", "2": "2", "3": "3", "4": "4"}, "ic"),
    "gas_discharge_tube": ("CircuitTutor:R", {"1": "1", "2": "2"}, "vert"),
    "resettable_fuse_1206": ("CircuitTutor:Fuse", {"1": "1", "2": "2"}, "vert"),
    "instrumentation_amp": ("CircuitTutor:NE555", {"IN+": "1", "IN-": "2", "REF": "3", "OUT": "4", "V+": "5", "V-": "6"}, "ic"),
    "current_sense_amp": ("CircuitTutor:NE555", {"IN+": "1", "IN-": "2", "OUT": "3", "VCC": "4", "GND": "5"}, "ic"),
    "digital_pot": ("CircuitTutor:NE555", {"A": "1", "W": "2", "B": "3", "CS": "4", "SCK": "5", "SI": "6", "VCC": "7", "GND": "8"}, "ic"),
    "dac_8bit": ("CircuitTutor:NE555", {"CS": "1", "SCK": "2", "DIN": "3", "OUT": "4", "VREF": "5", "VCC": "6", "GND": "7"}, "ic"),
    "adc_8bit": ("CircuitTutor:NE555", {"AIN": "1", "DOUT": "2", "CLK": "3", "CS": "4", "VREF": "5", "VCC": "6", "GND": "7"}, "ic"),
    "buzzer_magnetic_5v": ("CircuitTutor:Buzzer", {"+": "1", "-": "2"}, "horiz"),
    "relay_solid_state": ("CircuitTutor:Relay", {"IN+": "1", "IN-": "2", "OUT1": "3", "OUT2": "4"}, "ic"),
    "optocoupler_ac": ("CircuitTutor:Optocoupler", {"A": "1", "K": "2", "E": "3", "C": "4"}, "ic"),
}

# vcc/gnd 是网络锚点：不出符号，网络改名为 GND/VCC
ANCHOR_NET_NAMES = {"vcc": "VCC", "gnd": "GND"}


def _parse_pins(def_text: str) -> dict:
    """从符号定义文本提取 {引脚号: (文件坐标x, y, 指向角度)}。"""
    pins = {}
    pat = re.compile(
        r'\(pin\s+\w+\s+line\s+\(at\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\)\s+\(length\s+[\d.]+\)'
        r'[\s\S]*?\(number\s+"([^"]+)"')
    for m in pat.finditer(def_text):
        pins[m.group(4)] = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
    return pins


def _i2c_header_layout(part: dict) -> tuple[str, dict[str, str], str | None]:
    """Resolve a sourced physical connector order without changing logical I2C pins."""
    lib_id, default_map, _ = TYPE_MAP["i2c_header"]
    component = part.get("component") or {}
    if not isinstance(component, dict):
        raise ValueError(f"{part['id']}: component 必须为对象")
    mapping = component.get("pin_map")
    source = component.get("pin_map_source")
    if mapping is None:
        if source:
            raise ValueError(f"{part['id']}: 有引脚来源但缺少 pin_map")
        return lib_id, default_map, None
    if (not isinstance(mapping, dict) or set(mapping) != set(default_map)
            or any(not isinstance(v, str) or v not in {"1", "2", "3", "4"} for v in mapping.values())
            or len(set(mapping.values())) != 4):
        raise ValueError(f"{part['id']}: i2c_header pin_map 必须将四个逻辑脚一一映射到物理脚 1-4")
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"{part['id']}: 显式 i2c_header pin_map 缺少 pin_map_source")
    if mapping == default_map:
        return lib_id, default_map, None
    suffix = "".join(mapping[pin] for pin in default_map)
    variant_id = f"CircuitTutor:Mod_Header4_{suffix}"
    definition = _sym_ic_box(variant_id, "J", "I2C header",
                             [(mapping[pin], pin, "passive", "B") for pin in default_map])
    return variant_id, mapping, definition


def _compute_nets(circuit: dict, graph: CircuitGraph) -> dict:
    """{引脚引用 "PID:PIN": 网络名}；gnd/vcc 网络改名为 GND/VCC。"""
    part_type = {pid: p.get("type") for pid, p in graph.parts.items()}
    result = {}
    for root, members in graph.net_members.items():
        name = graph.net_names.get(root, "?")
        for anchor_t, label in ANCHOR_NET_NAMES.items():
            if any(part_type.get(r.split(":", 1)[0]) == anchor_t for r in members):
                name = label
                break
        for r in members:
            result[r] = name
    return result


# 版式 -> (Reference 属性偏移, Value 属性偏移)（画布坐标增量，Y 向下为正）
_PROP_OFFSET = {
    "vert":  ((4.445, -1.27), (4.445, 1.27)),
    "horiz": ((-2.54, -5.59), (-2.54, -3.81)),
    "ic":    ((-8.89, -9.525), (-8.89, -7.62)),
    "opamp": ((-2.54, -7.62), (-2.54, -5.715)),
}


def _grid(n: int):
    """n 个元件的确定性网格布局 -> (cols, cell_w, cell_h, x0, y0)。

    所有常量均为 KiCad 50mil (1.27mm) 栅格的整数倍——符号原点落在栅格上，
    引脚偏移（全部为 1.27 的倍数）与 2.54mm 短走线端点才能逐级保持对齐，
    否则 KiCad 官方 ERC 会报 endpoint_off_grid（真机 9.0.8 实测）。"""
    if n <= 12:
        return 4, 53.34, 38.1, 38.1, 44.45
    if n <= 25:
        return 5, 45.72, 31.75, 31.75, 40.64
    return 6, 40.64, 26.67, 30.48, 36.83


def export_kicad_sch(circuit: dict, name: str, kb: dict | None = None,
                     erc_report=None, collect_manifest: bool = False):
    """导出单个电路为 KiCad 原理图文本。

    返回：文本字符串；collect_manifest=True 时返回 (文本, {引脚引用: 网络名})。
    """
    kb = kb if kb is not None else catalog.build_erc_kb()
    if erc_report is None:
        erc_report = run_erc(circuit, kb)
    graph = CircuitGraph(circuit, kb)
    pin_net = _compute_nets(circuit, graph)
    defs = build_symbol_defs()

    warnings = []
    parts = [p for p in circuit.get("parts", []) if p.get("id") is not None]
    placed = [p for p in parts if p.get("type") in TYPE_MAP]
    for p in parts:
        if p["type"] in ANCHOR_NET_NAMES:
            continue
        if p["type"] not in TYPE_MAP:
            warnings.append(f"未知元件类型 {p['type']}({p['id']}) 未导出")
    # 电池优先，其余保持原顺序（确定性）
    placed.sort(key=lambda p: 0 if p["type"] == "battery" else 1)
    i2c_layouts = {id(p): _i2c_header_layout(p) for p in placed if p["type"] == "i2c_header"}
    for lib_id, _, definition in i2c_layouts.values():
        if definition:
            defs[lib_id] = definition

    # 需要 PWR_FLAG 的网络（官方 ERC 规则：power_in 引脚须由 power_out 驱动，
    # 而本导出方案的连通性全部由标签承载、引脚在 KiCad 侧几乎全为 passive）：
    # ① 锚点网络 GND/VCC——纯标签承载，无任何 power_out（ne555_blink 实测）；
    # ② 含真实供电源的网络——battery/dc_source/ac_source 的 POWER 脚在 KiCad
    #    侧同为 passive，电池直供芯片时官方报 power_pin_not_driven。此前只认
    #    字面锚点名，电池直供（网络名非 VCC/GND）漏挂标志，与“加 vcc 标记又
    #    触发内置 ERC 电源冲突”构成双死结（2026-09-10 解开：两处同时放宽）。
    #    按 KiCad 惯例对 flag_nets 一律挂 PWR_FLAG 声明外部供电。
    flag_nets = {v for v in pin_net.values() if v in ANCHOR_NET_NAMES.values()}
    for _pid, _part in graph.parts.items():
        for _pn, _et in (kb.get(str(_part.get("type"))) or {}).items():
            if _et == PT.POWER:
                _net = pin_net.get(f"{_pid}:{_pn}")
                if _net and _net != "?":
                    flag_nets.add(_net)
    needs_flag = bool(flag_nets)
    used_lib_ids = sorted({i2c_layouts[id(p)][0] if p["type"] == "i2c_header" else TYPE_MAP[p["type"]][0] for p in placed}
                          | ({"CircuitTutor:PWR_FLAG"} if needs_flag else set()))

    lines = []
    ap = lines.append
    ap(f'(kicad_sch (version {SCH_VERSION}) (generator "{GENERATOR}") '
       f'(generator_version "{GENERATOR_VERSION}")')
    ap(f'  (uuid "{_uid(name, "sheet")}")')
    ap('  (paper "A4")')
    counts = erc_report.counts()
    fatals = counts["fatal"]
    majors = counts["major"]
    minors = counts["minor"] + counts["warning"]
    ap('  (title_block')
    ap(f'    (title "{_esc(name)}")')
    ap(f'    (comment 1 "由 智绘电路 (CircuitJSON) 自动导出；ERC: fatal={fatals} major={majors} minor={minors}")')
    ap(f'    (comment 2 "连通性以网络标签承载（GND/VCC/N1..Nn），与 ERC 报文同源")')
    ap('  )')

    # ---- lib_symbols ----
    ap('  (lib_symbols')
    for lib_id in used_lib_ids:
        def_text = defs[lib_id].strip()
        # 去掉模板最外层的右括号后按缩进嵌入
        body_lines = def_text.splitlines()
        for ln in body_lines:
            ap("  " + ln)
    ap('  )')

    # ---- 元件布局与引脚标签 ----
    n = len(placed)
    cols, cw, ch, x0, y0 = _grid(n)
    manifest = {}
    symbol_blocks = []
    wire_lines = []
    label_lines = []
    noconn_lines = []
    flag_points = {}   # 锚点网络名 -> 首个短走线端点（挂 PWR_FLAG 的位置）

    for idx, p in enumerate(placed):
        lib_id, pin_map, orient = TYPE_MAP[p["type"]]
        if p["type"] == "i2c_header":
            lib_id, pin_map, _ = i2c_layouts[id(p)]
        col, row = idx % cols, idx // cols
        ax, ay = x0 + col * cw, y0 + row * ch
        pins = _parse_pins(defs[lib_id])
        ref = str(p["id"])
        value = (p.get("attrs") or {}).get("value") or p["type"]
        # 封装映射：原理图实例写入 Footprint，KiCad「更新 PCB」可自动落封装
        try:
            from core.kicad_footprints import footprint_for_part
            fp = footprint_for_part(p)
        except ImportError:
            fp = ""
        fp_prop = (
            f'    (property "Footprint" "{_esc(fp)}" (at {ax:g} {ay:g} 0)\n'
            f'      (effects (font (size {FONT_SZ} {FONT_SZ})) (hide yes))\n'
            f'    )\n'
        ) if fp else ""
        # uuid 键里带 idx：重复位号（错误示范电路）也能保证 uuid 全局唯一
        suid = _uid(name, "sym", idx, ref)

        (roff, voff) = _PROP_OFFSET[orient]
        symbol_blocks.append(
            f'  (symbol (lib_id "{lib_id}") (at {ax:g} {ay:g} 0) (unit 1)\n'
            f'    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n'
            f'    (uuid "{suid}")\n'
            f'    (property "Reference" "{_esc(ref)}" (at {ax + roff[0]:g} {ay + roff[1]:g} 0)\n'
            f'      (effects (font (size {FONT_SZ} {FONT_SZ})))\n'
            f'    )\n'
            f'    (property "Value" "{_esc(value)}" (at {ax + voff[0]:g} {ay + voff[1]:g} 0)\n'
            f'      (effects (font (size {FONT_SZ} {FONT_SZ})))\n'
            f'    )\n' + fp_prop +
            "".join(f'    (pin "{num}" (uuid "{_uid(name, "spin", idx, ref, num)}"))\n'
                    for num in sorted(pins, key=lambda s: (len(s), s))) +
            f'    (instances (project "zhihui" (path "/{_uid(name, "sheet")}" '
            f'(reference "{_esc(ref)}") (unit 1))))\n'
            f'  )'
        )

        # 引脚映射按 KiCad 引脚号分组（别名如 +/A 指向同一物理脚）：
        # 同组内任一别名有网络即认为该脚连通；全组无网络才打 no_connect。
        # （否则未引用的别名会被误判悬空——diode_protect 官方 ERC 实测）
        by_num = {}
        for our_pin, num in pin_map.items():
            by_num.setdefault(num, []).append(our_pin)
        for num in sorted(by_num, key=lambda s: (len(s), s)):
            netted = [(op, pin_net.get(f"{ref}:{op}")) for op in by_num[num]]
            netted = [(op, n) for op, n in netted if n is not None and n != "?"]
            if num not in pins:
                if netted:
                    warnings.append(f"{lib_id} 缺少引脚号 {num}"
                                    f"（{ref}:{', '.join(op for op, _ in netted)}）")
                continue
            px, py, _ang = pins[num]
            # 文件坐标 -> 画布坐标：canvas = (ax + px, ay - py)
            cx, cy = ax + px, ay - py
            if not netted:
                # KiCad 惯例：有意悬空的引脚打 no_connect 标记，否则官方 ERC
                # 报 pin_not_connected（错误示范电路 led_no_limit 实测）。
                # 应用层语义（重复位号等）由自身 ERC 报告负责，两者分层。
                noconn_lines.append(
                    f'  (no_connect (at {cx:g} {cy:g}) '
                    f'(uuid "{_uid(name, "noconn", idx, ref, num)}"))')
                continue
            # 引脚朝外方向（画布系）：取引脚偏移的主导轴向；
            # 文件系 Y+ 为屏幕上方，映射到画布系 Y 向下为正 -> 取反
            if abs(px) >= abs(py) and px != 0:
                dx, dy = math.copysign(1.0, px), 0.0
            elif py != 0:
                dx, dy = 0.0, -math.copysign(1.0, py)
            else:
                dx, dy = 1.0, 0.0
            ex, ey = cx + dx * STUB, cy + dy * STUB
            wire_lines.append(
                f'  (wire (pts (xy {cx:g} {cy:g}) (xy {ex:g} {ey:g})) '
                f'(stroke (width 0) (type default)) (uuid "{_uid(name, "wire", idx, ref, num)}"))'
            )
            just = "right" if dx < 0 else "left"
            for op, net in netted:
                label_lines.append(
                    f'  (label "{_esc(net)}" (at {ex:g} {ey:g} 0) '
                    f'(effects (font (size {FONT_SZ} {FONT_SZ})) (justify {just} bottom)) '
                    f'(uuid "{_uid(name, "label", idx, ref, num, op)}"))'
                )
                if net in flag_nets:
                    flag_points.setdefault(net, (ex, ey))
                manifest[f"{ref}:{op}"] = net

    # 电源标志：KiCad 规则“输入电源引脚须由输出电源引脚驱动”——本导出方案
    # 连通性全部由标签承载、引脚多为 passive，锚点网络（GND/VCC）与含真实
    # 供电源的网络（如电池正极直供芯片）都缺 power_out 驱动源，官方 ERC 报
    # power_pin_not_driven（ne555_blink / 电池直供双死结实测）。按 KiCad
    # 惯例给 flag_nets 中每个网络挂一枚 PWR_FLAG（power_out 引脚，官方
    # power 库同款语义），挂点取该网络首个短走线端点（与标签同点，连成一体）。
    for i, (net, (fx, fy)) in enumerate(sorted(flag_points.items())):
        sref = f"#FLG{i + 1:02d}"
        symbol_blocks.append(
            f'  (symbol (lib_id "CircuitTutor:PWR_FLAG") (at {fx:g} {fy:g} 0) (unit 1)\n'
            f'    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n'
            f'    (uuid "{_uid(name, "flag", i)}")\n'
            f'    (property "Reference" "{sref}" (at {fx + 2.54:g} {fy - 2.54:g} 0)\n'
            f'      (effects (font (size {FONT_SZ} {FONT_SZ})) (hide yes))\n'
            f'    )\n'
            f'    (property "Value" "PWR_FLAG" (at {fx:g} {fy - 5.08:g} 0)\n'
            f'      (effects (font (size {FONT_SZ} {FONT_SZ})))\n'
            f'    )\n'
            f'    (pin "1" (uuid "{_uid(name, "fpin", i)}"))\n'
            f'    (instances (project "zhihui" (path "/{_uid(name, "sheet")}" '
            f'(reference "{sref}") (unit 1))))\n'
            f'  )'
        )

    lines += wire_lines
    lines += label_lines
    lines += noconn_lines
    lines += symbol_blocks

    ap('  (sheet_instances')
    ap('    (path "/" (page "1"))')
    ap('  )')
    ap(')')

    text = "\n".join(lines) + "\n"
    return (text, manifest, warnings) if collect_manifest else text


# =====================================================================
# 配套工程文件：符号库 + sym-lib-table + .kicad_pro
# 真机 KiCad 9.0.8 实测：仅有内嵌符号的 .kicad_sch 在 ERC 中会报
# lib_symbol_issues（工程配置不含符号库）。补齐三件套后：
#   1) 官方 ERC 归零；2) 符号可在符号编辑器中直接编辑（真“完全可编辑”）；
#   3) 右键“从库中更新符号”可用。
# =====================================================================

LIB_NAME = "CircuitTutor"


def build_symbol_lib_text(circuit: dict | None = None) -> str:
    """全部符号定义打包为独立库文件（.kicad_sym）。

    库文件内符号名不带库名前缀（ sch 的 lib_symbols 节才带）。"""
    symbol_defs = build_symbol_defs()
    if circuit:
        for part in circuit.get("parts", []):
            if part.get("type") == "i2c_header":
                lib_id, _, definition = _i2c_header_layout(part)
                if definition:
                    symbol_defs[lib_id] = definition
    defs = "\n".join(
        d.replace(f'(symbol "{lib_id}"', f'(symbol "{lib_id.split(":", 1)[1]}"', 1)
        for lib_id, d in symbol_defs.items())
    return (f'(kicad_symbol_lib (version {SCH_VERSION}) (generator "{GENERATOR}")\n'
            + defs + "\n)\n")


SYM_LIB_TABLE_TEXT = (
    "(sym_lib_table\n"
    "  (version 7)\n"
    f'  (lib (name "{LIB_NAME}")(type "KiCad")'
    f'(uri "${{KIPRJMOD}}/{LIB_NAME}.kicad_sym")(options "")'
    '(descr "智绘电路 AI 生成符号库"))\n'
    ")\n"
)

# fp-lib-table：让官方 ERC 的 footprint_link_issues 能解析到系统封装库。
# 用 KiCad 内置环境变量（安装时自动设置），不写死绝对路径。
# KICAD8/9_FOOTPRINT_DIR 指向 share/kicad/footprints。
_FP_LIBS = [
    "Resistor_THT", "Resistor_SMD", "Capacitor_THT", "Capacitor_SMD",
    "Package_DIP", "Package_TO_SOT_THT", "Package_TO_SOT_SMD",
    "LED_THT", "LED_SMD", "Diode_THT", "Diode_SMD",
    "Inductor_THT", "Inductor_SMD", "Crystal",
    "Button_Switch_THT", "Button_Switch_SMD",
    "Connector_PinHeader_2.54mm", "Connector_BarrelJack",
    "Connector_USB", "Connector_Coaxial", "Connector_JST",
    "TerminalBlock", "TerminalBlock_Phoenix",
    "Battery", "Buzzer_Beeper", "Fuse", "Relay_THT",
    "Potentiometer_THT", "Transformer_THT", "Varistor",
    "Package_SIP", "RF_Module", "Display_7Segment", "OptoDevice",
    "MountingHole",
]


def _detect_footprint_dir() -> str:
    """探测本机 KiCad 官方封装库目录；找不到返回空串。"""
    import os
    from pathlib import Path as _P
    from kicad.cli_runner import find_kicad_cli
    explicit = os.environ.get("KICAD_FOOTPRINT_DIR", "")
    if explicit and _P(explicit).is_dir():
        return explicit.replace("\\", "/")
    # 1) kicad-cli 所在目录的上级/share/kicad/footprints
    cli = find_kicad_cli()
    if cli:
        # .../bin/kicad-cli.exe -> .../share/kicad/footprints
        root = _P(cli).parent.parent
        fp = root / "share" / "kicad" / "footprints"
        if fp.is_dir():
            return str(fp).replace("\\", "/")
    # 2) 常见安装路径
    for cand in (
        r"D:\KICAD\share\kicad\footprints",
        r"C:\Program Files\KiCad\9.0\share\kicad\footprints",
        r"C:\Program Files\KiCad\8.0\share\kicad\footprints",
        "/usr/share/kicad/footprints",
        "/usr/local/share/kicad/footprints",
    ):
        if _P(cand).is_dir():
            return cand.replace("\\", "/")
    return ""


def _fp_lib_table_text(project_libs: list[str] | None = None) -> str:
    fp_dir = _detect_footprint_dir()
    lines = ["(fp_lib_table", "  (version 7)"]
    if fp_dir:
        from pathlib import Path
        libraries = sorted(path.stem for path in Path(fp_dir).glob("*.pretty") if path.is_dir())
    else:
        libraries = _FP_LIBS
    for lib in libraries:
        if fp_dir:
            uri = f"{fp_dir}/{lib}.pretty"
        else:
            uri = f"${{KICAD8_FOOTPRINT_DIR}}/{lib}.pretty"
        lines.append(
            f'  (lib (name "{lib}")(type "KiCad")'
            f'(uri "{uri}")(options "")'
            f'(descr "{lib}"))'
        )
    # 工程内复现封装库：随导出目录一起走，用 ${KIPRJMOD} 而不是绝对路径，
    # 否则整包换机器后官方 ERC 会报 footprint_link_issues。
    local = sorted(set(project_libs or []) - set(libraries))
    for lib in local:
        lines.append(
            f'  (lib (name "{lib}")(type "KiCad")'
            f'(uri "${{KIPRJMOD}}/{lib}.pretty")(options "")'
            f'(descr "复现自外部源封装，仅焊盘铜几何"))'
        )
    lines.append(")")
    return "\n".join(lines) + "\n"


def kicad_pro_text(name: str, root_uuid: str, stem: str | None = None) -> str:
    """最小 .kicad_pro（缺失键由 KiCad 取默认值）。

    meta.filename 必须与磁盘上的工程文件名一致，且优先 ASCII stem，
    否则部分 KiCad 版本打开工程后图纸视图为空。
    """
    stem = stem or safe_project_stem(name)
    pro = {
        "board": {},
        "boards": [],
        "cvpcb": {"equivalence_files": []},
        "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
        "meta": {"filename": f"{stem}.kicad_pro", "version": 3},
        "net_settings": {},
        "pcbnew": {},
        "schematic": {
            "annotate_start_num": 0,
            "legacy_lib_dir": "",
            "legacy_lib_list": [],
        },
        "sheets": [[root_uuid, "Root"]],
        "text_variables": {},
    }
    return json.dumps(pro, ensure_ascii=False, indent=2) + "\n"


def _write_reproduced_footprints(out_dir: Path, circuit: dict | None) -> list[str]:
    """把 circuit 内携带的复现封装写成工程内 .pretty 库，返回库名列表。

    条目缺字段或封装文本与声明的 Library:Name 不一致时直接报错：静默跳过会
    让原理图指向一个不存在的封装，而官方 ERC 只会给出难定位的链接告警。
    """
    entries = (circuit or {}).get("reproduced_footprints") or []
    if not entries:
        return []
    from core.eagle_footprint_repro import write_repro_library
    normalized = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"reproduced_footprints[{index}] 必须是对象")
        for key in ("library", "footprint_id", "source_library", "source_package", "text"):
            if not str(entry.get(key) or "").strip():
                raise ValueError(f"reproduced_footprints[{index}] 缺少 {key}")
        library, name = entry["footprint_id"].split(":", 1)
        if library != entry["library"]:
            raise ValueError(
                f"reproduced_footprints[{index}] 的 footprint_id 库名与 library 不一致")
        if f'(footprint "{name}"' not in entry["text"]:
            raise ValueError(
                f"reproduced_footprints[{index}] 的封装文本与 {entry['footprint_id']} 不一致")
        normalized.append(entry)
    write_repro_library(out_dir, normalized)
    return sorted({entry["library"] for entry in normalized})


def write_project_kit(out_dir: Path, stem: str = "circuit", title: str = "",
                      circuit: dict | None = None) -> None:
    """向输出目录写入共享符号库、lib-table、打开说明/一键脚本（幂等）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    project_libs = _write_reproduced_footprints(out_dir, circuit)
    (out_dir / f"{LIB_NAME}.kicad_sym").write_text(
        build_symbol_lib_text(circuit), encoding="utf-8")
    (out_dir / "sym-lib-table").write_text(SYM_LIB_TABLE_TEXT, encoding="utf-8")
    (out_dir / "fp-lib-table").write_text(_fp_lib_table_text(project_libs), encoding="utf-8")
    (out_dir / HOW_TO_OPEN_NAME).write_text(
        how_to_open_text(stem, title), encoding="utf-8")
    (out_dir / OPEN_BAT_NAME).write_text(
        open_in_kicad_bat_text(stem), encoding="utf-8", newline="")
    (out_dir / ZIP_README_NAME).write_text(
        zip_readme_text(stem, title), encoding="utf-8")


def _try_svg_preview(sch_path: Path) -> bool:
    """若本机有 kicad-cli，导出 SVG 预览便于「不开 GUI 也能看见图」。"""
    import shutil
    import subprocess
    cli = shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")
    if not cli:
        for cand in (r"D:\KICAD\bin\kicad-cli.exe",
                     r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
                     r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe"):
            if Path(cand).is_file():
                cli = cand
                break
    if not cli:
        return False
    out_dir = sch_path.parent / "preview"
    try:
        out_dir.mkdir(exist_ok=True)
        subprocess.run(
            [cli, "sch", "export", "svg", str(sch_path), "-o", str(out_dir)],
            check=False, capture_output=True, timeout=60,
        )
        return any(out_dir.glob("*.svg"))
    except Exception:
        return False


def export_to_dir(circuit: dict, name: str, out_dir, kb: dict | None = None,
                  erc_report=None):
    """导出单个电路的完整工程到 out_dir，返回 (sch路径, 警告列表)。

    产物（工程文件名使用 ASCII stem，避免中文路径导致 KiCad 打开空白）：
      - {stem}.kicad_sch / {stem}.kicad_pro
      - CircuitTutor.kicad_sym + sym-lib-table + fp-lib-table
      - HOW_TO_OPEN.txt（正确打开方式：解压后「打开工程」而非「导入」）
      - preview/*.svg（本机存在 kicad-cli 时）
    标题仍写在原理图 title_block 中，不依赖文件名。
    官方 ERC 的 lib_symbol_issues 检查依赖工程上下文，裸 .kicad_sch 会误报。
    """
    text, _mani, warns = export_kicad_sch(circuit, name, kb=kb,
                                          erc_report=erc_report,
                                          collect_manifest=True)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_project_stem(name)
    write_project_kit(out_dir, stem=stem, title=str(name or stem), circuit=circuit)
    fp = out_dir / f"{stem}.kicad_sch"
    fp.write_text(text, encoding="utf-8")
    root_uuid = text.split('(uuid "', 1)[1].split('"', 1)[0]
    (out_dir / f"{stem}.kicad_pro").write_text(
        kicad_pro_text(name, root_uuid, stem=stem), encoding="utf-8")
    _try_svg_preview(fp)
    # 把 CLI 预览复制到工程根，方便「还没打开 KiCad」时先确认不是空包
    preview_src = out_dir / "preview" / f"{stem}.svg"
    if preview_src.is_file():
        try:
            (out_dir / PREVIEW_SVG_NAME).write_bytes(preview_src.read_bytes())
        except OSError:
            pass
    return fp, warns


# =====================================================================
# CLI
# =====================================================================

def slug(s: str, max_len: int | None = None) -> str:
    """文件名安全的 slug：空白折叠为下划线、可选截断、非法字符替换。
    供本模块 CLI 与 ai2kicad 输出目录命名共用（原先两处各写一份）。
    注意：本函数允许 Unicode（反斜杠 w 含中文），适合目录名；KiCad 工程
    文件名请用 safe_project_stem()，避免中文路径导致打开空白。"""
    s = re.sub(r"\s+", "_", s.strip())
    if max_len:
        s = s[:max_len]
    return re.sub(r"[^\w.-]+", "_", s, flags=re.UNICODE) or "circuit"


def _slug(s: str) -> str:   # 兼容旧内部调用：等价于 slug(s)
    return slug(s)


def safe_project_stem(name: str) -> str:
    """KiCad 工程文件主名：强制 ASCII，保证解压/双击可打开。

    社区与 KiCad 官方工作流是「打开 .kicad_pro」，不是「文件→导入」。
    中文/特殊字符文件名在部分环境会让 Eeschema 打开后图纸为空。
    纯中文标题会退化为 circuit_<md5前8位>，ASCII 字母数字尽量保留。
    """
    import hashlib
    s = str(name or "circuit").strip() or "circuit"
    out = []
    for ch in s:
        if ch.isascii() and ch.isalnum():
            out.append(ch)
        elif ch.isascii() and ch in "._-":
            out.append(ch)
        else:
            out.append("_")
    stem = re.sub(r"_+", "_", "".join(out)).strip("._-")
    if not stem or not re.search(r"[A-Za-z0-9]", stem):
        stem = "circuit_" + hashlib.md5(s.encode("utf-8")).hexdigest()[:8]
    return stem[:40] or "circuit"


HOW_TO_OPEN_NAME = "HOW_TO_OPEN.txt"
OPEN_BAT_NAME = "OPEN_IN_KICAD.bat"
ZIP_README_NAME = "00-请先读我.txt"
PREVIEW_SVG_NAME = "PREVIEW.svg"


def open_in_kicad_bat_text(stem: str) -> str:
    """生成 Windows 一键打开脚本：自动寻找 KiCad 并打开 .kicad_pro。

    对齐 GitHub 生态共识（dshills/KiCadAI、mikelenko/SolverSCH 等）：
    交付物是「完整工程目录」，正确入口是 Open Project，不是 File→Import。
    """
    pro = f"{stem}.kicad_pro"
    return (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "setlocal\r\n"
        "title Open KiCad Project\r\n"
        "cd /d \"%~dp0\"\r\n"
        f"set \"PRO={pro}\"\r\n"
        "if not exist \"%PRO%\" (\r\n"
        "  echo [ERROR] Missing %PRO% in this folder.\r\n"
        "  echo Please extract the whole zip first, then run this bat again.\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "set \"KICAD_EXE=\"\r\n"
        "if exist \"F:\\智绘电路工具链\\kicad\\bin\\kicad.exe\" set \"KICAD_EXE=F:\\智绘电路工具链\\kicad\\bin\\kicad.exe\"\r\n"
        "where kicad.exe >nul 2>nul && for /f \"delims=\" %%I in ('where kicad.exe') do if not defined KICAD_EXE set \"KICAD_EXE=%%I\"\r\n"
        "if not defined KICAD_EXE if exist \"%ProgramFiles%\\KiCad\\9.0\\bin\\kicad.exe\" set \"KICAD_EXE=%ProgramFiles%\\KiCad\\9.0\\bin\\kicad.exe\"\r\n"
        "if not defined KICAD_EXE if exist \"%ProgramFiles%\\KiCad\\8.0\\bin\\kicad.exe\" set \"KICAD_EXE=%ProgramFiles%\\KiCad\\8.0\\bin\\kicad.exe\"\r\n"
        "if not defined KICAD_EXE if exist \"%ProgramFiles%\\KiCad\\7.0\\bin\\kicad.exe\" set \"KICAD_EXE=%ProgramFiles%\\KiCad\\7.0\\bin\\kicad.exe\"\r\n"
        "if not defined KICAD_EXE if exist \"D:\\KICAD\\bin\\kicad.exe\" set \"KICAD_EXE=D:\\KICAD\\bin\\kicad.exe\"\r\n"
        "if not defined KICAD_EXE if exist \"C:\\KICAD\\bin\\kicad.exe\" set \"KICAD_EXE=C:\\KICAD\\bin\\kicad.exe\"\r\n"
        "if not defined KICAD_EXE (\r\n"
        "  echo [ERROR] KiCad not found. Install KiCad 8/9, or open %PRO% manually.\r\n"
        "  echo Do NOT use File - Import. Use Project Manager - Open Project.\r\n"
        "  pause\r\n"
        "  exit /b 2\r\n"
        ")\r\n"
        "echo Opening %PRO% with %KICAD_EXE%\r\n"
        "echo If the sheet looks empty, press Home in Eeschema to zoom to fit.\r\n"
        "start \"\" \"%KICAD_EXE%\" \"%cd%\\%PRO%\"\r\n"
        "exit /b 0\r\n"
    )


def zip_readme_text(stem: str, title: str = "") -> str:
    title = title or stem
    return (
        "【请先读我】智绘电路 KiCad 导出包\n"
        "================================\n"
        f"电路：{title}\n"
        f"工程主名：{stem}\n"
        "\n"
        "解压后请这样做（不要用「文件→导入」）\n"
        "--------------------------------------\n"
        f"1. 解压 zip，进入文件夹 {stem}/\n"
        f"2. 双击 OPEN_IN_KICAD.bat（推荐），或\n"
        f"   打开 KiCad → 打开工程 → 选 {stem}.kicad_pro\n"
        f"3. 在项目管理器双击 {stem}.kicad_sch\n"
        "4. 若图纸空白：按 Home / 适合页面；也可先看 PREVIEW.svg\n"
        "\n"
        "为什么「导入」会啥都没有？\n"
        "--------------------------\n"
        "KiCad 的「导入」用于外部格式转换，不是打开本工程。\n"
        "本包已是标准 KiCad 工程（.kicad_pro + .kicad_sch + 符号库），\n"
        "GitHub 同类项目（dshills/KiCadAI、mikelenko/SolverSCH 等）同样\n"
        "交付完整工程目录，要求用户 Open Project，而不是 Import。\n"
        "\n"
        "详见同目录 HOW_TO_OPEN.txt\n"
    )


def how_to_open_text(stem: str, title: str = "") -> str:
    title = title or stem
    return (
        "智绘电路 CircuitTutor · KiCad 打开说明\n"
        "====================================\n"
        f"电路标题：{title}\n"
        f"工程文件：{stem}.kicad_pro\n"
        f"原理图：  {stem}.kicad_sch\n"
        f"一键脚本：{OPEN_BAT_NAME}\n"
        f"预览图：  {PREVIEW_SVG_NAME}（若已生成）\n"
        "\n"
        "正确打开方式（社区/KiCad 标准工作流）\n"
        "------------------------------------\n"
        "0. 【推荐】解压后双击本目录的 OPEN_IN_KICAD.bat，自动打开工程。\n"
        "1. 或：先把整个 zip 完整解压到本地文件夹（不要在压缩包预览里双击）。\n"
        "2. 打开 KiCad 8 或 9 → 项目管理器 →「打开工程 / Open Project」。\n"
        f"3. 选择本目录下的 **{stem}.kicad_pro**（不是 .kicad_sch/.kicad_sym/.zip）。\n"
        f"4. 在项目管理器中双击同名原理图 **{stem}.kicad_sch** 进入 Eeschema。\n"
        "5. 若视野空白，按 Home /「适合页面」缩放到全图；元件与网络标签应可见。\n"
        "6. 也可先打开 PREVIEW.svg 确认电路内容确实已导出。\n"
        "\n"
        "请不要这样打开（会看起来像「啥都没有」）\n"
        "--------------------------------------\n"
        "- 不要用「文件 → 导入 / File → Import」选择 zip / sch / sym。\n"
        "  导入是外部格式转换入口，不是打开本工程；空白多半是选错了入口。\n"
        "- 不要只双击 .kicad_sym（符号库），也不要只打开 .net。\n"
        "- 不要在未解压的资源管理器压缩包预览中点文件。\n"
        "- 不要把 .kicad_pcb 当成原理图（PCB 是另一张图）。\n"
        "\n"
        "仍然空白时\n"
        "----------\n"
        "1. 确认解压后同目录存在：*.kicad_pro、*.kicad_sch、"
        "CircuitTutor.kicad_sym、sym-lib-table、OPEN_IN_KICAD.bat。\n"
        "2. 用官方 CLI 预览（不需要点开 GUI）：\n"
        f"   kicad-cli sch export svg {stem}.kicad_sch -o preview\n"
        "3. 本包符号已内嵌在 .kicad_sch 的 lib_symbols 中，一般无需再配置全局库。\n"
        "4. 参考 GitHub：dshills/KiCadAI 直接写 .kicad_pro/.kicad_sch/.kicad_pcb\n"
        "   完整工程；mikelenko/SolverSCH / jkriege2/kicad-symbol-generator 同理——\n"
        "   正确用法是打开生成的工程，而不是「导入」。\n"
        "\n"
        "由 智绘电路 自动生成，请勿手工改文件名与同名 pro/sch 配对关系。\n"
    )


def main(argv) -> int:
    argv = list(argv)
    out_dir = Path("kicad_export")
    if "--out" in argv:
        i = argv.index("--out")
        if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
            print("错误：--out 需要跟一个输出目录路径")
            return 2
        out_dir = Path(argv[i + 1])
        del argv[i:i + 2]
    from_stdin = "--stdin" in argv
    args = [a for a in argv if not a.startswith("--")]
    unknown = [a for a in argv if a.startswith("--") and a not in ("--stdin", "--all")]
    if unknown:
        print(f"错误：未知参数 {' '.join(unknown)}（支持：--all / --stdin / --out DIR）")
        return 2
    # 内置模板数据在项目根 data/ 目录（kicad/ 的上级）
    proj_dir = Path(__file__).resolve().parent.parent / "data"

    if from_stdin:
        try:
            circuit = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            print(f"错误：stdin 不是合法 JSON：{e}")
            return 2
        if not isinstance(circuit, dict) or not isinstance(circuit.get("parts"), list):
            print("错误：stdin JSON 缺少 parts 数组")
            return 2
        jobs = [(_slug(circuit.get("title") or circuit.get("name") or "circuit", ), circuit)]
    else:
        demos = json.loads((proj_dir / "circuit_templates.json").read_text(encoding="utf-8"))
        if not args or "--all" in argv:
            jobs = [(k, v) for k, v in demos.items()]
        else:
            jobs = []
            for k in args:
                if k not in demos:
                    print(f"未找到 demo：{k}（可选：{', '.join(demos)}）")
                    return 2
                jobs.append((k, demos[k]))

    kb = catalog.build_erc_kb()
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, circuit in jobs:
        fp, warns = export_to_dir(circuit, name, out_dir, kb=kb)
        note = f"（警告: {'; '.join(warns)}）" if warns else ""
        print(f"已导出 {fp}{note}")
    print(f"共 {len(jobs)} 个原理图，输出目录：{out_dir.resolve()}")
    print(f"同时生成 {LIB_NAME}.kicad_sym 符号库、sym-lib-table 与各 .kicad_pro 配套工程。")
    print("在 KiCad 中：双击 .kicad_pro 打开工程（或文件 -> 打开 .kicad_sch，"
          "工程不存在时选择“仍要打开”）。")
    return 0
