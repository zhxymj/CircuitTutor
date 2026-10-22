# 贡献指南

感谢你愿意改进智绘电路。本文说明本项目的开发约定，以及提交改动前需要满足的条件。

## 开始之前

本项目**运行时零第三方依赖**，只用 Python 3.10+ 标准库与原生 JavaScript。这是刻意维持的约束，请勿引入新的运行时依赖（含前端框架、打包器、CDN 资源）：

```bash
python server.py          # 启动本地服务，默认 http://127.0.0.1:8765/
```

可选工具（不装也能跑，只是对应门禁显示"未完成"）：`kicad-cli`、`ngspice`。

## 提交改动前

1. 跑通相关测试。统一入口按能力分组：

   ```bash
   python tests/run_tests.py --profile fast      # 快速核心，建议每次提交前都跑
   python tests/run_tests.py --profile eda       # 涉及电气/EDA 时
   python tests/run_tests.py --profile release   # 涉及交付打包时（需本地 7-Zip）
   python tests/run_tests.py --profile all       # 大改动
   ```

2. 涉及前端的改动再跑 Node 契约测试：

   ```bash
   node tests/test_frontend.js
   node tests/test_workbench_ui.js
   ```

3. 新增行为请配套测试。Python 测试放在 `tests/test_*.py`，前端契约放在 `tests/test_*.js`。

## 代码约定

- **只改与本次任务相关的代码**，不做顺手重构。
- 后端模块放在 `core/`，HTTP 路由统一在 `server.py`；新增静态资源需要在 `server.py` 的 `ALLOWED_STATIC` 白名单中登记。
- 前端保持原生 JS：`web/workbench.js` 负责工程台外壳，`web/app.js` 持有电路状态。新增 DOM 不要复用小范围已废弃的 ID。
- 面向用户的文案用中文，风格与现有界面一致。
- 注释解释"为什么"，不复述代码在做什么。

## 结论必须如实

本项目的可信度来自不夸大。以下规则属于硬约束，违反的 PR 不会被合并：

- 没有实际运行的工具或检查，必须显示**未完成**，不得当作通过。
- 模板回退不等于 AI 生成成功；预览完成不等于通过电气验收。
- 制造类输出保持 `manufacturing_release=false`。
- 不编造波形、不补默认值掩盖缺失信息；无法完整转换时整份拒绝。
- 涉及第三方代码时，必须同步更新 `third_party/` 下的归因文件与 `docs/开源项目集成.md`，写明固定版本与许可事实；许可不明确的上游不得引入。

## 提交与 PR

- 提交信息用祈使句，说明**为什么**改，例如：`fix: 修正画布留白导致的点选坐标偏移`。
- 一个 PR 聚焦一件事；说明改了什么、怎么验证的、还有什么没验证。
- 涉及界面变化请附截图。
- 提交前请确认没有把 `.env`、密钥、`evidence/` 等本地产物带入提交。

## 许可

提交贡献即表示你同意以本项目的 [MIT 许可](LICENSE) 发布你的贡献。
