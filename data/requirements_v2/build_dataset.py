"""Build the author-written requirement fixture; no external data or templates read."""
from pathlib import Path
import hashlib
import json


def task(category, prompt, *, missing=(), models=(), rules=(), required=None):
    return {"category": category, "prompt": prompt, "missing_fields": list(missing),
            "unsupported_models": list(models), "negative_rule_ids": list(rules),
            "required_types_override": required}


# Each row describes one internally authored scenario. Variants stay in its case
# group. These are request contracts, not independently sourced circuit diagrams.
SCENARIOS = [
    ("C01", "ATmega 气压采集", "single_sensor", ["atmega328", "bmp280"],
     ["3.3V 共地", "PC4/PC5 对应 SDA/SCL", "各一只上拉电阻"],
     "3.3V ATmega328P 通过 PC4/PC5 连接 BMP280 的 SDA/SCL；各加一只 4.7k 上拉电阻和 100nF 去耦电容，所有地共地。",
     task("ambiguous", "3.3V 单片机用 I2C 读取 BMP280，请生成接口。", missing=["controller_model"], required=["bmp280"]),
     task("unsupported_model", "用 STM32F103 与 BMP280 做 3.3V I2C 气压采集。", models=["STM32F103"], required=["bmp280"])),
    ("C02", "ESP32 姿态采集", "single_sensor", ["esp32", "mpu6050"],
     ["3.3V 共地", "GPIO21/22 对应 SDA/SCL", "AD0 地址配置明确"],
     "ESP32-WROOM 和 MPU6050 都使用 3.3V，GPIO21 接 SDA、GPIO22 接 SCL，AD0 接地，加入两只上拉电阻与各器件去耦电容。",
     task("negative", "3.3V ESP32 接 MPU6050 走 I2C，把 SDA 和 SCL 短接成一条导线以节约空间。", rules=["SDA_SCL_SHORT"]),
     task("ambiguous", "ESP32 读取 MPU6050 的 I2C 数据，但供电和上拉电压还没确定。", missing=["bus_voltage"])),
    ("C03", "ATmega 状态显示", "display", ["atmega328", "oled_ssd1306"],
     ["3.3V I2C", "不增加独立 LED", "显示地址固定"],
     "ATmega328P 用 3.3V I2C 连接 SSD1306 OLED，PC4 接 SDA、PC5 接 SCL；总线上拉电阻与 OLED 去耦电容齐全，不要添加独立 LED。",
     task("ambiguous", "3.3V ATmega328P 接一个 I2C 显示模块，请先决定型号。", missing=["device_model"], required=["atmega328"]),
     task("unsupported_model", "3.3V ATmega328P 通过 I2C 驱动 SH1106 显示屏，不可替换成 SSD1306。", models=["SH1106"], required=["atmega328"])),
    ("C04", "ESP32 气压与姿态共线", "two_sensor", ["esp32", "bmp280", "mpu6050"],
     ["3.3V 共享 SDA/SCL", "每线一组上拉", "每个从设备独立去耦"],
     "3.3V ESP32 连接 BMP280 和 MPU6050，两只从设备共用 GPIO21/SDA 与 GPIO22/SCL；每线一只上拉电阻，各有去耦电容并共地。",
     task("negative", "3.3V ESP32、BMP280、MPU6050 共用 I2C；禁止接任何公共地，仍要求可靠通信。", rules=["NO_COMMON_GROUND"]),
     task("ambiguous", "ESP32 与 BMP280、MPU6050 共用 I2C，器件供电尚未选择。", missing=["bus_voltage"])),
    ("C05", "ATmega 气压与显示共线", "sensor_display", ["atmega328", "bmp280", "oled_ssd1306"],
     ["3.3V 共享 I2C", "按器件分别配置地址", "不产生排针"],
     "3.3V ATmega328P 的 I2C 同时连接 BMP280 和 SSD1306 OLED，分别配置地址，每条总线只放一只上拉电阻，所有芯片去耦并共地。",
     task("ambiguous", "3.3V ATmega328P 需要气压传感器和 I2C 显示屏，两个器件的型号都未确定。", missing=["device_models"], required=["atmega328"]),
     task("unsupported_model", "3.3V ATmega328P 接 BME680 和 SSD1306 OLED，BME680 型号不可替换。", models=["BME680"], required=["atmega328", "oled_ssd1306"])),
    ("C06", "ESP32 姿态与显示共线", "sensor_display", ["esp32", "mpu6050", "oled_ssd1306"],
     ["3.3V I2C", "只一组总线上拉", "OLED 不等于 LED"],
     "3.3V ESP32 用 GPIO21/22 共接 MPU6050 和 SSD1306 OLED；增加两只总线上拉电阻，每个模块加去耦电容，不需要 LED 指示灯。",
     task("negative", "ESP32、MPU6050、SSD1306 OLED 都接 3.3V I2C，将 ESP32 的 GPIO21/SDA 直接焊到 3.3V 电源。", rules=["SIGNAL_TIED_TO_SUPPLY"]),
     task("ambiguous", "ESP32 用 I2C 接 MPU6050 与 OLED，但希望后续再决定总线电压。", missing=["bus_voltage"])),
    ("C07", "三从设备控制子板", "three_slave", ["atmega328", "bmp280", "mpu6050", "oled_ssd1306"],
     ["3.3V 三从设备共享 I2C", "地址不冲突", "分支分别去耦"],
     "ATmega328P、BMP280、MPU6050 与 SSD1306 OLED 都供电 3.3V，三个从设备共用 PC4/PC5 I2C，每线只一只上拉电阻并分别去耦。",
     task("ambiguous", "3.3V 主控接 BMP280、MPU6050、SSD1306 OLED 共用 I2C，主控型号尚未确定。", missing=["controller_model"], required=["bmp280", "mpu6050", "oled_ssd1306"]),
     task("unsupported_model", "3.3V RP2040 接 BMP280、MPU6050、SSD1306 OLED 共用 I2C。", models=["RP2040"], required=["bmp280", "mpu6050", "oled_ssd1306"])),
    ("C08", "双气压传感器寻址", "duplicate_address", ["atmega328", "bmp280"],
     ["两只 BMP280 地址配置不同", "3.3V I2C", "SDO 电平分别确定"],
     "3.3V ATmega328P 同时连接两只 BMP280 的 I2C，第一只 SDO 接地、第二只 SDO 接 3.3V；每个器件去耦，总线只一组上拉电阻。",
     task("negative", "3.3V ATmega328P 的同一 I2C 上接两只 BMP280，二者地址都设成 0x76，不加多路复用器但必须分别读取。", rules=["I2C_ADDRESS_COLLISION"]),
     task("ambiguous", "3.3V ATmega328P 接两只 BMP280 共用 I2C，两只的地址与 SDO 电平还没决定。", missing=["slave_addresses"])),
    ("C09", "双姿态传感器寻址", "duplicate_address", ["esp32", "mpu6050"],
     ["两只 MPU6050 地址配置不同", "3.3V I2C", "AD0 电平分别确定"],
     "ESP32 与两只 MPU6050 供电 3.3V，共用 I2C；一只 AD0 接地、另一只接 3.3V，分别去耦并只使用一组总线上拉电阻。",
     task("ambiguous", "3.3V 主控接两只 MPU6050 的 I2C，主控型号仍未选。", missing=["controller_model"], required=["mpu6050"]),
     task("unsupported_model", "3.3V ESP32 接两只 ICM20948 走 I2C，不允许改用 MPU6050。", models=["ICM20948"], required=["esp32"])),
    ("C10", "ATmega 稳压气压子板", "regulated_board", ["ldo_3v3", "atmega328", "bmp280"],
     ["5V 输入经 3.3V LDO", "LDO 输入输出电容", "逻辑仅 3.3V"],
     "5V 输入经 AMS1117-3.3 供给 ATmega328P 和 BMP280，LDO 两侧按要求各有电容，I2C 上拉到 3.3V并共地。",
     task("negative", "5V 输入经 AMS1117-3.3 给 ATmega328P、BMP280 供电，但 I2C 的 SDA/SCL 上拉必须直接接 5V。", rules=["I2C_PULLUP_WRONG_DOMAIN"]),
     task("ambiguous", "ATmega328P 接 BMP280 的 I2C，需要输入电源，但还没选输入和逻辑电压。", missing=["supply_domains"], required=["atmega328", "bmp280"])),
    ("C11", "ESP32 稳压姿态子板", "regulated_board", ["ldo_3v3", "esp32", "mpu6050"],
     ["5V 经 LDO 输出 3.3V", "EN 保持有效", "I2C 共地"],
     "5V 经 AMS1117-3.3 供给 ESP32-WROOM 与 MPU6050，ESP32 EN 上拉有效，I2C 用 GPIO21/22，LDO 两端电容和器件去耦齐全。",
     task("unsupported_model", "5V 输入必须经 MCP1700-3302 给 ESP32 和 MPU6050 的 3.3V I2C 供电，不可换 LDO 型号。", models=["MCP1700-3302"], required=["esp32", "mpu6050"]),
     task("unsupported_model", "5V 用 TLV75533 给 3.3V ESP32 与 MPU6050 供电，必须使用这个 LDO。", models=["TLV75533"], required=["esp32", "mpu6050"])),
    ("C12", "低压气压显示稳压板", "regulated_display", ["ldo_3v3", "atmega328", "bmp280", "oled_ssd1306"],
     ["5V 到 3.3V 电源", "气压与 OLED 共享 I2C", "各端去耦"],
     "5V 输入经 AMS1117-3.3，给 ATmega328P、BMP280 和 SSD1306 OLED 提供 3.3V；三者共地，总线有上拉电阻，LDO 和器件都有电容。",
     task("negative", "5V 经 AMS1117-3.3 给 ATmega328P、BMP280、SSD1306 OLED 供电，删除 LDO 输出电容且禁止补回。", rules=["LDO_OUTPUT_CAP_OMITTED"]),
     task("negative", "AMS1117-3.3 供 ATmega328P、BMP280 和 SSD1306 OLED 的 3.3V I2C，但主控与两个从设备必须各自隔离地且不加隔离器。", rules=["NO_COMMON_GROUND"])),
    ("C13", "外部气压维护口", "external_header", ["atmega328", "bmp280", "i2c_header"],
     ["3.3V 本地气压器件", "四针 I2C 维护口", "只一组上拉"],
     "3.3V ATmega328P 接 BMP280，同时引出 SDA、SCL、3.3V、GND 的四针 I2C 排针维护口，整个总线一组上拉电阻并共地。",
     task("ambiguous", "3.3V 单片机接 BMP280，并引出四针 I2C 排针维护口，主控型号待定。", missing=["controller_model"], required=["bmp280", "i2c_header"]),
     task("unsupported_model", "3.3V ATmega328P 接 DPS310，并引出四针 I2C 排针维护口。", models=["DPS310"], required=["atmega328", "i2c_header"])),
    ("C14", "外部姿态维护口", "external_header", ["esp32", "mpu6050", "i2c_header"],
     ["3.3V I2C", "外部接口共地", "本地与排针共网"],
     "3.3V ESP32 接 MPU6050，SDA/SCL 还引到四针 I2C 排针，排针提供 3.3V 和 GND；本地去耦和总线上拉电阻齐全。",
     task("negative", "ESP32 与 MPU6050 都使用 3.3V I2C；四针 I2C 排针提供 3.3V，却把它的 GND 悬空。", rules=["EXTERNAL_GROUND_OMITTED"]),
     task("ambiguous", "ESP32 接 MPU6050 并引出 I2C 排针，外部口提供什么电压尚未决定。", missing=["external_supply_voltage"])),
    ("C15", "气压子板信号 ESD", "signal_protection", ["atmega328", "bmp280", "i2c_header", "esd_diode"],
     ["3.3V I2C 外部口", "两线分别 ESD 保护", "保护器件共地"],
     "3.3V ATmega328P 和 BMP280 的 I2C 引出四针排针，SDA、SCL 各加 ESD 二极管保护，保护器件回到公共地，传感器有去耦电容。",
     task("ambiguous", "3.3V ATmega328P 接一个 I2C 传感器，外部排针需要 ESD 二极管保护，传感器型号未定。", missing=["device_model"], required=["atmega328", "i2c_header", "esd_diode"]),
     task("unsupported_model", "3.3V ESP32 接 SHT31 的 I2C，排针的 SDA/SCL 都加 ESD 二极管，禁止替换传感器。", models=["SHT31"], required=["esp32", "i2c_header", "esd_diode"])),
    ("C16", "显示子板保护阵列", "signal_protection", ["esp32", "oled_ssd1306", "i2c_header", "tvs_array"],
     ["3.3V I2C 外部口", "使用多路 TVS 阵列", "阵列接公共地"],
     "3.3V ESP32 与 SSD1306 OLED 共用 I2C，并引出四针排针；SDA、SCL 用 TVS 阵列保护，阵列 GND 接地，器件分别去耦。",
     task("negative", "3.3V ESP32、SSD1306 OLED 与 I2C 排针加 TVS 阵列保护，把阵列 GND 直接接 3.3V 电源。", rules=["PROTECTION_GROUND_WRONG"]),
     task("negative", "3.3V ESP32、SSD1306 OLED 和 I2C 排针有 TVS 阵列，阵列 GND 必须悬空且禁止连接公共地。", rules=["PROTECTION_GROUND_OMITTED"])),
    ("C17", "ATmega 复位监控气压板", "supervised_reset", ["atmega328", "bmp280", "voltage_monitor"],
     ["3.3V 电压监控", "复位信号进入主控", "I2C 与供电分网"],
     "3.3V ATmega328P 用 I2C 接 BMP280，增加电压监控器输出复位信号，监控器和两只芯片共地并分别去耦，总线有上拉电阻。",
     task("ambiguous", "3.3V 主控用 I2C 接 BMP280，增加电压监控器复位，主控型号未定。", missing=["controller_model"], required=["bmp280", "voltage_monitor"]),
     task("unsupported_model", "3.3V ATmega328P 接 BMP280，复位监控必须用 TPS3839，型号不得换。", models=["TPS3839"], required=["atmega328", "bmp280"])),
    ("C18", "ESP32 使能控制显示板", "enable_control", ["esp32", "oled_ssd1306", "pushbutton"],
     ["3.3V I2C", "EN 上拉", "按钮对 EN 拉低用于复位"],
     "3.3V ESP32 通过 I2C 接 SSD1306 OLED，EN 用电阻上拉，复位按钮按下将 EN 拉到地，各芯片去耦并共地。",
     task("negative", "3.3V ESP32 通过 I2C 接 SSD1306 OLED，要求 EN 永久短接 GND，但仍要主控运行。", rules=["MCU_ENABLE_HELD_INACTIVE"], required=["esp32", "oled_ssd1306"]),
     task("ambiguous", "ESP32 接 SSD1306 OLED 的 I2C，需要按钮复位，但供电电压待定。", missing=["bus_voltage"])),
    ("C19", "ATmega 模拟供电气压板", "mcu_power_pins", ["atmega328", "bmp280", "capacitor"],
     ["3.3V VCC/AVCC 都供电", "AREF 按方案配置", "I2C 正确脚"],
     "ATmega328P 的 VCC 和 AVCC 都接 3.3V，各放 100nF 电容，AREF 按外部参考方案保留；PC4/PC5 接 BMP280 I2C 并共地。",
     task("ambiguous", "3.3V 单片机接 BMP280 的 I2C，还需要模拟参考端，主控型号没有选。", missing=["controller_model"], required=["bmp280"]),
     task("unsupported_model", "用 ATmega4809 和 BMP280 做 3.3V I2C 控制板，不能换 ATmega328P。", models=["ATmega4809"], required=["bmp280"])),
    ("C20", "气压板状态指示支路", "status_branch", ["atmega328", "bmp280", "led", "resistor"],
     ["3.3V I2C", "LED 独立限流", "指示支路不干扰总线"],
     "3.3V ATmega328P 用 I2C 接 BMP280，并以 PB0 经限流电阻驱动一个 LED 指示灯；总线上拉电阻与去耦电容齐全。",
     task("negative", "3.3V ATmega328P 接 BMP280 的 I2C，另用 PB0 直接接 LED，不允许限流电阻。", rules=["LED_CURRENT_LIMIT_OMITTED"], required=["atmega328", "bmp280", "led"]),
     task("ambiguous", "ATmega328P 接 BMP280 的 I2C，加 LED 状态支路，但供电电压未确定。", missing=["bus_voltage"], required=["atmega328", "bmp280", "led"])),
    ("C21", "保险与反接保护气压板", "protected_input", ["fuse", "schottky", "ldo_3v3", "atmega328", "bmp280"],
     ["5V 经保险丝与肖特基保护", "LDO 后逻辑 3.3V", "I2C 同域"],
     "5V 输入先过保险丝和肖特基二极管，再经 AMS1117-3.3 给 ATmega328P 与 BMP280 供电，I2C 上拉到 3.3V并共地，输入输出电容齐全。",
     task("negative", "5V 保险丝与肖特基后接 AMS1117-3.3，再接 ATmega328P 和 BMP280；把 LDO 的 OUT 和 GND 短接。", rules=["REGULATED_SUPPLY_SHORT"])),
    ("C22", "Type-C 输入姿态板", "type_c_input", ["usb_c", "ldo_3v3", "esp32", "mpu6050"],
     ["Type-C 仅作 5V 输入", "3.3V 控制域", "接口配置待器件复核"],
     "Type-C 仅作 5V 电源输入，经 AMS1117-3.3 供 ESP32 与 MPU6050 的 3.3V I2C；电源地共地，LDO 与芯片各有电容。",
     task("unsupported_model", "Type-C 5V 输入经 AP2112K-3.3 给 ESP32 与 MPU6050 的 3.3V I2C 供电，LDO 型号不可换。", models=["AP2112K-3.3"], required=["usb_c", "esp32", "mpu6050"])),
    ("C23", "电源时序姿态显示板", "power_sequence", ["power_sequencer", "esp32", "mpu6050", "oled_ssd1306"],
     ["3.3V 受控上电", "ESP32 EN 与有效电源关联", "I2C 共地"],
     "3.3V 输入经电源时序器给 ESP32、MPU6050 和 SSD1306 OLED 供电；上电有效后使能主控，共享 I2C 总线有上拉电阻与器件去耦。",
     task("ambiguous", "3.3V 电源时序器给 ESP32、MPU6050、SSD1306 OLED 的 I2C 板供电，电源好信号与 EN 的使能时序尚未确定。", missing=["power_enable_sequence"])),
    ("C24", "外部未知传感扩展口", "external_unknown_device", ["esp32", "i2c_header"],
     ["主控 3.3V", "外部四针 I2C", "外部器件需明确型号后验收"],
     "3.3V ESP32 引出 SDA、SCL、3.3V、GND 的四针 I2C 排针扩展口，主控去耦，接口总线上拉电阻齐全；暂不安装从设备。",
     task("ambiguous", "3.3V ESP32 的四针 I2C 排针要接外部传感器，但传感器型号与引脚还没决定。", missing=["external_device_model"], required=["esp32", "i2c_header"])),
    ("C25", "两主控共享总线审核", "multi_controller", ["atmega328", "esp32", "bmp280"],
     ["两个主控 3.3V", "声明多主协议", "单一上拉组"],
     "ATmega328P 与 ESP32 都使用 3.3V，以多主 I2C 协议共享 BMP280；PC4/GPIO21 共 SDA、PC5/GPIO22 共 SCL，只有一组上拉电阻并共地。",
     task("ambiguous", "ATmega328P 与 ESP32 都用 3.3V，共同控制 BMP280 的 I2C，但多主仲裁协议与主控分工尚未确定。", missing=["multi_controller_arbitration"])),
    ("C26", "未知主控受控替换", "controller_substitution", ["atmega328", "bmp280", "i2c_header"],
     ["明确 ATmega328P 3.3V", "气压与排针同总线", "型号不得静默替换"],
     "使用 3.3V ATmega328P 接 BMP280，并加四针 I2C 排针；主从芯片和排针共地，总线上拉电阻与芯片去耦齐全。",
     task("unsupported_model", "必须用 nRF52840 在 3.3V 下接 BMP280 与四针 I2C 排针，不接受 ATmega 或 ESP32 替代。", models=["nRF52840"], required=["bmp280", "i2c_header"])),
    ("C27", "总线电源隔离约束", "bus_domain_boundary", ["atmega328", "bmp280", "ldo_3v3"],
     ["5V 只到 LDO 输入", "MCU/传感/上拉全 3.3V", "地连续"],
     "5V 仅进入 AMS1117-3.3 的 IN，ATmega328P、BMP280 和 I2C 上拉电阻全部使用其 3.3V OUT，所有地相连，各域清楚标注。",
     task("negative", "AMS1117-3.3 从 5V 得到 3.3V 给 ATmega328P 和 BMP280，I2C 上拉却改用另一根 5V 电源且禁止电平转换。", rules=["I2C_PULLUP_WRONG_DOMAIN"])),
    ("C28", "未知电平转换器审核", "level_conversion", ["atmega328", "bmp280", "i2c_header"],
     ["本期基线全部 3.3V", "外部口电压明确", "跨域需另行选型"],
     "本期使用全 3.3V 的 ATmega328P 与 BMP280 I2C，外部四针排针也输出 3.3V，各信号一只上拉电阻并共地，不做跨域。",
     task("unsupported_model", "ATmega328P 一侧 5V、BMP280 一侧 3.3V，I2C 中间必须放 PCA9306 并引出四针排针。", models=["PCA9306"], required=["atmega328", "bmp280", "i2c_header"])),
    ("C29", "逻辑引脚合同审核", "logical_pin_contract", ["atmega328", "bmp280"],
     ["PC4/PC5 合法逻辑引脚", "不臆造 SDA 主控脚", "3.3V I2C"],
     "3.3V ATmega328P 的 PC4 接 BMP280 SDA、PC5 接 SCL；VCC/AVCC 供电、地连接，加入两只上拉电阻与去耦电容。",
     task("negative", "3.3V ATmega328P 接 BMP280 的 I2C，主控必须使用不存在于目录的 SDA 和 SCL 引脚名，不能用 PC4/PC5。", rules=["UNSUPPORTED_LOGICAL_PIN"])),
    ("C30", "未知传感阵列审核", "sensor_substitution", ["esp32", "bmp280", "mpu6050"],
     ["3.3V 已支持传感阵列", "I2C 一组上拉", "目录外阵列单独拒绝"],
     "3.3V ESP32 共接 BMP280 和 MPU6050 的 I2C 阵列，GPIO21/22 做 SDA/SCL，各从设备分别去耦，共用一组上拉电阻。",
     task("unsupported_model", "3.3V ESP32 通过 I2C 同时读取 BME688 与 BMI270，两只型号必须保留。", models=["BME688", "BMI270"], required=["esp32"])),
]

# Use broad shared families as well as the detailed scenario subtype so that
# variant topology families are not disguised as independent held-out families.
BROAD_FAMILIES = {
    "single_sensor": ("C01", "C02", "C19", "C29"),
    "sensor_display": ("C03", "C04", "C05", "C06", "C07", "C08", "C09", "C18", "C25", "C30"),
    "regulated_board": ("C10", "C11", "C12", "C21", "C22", "C23", "C27"),
    "external_header": ("C13", "C14", "C15", "C16", "C24", "C26", "C28"),
    "supervised_reset": ("C17",), "status_branch": ("C20",),
}


def build_dataset():
    cases, tasks = [], []
    for index, row in enumerate(SCENARIOS):
        cid, title, family, parts, constraints, explicit, *variants = row
        split = "dev" if index < 20 else "holdout"
        broad_family = next(name for name, members in BROAD_FAMILIES.items() if cid in members)
        cases.append({"case_id": cid, "title": title, "related_family": broad_family,
                      "scenario_subtype": family,
                      "split": split, "intended_parts": parts, "requirements": constraints,
                      "source": "internal_original_requirement", "has_reference_circuit": False})
        entries = [task("explicit", explicit), *variants]
        for number, item in enumerate(entries, 1):
            category = item["category"]
            required = item["required_types_override"]
            fields = {key:value for key,value in item.items() if key != "required_types_override"}
            tasks.append({"task_id": f"{cid}_T{number}", "case_id": cid, "split": split,
                          **fields, "expected_disposition": {"explicit":"plan", "ambiguous":"clarify",
                          "unsupported_model":"reject", "negative":"reject"}[category],
                          "required_types": parts if required is None else required,
                          "forbidden_types": (["i2c_header"] if "i2c_header" not in parts else []) +
                                             (["led"] if "oled_ssd1306" in parts and "led" not in parts else [])})
    return {"metadata": {"schema_version":"2.0", "dataset_kind":"internal_original_requirements",
        "created_on":"2026-09-26", "scenario":"5V/3.3V MCU 与 I2C 传感接口子板",
        "source_license":"project_original", "external_examples_imported":0,
        "reference_circuit_count":0, "case_group_count":30, "task_count":80,
        "holdout_task_count":20, "split_policy":"同一案例的任务只在一个划分中；相关族可能跨划分",
        "training_use":"未用于训练", "holdout_status":"内部预留分区，非独立盲测",
        "T03_external_circuit_goal_met":False,
        "limitations":["内部原创需求合同，未附30个独立外部电路或已验证网表",
        "任务变体属于同一案例；30个场景不能当成30个真实独立泛化样本",
        "元件参数与最终电路仍需电子工程师和具体器件数据手册复核",
        "留出分区按案例组隔离，但同族场景可能重叠且开发者已查看标注"]},
        "cases":cases, "tasks":tasks}


if __name__ == "__main__":
    destination = Path(__file__).with_name("requirements.json")
    dataset = build_dataset()
    destination.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raw = json.dumps(dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest = {"dataset_sha256":hashlib.sha256(raw).hexdigest(), "split_policy":dataset["metadata"]["split_policy"],
                "reference_circuit_count":0, "T03_complete":False,
                "partitions":{split:{"case_ids":[c["case_id"] for c in dataset["cases"] if c["split"] == split],
                              "task_ids":[t["task_id"] for t in dataset["tasks"] if t["split"] == split],
                              "related_families":sorted({c["related_family"] for c in dataset["cases"] if c["split"] == split})}
                              for split in ("dev", "holdout")}}
    destination.with_name("split_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(destination)
