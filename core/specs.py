# -*- coding: utf-8 -*-
"""
specs.py —— 器件电气规格库（工业级额定值，零第三方依赖）
=========================================================
教学目录只回答「有没有这个脚」；工业级还要回答「能不能承受这个电流/电压」。
本模块为常见器件补充规格字段，供：
* simulate.py 准仿真安全裕量
* ERC 扩展（过流/过压/功耗）
* BOM 导出（封装/额定）

字段约定（可缺省）：
  vf_typ / vf_max     正向压降（V，LED/二极管）
  i_max_ma / i_typ_ma 额定/典型电流（mA）
  p_max_mw            额定功耗（mW）
  v_max               最大工作电压（V）
  package             教学默认封装名（BOM 用）
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# type -> specs
SPECS: Dict[str, Dict[str, Any]] = {
    "led": {
        "vf_typ": 2.0, "vf_max": 2.4,
        "i_typ_ma": 10.0, "i_max_ma": 20.0, "p_max_mw": 100,
        "package": "LED 5mm",
    },
    "diode": {
        "vf_typ": 0.7, "vf_max": 1.0,
        "i_max_ma": 200, "v_max": 50, "package": "DO-35",
    },
    "zener": {
        "vf_typ": 5.1, "i_typ_ma": 5, "i_max_ma": 50,
        "p_max_mw": 500, "package": "DO-35",
    },
    "resistor": {
        "p_max_mw": 250, "package": "Axial 0.25W",
    },
    "capacitor": {
        "v_max": 50, "package": "Radial",
    },
    "inductor": {
        "i_max_ma": 100, "package": "Axial",
    },
    "battery": {
        "v_max": 15, "package": "Battery holder",
    },
    "ne555": {
        "v_min": 4.5, "v_max": 16, "i_max_ma": 200,
        "package": "DIP-8",
    },
    "regulator_7805": {
        "v_in_min": 7.0, "v_in_max": 25, "v_out": 5.0,
        "i_max_ma": 1500, "package": "TO-220",
    },
    "npn": {
        "i_c_max_ma": 100, "v_ceo_max": 40, "package": "TO-92",
    },
    "pnp": {
        "i_c_max_ma": 100, "v_ceo_max": 40, "package": "TO-92",
    },
    "buzzer": {
        "v_max": 5, "i_max_ma": 30, "package": "Buzzer 12mm",
    },
    "pushbutton": {
        "package": "6x6 Tact",
    },
    "switch": {
        "package": "SPST",
    },
    "electrolytic": {"v_max": 25, "polarized": True, "package": "Radial 5mm"},
    "tvs": {"v_max": 5, "p_max_mw": 600, "package": "SMB"},
    "phototransistor": {"i_c_max_ma": 20, "package": "TO-92"},
    "ferrite_bead": {"package": "0805 / Axial"},
    "varistor": {"v_max": 300, "p_max_mw": 400, "package": "Disc 10mm"},
    "scr": {"i_max_ma": 12000, "package": "TO-202"},
    "buzzer_passive": {"v_max": 5, "i_max_ma": 30, "package": "Buzzer 12mm"},
    "ammeter": {"package": "Panel meter"},
    "voltmeter": {"package": "Panel meter"},
    "capacitor_ceramic": {"package": "Disc"},
    "capacitor_film": {"package": "Box"},
    "capacitor_tantalum": {"package": "Case-A"},
    "resistor_network": {"package": "SIP-9"},
    "inductor_toroid": {"package": "Toroid"},
    "transformer": {"package": "EI-35"},
    "led_rgb": {"package": "5mm RGB"},
    "diode_rectifier": {"package": "DO-41"},
    "triac": {"package": "TO-220"},
    "diac": {"package": "DO-35"},
    "fuse_resettable": {"package": "1206"},
    "igbt": {"package": "TO-247"},
    "ujt": {"package": "TO-92"},
    "phototriac": {"package": "DIP-6"},
    "ic_and": {"package": "DIP-14"},
    "ic_or": {"package": "DIP-14"},
    "ic_not": {"package": "DIP-14"},
    "ic_xor": {"package": "DIP-14"},
    "ic_nand": {"package": "DIP-14"},
    "ic_nor": {"package": "DIP-14"},
    "ic_buffer": {"package": "DIP-20"},
    "flipflop_d": {"package": "DIP-14"},
    "counter_4bit": {"package": "DIP-14"},
    "shiftreg_8bit": {"package": "DIP-16"},
    "lm393": {"package": "DIP-8"},
    "lm324": {"package": "DIP-14"},
    "ne556": {"package": "DIP-14"},
    "cd4017": {"package": "DIP-16"},
    "atmega328": {"package": "DIP-28"},
    "esp32": {"package": "Module"},
    "dht11": {"package": "Module"},
    "ds18b20": {"package": "TO-92"},
    "bmp280": {"package": "Module"},
    "mpu6050": {"package": "Module"},
    "hc_sr04": {"package": "Module"},
    "pir": {"package": "Module"},
    "mq2": {"package": "Module"},
    "sound_sensor": {"package": "Module"},
    "ir_receiver": {"package": "TO-92"},
    "ir_led": {"package": "5mm IR"},
    "hall_sensor": {"package": "TO-92"},
    "load_cell": {"package": "Module"},
    "microphone": {"package": "Φ9.7"},
    "sevenseg": {"package": "0.56inch"},
    "lcd1602": {"package": "Module"},
    "oled_ssd1306": {"package": "Module"},
    "max7219": {"package": "DIP-24"},
    "tm1637": {"package": "Module"},
    "stepper": {"package": "28BYJ"},
    "solenoid": {"package": "Push-Pull"},
    "heater": {"package": "PTC"},
    "lm317": {"package": "TO-220"},
    "buck_module": {"package": "Module"},
    "boost_module": {"package": "Module"},
    "lipo_battery": {"package": "103450"},
    "solar_cell": {"package": "Poly"},
    "usb_port": {"package": "Micro-B"},
    "barrel_jack": {"package": "DC-005"},
    "conn_header": {"package": "2.54mm"},
    "switch_toggle": {"package": "SS-12D"},
    "switch_dip": {"package": "DIP-6"},
    "switch_reed": {"package": "Φ3"},
    "connector_screw2": {"package": "KF301-2"},
    "potentiometer_trim": {"package": "3296"},
    "varactor": {"package": "SOD-323"},
    "optointerrupter": {"package": "Module"},
    "buzzer_magnetic": {"package": "12mm"},
    "speaker_8ohm": {"package": "40mm"},
    "antenna": {"package": "SMA"},
    "jumper_wire": {"package": "2.54mm"},
    "capacitor_trim": {"package": "Trimmer"},
    "capacitor_variable": {"package": "Polyvaricon"},
    "capacitor_feedthrough": {"package": "Feedthrough"},
    "crystal_gnd": {"package": "HC-49S"},
    "circuit_breaker_1p": {"package": "1P MCB"},
    "circuit_breaker_2p": {"package": "2P MCB"},
    "circuit_breaker_3p": {"package": "3P MCB"},
    "capacitor_network": {"package": "SIP-5"},
    "antenna_dipole": {"package": "Dipole"},
    "battery_cell_1v5": {"package": "AA/AAA"},
    "vcc": {"package": "Power flag"},
    "gnd": {"package": "Power flag"},
    # ---------- 2026 电源管理 + 通信接口 + 保护器件 + 模拟前端扩容 ----------
    "ldo_3v3": {"v_out": 3.3, "v_in_min": 4.3, "i_max_ma": 800, "package": "SOT-223"},
    "ldo_1v8": {"v_out": 1.8, "v_in_min": 2.8, "i_max_ma": 800, "package": "SOT-223"},
    "charge_pump": {"v_max": 10.5, "i_max_ma": 100, "package": "DIP-8"},
    "voltage_monitor": {"v_max": 5.5, "package": "SOT-23"},
    "power_sequencer": {"v_max": 6.5, "package": "SOT-23-6"},
    "uart_header": {"package": "2.54mm 1x4"},
    "spi_header": {"package": "2.54mm 1x6"},
    "i2c_header": {"package": "2.54mm 1x4"},
    "can_transceiver": {"v_max": 5.5, "package": "DIP-8"},
    "rs485_transceiver": {"v_max": 5.5, "package": "DIP-8"},
    "usb_c": {"v_max": 20, "package": "USB-C 16P"},
    "tvs_array": {"v_max": 5, "p_max_mw": 350, "package": "SOT-23-6"},
    "esd_diode": {"v_max": 5, "p_max_mw": 350, "package": "SOD-323"},
    "common_mode_choke": {"i_max_ma": 1000, "package": "SMD 1210"},
    "gas_discharge_tube": {"v_max": 230, "package": "Φ5.5"},
    "resettable_fuse_1206": {"i_max_ma": 500, "package": "1206"},
    "instrumentation_amp": {"v_max": 36, "package": "DIP-8"},
    "current_sense_amp": {"v_max": 26, "package": "SOT-23-5"},
    "digital_pot": {"v_max": 5.5, "package": "DIP-8"},
    "dac_8bit": {"v_max": 5.5, "package": "DIP-20"},
    "adc_8bit": {"v_max": 5.5, "package": "DIP-20"},
    "buzzer_magnetic_5v": {"v_max": 5, "i_max_ma": 30, "package": "12mm"},
    "relay_solid_state": {"v_max": 250, "i_max_ma": 25000, "package": "Hockey puck"},
    "optocoupler_ac": {"v_max": 5, "package": "DIP-6"},
}


def get_spec(part_type: str) -> Dict[str, Any]:
    return dict(SPECS.get(str(part_type or ""), {}))


def package_of(part_type: str, fallback: str = "") -> str:
    return str(get_spec(part_type).get("package") or fallback or part_type)


def led_i_max_ma(color: Any = None) -> float:
    """教学默认 20mA；可按颜色微调。"""
    return float(SPECS["led"]["i_max_ma"])


if __name__ == "__main__":
    assert package_of("led") == "LED 5mm"
    assert get_spec("regulator_7805")["v_out"] == 5.0
    print("specs self-check ok", len(SPECS), "types")
