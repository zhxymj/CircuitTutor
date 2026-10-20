# -*- coding: utf-8 -*-
"""One bounded internal ATmega328P-PU + I2C/ISP engineering example.

Explicit routes and official filled copper planes, not a general autorouter.
No claim of firmware, clock-fuse, I2C waveform or assembled-board validation.
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path

from core.pcb_examples import HEADER_SOURCE, RESISTOR_SOURCE, LIBRARY_SOURCE
from core.pcb_real import (build_pcb_candidate, parse_sexpr, dump_sexpr, _children, _one,
                           _a, _num, _uuid, _value, _nets)
from core.pcb_manufacturing import _command, run_manufacturing_pipeline, write_fp_lib_table
from core.pcb_source_schematic import build_real_schematic
from core.validation_gate import validate_candidate

MCU_SOURCE = "https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATmega48A-PA-88A-PA-168A-PA-328-P-DS-DS40002061B.pdf"
HARDWARE_SOURCE = "https://ww1.microchip.com/downloads/en/appnotes/atmel-2521-avr-hardware-design-considerations_applicationnote_avr042.pdf"
CAP_SOURCE = "https://search.murata.co.jp/Ceramy/image/img/A01X/G101/ENG/GRM21BR71H104KA01-01.pdf"
RESET_SOURCE = "https://www.yageogroup.com/component-documentation/download/specsheet/RC0805FR-0710KL"
ISP_SOURCE = "https://www.samtec.com/products/tsw-106-07-g-s"
# SHT4x 引脚序（1=SDA 2=SCL 3=VDD 4=VSS）与封装配对取自 KiCad 官方符号库
# Sensor_Humidity.kicad_sym 的 SHT4x 条目，其 Datasheet 属性即下面这个地址。
SENSOR_SOURCE = "https://sensirion.com/media/documents/33FD6951/624C4357/Datasheet_SHT4x.pdf"
SENSOR_FOOTPRINT = "Sensor_Humidity:Sensirion_DFN-4_1.5x1.5mm_P0.8mm_SHT4x_NoCentralPad"


def active_circuit() -> dict:
    pin_map = {"RESET": "1", "PD0": "2", "PD1": "3", "PD2": "4", "PD3": "5", "PD4": "6",
               "VCC": "7", "GND": "8", "XTAL1": "9", "XTAL2": "10", "PD5": "11", "PD6": "12",
               "PD7": "13", "PB0": "14", "PB1": "15", "PB2": "16", "PB3": "17", "PB4": "18",
               "PB5": "19", "AVCC": "20", "AREF": "21", "GND2": "22", "PC0": "23", "PC1": "24",
               "PC2": "25", "PC3": "26", "PC4": "27", "PC5": "28"}
    electrical = {p: "bidirectional" for p in pin_map}
    electrical.update(VCC="power_in", AVCC="power_in", GND="power_in", GND2="power_in",
                      RESET="input", XTAL1="input", XTAL2="output", AREF="passive")
    parts = []
    def part(ref, kind, value, mpn, manufacturer, footprint, pin_map, source, x, y, pin_types=None):
        component = {"mpn": mpn, "manufacturer": manufacturer, "footprint": footprint,
                     "datasheet": source, "pin_map_source": source, "pin_map": pin_map}
        if pin_types:
            component["pin_types"] = pin_types
        parts.append({"id": ref, "type": kind, "attrs": {"value": value}, "component": component,
                      "pcb": {"x_mm": x, "y_mm": y, "rotation_deg": 0, "side": "front"}})
    part("U1", "atmega328", "ATmega328P-PU", "ATMEGA328P-PU", "Microchip", "Package_DIP:DIP-28_W7.62mm",
         pin_map, MCU_SOURCE, 22, 8, electrical)
    part("J1", "i2c_header", "I2C GND VCC SDA SCL", "TSW-104-07-G-S", "Samtec",
         "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
         {"GND": "1", "VCC": "2", "SDA": "3", "SCL": "4"}, HEADER_SOURCE, 40, 8)
    part("J2", "spi_header", "CUSTOM ISP MISO VCC SCK MOSI RESET GND", "TSW-106-07-G-S", "Samtec",
         "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
         {"MISO": "1", "VCC": "2", "SCK": "3", "MOSI": "4", "CS": "5", "GND": "6"}, ISP_SOURCE, 40, 33.4)
    for ref, value, mpn, x, y, source in [("R1", "4.7k", "RC0805FR-074K7L", 36, 14.3, RESISTOR_SOURCE),
                                        ("R2", "4.7k", "RC0805FR-074K7L", 36, 19, RESISTOR_SOURCE),
                                        ("R3", "10k", "RC0805FR-0710KL", 18, 8, RESET_SOURCE)]:
        part(ref, "resistor", value, mpn, "YAGEO", "Resistor_SMD:R_0805_2012Metric", {"1": "1", "2": "2"}, source, x, y)
    for ref, x, y in [("C1", 18, 24.51), ("C2", 34, 28.6), ("C3", 34, 24.9), ("C4", 45, 10.54)]:
        part(ref, "capacitor_ceramic", "100nF", "GRM21BR71H104KA01L", "Murata",
             "Capacitor_SMD:C_0805_2012Metric", {"+": "1", "-": "2"}, CAP_SOURCE, x, y)
    parts += [{"id": "GND1", "type": "gnd", "attrs": {"value": "external common ground"}},
              {"id": "VCC1", "type": "vcc", "attrs": {"value": "5V"}}]
    groups = [["GND1:1", "J1:GND", "U1:GND", "U1:GND2", "C1:-", "C2:-", "C3:-", "C4:-", "J2:GND"],
              ["VCC1:1", "J1:VCC", "U1:VCC", "U1:AVCC", "C1:+", "C2:+", "C4:+", "R1:1", "R2:1", "R3:1", "J2:VCC"],
              ["U1:PC4", "R1:2", "J1:SDA"], ["U1:PC5", "R2:2", "J1:SCL"],
              ["U1:AREF", "C3:+"], ["U1:RESET", "R3:2", "J2:CS"],
              ["U1:PB3", "J2:MOSI"], ["U1:PB4", "J2:MISO"], ["U1:PB5", "J2:SCK"]]
    return {"version": 2, "title": "Internal ATmega328P-PU 5V I2C + custom ISP candidate",
            "parts": parts, "connections": [[a, b] for g in groups for a, b in zip(g, g[1:])],
            "board": {"width_mm": 50, "height_mm": 55, "layers": 2,
                      "min_clearance_mm": 0.2, "min_trace_width_mm": 0.2},
            "voltage_domains": [{"id": "logic", "nominal_v": 5, "min_v": 4.75, "max_v": 5.25}],
            "provenance": {"origin": "manual", "source_id": "internal-active-atmega328p-pu-01"},
            "engineering_example": {"classification": "internal_engineering_example", "review_required": True,
                "topology": "active_atmega328p_internal_rc_i2c_custom_isp", "independent_external_sample": False,
                "sources": [MCU_SOURCE, HARDWARE_SOURCE, CAP_SOURCE, RESET_SOURCE, HEADER_SOURCE, ISP_SOURCE, RESISTOR_SOURCE, LIBRARY_SOURCE],
                "clock": "Internal calibrated 8MHz RC; factory CKDIV8 gives 1MHz CPU. No external crystal populated; fuse/firmware state must be verified with ISP.",
                "analog": "AVCC connected to VCC with 100nF decoupling; AREF only decoupled to GND, no external driven reference. ADC precision/filter not validated.",
                "pin_assignment": "J1 pads GND,VCC,SDA,SCL; J2 single-row custom ISP pads MISO,VCC,SCK,MOSI,RESET,GND. Not the standard 2x3 ISP connector.",
                "assumptions": "One regulated external 5V source, external 5V I2C slave, matching custom cables, 4.7k pullups; no firmware/waveform/assembled board validation.",
                "limitations": ["ATmega328P is NRND per current manufacturer page", "No ESD/protection circuit or reset pushbutton", "No firmware or fuse programming performed", "No physical assembly or I2C timing validation"]}}


def active_circuit_3v3_sensor() -> dict:
    """3.3V ATmega328P + 板载 SHT4x I2C 温湿度传感器的目标场景候选。

    与 5V 例子的差别正是验收缺口：板载真实 I2C 传感器、显式 3.3V 上拉、
    传感器独立去耦。供电域只声明 3.3V，不把任何名称推断成电压。
    """
    pin_map = {"RESET": "1", "PD0": "2", "PD1": "3", "PD2": "4", "PD3": "5", "PD4": "6",
               "VCC": "7", "GND": "8", "XTAL1": "9", "XTAL2": "10", "PD5": "11", "PD6": "12",
               "PD7": "13", "PB0": "14", "PB1": "15", "PB2": "16", "PB3": "17", "PB4": "18",
               "PB5": "19", "AVCC": "20", "AREF": "21", "GND2": "22", "PC0": "23", "PC1": "24",
               "PC2": "25", "PC3": "26", "PC4": "27", "PC5": "28"}
    electrical = {p: "bidirectional" for p in pin_map}
    electrical.update(VCC="power_in", AVCC="power_in", GND="power_in", GND2="power_in",
                      RESET="input", XTAL1="input", XTAL2="output", AREF="passive")
    parts = []

    def part(ref, kind, value, mpn, manufacturer, footprint, pin_map, source, x, y, pin_types=None):
        component = {"mpn": mpn, "manufacturer": manufacturer, "footprint": footprint,
                     "datasheet": source, "pin_map_source": source, "pin_map": pin_map}
        if pin_types:
            component["pin_types"] = pin_types
        parts.append({"id": ref, "type": kind, "attrs": {"value": value}, "component": component,
                      "pcb": {"x_mm": x, "y_mm": y, "rotation_deg": 0, "side": "front"}})

    part("U1", "atmega328", "ATmega328P-PU", "ATMEGA328P-PU", "Microchip",
         "Package_DIP:DIP-28_W7.62mm", pin_map, MCU_SOURCE, 14, 8, electrical)
    part("U2", "i2c_rh_sensor", "SHT41-AD1B", "SHT41-AD1B-R2", "Sensirion", SENSOR_FOOTPRINT,
         {"SDA": "1", "SCL": "2", "VDD": "3", "VSS": "4"}, SENSOR_SOURCE, 33, 12,
         {"SDA": "bidirectional", "SCL": "input", "VDD": "power_in", "VSS": "power_in"})
    part("J1", "i2c_header", "I2C GND VCC SDA SCL", "TSW-104-07-G-S", "Samtec",
         "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
         {"GND": "1", "VCC": "2", "SDA": "3", "SCL": "4"}, HEADER_SOURCE, 42, 8)
    part("J2", "spi_header", "CUSTOM ISP MISO VCC SCK MOSI RESET GND", "TSW-106-07-G-S", "Samtec",
         "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
         {"MISO": "1", "VCC": "2", "SCK": "3", "MOSI": "4", "CS": "5", "GND": "6"}, ISP_SOURCE, 42, 28)
    for ref, value, mpn, x, y, source in [("R1", "4.7k", "RC0805FR-074K7L", 28, 9, RESISTOR_SOURCE),
                                          ("R2", "4.7k", "RC0805FR-074K7L", 28, 14, RESISTOR_SOURCE),
                                          ("R3", "10k", "RC0805FR-0710KL", 10, 8, RESET_SOURCE)]:
        part(ref, "resistor", value, mpn, "YAGEO", "Resistor_SMD:R_0805_2012Metric",
             {"1": "1", "2": "2"}, source, x, y)
    for ref, x, y in [("C1", 10, 30), ("C2", 25, 30), ("C3", 25, 25), ("C4", 33, 18)]:
        part(ref, "capacitor_ceramic", "100nF", "GRM21BR71H104KA01L", "Murata",
             "Capacitor_SMD:C_0805_2012Metric", {"+": "1", "-": "2"}, CAP_SOURCE, x, y)
    parts += [{"id": "GND1", "type": "gnd", "attrs": {"value": "external common ground"}},
              {"id": "VCC1", "type": "vcc", "attrs": {"value": "3.3V"}}]
    groups = [["GND1:1", "J1:GND", "U1:GND", "U1:GND2", "U2:VSS", "C1:-", "C2:-", "C3:-", "C4:-", "J2:GND"],
              ["VCC1:1", "J1:VCC", "U1:VCC", "U1:AVCC", "U2:VDD", "C1:+", "C2:+", "C4:+",
               "R1:1", "R2:1", "R3:1", "J2:VCC"],
              ["U1:PC4", "R1:2", "U2:SDA", "J1:SDA"],
              ["U1:PC5", "R2:2", "U2:SCL", "J1:SCL"],
              ["U1:AREF", "C3:+"],
              ["U1:RESET", "R3:2", "J2:CS"],
              ["U1:PB3", "J2:MOSI"], ["U1:PB4", "J2:MISO"], ["U1:PB5", "J2:SCK"]]
    return {"version": 2, "title": "Internal ATmega328P-PU 3.3V I2C sensor candidate",
            "parts": parts, "connections": [[a, b] for g in groups for a, b in zip(g, g[1:])],
            "board": {"width_mm": 50, "height_mm": 55, "layers": 2,
                      "min_clearance_mm": 0.2, "min_trace_width_mm": 0.2},
            "voltage_domains": [{"id": "logic", "nominal_v": 3.3, "min_v": 3.0, "max_v": 3.6}],
            "provenance": {"origin": "manual", "source_id": "internal-active-atmega328p-sht4x-3v3-01"},
            "engineering_example": {"classification": "internal_engineering_example", "review_required": True,
                "topology": "active_atmega328p_3v3_onboard_i2c_rh_sensor",
                "independent_external_sample": False,
                "sources": [MCU_SOURCE, HARDWARE_SOURCE, CAP_SOURCE, RESET_SOURCE, HEADER_SOURCE,
                            ISP_SOURCE, RESISTOR_SOURCE, LIBRARY_SOURCE, SENSOR_SOURCE],
                "clock": "Internal calibrated 8MHz RC; factory CKDIV8 gives 1MHz CPU. No external crystal populated; fuse/firmware state must be verified with ISP.",
                "analog": "AVCC connected to VCC with 100nF decoupling; AREF only decoupled to GND, no external driven reference. ADC precision/filter not validated.",
                "sensor": ("On-board Sensirion SHT4x I2C temperature/humidity sensor; pin order 1=SDA 2=SCL "
                           "3=VDD 4=VSS taken from the KiCad official Sensor_Humidity symbol and its paired "
                           "Sensirion DFN-4 footprint. I2C address not declared: SHT4x has a fixed factory "
                           "address and this project does not guess addresses."),
                "pin_assignment": "J1 pads GND,VCC,SDA,SCL; J2 single-row custom ISP pads MISO,VCC,SCK,MOSI,RESET,GND. Not the standard 2x3 ISP connector.",
                "assumptions": "One regulated external 3.3V source; 4.7k on-board pull-ups to 3.3V; matching custom cables; no firmware/waveform/assembled board validation.",
                "limitations": ["ATmega328P is NRND per current manufacturer page",
                                "No ESD/protection circuit or reset pushbutton",
                                "No firmware or fuse programming performed",
                                "No physical assembly, I2C timing or sensor accuracy validation",
                                "Sensor VSS returns to the back ground plane through a single via"]}}


def explicit_active_routes(circuit: dict, candidate: dict) -> str:
    board = parse_sexpr(candidate["board_text"])
    coords = {}
    for fp in _children(board, "footprint"):
        ref = next(_value(p[2]) for p in _children(fp, "property") if _value(p[1]) == "Reference")
        ax, ay = [float(_value(x)) for x in _one(fp, "at")[1:3]]
        for pad in _children(fp, "pad"):
            x, y = [float(_value(x)) for x in _one(pad, "at")[1:3]]
            coords[ref, _value(pad[1])] = (ax + x, ay + y)
    parts = {p["id"]: p for p in circuit["parts"]}
    numbers, labels = _nets(circuit, parts)
    labels = dict(labels)
    tracks, vias = [], []
    def at(ref, logical):
        return coords[ref, parts[ref]["component"]["pin_map"][logical]]
    def path(endpoint, layer, points):
        number = numbers[tuple(endpoint.split(":"))]
        for start, end in zip(points, points[1:]):
            tracks.append((number, layer, start, end))
    def via(endpoint, point):
        vias.append((numbers[tuple(endpoint.split(":"))], point))
    # Signal paths are explicit, with crossings on different copper layers.
    path("U1:PC4", "F.Cu", [at("U1", "PC4"), at("J1", "SDA")])
    rp = at("R1", "2")
    ty = 10.54 + (rp[0] - 29.62) * (13.08 - 10.54) / (40 - 29.62)
    path("U1:PC4", "F.Cu", [rp, (rp[0], ty)])
    path("U1:PC5", "B.Cu", [at("U1", "PC5"), at("J1", "SCL")])
    path("U1:PC5", "F.Cu", [at("R2", "2"), (38, 19)])
    path("U1:PC5", "B.Cu", [(38, 19), at("J1", "SCL")])
    via("U1:PC5", (38, 19))
    path("U1:AREF", "F.Cu", [at("U1", "AREF"), at("C3", "+")])
    path("U1:RESET", "F.Cu", [at("R3", "2"), at("U1", "RESET")])
    path("U1:RESET", "B.Cu", [at("U1", "RESET"), (20, 6), (15, 6), (15, 44.8), (38.5, 44.8), at("J2", "CS")])
    path("U1:PB4", "F.Cu", [at("U1", "PB4"), at("J2", "MISO")])
    path("U1:PB5", "B.Cu", [at("U1", "PB5"), at("J2", "SCK")])
    path("U1:PB3", "F.Cu", [at("U1", "PB3"), at("J2", "MOSI")])
    # Surface ground pads join the actual filled back ground plane with vias.
    for ref, point in [("C1", (20.5, 25.7)), ("C2", (36.5, 28.6)), ("C3", (36.5, 24.9)), ("C4", (47, 12))]:
        path(ref + ":-", "F.Cu", [at(ref, "-"), point])
        via(ref + ":-", point)
    for index, (number, layer, start, end) in enumerate(tracks):
        board.append([_a("segment"), [_a("start"), *[_a(_num(n)) for n in start]],
                      [_a("end"), *[_a(_num(n)) for n in end]], [_a("width"), _a("0.25")],
                      [_a("layer"), _a(layer, True)], [_a("net"), _a(number)],
                      [_a("uuid"), _a(_uuid("active-track", index), True)]])
    for index, (number, point) in enumerate(vias):
        board.append([_a("via"), [_a("at"), *[_a(_num(n)) for n in point]], [_a("size"), _a("0.6")],
                      [_a("drill"), _a("0.3")], [_a("layers"), _a("F.Cu", True), _a("B.Cu", True)],
                      [_a("net"), _a(number)], [_a("uuid"), _a(_uuid("active-via", index), True)]])
    for logical, layer in [("VCC", "F.Cu"), ("GND", "B.Cu")]:
        number = numbers["J1", logical]
        zone = f'''(zone (net {number}) (net_name "{labels[number]}") (layer "{layer}")
          (uuid "{_uuid('active-plane', layer)}") (hatch edge 0.5)
          (connect_pads (clearance 0.2)) (min_thickness 0.25) (filled_areas_thickness no)
          (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
          (polygon (pts (xy 2 2) (xy 48 2) (xy 48 53) (xy 2 53))))'''
        board.append(parse_sexpr(zone))
    return dump_sexpr(board) + "\n"


def active_circuit_3v3() -> dict:
    """3.3V MCU + 板载 I2C 温湿度传感器候选：目标场景实测板。

    与 5V 例的差别：单一 3.3V 供电域、板载 SHT4x 裸片（不是模块排针）、
    显式 4.7k 上拉到 3.3V。传感器引脚序取自 KiCad 官方符号/封装配对，不另造。
    """
    pin_map = {"RESET": "1", "PD0": "2", "PD1": "3", "PD2": "4", "PD3": "5", "PD4": "6",
               "VCC": "7", "GND": "8", "XTAL1": "9", "XTAL2": "10", "PD5": "11", "PD6": "12",
               "PD7": "13", "PB0": "14", "PB1": "15", "PB2": "16", "PB3": "17", "PB4": "18",
               "PB5": "19", "AVCC": "20", "AREF": "21", "GND2": "22", "PC0": "23", "PC1": "24",
               "PC2": "25", "PC3": "26", "PC4": "27", "PC5": "28"}
    electrical = {p: "bidirectional" for p in pin_map}
    electrical.update(VCC="power_in", AVCC="power_in", GND="power_in", GND2="power_in",
                      RESET="input", XTAL1="input", XTAL2="output", AREF="passive")
    parts = []

    def part(ref, kind, value, mpn, manufacturer, footprint, pin_map, source, x, y, pin_types=None):
        component = {"mpn": mpn, "manufacturer": manufacturer, "footprint": footprint,
                     "datasheet": source, "pin_map_source": source, "pin_map": pin_map}
        if pin_types:
            component["pin_types"] = pin_types
        parts.append({"id": ref, "type": kind, "attrs": {"value": value}, "component": component,
                      "pcb": {"x_mm": x, "y_mm": y, "rotation_deg": 0, "side": "front"}})

    part("U1", "atmega328", "ATmega328P-PU", "ATMEGA328P-PU", "Microchip",
         "Package_DIP:DIP-28_W7.62mm", pin_map, MCU_SOURCE, 14, 8, electrical)
    part("U2", "i2c_rh_sensor", "SHT41-AD1B", "SHT41-AD1B-R2", "Sensirion", SENSOR_FOOTPRINT,
         {"SDA": "1", "SCL": "2", "VDD": "3", "VSS": "4"}, SENSOR_SOURCE, 33, 12,
         {"SDA": "bidirectional", "SCL": "input", "VDD": "power_in", "VSS": "power_in"})
    part("J1", "i2c_header", "I2C GND VCC SDA SCL", "TSW-104-07-G-S", "Samtec",
         "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
         {"GND": "1", "VCC": "2", "SDA": "3", "SCL": "4"}, HEADER_SOURCE, 42, 8)
    part("J2", "spi_header", "CUSTOM ISP MISO VCC SCK MOSI RESET GND", "TSW-106-07-G-S", "Samtec",
         "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
         {"MISO": "1", "VCC": "2", "SCK": "3", "MOSI": "4", "CS": "5", "GND": "6"}, ISP_SOURCE, 42, 28)
    for ref, value, mpn, x, y, source in [("R1", "4.7k", "RC0805FR-074K7L", 28, 9, RESISTOR_SOURCE),
                                          ("R2", "4.7k", "RC0805FR-074K7L", 28, 14, RESISTOR_SOURCE),
                                          ("R3", "10k", "RC0805FR-0710KL", 10, 8, RESET_SOURCE)]:
        part(ref, "resistor", value, mpn, "YAGEO", "Resistor_SMD:R_0805_2012Metric",
             {"1": "1", "2": "2"}, source, x, y)
    # 去耦电容用规范类型 capacitor：ERC 的去耦检查按规范类型判定，
    # capacitor_ceramic 只是文本匹配子类型，会让检查看不见已存在的去耦。
    for ref, x, y in [("C1", 10, 23.24), ("C2", 25, 32), ("C3", 25, 24), ("C4", 33, 16)]:
        part(ref, "capacitor", "100nF", "GRM21BR71H104KA01L", "Murata",
             "Capacitor_SMD:C_0805_2012Metric", {"+": "1", "-": "2"}, CAP_SOURCE, x, y)
    parts += [{"id": "GND1", "type": "gnd", "attrs": {"value": "external common ground"}},
              {"id": "VCC1", "type": "vcc", "attrs": {"value": "3.3V"}}]
    groups = [["GND1:1", "J1:GND", "U1:GND", "U1:GND2", "U2:VSS", "C1:-", "C2:-", "C3:-", "C4:-", "J2:GND"],
              ["VCC1:1", "J1:VCC", "U1:VCC", "U1:AVCC", "U2:VDD", "C1:+", "C2:+", "C4:+",
               "R1:1", "R2:1", "R3:1", "J2:VCC"],
              ["U1:PC4", "R1:2", "U2:SDA", "J1:SDA"],
              ["U1:PC5", "R2:2", "U2:SCL", "J1:SCL"],
              ["U1:AREF", "C3:+"],
              ["U1:RESET", "R3:2", "J2:CS"],
              ["U1:PB3", "J2:MOSI"], ["U1:PB4", "J2:MISO"], ["U1:PB5", "J2:SCK"]]
    return {"version": 2, "title": "Internal ATmega328P-PU 3.3V I2C + on-board SHT4x candidate",
            "parts": parts, "connections": [[a, b] for g in groups for a, b in zip(g, g[1:])],
            "board": {"width_mm": 50, "height_mm": 55, "layers": 2,
                      "min_clearance_mm": 0.2, "min_trace_width_mm": 0.2},
            "voltage_domains": [{"id": "logic", "nominal_v": 3.3, "min_v": 3.0, "max_v": 3.6}],
            "provenance": {"origin": "manual", "source_id": "internal-active-atmega328p-sht4x-3v3-01"},
            "engineering_example": {"classification": "internal_engineering_example",
                "review_required": True,
                "topology": "active_atmega328p_3v3_onboard_i2c_rh_sensor",
                "independent_external_sample": False,
                "sources": [MCU_SOURCE, HARDWARE_SOURCE, CAP_SOURCE, RESET_SOURCE, HEADER_SOURCE,
                            ISP_SOURCE, RESISTOR_SOURCE, LIBRARY_SOURCE, SENSOR_SOURCE],
                "clock": "Internal calibrated 8MHz RC; at 3.3V the ATmega328P supports this clock per datasheet. No external crystal populated; fuse/firmware state must be verified with ISP.",
                "analog": "AVCC connected to VCC with 100nF decoupling; AREF only decoupled to GND. ADC precision/filter not validated.",
                "sensor": "On-board Sensirion SHT4x I2C temperature/humidity sensor, bare DFN-4; pin order 1=SDA 2=SCL 3=VDD 4=VSS taken from the KiCad official Sensor_Humidity symbol/footprint pair. I2C address not declared; sensor reading not validated.",
                "pin_assignment": "J1 pads GND,VCC,SDA,SCL; J2 single-row custom ISP pads MISO,VCC,SCK,MOSI,RESET,GND. Not the standard 2x3 ISP connector.",
                "assumptions": "One regulated external 3.3V source; 4.7k on-board pullups to 3.3V; external 3.3V I2C devices optional; no firmware/waveform/assembled board or sensor reading validation.",
                "limitations": ["ATmega328P is NRND per current manufacturer page",
                                "No ESD/protection circuit or reset pushbutton",
                                "No firmware or fuse programming performed",
                                "No physical assembly, I2C timing or sensor reading validation"]}}


def explicit_sensor_routes(circuit: dict, candidate: dict) -> str:
    """3.3V 传感器板的显式走线：信号线手工指定，电源/地交给覆铜区。

    SHT4x 是 0.8mm 间距 DFN-4，但 VDD/VSS 由覆铜区热 relief 直连（VSS 经一个
    过孔下到背面地平面），因此只有 SDA/SCL 需要从封装径向向外逃逸，避免在
    0.3mm 焊盘缝隙里穿线。SDA 走正面、SCL 走背面，交叉靠分层而不是靠绕行。
    """
    board = parse_sexpr(candidate["board_text"])
    coords = {}
    for fp in _children(board, "footprint"):
        ref = next(_value(p[2]) for p in _children(fp, "property") if _value(p[1]) == "Reference")
        ax, ay = [float(_value(x)) for x in _one(fp, "at")[1:3]]
        for pad in _children(fp, "pad"):
            x, y = [float(_value(x)) for x in _one(pad, "at")[1:3]]
            coords[ref, _value(pad[1])] = (ax + x, ay + y)
    parts = {p["id"]: p for p in circuit["parts"]}
    numbers, labels = _nets(circuit, parts)
    labels = dict(labels)
    tracks, vias = [], []

    def at(ref, logical):
        return coords[ref, parts[ref]["component"]["pin_map"][logical]]

    def path(endpoint, layer, points):
        number = numbers[tuple(endpoint.split(":"))]
        for start, end in zip(points, points[1:]):
            tracks.append((number, layer, start, end))

    def via(endpoint, point):
        vias.append((numbers[tuple(endpoint.split(":"))], point))

    # SDA 正面只到传感器焊盘；去 J1 的一段走背面，避免正面与 SCL 进传感器的
    # 横线交叉（SMD 焊盘只有正面铜，交叉只能靠分层消掉）。
    sda2, u2sda, j1sda = at("R1", "2"), at("U2", "SDA"), at("J1", "SDA")
    path("U1:PC4", "F.Cu", [at("U1", "PC4"), (sda2[0], at("U1", "PC4")[1]), sda2])
    path("U1:PC4", "F.Cu", [sda2, (31, sda2[1]), (31, u2sda[1]), u2sda])
    via("U1:PC4", (31, sda2[1]))
    path("U1:PC4", "B.Cu", [(31, sda2[1]), (31, j1sda[1]), j1sda])
    # SCL 长距离走背面避开 SDA；两个 SMD 端点各用一个过孔上正面。
    scl2, u2scl, j1scl = at("R2", "2"), at("U2", "SCL"), at("J1", "SCL")
    path("U1:PC5", "B.Cu", [at("U1", "PC5"), (28, at("U1", "PC5")[1]), (28, 16)])
    via("U1:PC5", (28, 16))
    path("U1:PC5", "F.Cu", [(28, 16), (scl2[0], 16), scl2])
    path("U1:PC5", "B.Cu", [(28, 16), (30.5, 16)])
    via("U1:PC5", (30.5, 16))
    path("U1:PC5", "F.Cu", [(30.5, 16), (30.5, u2scl[1]), u2scl])
    path("U1:PC5", "B.Cu", [(30.5, 16), (30.5, j1scl[1]), j1scl])
    path("U1:AREF", "F.Cu", [at("U1", "AREF"), (23, at("U1", "AREF")[1]),
                             (23, at("C3", "+")[1]), at("C3", "+")])
    path("U1:RESET", "F.Cu", [at("R3", "2"), at("U1", "RESET")])
    reset_via = (at("U1", "RESET")[0], at("U1", "RESET")[1] - 1.5)
    path("U1:RESET", "F.Cu", [at("U1", "RESET"), reset_via])
    via("U1:RESET", reset_via)
    path("U1:RESET", "B.Cu", [reset_via, (11, reset_via[1]), (11, 44), (40, 44),
                              (40, at("J2", "CS")[1]), at("J2", "CS")])
    path("U1:PB3", "F.Cu", [at("U1", "PB3"), (39, at("U1", "PB3")[1]),
                            (39, at("J2", "MOSI")[1]), at("J2", "MOSI")])
    path("U1:PB4", "B.Cu", [at("U1", "PB4"), (38, at("U1", "PB4")[1]),
                            (38, at("J2", "MISO")[1]), at("J2", "MISO")])
    path("U1:PB5", "F.Cu", [at("U1", "PB5"), (36, at("U1", "PB5")[1]),
                            (36, at("J2", "SCK")[1]), at("J2", "SCK")])
    # 正面 SMD 的地焊盘用过孔接到背面实地平面；传感器 VSS 同样处理。
    for ref, point in [("C1", (12.5, 23.24)), ("C2", (27.5, 32)), ("C3", (27.5, 24))]:
        path(ref + ":-", "F.Cu", [at(ref, "-"), point])
        via(ref + ":-", point)
    # C4 的地过孔避开背面 SCL 去 J1 的横线（y=15.62）
    path("C4:-", "F.Cu", [at("C4", "-"), (36, at("C4", "-")[1]), (36, 17.5)])
    via("C4:-", (36, 17.5))
    vss_via = (35.5, at("U2", "VSS")[1])
    path("U2:VSS", "F.Cu", [at("U2", "VSS"), vss_via])
    via("U2:VSS", vss_via)
    for index, (number, layer, start, end) in enumerate(tracks):
        board.append([_a("segment"), [_a("start"), *[_a(_num(n)) for n in start]],
                      [_a("end"), *[_a(_num(n)) for n in end]], [_a("width"), _a("0.25")],
                      [_a("layer"), _a(layer, True)], [_a("net"), _a(number)],
                      [_a("uuid"), _a(_uuid("sensor-track", index), True)]])
    for index, (number, point) in enumerate(vias):
        board.append([_a("via"), [_a("at"), *[_a(_num(n)) for n in point]], [_a("size"), _a("0.6")],
                      [_a("drill"), _a("0.3")], [_a("layers"), _a("F.Cu", True), _a("B.Cu", True)],
                      [_a("net"), _a(number)], [_a("uuid"), _a(_uuid("sensor-via", index), True)]])
    for logical, layer in [("VCC", "F.Cu"), ("GND", "B.Cu")]:
        number = numbers["J1", logical]
        # VCC 区用实心连接：SHT4x 的 VDD 焊盘只有 0.5x0.3mm，热 relief 放不下
        # 两根引线会报 starved_thermal；GND 区焊盘/过孔够大，保留热 relief。
        # KiCad 10.0.6 的 zone connect_pads 语法只接受 yes/no/clearance
        # （本机 kicad-cli 实证：yes=实心、no=不连接、裸 clearance=热 relief），
        # 历史上的 "(type solid)" 写法会让 kicad-cli 加载板子直接失败。
        connect = ("(connect_pads yes (clearance 0.2))" if logical == "VCC"
                   else "(connect_pads (clearance 0.2))")
        zone = f'''(zone (net {number}) (net_name "{labels[number]}") (layer "{layer}")
          (uuid "{_uuid('sensor-plane', layer)}") (hatch edge 0.5)
          {connect} (min_thickness 0.25) (filled_areas_thickness no)
          (fill yes (thermal_gap 0.25) (thermal_bridge_width 0.25))
          (polygon (pts (xy 2 2) (xy 48 2) (xy 48 53) (xy 2 53))))'''
        board.append(parse_sexpr(zone))
    return dump_sexpr(board) + "\n"


def generate_active_example(output_dir, footprint_roots, cli_path, variant: str = "5v") -> dict:
    if variant not in ("5v", "3v3"):
        raise ValueError(f"未知 variant：{variant}")
    circuit_fn = active_circuit_3v3 if variant == "3v3" else active_circuit
    routes_fn = explicit_sensor_routes if variant == "3v3" else explicit_active_routes
    board_title = ("Active ATmega328P 3.3V SHT4x candidate" if variant == "3v3"
                   else "Active ATmega328P I2C candidate")
    directory = Path(output_dir) / ("run_" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    circuit = circuit_fn()
    (directory / "circuit.json").write_text(json.dumps(circuit, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {"classification": "internal_active_engineering_example", "manufacturing_release": False,
              "variant": variant, "run_dir": str(directory.resolve()), "status": "unavailable"}
    def finish():
        (directory / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    gate = validate_candidate(circuit, source="manual", name="candidate", require_official=True,
                              work_dir=directory / "t05_source")
    result["source_gate"] = gate
    if not gate["accepted"]:
        result["status"] = "source_gate_failed"
        return finish()
    candidate = build_pcb_candidate(circuit, board_title, footprint_roots, 20260206)
    board = directory / "candidate.kicad_pcb"
    board.write_text(routes_fn(circuit, candidate), encoding="utf-8")
    (directory / "candidate.kicad_pro").write_text(json.dumps({"meta": {"version": 3}}), encoding="utf-8")
    write_fp_lib_table(circuit, footprint_roots, directory / "fp-lib-table")
    # KiCad itself fills and saves real copper before the immutable manufacturing
    # pipeline starts. The fill DRC and its nonzero errors are retained separately.
    fill = _command(cli_path, ["pcb", "drc", "--refill-zones", "--save-board", "--format", "json",
                              "--severity-all", "--exit-code-violations", "-o", str(directory / "fill-drc.json"),
                              str(board)], 90)
    result["zone_fill_command"] = fill
    if fill["exit_code"] is None or not board.is_file():
        result["status"] = "zone_fill_unavailable"
        return finish()
    result["official_pipeline"] = run_manufacturing_pipeline(circuit, board.read_text(encoding="utf-8"),
                                                            directory / "manufacturing", cli_path, footprint_roots)
    result["status"] = result["official_pipeline"]["status"]
    return finish()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--footprints", required=True)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--variant", default="5v", choices=("5v", "3v3"),
                        help="5v=5V I2C 例；3v3=3.3V MCU + 板载 I2C 传感器目标场景例")
    args = parser.parse_args()
    result = generate_active_example(args.output, [args.footprints], args.cli, variant=args.variant)
    print(json.dumps({"status": result["status"], "run_dir": result["run_dir"],
                      "source_gate": result.get("source_gate", {}).get("blocking_gates"),
                      "failure": result.get("official_pipeline", {}).get("failure")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
