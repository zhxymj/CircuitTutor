/* Real Edge smoke test of the shipped page. The HTTP backend is a controlled
 * fixture so this verifies UI state transitions, not the recognition model or
 * official KiCad validation. Run: node tests/test_image_import_ui.js
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { spawn } = require('node:child_process');

const web = path.resolve(__dirname, '../web');
const edgePaths = [process.env.EDGE_TEST_BINARY,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe'].filter(Boolean);
const edge = edgePaths.find(p => fs.existsSync(p));
if (!edge) { console.log('SKIP: Edge executable unavailable'); process.exit(0); }

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let validationUnavailable = false;
let editAccepted = false;
const validationDelays = new Map();
const validateRequests = [];
const validationSources = [];
const externalRequests = [];
let externalDelay = 0, externalRejected = false, externalAccepted = false, externalUnavailable = false;
const externalCircuit = { version: 2, title: "External", parts: [{ id: "R42", type: "resistor", left: 100, top: 100, attrs: { value: "10kΩ" } }], connections: [], provenance: { origin: "external", source_id: "external.json" } };
const erc = { passed: true, fatal: 0, major: 0, minor: 0, warning: 0, issues: [] };
const server = http.createServer(async (req, res) => {
  if (req.url === '/' || req.url.startsWith('/static/')) {
    const basename = req.url === '/' ? 'index.html' : path.basename(req.url);
    const filename = path.join(web, basename);
    if (!fs.existsSync(filename)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type', basename.endsWith('.html') ? 'text/html; charset=utf-8' : 'text/javascript; charset=utf-8');
    res.end(fs.readFileSync(filename)); return;
  }
  res.setHeader('Content-Type', 'application/json');
  if (req.url === '/api/providers') { res.end(JSON.stringify({ providers: [], deepseek_env_key: false })); return; }
  if (req.url === '/api/import/external') {
    let raw = ''; for await (const data of req) raw += data;
    const body = JSON.parse(raw); externalRequests.push(body);
    await delay(externalDelay);
    if (externalUnavailable) { res.writeHead(503); res.end(JSON.stringify({ error: 'fixture import offline' })); return; }
    const circuit = JSON.parse(JSON.stringify(externalCircuit)); circuit.provenance.source_id = body.name;
    if (externalRejected) res.writeHead(422);
    res.end(JSON.stringify({ status: 'needs_review', circuit: externalRejected ? null : circuit,
      issues: [{ code: externalRejected ? 'MISSING_VALUE' : 'REVIEW_REQUIRED', message: externalRejected ? '缺少电压和 I2C 地址，请填写后重试' : '请核对原始连接与器件参数', path: 'parts[0]' }],
      readiness: { importable: !externalRejected, accepted: false, electrical_validated: false, needs_review: true },
      source: { format: body.source, sha256: 'fixture' },
      accepted: true, erc: { passed: true } })); return;
  }
  if (req.url === '/api/validate') {
    let raw = ''; for await (const data of req) raw += data;
    const body = JSON.parse(raw || '{}');
    const source = body.circuit && body.circuit.provenance && body.circuit.provenance.source_id;
    validateRequests.push(source);
    validationSources.push(body.source);
    await delay(validationDelays.get(source) || 0);
    if (validationUnavailable) { res.writeHead(503); res.end(JSON.stringify({ error: 'fixture offline' })); return; }
    const accepted = (body.source === 'ai' && editAccepted) || (body.source === 'import' && externalAccepted);
    res.end(JSON.stringify({ accepted, blocking_gates: accepted ? [] : ['kicad_erc'],
      gates: { erc: { status: 'pass', issues: [], evidence: { counts: erc } },
        kicad_erc: { status: 'not_run', reason: 'fixture: official tool unavailable' } } })); return;
  }
  if (req.url === '/api/edit') {
    let raw = ''; for await (const data of req) raw += data;
    const body = JSON.parse(raw || '{}');
    body.circuit.parts[0].attrs.value = '470Ω';
    res.end(JSON.stringify({ circuit: body.circuit, erc,
      changes: { parts_modified: ['R900'] },
      validation: { accepted: editAccepted, blocking_gates: editAccepted ? [] : ['kicad_erc'],
        gates: { kicad_erc: { status: editAccepted ? 'pass' : 'not_run', reason: 'controlled UI fixture' } } } })); return;
  }
  res.end(JSON.stringify(req.url === '/api/erc' ? erc : {}));
});

class Cdp {
  constructor(socket) {
    this.socket = socket; this.id = 0; this.waiting = new Map(); this.errors = [];
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.method === 'Runtime.exceptionThrown') this.errors.push(message.params.exceptionDetails.text);
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

const sample = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../data/image_samples/synthetic_printed_v1.json'), 'utf8')).samples[0].svg;
const original = { version: 2, title: 'Original', parts: [
  { id: 'R900', type: 'resistor', left: 100, top: 100, attrs: { value: '1kΩ' } }
], connections: [] };
let cdp, browser, profile;
const failures = [];
const results = [];
async function upload(id, content, name, type) {
  await cdp.eval(`(() => { const dt = new DataTransfer(); dt.items.add(new File([${JSON.stringify(content)}], ${JSON.stringify(name)}, {type:${JSON.stringify(type)}})); const input=document.getElementById(${JSON.stringify(id)}); input.files=dt.files; input.dispatchEvent(new Event('change', {bubbles:true})); })()`);
}
async function reset() {
  validationUnavailable = false; editAccepted = false; validationDelays.clear(); validateRequests.length = 0;
  validationSources.length = 0;
  externalDelay = 0; externalRejected = false; externalAccepted = false; externalUnavailable = false; externalRequests.length = 0;
  await cdp.send('Page.navigate', { url: baseUrl });
  await cdp.until("document.readyState === 'complete' && typeof CircuitImageRecognition !== 'undefined' && document.getElementById('editAccept')");
}
async function check(name, task) {
  try { await reset(); await task(); results.push({name, passed:true}); console.log('PASS ' + name); }
  catch (err) { failures.push({ name, reason: err.message }); results.push({name, passed:false, reason:err.message}); console.log('FAIL ' + name + ': ' + err.message); }
}
let baseUrl;
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  baseUrl = 'http://127.0.0.1:' + server.address().port + '/';
  profile = fs.mkdtempSync(path.join(os.tmpdir(), 'circuit-image-ui-'));
  browser = spawn(edge, ['--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], { windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] });
  let browserDiagnostics = '';
  browser.stderr.on('data', data => { browserDiagnostics = (browserDiagnostics + data).slice(-1500); });
  const activePort = path.join(profile, 'DevToolsActivePort');
  for (let attempt = 0; !fs.existsSync(activePort) && attempt < 150; attempt++) await delay(50);
  if (!fs.existsSync(activePort)) throw new Error('Edge did not expose DevTools: ' + browserDiagnostics);
  const port = fs.readFileSync(activePort, 'utf8').split(/\r?\n/)[0];
  const targets = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
  const target = targets.find(t => t.type === 'page');
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
  cdp = new Cdp(socket); await cdp.send('Runtime.enable'); await cdp.send('Page.enable');
  await check('empty canvas candidate is previewed without committing', async () => {
    await upload('file-image', sample, 'empty.svg', 'image/svg+xml');
    await cdp.until("document.getElementById('editReview').classList.contains('on')");
    assert.equal(await cdp.eval("document.querySelectorAll('#svg-host g[data-part]').length"), 0);
    assert((await cdp.eval("document.getElementById('editAccept').textContent")).includes('未验证草稿'));
    await cdp.eval("document.getElementById('editAccept').click()");
    assert((await cdp.eval("document.querySelectorAll('#svg-host g[data-part]').length")) > 0);
    assert.equal(await cdp.eval("document.getElementById('srcBadge').textContent"), '图片候选');
    const undoDisabled = await cdp.eval("document.getElementById('undo').disabled");
    const message = await cdp.eval("document.getElementById('edit-status').textContent");
    assert(!undoDisabled || !message.includes('可「撤回更改」'), '首份导入无撤销历史时仍宣称可以撤回更改');
  });
  await check('existing canvas replacement keeps history and honest revert action', async () => {
    await upload('file-json', JSON.stringify({ circuit: original }), 'original.json', 'application/json');
    await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')");
    await upload('file-image', sample, 'replace.svg', 'image/svg+xml');
    await cdp.until("document.getElementById('editReview').classList.contains('on')");
    assert(await cdp.eval("!!document.querySelector('#svg-host g[data-part=R900]')"));
    await cdp.eval("document.getElementById('editAccept').click()");
    assert.equal(await cdp.eval("document.getElementById('undo').disabled"), false);
    const revertDisabled = await cdp.eval("document.getElementById('revert-edit').disabled");
    const message = await cdp.eval("document.getElementById('edit-status').textContent");
    await cdp.eval("document.getElementById('undo').click()");
    assert(await cdp.eval("!!document.querySelector('#svg-host g[data-part=R900]')"), '撤销未恢复已有画布');
    assert(!revertDisabled || !message.includes('可「撤回更改」'), '图片替换后撤回更改按钮禁用但状态承诺可点击');
    assert.notEqual(await cdp.eval("document.getElementById('srcBadge').textContent"), '图片候选', '撤销恢复原图后仍错误标注为图片候选');
  });
  await check('image requests serialize or preserve the latest candidate', async () => {
    validationDelays.set('old.svg', 250); validationDelays.set('new.svg', 10);
    await upload('file-image', sample, 'old.svg', 'image/svg+xml');
    for (let i=0; !validateRequests.includes('old.svg') && i<100; i++) await delay(20);
    const locked = await cdp.eval("document.getElementById('load-image').disabled");
    await upload('file-image', sample, 'new.svg', 'image/svg+xml');
    await cdp.until("document.getElementById('editReview').classList.contains('on')");
    await delay(350);
    if (locked) {
      assert.deepEqual(validateRequests, ['old.svg'], '导入锁未阻止第二次识别请求');
      assert.equal(await cdp.eval("document.getElementById('load-image').disabled"), false, '导入锁未释放');
    } else assert((await cdp.eval("document.getElementById('meta').textContent")).includes('new.svg'), '旧请求后返回覆盖了新图片候选');
  });
  await check('current circuit changes while validation is pending reject stale candidate', async () => {
    validationDelays.set('stale.svg', 250);
    await upload('file-image', sample, 'stale.svg', 'image/svg+xml');
    for (let i=0; !validateRequests.includes('stale.svg') && i<100; i++) await delay(20);
    await upload('file-json', JSON.stringify({ circuit: original }), 'original.json', 'application/json');
    await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')"); await delay(350);
    assert.equal(await cdp.eval("document.getElementById('editReview').classList.contains('on')"), false);
    assert(await cdp.eval("!!document.querySelector('#svg-host g[data-part=R900]')"));
  });
  await check('validation unavailable remains a clearly labeled editable draft', async () => {
    validationUnavailable = true;
    await upload('file-image', sample, 'offline.svg', 'image/svg+xml');
    await cdp.until("document.getElementById('editReview').classList.contains('on')");
    assert((await cdp.eval("document.getElementById('editReviewBody').textContent")).includes('校验服务不可用'));
    assert.equal(await cdp.eval("document.getElementById('editAccept').textContent"), '应用未验证草稿');
    await cdp.eval("document.getElementById('editDiscard').click()");
    assert.equal(await cdp.eval("document.querySelectorAll('#svg-host g[data-part]').length"), 0);
  });
  await check('AI draft accepts as draft and the dedicated revert restores the old value', async () => {
    await upload('file-json', JSON.stringify({ circuit: original }), 'original.json', 'application/json');
    await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')");
    await cdp.eval("document.getElementById('edit-q').value='R900 改成 470Ω'; document.getElementById('edit-btn').click()");
    await cdp.until("document.getElementById('editReview').classList.contains('on')");
    assert.equal(await cdp.eval("document.getElementById('editAccept').textContent"), '应用未验证草稿');
    await cdp.eval("document.getElementById('editAccept').click()");
    assert.equal(await cdp.eval("document.getElementById('srcBadge').textContent"), '未验证草稿');
    assert.equal(await cdp.eval("document.getElementById('revert-edit').disabled"), false);
    await cdp.eval("document.getElementById('revert-edit').click()");
    assert((await cdp.eval("document.getElementById('svg-host').textContent")).includes('1kΩ'), '专用撤回按钮未恢复原器件参数');
  });
  await check('officially accepted AI candidate uses the completed badge', async () => {
    validationUnavailable = false;
    editAccepted = true;
    await upload('file-json', JSON.stringify({ circuit: original }), 'original.json', 'application/json');
    await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')");
    await cdp.eval("document.getElementById('edit-q').value='R900 改成 470Ω'; document.getElementById('edit-btn').click()");
    await cdp.until("document.getElementById('editReview').classList.contains('on')");
    assert.equal(await cdp.eval("document.getElementById('editAccept').textContent"), '接受修改');
    await cdp.eval("document.getElementById('editAccept').click()");
    assert.equal(await cdp.eval("document.getElementById('srcBadge').textContent"), 'AI 完成');
  });
  await check('applying a candidate reruns the gate and keeps a current report', async () => {
    editAccepted = true;
    await upload('file-json', JSON.stringify({ circuit: original }), 'original.json', 'application/json');
    await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')");
    await cdp.eval("document.getElementById('edit-q').value='R900 改成 470Ω'; document.getElementById('edit-btn').click()");
    await cdp.until("document.getElementById('editReview').classList.contains('on')");
    const before = validateRequests.length;
    await cdp.eval("document.getElementById('editAccept').click()");
    await cdp.until("!!window.__lastValidation", 4000);
    assert(validateRequests.length > before, '接受后未重新请求统一门禁');
    assert.equal(await cdp.eval("window.__lastValidation.accepted"), true);
    assert.equal(await cdp.eval("document.getElementById('srcBadge').textContent"), 'AI 完成');
  });
  async function importExternal(source = 'hcd', name = 'external.json') {
    assert(await cdp.eval("!!document.getElementById('file-external') && !!document.getElementById('external-source')"), '外部文件导入入口缺失');
    await cdp.eval(`document.getElementById('external-source').value=${JSON.stringify(source)}`);
    await upload('file-external', 'raw fixture input\nwith original whitespace  ', name, 'application/json');
  }
  for (const source of ['circuitvision_spice', 'hcd', 'tscircuit']) {
    await check(source + ' imports as a reviewed draft with original evidence', async () => {
      await importExternal(source);
      await cdp.until("document.getElementById('editReview').classList.contains('on')");
      assert.deepEqual(externalRequests, [{ source, content: 'raw fixture input\nwith original whitespace  ', name: 'external.json' }]);
      assert(validateRequests.includes('external.json'), '候选未请求实际校验');
      assert(validationSources.includes('import'), '外部候选未使用统一导入校验来源');
      assert.equal(await cdp.eval("document.querySelectorAll('#svg-host g[data-part]').length"), 0);
      assert((await cdp.eval("document.getElementById('editReviewBody').textContent")).includes('请核对原始连接'));
      assert.equal(await cdp.eval("document.getElementById('editAccept').textContent"), '应用未验证草稿', '导入结果的 accepted:true 被当成实际校验通过');
      await cdp.eval("window.__evidence=[]; URL.createObjectURL = blob => { window.__evidence.push(blob.text()); return 'blob:fixture'; }; HTMLAnchorElement.prototype.click=function(){}; Array.from(document.querySelectorAll('#editReviewBody button')).find(b=>b.textContent.includes('下载原始')).click()");
      assert.equal(await cdp.eval("Promise.all(window.__evidence).then(a=>a[0])"), 'raw fixture input\nwith original whitespace  ');
      await cdp.eval("document.getElementById('editDiscard').click()");
      assert.equal(await cdp.eval("document.querySelectorAll('#svg-host g[data-part]').length"), 0);
    });
  }
  await check('external draft applies editable content without inventing electrical parameters and can undo', async () => {
    await upload('file-json', JSON.stringify({ circuit: original }), 'original.json', 'application/json');
    await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')");
    await importExternal();
    await cdp.until("document.getElementById('editReview').classList.contains('on')");
    await cdp.eval("document.getElementById('editAccept').click()");
    assert(await cdp.eval("!!document.querySelector('#svg-host g[data-part=R42]')"));
    assert.equal(await cdp.eval("document.getElementById('undo').disabled"), false);
    await cdp.eval("window.__saved=[]; URL.createObjectURL=blob=>{window.__saved.push(blob.text());return 'blob:fixture'}; HTMLAnchorElement.prototype.click=function(){}; document.getElementById('save-json').click()");
    const saved = JSON.parse(await cdp.eval("Promise.all(window.__saved).then(a=>a[0])"));
    assert.deepEqual(saved.circuit.parts[0].attrs, { value: '10kΩ' });
    assert.equal(saved.circuit.i2c_address, undefined); assert.equal(saved.circuit.power_voltage, undefined);
    assert.equal(await cdp.eval("document.getElementById('srcBadge').textContent"), '未验证草稿');
    await cdp.eval("document.getElementById('undo').click()");
    assert(await cdp.eval("!!document.querySelector('#svg-host g[data-part=R900]')"));
  });
  await check('external candidate is normally accepted only after actual validation', async () => {
    externalAccepted = true; await importExternal();
    await cdp.until("document.getElementById('editReview').classList.contains('on')");
    assert.equal(await cdp.eval("document.getElementById('editAccept').textContent"), '接受修改');
    await cdp.eval("document.getElementById('editAccept').click()");
    assert.equal(await cdp.eval("document.getElementById('srcBadge').textContent"), '校验通过');
  });
  await check('422 external rejection shows missing fields and preserves the current canvas', async () => {
    externalRejected = true;
    await upload('file-json', JSON.stringify({ circuit: original }), 'original.json', 'application/json');
    await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')");
    await importExternal();
    await cdp.until("document.getElementById('edit-status').textContent.includes('缺少电压和 I2C 地址')");
    assert(await cdp.eval("!!document.querySelector('#svg-host g[data-part=R900]')"));
    assert.equal(await cdp.eval("document.getElementById('editReview').classList.contains('on')"), false);
    assert.equal(await cdp.eval("document.getElementById('load-external').disabled"), false);
  });
  await check('external import failure preserves current canvas and releases busy state', async () => {
    externalUnavailable = true;
    await upload('file-json', JSON.stringify({ circuit: original }), 'original.json', 'application/json');
    await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')"); await importExternal();
    await cdp.until("document.getElementById('edit-status').textContent.includes('fixture import offline')");
    assert(await cdp.eval("!!document.querySelector('#svg-host g[data-part=R900]')"));
    assert.equal(await cdp.eval("document.getElementById('load-external').disabled"), false);
    assert.equal(await cdp.eval("document.getElementById('editReview').classList.contains('on')"), false);
  });
  await check('external validation unavailable offers explicit draft with no successful ERC claim', async () => {
    validationUnavailable = true; await importExternal();
    await cdp.until("document.getElementById('editReview').classList.contains('on')");
    assert.equal(await cdp.eval("document.getElementById('editAccept').textContent"), '应用未验证草稿');
    const text = await cdp.eval("document.getElementById('editReviewBody').textContent");
    assert(text.includes('校验服务不可用')); assert(!text.includes('未发现问题'));
  });
  await check('external in-flight request serializes and rejects a stale response', async () => {
    externalDelay = 250; await importExternal('tscircuit', 'stale.json');
    for (let i=0; !externalRequests.length && i<100; i++) await delay(10);
    assert.equal(await cdp.eval("document.getElementById('load-external').disabled"), true);
    await importExternal('hcd', 'ignored.json'); assert.equal(externalRequests.length, 1);
    await upload('file-json', JSON.stringify({ circuit: original }), 'original.json', 'application/json');
    await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')"); await delay(350);
    assert.equal(await cdp.eval("document.getElementById('editReview').classList.contains('on')"), false);
    assert.equal(await cdp.eval("document.getElementById('load-external').disabled"), false);
    assert(await cdp.eval("!!document.querySelector('#svg-host g[data-part=R900]')"));
  });
  if (cdp.errors.length) failures.push({ name: 'uncaught browser exception', reason: cdp.errors.join('; ') });
  console.log(JSON.stringify({ browser: 'Microsoft Edge (CDP)', passed: results.filter(x=>x.passed).length, total: results.length, failures }, null, 2));
  const evidencePath = path.resolve(__dirname, '../evidence/upgrade/open_source_integration/frontend.json');
  fs.mkdirSync(path.dirname(evidencePath), { recursive: true });
  const evidence = fs.existsSync(evidencePath) ? JSON.parse(fs.readFileSync(evidencePath, 'utf8')) : { runs: [] };
  evidence.runs.push({ timestamp: new Date().toISOString(), command: 'node tests/test_image_import_ui.js', browser: 'Microsoft Edge (CDP)', scope: 'Browser interaction contract tests using controlled HTTP fixtures; not real Python backend conversion or actual electrical validation', passed: results.filter(x=>x.passed).length, total: results.length, results, failures });
  fs.writeFileSync(evidencePath, JSON.stringify(evidence, null, 2) + '\n');
  if (failures.length) process.exitCode = 1;
})().catch(err => { console.error(err); process.exitCode = 1; }).finally(async () => {
  const socketClosed = cdp && cdp.socket.readyState !== WebSocket.CLOSED
    ? new Promise(resolve => cdp.socket.addEventListener('close', resolve, { once: true })) : Promise.resolve();
  const browserClosed = browser && browser.exitCode === null && browser.signalCode === null
    ? new Promise(resolve => browser.once('close', resolve)) : Promise.resolve();
  if (cdp) cdp.socket.close();
  if (browser) browser.kill();
  const serverClosed = new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  let cleanupTimer;
  try {
    await Promise.race([
      Promise.all([socketClosed, browserClosed, serverClosed]),
      new Promise((_, reject) => { cleanupTimer = setTimeout(() => reject(new Error('Image UI test resources did not close within 5 seconds')), 5000); }),
    ]);
  } catch (error) { console.error(error); process.exitCode = 1; }
  finally { clearTimeout(cleanupTimer); }
  // Leave the temporary browser profile in the OS temp folder; no user files are removed.
});
