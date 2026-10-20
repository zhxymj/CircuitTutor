## 改了什么

<!-- 一句话说清目的，以及为什么 -->

## 怎么验证的

<!-- 跑了哪些测试/命令，看到了什么结果 -->

```
python tests/run_tests.py --profile fast
```

## 还没验证的 / 已知限制

<!-- 如实写出没跑的、跑不了的、以及边界情况 -->

## 自查清单

- [ ] 没有引入新的运行时依赖（前端无框架/CDN，后端仅标准库）
- [ ] 跑通相关测试（`--profile fast`，涉及 EDA 时加 `--profile eda`）
- [ ] 涉及前端时跑通 `node tests/test_frontend.js` 与 `node tests/test_workbench_ui.js`
- [ ] 结论如实：未运行的工具/检查显示"未完成"，没有把估算当结论
- [ ] 涉及第三方代码时已更新 `third_party/` 归因文件与 `docs/开源项目集成.md`
- [ ] 未把 `.env`、密钥、`evidence/` 等本地产物带入提交
- [ ] 界面变化附了截图
