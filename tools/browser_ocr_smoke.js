/* Actual shipped server, actual Edge, actual local Tesseract/WASM/eng model. No mocks. */
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const project = path.resolve(__dirname, '..');
const python = process.env.ZH_TEST_PYTHON || 'F:/智绘电路工具链/kicad/bin/python.exe';
const edge = [process.env.EDGE_TEST_BINARY, 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(p => p && fs.existsSync(p));
if (!edge || !fs.existsSync(python)) throw new Error('Real Edge/Python executable unavailable');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let server, browser, socket;
const visionResponses = [], browserErrors = [], browserLog = [];
const validationResponses = [];
const report = { date: new Date().toISOString(), backend: 'shipped server.py', browser: 'Microsoft Edge', mock: false };

async function unusedPort() {
  const holder = net.createServer();
  await new Promise(resolve => holder.listen(0, '127.0.0.1', resolve));
  const port = holder.address().port;
  await new Promise(resolve => holder.close(resolve));
  return port;
}
class Cdp {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map();
    ws.addEventListener('message', event => {
      const m = JSON.parse(event.data);
      if (m.method === 'Target.attachedToTarget') {
        const sid = m.params.sessionId;
        this.send('Network.enable', {}, sid).then(() => this.send('Runtime.runIfWaitingForDebugger', {}, sid))
          .catch(error => browserErrors.push('Worker CDP: ' + error.message));
      }
      if (m.method === 'Network.responseReceived' && m.params.response.url.includes('/static/vision/')) {
        const r = m.params.response;
        visionResponses.push({ url: new URL(r.url).pathname, status: r.status, mime: r.mimeType });
      }
      if (m.method === 'Network.responseReceived' && m.params.response.url.endsWith('/api/validate')) {
        validationResponses.push({ status: m.params.response.status, mime: m.params.response.mimeType });
      }
      if (m.method === 'Runtime.exceptionThrown') browserErrors.push(m.params.exceptionDetails.text);
      if (m.method === 'Log.entryAdded') browserLog.push({ level: m.params.entry.level, text: m.params.entry.text });
      if (!m.id || !this.pending.has(m.id)) return;
      const p = this.pending.get(m.id); this.pending.delete(m.id); clearTimeout(p.timer);
      m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result);
    });
  }
  send(method, params = {}, sessionId) {
    return new Promise((resolve, reject) => {
      const id = ++this.id;
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('CDP timeout: ' + method)); }, 60000);
      this.pending.set(id, { resolve, reject, timer }); this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }
  async eval(expression) {
    const r = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  }
}

(async () => {
  const port = await unusedPort(), base = 'http://127.0.0.1:' + port;
  server = spawn(python, ['server.py'], { cwd: project, windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'],
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', LSQC_PORT: String(port), LSQC_HOST: '127.0.0.1' } });
  let startup = '';
  server.stderr.on('data', d => { startup = (startup + d).slice(-2000); });
  let page;
  for (let n = 0; n < 100; n++) {
    try { page = await fetch(base + '/'); if (page.ok) break; } catch (_) {}
    await sleep(50);
  }
  if (!page?.ok) throw new Error('Actual server did not start: ' + startup);
  report.csp = page.headers.get('content-security-policy');
  await page.arrayBuffer();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'circuit-real-ocr-'));
  browser = spawn(edge, ['--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'],
    { windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] });
  let diagnostics = ''; browser.stderr.on('data', d => { diagnostics = (diagnostics + d).slice(-1000); });
  const activePort = path.join(profile, 'DevToolsActivePort');
  for (let n = 0; n < 150 && !fs.existsSync(activePort); n++) await sleep(50);
  if (!fs.existsSync(activePort)) throw new Error('Edge did not expose CDP: ' + diagnostics);
  const cdpPort = fs.readFileSync(activePort, 'utf8').split(/\r?\n/)[0];
  const targets = await (await fetch('http://127.0.0.1:' + cdpPort + '/json/list')).json();
  socket = new WebSocket(targets.find(t => t.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
  const cdp = new Cdp(socket);
  await cdp.send('Runtime.enable'); await cdp.send('Page.enable'); await cdp.send('Network.enable'); await cdp.send('Log.enable');
  await cdp.send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
  await cdp.send('Page.navigate', { url: base + '/' });
  for (let n = 0; n < 150; n++) {
    if (await cdp.eval("document.readyState === 'complete' && typeof CircuitImageRecognition !== 'undefined'")) break;
    await sleep(30);
  }
  const result = await cdp.eval(`(async () => {
    if (!window.Tesseract) await new Promise((resolve, reject) => {
      const script = document.createElement('script'); script.src = '/static/vision/tesseract.min.js';
      script.onload = resolve; script.onerror = () => reject(new Error('local OCR script blocked')); document.head.appendChild(script);
    });
    const canvas = document.createElement('canvas'); canvas.width=500; canvas.height=160;
    const ctx = canvas.getContext('2d'); ctx.fillStyle='#fff'; ctx.fillRect(0,0,500,160);
    ctx.fillStyle='#000'; ctx.font='48px Arial'; ctx.fillText('R1 4.7k',30,100);
    window.__ocrSmokeCanvas=canvas;
    const adapter = await CircuitImageRecognition.createTesseractAdapter(Tesseract, {
      langPath:location.origin+'/static/vision/lang',corePath:location.origin+'/static/vision/core',
      workerPath:location.origin+'/static/vision/worker.min.js',cacheMethod:'none',psm:7,
    });
    try { const raw=await adapter.recognize(canvas); return {raw,inputPng:canvas.toDataURL('image/png')}; }
    finally { await adapter.terminate(); }
  })()`);
  fs.writeFileSync(path.join(project, 'evaluation', 'image_browser_ocr_input.png'), Buffer.from(result.inputPng.split(',')[1], 'base64'));
  report.ocr = result.raw;
  await cdp.eval(`(async () => {
    const blob=await new Promise(resolve=>window.__ocrSmokeCanvas.toBlob(resolve,'image/png'));
    const dt=new DataTransfer();dt.items.add(new File([blob],'ascii_ocr.png',{type:'image/png'}));
    const input=document.getElementById('file-image');input.files=dt.files;input.dispatchEvent(new Event('change',{bubbles:true}));
  })()`);
  let reviewText = '';
  for (let n = 0; n < 300; n++) {
    const state = await cdp.eval("({on:document.getElementById('editReview').classList.contains('on'),text:document.getElementById('editReviewBody').textContent,status:document.getElementById('edit-status').textContent})");
    if (state.on) { reviewText=state.text; break; }
    if (state.status.includes('图片识别失败')) throw new Error(state.status);
    await sleep(50);
  }
  report.ui_review_text = reviewText;
  report.validation_responses = validationResponses;
  if (reviewText.includes('Failed to fetch')) report.validation_server_diagnostics = startup;
  report.resources = visionResponses;
  report.runtime_errors = browserErrors;
  report.csp_errors = browserLog.filter(e => /content security|content-security|refused|wasm.*blocked/i.test(e.text));
  assert.match(result.raw.text, /R1/); assert.match(result.raw.text, /4\.7k/i);
  assert.match(reviewText, /4\.7k/i);
  assert(!reviewText.includes('Failed to fetch'), 'Actual backend validation failed');
  assert(validationResponses.some(r => r.status === 200), 'Actual backend validation did not return HTTP 200');
  assert(report.csp.includes('wasm-unsafe-eval') && report.csp.includes("worker-src 'self' blob:"));
  assert(visionResponses.every(r => r.status === 200));
  for (const wanted of ['tesseract.min.js','worker.min.js','eng.traineddata.gz','.wasm'])
    assert(visionResponses.some(r => r.url.includes(wanted)), 'Missing actual resource: ' + wanted);
  assert.equal(report.csp_errors.length, 0); assert.equal(browserErrors.length, 0);
  report.passed = true;
})().catch(error => { report.passed=false; report.error=error.message; process.exitCode=1; }).finally(() => {
  report.resources ||= visionResponses; report.runtime_errors ||= browserErrors;
  fs.writeFileSync(path.join(project, 'evaluation', 'image_browser_ocr_results.json'), JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report,null,2));
  if (socket) socket.close(); if (browser) browser.kill(); if (server) server.kill();
});
