# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)（SemVer）。

## [1.4.1] - 2026-10-22

### Fixed
- Linux CI 上 Windows 专用的 taskkill 进程树测试未跳过，导致 `--profile fast` 失败。
- Windows CI 控制台默认 cp1252，校验脚本输出中文时触发 `UnicodeEncodeError`。

## [1.4.0] - 2026-10-22

### Added
- **教学模式**：术语解释、学习路径、8 项门禁"为什么"、新手引导与 31 类元件教学卡；默认关闭，只影响呈现，不改变电路与校验结果。
- **可调整布局**：左栏、右栏与底部抽屉支持拖拽，尺寸记忆到 `zht-layout`，窄屏保持原布局。
- **教学瞬态时序图**：接入 wavedrom 3.7.0（MIT），新增 `GET /api/spice_transient`，图形来自真实 `.tran` 输出。
- **开源项目化**：重写 README，新增 CONTRIBUTING / CODE_OF_CONDUCT / SECURITY / issue 与 PR 模板，并建立 `docs/优化清单.md`。
- **持续集成**：新增 GitHub Actions，覆盖 Ubuntu / Windows 与 Python 3.10 / 3.12。

### Changed
- 测试编排新增 `release` profile，交付打包套件由 `fast` 移出。
- 新克隆所需的小夹具入库；大数据集与 7-Zip 改为按能力跳过。
- README 配置表与 `core/config.py` 默认值对齐；旧交付记录归档到 `docs/交付与验收记录.md`。

### Fixed
- 新克隆执行 `--profile fast` 时 8 个套件因缺夹具失败。
- 外部导入读取 `evidence/` 的路径错误，改为 `data/external_import/acquisition.json`。
- 清理文档中 77 处指向 `evidence/` 的死链与 12 处相对路径死链。
- 删除 3 个空壳 ERC 检查函数，使定义数与注册数一致（33）。
- ngspice 分页表头导致 `.tran` 数据只解析前 55 行，现按完整 123 行解析。

## [1.3.0] - 2026-09-20

### Added
- 新增产品版本一致性回归测试，覆盖运行时、README、变更日志、API 文档与解决书构建元数据。

### Changed
- 根目录 `VERSION` 成为唯一产品版本源，`GET /api/health` 与解决书构建脚本统一从版本模块读取。

## [1.2.1] - 2026-09-11

### Added
- **KiCad 官方库批量导入草稿** core/kicad_batch_import.py：按教学白名单导出中文对照 Markdown + JSON（data/kicad_drafts/）
- 命令：python -m core.kicad_batch_import（默认教学过滤；--full 关闭）

## [1.2.0] - 2026-09-11

### Added
- **器件库扩至 117 种**：电解电容 / TVS / 光敏三极管 / 通用运放 / 磁珠 / 压敏 / 晶闸管 / 无源蜂鸣器 / 电流表 / 电压表 + 约 70 类教学模块件
- **专用 SVG / KiCad 符号精修**：70+ 新件从占位框改为教学标准画法；KiCad 自产符号库 32 → 94
- **ERC 额定值告警** `core/erc_ratings.py`：LED 过流、电阻功耗、7805 压差、电容耐压
- **准仿真引擎** `core/simulate.py`：LED 电流 / 分压 / 稳压功耗直流工作点
- **SPICE 导出** `core/spice.py`：ngspice 兼容 `.cir` / `.subckt`，前端工具条按钮
- **BOM 导出** `core/bom.py`：同值合并、CSV（Excel）/ JSON，前端工具条按钮
- **工程惯例补全**：缺失按键上拉自动写回 10kΩ
- **拓扑模板 10 → 20 套**：RC 滤波 / 整流滤波 / PNP 开关 / 电压跟随 / 晶振 / 光耦 / 降压模块 / H 桥 / CD4017 / DHT11
- **基准用例 40 → 55**：自由表达口语用例 10 → 25 条
- **健康检查** `GET /api/health`：版本 / Key / 默认模型 / kicad-cli / features
- **UI**：画布空态骨架、工具条 SVG 图标、生成加载态、ERC 明细面板、精确性卡片、准仿真/额定值阶段、属性面板关闭、reduced-motion
- **模型预设**：DeepSeek 默认 `deepseek-flash`（官方 V4.1）；通义 / GLM / Kimi / OpenAI 升至 2026-09 最新 ID
- **KiCad 官方库检索** `core/kicad_lib.py`：本机官方库搜索 / 草稿导入
- **版本号** `VERSION` 文件与 `/api/health` 透出

### Changed
- DeepSeek 空 content：官方端点默认 `thinking=disabled`；`finish_reason=length` 时自动抬升 max_tokens
- 提示词：规划强制欧姆定律算式；NE555 黄金骨架 few-shot；输出前自检清单
- 「自动修复」更名为「工程惯例补全」
- 作品简介 / 说明书按官网最新能力重写

### Fixed
- 网络标签避让（不压元件盒）
- 连线模式引脚命中可视化
- KiCad 导出 / 额定值 / 拓扑相关回归

## [1.1.0] - 2026-09-11

### Added
- 参数精确化：LED 限流 E12、工程估算说明
- 拓扑黄金模式校验
- 器件规格库 `core/specs.py`（16 类额定值与封装）

## [1.0.0] - 2026-09-10

### Added
- 首个可用版本：中文生成 → ERC → 自动布局 → SVG / KiCad 导出
- 18 套件离线回归（后增至 21 / 22 / 24）
