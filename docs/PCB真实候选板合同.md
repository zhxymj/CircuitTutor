# 真实封装 PCB 候选板合同

`core.pcb_real.build_pcb_candidate()` 生成 **未布线** 的 KiCad 双层候选板和逐焊盘审计记录，默认目标为 KiCad 8；显式传入 `target_version=20260206` 可使用 KiCad 10 封装。它读取本地 `.kicad_mod` 文件中的焊盘编号、尺寸、孔径、位置和层。旧 `core.pcb_layout.to_kicad_pcb()` 仍是可视模型，其返回值明确标为 `visual_mockup`。

## 必需输入

- CircuitJSON `version: 2`，含 `parts`、`connections`。
- `board`: `width_mm`、`height_mm`、`layers: 2`、`min_clearance_mm`、`min_trace_width_mm`。
- 每个物理元件的 `component`: `mpn`、`datasheet`、`footprint`（`库名:封装名`）、`pin_map`（原理图引脚名 → 焊盘号）、`pin_map_source`（核对依据）。这些字段是来源声明，程序不会替电子工程师核对数据手册。
- 每个物理元件的 `pcb`: `x_mm`、`y_mm`，可选 `rotation_deg`（0、90、180、270）和 `side: front`。
- `footprint_roots` 参数指向含 `*.pretty` 目录的本地 KiCad 封装库根目录；省略时读取环境变量 `KICAD_FOOTPRINT_DIR`。

调用示例：

```python
from core.pcb_real import PCBContractError, build_pcb_candidate

try:
    result = build_pcb_candidate(circuit, footprint_roots=[r"D:\KiCad\share\kicad\footprints"])
except PCBContractError as exc:
    print(exc.code, str(exc))
else:
    board_text = result["board_text"]
    pad_audit = result["pad_audit"]
```

没有本地封装、焊盘号不一致、连接引脚未映射、缺少来源、器件越过板边界等情况会抛出 `PCBContractError`。这个生成函数的 `routing`、`drc`、`gerber`、`manufacturing` 均为 `unavailable`。另有 `core.pcb_manufacturing.run_manufacturing_pipeline()` 对实际板文本执行本地封装/网络核对、官方 KiCad DRC 和制造候选文件导出，见 `docs/PCB制造候选管道.md`。没有通过该管道和人工复核的板不能发厂。

回归命令：

```powershell
python -m unittest tests.test_pcb_real -v
python tests/test_footprints_pcb.py
```

测试用焊盘几何来自 KiCad 8.0.0 官方库，来源和 CC BY-SA 4.0 许可证见 `tests/fixtures/pcb_real/README.md`。测试中的料号与数据手册字段仅是 fixture 标识，不构成可采购器件证据。
