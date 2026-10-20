# PCBWorld 隔离式扩展（当前为实验性）

本目录只保存“智绘电路”与外部 PCB 路由服务之间的稳定合同、可行性证据和复现说明，不复制第三方源码，也不让 A 主线依赖 PCBWorld。

## 当前结论

2026-09-19 的时间盒探测未通过六项可行性门。WSL2 能运行 Linux x86_64 / Python 3.12.3，并能识别 NVIDIA GeForce RTX 4060 Laptop GPU（8188 MiB），但当前执行环境无法从 GitHub 获取源码，且 WSL 中没有 conda、CMake、Ninja。因此没有构建 Engine、没有执行路由动作、没有获得 KiCad DRC 奖励，也没有启动 PPO 训练。

这不是“训练失败后的坏成绩”，而是“前置环境门未通过”。项目据此采用以下降级：

- 已实现 `core/routing_job.py`：任务 schema、双层范围约束、奖励公式、静态几何评价、失败回退和原子 JSON 写入；
- 已实现 `POST /api/routing_job`：只校验并转交任务，不在核心进程内启动 WSL/PPO；
- 已保留现有规则预布局作为稳定回退；
- 布通率、网络完成率和 DRC 违规数在未测量时写为 `null`，不写成 0；
- 不报告 PPO 提升比例，不生成虚假检查点。

逐项结果见 [feasibility_result.json](feasibility_result.json)，固定合同见 [routing_job.example.json](routing_job.example.json)。

## 第三方来源与许可证

- [PCBWorld](https://github.com/LGAI-Research/PCBWorld)：官方 README 标注 v1.0.1、Python 3.12+、Linux x86_64 为主要平台，环境/训练/评测代码采用 BSD-3-Clause。
- [PCBWorld-Engine](https://github.com/LGAI-Research/PCBWorld-Engine)：KiCad 修改、RL router 与 engine server，GPLv3；上游 PCBWorld 将其作为独立子模块分发。
- [PCBench](https://github.com/PCBench/PCBench)：164 个 PCB 的公开路由数据集，MIT；本次没有下载或使用其样本，因此本项目的 A 主线指标不能称为 PCBench 实验结果。

实际接入时必须保留上游 LICENSE、Notice.md 和 Engine 的 GPLv3 声明；不得将 Engine 改写或标注为本项目原创闭源组件。

## 环境恢复后的最小复验

以下命令仅是下一次可行性门的复验顺序，不表示本次已经成功运行：

```bash
git clone --recursive https://github.com/LGAI-Research/PCBWorld.git pcbworld
cd pcbworld
bash tools/setup/setup_all.sh
conda activate pcbworld
export PYTHONPATH=build_rl/pcbnew/python/rl:.
export PCBWORLD_DATA_ROOT="$PWD/var/datasets"
python - <<'PY'
from pcb_world.core.env import PCBWorld
env = PCBWorld(
    board_path="tests/fixtures/simple_routing_board.kicad_pcb",
    max_steps=20,
)
obs, info = env.reset(seed=1550)
print(type(obs), sorted(info))
PY
```

只有继续完成“一次合法路由动作返回 reward 与 DRC 信息”和“一次短 PPO 训练产生真实日志/检查点”后，才允许把对应门槛从 `skip` 改为 `pass`。

## 与核心程序的边界

任务输入为 `routing_job.json`，输出必须包含：完全布通状态、网络完成率、KiCad DRC 违规数、总线长、过孔数、推理耗时、回退标志、模型版本和随机种子。任何外部超时或异常均保留原板并返回结构化失败原因，不能静默吞掉异常。

