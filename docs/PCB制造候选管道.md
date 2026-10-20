# PCB 制造候选管道与内部样例

## T09/T10 当前核查（2026-09-29）

分母按板型分开：被动排针/上拉适配板有 10 个内部样例，已有 KiCad 10.0.6 官方 DRC 及 parity 通过证据为 **10/10**，但它们不含板载 MCU。现有 ATmega328P-PU **5 V** I2C/自定义 ISP 内部有源板的官方 KiCad 10.0.6 门禁通过数为 **1/1**。本板没有板载 I2C 传感器，也不是目标 3.3 V MCU+I2C 传感器板；该目标场景目前 **0 个实测板，尚未覆盖**。两类板的分母不能相加冒充目标场景验收。制造放行数仍为 **0**。

本次从当前源码重新生成的单例证据在 `data/pcb_active/run_b5fa57c4e23c4e919414d3432d2dea58`。完整 T05 源门禁通过；官方源原理图 ERC **0** 项，官方网表与声明网络差异 **0**。KiCad 10.0.6 对实际填充铜区后的板执行 DRC，填充报告 **0** 项违规、**0** 个未连接项；制造管道另执行 `--schematic-parity`，正式报告为 **0** 项违规、**0** 个未连接项、**0** 个原理图一致性差异。本地物理焊盘/网络审计为 **52 个焊盘、9 个已连接网络**，导出 **12 个 CAM/BOM 文件**。状态为 `fabrication_candidate_for_review`，`manufacturing_release: false`，仍待电子工程师与板厂复核。该板未做固件、熔丝、波形、装配或实物验证；缺少 MCU/接口 SPICE 模型，SPICE 对该板记为 `not_applicable`，不算仿真通过。

内部语义门禁另留有 3 条非阻断提示：J1/J2 的 VCC 同接一网被通用规则视作“并联供电输出”，需按此例两个外部连接口的实际供电方式和防反灌要求复核；U1:XTAL1 悬空对应声明的内部 RC 时钟方案，需核对熔丝与固件配置；U1:VCC 被提示缺少就近 100 nF 去耦，虽板上有 C1/C2/C4，仍须核对实际焊盘位置、回流路径与器件数据手册。额定值规则还提示 R3 在假设 10 mA 下的功耗上界，需按 RESET 引脚实际电流、异常工况和电阻额定功率复核。上述提示不能由官方 ERC/DRC 计数为零自动消除。

旧有源板 `data/pcb_active/run_a4268931173446499f2478e8cd4974e9` 的 `fill-drc.json` 记录 15 项违规，属于旧布局的失败证据，不能覆盖或合并成本次成功。旧板的 R1 位于 `(36,12)`，其 VCC 焊盘与 SDA 线相交；当前源码将 R1 移至 `(36,14.3)`、R2 移至 `(36,19)`，并在填充前登记本地封装库。先前 KiCad 9 无法加载 KiCad 10 格式 PCB 的失败记录同样保留为历史运行证据。

本地单元检查命令（项目根目录）：

```powershell
python tests/test_pcb_manufacturing.py ManufacturingTests.test_active_board_sda_track_clears_vcc_resistor_pad ManufacturingTests.test_active_zone_fill_has_local_footprint_table -v
python tests/test_pcb_manufacturing.py -q
python tests/test_pcb_real.py -q
python tests/test_pcb_source_schematic.py -q
```

## 当前结果（2026-09-26）

已使用官方签名验证后的 KiCad 10.0.6，读取本地完整官方封装库，生成 10 个低压双层内部工程样例。3 种拓扑分别为单口 I2C 上拉接口、双口 I2C 上拉适配板、三口 I2C 上拉分配板。它们含真实铜线；单口板含 2 个贯通过孔。全部样例先通过完整严格 T05；每例的实际引脚源原理图官方 ERC 为 0 项、实际焊盘网表差异为 0 项，官方 DRC 全部严重级违规数为 0、未连接项为 0、`--schematic-parity` 差异为 0。每例实际生成 12 个 CAM/BOM 文件，含 Gerber、Excellon 钻孔、贴片坐标与真实器件 BOM。

这 10 个样例是 3 种拓扑的电压与布局变体，**不是外部独立评测样本**。板载器件仅为排针和上拉电阻，外部 MCU/传感器不在板上；完整有源 MCU/传感器板、通用自动布线器和实物打样验收仍未完成。没有测量 I2C 电容、速率、波形或器件装配，不能从 DRC=0 推断电路电气性能或制造放行。

逐样例及文件 SHA256 见 `data/pcb_examples/manifest.json`。每次运行保留独立 `runs/run_*` 目录；早期未登记封装库的失败报告也保留，后续通过结果没有将这些警告列为忽略项。

## 来源与映射

- 排针料号：[Samtec TSW-104-07-G-S](https://www.samtec.com/products/tsw-104-07-g-s)，4 针单排、2.54 mm 间距、0.635 mm 方针。使用本地官方 `Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical` 的真实通孔焊盘。机械配合、孔公差、焊接和采购仍待工程师核对。
- 上拉电阻：[YAGEO RC0805FR-074K7L](https://www.yageogroup.com/component-documentation/download/specsheet/RC0805FR-074K7L)，4.7 kΩ、1%、0805/2012、0.125 W，使用 `Resistor_SMD:R_0805_2012Metric`。两端无极性，映射原理图 1/2 到焊盘 1/2。
- 官方封装库：[KiCad Footprints](https://gitlab.com/kicad/libraries/kicad-footprints)，[CC BY-SA 4.0 库许可证](https://www.kicad.org/libraries/license/)。实际板文件保留官方铜焊盘与图形，逐元件记录源文件 SHA256。
- 接口顺序由内部设计明确选择：1=GND、2=VCC、3=SDA、4=SCL；不是通用线缆标准。连接外部模块时必须核对顺序、共同电压域、已有上拉和总线电容。

## 调用合同

```python
from core.pcb_manufacturing import run_manufacturing_pipeline

manifest = run_manufacturing_pipeline(
    circuit, routed_board_text, output_dir,
    cli_path=r"C:\Users\30789\Desktop\智绘电路工具链\kicad\bin\kicad-cli.exe",
    footprint_roots=[r"C:\Users\30789\Desktop\智绘电路工具链\kicad\share\kicad\footprints"],
)
```

管道先重验 CircuitJSON v2 元数据、实际嵌入焊盘几何、板框、双层约束、器件放置、MPN/厂家/数据手册、原理图实例路径和焊盘网络集合，再建立项目内真实封装库表与最小间距/线宽规则。直接调用同样不能跳过结构、内部 ERC、额定值与适用的 SPICE 运行检查；此独立报告名为 `semantic_validation`，`assurance: internal`，不代表完整官方 T05。HTTP 制造入口与样例生成器另行强制完整严格 T05，并留存其正式报告。适配板缺少 MCU/接口 SPICE 模型，SPICE 两项记录 `not_applicable`，不算仿真通过。

随后自动使用 `core.pcb_source_schematic.build_real_schematic` 产生自包含实际引脚源原理图与符号库。每个实例的符号针号直接来自 `component.pin_map`，网络名与 PCB 同源，实例路径使用同一确定性 UUID。GND/VCC 外部参考以不进入 PCB/BOM 的虚拟供电符号表示；通用概念符号的针号不用于该源。有源件还必须提供逐针 `component.pin_types`，不能推测电气类型。

管道实际运行 `sch erc --format json --severity-all --exit-code-violations`、`sch export netlist --format kicadsexpr` 并逐网络核对真实焊盘端点，然后执行 `pcb drc --format json --severity-all --schematic-parity --exit-code-violations`。报告明确记录 `official_schematic_parity_checked: true`。任一非零退出码、警告、错误、未连接、网络差异、parity 差异、报告字段缺失或验证后源板/原理图 SHA256 变化，均阻止制造导出；未知计数为 `null`。可选参数 `source_schematic` 必须逐文本等于当前电路自动生成的源，否则拒绝。

通过后状态为 `fabrication_candidate_for_review`，`manufacturing_release` 始终为 `false`。包内含复核声明、CircuitJSON、源板、实际引脚原理图/符号库/库表、官方 ERC/网表/DRC JSON、语义门禁、设计规则与 CAM 文件；需电子工程师与板厂检查后才能制造。失败时保留已有报告、源文件、逐命令输出/退出码与 `manifest.json`，不生成已通过包。历史 `official_schematic_parity_checked: false` 的旧运行不作为本轮正式证据。

## 实测失败回退

| 内部负例 | 官方结果 | 导出行为 |
|---|---|---|
| 未布线接口板（`data/pcb_examples/negative_unrouted/run_aa134e87c1b44e349c925f4ffcf12b66`） | 0 个一般违规，4 个未连接项，0 parity 差异 | 通过源检查后 DRC 拒绝，未导出制造文件 |
| 故意将 VCC 铜线接触 GND 焊盘（`data/pcb_examples/negative_deliberate_short/run_aba1598040294fa3b863de7a0b2a5f9c`） | 2 个违规，0 个未连接项，0 parity 差异 | 通过源检查后 DRC 拒绝，未导出制造文件 |
| 去掉虚拟供电/地锚点的早期源 | 完整 T05 `erc` 失败，未共地 | 制造不启动；独立目录保留源及报告 |

## 复现

在项目根 `智绘电路` 运行；使用桌面已有工具链：

```powershell
$env:KICAD_CONFIG_HOME = 'C:\Users\30789\Desktop\智绘电路工具链\kicad-config'
$env:KICAD_DOCUMENTS_HOME = 'C:\Users\30789\Desktop\智绘电路工具链\kicad-documents'
$env:KICAD_CLI_PATH = 'C:\Users\30789\Desktop\智绘电路工具链\kicad\bin\kicad-cli.exe'
$env:KICAD_FOOTPRINT_DIR = 'C:\Users\30789\Desktop\智绘电路工具链\kicad\share\kicad\footprints'
$env:NGSPICE_PATH = 'C:\Users\30789\Desktop\智绘电路工具链\ngspice\Spice64\bin\ngspice.exe'
python -m core.pcb_active_example --output data/pcb_active --footprints $env:KICAD_FOOTPRINT_DIR --cli $env:KICAD_CLI_PATH
python tests/test_pcb_manufacturing.py -q
python tests/test_pcb_real.py -q
python tests/test_pcb_source_schematic.py -q
```

CLI 参数依据：[KiCad 10.0 官方命令行文档](https://docs.kicad.org/10.0/en/cli/cli.html)；设计规则依据：[KiCad 10.0 PCB 文档](https://docs.kicad.org/10.0/zh/pcbnew/pcbnew.html)。
