# API 文档 · 智绘电路 CircuitTutor

> 说明：文中标注“本地证据”的引用指向本机验收归档目录 `evidence/`（体积达 GB 级、不随仓库分发，因此不提供可点击链接）。该归档按 `evidence/upgrade/<领域>/` 组织，需要复现时请按对应命令重新生成。

版本：`1.4.3`（见 `VERSION` / `GET /api/health`）

根目录 `VERSION` 是唯一产品版本源；本页版本和接口返回值须与其一致。

## 工程工作台静态资源

主页面使用本地 `GET /static/workbench.css` 和 `GET /static/workbench.js`，分别返回 `text/css; charset=utf-8`、`text/javascript; charset=utf-8`。这两个文件通过既有静态白名单提供，响应字节与 `web/` 中实际文件一致，并保留 ETag、Cache-Control 和 nosniff。默认监听地址、启动参数和业务 API 保持既有合同。2026-10-17 的真实 HTTP 404→200、类型和资源字节验证见工程台运行证据（本地证据）。

## 2026-09-26 改造接口补充

| 方法 / 路径 | 请求 | 响应与验收语义 |
|---|---|---|
| GET `/api/schema/circuitjson?version=2` | 版本 1 或 2 | 默认仍为 v1；v2 增加工程元数据 |
| POST `/api/circuit/migrate` | `{"circuit": {...}}` | 保留元件/连接，无损迁移；不猜测封装或真实引脚 |
| POST `/api/import/external` | `source`、UTF-8 `content`、可选 `name` | 转成项目 CircuitJSON 候选；只进入审核，不代表 accepted 或电气校验通过 |
| POST `/api/design/tscircuit` | TSX/JSX 文本 `code`、可选 `name` | 本机上游库生成 Circuit JSON、原理图 SVG 与待审核候选 |
| POST `/api/image/analyze` | `backend`、data URL `image`、可选 `source_name` | CircuitVision 原始识别结果；只供人工校正，不生成电路候选 |
| POST `/api/validate` | `circuit`、`source`；可选 ASCII `name` | 默认执行适用 SPICE 及 KiCad 官方门禁，返回 `accepted/verdict/gates/blocking_gates` |
| POST `/api/pcb/candidate` | v2 `circuit`，可选 `name/target_version` | 真实封装、焊盘/网络审计及未布线 PCB；不代表制造通过 |
| POST `/api/pcb/manufacture` | v2 `circuit`、实际已布线的 `board_text` | 严格源门禁、真实引脚网表、官方原理图 parity 与 DRC 通过后返回待复核 ZIP |

`source` 取 `template/ai/import/image/manual`。`verdict=fail` 表示已发现失败，`incomplete` 表示必要工具或验证未完成。明确请求内部模式 `require_official:false` 时报告标明 `assurance:internal`，不可把它作为官方工程验证证据。

真实 PCB 要求每个实体元件提供 manufacturer、mpn、datasheet、footprint、pin_map、pin_map_source 和已确认的位置；GND/VCC 虚拟锚点不生成封装。板框、两层、间距和线宽约束见 `docs/PCB真实候选板合同.md`。KiCad 10 使用 `target_version:20260206`，KiCad 8 使用 `20240108`；省略时按本机 CLI 版本选择。

制造接口读取提交的 PCB 文本，不接受客户端服务器文件路径。失败返回 422，并保留逐项门禁和唯一运行目录；通过包包含板文件、源电路/原理图、BOM、贴片、Gerber、钻孔及复验证据。输出始终为 `fabrication_candidate_for_review`，`manufacturing_release:false`。

2026-10-16以当前Handler及KiCad 10.0.6重新运行内部3.3V ATmega328P＋SHT41板：正例HTTP 200，源ERC、网表差异、PCB DRC、未连接及原理图parity均0，12个CAM/BOM文件与HTTP ZIP逐项SHA一致；未布线422（30未连接）、短路422（3违规）、缺共地422（官方ERC拒绝）且三个负例无CAM，合计4/4符合预期。MCU的SPICE为 `not_applicable`；缺共地源拒绝后制造DRC为 `incomplete`，没有执行。该分母是1块内部板及3个构造故障，未实物制作、上电或制造放行。原始工具命令、退出码、报告、请求和包保留在本轮制造HTTP证据（本地证据）。验收子进程仅将输出ROOT定向至该证据目录，生产Handler和门禁不改写。

浏览器 OCR 依赖固定 `/static/vision/` 本地资源，不上传图片。原始文字、检测评分及网络候选可在审核界面下载。评分未校准，器件数值须人工确认。

- 基址：`http://127.0.0.1:8765`
- 编码：UTF-8
- 本文档与 `server.py` 中 `_handle_*` 方法及 `GET /api/docs` 同源

## 端点总览

| 方法 | 路径 | 说明 | 限流 |
|---|---|---|---|
| GET | `/api/health` | 健康检查 | 否 |
| GET | `/api/metrics` | 运行指标 + LLM 熔断状态 | 否 |
| GET | `/api/docs` | 机器可读 API 目录 | 否 |
| GET | `/api/schema/circuitjson?version=1\|2` | CircuitJSON v1/v2 JSON Schema（默认 v1） | 否 |
| POST | `/api/circuit/migrate` | 无损迁移 CircuitJSON v1 至 v2 | 否 |
| POST | `/api/import/external` | CircuitVision/HCD/tscircuit 外部结果转换候选 | 否 |
| POST | `/api/design/tscircuit` | 本机 TSX/JSX 代码建电路，生成预览与待审核候选 | 单 worker |
| GET | `/api/design/tscircuit/example` | 返回本机 RC 示例的 `code` 文本 | 否 |
| POST | `/api/image/analyze` | CircuitVision 本地图片检测与 SAM2 分割原始结果 | 否 |
| POST | `/api/validate` | 统一候选校验；默认要求 KiCad 官方 ERC 和网表 | 是（KiCad） |
| GET | `/api/providers` | 多模型预设目录 | 否 |
| GET | `/api/generate` | SSE 生成流 | 是（LLM） |
| POST | `/api/edit` | AI 增量修改当前电路 | 是（LLM） |
| POST | `/api/erc` | 编辑后 ERC 重算 | 否 |
| POST | `/api/simulate` | 准仿真（直流工作点） | 否 |
| POST | `/api/spice_analyze` | SPICE 分析 op/ac/tran/dc | 否 |
| POST | `/api/spice_transient` | 真实 .tran 节点电压时间序列（教学时序图数据源；跑不出来返回 `ran=false`） | 否 |
| POST | `/api/spice` | SPICE 网表导出 | 否 |
| POST | `/api/bom` | BOM 导出 | 否 |
| POST | `/api/pcb` | PCB 预布局 | 否 |
| POST | `/api/pcb/candidate` | 用真实 KiCad 封装生成未布线候选与焊盘审计 | 是（KiCad） |
| POST | `/api/routing_job` | 隔离式 PCB 路由任务合同校验 | 否 |
| POST | `/api/schemes` | 多方案拓扑草案 | 否 |
| POST | `/api/kicad` | KiCad 工程 zip 导出 | 是（KiCad） |
| GET/POST/DELETE | `/api/history` | 工程历史 | 否 |
| GET | `/api/trace` | AI 调用链路日志 | 否 |
| POST | `/api/clarify` | 需求澄清 | 是（LLM） |
| POST | `/api/llm_config` | 注册模型配置 | 是（LLM） |

> LLM 限流默认 `LSQC_RATE_LIMIT=30` 次/分/IP；KiCad 导出默认 `LSQC_KICAD_RATE_LIMIT=20` 次/分/IP。
> 请求体上限 1MB。

### CircuitJSON 结构

除特别说明外，所有 `circuit` 字段均为同一结构：

```json
{
  "parts": [
    {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
    {"type": "resistor", "id": "R1", "attrs": {"value": "330Ω"}},
    {"type": "led", "id": "LED1", "attrs": {"value": "红"}},
    {"type": "gnd", "id": "GND1"}
  ],
  "connections": [
    ["BAT1:+", "R1:1", "red", []],
    ["R1:2", "LED1:+", "red", []],
    ["LED1:-", "GND1:1", "black", []],
    ["BAT1:-", "GND1:1", "black", []]
  ]
}
```

- `parts[].type` 必须在元件库（152 种，以 `core.catalog.CATALOG` 为准）内；`id` 唯一
- `connections` 每项为 `[引脚A, 引脚B, 颜色, 路由点]`，引脚格式 `元件id:引脚名`
- `version` 可省略（按 v1 处理）；出现时必须为整数 `1` 或 `2`
- v1 完整合同见 `GET /api/schema/circuitjson`；v2 工程元数据合同见 `GET /api/schema/circuitjson?version=2`

### POST /api/validate

请求体为 `{"circuit": CircuitJSON, "source": "ai", "name": "candidate"}`。
`source` 取 `template`、`ai`、`import`、`image`、`manual` 之一；`name` 只能含
ASCII 字母、数字、下划线和连字符。可选布尔字段 `require_official`（默认
`true`）和 `require_spice_runtime`（默认与 `require_official` 相同）。接口复用 KiCad 导出限流。

响应包括 `verdict`（`pass`、`fail` 或 `incomplete`）、`accepted`、
`assurance`、`gate_order`、逐项 `gates`、`blocking_gates` 及产物摘要。
没有 `kicad-cli` 时，默认官方复验为 `unavailable`，总结果为
`incomplete`、`accepted=false`。显式传入 `require_official=false` 只得到
`assurance=internal` 的内部校验结果，不等同官方验收。SPICE 不适用于
缺少模型的器件时，报告 `not_applicable`，不会声称已完成仿真。

### POST /api/import/external

此接口按明确的 `source` 解析外部文件并转换为项目 CircuitJSON v2。它不运行上游模型，也不接受客户端自报的 ERC 结果。浏览器流程是：选择来源与文件，预览审核候选，调用 `/api/validate` 且 `source=import`，再由用户选择接受或应用未验证草稿；后者会保留草稿状态，并可撤销。

**请求**

```json
{
  "source": "circuitvision_spice",
  "content": "* source file contents, preserved as UTF-8 text",
  "name": "input.cir"
}
```

`source` 只接受 `circuitvision_spice`、`hcd`、`tscircuit`；`content` 为 UTF-8 文本且不超过 1 MiB；`name` 可省略且不超过 240 字符，仅作显示。tscircuit 的 Circuit JSON 是独立格式，需经适配转换后才成为项目 CircuitJSON。

**响应 200：可审核的候选**

```json
{
  "status": "needs_review",
  "circuit": {"version": 2, "parts": [], "connections": []},
  "issues": [
    {"code": "EXTERNAL_REVIEW_REQUIRED", "message": "转换仅生成候选；参数、拓扑和额定值需复核，电气结果由统一门禁产生", "path": ""},
    {"code": "RATINGS_UNKNOWN", "message": "原始结果未提供可确认的器件额定功率、耐压等额定值", "path": "parts"}
  ],
  "readiness": {
    "importable": true,
    "accepted": false,
    "electrical_validated": false,
    "needs_review": true
  },
  "source": {"format": "circuitvision_spice", "sha256": "..."}
}
```

`issues` 列出待确认项和路径。`RATINGS_UNKNOWN` 提醒原始结果通常未确认电阻功率、电容耐压等器件额定值；来源声明或导入状态不替代真实校验。未知直流电压、I2C 地址、IC/MCU 多引脚映射以及未支持的器件/端口不允许猜测或静默删除，不能完整映射时整份拒绝。

**错误**

- `400`：请求体非法、来源不受支持或缺少必要字段。
- `422`：外部内容无法完整、无歧义地转换；返回 `status: "rejected"`、相同结果字段，`circuit: null` 且 `readiness.importable: false`，逐项说明拒绝原因。

源码获取、前端合同测试、真实后端转换和电气门禁分别记录在 [`docs/开源项目集成.md`](开源项目集成.md) 与 `evidence/upgrade/open_source_integration/`。Edge CDP 受控 HTTP fixture 的 17/17 只证明浏览器交互合同；另有真实 Edge + 子进程 `server.py` 的 2/2 组合测试，覆盖 `/api/import/external`、`/api/validate`、候选审核应用、编辑撤销与 422 原图保护，详见 `browser_e2e.json`。二者不合并成一个分母，也不表示上游识别精度。

CircuitVision 上游独立界面的本机入口与验证边界见[开源项目集成说明](开源项目集成.md#circuitvision-本机启动)：`tools/run_upstream.ps1 -CheckOnly` 只检查依赖和运行文件，`tools/run_upstream.ps1 -Start` 显式启动本地 Streamlit 界面。该上游启动器与本 API 的外部格式适配接口分开运行。

### POST /api/design/tscircuit

请求为 `{"code":"export default () => (<board width=\"24mm\" height=\"16mm\">...</board>)","name":"rc.tsx"}`；`code` 必须是非空 UTF-8 TSX/JSX 文本且不超过 1 MiB，整个 JSON 请求体也受 1 MiB 限制；`name` 可省略且不超过 240 字符。组件通过默认导出或上游支持的组件导出方式提供。`GET /api/design/tscircuit/example` 返回现有 RC 示例的 `code`，可直接提交运行。

服务端调用既有本机 npm 安装中的 `@tscircuit/eval`、`@tscircuit/core` 和 `circuit-to-svg`，禁用 CDN 和网络下载；不需要模型 API。默认发现项目 `runtime/tscircuit` 或同级工具链中的安装，可用 `TSCIRCUIT_PREFIX` 指定本机安装前缀。优先使用前缀内 `node.exe`，其次 `TSCIRCUIT_NODE` 或系统 Node。运行时不执行 source/evidence 目录内脚本。

成功返回 HTTP 200：`status=needs_review`，`circuit` 为完整转换的项目 CircuitJSON v2，`upstream_circuit_json` 为上游实际记录数组，`schematic_svg` 为实际原理图，`diagnostics` 保留上游诊断，`runtime` 包含实际 npm 版本、Node 版本和 `offline:true`。`source` 记录代码摘要、上游数据摘要和本机归属文件。`readiness` 固定 `accepted=false`、`electrical_validated=false`；`importable=true` 只表示可审核。`issues` 包含 `CODE_REVIEW_REQUIRED` 和 `RATINGS_UNKNOWN`，代码建电路不会自动确认器件额定功率或耐压。

转换直接复用原外部结果适配器的完整转换逻辑，仅支持明确二端电阻、电容、电感及极性已知的直流电源。未知 IC/MCU、未知多引脚映射、缺失参数或不完整网络整份拒绝，`circuit=null`，不输出删除未知器件后的部分候选。已生成的上游 JSON、SVG 和诊断仍保留，便于核对和下载。

浏览器“代码建电路”面板提供代码载入、示例、原始代码/JSON/SVG 下载和独立原理图预览。成功候选先调用 `/api/validate`、`source=import`，再进入已有候选审核；用户显式应用后才改变当前画布，并可编辑、撤销。取消、失败或请求期间画布已变化时不应用候选。

错误码：400 请求非法；422 上游代码执行/语法错误或整份转换拒绝；409 本机 worker 忙；503 Node/npm 运行库不可用；504 代码运行超过 60 秒，回收该请求的进程树；502 运行桥接异常。转换拒绝与代码错误返回 `status=rejected`、原始诊断及 `readiness.importable=false`；运行依赖/超时错误返回 `status=incomplete`、`accepted=false`。生产代码和来源见 `core/tscircuit_engine.py`、`tools/tscircuit_worker.mjs` 与 `third_party/tscircuit-runtime-attribution.json`。

本轮真实 Edge + 本机 Python HTTP + Node 的结果记录在 `evidence/upgrade/native_integration/tscircuit-ui.json`：3 个流程分别验证 RC 预览与候选校验、应用/修改参数/真实工程导出/撤销，未知 IC 与语法错误保护原图，以及取消保护原图。`previousRuns` 保留此前把元件 ID 输入框误当参数值的断言失败；修复后的断言读取同一参数值输入框，并核验实际导出的 `R1.attrs.value`。单个真实样本的代码、上游 JSON、SVG 和确切 `source=import` 候选校验响应分别为同目录 `tscircuit_rc.tsx`、`tscircuit_rc.json`、`tscircuit_rc.svg`、`tscircuit_rc_validation.json`。这些流程证据与历史 84 条记录分开保存，不构成生成正确率或上游识别精度。

后端独立 8 项实测结果在同目录 `tscircuit.json`，覆盖真实 RC 生成、未知 IC 整图拒绝、语法诊断、请求与运行依赖检查、便携 Node 优先、真实超时与进程回收、HTTP 成功/拒绝和运行错误状态码。HTTP 运行错误映射采用受控异常，真实生成与超时另有实际执行测试；修复 Windows 中文进程回收输出的解码问题后，仅增量复验真实超时项目，并保留原完整测试结果与观察。上述 RC 样本为两只电阻和一只电容构成的无电源被动网络。

### POST /api/image/analyze

该接口把浏览器归一化后的 PNG/JPEG 交给本机 CircuitVision worker，返回检测框、原始类别、分数、SAM2 mask、issues 与 provenance。它不推断电气拓扑，也不构造或接受电路；用户须逐行确认器件类型、数值和每个引脚网络，或显式排除误检，再走已有 `/api/validate` 和审核流程。

请求格式为 `{"backend":"circuitvision","image":"data:image/png;base64,...","source_name":"board-photo.jpg"}`。图片必须是 PNG/JPEG data URL；该端点请求体上限为 10 MiB，图像上限为 20,000,000 像素。浏览器按现有导入规则将输入归一化为 PNG。界面可下载确切模型输入 PNG，并在独立人工校正记录中记录原文件信息、解码尺寸、缩放和输入 SHA；原始响应 JSON 保持独立。模型返回的 `image.sha256` 对应实际提交 PNG，而非原始 JPEG。

成功响应固定为 `status=needs_manual_correction`、`circuit=null`、`accepted=false`、`electrical_validated=false`。`detections` 保留原始框、类别、评分，`pins=null`、`value=null`；`segmentation` 返回实际 mask、尺寸、非零像素数与阈值。`topology.status=not_inferred`，`pins=null`、`connections=[]`、`nets=[]`。

新增 `wire_geometry` 为内置几何模块生成的待确认导线候选：`status=geometry_candidates`、`candidate_only=true`、`accepted=false`、`electrical_validated=false`，包含 `width/height`、`segments[{id,x1,y1,x2,y2,pixel_support,point_ids}]`、`points[{id,x,y,snapped_x,snapped_y,cc_label,snap_dist}]`、`pixel_regions[{id,endpoint_ids,area}]`、`ambiguous_intersections[{x,y,segment_ids,kind:"unconfirmed_crossing"}]`、`truncated`、`foreground_pixels_after_exclusion` 和 `provenance`。最多返回 160 条线段；截取时界面提示“导线候选已截取，需人工补充”。几何相交、像素连通与端点吸附均须人工确认，不自动生成电气引脚映射或网表。响应 `provenance.implementation=core.integrations.circuitvision`，`timings_ms.wire_geometry` 记录几何阶段耗时。

本机 worker 超时为 180 秒，超时返回 `code=timeout`、HTTP 504 并回收请求进程。其他错误状态为 400 请求不合法、409 worker 忙、503 运行依赖不可用、502 推理失败；失败不会替换当前画布。模型框、mask 与线段候选不表示正确率或电气校验通过。历史单图 smoke 和历史受控 HTTP UI 证据保持在 `evidence/upgrade/open_source_integration/vision_pipeline/`，本轮内置几何模块和真实 bridge 验证记录在 `evidence/upgrade/native_integration/`，各自按验证范围解释，不合并为识别准确率。

### POST /api/pcb/candidate

请求体为 `{"circuit": CircuitJSON v2, "name": "board1"}`。服务端从
`KICAD_FOOTPRINT_DIR` 环境变量读取本地 `.pretty` 库根目录（多个目录用系统
路径分隔符分开）；客户端不能指定磁盘路径。每个物理元件必须有料号、
`component.footprint`、`component.pin_map`、`component.pin_map_source`、
`component.datasheet` 和 `pcb` 坐标。`board` 必须给出尺寸、双层、间距及线宽。

响应 `status=unrouted_candidate`，包含 `.kicad_pcb` 文本、`pad_audit` 和封装
哈希。`routing`、`drc`、`gerber`、`manufacturing` 在没有相应复验时均为
`unavailable`。缺少必需证据或封装时返回 422 和机器可读 `code`，不会使用
旧版 `/api/pcb` 的占位封装代替真实封装。

---

## GET /api/health

健康检查：版本、Key 是否配置、默认模型、kicad-cli 探测结果。

**请求**：无参数

**响应 200**

```json
{
  "status": "ok",
  "version": "1.4.3",
  "product": "CircuitTutor",
  "llm_key_configured": true,
  "default_model": "deepseek-flash",
  "kicad_cli": false,
  "features": ["erc", "autofix", "precision", "simulate",
               "spice_analyze", "trace", "bom", "kicad_export",
               "circuitjson_schema_v1", "incremental_edit"]
}
```

```bash
curl -s http://127.0.0.1:8765/api/health
```

---

## GET /api/metrics

运行指标：HTTP 计数、平均延迟、LLM 熔断器状态。进程内存态，重启清零。

**响应 200**

```json
{
  "counters": {"http_GET": 12, "http_POST": 3, "status_2xx": 15},
  "avg_latency_s": {"/api/erc": 0.004, "/api/health": 0.001},
  "llm_breaker": {"state": "closed", "failures": 0, "threshold": 5, "rejected": 0}
}
```

---

## GET /api/docs

机器可读端点目录。

**响应 200（节选）**

```json
{
  "product": "CircuitTutor",
  "version": "1.4.3",
  "description": "中文需求 → 电路原理图 → KiCad/SPICE/BOM 导出",
  "endpoints": [
    {"method": "GET", "path": "/api/health", "desc": "健康检查"},
    {"method": "GET", "path": "/api/schema/circuitjson", "desc": "CircuitJSON v1/v2 Schema"},
    {"method": "POST", "path": "/api/circuit/migrate", "desc": "CircuitJSON v1 到 v2 无损迁移"},
    {"method": "POST", "path": "/api/edit", "desc": "AI 增量改图（前端可「撤回更改」）"},
    {"method": "POST", "path": "/api/erc", "desc": "编辑后 ERC 重算"}
  ],
  "features": ["erc_33_rules", "simulate", "bom", "spice",
               "spice_analyze", "trace", "schemes", "history",
               "kicad_export", "pcb_layout", "routing_job_v1_fallback",
               "circuitjson_schema_v1", "circuitjson_schema_v2", "incremental_edit"]
}
```

---

## GET /api/schema/circuitjson

默认返回 Draft 2020-12 CircuitJSON v1 JSON Schema；`?version=2` 返回 v2 Schema。Schema 规定跨工具共享的数据形状；元件类型、合法引脚和电气规则仍由目录白名单与 ERC 负责。

```bash
curl -s http://127.0.0.1:8765/api/schema/circuitjson
curl -s 'http://127.0.0.1:8765/api/schema/circuitjson?version=2'
```

关键约束：v1 的 `parts`、`connections` 必填，`version` 可省略但出现时只能为 `1`；v2 的 `version: 2` 必填，保留相同的元件与连接结构，增加可选的器件身份/引脚到焊盘映射、供电域、双层板约束和来源字段。迁移时不会猜测未知器件、封装或电压。

## POST /api/circuit/migrate

请求体为 `{"circuit": CircuitJSON}`，成功时返回 `{"circuit": CircuitJSON-v2}`。v1 的元件、连接和其他现有字段原样保留；已是 v2 的输入复制后返回。形状或版本不合法时返回 400。

---

## GET /api/providers

多模型预设目录（无密钥、无 base_url）。

**响应 200**

```json
{
  "providers": [
    {
      "id": "deepseek",
      "name": "DeepSeek",
      "custom": false,
      "models": [{"id": "deepseek-flash", "name": "V4.1 Flash"}]
    },
    {"id": "custom", "name": "自定义端点", "custom": true, "models": []}
  ],
  "deepseek_env_key": true
}
```

`deepseek_env_key=true` 表示服务端已配置 DeepSeek Key，前端选 DeepSeek 时可免填 Key。

---

## GET /api/generate

SSE 流式生成：中文需求 → CircuitJSON。前端用 `EventSource` 消费。

### 查询参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `q` | 是 | 中文需求，≤`MAX_QUERY_LEN`（默认 500） |
| `c` | 否 | 澄清问答对 JSON 数组，由 `/api/clarify` 结果回填 |
| `cfg` | 否 | `/api/llm_config` 返回的配置 id |

**示例**

```
GET /api/generate?q=用555定时器做一个LED闪烁灯
GET /api/generate?q=做一个LED灯&c=[{"question":"电压？","answer":"5V"}]&cfg=a1b2c3
```

### SSE 事件

| 事件 | data 形状 | 说明 |
|---|---|---|
| `stage` | `{"stage":"plan","detail":"电气规划中"}` | 进度。stage ∈ model / components / extract / plan / generate / repair_struct / repair_erc / autofix / precision / ratings / simulate / done |
| `circuit` | CircuitJSON | 生成（或兜底）成功的电路 |
| `schemes` | `{"schemes":[{"key":"...","title":"...","reason":"...","score":1.0}]}` | 附带拓扑草案（精简字段） |
| `erc` | `{"passed":true,"fatal":0,"major":0,"minor":0,"warning":0}` | ERC 终检摘要 |
| `fallback` | 匹配模板时含 `reason/template`；拒绝时含 `reason/rejected/blockers/questions` | 说明匹配的模板或拒绝、澄清原因 |
| `validation` | `{"accepted":true,"verdict":"pass","gates":{...}}` | 统一门禁；须按适用状态解释，工具未完成为 `incomplete` |
| `done` | 成功含 `ok/budget_used/budget_max/accepted/validation_verdict`；拒绝含 `ok:false/rejected:true/accepted:false` | 流结束；`ok` 不替代统一门禁的 `accepted` |

前置拒绝（超长、限流、目录外型号、电气矛盾或缺少关键条件）也走 SSE：`fallback` + `done`，`rejected=true`，不发送 `circuit`。直接调用生成接口也执行缺项检查；供电、I2C 地址等未确定时在 `questions` 中继续追问。`c` 只合并有效问答，部分回答后仍缺条件时继续澄清。

模型未配置或运行失败时，仅对明确匹配且覆盖所需器件的离线模板生成电路，并保留用户显式参数。无匹配模板或模板缺少需求器件时拒绝；5V/3.3V MCU＋I2C 请求在没有匹配模板时不会变成LED示例。本地工作流诊断为9/9预期行为，包含1个匹配模板与8个拒绝用例，不能作为在线生成正确率或泛化指标。原4/9与修复后记录、原始SSE和隔离配置见本轮EDA验收（本地证据）。

**示例（成功片段）**

```
event: stage
data: {"stage":"model","detail":"模型：DeepSeek V4.1 Flash"}

event: stage
data: {"stage":"components","detail":"元件：ne555、led、resistor（本地命中）"}

event: circuit
data: {"parts":[{"type":"ne555","id":"U1","attrs":{}}], "connections":[...]}

event: erc
data: {"passed":true,"fatal":0,"major":0,"minor":0,"warning":0}

event: done
data: {"ok":true,"budget_used":2,"budget_max":5,"request_id":"ab12cd34"}
```

---

## POST /api/edit

使用自然语言增量修改当前电路。模型只接收当前图所用元件与修改指令命中的新增元件引脚白名单；候选结果随后经过完整目录/引脚结构校验和 ERC 前后对比。候选若新增 fatal 或 major 问题会被拒绝。

**请求**

```json
{
  "circuit": {"parts": [...], "connections": [...]},
  "instruction": "把 R1 改成 470Ω，并增加一个去耦电容",
  "cfg": "可选的模型配置 id"
}
```

**响应 200**

```json
{
  "ok": true,
  "circuit": {"parts": [...], "connections": [...]},
  "changes": {
    "parts_added": ["C1"],
    "parts_removed": [],
    "parts_modified": ["R1"],
    "connections_added": 2,
    "connections_removed": 0
  },
  "erc": {"passed": true, "issues": []},
  "request_id": "ab12cd34ef56"
}
```

失败响应只包含 `error`，不含替代 `circuit`，因此前端保留原画布：`400` 输入非法；`422` 候选结构或 ERC 质量门禁失败；`429` 限流；`502` 模型调用失败；`503` 未配置模型。

---

## POST /api/erc

编辑后重算 ERC（纯计算，不占 LLM 配额）。前端拖动/改值/删除后防抖调用。

**请求**

```json
{"circuit": {"parts": [{"type": "led", "id": "LED1", "attrs": {"value": "红"}}],
             "connections": []}}
```

**响应 200**（`ERCReport.to_dict()`）

```json
{
  "passed": false,
  "failure_count": 2,
  "fatal_errors": [],
  "major_errors": ["电路中没有任何接地（GND）连接，电流无法构成回路"],
  "minor_errors": ["元件 LED1（led）没有连接任何导线"],
  "warnings": [],
  "issues": [
    {"severity": "major", "rule": "未共地",
     "message": "电路中没有任何接地（GND）连接，电流无法构成回路", "pins": []},
    {"severity": "minor", "rule": "未连接元件",
     "message": "元件 LED1（led）没有连接任何导线", "pins": ["LED1"]}
  ]
}
```

- `severity`：`fatal` | `major` | `minor` | `warning`
- `passed` = 无 fatal 且无 major
- 空电路（`parts: []`）返回通过的空报告，不报 400

**错误**：`400` 缺 circuit / parts 非数组 / 元件缺 id、type

---

## POST /api/simulate

准仿真。成功时额外可能返回 `blocks[]`（电路块启发式解释，措辞保守）与 LED 电流等工程估算。


准仿真：LED 电流、电阻分压、线性稳压功耗的确定性直流近似（教学向，非 SPICE 替代）。

**请求**

```json
{"circuit": {"parts": [...], "connections": [...]}}
```

**响应 200**

```json
{
  "passed": true,
  "issues": [],
  "led_currents": [
    {"led": "LED1", "r": "R1", "vcc_v": 5.0, "vf_v": 2.0,
     "r_ohm": 330.0, "i_ma": 9.1}
  ],
  "dividers": [
    {"r1": "R1", "r2": "R2", "vin": 5.0, "vout": 2.5}
  ],
  "regulator": [
    {"reg": "U1", "vin": 9.0, "vout": 5.0, "i_ma": 100.0, "p_w": 0.4}
  ]
}
```

`issues[].severity`：`info` | `warn` | `danger`

---

## POST /api/spice_analyze

SPICE 分析。优先本机 ngspice；不可用时回退解析解。

**请求**

```json
{
  "circuit": {"parts": [...], "connections": [...]},
  "kind": "op"
}
```

`kind` ∈ `op` | `ac` | `tran` | `dc`（默认 `op`）

**响应 200**

```json
{
  "kind": "op",
  "deck": "* CircuitTutor\nV1 n1 0 DC 5\n...\n.end",
  "engine": "ngspice",
  "ngspice_available": true,
  "ngspice_output": "...\n(超 8000 字符自动截断)",
  "node_voltages": {"n1": 5.0, "n2": 2.0},
  "analytic": {"led_currents": [...], "dividers": [...]},
  "ac_response": null
}
```

- `kind=ac` 时 `ac_response` 含 `topology` / `fc_hz` / `points[{f_hz,gain_db,gain}]`
- `engine`：`ngspice` 或 `analytic`
- 无 ngspice 时 `ngspice_output` 为 null

**错误**：`400` kind 非法；`500` 分析失败

---

## POST /api/spice

导出 ngspice 兼容网表文件（下载）。

**请求**

```json
{
  "circuit": {"parts": [...], "connections": [...]},
  "name": "led_demo",
  "format": "cir"
}
```

- `format`：`cir`（默认，完整网表）| `subckt`（子电路）
- `name` ≤40 字符

**响应 200**

- `Content-Type: text/plain; charset=utf-8`
- `Content-Disposition: attachment; filename="led_demo.cir"`

```
* led_demo
V1 n1 0 DC 5
R1 n1 n2 330
D1 n2 0 LED
.end
```

---

## POST /api/bom

BOM 物料清单。同值同类型自动合并；`gnd`/`vcc` 标记件不计入。

**请求**

```json
{
  "circuit": {"parts": [...], "connections": [...]},
  "format": "json"
}
```

`format`：`json`（默认）| `csv`（UTF-8 BOM，Excel 可开）

**响应 200 · JSON**

```json
{
  "app": "CircuitTutor",
  "kind": "bom",
  "items": [
    {"no": 1, "type": "resistor", "value": "330Ω", "package": "R_AXIAL",
     "footprint": "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
     "rating": "≤250mW", "qty": 2, "refs": "R1、R2"}
  ],
  "total_line_items": 3,
  "total_parts": 4
}
```

**响应 200 · CSV**

- `Content-Type: text/csv; charset=utf-8`
- 表头：`序号,类型,参数值,封装,KiCad封装,额定,数量,位号`

---

## POST /api/pcb

PCB 预布局：板框 + 网格放置的封装占位 + 位号丝印（教学/原型向）。

**请求**

```json
{
  "circuit": {"parts": [...], "connections": [...]},
  "name": "led_demo",
  "summary": false
}
```

- `summary=true` → 返回 JSON 摘要
- `summary` 缺省/false → 返回 `.kicad_pcb` 文件下载

**响应 200 · 摘要**

```json
{
  "board_w_mm": 50.0,
  "board_h_mm": 40.0,
  "parts": 4,
  "with_footprint": 3,
  "coverage": 75.0,
  "grid_mm": 15.0,
  "placements": [
    {"ref": "R1", "type": "resistor", "fp": "Resistor_THT:R_Axial_…",
     "x": 10.0, "y": 10.0}
  ]
}
```

**响应 200 · 文件**

- `Content-Type: application/x-kicad-pcb`
- `Content-Disposition: attachment; filename*=UTF-8''led_demo.kicad_pcb`

---

## POST /api/routing_job

校验 B 方向隔离式路由任务合同。该端点不会在核心进程中启动 WSL、PCBWorld
或 PPO；合法请求返回规范化合同和显式 `fallback` 结果，供独立外部服务消费。

**请求**

```json
{
  "job": {
    "schema_version": "1.0",
    "job_id": "demo-small-2l-001",
    "board_path": "artifacts/demo-small-2l.kicad_pcb",
    "output_dir": "artifacts/routing/demo-small-2l-001",
    "constraints": {
      "layers": 2,
      "clearance_mm": 0.2,
      "max_vias": 20,
      "max_steps": 500,
      "timeout_s": 300
    },
    "policy": {
      "name": "ppo-small-v1",
      "checkpoint": ""
    },
    "random_seed": 1550
  }
}
```

**响应 200**

```json
{
  "accepted": true,
  "job": {"schema_version": "1.0", "job_id": "demo-small-2l-001"},
  "result": {
    "status": "fallback",
    "reason": "核心进程仅校验并转交任务；PCBWorld/PPO 必须由隔离式外部服务执行",
    "metrics": {
      "fully_routed": null,
      "net_completion_rate": null,
      "drc_violations": null,
      "total_trace_length_mm": null,
      "via_count": null,
      "inference_time_ms": 0.0,
      "fallback_triggered": true,
      "model_version": "ppo-small-v1",
      "random_seed": 1550
    }
  }
}
```

未测指标使用 `null`，不能将其解释为 0 违规或 100% 布通。

**错误 400**：合同字段、双层范围、间距、过孔、步数、超时、策略或随机种子非法；
响应包含 `errors[]`，一次列出全部可执行修正提示。

---

## POST /api/schemes

按需求从 40 套内置模板中选出 top-2 拓扑草案（零 LLM）。

**请求**

```json
{"query": "用555定时器做一个LED闪烁灯"}
```

**响应 200**

```json
{
  "schemes": [
    {"key": "ne555_blink", "title": "555 闪烁灯",
     "reason": "模板命中", "score": 9.0,
     "circuit": {"parts": [...], "connections": [...]}},
    {"key": "led_limit", "title": "LED 限流电路（5V-330Ω-LED）",
     "reason": "邻近拓扑参考", "score": 1.2,
     "circuit": {"parts": [...], "connections": [...]}}
  ]
}
```

---

## POST /api/kicad

导出 KiCad 工程压缩包：`.kicad_sch` + `.kicad_pro` + 符号库 + `sym-lib-table` + `HOW_TO_OPEN.txt`（+ `preview/*.svg` 若本机有 kicad-cli）。工程文件名使用 ASCII stem（中文标题仅保留在原理图 title_block），解压后请用 KiCad **「打开工程」** 选择 `.kicad_pro`，不要用「文件→导入」。

**请求**

```json
{
  "circuit": {"parts": [...], "connections": [...]},
  "name": "led_demo"
}
```

**响应 200**

- `Content-Type: application/zip`
- `Content-Disposition: attachment; filename*=UTF-8''zhihui-kicad-led_demo.zip`

解压后双击 `.kicad_pro` 即可在 KiCad 8/9 打开编辑。

**错误**：`400` parts 为空或缺 id/type；`429` 限流；`500` 导出失败

---

## GET /api/history

列出本地保存的工程历史（最多 50 条）。默认目录为项目内的 `data/history`，随包迁移；可用 `LSQC_HISTORY_DIR` 覆盖。

**响应 200**

```json
{
  "items": [
    {"id": "a1b2c3d4e5f6", "saved_at": "2026-09-16 14:30:00",
     "title": "555闪烁灯", "query": "用555做闪烁灯",
     "parts": 6, "connections": 5}
  ]
}
```

### GET /api/history?id=\<id\>

加载单条记录（含完整 circuit）。

**响应 200**

```json
{
  "id": "a1b2c3d4e5f6",
  "saved_at": "2026-09-16 14:30:00",
  "title": "555闪烁灯",
  "query": "用555做闪烁灯",
  "meta": {},
  "circuit": {"parts": [...], "connections": [...]}
}
```

**404**：`{"error": "历史记录不存在"}`

---

## POST /api/history

保存当前电路到工程历史。

**请求**

```json
{
  "circuit": {"parts": [...], "connections": [...]},
  "title": "555闪烁灯",
  "query": "用555做闪烁灯"
}
```

**响应 200**

```json
{
  "id": "a1b2c3d4e5f6",
  "saved_at": "2026-09-16 14:30:00",
  "title": "555闪烁灯",
  "query": "用555做闪烁灯"
}
```

---

## DELETE /api/history?id=\<id\>

删除一条历史记录。

**响应 200**：`{"ok": true}`  
**响应 404**：`{"ok": false}`

---

## GET /api/evidence/summary

只读读取包内 `evidence/` 摘要（回归、合规集、模板/KiCad、在线固定集），**不读取 `.env`/密钥**。

```bash
curl -s http://127.0.0.1:8765/api/evidence/summary
```

响应含 `summary.validation / compliance / generation / ai_requirements` 与 `note`（以包内 evidence 为准）。

## GET /api/trace


查询 AI 调用链路日志（内存环形缓冲，不含密钥/完整 prompt）。

| 参数 | 说明 |
|---|---|
| `rid` | 按 request_id 查单次生成的全部事件 |
| `n` | 最近 n 次请求摘要（1~20，默认 5）；与 `rid` 互斥 |

**响应 200 · 按 rid**

```json
{
  "request_id": "ab12cd34ef56",
  "events": [
    {"request_id": "ab12cd34ef56", "span": "pipeline",
     "msg": "start", "q_len": 18, "ts": 1726470000.12}
  ]
}
```

**响应 200 · latest**

```json
{"latest": [{"request_id": "ab12cd34ef56", "ok": true, "events": 12}]}
```

`/api/generate` 成功时 `done` 事件会返回 `request_id`，可用于回查。

---

## POST /api/clarify

需求判歧：歧义需求先列出理解方式供选择。内部失败一律按 `clear` 放行，不阻塞生成。

**请求**

```json
{"query": "做一个灯的电路", "cfg": ""}
```

**响应 200 · 清晰**

```json
{"status": "clear", "questions": [], "assumptions": [], "source": "llm"}
```

**响应 200 · 需澄清**

```json
{
  "status": "clarify",
  "questions": [
    {
      "question": "「灯」指哪种？",
      "options": ["LED", "白炽灯", "霓虹灯"],
      "reason": "元件类型影响限流与驱动方式"
    }
  ],
  "assumptions": [],
  "source": "llm"
}
```

用户确认后，将问答对经 `/api/generate?c=[...]` 并入增强需求。

---

## POST /api/llm_config

注册前端选择的模型配置，返回 `cfg` id。Key 只驻留服务端内存（TTL 2h），不落盘、不进 URL、不打日志。

**请求**

```json
{
  "provider": "deepseek",
  "model": "deepseek-flash",
  "api_key": "sk-xxxxxxxx",
  "label": "DeepSeek V4.1"
}
```

- `provider`：预设 id 或 `custom`
- `custom` 时必填 `base_url`（https://，或本机回环 http://）与 `model`
- 预设可省略 `model`（取该提供商首个模型）

**响应 200**

```json
{"cfg": "Xy9…token…", "label": "DeepSeek V4.1", "model": "deepseek-flash"}
```

**错误**：`400` 配置非法；`429` 限流

生成时带 `?cfg=Xy9…` 引用该配置。cfg 非法或过期静默回退服务端默认配置。

---

## 通用错误格式

JSON 端点错误统一为：

```json
{"error": "人类可读的原因"}
```

| 状态码 | 场景 |
|---|---|
| 400 | 请求体非法、缺字段、超长 |
| 404 | 路径不存在、历史记录不存在 |
| 405 | 方法不允许 |
| 429 | 触发限流 |
| 500 | 服务端计算/导出失败 |

---

## curl 速查

```bash
# 健康检查
curl -s http://127.0.0.1:8765/api/health | python -m json.tool

# 生成（SSE）
curl -N "http://127.0.0.1:8765/api/generate?q=用555做LED闪烁灯"

# ERC 重算
curl -s -X POST http://127.0.0.1:8765/api/erc \
  -H "Content-Type: application/json" \
  -d '{"circuit":{"parts":[{"type":"led","id":"LED1","attrs":{"value":"红"}}],"connections":[]}}'

# BOM CSV
curl -s -X POST http://127.0.0.1:8765/api/bom \
  -H "Content-Type: application/json" \
  -d '{"circuit":{"parts":[{"type":"resistor","id":"R1","attrs":{"value":"330Ω"}}],"connections":[]},"format":"csv"}'

# 保存历史
curl -s -X POST http://127.0.0.1:8765/api/history \
  -H "Content-Type: application/json" \
  -d '{"circuit":{"parts":[],"connections":[]},"title":"空电路"}'
```
