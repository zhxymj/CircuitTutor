# 内部原创结构化需求集 v2

本目录包含 **30 个内部场景组、80 条中文文本任务**。20 个开发场景组对应 60 条任务；10 个预留场景组对应 20 条任务。类别分布为明确需求 30 条、歧义需求 20 条、目录外型号 15 条、负例 15 条。

预留分区包含明确需求 10 条、歧义需求 3 条、目录外型号 4 条、负例 3 条。开发和预留分区共享 `single_sensor`、`sensor_display`、`regulated_board`、`external_header` 四个相关家族，报告显式保留这一重叠。

所有文本和需求约束由本次项目改造原创编写，未读取既有电路模板来生成变体，未复制第三方图纸、代码或数据。`source_license: project_original` 表示无第三方内容引入，不是另行发布的新许可证。

## 数据界限与 T03 状态

这些是需求合同，**不是 30 个经过验证的外部独立电路**。没有附带参考原理图、参考网表或工程师签核结果。任务变体留在同一场景组，避免把同一案例同时放入开发集和留出集；预留分区仍由开发者编写并查看，不是独立盲测。相近族场景也不能作为真实泛化分布。

因此内部文本数量达到本目录目标，但 **T03 外部电路案例验收未达标，T03 未完成**。后续需在来源和许可证逐例记录后引入适用的外部参考电路，并按照原设计来源/家族分组隔离划分。元件参数和目标电路须用具体型号数据手册及电子工程师复核。

## 文件与标注

- `build_dataset.py`：逐条保存原创场景与原始任务文本；只写本目录的 `requirements.json`，不读取模板或外部文件。
- `requirements.json`：结构化数据。`cases` 是场景组，`tasks` 引用场景组且继承同一划分。
- `offline_report.json`：本次确定性预规划的逐任务记录；含成功与失败，不含在线生成结果。
- `split_manifest.json`：数据指纹、场景与任务划分索引。

每条任务有 `category` 和 `expected_disposition`。`required_types` 表示该文本明确指定的受支持元件类型；`missing_fields`、`unsupported_models`、`negative_rule_ids` 分别标注歧义、目录缺口和危险/矛盾要求。负例规则标识属于需求层标注，不能当作现有 ERC 已实现规则。

明确需求预期进入规划；歧义需求预期澄清；目录外型号与负例预期拒绝或定位问题。标签表达期望处理行为，**不表示当前代码已经完成相应能力**。

## 复现

在“智绘电路”目录执行：

```powershell
python data/requirements_v2/build_dataset.py
python -m evaluation.requirements_v2 --output data/requirements_v2/offline_report.json
python tests/test_requirements_v2.py
```

评测只运行本地目录检索、规则澄清和确定性预规划。`structure_valid`、`erc_passed`、`template_fallback` 均为空，`model_calls` 为 0。目录外型号或 `requirement_blockers` 优先触发拒绝；负例记录预期与命中的需求规则标识、文本证据与位置。`negative_rule_detection` 只表示文本规则覆盖相应标签，不是 ERC 效果。

规则覆盖短接、错误上拉、明确禁止必需器件等有限措辞；对隔离器、电平转换、恒流驱动、地址复用等声明的例外做保守处理。没有完整解析电路，也没有核对具体型号的容限、电流、热或元件参数。规则开发使用了本集标签，预留分区亦由开发者查看，因此命中数不能称为独立泛化表现。目录命中和调度匹配不能计为 AI 电路生成成功率。
