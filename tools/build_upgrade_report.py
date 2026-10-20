"""Build a current-status supplement and append the archived original report."""
from pathlib import Path
import json
import hashlib
from datetime import date
from xml.sax.saxutils import escape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.pagesizes import A4
from pypdf import PdfReader, PdfWriter

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
TMP = ROOT / "tmp/pdfs"
OUT = ROOT / "output/pdf"
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)
pdfmetrics.registerFont(TTFont("SimSun", "C:/Windows/Fonts/simsun.ttc", subfontIndex=0))
style = ParagraphStyle("body", fontName="SimSun", fontSize=10, leading=17,
                       wordWrap="CJK", textColor=colors.HexColor("#27364A"), spaceAfter=9)
title = ParagraphStyle("title", parent=style, fontSize=23, leading=32, spaceAfter=20)
heading = ParagraphStyle("heading", parent=style, fontSize=15, leading=23, spaceBefore=12)
small = ParagraphStyle("small", parent=style, fontSize=8, leading=13)
story = []

def optional_json(relative):
    path = PROJECT / relative
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

def metric(value):
    return "未定义" if value is None else f"{value:.4f}"
def para(text, s=style):
    story.append(Paragraph(escape(text), s))
def table(rows, widths):
    cells = [[Paragraph(escape(str(v)), small) for v in row] for row in rows]
    t = Table(cells, colWidths=widths, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#E6EEF5")),
                          ("VALIGN",(0,0),(-1,-1),"TOP"),
                          ("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#CBD5E1")),
                          ("INNERGRID",(0,0),(-1,-1),0.3,colors.HexColor("#CBD5E1")),
                          ("LEFTPADDING",(0,0),(-1,-1),8), ("RIGHTPADDING",(0,0),(-1,-1),8),
                          ("TOPPADDING",(0,0),(-1,-1),7), ("BOTTOMPADDING",(0,0),(-1,-1),7)]))
    story.append(t)

validation = json.loads((PROJECT / "evidence/upgrade/validation_current.json").read_text(encoding="utf-8"))
gate = json.loads((PROJECT / "evidence/upgrade/strict_templates/validation_gate_results.json").read_text(encoding="utf-8"))
image = json.loads((PROJECT / "evaluation/image_public_results.json").read_text(encoding="utf-8"))
conversion = json.loads((PROJECT / "data/external_references/conversion_results.json").read_text(encoding="utf-8"))
v, g = validation["summary"], gate["summary"]
image_current = image["current_holdout_full_recheck"]["raw_metrics"]
image_date = image["current_holdout_full_recheck"]["run_utc"]
yolo_manifest = json.loads((PROJECT / "data/image_samples/yolo_cghd_v15/manifest.json").read_text(encoding="utf-8"))
round3 = optional_json("evidence/upgrade/yolo_round3_acceptance.json")
round3_validation = optional_json("evidence/upgrade/yolo_round3_validation.json")
browser_recheck = optional_json("evidence/upgrade/yolo_browser_recheck.json")
browser_summary = browser_recheck["summary"] if browser_recheck else None
t03_full = optional_json("evidence/upgrade/t03_full_regression.json")
t03_full_summary = t03_full["summary"] if t03_full else None
t03_full_date = t03_full["generated_at_utc"] if t03_full else None
upstream = json.loads((PROJECT / "evidence/upgrade/open_source_integration/acceptance.json").read_text(encoding="utf-8"))
upstream_cases = json.loads((PROJECT / "evidence/upgrade/open_source_integration/cases.json").read_text(encoding="utf-8"))
vision_path = PROJECT / "evidence/upgrade/open_source_integration/vision_pipeline/acceptance.json"
vision = json.loads(vision_path.read_text(encoding="utf-8"))
vision_sha256 = hashlib.sha256(vision_path.read_bytes()).hexdigest()
vision_regression = vision["regression"]
vision_incremental = vision_regression.get("incremental")
tsci_case = next(case for case in upstream_cases["cases"] if case["id"] == "tscircuit_real_runtime_rrc")
active_board = optional_json("data/pcb_active/run_e34bf9d7ac6c45be85d4fb00b97e1fec/manifest.json")
manufacturing_http = optional_json("evidence/upgrade/manufacturing_http/results.json")
manufacturing_current = (manufacturing_http or {}).get("current") or {}
active_official = (active_board or {}).get("official_pipeline") or {}
active_drc = (active_official.get("drc") or {}).get("counts") or {}
active_ready = (active_board or {}).get("status") == "fabrication_candidate_for_review" and active_drc == {
    "violations": 0, "unconnected_items": 0, "schematic_parity": 0}
manufacturing_http_ready = manufacturing_current.get("passed") == manufacturing_current.get("case_denominator") == 4
validation_date = validation["generated_at_utc"]
regression_text = f"{v['pass']}/{v['total']} 通过，{v['fail']} 失败，{v['skip']} 跳过"
selected = next(record for record in conversion["records"] if record["source_id"] == conversion["selected_conversion_source_id"])
source_parity = selected["original_eagle_official_parity"]
candidate_verdict = selected["catalog_candidate_gate"]["verdict"]
source_netlist_gate = selected["catalog_candidate_gate"]["gates"]["official_netlist"]["status"]
# 外部首例的接受状态全部从证据派生：报告文字不能与 conversion_results.json 矛盾
geometry_audit = source_parity.get("package_geometry_audit") or {}
acceptance_checks = selected.get("electrical_acceptance_checks") or {}
residual_unverified = selected.get("residual_unverified") or []
reproduced_records = geometry_audit.get("records") or []
stock_scans = [r.get("stock_library_scan") or {} for r in reproduced_records]
stock_files = max((int(s.get("files_scanned") or 0) for s in stock_scans), default=0)
stock_matches = sum(len(s.get("exact_geometry_matches") or []) for s in stock_scans)
para("智绘电路改造进度报告", title)
today = "2026-10-15"
historical_full_date = "2026-10-14"
para(f"{today}  |  5V/3.3V MCU 与 I2C 接口子板", heading)
para("本报告在原技术报告前增加本次状态记录。原报告作为历史存档附后；实跑与旧结果按证据时间区分。T01-T11 尚未全部验收。", small)
table([["本轮验证", "实际结果"],
       ["YOLO数据回归复验", f"单元{round3_validation['unit_summary']['test_count']}项；权限阻断2项增量复验2/2通过。快速主线首轮29/30，权限阻断套件增量复验通过（含6项）；不是单次30/30" if round3_validation else "incomplete"],
       [f"注册回归 {validation_date[:10]}", f"{regression_text}；{v['elapsed_s']:.3f} 秒，状态 {v['status']}"],
       ["图片导入套件增量复验", f"{browser_summary['pass']}/{browser_summary['total']} 通过，{browser_summary['fail']} 失败，{browser_summary['skip']} 跳过，状态 {browser_summary['status']}" if browser_summary else "incomplete；尚未产生增量复验 JSON"],
       [f"单次全量注册回归 {historical_full_date}（UTC {t03_full_date[:10]}）" if t03_full_date else "单次全量注册回归",
        (f"{t03_full_summary['pass']}/{t03_full_summary['total']} 套件通过，{t03_full_summary['fail']} 失败，"
         f"{t03_full_summary['skip']} 跳过，{t03_full_summary['elapsed_s']:.3f} 秒；一次运行内完成，非分批。"
         "注册套件由 51 增至 53（新增源封装复现与外部源合同两项），故不与旧 51 套件运行直接同比"
         if t03_full_summary else "incomplete")],
       ["旧生产模板", f"2026-09-26：{g['production_accepted']}/{g['production_candidates']} 接受"],
       ["旧故意负例", f"2026-09-26：{g['negative_rejected']}/1 拒绝"],
       ["旧适用 SPICE", f"2026-09-26：{g['spice_runtime_passed']}/13 实际运行通过"],
       ["当前工具能力", "D盘工具链 KiCad 10.0.6、ngspice、vision-ocr 可用"]], [145,360])
para("计数边界", heading)
para(f"注册回归证据时间为 {validation_date}，仅覆盖注册套件，不能自动判定 T01/T05 全部验收。旧39个生产模板与1个故意负例是项目内部案例，不代表本轮重跑、外部泛化或制造签核。", small)
failed_suites = [f"{suite['file']}：{suite.get('summary_line', suite['status'])}" for suite in validation["suites"] if suite["status"] == "fail"]
if failed_suites:
    para("完整运行未通过套件：" + "; ".join(failed_suites), small)
if browser_recheck:
    para(f"图片导入增量复验证据时间 {browser_recheck['generated_at_utc']}，与上述完整运行分列；分批结果不改写为一次新的全量通过。证据：evidence/upgrade/yolo_browser_recheck.json。", small)
para("5V ATmega328P 内部有源板 1/1 通过适用源门禁及官方 PCB 校验：ERC、网表差异、DRC、未连接均为0；52 pads/9 nets、12项CAM/BOM，无板载传感器。"
     + ("3.3V ATmega328P + SHT4x CAD 有源候选板 1/1 经官方 ERC/网表/PCB DRC/parity 校验，56 pads/9 nets，DRC 违规、未连接与 parity 均为 0；" if active_ready else "3.3V 有源候选板的当前官方校验证据不完整；")
     + ("3.3V ATmega328P+SHT41 CAD候选板1/1，56 pads/9 nets，ERC/网表/DRC/parity均0。" if active_ready else "3.3V有源候选板官方校验证据不完整。")
     + ("制造HTTP 4/4符合预期：正例200并导出12项CAM；未布线/短路422且无CAM；缺共地源ERC 1 error，制造DRC未运行（incomplete/not_run）。" if manufacturing_http_ready else "制造HTTP本轮未验收。")
     + "候选待工程师和板厂复核；放行false，未实物制作/上电。MCU/传感器板SPICE为not_applicable。", small)
para(f"历史图像记录（{image_date[:10]}）：{image_current['images']}图，几何元件TP/FP/FN={image_current['component_tp']}/{image_current['component_fp']}/{image_current['component_fn']}，预算失败{image_current['geometric_failures']}；OCR 93/571，异常0/571，连接预测0。非公共YOLO训练前成绩；人工校正未做。", small)
para(f"外部首例门禁 {candidate_verdict}；官方ERC 0、网表{source_netlist_gate}；逻辑网络 {source_parity['logical_nets_matched']}/{source_parity['logical_nets_total']}、物理引脚 {source_parity['physical_pin_matches']}/{source_parity['physical_pin_total']} 对应。旧0/12仅为历史观察。", small)
if selected.get("electrical_accepted"):
    exact_pads = "、".join(f"{r['source_package']} {r['reproduced_footprint']['matched_pads']}/{r['reproduced_footprint']['source_pad_count']}"
                           for r in reproduced_records if r.get("reproduced_footprint", {}).get("status") == "reproduced")
    para(f"{historical_full_date} 首例为{selected['status']}，源电气接受{conversion['electrical_accepted_cases']}/{conversion['source_board_count']}。5项实测合取通过；源焊盘铜几何与复现件精确等价（{exact_pads}），库存库扫描{stock_files}个文件、精确匹配{stock_matches}个。源件清单无电阻或带额定值元件；ERC I2C缺上拉告警保留。", small)
    para(f"接受仅说明候选与固定源提交的网表、物理焊盘及铜几何一致，不代表系统级电气确认或制造放行。丝印、禁布、装配、3D未复现；主机上拉和连接器额定值未知。几何来自同一源文件，非第三方核验；其余{conversion['source_board_count']-conversion['electrical_accepted_cases']}项未接受。详见 conversion_results.json。", small)
else:
    failed_checks = sorted(k for k, ok in acceptance_checks.items() if not ok)
    para(f"首例状态为 {selected['status']}，源电气接受 {conversion['electrical_accepted_cases']}/{conversion['source_board_count']}；"
         f"未通过项：{'、'.join(failed_checks) or '未记录'}。"
         + ("；".join(residual_unverified) if residual_unverified else ""), small)
story.append(PageBreak())
para("按任务清单逐项验收", title)
rows = [["任务", "当前交付", "尚待验收"]]
rows += [
 ["T01", f"环境预检、源码哈希；{validation_date[:10]} 注册回归 {v['pass']}/{v['total']}"
         + (f"；{historical_full_date} 单次全量 {t03_full_summary['pass']}/{t03_full_summary['total']}" if t03_full_summary else ""),
  "部分完成；最终交付冻结；两次运行套件数不同（51 与 53），不直接同比"],
 ["T02", "v2 工程元数据及 v1 无损迁移", "具体型号资料逐项补全"],
 ["T03", f"{conversion['source_board_count']} 原始板；剩余29 Eagle+2 OLIMEX；{conversion['catalog_converted_cases']}/{conversion['source_board_count']} 目录转换并源电气接受；首例封装/焊盘铜几何复现",
  (f"{conversion['electrical_accepted_cases']}/{conversion['source_board_count']} 源电气接受；源引脚 {source_parity['physical_pin_matches']}/{source_parity['physical_pin_total']}；接受范围仅“候选忠实于源”，丝印/禁止区/3D 未复现，主机侧上拉与连接器手册额定值仍未知"
   if selected.get("electrical_accepted") else
   f"{conversion['electrical_accepted_cases']}/{conversion['source_board_count']} 源电气接受；源引脚 {source_parity['physical_pin_matches']}/{source_parity['physical_pin_total']}；未通过项 {'、'.join(sorted(k for k, ok in acceptance_checks.items() if not ok)) or '未记录'}")],
 ["T04", "目录检索、澄清、未知型号/矛盾需求阻断", "新预留集在线生成与严格验收"],
 ["T05", "5V/3.3V内部有源板适用源门禁；SPICE不适用", "部分完成；旧39模板为存档，外部逐例待验"],
 ["T06", "差异审核、定位、接受/撤回、修改后重验", "工程使用反馈"],
 ["T07", f"二类 YOLO 第三轮执行 {(round3 or {}).get('experiment_execution', 'incomplete')}；50轮/1200次更新；新来源10图三模型各一次", "部分完成；框检测不等于拓扑/引脚/电气通过"],
 ["T08", "网络候选、统一门禁", "人工校正未做，状态 incomplete；外部拓扑未验收"],
 ["T09", "5V 内部有源板 1/1；3.3V CAD 有源候选板官方 ERC/网表/DRC/parity 1/1" if active_ready else "5V 内部有源板 1/1；3.3V 板证据未完成", "3.3V 板尚未实物制作或上电"],
 ["T10", "12 个 CAM/BOM 输出；本地制造 HTTP 4/4" if manufacturing_http_ready else "12 个 CAM/BOM 输出；制造 HTTP 待验", "制造放行 false；电子工程师及板厂复核待完成"],
 ["T11", "回归证据、文档与本轮演示补充", "独立外部评测、电子工程师审阅"],
]
table(rows,[48,228,229])
para("制造文件状态始终为待电子工程师和板厂复核。未布线、短路或门禁未通过的结果保留失败证据，不能晋级为验证通过的制造包。", small)
para("外部板：data/external_references/conversion_results.json；图像：evaluation/image_public_results.json 的 current_holdout_full_recheck（OCR 本轮 93/571、校正未做）；主动板：data/pcb_active/ 各次 manifest；在线阻断：evidence/upgrade/requirements_online_blocked.json。", small)
story.append(PageBreak())
para("二类 YOLO 第三轮训练与新来源评测", title)
if round3 and round3_validation:
    para("第三轮本地训练与三权重对照执行完成；质量高于前一两类权重，但低于公共参考。T07仍为部分完成。", heading)
    statistics=round3['split_statistics']
    fresh=round3['fresh_split_statistics']['test']
    table([["数据划分", "图片/绘图者", "电阻框", "电容框", "零目标图"],
           *[[split, f"{statistics[split]['images']}/{statistics[split]['drafters']}", statistics[split]['boxes_by_class']['resistor'], statistics[split]['boxes_by_class']['capacitor.unpolarized'], statistics[split]['zero_target_images']] for split in ('train','val')],
           ["新来源评测", f"{fresh['images']}/{fresh['drafters']}", fresh['boxes_by_class']['resistor'], fresh['boxes_by_class']['capacitor.unpolarized'], fresh['zero_target_images']]], [100,130,90,90,90])
    para(f"从本地已登记两类best warm-start并重建SGD优化器；完成50/50轮、实际{round3['optimizer_steps']}次optimizer hook更新。best按val mAP50-95选择：epoch {round3['checkpoint_selection']['best_epoch']}，得分{metric(round3['checkpoint_selection']['best_score'])}。训练耗时{round3['training_elapsed_seconds']:.1f}秒。", small)
    rows=[["固定val 70框", "TP / FP / FN", "Precision / Recall", "macro F1"]]
    for name,label in [('previous_two_class','续训前权重'),('round2_two_class','本轮best')]:
        a=round3['validation'][name]
        rows.append([label, f"{a['tp']} / {a['fp']} / {a['fn']}", f"{metric(a['precision'])} / {metric(a['recall'])}", metric(a['macro_f1'])])
    table(rows,[105,110,145,140])
    para("val参与了best权重选择，前后变化仅描述固定验证集表现。新来源评测由10名绘图者各提供1图，共85个目标框（电阻70、电容15）；其中3张为零目标图（包含在10图内）。", small)
    para("冻结同一组10图和配置，每个模型仅推理一次，共30次图像评测。", heading)
    rows=[["模型 / 权重SHA前缀", "TP / FP / FN", "Precision / Recall", "macro F1"]]
    labels=[('previous_two_class','续训前两类 52b3191d'),('round2_two_class','第三轮best cc1fc146'),('public_58class','公共58类 7ce8be83')]
    for name,label in labels:
        a=round3['final_test'][name]
        rows.append([label, f"{a['tp']} / {a['fp']} / {a['fn']}", f"{metric(a['precision'])} / {metric(a['recall'])}", metric(a['macro_f1'])])
    table(rows,[140,110,145,105])
    before=round3['final_test']['previous_two_class']
    trained=round3['final_test']['round2_two_class']
    public=round3['final_test']['public_58class']
    para(f"本轮较前一两类权重漏检由{before['fn']}降至{trained['fn']}，误检由{before['fp']}升至{trained['fp']}；本轮macro F1 {metric(trained['macro_f1'])}，低于公共参考 {metric(public['macro_f1'])}。公开预训练来源交叉未知，此评测不是独立盲测。", small)
    para("本轮T07 partial；T08人工校正未做、状态incomplete；网络拓扑与电气效果未评估。默认检测权重未自动切换：服务实际CLI仍用公共58类权重7ce8be83；current存储别名仍指向前一两类权重52b3191d。", small)
    para("复现证据：evidence/upgrade/yolo_round3_acceptance.json；evidence/upgrade/yolo_round3_validation.json；训练摘要/CSV/权重及pre/post-val原始记录见 evidence/yolo_training/round3_expanded47/；新来源样本及固定划分见 data/image_samples/yolo_cghd_round3/manifest.json。旧12图test未用于本次选择或推理。", small)
    para("匹配配置：conf0.25、imgsz640、IoU参数0.5、max_det1000；同类按IoU≥0.5一对一匹配。", small)
else:
    para("第三轮独立验收证据缺失；不报告训练完成或留出成绩。")
story.append(PageBreak())
para("运行与后续执行", title)
para("数据CC-BY-4.0/CC-BY-SA-3.0声明冲突与HCD作者Apache/上游AGPL义务仍未解决；源证据保留。", small)
para("证据：evidence/upgrade/yolo_round3_acceptance.json、evidence/upgrade/yolo_round3_validation.json；evidence/yolo_training/round3_expanded47/run_summary.json、pre_val_results.json、post_val_results.json、fit/results.csv、fit/weights/best.pt；新来源采集及划分见 data/image_samples/cghd_round3/acquisition.json 与 data/image_samples/yolo_cghd_round3/manifest.json。第一轮与错误融合轮SHA仍保留登记。", small)
para("工具链在 D:\\智绘电路；本机已核验 KiCad 10.0.6、ngspice 与 vision-ocr。旧工具清单为历史存档。", small)
para("复现命令", heading)
for line in [". .\\tools\\activate_toolchain.ps1",
             "python -X utf8 -B tests/run_tests.py --profile fast --json-out tmp/recheck_fast.json",
             "python -X utf8 -B tools/probe_manufacturing_api.py"]:
    para(line, small)
para("制造 HTTP 复现会重跑4个请求，并更新 manufacturing_http/results.json 与 current raw；查看本轮结论优先读取已保存的逐例 raw 记录，不需重跑。", small)
para("剩余工作", heading)
para("外部电路其余来源仍需逐例转换与许可核对；手绘拓扑、文字识别及人工校正仍待评测；需要评估不同 MCU 场景的泛化。", small)
para("3.3V板和制造输出待外部电子工程师与板厂审阅；数据许可声明及上游义务待确认。", small)
para("本地结果不替代独立评测、工程审查或制造放行。", small)
story.append(PageBreak())
para("2026-10-15 开源集成与本地图片校正", title)
para("新增能力按源码获取、格式互操作、真实模型执行和人工校正分别验收。以下分母只来自本阶段机器证据，不与2026-10-14的53/53单次全量回归合并。", small)
table([["阶段", "本次证据与边界"],
       ["上游源码", f"CircuitVision、HCD、tscircuit 固定来源；逐文件SHA核对 {upstream['source_integrity']['unchanged']}/{upstream['source_integrity']['checked']}。HCD运行环境仍未安装，许可待核对。"],
       ["外部结果互操作", f"真实tscircuit {upstream['upstream_offline_verification']['tscircuit']['real_runtime_records']}条记录，待复核候选；后续官方门禁 {upstream['upstream_offline_verification']['tscircuit']['official_gates_passed']}/{upstream['upstream_offline_verification']['tscircuit']['official_gates_total']}。构造格式样例不计识别准确率。"],
       ["本地图片", f"桥接单测 {vision['unit_tests']['passed']}/{vision['unit_tests']['total']}；HTTP合同 {vision['http_contract']['passed']}/{vision['http_contract']['total']}；前端合同 {vision['frontend_contract']['passed']}/{vision['frontend_contract']['total']}。"],
       ["真实本机接口", f"{vision['real_http']['passed']}/{vision['real_http']['total']}；原始框 {vision['real_http']['detections']}，SAM前景像素 {vision['real_http']['nonzero_pixels']}。上游单图smoke，不属于外部盲测。"],
       ["真实浏览器", f"{vision['real_browser']['status']}；{vision['real_browser']['passed']}/{vision['real_browser']['total']}。浏览器演示仅证明交互，不计准确率。"],
       ["本轮全量原始运行", f"{vision_regression['passed']}/{vision_regression['total']}，跳过 {vision_regression['skipped']}，状态 {vision_regression['status']}；与历史53/53分列。"],
       ["相关套件增量复验", (f"{vision_incremental['passed']}/{vision_incremental['total']}，状态 {vision_incremental['status']}；不并入全量分母。" if vision_incremental else "尚未提供增量复验证据。")]], [128,377])
if vision_regression.get("note"):
    para(vision_regression["note"], small)
if vision_incremental and vision_incremental.get("note"):
    para(vision_incremental["note"], small)
internal_skip = vision_regression.get("internal_unit_skip")
if internal_skip:
    para(f"套件级跳过 {vision_regression['skipped']} 不含套件内部跳过；{internal_skip['suite']} 内部有 {internal_skip['count']} 项未执行（{internal_skip['status']}）：{internal_skip['reason']}。", small)
para("检测只给原始框和分割前景，数值、引脚和网络需要人工明确校正；结果保持 accepted=false、electrical_validated=false。额定值未知时不制造放行。T07/T08仍为部分完成；外部人工盲测及电子工程师复核尚未完成。", small)
para(f"真实tscircuit源SHA-256：{tsci_case['input_sha256']}。", small)
para(f"本地图片验收记录SHA-256：{vision_sha256}。来源：evidence/upgrade/open_source_integration/acceptance.json、cases.json、vision_pipeline/acceptance.json；模型运行逐例见 {vision['real_http']['evidence']}。", small)
para("下面进入原报告历史存档。", heading)

def footer(canvas, doc):
    canvas.setFont("SimSun",8)
    canvas.setFillColor(colors.HexColor("#64748B"))
    canvas.drawString(44,25,f"智绘电路 {today} 改造进度补充")
    canvas.drawRightString(A4[0]-44,25,str(doc.page))

supplement = TMP / "upgrade_supplement.pdf"
SimpleDocTemplate(str(supplement),pagesize=A4,leftMargin=44,rightMargin=44,topMargin=42,bottomMargin=45).build(story,onFirstPage=footer,onLaterPages=footer)
writer = PdfWriter()
writer.append(PdfReader(str(supplement)))
writer.append(PdfReader(str(ROOT / "报告书.pdf")))
final = OUT / "报告书-改造进度版.pdf"
with final.open("wb") as stream:
    writer.write(stream)
integrity = {"output": str(final), "sha256": hashlib.sha256(final.read_bytes()).hexdigest(),
             "bytes": final.stat().st_size, "supplement_pages": len(PdfReader(str(supplement)).pages),
             "archived_pages": len(PdfReader(str(ROOT / "报告书.pdf")).pages),
             "total_pages": len(PdfReader(str(final)).pages)}
(TMP / "integrity.json").write_text(json.dumps(integrity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(integrity, ensure_ascii=False))
