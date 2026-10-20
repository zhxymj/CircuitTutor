# wavedrom source attribution

`web/wavedrom.unpkg.min.js`（同一文件亦存于 `third_party/upstream/wavedrom/`）
来自 https://github.com/wavedrom/wavedrom 的官方发布包 `wavedrom@3.7.0`
（经 `npm pack wavedrom@3.7.0` 取得，tarball shasum
`50e4b3a833d55b40b18224a72713abb5593d0918`）。

许可证：MIT，Copyright (c) 2011-2026 Aliaksei Chapyzhenka，原文见同目录
`LICENSE`（1092 字节，SHA-256
`68f6442e5967ddb6a75ec8a4570215cac8cc415ea293c05cf857843c73fa5a57`）。

## 复用范围

本项目复用该 **预构建自包含 bundle**（98928 字节，SHA-256
`e3579493c2a0c11eb0d32344ea0eadb2904a7c85ad729b28b6222ab837297742`），
通过 `window.wavedrom` 调用其 `renderAny` / `onml.stringify` / `waveSkin`
把 WaveJSON 渲染成 SVG。**未修改该文件任何字节**，未从上游源码树重新构建，
未在浏览器之外调用其 CLI。

bundle 内联了 onml、json5、bit-field、logidrom、tspan 五个运行依赖；
本项目未单独引入这些包，也未逐一完成其传递依赖的许可审查。逐项字节数与
哈希见 `third_party/wavedrom-attribution.json`。

## 项目集成代码（非上游函数）

以下是项目自身代码，不属于 wavedrom 上游，也未改编其源码：

- `core/spice_enhanced.py` 的 `_batch_node_names` / `_parse_tran_table` /
  `transient_series`：拼出带 `.print tran` 控制卡的网表、运行本机 ngspice，
  并解析其批处理输出的真实时间序列表。
- `server.py` 的 `/api/spice_transient`：返回上述结果。
- `web/workbench.js` 的 `timingLevels` / `drawTiming` / `runTiming`：把真实
  序列按各节点电压中值做阈值数字化，交给 wavedrom 渲染，并显示阈值、每格
  时长、`.tran` 采样行数与 engine。

## 边界与不声明项

- 时序图数据只来自本机 ngspice 的实际 `.tran` 打印输出；ngspice 不可用、
  无电压节点或输出无法解析时返回 `ran=false` 并写明原因，界面显示“未运行”，
  **不生成示意波形，也不以解析解替代波形**。
- 阈值数字化会丢掉模拟细节，图上标注了阈值与每格时长；该图用于教学阅读，
  不构成实测波形、电气验收或制造结论。
- 上游突变的细节、传递依赖许可与完整上游构建过程均未复现。
