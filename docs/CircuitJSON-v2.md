# CircuitJSON v2 工程元数据合同

CircuitJSON v2 保留 v1 的 `parts` 与 `connections` 结构。现有渲染、ERC 和 KiCad 原理图流程继续读取这两项；v2 额外记录制造与证据所需的已核实信息。机器可读合同见 `GET /api/schema/circuitjson?version=2` 或 `core.circuit_schema.get_circuit_json_schema(2)`。

## 与 v1 的关系

- v1 仍是默认 HTTP Schema 和当前生成格式；`GET /api/schema/circuitjson` 返回 v1。
- `migrate_v1_to_v2(circuit)` 和 `POST /api/circuit/migrate` 无损复制 v1 图，设置 `version: 2`。缺失的器件型号、封装、引脚到焊盘映射和电压不会推测。
- 已为 v2 的图再次迁移只返回副本。输入缺少 `parts`/`connections` 数组或版本不受支持时拒绝。
- `version: 2`、`parts`、`connections` 是 v2 的必填项；新工程字段均可选，因此旧图可以先迁移再逐步补充证据。

## 新字段

| 位置 | 字段 | 含义 |
|---|---|---|
| `parts[]` | `component.manufacturer`、`component.mpn`、`component.symbol`、`component.footprint`、`component.datasheet` | 经资料或 KiCad 库核实的具体器件身份。目录通用类型不等于具体料号。 |
| `parts[]` | `component.pin_map` | 原理图引脚名到实际封装焊盘号的映射，例如 `{"VCC":"7"}`。必须按选定料号验证。 |
| `parts[]` | `component.pin_map_source` | 引脚映射的手册、连接器规格或库来源；真实 PCB 合同要求提供可核查的来源。 |
| `parts[]` | `component.pin_types` | 具名引脚的 KiCad 电气类型，例如 `{"VCC":"power_in"}`；真实有源器件的源原理图要求完整提供并按手册核实。 |
| `parts[]` | `supply_domain` | 所属供电域的 ID，与顶层 `voltage_domains[].id` 对应。 |
| `parts[]` | `pcb` | 已确认的 `x_mm`、`y_mm`、`rotation_deg` 和板面 `front/back`。 |
| 顶层 | `voltage_domains` | 显式供电域及额定电压范围；未知电压留空。 |
| 顶层 | `board` | 双层板宽高、最小间距和最小线宽等约束。 |
| 顶层 | `provenance` | 来源类别、原始记录 ID、模型 ID、置信度和迁移前版本。 |

Schema 只检查数据形状。引脚确实存在、焊盘号正确、额定值与电压域兼容、物理间距和连线正确，都须由器件库、数据手册、ERC、KiCad 和 PCB 验证模块分别确认。没有 `component.pin_map` 的 MCU/传感器可用于原理图草案，但不能直接晋级为已验证 PCB。

## 无损迁移示例

请求：

```json
{"circuit":{"version":1,"parts":[{"type":"resistor","id":"R1","attrs":{"value":"10k"}}],"connections":[]}}
```

响应中的 `circuit` 保留原元件和连接，设置 `version: 2`，并增加 `"provenance":{"origin":"legacy","source_version":1}`。不会自动填入封装或板框。

## 后续模块接入约定

1. 文本生成与图片识别可以继续产出 v1 图；在进入工程信息补全时迁移至 v2。
2. PCB 模块只使用经过核实的 `component.footprint` 与 `component.pin_map`，不得把当前预布局占位焊盘当作真实封装。
3. 所有来源（模板、AI、图片、导入、人工修改）通过同一验证门禁，门禁未运行或工具缺失时返回 `incomplete`，不标记为通过。
4. 输出文件和评测记录保留原始来源与迁移版本，便于复现和追溯。
