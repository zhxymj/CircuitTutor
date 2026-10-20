# 图片识别基础模块：范围与测试证据

## 2026-09-26 当前状态（下方 2026-09-25 内容为历史记录）

真实本地 Tesseract.js 7 英文 OCR 已接入 `createTesseractAdapter` / `recognizeRasterWithOcr`，返回原始文字、框与置信度，没有自动猜器件数值。复杂照片超出几何预算返回 `GEOMETRY_LIMIT` 和空候选，保留原图/OCR供人工纠错。前端 PNG/JPEG/SVG、候选审核、人工编辑和后续校验已接线。

`tools/browser_ocr_smoke.js` 已在真实 Edge + 原始 `server.py` 上无 mock 通过：本地 script/worker/core/lang HTTP 200、CSP/运行时错误 0、实际 `/api/validate` HTTP 200，审核显示 `R14.7k` 与置信度 76/100。原始证据为 `image_browser_ocr_results.json`。纯文字输入无器件候选，后续电路校验未通过；OCR 接通不等于电路验收通过。

实际 CGHD v15 CC-BY-4.0 原始图片/XML共30对已保留署名与每文件 SHA256，20开发绘图者与10留出绘图者隔离。固定几何基线全体 TP/FP/FN=0/0/245，29/30 几何预算失败；已知真值文字框 OCR exact139/933，CER58.82%，执行异常0/933。外部10图 TP/FP/FN=0/0/111、9/10预算失败、OCR exact93/571（16.29%）、CER50.61%。没有预测使 precision 未定义。该 XML 子集没有 pin/net 图真值，30图连接预测为0，网络准确率不可计。

逐图/逐文字证据 `image_public_results.json`，方法、完整分母与限制见 [真实手绘评测说明](image_public_report.md)。这些 OCR 是真值框裁剪后的识别，不能宣称整图文字检测效果；两个组件类别不能宣称全符号性能。当前基线未满足 T07 真实手绘识别验收条件。

资源审计见 `data/image_samples/resource_audit_2026-09-26.json`。Sketch2Schematic MIT 原始打印样本5张已入库并署名，一个样本15个手工文字框真实OCR为基线3/15、固定阈值放大变体1/15（均保留）。HCD代码无明确LICENSE未导入；独立HF作者模型卡Apache2声明与YOLO AGPL衍生义务仍须分别核对；部分权重下载不当作可用模型。

## 2026-09-25 历史初始范围

2026-09-25。实现位于 `web/image_recognition.js`，只使用浏览器原生 JavaScript。没有训练权重、OCR 引擎或 ONNX 运行时，默认方法为 `geometry-heuristic`。所有输出都是**待人工纠错的候选**，尚未经过 ERC、KiCad 或真实器件校验。

## 当前输入与输出

- `recognizeSvg(svgText, options)`：读取无 transform 的轴对齐 SVG `line`、`rect`、`polyline`、`circle`、`text`。`path`、斜线、组变换等会给出问题提示；脚本、外部图像和引用元素直接拒绝。
- `recognizeRaster(imageData, options)`：浏览器可用 Canvas `getImageData()` 提供像素；按阈值提取清晰的横竖细线。像素文字**不进行 OCR**。
- `recognizePrimitives(primitives, options)`：接受上层预处理后的线、矩形、文字、圆点数据。
- `recognizeWithDetector(input, adapter, options)`：独立外部检测器接口。调用方可传入 `{ detect: async input => [{type, box, pins, score}] }`；模块检查类型和引脚坐标，外部检测结果覆盖相同位置的启发式结果。仓库**没有**可用的 ONNX 检测器或模型权重。

目前几何识别仅覆盖**水平 IEC 矩形电阻**与**水平双板电容**。位号仅从 SVG 文本读取；像素图自动赋位号。网络重建遵循：端点接触、T 形接点和有实心圆点的交叉处导通，单纯十字交叉不导通。结果包括 `circuit.version=2`、`parts`、`connections`、`provenance.origin=image`、原始识别框与引脚、网络列表及待修正问题。分数为未校准的启发式数值，不能当作识别概率。

## 样本和原始结果

`data/image_samples/synthetic_printed_v1.json` 有 30 张自行生成的 SVG 电路小图与标注，均来自**同一种电阻-电容拓扑的几何变化**。其中 20 张标为开发样本，10 张标为测试专用样本。生成脚本为同目录 `generate_synthetic.js`，第三方图像、代码和手写样本均未入库。这个划分没有达到真实场景的独立留出评测要求。

运行：`node --test tests/test_image_recognition.js`

| 样本路径 | 数量 | 元件真值 / 预测 / 命中 | 连接真值 / 预测 / 命中 |
|---|---:|---:|---:|
| 自生成 SVG | 20 | 40 / 40 / 40 | 20 / 20 / 20 |
| 同族图形像素化 | 10 | 20 / 20 / 20 | 10 / 10 / 10 |
| 合计 | 30 | 60 / 60 / 60 | 30 / 30 / 30 |

以上为 2026-09-25 在 Node v24.21.0 运行的原始计数。严格说只说明**固定、无噪声、轴对齐的单一图形族**能被解析，不能外推为手绘识别或真实图片准确率。另有十字交叉、圆点交叉、外部检测器入参和不支持 SVG 结构的单元测试。没有完整的 T07 真实/手绘 30 张样本评测，也没有 T08 图片人工纠错界面和 ERC 接线。

## 集成条件

`server.py` 的 `ALLOWED_STATIC` 需加入 `image_recognition.js`，`index.html` 需按现有前端脚本顺序加载它。`app.js` 的文件选择回调可调用 `CircuitImageRecognition.recognizeSvg(await file.text())`，或对普通图片绘制到 Canvas 后调用 `recognizeRaster(ctx.getImageData(...))`。界面应先展示 `detections`、`topology` 和 `issues`，允许修改元件类型、位号和引脚/网络，再调用现有结构和 ERC 校验；原始候选不能直接标记为已接受。普通图片上的文字、曲线、斜向导线和任何非所列符号需要后续模型/OCR或人工输入。
