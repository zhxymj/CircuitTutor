# -*- coding: utf-8 -*-
"""
kicad_footprints.py —— CircuitJSON 类型 → KiCad 封装名称建议
=============================================================
本表只提供通用封装名称建议，既没有核对本机封装文件，也没有核对具体料号、
数据手册和原理图引脚到焊盘的映射。真实 PCB 候选必须走 core.pcb_real 的
显式元数据及本地封装核对；不能凭本表的名称进入制造输出。

映射值为 KiCad `库名:封装名` 格式。教学优先选 THT 直插、通用尺寸；
SMD 变体可通过 attrs.package 覆盖。

用法：
    from core.kicad_footprints import footprint_of, FOOTPRINTS
    fp = footprint_of("resistor")           # -> "Resistor_THT:R_Axial_..."
    fp = footprint_of("led", "SMD_0805")    # -> "LED_SMD:LED_0805_..."
"""

from __future__ import annotations

from typing import Dict, Optional

# type -> "Library:Footprint"（KiCad 官方库）
FOOTPRINTS: Dict[str, str] = {
    # ---------- 无源 ----------
    "resistor":          "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
    "potentiometer":     "Potentiometer_THT:Potentiometer_Alps_RK09K_Single_Horizontal",
    "potentiometer_trim":"Potentiometer_THT:Potentiometer_Bourns_3296W_Vertical",
    "capacitor":         "Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm",
    "capacitor_ceramic": "Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm",
    "capacitor_film":    "Capacitor_THT:C_Rect_L10.0mm_W3.0mm_P7.50mm_MKS4",
    "capacitor_tantalum":"Capacitor_Tantalum_SMD:CP_EIA-3528-21_Kemet-B",
    "electrolytic":      "Capacitor_THT:CP_Radial_D5.0mm_P2.50mm",
    "capacitor_trim":    "Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm",
    "capacitor_variable":"Capacitor_THT:CP_Radial_D5.0mm_P2.50mm",
    "capacitor_network": "Package_SIP:SIP-8_19x3mm_P2.54mm",
    "capacitor_feedthrough":"Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm",
    "inductor":          "Inductor_THT:L_Axial_L11.0mm_D4.5mm_P7.62mm_Vertical_Fastron_MECC",
    "inductor_toroid":   "Inductor_THT:L_Axial_L11.0mm_D4.5mm_P7.62mm_Vertical_Fastron_MECC",
    "ferrite_bead":      "Inductor_SMD:L_0805_2012Metric",
    "transformer":       "Transformer_THT:Transformer_CHK_EI30-2VA_1xSec",
    "resistor_network":  "Package_SIP:SIP-8_19x3mm_P2.54mm",
    # ---------- 二极管 / LED ----------
    "diode":             "Diode_THT:D_DO-35_SOD27_P10.16mm_Horizontal",
    "zener":             "Diode_THT:D_DO-35_SOD27_P10.16mm_Horizontal",
    "schottky":          "Diode_THT:D_DO-35_SOD27_P10.16mm_Horizontal",
    "diode_rectifier":   "Diode_THT:D_DO-41_SOD81_P10.16mm_Horizontal",
    "tvs":               "Diode_SMD:D_SMB",
    "led":               "LED_THT:LED_D5.0mm",
    "led_rgb":           "LED_THT:LED_D5.0mm",
    "ir_led":            "LED_THT:LED_D5.0mm",
    "photodiode":        "Diode_THT:D_DO-35_SOD27_P10.16mm_Horizontal",
    "phototransistor":   "Package_TO_SOT_THT:TO-92_Inline",
    "photocell":         "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
    "thermistor":        "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
    "varistor":          "Varistor:RV_Disc_D12mm_W3.9mm_P7.5mm",
    "varactor":          "Diode_SMD:D_SOD-323",
    # ---------- 三极管 / MOSFET / 功率 ----------
    "npn":               "Package_TO_SOT_THT:TO-92_Inline",
    "pnp":               "Package_TO_SOT_THT:TO-92_Inline",
    "nmos":              "Package_TO_SOT_THT:TO-92_Inline",
    "pmos":              "Package_TO_SOT_THT:TO-92_Inline",
    "ujt":               "Package_TO_SOT_THT:TO-92_Inline",
    "igbt":              "Package_TO_SOT_THT:TO-247-3_Vertical",
    "scr":               "Package_TO_SOT_THT:TO-220-3_Vertical",
    "triac":             "Package_TO_SOT_THT:TO-220-3_Vertical",
    "diac":              "Diode_THT:D_DO-35_SOD27_P10.16mm_Horizontal",
    "phototriac":        "Package_DIP:DIP-6_W7.62mm",
    "optocoupler":       "Package_DIP:DIP-4_W7.62mm",
    "optointerrupter":   "OptoDevice:Everlight_ITR8307",
    # ---------- IC ----------
    "ne555":             "Package_DIP:DIP-8_W7.62mm",
    "ne556":             "Package_DIP:DIP-14_W7.62mm",
    "regulator_7805":    "Package_TO_SOT_THT:TO-220-3_Vertical",
    "lm317":             "Package_TO_SOT_THT:TO-220-3_Vertical",
    "lm393":             "Package_DIP:DIP-8_W7.62mm",
    "lm324":             "Package_DIP:DIP-14_W7.62mm",
    "cd4017":            "Package_DIP:DIP-16_W7.62mm",
    "ic_and":            "Package_DIP:DIP-14_W7.62mm",
    "ic_or":             "Package_DIP:DIP-14_W7.62mm",
    "ic_nand":           "Package_DIP:DIP-14_W7.62mm",
    "ic_nor":            "Package_DIP:DIP-14_W7.62mm",
    "ic_xor":            "Package_DIP:DIP-14_W7.62mm",
    "ic_not":            "Package_DIP:DIP-14_W7.62mm",
    "ic_buffer":         "Package_DIP:DIP-20_W7.62mm",
    "flipflop_d":        "Package_DIP:DIP-14_W7.62mm",
    "counter_4bit":      "Package_DIP:DIP-16_W7.62mm",
    "shiftreg_8bit":     "Package_DIP:DIP-16_W7.62mm",
    "max7219":           "Package_DIP:DIP-24_W7.62mm",
    "atmega328":         "Package_DIP:DIP-28_W7.62mm",
    "esp32":             "RF_Module:ESP32-WROOM-32",
    # ---------- 传感器 / 模块 ----------
    "dht11":             "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
    "ds18b20":           "Package_TO_SOT_THT:TO-92_Inline",
    "bmp280":            "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
    "mpu6050":           "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
    "hc_sr04":           "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
    "pir":               "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
    "mq2":               "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
    "sound_sensor":      "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
    "ir_receiver":       "Package_TO_SOT_THT:TO-92_Inline",
    "hall_sensor":       "Package_TO_SOT_THT:TO-92_Inline",
    "load_cell":         "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
    "microphone":        "TerminalBlock:TerminalBlock_MaiXu_MX126-5.0-02P_1x02_P5.00mm",
    # ---------- 显示 ----------
    "sevenseg":          "Display_7Segment:7SegmentLED_LTS6760_LTS6780",
    "lcd1602":           "Connector_PinHeader_2.54mm:PinHeader_1x16_P2.54mm_Vertical",
    "oled_ssd1306":      "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
    "tm1637":            "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
    # ---------- 执行器 ----------
    "dc_motor":          "TerminalBlock:TerminalBlock_MaiXu_MX126-5.0-02P_1x02_P5.00mm",
    "servo":             "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
    "stepper":           "Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical",
    "solenoid":          "TerminalBlock:TerminalBlock_MaiXu_MX126-5.0-02P_1x02_P5.00mm",
    "heater":            "TerminalBlock:TerminalBlock_MaiXu_MX126-5.0-02P_1x02_P5.00mm",
    "relay":             "Relay_THT:Relay_SPDT_Finder_32.21-x000",
    "buzzer":            "Buzzer_Beeper:Buzzer_12x9.5RM7.6",
    "buzzer_passive":    "Buzzer_Beeper:Buzzer_12x9.5RM7.6",
    "buzzer_magnetic":   "Buzzer_Beeper:Buzzer_12x9.5RM7.6",
    "speaker_8ohm":      "TerminalBlock:TerminalBlock_MaiXu_MX126-5.0-02P_1x02_P5.00mm",
    # ---------- 开关 / 连接器 ----------
    "pushbutton":        "Button_Switch_THT:SW_PUSH_6mm",
    "switch":            "Button_Switch_THT:SW_PUSH_6mm",
    "switch_toggle":     "Button_Switch_THT:SW_Push_2P1T_Toggle_CK_PVA1xxH1xxxxxxV2",
    "switch_dip":        "Button_Switch_THT:SW_DIP_SPSTx06_Slide_6.7x16.8mm_W7.62mm_P2.54mm_LowProfile",
    "switch_reed":       "Button_Switch_THT:SW_PUSH-12mm",
    "fuse":              "Fuse:Fuseholder_Clip-5x20mm_Keystone_3512_Inline_P23.62x7.27mm_D1.02x1.57mm_Horizontal",
    "fuse_resettable":   "Fuse:Fuse_1206_3216Metric",
    "circuit_breaker_1p":"TerminalBlock:TerminalBlock_MaiXu_MX126-5.0-02P_1x02_P5.00mm",
    "circuit_breaker_2p":"TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal",
    "circuit_breaker_3p":"TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal",
    "screw_terminal":    "TerminalBlock:TerminalBlock_MaiXu_MX126-5.0-02P_1x02_P5.00mm",
    "connector_screw2":  "TerminalBlock:TerminalBlock_MaiXu_MX126-5.0-02P_1x02_P5.00mm",
    "usb_port":          "Connector_USB:USB_Micro-B_Molex-105017-0001",
    "barrel_jack":       "Connector_BarrelJack:BarrelJack_CUI_PJ-063AH_Horizontal",
    "conn_header":       "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    "jumper_wire":       "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    "antenna":           "Connector_Coaxial:SMA_Amphenol_132134_Vertical",
    "antenna_dipole":    "Connector_Coaxial:SMA_Amphenol_132134_Vertical",
    # ---------- 电源 ----------
    "battery":           "Battery:BatteryHolder_Keystone_1042_1x18650",
    "battery_cell_1v5":  "Battery:BatteryHolder_Keystone_103_1x20mm",
    "lipo_battery":      "Connector_JST:JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical",
    "solar_cell":        "TerminalBlock:TerminalBlock_MaiXu_MX126-5.0-02P_1x02_P5.00mm",
    "buck_module":       "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
    "boost_module":      "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
    "ac_source":         "TerminalBlock:TerminalBlock_MaiXu_MX126-5.0-02P_1x02_P5.00mm",
    "crystal":           "Crystal:Crystal_HC49-U_Vertical",
    "crystal_gnd":       "Crystal:Crystal_HC49-U_Vertical",
    # ---------- 其他 ----------
    "ammeter":           "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    "voltmeter":         "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    # ---------- 2026 电源管理 / 通信接口 / 保护器件 / 模拟前端 ----------
    "ldo_3v3":           "Package_TO_SOT_SMD:SOT-223-3_TabPin2",
    "ldo_1v8":           "Package_TO_SOT_SMD:SOT-223-3_TabPin2",
    "charge_pump":       "Package_DIP:DIP-8_W7.62mm",
    "voltage_monitor":   "Package_TO_SOT_SMD:SOT-23",
    "power_sequencer":   "Package_TO_SOT_SMD:SOT-23-6",
    "uart_header":       "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
    "spi_header":        "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
    "i2c_header":        "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
    # 裸片封装而非模块排针：与目录项 i2c_rh_sensor 的引脚序同源（KiCad 官方配对）
    "i2c_rh_sensor":     "Sensor_Humidity:Sensirion_DFN-4_1.5x1.5mm_P0.8mm_SHT4x_NoCentralPad",
    "can_transceiver":   "Package_DIP:DIP-8_W7.62mm",
    "rs485_transceiver": "Package_DIP:DIP-8_W7.62mm",
    "usb_c":             "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
    "tvs_array":         "Package_TO_SOT_SMD:SOT-23-6",
    "esd_diode":         "Diode_SMD:D_SOD-323",
    "common_mode_choke": "Inductor_SMD:L_CommonMode_Choke_Murata_DLW5BSN",
    "gas_discharge_tube":"Varistor:RV_Disc_D12mm_W3.9mm_P7.5mm",
    "resettable_fuse_1206": "Fuse:Fuse_1206_3216Metric",
    "instrumentation_amp": "Package_DIP:DIP-8_W7.62mm",
    "current_sense_amp": "Package_TO_SOT_SMD:SOT-23-5",
    "digital_pot":       "Package_DIP:DIP-8_W7.62mm",
    "dac_8bit":          "Package_DIP:DIP-20_W7.62mm",
    "adc_8bit":          "Package_DIP:DIP-20_W7.62mm",
    "buzzer_magnetic_5v":"Buzzer_Beeper:Buzzer_12x9.5RM7.6",
    "relay_solid_state": "Relay_THT:Relay_SPDT_Finder_32.21-x000",
    "optocoupler_ac":    "Package_DIP:DIP-6_W7.62mm",
    # 电源锚点不落板
    "vcc":               "",
    "gnd":               "",
}

# SMD 变体覆盖（attrs.package 含 SMD/0805 等关键词时替换）
_SMD_OVERRIDES = {
    "resistor":   "Resistor_SMD:R_0805_2012Metric",
    "capacitor":  "Capacitor_SMD:C_0805_2012Metric",
    "led":        "LED_SMD:LED_0805_2012Metric",
    "inductor":   "Inductor_SMD:L_0805_2012Metric",
    "diode":      "Diode_SMD:D_SOD-123",
}

_SMD_KEYWORDS = ("smd", "0805", "0603", "1206", "0402", "smt")


def footprint_of(part_type: str, package_hint: str = "") -> str:
    """返回 KiCad 封装字符串；未映射返回空串。

    package_hint 为 attrs.package 等自由文本，含 SMD 关键词时尝试 SMD 变体。
    """
    t = str(part_type or "")
    hint = str(package_hint or "").lower()
    if hint and any(k in hint for k in _SMD_KEYWORDS):
        smd = _SMD_OVERRIDES.get(t)
        if smd:
            return smd
    return FOOTPRINTS.get(t, "")


def footprint_for_part(part: dict) -> str:
    """从 CircuitJSON part dict 取封装。

    component.footprint 是显式封装声明（core.pcb_real 的同一约定），优先于
    本模块的通用建议表：导入的外部源板带复现封装时，不能让建议表把它换回
    通用件，否则原理图与 PCB 会指向两个不同的物理封装。
    """
    explicit = ((part.get("component") or {}).get("footprint") or "").strip()
    if explicit:
        return explicit
    attrs = part.get("attrs") or {}
    return footprint_of(part.get("type"), attrs.get("package") or "")


if __name__ == "__main__":
    assert footprint_of("resistor").startswith("Resistor_THT:")
    assert footprint_of("led").startswith("LED_THT:")
    assert footprint_of("ne555") == "Package_DIP:DIP-8_W7.62mm"
    assert footprint_of("resistor", "SMD 0805") == "Resistor_SMD:R_0805_2012Metric"
    assert footprint_of("unknown_xyz") == ""
    assert footprint_of("vcc") == ""
    assert footprint_for_part({"type": "i2c_header"}) == FOOTPRINTS["i2c_header"]
    assert footprint_for_part({"type": "i2c_header",
                               "component": {"footprint": "Lib:Exact"}}) == "Lib:Exact"
    n = sum(1 for v in FOOTPRINTS.values() if v)
    print(f"kicad_footprints self-check ok  {n} 条有效映射")
