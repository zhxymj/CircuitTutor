/* Edge browser contract test for CircuitVision image-analysis review.
 * Default mode uses controlled HTTP fixtures. --real opts into one actual server.py +
 * local CircuitVision worker run on the upstream circuits_1.jpg sample.
 * --full uses a project-constructed RC image and actual official validation.
 * Commands: node tests/test_upstream_vision_ui.js [--real|--full]
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const net = require('node:net');
const crypto = require('node:crypto');
const zlib = require('node:zlib');
const { spawn, execFileSync } = require('node:child_process');

const web = path.resolve(__dirname, '../web');
const project = path.resolve(__dirname, '..');
const fullMode = process.argv.includes('--full');
const realMode = fullMode || process.argv.includes('--real');
const realEvidenceRoot = process.env.VISION_REAL_EVIDENCE_DIR
  ? path.resolve(process.env.VISION_REAL_EVIDENCE_DIR)
  : path.resolve(__dirname, fullMode ? '../evidence/upgrade/full_workflow/vision' : '../evidence/upgrade/open_source_integration/vision_pipeline');
const realEvidenceDirectory = fullMode
  ? path.join(realEvidenceRoot, 'runs', new Date().toISOString().replace(/[:.]/g, '-')) : realEvidenceRoot;
const evidencePath = filename => path.relative(path.dirname(realEvidenceRoot), path.join(realEvidenceDirectory, filename)).replace(/\\/g, '/');
const python = 'D:/智绘电路/智绘电路工具链/vision-python/Scripts/python.exe';
const toolchain = 'D:/智绘电路/智绘电路工具链';
const edgePaths = [process.env.EDGE_TEST_BINARY,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe'].filter(Boolean);
const edge = edgePaths.find(p => fs.existsSync(p));
if (!edge) { console.log('SKIP: Edge executable unavailable'); process.exit(0); }

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}
function pngChunk(name, data) {
  const type = Buffer.from(name, 'ascii'), length = Buffer.alloc(4), checksum = Buffer.alloc(4);
  length.writeUInt32BE(data.length); checksum.writeUInt32BE(crc32(Buffer.concat([type, data])));
  return Buffer.concat([length, type, data, checksum]);
}
const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(1, 0); ihdr.writeUInt32BE(1, 4);
ihdr[8] = 8; ihdr[9] = 6;
const pngBytes = Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
  pngChunk('IHDR', ihdr), pngChunk('IDAT', zlib.deflateSync(Buffer.from([0, 255, 255, 255, 255]))),
  pngChunk('IEND', Buffer.alloc(0))]);
const pngDataUrl = 'data:image/png;base64,' + pngBytes.toString('base64');
const rawResult = {
  status: 'needs_manual_correction', circuit: null, accepted: false, electrical_validated: false,
  detections: [
    { class_id: 0, class_name: 'resistor', type: 'resistor', score: 0.82, box: { x: 10, y: 20, w: 50, h: 18 }, pins: null, value: null },
    { class_id: 0, class_name: 'resistor', type: 'resistor', score: 0.75, box: { x: 90, y: 20, w: 50, h: 18 }, pins: null, value: null },
    { class_id: 4, class_name: 'unknown IC', type: null, score: 0.61, box: { x: 60, y: 70, w: 80, h: 50 }, pins: null, value: null },
    { class_id: 7, class_name: 'voltage.ac', type: 'voltage_source', score: 0.58, box: { x: 12, y: 62, w: 32, h: 24 }, pins: null, value: null },
  ],
  segmentation: { status: 'segmented', mask_data_url: pngDataUrl, width: 160, height: 100,
    nonzero_pixels: 12, threshold: 0.5, meaning: 'segmentation mask; not a circuit trace' },
  image: { width: 160, height: 100, source_name: 'board.png', sha256: 'raw-image-sha', exif_applied: true },
  topology: { status: 'not_inferred', pins: null, connections: [], nets: [] },
  issues: [
    { code: 'VALUES_UNKNOWN', message: '元件参数未识别，需要人工补充' },
    { code: 'TOPOLOGY_NOT_INFERRED', message: '连线拓扑未识别，需要人工补充' },
    { code: 'ENGINEER_REVIEW_REQUIRED', message: '模型结果不能表示电气验收通过' },
  ],
  provenance: { commit: 'fixture-commit', checkpoint_sha256: 'fixture-checkpoint', elapsed_ms: 20 },
};
const original = { version: 2, title: 'Original', parts: [
  { id: 'R900', type: 'resistor', left: 100, top: 100, attrs: { value: '1kΩ' } },
], connections: [] };
const validateRequests = [];
let visionRequests = [], validateAccepted = false, analysisStatus = 200, validateStatus = 200;
let analysisDelay = 0, validateDelay = 0;
let emptyDetections = false;
const server = http.createServer(async (req, res) => {
  if (req.url === '/' || req.url.startsWith('/static/')) {
    const basename = req.url === '/' ? 'index.html' : path.basename(req.url);
    const filename = path.join(web, basename);
    if (!fs.existsSync(filename)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type', basename.endsWith('.html') ? 'text/html; charset=utf-8' : basename.endsWith('.css') ? 'text/css; charset=utf-8' : 'text/javascript; charset=utf-8');
    res.end(fs.readFileSync(filename)); return;
  }
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  if (req.url === '/api/providers') { res.end(JSON.stringify({ providers: [], deepseek_env_key: false })); return; }
  if (req.url === '/api/image/analyze') {
    let raw = ''; for await (const data of req) raw += data;
    const request = JSON.parse(raw); visionRequests.push(request);
    await delay(analysisDelay);
    if (analysisStatus !== 200) { res.writeHead(analysisStatus); res.end(JSON.stringify({ error: 'fixture analysis unavailable' })); return; }
    const response = JSON.parse(JSON.stringify(rawResult));
    if (emptyDetections) response.detections = [];
    response.image.sha256 = crypto.createHash('sha256').update(Buffer.from(request.image.split(',')[1], 'base64')).digest('hex');
    res.end(JSON.stringify(response)); return;
  }
  if (req.url === '/api/validate') {
    let raw = ''; for await (const data of req) raw += data;
    const body = JSON.parse(raw || '{}'); validateRequests.push(body);
    await delay(validateDelay);
    if (validateStatus !== 200) { res.writeHead(validateStatus); res.end(JSON.stringify({ error: 'fixture validation unavailable' })); return; }
    res.end(JSON.stringify({ accepted: validateAccepted, blocking_gates: validateAccepted ? [] : ['kicad_erc'],
      gates: { erc: { status: validateAccepted ? 'pass' : 'fail', issues: [], evidence: { counts: {} } },
        kicad_erc: { status: validateAccepted ? 'pass' : 'not_run', reason: 'controlled UI fixture' } } })); return;
  }
  res.end('{}');
});

class Cdp {
  constructor(socket) {
    this.socket = socket; this.id = 0; this.waiting = new Map(); this.errors = [];
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.method === 'Runtime.exceptionThrown') this.errors.push(message.params.exceptionDetails.text);
      if (message.method === 'Log.entryAdded') browserLogEntries.push(message.params.entry);
      if (message.method === 'Network.responseReceived') browserResponses.push({id:message.params.requestId,url:message.params.response.url,status:message.params.response.status});
      if(message.method==='Network.requestWillBeSent'&&/^https?:/.test(message.params.request.url)&&!/^http:\/\/127\.0\.0\.1:/.test(message.params.request.url))externalBrowserRequests.push(message.params.request.url);
      if(message.method==='Network.eventSourceMessageReceived'&&message.params.eventName==='done')generationCompletions.push(JSON.parse(message.params.data));
      if(message.method==='Page.javascriptDialogOpening'&&message.params.type==='beforeunload'){
        resetNavigationDialogs.push({type:message.params.type,message:message.params.message,action:'accept intentional test reset navigation'});
        this.send('Page.handleJavaScriptDialog',{accept:true}).catch(error=>this.errors.push(error.message));
      }
      if (!message.id) return;
      const pending = this.waiting.get(message.id); if (!pending) return;
      this.waiting.delete(message.id);
      message.error ? pending.reject(new Error(message.error.message)) : pending.resolve(message.result);
    });
  }
  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++this.id;
      const timer = setTimeout(() => { this.waiting.delete(id); reject(new Error('CDP timeout: ' + method)); }, 5000);
      this.waiting.set(id, { resolve: value => { clearTimeout(timer); resolve(value); },
        reject: error => { clearTimeout(timer); reject(error); } });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
  async eval(expression) {
    const result = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text + ': ' + (result.exceptionDetails.exception && result.exceptionDetails.exception.description));
    return result.result.value;
  }
  async until(expression, timeout = 5000) {
    const end = Date.now() + timeout;
    while (Date.now() < end) { if (await this.eval(expression)) return; await delay(30); }
    throw new Error('Browser state timed out: ' + expression);
  }
}

let cdp, browserCdp, browser, browserPid = null, browserProfileVerified = false;
let profile, baseUrl, realServerProcess, realServerDiagnostics = '';
const results = [], failures = [];
const browserLogEntries=[],browserResponses=[];
const externalBrowserRequests=[],generationCompletions=[];
const resetNavigationDialogs=[];
const expectedHttpErrorIds=new Set();
function unexpectedBrowserLogErrors(){return browserLogEntries.filter(entry=>entry.level==='error'&&!(entry.source==='network'&&expectedHttpErrorIds.has(entry.networkRequestId)&&browserResponses.some(r=>r.id===entry.networkRequestId&&r.status===503&&r.url.endsWith('/api/image/analyze'))));}
async function upload(id, bytes, name, type) {
  await cdp.eval(`(() => { const dt = new DataTransfer(); dt.items.add(new File([Uint8Array.from(atob(${JSON.stringify(Buffer.from(bytes).toString('base64'))}), c=>c.charCodeAt(0))], ${JSON.stringify(name)}, {type:${JSON.stringify(type)}})); const input=document.getElementById(${JSON.stringify(id)}); input.files=dt.files; input.dispatchEvent(new Event('change', {bubbles:true})); })()`);
}
async function setValue(selector, value) {
  await cdp.eval(`(() => { const e=document.querySelector(${JSON.stringify(selector)}); if(!e) throw new Error('Missing control '+${JSON.stringify(selector)}); e.value=${JSON.stringify(value)}; e.dispatchEvent(new Event('change',{bubbles:true})); })()`);
}
async function reset() {
  visionRequests = []; validateRequests.length = 0; validateAccepted = false;
  analysisStatus = 200; validateStatus = 200; analysisDelay = 0; validateDelay = 0;
  emptyDetections = false;
  await cdp.send('Page.navigate', { url: baseUrl });
  await cdp.until("document.readyState === 'complete' && document.getElementById('editAccept') && document.getElementById('file-image')");
  // A real user gesture gives dirty-project navigation the same beforeunload
  // behavior as production. Explicit reset navigation accepts its real dialog.
  const point=await cdp.eval(`(()=>{const e=document.getElementById('workbench-title')||document.getElementById('load-image'),r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);
  await cdp.send('Input.dispatchMouseEvent',{type:'mousePressed',...point,button:'left',clickCount:1});
  await cdp.send('Input.dispatchMouseEvent',{type:'mouseReleased',...point,button:'left',clickCount:1});
  await cdp.eval(`document.querySelector('[data-workbench-tab="files"]')?.click()`);
}
async function reservePort() {
  const probe = net.createServer();
  await new Promise((resolve, reject) => probe.once('error', reject).listen(0, '127.0.0.1', resolve));
  const port = probe.address().port;
  await new Promise((resolve, reject) => probe.close(error => error ? reject(error) : resolve()));
  return port;
}
async function startRealServer() {
  if (!fs.existsSync(python)) throw new Error('vision-python 未找到: ' + python);
  const port = await reservePort();
  realServerProcess = spawn(python, ['-X', 'utf8', '-B', 'server.py'], {
    cwd: project, windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'],
    env: Object.assign({}, process.env, { LSQC_HOST: '127.0.0.1', LSQC_PORT: String(port),
      PYTHONIOENCODING: 'utf-8', DEEPSEEK_API_KEY: '', PYTHON_DOTENV_DISABLED: '1',
      NGSPICE_PATH: path.join(toolchain,'ngspice/Spice64/bin/ngspice_con.exe'),
      KICAD_CLI_PATH: path.join(toolchain,'kicad/bin/kicad-cli.exe'),
      KICAD_SYMBOL_DIR: path.join(toolchain,'kicad/share/kicad/symbols'),
      KICAD_FOOTPRINT_DIR: path.join(toolchain,'kicad/share/kicad/footprints'), KICAD_CLI_TIMEOUT_S: '30' }),
  });
  realServerProcess.stderr.on('data', data => { realServerDiagnostics = (realServerDiagnostics + data).slice(-3000); });
  baseUrl = 'http://127.0.0.1:' + port + '/';
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline) {
    if (realServerProcess.exitCode !== null) throw new Error('真实 server.py 已退出: ' + realServerDiagnostics);
    try {
      const response = await fetch(baseUrl + 'api/health');
      if (response.ok) {const health=await response.json();assert.equal(health.llm_key_configured,false);return health;}
    } catch (_) { /* server starting */ }
    await delay(250);
  }
  throw new Error('真实 server.py 未在 45 秒内健康: ' + realServerDiagnostics);
}
async function check(name, task) {
  const provenance={input_source:'Synthetic board.png and controlled HTTP fixture in tests/test_upstream_vision_ui.js',license:'Project LICENSE (MIT)',data_split:'workflow_smoke_not_holdout'};
  try { await reset(); await task(); results.push({ name, passed: true,...provenance }); console.log('PASS ' + name); }
  catch (err) { failures.push({ name, reason: err.message }); results.push({ name, passed: false, reason: err.message,...provenance }); console.log('FAIL ' + name + ': ' + err.message); }
}
async function chooseVision() {
  assert(await cdp.eval("!!document.getElementById('image-recognition-mode')"), '图片识别方式选择缺失');
  await setValue('#image-recognition-mode', 'circuitvision');
}
async function runAnalysis() {
  await chooseVision();
  await upload('file-image', pngBytes, 'board.png', 'image/png');
}
async function waitForVisionForm() {
  try { await cdp.until("document.getElementById('visionCorrection') && !document.getElementById('visionCorrection').hidden"); }
  catch (error) {
    const state = await cdp.eval("JSON.stringify({status:document.getElementById('edit-status').textContent,meta:document.getElementById('meta').textContent,requests:document.getElementById('image-recognition-mode').value})");
    throw new Error(error.message + '; UI state ' + state);
  }
}
async function fillResistor(index, net1, net2, value) {
  await setValue(`[data-detection-index="${index}"] [data-role="type"]`, 'resistor');
  await setValue(`[data-detection-index="${index}"] [data-role="value"]`, value);
  await setValue(`[data-detection-index="${index}"] [data-pin="1"]`, net1);
  await setValue(`[data-detection-index="${index}"] [data-pin="2"]`, net2);
}
async function runRealSmoke() {
  const sourcePath = path.join(project, fullMode ? 'evidence/upgrade/native_integration/tscircuit_rc.svg' : 'third_party/upstream/CircuitVision/static/images/circuits_1.jpg');
  let sourceBytes = fs.readFileSync(sourcePath);
  const originalSourceBytes = sourceBytes;
  const sourceSha256 = crypto.createHash('sha256').update(sourceBytes).digest('hex');
  await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: `
    window.__appRequests=[]; const originalFetch=window.fetch.bind(window);
    window.fetch=(resource,options)=>{ const url=typeof resource==='string'?resource:resource.url;
      const entry={url:url,method:(options&&options.method)||'GET'};
      if(url==='/api/image/analyze' && options && typeof options.body==='string') {
        try { const p=JSON.parse(options.body); entry.backend=p.backend; entry.source_name=p.source_name; entry.image_data_url_length=p.image.length; } catch(_) {}
      }
      if(url==='/api/validate' && options && typeof options.body==='string') {
        try { entry.body=JSON.parse(options.body); entry.source=entry.body.source; } catch(_) {}
      }
      window.__appRequests.push(entry);
      return originalFetch(resource,options).then(async response=>{
        entry.status=response.status;
        if(url==='/api/validate') entry.response=await response.clone().json();
        if(!response.ok) entry.error_body=(await response.clone().text()).slice(0,1200);
        return response;
      }); };
  ` });
  await cdp.send('Page.navigate', { url: baseUrl });
  await cdp.until("document.readyState === 'complete' && document.getElementById('file-image')");
  await cdp.eval(`document.querySelector('[data-workbench-tab="files"]')?.click()`);
  await upload('file-json', JSON.stringify({ circuit: original }), 'original.json', 'application/json');
  await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')");
  await cdp.until("window.__appRequests.some(item=>item.url==='/api/validate' && item.status !== undefined)");
  await cdp.eval('window.__appRequests=[]'); // exclude the normal validation caused by loading the baseline project
  if (fullMode) {
    const rendered = await cdp.eval(`(async()=>{const image=new Image();image.src='data:image/svg+xml;base64,'+${JSON.stringify(sourceBytes.toString('base64'))};await image.decode();const canvas=document.createElement('canvas');canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;const ctx=canvas.getContext('2d');ctx.fillStyle='white';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(image,0,0);return canvas.toDataURL('image/png').split(',')[1]})()`);
    sourceBytes = Buffer.from(rendered, 'base64');
  }
  await chooseVision();
  await upload('file-image', sourceBytes, fullMode ? 'constructed-rc.png' : path.basename(sourcePath), fullMode ? 'image/png' : 'image/jpeg');
  const waitDeadline = Date.now() + 210000; // Includes upload/server overhead around the 180s worker limit.
  while (Date.now() < waitDeadline) {
    const state = await cdp.eval(`(()=>({ready:!!document.getElementById('visionCorrection')&&!document.getElementById('visionCorrection').hidden,
      status:document.getElementById('edit-status')?.textContent||'',meta:document.getElementById('meta')?.textContent||'',requests:window.__appRequests}))()`);
    if (state.ready) break;
    const lastAnalysis = [...state.requests].reverse().find(item => item.url === '/api/image/analyze');
    const appFailure = /识别失败|返回的图像 SHA|结果已丢弃|请求大小限制/.test(state.status);
    if (appFailure || (lastAnalysis && lastAnalysis.status >= 400))
      throw new Error('真实识别请求结束但未显示结果面板: ' + JSON.stringify({ status: state.status, meta: state.meta,
        request: lastAnalysis || null, server_stderr: realServerDiagnostics }));
    await delay(250);
  }
  const visible = await cdp.eval("!!document.getElementById('visionCorrection')&&!document.getElementById('visionCorrection').hidden");
  if (!visible) throw new Error('真实识别等待210秒仍无结果面板；server_stderr=' + realServerDiagnostics);

  const requestLog = await cdp.eval('window.__appRequests');
  assert(requestLog.some(item => item.url === '/api/image/analyze' && item.method === 'POST'), '浏览器未调用本机图片识别接口');
  assert.equal(requestLog.some(item => item.url === '/api/validate'), false, '原始模型结果不应自动进入电气校验');
  assert.equal(await cdp.eval("document.getElementById('editReview').classList.contains('on')"), false, '模型输出不应自动晋级候选审核');
  assert.equal(await cdp.eval("!!document.querySelector('#svg-host g[data-part=R900]')"), true, '原工程画布不应被原始识别结果替换');
  assert.equal(await cdp.eval("document.querySelectorAll('#visionOverlay image[data-layer=mask]').length"), 1, '页面未展示真实分割 mask');

  const pngBase64 = await cdp.eval(`(async()=>{window.__downloads=[]; URL.createObjectURL=blob=>{window.__downloads.push(blob.arrayBuffer());return 'blob:real'}; HTMLAnchorElement.prototype.click=function(){}; document.getElementById('visionDownloadInput').click(); const bytes=new Uint8Array(await window.__downloads[0]); let binary=''; for(let i=0;i<bytes.length;i+=16384) binary+=String.fromCharCode(...bytes.subarray(i,i+16384)); return btoa(binary)})()`);
  const pngBytes = Buffer.from(pngBase64, 'base64');
  const inputSha256 = crypto.createHash('sha256').update(pngBytes).digest('hex');
  await cdp.eval("window.__downloads=[]; document.getElementById('visionDownloadRaw').click()");
  const rawText = await cdp.eval("Promise.all(window.__downloads).then(async a=>new TextDecoder().decode(a[0]))");
  const raw = JSON.parse(rawText);
  assert.equal(raw.status, 'needs_manual_correction');
  assert.equal(raw.circuit, null); assert.equal(raw.accepted, false); assert.equal(raw.electrical_validated, false);
  assert.equal(raw.segmentation.status, 'segmented'); assert.match(raw.segmentation.mask_data_url, /^data:image\/png;base64,/);
  assert.equal(raw.image.sha256, inputSha256, 'raw response SHA must identify the exact browser-normalized PNG bytes');
  assert.equal(raw.image.source_name, fullMode ? 'constructed-rc.png' : path.basename(sourcePath));
  assert.equal(await cdp.eval("document.querySelectorAll('#visionOverlay [data-detection-index]').length"), raw.detections.length);

  await cdp.eval("document.getElementById('visionCorrection').scrollIntoView({block:'start'})");
  const screenshot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  const evidenceDirectory = realEvidenceDirectory;
  fs.mkdirSync(evidenceDirectory, { recursive: true });
  const inputPath = path.join(evidenceDirectory, 'recognition-input.png');
  fs.writeFileSync(inputPath, pngBytes);
  const screenshotPath = path.join(evidenceDirectory, 'recognition-preview.png');
  fs.writeFileSync(screenshotPath, Buffer.from(screenshot.data, 'base64'));
  if (fullMode) return await finishFullCorrection(raw, { sourcePath, originalSourceBytes, sourceSha256,
    inputPath, inputSha256, screenshotPath, pngBytes, requestLog });
  return { source_image: { path: 'third_party/upstream/CircuitVision/static/images/circuits_1.jpg',
      mime: 'image/jpeg', size_bytes: sourceBytes.length, sha256: sourceSha256 },
    browser_model_input: { mime: 'image/png', size_bytes: pngBytes.length, width: raw.image.width,
      height: raw.image.height, sha256: inputSha256,
      path: path.relative(project, inputPath) },
    http: { analyze_post: true, validate_called: false, server_health: 'ok' },
    ui: { raw_panel_visible: true, detections: raw.detections.length, mask_status: raw.segmentation.status,
      candidate_review_open: false, original_canvas_part_R900_preserved: true },
    raw_response: raw, screenshot: path.relative(project, screenshotPath),
    screenshot_sha256: crypto.createHash('sha256').update(fs.readFileSync(screenshotPath)).digest('hex'),
    scope: 'scripted_workflow_smoke / not_ground_truth_correction; one real upstream sample through local server.py and CircuitVision worker; no accuracy or manual-correction claim' };
}

async function finishFullCorrection(raw, input) {
  fs.writeFileSync(path.join(realEvidenceDirectory,'raw.json'), JSON.stringify(raw,null,2)+'\n');
  fs.writeFileSync(path.join(realEvidenceDirectory,'mask.png'), Buffer.from(raw.segmentation.mask_data_url.split(',')[1], 'base64'));
  const truth = [
    {source_id:'R1',type:'resistor',value:'10kΩ',pins:{'1':'IN','2':'MID'},x:158.536,y:58.138},
    {source_id:'C1',type:'capacitor',value:'100nF',pins:{'+':'MID','-':'GND'},source_pin_mapping:{'1':'+','2':'-'},x:863.240,y:58.138},
    {source_id:'R2',type:'resistor',value:'100kΩ',pins:{'1':'IN','2':'GND'},x:510.888,y:551.431},
  ];
  const unused = new Set(raw.detections.map((_,i)=>i)), assignments=[];
  for (const component of truth) {
    const possible = [...unused].filter(index=>raw.detections[index].type===component.type).map(index=>{const b=raw.detections[index].box;
      return {index,d:Math.hypot((b.x+b.w/2)-component.x,(b.y+b.h/2)-component.y)};})
      .filter(p=>p.d<65).sort((a,b)=>a.d-b.d);
    let index;
    if (possible.length) { index=possible[0].index; unused.delete(index); }
    else {
      await cdp.eval("document.getElementById('visionAddMissing').click()");
      index = await cdp.eval("Number(document.querySelector('#visionCorrectionRows fieldset:last-child').getAttribute('data-detection-index'))");
    }
    await setValue(`[data-role="correction-row"][data-detection-index="${index}"] [data-role="type"]`, component.type);
    await setValue(`[data-role="correction-row"][data-detection-index="${index}"] [data-role="value"]`, component.value);
    for (const [pin,net] of Object.entries(component.pins))
      await setValue(`[data-role="correction-row"][data-detection-index="${index}"] [data-pin="${pin}"]`,net);
    assignments.push({source_id:component.source_id,correction_index:index,added_manually:index>=raw.detections.length,
      type:component.type,value:component.value,pins:component.pins});
  }
  for (const index of unused) await cdp.eval(`document.querySelector('[data-role="correction-row"][data-detection-index="${index}"] [data-role="exclude"]').click()`);
  assert.equal(await cdp.eval("document.getElementById('visionBuild').disabled"),false);
  await cdp.eval("document.getElementById('visionCorrection').scrollIntoView({block:'start'})");
  const correctedShot=await cdp.send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
  fs.writeFileSync(path.join(realEvidenceDirectory,'corrected-form.png'),Buffer.from(correctedShot.data,'base64'));
  await cdp.eval("document.getElementById('visionBuild').click()");
  await cdp.until("document.getElementById('editReview').classList.contains('on')",60000);
  const requests=await cdp.eval('window.__appRequests');
  const validationRequest=requests.find(r=>r.url==='/api/validate');
  assert(validationRequest && validationRequest.response,'actual validation response must be recorded');
  const validation=validationRequest.response;
  fs.writeFileSync(path.join(realEvidenceDirectory,'validation.json'),JSON.stringify(validation,null,2)+'\n');
  const reports=await cdp.eval(`(async()=>{window.__saved=[];URL.createObjectURL=blob=>{window.__saved.push(blob.text());return 'blob:full'};HTMLAnchorElement.prototype.click=function(){};const b=[...document.querySelectorAll('#editReviewBody button')];b.find(x=>x.textContent.includes('人工校正')).click();return await Promise.all(window.__saved)})()`);
  const correction=JSON.parse(reports[0]);
  fs.writeFileSync(path.join(realEvidenceDirectory,'corrected.json'),JSON.stringify(correction,null,2)+'\n');
  assert.equal(correction.circuit.parts.length,3);
  assert.equal(correction.circuit.connections.length,3);
  assert.equal(correction.circuit.parts.some(p=>['battery','gnd'].includes(p.type)),false,'no absent power or ground symbol may be invented');
  assert.equal(validation.accepted,true,'constructed passive RC must pass actual official gates');
  for (const gate of ['official_erc','official_netlist']) assert.equal(validation.gates[gate].status,'pass');
  assert.equal(validation.gates.official_erc.evidence.total,0);
  assert.deepEqual(validation.gates.official_netlist.evidence.differences,[]);
  assert.equal(await cdp.eval("document.getElementById('editAccept').disabled"),false,
    'the actual accepted validation response must allow applying the reviewed candidate');
  const reviewShot=await cdp.send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
  fs.writeFileSync(path.join(realEvidenceDirectory,'review.png'),Buffer.from(reviewShot.data,'base64'));
  await cdp.eval("document.getElementById('editAccept').click()");
  await cdp.until("document.querySelectorAll('#svg-host g[data-part]').length===3");
  const applied=await cdp.eval("[...document.querySelectorAll('#svg-host g[data-part]')].map(e=>e.dataset.part)");
  assert.equal(applied.includes('R900'),false);
  await cdp.eval("document.getElementById('undo').click()");
  await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')");
  assert.equal(await cdp.eval("document.querySelectorAll('#svg-host g[data-part]').length"),1);
  return {status:'passed',scope:'workflow_constructed_smoke_not_holdout',review_scope:'scripted_correction_not_engineer_review',
    sample_count:1,denominator:1,passed:1,source:{path:path.relative(project,input.sourcePath),sha256:input.sourceSha256,
      size_bytes:input.originalSourceBytes.length,origin:'project constructed tscircuit RC example',
      license:'Project-owned TSX example; renderer runtime attribution: third_party/tscircuit-runtime-attribution.json',
      truth_source:'evidence/upgrade/native_integration/tscircuit_rc.tsx',
      truth_sha256:crypto.createHash('sha256').update(fs.readFileSync(path.join(project,'evidence/upgrade/native_integration/tscircuit_rc.tsx'))).digest('hex'),
      split:'workflow_constructed_smoke_not_holdout',render:'existing Edge native SVG Image + Canvas PNG, no new dependencies'},
    raw:{detections:raw.detections.length,mask_status:raw.segmentation.status,accepted:raw.accepted,
      electrical_validated:raw.electrical_validated,circuit:raw.circuit,path:evidencePath('raw.json'),
      model_provenance:raw.provenance,input_sha256:input.inputSha256,input_path:evidencePath('recognition-input.png'),
      preview_path:evidencePath('recognition-preview.png'),mask_path:evidencePath('mask.png'),
      mask_origin:'decoded_from_downloaded_raw_json'},
    correction:{method:'scripted_correction_not_engineer_review',truth,assignments,explicitly_excluded_indices:[...unused],
      added_components:assignments.filter(a=>a.added_manually).length,parts:3,connections:3,path:evidencePath('corrected.json'),
      form_screenshot:evidencePath('corrected-form.png')},
    validation:{http_status:validationRequest.status,source:validationRequest.source,accepted:validation.accepted,
      gates:Object.fromEntries(Object.entries(validation.gates).map(([key,value])=>[key,value.status])),
      official_erc_violations:validation.gates.official_erc.evidence.total,
      official_netlist_differences:validation.gates.official_netlist.evidence.differences,path:evidencePath('validation.json')},
    ui:{raw_detections_and_mask_shown:true,original_preserved_until_apply:true,applied_ids:applied,undo_restored_original_R900:true,
      review_screenshot:evidencePath('review.png')},
    frozen:{new_dependencies:0,cloud_api_calls:0,training_runs:0,fresh10_inferences:0},
    limits:['One project-constructed passive RC image, not held-out accuracy evidence',
      'Script fills explicit source-truth corrections; not independent engineer review or independent T08 acceptance',
      'Mask and wire geometry do not infer electrical pin topology; all pin nets explicitly corrected',
      'Passive circuit has no excitation source; successful SPICE is a zero-excitation structural smoke, not functional validation']};
}

function saveFullRun(run) {
  const filename=path.join(realEvidenceRoot,'../vision.json');
  fs.mkdirSync(path.dirname(filename),{recursive:true});
  const evidence=fs.existsSync(filename)?JSON.parse(fs.readFileSync(filename,'utf8')):{schema_version:1,runs:[]};
  evidence.runs.push(run); fs.writeFileSync(filename,JSON.stringify(evidence,null,2)+'\n');
}

(async () => {
  let realHealth = null;
  if (realMode) realHealth = await startRealServer();
  else {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    baseUrl = 'http://127.0.0.1:' + server.address().port + '/';
  }
  profile = fs.mkdtempSync(path.join(os.tmpdir(), 'circuit-upstream-vision-ui-'));
  browser = spawn(edge, ['--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run', '--no-default-browser-check', '--enable-automation',
    '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], { windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] });
  let browserDiagnostics = '';
  browser.stderr.on('data', data => { browserDiagnostics = (browserDiagnostics + data).slice(-1500); });
  const activePort = path.join(profile, 'DevToolsActivePort');
  for (let attempt = 0; !fs.existsSync(activePort) && attempt < 150; attempt++) await delay(50);
  if (!fs.existsSync(activePort)) throw new Error('Edge did not expose DevTools: ' + browserDiagnostics);
  const port = fs.readFileSync(activePort, 'utf8').split(/\r?\n/)[0];
  const version = await (await fetch('http://127.0.0.1:' + port + '/json/version')).json();
  const browserSocket = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { browserSocket.addEventListener('open', resolve, { once: true }); browserSocket.addEventListener('error', reject, { once: true }); });
  browserCdp = new Cdp(browserSocket);
  const processInfo = await browserCdp.send('SystemInfo.getProcessInfo');
  const browserProcess = processInfo.processInfo.find(item => item.type === 'browser');
  if (browserProcess) browserPid = browserProcess.id;
  const commandLine = await browserCdp.send('Browser.getBrowserCommandLine');
  const profileArgument = (commandLine.arguments || []).find(arg => arg.toLowerCase().startsWith('--user-data-dir='));
  browserProfileVerified = !!profileArgument && path.resolve(profileArgument.slice(profileArgument.indexOf('=') + 1)).toLowerCase()
    === path.resolve(profile).toLowerCase();
  if (!browserProfileVerified) throw new Error('CDP 浏览器命令行未确认指向本次临时 profile；拒绝操作未核实的 Edge 进程');
  const targets = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
  const target = targets.find(t => t.type === 'page');
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
  cdp = new Cdp(socket); await cdp.send('Runtime.enable'); await cdp.send('Page.enable');await cdp.send('Log.enable');await cdp.send('Network.enable');

  if (realMode) {
    let realEvidence;
    try { realEvidence = await runRealSmoke(); }
    catch (error) {
      if (fullMode) {
        saveFullRun({timestamp:new Date().toISOString(),command:'node tests/test_upstream_vision_ui.js --full',
          status:'failed',reason:error.message,server_health:realHealth,server_stderr:realServerDiagnostics,browserLogEntries,browserResponses,
          artifact_directory:path.relative(path.dirname(realEvidenceRoot),realEvidenceDirectory).replace(/\\/g,'/'),
          scope:'workflow_constructed_smoke_not_holdout',denominator:1,passed:0});
        throw error;
      }
      const evidencePath = path.join(realEvidenceDirectory, 'browser_e2e.json');
      fs.mkdirSync(path.dirname(evidencePath), { recursive: true });
      fs.writeFileSync(evidencePath, JSON.stringify({ timestamp: new Date().toISOString(),
        command: 'node tests/test_upstream_vision_ui.js --real', browser: 'Microsoft Edge (CDP)',
        status: 'failed', reason: error.message, server_health: realHealth,
        server_stderr: realServerDiagnostics,
        scope: 'actual local server.py + CircuitVision worker attempt; failure retained for diagnosis' }, null, 2) + '\n');
      throw error;
    }
    realEvidence.timestamp = new Date().toISOString();
    realEvidence.command = 'node tests/test_upstream_vision_ui.js ' + (fullMode ? '--full' : '--real');
    realEvidence.browser = 'Microsoft Edge (CDP)';
    realEvidence.health_response = realHealth;
    realEvidence.browserLogEntries=browserLogEntries;realEvidence.browserResponses=browserResponses;
    realEvidence.uncaughtBrowserErrors=cdp.errors;
    assert.deepEqual(cdp.errors,[],'Real model workflow uncaught browser errors');
    realEvidence.server_configuration={direct_server_py:true,DEEPSEEK_API_KEY:'empty',PYTHON_DOTENV_DISABLED:'1'};
    realEvidence.externalBrowserRequests=externalBrowserRequests;realEvidence.generationCompletions=generationCompletions;realEvidence.llmCalls=generationCompletions.reduce((n,g)=>n+(Number(g.budget_used)||0),0);
    assert.equal(realEvidence.llmCalls,0);assert.deepEqual(externalBrowserRequests,[]);
  assert.deepEqual(unexpectedBrowserLogErrors(),[],'Real model workflow browser errors');
    if (fullMode) saveFullRun(realEvidence);
    else fs.writeFileSync(path.join(realEvidenceDirectory, 'browser_e2e.json'), JSON.stringify(realEvidence, null, 2) + '\n');
    results.push({ name: 'real local server.py and CircuitVision worker raw-result smoke', passed: true });
    console.log('PASS real local server.py and CircuitVision worker raw-result smoke');
  } else {
  await check('CircuitVision mode sends the resized image to the local analysis API and shows unmodified raw detections', async () => {
    await runAnalysis(); await waitForVisionForm();
    assert.equal(visionRequests.length, 1);
    assert.equal(visionRequests[0].backend, 'circuitvision');
    assert.equal(visionRequests[0].source_name, 'board.png');
    assert.match(visionRequests[0].image, /^data:image\/png;base64,/);
    assert.equal(await cdp.eval("document.querySelectorAll('#visionOverlay [data-detection-index]').length"), 4);
    assert.equal(await cdp.eval("document.querySelectorAll('#visionOverlay image[data-layer=mask]').length"), 1);
    assert.equal(await cdp.eval("document.querySelector('#visionCorrection').textContent.includes('unknown IC')"), true);
    assert.equal(await cdp.eval("!!document.getElementById('visionDownloadInput')"), true, '模型实际输入PNG应可独立下载');
    assert.equal(validateRequests.length, 0, '未经人工确认的模型结果不应送入电路校验');
    assert.equal(await cdp.eval("document.querySelectorAll('#svg-host g[data-part]').length"), 0, '原画布被模型结果改动');
    await cdp.eval("window.__saved=[]; URL.createObjectURL=blob=>{window.__saved.push(blob.text());return 'blob:fixture'}; HTMLAnchorElement.prototype.click=function(){}; document.getElementById('visionDownloadRaw').click()");
    const raw = await cdp.eval("Promise.all(window.__saved).then(a=>a[0])");
    assert.equal(JSON.parse(raw).detections[2].class_name, 'unknown IC');
    assert.equal(JSON.parse(raw).detections[3].class_name, 'voltage.ac');
    await cdp.eval("window.__saved=[]; URL.createObjectURL=blob=>{window.__saved.push(blob.arrayBuffer());return 'blob:fixture'}; HTMLAnchorElement.prototype.click=function(){}; document.getElementById('visionDownloadInput').click()");
    const inputBase64 = await cdp.eval("Promise.all(window.__saved).then(a=>btoa(String.fromCharCode(...new Uint8Array(a[0]))))");
    assert.equal(inputBase64, visionRequests[0].image.split(',')[1], '下载PNG必须与实际发送给模型的字节完全一致');
  });

  await check('unknown IC must be explicitly classified or excluded before a candidate can be built', async () => {
    await runAnalysis(); await waitForVisionForm();
    await fillResistor(0, 'SHARED', 'R1_B', '10kΩ'); await fillResistor(1, 'SHARED', 'R2_B', '1kΩ');
    assert.equal(await cdp.eval("document.getElementById('visionBuild').disabled"), true);
    assert.equal(validateRequests.length, 0);
    await cdp.eval("document.querySelector('[data-detection-index=\"2\"] [data-role=\"exclude\"]').click()");
    assert.equal(await cdp.eval("document.getElementById('visionBuild').disabled"), true, '未人工处理AC源类别不可静默略过');
    await cdp.eval("document.querySelector('[data-detection-index=\"3\"] [data-role=\"exclude\"]').click()");
    assert.equal(await cdp.eval("document.getElementById('visionBuild').disabled"), false);
    assert((await cdp.eval("document.querySelector('[data-role=\"correction-row\"][data-detection-index=\"2\"]').textContent")).includes('排除'));
  });

  await check('manual type, value, and per-pin nets build a separate candidate then use the real validation result and existing undo', async () => {
    await upload('file-json', JSON.stringify({ circuit: original }), 'original.json', 'application/json');
    await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')");
    await runAnalysis(); await waitForVisionForm();
    await fillResistor(0, 'SHARED', 'R1_B', '10kΩ'); await fillResistor(1, 'SHARED', 'R2_B', '1kΩ');
    await cdp.eval("document.querySelector('[data-detection-index=\"2\"] [data-role=\"exclude\"]').click()");
    await cdp.eval("document.querySelector('[data-detection-index=\"3\"] [data-role=\"exclude\"]').click()");
    await cdp.eval("document.getElementById('visionBuild').click()");
    await cdp.until("document.getElementById('editReview').classList.contains('on')");
    assert.equal(validateRequests.length, 1);
    assert.equal(validateRequests[0].source, 'image');
    assert.deepEqual(validateRequests[0].circuit.parts.map(p => [p.id, p.attrs.value]), [['R1', '10kΩ'], ['R2', '1kΩ']]);
    assert.deepEqual(validateRequests[0].circuit.connections.map(c => c.slice(0, 2)), [['R1:1', 'R2:1']]);
    assert.equal(validateRequests[0].circuit.provenance.recognition_sha256,
      crypto.createHash('sha256').update(Buffer.from(visionRequests[0].image.split(',')[1], 'base64')).digest('hex'));
    assert.equal(await cdp.eval("document.getElementById('editAccept').textContent"), '应用未验证草稿');
    assert((await cdp.eval("document.getElementById('editReviewBody').textContent")).includes('原始模型识别证据'));
    assert((await cdp.eval("document.getElementById('editReviewBody').textContent")).includes('下载人工校正证据'));
    assert((await cdp.eval("document.getElementById('editReviewBody').textContent")).includes('额定值仍需依据具体料号核对'));
    await cdp.eval("document.getElementById('editAccept').click()");
    assert(await cdp.eval("!!document.querySelector('#svg-host g[data-part=R1]')"));
    assert.equal(await cdp.eval("document.getElementById('undo').disabled"), false);
    await cdp.eval("document.getElementById('undo').click()");
    assert(await cdp.eval("!!document.querySelector('#svg-host g[data-part=R900]')"));
  });

  await check('known detection labels are still not preselected as manual component types or pin nets', async () => {
    await runAnalysis(); await waitForVisionForm();
    assert.equal(await cdp.eval("document.querySelector('[data-detection-index=\"0\"] [data-role=\"type\"]').value"), '');
    assert.equal(await cdp.eval("document.querySelector('[data-detection-index=\"3\"] [data-role=\"type\"]').value"), '', '模型给出的 voltage_source hint 不能静默当成人工确认');
    await setValue('[data-detection-index="0"] [data-role="type"]', 'resistor');
    assert.equal(await cdp.eval("document.querySelector('[data-detection-index=\"0\"] [data-pin=\"1\"]').value"), '');
  });

  await check('missing components can be added and deleted without changing raw detections; unsupported pin 999 blocks build', async () => {
    await runAnalysis(); await waitForVisionForm();
    await cdp.eval("document.querySelectorAll('[data-role=exclude]').forEach(e=>e.click()); document.getElementById('visionAddMissing').click()");
    await fillResistor(4, 'A', 'B', '12kΩ');
    assert.equal(await cdp.eval("document.getElementById('visionBuild').disabled"), false);
    await cdp.eval(`(()=>{const row=document.querySelector('[data-role="correction-row"][data-detection-index="4"]');const pin=document.createElement('input');pin.setAttribute('data-pin','999');pin.value='A';row.appendChild(pin);pin.dispatchEvent(new Event('input',{bubbles:true}))})()`);
    assert.equal(await cdp.eval("document.getElementById('visionBuild').disabled"), true, 'unknown physical pin cannot be accepted');
    await cdp.eval("document.querySelector('[data-pin=\"999\"]').remove(); document.querySelector('[data-detection-index=\"4\"] [data-role=\"value\"]').dispatchEvent(new Event('input',{bubbles:true}))");
    await cdp.eval("document.querySelector('[data-role=remove-manual]').click()");
    assert.equal(await cdp.eval("document.querySelectorAll('[data-added-manually]').length"), 0);
    assert.equal(await cdp.eval("document.getElementById('visionBuild').disabled"), true, 'deleting the only included part must block build');
    await cdp.eval("document.getElementById('visionAddMissing').click()");
    await fillResistor(5, 'A', 'B', '12kΩ');
    await cdp.eval("document.getElementById('visionBuild').click()");
    await cdp.until("document.getElementById('editReview').classList.contains('on')");
    assert.equal(validateRequests[0].circuit.parts.length, 1);
    const reports = await cdp.eval(`(async()=>{window.__saved=[];URL.createObjectURL=blob=>{window.__saved.push(blob.text());return 'blob:fixture'};HTMLAnchorElement.prototype.click=function(){};const b=[...document.querySelectorAll('#editReviewBody button')];b.find(e=>e.textContent.includes('原始模型')).click();b.find(e=>e.textContent.includes('人工校正')).click();return await Promise.all(window.__saved)})()`);
    const raw=JSON.parse(reports[0]), correction=JSON.parse(reports[1]);
    assert.equal(raw.detections.length, 4);
    assert.equal(raw.detections[0].value, null);
    assert.equal(correction.detections.length, 5);
    assert.equal(correction.detections[4].added_manually, true);
    assert.equal(correction.detections[4].detection_index, null);
  });

  await check('zero detections allow explicit missing-component correction, while cancel preserves the original project', async () => {
    emptyDetections = true;
    await upload('file-json', JSON.stringify({circuit:original}), 'original.json', 'application/json');
    await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')");
    await runAnalysis(); await waitForVisionForm();
    assert.equal(await cdp.eval("document.querySelectorAll('[data-role=correction-row]').length"), 0);
    assert.equal(await cdp.eval("document.getElementById('visionBuild').disabled"), true);
    await cdp.eval("document.getElementById('visionAddMissing').click()");
    await fillResistor(0, 'A', 'B', '12kΩ');
    assert.equal(await cdp.eval("document.getElementById('visionBuild').disabled"), false);
    await cdp.eval("document.getElementById('visionCancel').click()");
    assert.equal(validateRequests.length, 0);
    assert(await cdp.eval("!!document.querySelector('#svg-host g[data-part=R900]')"));
    assert.equal(await cdp.eval("document.getElementById('visionCorrectionHost').hidden"), true);
  });

  await check('raw model, human correction, and electrical validation downloads remain separate records', async () => {
    await runAnalysis(); await waitForVisionForm();
    await fillResistor(0, 'SHARED', 'R1_B', '10kΩ'); await fillResistor(1, 'SHARED', 'R2_B', '1kΩ');
    await cdp.eval("document.querySelector('[data-role=\"correction-row\"][data-detection-index=\"2\"] [data-role=\"exclude\"]').click()");
    await cdp.eval("document.querySelector('[data-role=\"correction-row\"][data-detection-index=\"3\"] [data-role=\"exclude\"]').click()");
    await cdp.eval("document.getElementById('visionBuild').click()");
    await cdp.until("document.getElementById('editReview').classList.contains('on')");
    const reports = await cdp.eval(`(async()=>{window.__saved=[]; URL.createObjectURL=blob=>{window.__saved.push(blob.text());return 'blob:fixture'}; HTMLAnchorElement.prototype.click=function(){}; const buttons=Array.from(document.querySelectorAll('#editReviewBody button')); buttons.find(b=>b.textContent.includes('原始模型')).click(); buttons.find(b=>b.textContent.includes('人工校正')).click(); buttons.find(b=>b.textContent.includes('电气校验报告')).click(); return await Promise.all(window.__saved)})()`);
    assert.equal(reports.length, 3);
    const raw = JSON.parse(reports[0]), correction = JSON.parse(reports[1]), validation = JSON.parse(reports[2]);
    assert.equal(raw.circuit, null); assert.equal(raw.detections[0].value, null);
    assert.equal(correction.detections[0].value, '10kΩ');
    assert.equal(correction.detections[2].reason, 'user_marked_false_positive');
    assert.equal(validation.source, 'image'); assert.equal(validation.response.accepted, false);
    assert.equal(correction.normalization.source_file, 'board.png');
    assert.equal(correction.normalization.output_mime, 'image/png');
    assert.equal(correction.normalization.input_png_sha256, raw.image.sha256);
    assert.equal(correction.normalization.input_png_sha256, crypto.createHash('sha256').update(Buffer.from(visionRequests[0].image.split(',')[1], 'base64')).digest('hex'));
  });

  await check('canceling an in-flight local analysis preserves the current project', async () => {
    await upload('file-json', JSON.stringify({ circuit: original }), 'original.json', 'application/json');
    await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')");
    analysisDelay = 400; await runAnalysis();
    for (let i = 0; i < 100 && !visionRequests.length; i++) await delay(10);
    assert.equal(visionRequests.length, 1);
    assert.equal(await cdp.eval("document.getElementById('cancel-vision-request').hidden"), false);
    await cdp.eval("document.getElementById('cancel-vision-request').click()");
    await cdp.until("document.getElementById('edit-status').textContent.includes('已取消本机识别')");
    await delay(450);
    assert(await cdp.eval("!!document.querySelector('#svg-host g[data-part=R900]')"));
    assert.equal(await cdp.eval("document.getElementById('visionCorrectionHost').hidden"), true);
    assert.equal(validateRequests.length, 0);
  });

  await check('late analysis cannot replace a project loaded while the request was in flight', async () => {
    analysisDelay = 250; await runAnalysis();
    for (let i = 0; i < 100 && !visionRequests.length; i++) await delay(10);
    const replacement = { version: 2, title: 'Replacement', parts: [
      { id: 'R901', type: 'resistor', left: 120, top: 100, attrs: { value: '2kΩ' } },
    ], connections: [] };
    await upload('file-json', JSON.stringify({ circuit: replacement }), 'replacement.json', 'application/json');
    await cdp.until("!!document.querySelector('#svg-host g[data-part=R901]')");
    await delay(300);
    assert(await cdp.eval("!!document.querySelector('#svg-host g[data-part=R901]')"));
    assert.equal(await cdp.eval("document.getElementById('editReview').classList.contains('on')"), false);
    assert.equal(validateRequests.length, 0);
  });

  await check('analysis error preserves the original canvas and offers a distinct status', async () => {
    analysisStatus = 503;
    const before=browserResponses.length;
    await upload('file-json', JSON.stringify({ circuit: original }), 'original.json', 'application/json');
    await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')");
    await runAnalysis(); await cdp.until("document.getElementById('edit-status').textContent.includes('fixture analysis unavailable')");
    assert(await cdp.eval("!!document.querySelector('#svg-host g[data-part=R900]')"));
    assert.equal(validateRequests.length, 0);
    assert.equal(await cdp.eval("document.getElementById('visionCorrectionHost').hidden"), true);
    const rejection=browserResponses.slice(before).find(r=>r.status===503&&r.url.endsWith('/api/image/analyze'));assert(rejection,'Expected controlled 503 must have an exact request ID');expectedHttpErrorIds.add(rejection.id);
  });

  if (cdp.errors.length) failures.push({ name: 'uncaught browser exception', reason: cdp.errors.join('; ') });
  await delay(100);
  if(unexpectedBrowserLogErrors().length)failures.push({name:'unexpected native browser log error',reason:JSON.stringify(unexpectedBrowserLogErrors())});
  console.log(JSON.stringify({ browser: 'Microsoft Edge (CDP)', passed: results.filter(x => x.passed).length, total: results.length, failures }, null, 2));
  const evidencePath = process.env.VISION_UI_EVIDENCE_PATH
    ? path.resolve(process.env.VISION_UI_EVIDENCE_PATH)
    : path.resolve(__dirname, '../evidence/upgrade/open_source_integration/vision_pipeline/frontend.json');
  fs.mkdirSync(path.dirname(evidencePath), { recursive: true });
  const evidence = fs.existsSync(evidencePath) ? JSON.parse(fs.readFileSync(evidencePath, 'utf8')) : { runs: [] };
  evidence.runs.push({ timestamp: new Date().toISOString(), command: 'node tests/test_upstream_vision_ui.js', browser: 'Microsoft Edge (CDP)',
    scope: 'CircuitVision browser contract using controlled HTTP fixture; not real model inference or real electrical validation',
    passed: results.filter(x => x.passed).length, total: results.length, results, failures,browserLogEntries,browserResponses,resetNavigationDialogs,expectedHttpErrorIds:[...expectedHttpErrorIds],unexpectedBrowserLogErrors:unexpectedBrowserLogErrors() });
  fs.writeFileSync(evidencePath, JSON.stringify(evidence, null, 2) + '\n');
  }
  if (failures.length) process.exitCode = 1;
})().catch(err => { console.error(err); process.exitCode = 1; }).finally(async () => {
  const socketClosed = cdp && cdp.socket.readyState !== WebSocket.CLOSED
    ? new Promise(resolve => cdp.socket.addEventListener('close', resolve, { once: true })) : Promise.resolve();
  const browserClosed = browser && browser.exitCode === null && browser.signalCode === null
    ? new Promise(resolve => browser.once('close', resolve)) : Promise.resolve();
  const browserSocketClosed = browserCdp && browserCdp.socket.readyState !== WebSocket.CLOSED
    ? new Promise(resolve => browserCdp.socket.addEventListener('close', resolve, { once: true })) : Promise.resolve();
  const realServerClosed = realServerProcess && realServerProcess.exitCode === null && realServerProcess.signalCode === null
    ? new Promise(resolve => realServerProcess.once('close', resolve)) : Promise.resolve();
  if (cdp) cdp.socket.close();
  if (browserCdp && browserProfileVerified) {
    try { await browserCdp.send('Browser.close'); } catch (_) { /* browser may already be closing */ }
  }
  if (browserProfileVerified && browserPid && process.platform === 'win32') {
    await delay(2000);
    try { execFileSync('taskkill', ['/PID', String(browserPid), '/T', '/F'], { stdio: 'ignore', windowsHide: true }); }
    catch (_) { /* Browser.close may already have released the process tree */ }
    await delay(1500);
  } else if (browser && !browserProfileVerified) {
    // Only the spawned launcher PID is ours; leave any unverified browser process untouched.
    browser.kill();
  }
  const serverClosed = server.listening
    ? new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve())) : Promise.resolve();
  if (realServerProcess && realServerProcess.exitCode === null && realServerProcess.signalCode === null) realServerProcess.kill();
  let cleanupTimer;
  try {
    await Promise.race([Promise.all([socketClosed, browserSocketClosed, browserClosed, realServerClosed, serverClosed]),
      new Promise((_, reject) => { cleanupTimer = setTimeout(() => reject(new Error('Vision UI test resources did not close within 5 seconds')), 5000); })]);
  } catch (error) { console.error(error); process.exitCode = 1; }
  finally {
    clearTimeout(cleanupTimer);
    if (profile && browserProfileVerified && fs.existsSync(profile)) {
      try { fs.rmSync(profile, { recursive: true, force: true, maxRetries: 12, retryDelay: 250 }); }
      catch (error) { console.error('Temporary Edge profile retained after owned process shutdown: ' + error.message); }
    } else if (profile && fs.existsSync(profile)) console.error('Temporary Edge profile retained because ownership could not be verified.');
  }
});
