import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { pathToFileURL, fileURLToPath } from 'node:url';

const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const workspaceDir = path.resolve(project, '..');
const home = process.env.USERPROFILE;
if (!home) throw new Error('USERPROFILE is required to locate the bundled artifact runtime');
const runtime = path.join(home,'.cache/codex-runtimes/codex-primary-runtime/dependencies');
process.env.RUNTIME_NODE_MODULES=path.join(runtime,'node/node_modules');
process.env.RUNTIME_NODE=path.join(runtime,'node/bin/node.exe');
process.env.RUNTIME_PYTHON=path.join(runtime,'python/python.exe');
const presentationCache = path.join(home,'.codex/plugins/cache/openai-primary-runtime/presentations');
const installedReleases = (await fs.readdir(presentationCache,{withFileTypes:true}))
  .filter(entry=>entry.isDirectory()).map(entry=>entry.name).sort();
if (!installedReleases.length) throw new Error('No installed presentations skill release');
const SKILL_DIR = path.join(presentationCache,installedReleases.at(-1),'skills/presentations');
const { Presentation, PresentationFile } = await import(pathToFileURL(path.join(runtime, 'node/node_modules/@oai/artifact-tool/dist/artifact_tool.mjs')).href);
const { finalizePresentation } = await import(pathToFileURL(path.join(SKILL_DIR,'container_tools/artifact_tool_utils.mjs')).href);
const build = path.join(workspaceDir,'tmp/upgrade_deck');
const output = path.join(workspaceDir,'output/presentation');
await fs.mkdir(build,{recursive:true}); await fs.mkdir(output,{recursive:true});
const read = async p => JSON.parse(await fs.readFile(path.join(project,p),'utf8'));
const validationDocument = await read('evidence/upgrade/validation_current.json');
const validation = validationDocument.summary;
const validationDate = validationDocument.generated_at_utc.slice(0,10);
const gate = (await read('evidence/upgrade/strict_templates/validation_gate_results.json')).summary;
const imageDocument = (await read('evaluation/image_public_results.json')).current_holdout_full_recheck;
const imageCurrent = imageDocument.raw_metrics;
const optionalRead = async relative => {
  try { return await read(relative); } catch(error) { if(error.code==='ENOENT')return null; throw error; }
};
const round3 = await read('evidence/upgrade/yolo_round3_acceptance.json');
const round3Validation = await read('evidence/upgrade/yolo_round3_validation.json');
const freshComplete=round3.fresh_test_evaluation==='complete';
const browserRecheck = await optionalRead('evidence/upgrade/yolo_browser_recheck.json');
const browserSummary = browserRecheck?.summary;
const percent = value => value===null?'未定义':`${(value*100).toFixed(2)}%`;
const conversion = await read('data/external_references/conversion_results.json');
const selectedConversion = conversion.records.find(r => r.source_id === conversion.selected_conversion_source_id);
const sourceParity = selectedConversion.original_eagle_official_parity;
const t03Full = await optionalRead('evidence/upgrade/t03_full_regression.json');
const t03Summary = t03Full?.summary;
const t03Date = t03Full?.generated_at_utc?.slice(0,10);
const upstream = await read('evidence/upgrade/open_source_integration/acceptance.json');
const upstreamCases = await read('evidence/upgrade/open_source_integration/cases.json');
const visionEvidencePath = 'evidence/upgrade/open_source_integration/vision_pipeline/acceptance.json';
const visionEvidenceBytes = await fs.readFile(path.join(project,visionEvidencePath));
const vision = JSON.parse(visionEvidenceBytes.toString('utf8'));
const visionEvidenceHash = createHash('sha256').update(visionEvidenceBytes).digest('hex');
const visionRegression = vision.regression;
const visionIncremental = visionRegression.incremental;
const visionInternalSkip = visionRegression.internal_unit_skip;
const tsciCase = upstreamCases.cases.find(item=>item.id==='tscircuit_real_runtime_rrc');
if(!tsciCase)throw new Error('Missing frozen real tscircuit interoperability case');
const activeBoard = await optionalRead('data/pcb_active/run_e34bf9d7ac6c45be85d4fb00b97e1fec/manifest.json');
const manufacturingCurrent = (await optionalRead('evidence/upgrade/manufacturing_http/results.json'))?.current;
const activeCounts = activeBoard?.official_pipeline?.drc?.counts;
const activeReady = activeBoard?.status === 'fabrication_candidate_for_review'
  && activeCounts?.violations === 0 && activeCounts?.unconnected_items === 0 && activeCounts?.schematic_parity === 0;
const manufacturingHttpReady = manufacturingCurrent?.passed === 4 && manufacturingCurrent?.case_denominator === 4;
// 外部首例的接受状态全部从证据派生：幻灯片文字不能与 conversion_results.json 矛盾
const geometryAudit = sourceParity.package_geometry_audit || {};
const acceptanceChecks = selectedConversion.electrical_acceptance_checks || {};
const acceptanceScope = selectedConversion.electrical_acceptance_scope || '';
const residualUnverified = selectedConversion.residual_unverified || [];
const externallyAccepted = !!selectedConversion.electrical_accepted;
const failedChecks = Object.entries(acceptanceChecks).filter(([, ok]) => !ok).map(([key]) => key);
const stockScans = (geometryAudit.records || []).map(r => r.stock_library_scan || {});
const stockFiles = Math.max(0, ...stockScans.map(s => s.files_scanned || 0));
const stockMatches = stockScans.reduce((n, s) => n + (s.exact_geometry_matches || []).length, 0);
const reproPadText = (geometryAudit.records || [])
  .filter(r => r.reproduced_footprint?.status === 'reproduced')
  .map(r => `${r.source_package} ${r.reproduced_footprint.matched_pads}/${r.reproduced_footprint.source_pad_count}`)
  .join('、');
const currentDate = '2026-10-15';
const historicalFullDate = '2026-10-14';
const p = Presentation.create({slideSize:{width:1280,height:720}});
const family = 'Microsoft YaHei';
function text(slide,value,x,y,w,h,size=28,color='#26384A',bold=false) {
  const box=slide.shapes.add({geometry:'textbox',position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});
  box.text=value;box.text.style={typeface:family,fontSize:size,color,bold,autoFit:'none'};
  return box;
}
function slide(title, note) {
  const s=p.slides.add();s.background.fill='#FFFFFF';
  text(s,title,64,44,1150,82,42,'#163A55',true);
  text(s,`智绘电路改造进度  ${currentDate}`,64,656,1150,34,18,'#66788A');
  s.speakerNotes.textFrame.setText(note);
  return s;
}
function table(s,values,widths,y=165,rowHeight=76) {
  const t=s.tables.add({rows:values.length,columns:values[0].length,left:64,top:y,width:1152,height:Math.min(430,values.length*rowHeight),columnWidths:widths,values});
  for(let r=0;r<values.length;r++)for(let c=0;c<values[0].length;c++){
    const cell=t.getCell(r,c);cell.text.style={typeface:family,fontSize:25,color:'#26384A',bold:r===0};cell.fill=r===0?'#E5EFF6':'#FFFFFF';
  }
  t.borders.assign({style:'solid',fill:'#C9D5DF',width:1});
  return t;
}
let s=slide('代码流程与本机验收边界','来源：根目录改造任务清单.md；docs/改造执行状态.md。原智绘电路.pptx保留历史内容。');
text(s,'中文需求与候选审核',64,165,1152,62,32,'#163A55',true);
text(s,'目录检索、缺项澄清、未知型号拒绝、明确矛盾阻断',64,224,1152,58);
text(s,'图片输入与人工校正',64,320,1152,62,32,'#163A55',true);
text(s,`二类 YOLO 第三轮 ${round3.experiment_execution}；新来源10图对照完成；人工校正未做`,64,379,1152,58,27);
text(s,'真实 PCB 与制造校验',64,475,1152,62,32,'#163A55',true);
text(s,`5V 板 1/1；3.3V CAD 有源候选板 ${activeReady?'1/1':'未完成'} 官方校验；制造 HTTP ${manufacturingHttpReady?'4/4':'未完成'}`,64,534,1152,58,27);
s=slide('当前实跑与旧存档','来源：evidence/upgrade/validation_current.json；本轮数据回归：evidence/upgrade/yolo_round3_validation.json；evidence/upgrade/strict_templates/validation_gate_results.json 是 2026-09-26 存档。');
table(s,[['证据时间','对象','结果'],['本轮YOLO数据回归',`${round3Validation.unit_summary.test_count}个单元项`,`权限阻断2项复验2/2；快线首轮29/30，单个阻断suite增量复验通过（含6项）`],[t03Date?`${historicalFullDate}（UTC ${t03Date}）`:'未产生','单次全量注册回归',t03Summary?`${t03Summary.pass}/${t03Summary.total}通过；${t03Summary.fail}失败/${t03Summary.skip}跳过；${t03Summary.elapsed_s.toFixed(3)}秒，一次运行完成`:'incomplete'],['2026-09-26',`模板 ${gate.production_accepted}/${gate.production_candidates} 接受`,'KiCad 10.0.6 实跑'],['2026-09-26',`负例 ${gate.negative_rejected}/1 拒绝`,'内部构造案例'],['2026-09-26',`SPICE ${gate.spice_runtime_passed}/13 通过`,'ngspice；旧存档']],[250,440,462]);
text(s,`注册套件 ${validation.total} → ${t03Summary?.total ?? validation.total}（新增源封装复现与外部源合同）；套件数不同，旧运行不直接同比`,64,610,1152,36,22,'#66788A');
s=slide('候选接受前可以查看差异和识别原文','来源：web/app.js；tests/test_image_import_ui.js；evaluation/image_browser_ocr_results.json。OCR结果需人工确认数值，不能自动当作器件规格。');
['1. 对比新增、删除和修改的器件与连接','2. 查看错误涉及的引脚和候选原理图','3. 图片候选显示原图、识别文字与置信度','4. 下载原始检测、OCR 和拓扑证据','5. 接受或应用草稿后自动重新执行门禁'].forEach((v,i)=>text(s,v,64,165+i*85,1152,64,29));
 s=slide('尚未满足的最终验收条件',`来源：docs/改造执行状态.md；data/external_references/conversion_results.json；evaluation/image_public_results.json 的 current_holdout_full_recheck；evidence/upgrade/yolo_round3_acceptance.json。外部首例候选门禁 ${selectedConversion.catalog_candidate_gate.verdict}、KiCad 10 ERC 为 0，源图逻辑网络 ${sourceParity.logical_nets_matched}/${sourceParity.logical_nets_total}、物理引脚 ${sourceParity.physical_pin_matches}/${sourceParity.physical_pin_total} 对应；旧 0/12 是历史观察。${externallyAccepted?`${historicalFullDate} 首例状态 ${selectedConversion.status}，源电气接受 ${conversion.electrical_accepted_cases}/${conversion.source_board_count}；原阻断项“封装不等价”按复现源封装关闭（焊盘铜几何 ${reproPadText}），库存 ${stockFiles} 个封装全扫精确等价件 ${stockMatches}；上拉与额定值按源件清单关闭，ERC 缺上拉告警保留未抑制。接受范围：${acceptanceScope}。`:`源电气接受 ${conversion.electrical_accepted_cases}/${conversion.source_board_count}；未通过项 ${failedChecks.join('、')||'未记录'}。`}本轮图像 OCR 93/571，旧存档同值另列；人工校正未做。制造 HTTP：evidence/upgrade/manufacturing_http/results.json；3.3V有源板：data/pcb_active/run_e34bf9d7ac6c45be85d4fb00b97e1fec/manifest.json。`);
table(s,[['任务','当前分母与缺口'],['外部电路',`${conversion.catalog_converted_cases}/${conversion.source_board_count} 目录；源接受 ${conversion.electrical_accepted_cases}/${conversion.source_board_count}`+(externallyAccepted?`；接受范围仅“候选忠实于源”，其余 ${conversion.source_board_count-conversion.electrical_accepted_cases} 板未接受`:`；未通过项 ${failedChecks.join('、')||'未记录'}`)],['图片识别',`二类 YOLO ${round3.experiment_execution}；T07 partial，T08人工校正未做/incomplete；网络与引脚尚待验收`],['真实 PCB',`5V 内部板 1/1；3.3V CAD 有源候选板 ${activeReady?'1/1':'未完成'}，56 pads/9 nets`],['最终验收',`制造 HTTP ${manufacturingHttpReady?'4/4':'未完成'}；无实物上电；制造放行 false`]],[300,852]);
text(s,externallyAccepted?'残余：丝印/禁布/装配/3D未复现；主机I2C上拉与连接器额定值未知（详见conversion_results.json）':'整体 T01-T11 清单尚未全部验收。',64,565,1152,60,18,'#A15E22');
s=slide('当前工具与在线评测','来源：evidence/upgrade/validation_current.json；制造逐例结果及raw：evidence/upgrade/manufacturing_http/results.json；3.3V候选板：data/pcb_active/run_e34bf9d7ac6c45be85d4fb00b97e1fec/manifest.json。若运行tools/probe_manufacturing_api.py，会重跑4个HTTP请求并更新results.json/current及raw；查看已有结论优先读取raw，无需重跑。');
text(s,'D盘 KiCad 10、ngspice、vision-ocr 已可用',64,165,1152,66,36,'#163A55',true);
text(s,`${validationDate} 注册回归 ${validation.pass}/${validation.total}；工具清单旧存档`,64,240,1152,66,29);
text(s,'新在线 10 条提示未发送，model_calls=0',64,345,1152,66,29);
text(s,'详细状态与复现命令：docs/改造执行状态.md',64,450,1152,66,29);
text(s,'制造文件待电子工程师及板厂复核',64,545,1152,66,29);

s=slide('二类 YOLO 第三轮训练数据',`来源：evidence/upgrade/yolo_round3_acceptance.json；冻结数据见data/image_samples/yolo_cghd_round2/manifest.json及data/image_samples/yolo_cghd_round3/manifest.json；训练原始证据见evidence/yolo_training/round3_expanded47/。本轮47张训练图、固定4张验证图，50轮和1200次实际optimizer hook更新；best epoch 43，val mAP50-95=0.76557。数据许可声明冲突与HCD许可未决。`);
const trainingStats=round3.split_statistics;
const freshStats=round3.fresh_split_statistics.test;
text(s,'50轮本地训练；旧12图test未用于选择或推理',64,132,1152,38,25);
table(s,[['实际划分','图片 / 绘图者','电阻框','电容框','零目标图'],
  ...['train','val'].map(split=>{const a=trainingStats[split];return [split,`${a.images} / ${a.drafters}`,String(a.boxes_by_class.resistor),String(a.boxes_by_class['capacitor.unpolarized']),String(a.zero_target_images)];}),
  ['新来源评测',`${freshStats.images} / ${freshStats.drafters}`,String(freshStats.boxes_by_class.resistor),String(freshStats.boxes_by_class['capacitor.unpolarized']),String(freshStats.zero_target_images)]],[160,280,220,300,192],195,64);
text(s,`SGD新优化器；实际 ${round3.optimizer_steps} 次更新`,64,465,1152,40,27,'#163A55',true);
text(s,`best按val mAP50-95选择：epoch ${round3.checkpoint_selection.best_epoch}，得分${round3.checkpoint_selection.best_score.toFixed(5)}`,64,514,1152,38,25);
text(s,'新来源评测：10名绘图者各1图，85框，含3张零目标图',64,564,1152,38,24);
text(s,'T07 partial；T08人工校正未做/incomplete',64,608,1152,38,22,'#66788A');

s=slide('新来源10图：三种权重对照',`来源：evidence/upgrade/yolo_round3_acceptance.json及冻结协议evidence/yolo_training/round3_expanded47/fresh_protocol.freeze.json；原始逐图结果在evidence/yolo_training/round3_expanded47/。同一组10图由10名绘图者提供，85目标框（电阻70、电容15），含3张零目标图。三模型各推理一次，共30次模型-图像评估，无重跑。conf=0.25、imgsz=640、IoU参数0.5、max_det=1000；同类IoU≥0.5一对一。公开预训练数据交叉未知，非独立盲测；框检测不评估网络拓扑或电气效果。`);
text(s,`评测执行 ${round3.experiment_execution}；T07仍partial；拓扑/电气未评估`,64,132,1152,38,25);
const rows=[['权重','TP / FP / FN','Precision','Recall','macro F1']];
const stages=[['previous_two_class','续训前 52b3191d'],['round2_two_class','第三轮 cc1fc146'],['public_58class','公共 7ce8be83']];
for(const [name,label] of stages){
  const a=round3.final_test[name];rows.push([label,`${a.tp} / ${a.fp} / ${a.fn}`,percent(a.precision),percent(a.recall),percent(a.macro_f1)]);
}
table(s,rows,[245,287,210,210,200],190,68);
const previous=round3.final_test.previous_two_class;
const trained=round3.final_test.round2_two_class;
const publicRef=round3.final_test.public_58class;
text(s,`漏检 ${previous.fn}→${trained.fn}；误检 ${previous.fp}→${trained.fp}；第三轮 macro F1 ${percent(trained.macro_f1)}，仍低于公共参考 ${percent(publicRef.macro_f1)}`,64,477,1152,42,22,'#163A55',true);
const valBefore=round3.validation.previous_two_class;
const valAfter=round3.validation.round2_two_class;
text(s,`固定val70框：${valBefore.tp}/${valBefore.fp}/${valBefore.fn} → ${valAfter.tp}/${valAfter.fp}/${valAfter.fn}；val参与选best`,64,529,1152,38,23);
text(s,'预训练来源交叉未知；此对照不是独立盲测',64,571,1152,38,24);
text(s,'默认检测器未自动切换；服务CLI仍用公共权重；拓扑/电气未评估',64,614,1152,34,20,'#66788A');

s=slide('三项开源结果已接入待复核流程',`来源：evidence/upgrade/open_source_integration/acceptance.json、cases.json、acquisition.json。真实tscircuit原文SHA-256 ${tsciCase.input_sha256}；CircuitVision与HCD构造合同样例不代表模型预测准确率。`);
text(s,`固定源码逐文件SHA ${upstream.source_integrity.unchanged}/${upstream.source_integrity.checked} 未变`,64,145,1152,54,30,'#163A55',true);
text(s,`真实tscircuit ${upstream.upstream_offline_verification.tscircuit.real_runtime_records}条记录转待复核候选；后续官方门禁 ${upstream.upstream_offline_verification.tscircuit.official_gates_passed}/${upstream.upstream_offline_verification.tscircuit.official_gates_total}`,64,237,1152,58,27);
text(s,'CircuitVision、HCD格式适配仅在完整参数和网络可表达时导入',64,335,1152,58,27);
text(s,'缺值、未知IC或端口歧义整份拒绝；额定值未知，不制造放行',64,433,1152,58,27);
text(s,'HCD完整运行环境未安装；许可核对与外部准确率验收仍未完成',64,531,1152,58,24,'#66788A');

s=slide('本地图片识别保留人工校正边界',`来源：${visionEvidencePath}，SHA-256 ${visionEvidenceHash}；真实接口逐例 ${vision.real_http.evidence}。上游单张样图smoke不代表外部盲测准确率；T07/T08仍partial。`);
text(s,`桥接 ${vision.unit_tests.passed}/${vision.unit_tests.total}；HTTP合同 ${vision.http_contract.passed}/${vision.http_contract.total}；前端合同 ${vision.frontend_contract.passed}/${vision.frontend_contract.total}`,64,150,1152,59,28,'#163A55',true);
text(s,`真实本机接口 ${vision.real_http.passed}/${vision.real_http.total}：原始框 ${vision.real_http.detections}，SAM前景 ${vision.real_http.nonzero_pixels} 像素`,64,245,1152,58,27);
text(s,`真实浏览器 ${vision.real_browser.status}：${vision.real_browser.passed}/${vision.real_browser.total}；只记录交互，不计识别准确率`,64,342,1152,58,27);
text(s,'框与前景不是引脚或网络；数值、连接与额定值需人工确认',64,439,1152,58,27);
text(s,`全量原始运行 ${visionRegression.passed}/${visionRegression.total}，跳过${visionRegression.skipped}，状态${visionRegression.status}；外部盲测未做`,64,525,1152,48,24,'#66788A');
text(s,visionIncremental?`相关增量 ${visionIncremental.passed}/${visionIncremental.total}，状态${visionIncremental.status}；内部 ${visionInternalSkip?.count??0} 项未执行，见备注`:'相关套件增量复验尚未提供证据',64,580,1152,48,22,'#66788A');
s.speakerNotes.textFrame.setText(`来源：${visionEvidencePath}，SHA-256 ${visionEvidenceHash}；真实接口逐例 ${vision.real_http.evidence}。原始全量回归说明：${visionRegression.note||'见验收 JSON'}。增量复验说明：${visionIncremental?.note||'见验收 JSON'}。两轮测试分列。套件级跳过 ${visionRegression.skipped} 不含内部跳过；${visionInternalSkip?`${visionInternalSkip.suite} 内部 ${visionInternalSkip.count} 项 ${visionInternalSkip.status}：${visionInternalSkip.reason}`:'内部跳过信息见验收 JSON'}。上游单张样图smoke不代表外部盲测准确率；T07/T08仍partial。`);

const candidatePath=path.join(build,'candidate.pptx');
await (await PresentationFile.exportPptx(p)).save(candidatePath);
const FINAL_PPTX=path.join(output,'智绘电路-改造进度.pptx');
const previousPath=path.join(build,'previous.pptx');
await fs.rm(path.join(build,'validation.json'),{force:true});
const hadPrevious=await fs.stat(FINAL_PPTX).then(()=>true,()=>false);
if(hadPrevious){await fs.copyFile(FINAL_PPTX,previousPath);await fs.rm(FINAL_PPTX);}
let receipt;
try {receipt=await finalizePresentation({workspaceDir,candidatePath,finalPath:FINAL_PPTX,
  pythonExecutable:path.join(runtime,'python/python.exe'),
  integrityValidatorPath:path.join(SKILL_DIR,'container_tools/inspect_presentation_package_integrity.py'),
  layoutValidatorPath:path.join(SKILL_DIR,'container_tools/inspect_presentation_layout_geometry.py'),
  layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-heading-fit',...[2,4,6,7].flatMap(n=>['--require-native-table-slide',String(n)])],
  explicitTotalSlideCount:9,requiredNativeTableOwnerSlides:[2,4,6,7],
  fontPolicy:{basis:'design',families:[family]},verifyArtifactToolImport:true,
  receiptPath:path.join(build,'validation.json')});}
catch(error){if(hadPrevious)await fs.copyFile(previousPath,FINAL_PPTX);throw error;}
const finalBytes=await fs.readFile(FINAL_PPTX);
const finalArtifact={path:FINAL_PPTX,sha256:createHash('sha256').update(finalBytes).digest('hex'),bytes:finalBytes.length};
receipt.final_artifact=finalArtifact;
await fs.writeFile(path.join(build,'validation.json'),`${JSON.stringify(receipt,null,2)}\n`,'utf8');
for(let i=0;i<p.slides.items.length;i++){
  const preview=await p.export({slide:p.slides.items[i],format:'png',scale:1});
  await fs.writeFile(path.join(build,`slide-${i+1}.png`),new Uint8Array(await preview.arrayBuffer()));
}
if(hadPrevious)await fs.rm(previousPath,{force:true});
console.log(JSON.stringify({output:FINAL_PPTX,receipt},null,2));
