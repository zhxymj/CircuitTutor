/* Real browser + real Python Handler smoke test for the external import flow.
 * Run: node tests/test_external_import_e2e.js
 * No HTTP API substitutes or LLM calls are used. */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const { spawn } = require('node:child_process');

const project = path.resolve(__dirname, '..');
const toolchain = path.resolve(project, '..', '..', '智绘电路工具链');
const python = path.join(toolchain, 'vision-python', 'Scripts', 'python.exe');
const edgePaths = [process.env.EDGE_TEST_BINARY,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe'].filter(Boolean);
const edge = edgePaths.find(file => fs.existsSync(file));
const supportedPath = path.join(project, 'data/external_import/fixtures/circuitvision_supported.net');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const results = [];
const cleanupResults = [];
const observations = [{
  tool: 'Playwright MCP',
  url: 'http://127.0.0.1:51233',
  outcome: 'ERR_CONNECTION_TIMED_OUT',
  classification: '本机网络隔离环境观察；不作为页面功能失败判定。',
}];

let serverProcess = null;
let browserProcess = null;
let cdp = null;
let browserControl = null;
let profile = null;
let verifiedBrowserPid = null;
let verifiedBrowserCommandLine = null;
let tempConfig = null;
let port = null;
let serverLog = '';
let edgeLog = '';
let baseUrl = '';
let harnessFailure = null;
let evidenceFailure = null;
const networkRequests = [];

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.id = 0;
    this.waiting = new Map();
    this.errors = [];
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.method === 'Runtime.exceptionThrown')
        this.errors.push(message.params.exceptionDetails.text);
      if (message.method === 'Network.requestWillBeSent') {
        const req = message.params.request;
        if (req.url.indexOf('/api/import/external') >= 0 || req.url.indexOf('/api/validate') >= 0) {
          let payload = null;
          try { payload = JSON.parse(req.postData || 'null'); } catch (_) { /* keep raw request evidence */ }
          networkRequests.push({ id: message.params.requestId, url: req.url,
            method: req.method, payload: payload, status: null });
        }
      }
      if (message.method === 'Network.responseReceived') {
        const item = networkRequests.find(req => req.id === message.params.requestId);
        if (item) item.status = message.params.response.status;
      }
      if (!message.id) return;
      const pending = this.waiting.get(message.id);
      if (!pending) return;
      this.waiting.delete(message.id);
      message.error ? pending.reject(new Error(message.error.message)) : pending.resolve(message.result);
    });
  }
  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++this.id;
      const timer = setTimeout(() => {
        this.waiting.delete(id);
        reject(new Error('CDP timeout: ' + method));
      }, 10000);
      this.waiting.set(id, {
        resolve: value => { clearTimeout(timer); resolve(value); },
        reject: error => { clearTimeout(timer); reject(error); },
      });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
  async eval(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression, awaitPromise: true, returnByValue: true,
    });
    if (result.exceptionDetails)
      throw new Error(result.exceptionDetails.text + ': ' +
        (result.exceptionDetails.exception && result.exceptionDetails.exception.description));
    return result.result.value;
  }
  async until(expression, timeout = 12000) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      if (await this.eval(expression)) return;
      await delay(40);
    }
    throw new Error('Browser state timed out: ' + expression);
  }
}

async function reservePort() {
  const probe = net.createServer();
  await new Promise((resolve, reject) => probe.once('error', reject).listen(0, '127.0.0.1', resolve));
  const freePort = probe.address().port;
  await new Promise((resolve, reject) => probe.close(err => err ? reject(err) : resolve()));
  return freePort;
}

async function waitForHealth() {
  const deadline = Date.now() + 25000;
  let lastError = 'server did not start';
  while (Date.now() < deadline) {
    if (serverProcess.exitCode !== null)
      throw new Error('server.py exited before health check: ' + serverLog.slice(-1200));
    try {
      const response = await fetch(baseUrl + 'api/health');
      if (response.ok) {
        const health = await response.json();
        assert.equal(health.llm_key_configured, false, 'E2E 子进程不应加载在线模型密钥');
        return health;
      }
      lastError = 'health HTTP ' + response.status;
    } catch (error) { lastError = error.message; }
    await delay(150);
  }
  throw new Error('real Python Handler did not become healthy: ' + lastError + '\n' + serverLog.slice(-1200));
}

async function startServer() {
  if (!fs.existsSync(python)) throw new Error('project vision Python is unavailable: ' + python);
  if (!fs.existsSync(supportedPath)) throw new Error('supported SPICE fixture is missing');
  port = await reservePort();
  baseUrl = 'http://127.0.0.1:' + port + '/';
  tempConfig = fs.mkdtempSync(path.join(os.tmpdir(), 'zh-external-import-config-'));
  const env = Object.assign({}, process.env, {
    PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1',
    LSQC_HOST: '127.0.0.1', LSQC_PORT: String(port), DEEPSEEK_API_KEY: '',
    KICAD_CLI_PATH: path.join(toolchain, 'kicad/bin/kicad-cli.exe'),
    KICAD_SYMBOL_DIR: path.join(toolchain, 'kicad/share/kicad/symbols'),
    KICAD_FOOTPRINT_DIR: path.join(toolchain, 'kicad/share/kicad/footprints'),
    KICAD_CONFIG_HOME: path.join(tempConfig, 'config'),
    KICAD_DOCUMENTS_HOME: path.join(tempConfig, 'documents'),
    KICAD_CACHE_HOME: path.join(tempConfig, 'cache'),
    NGSPICE_PATH: path.join(toolchain, 'ngspice/Spice64/bin/ngspice_con.exe'),
    KICAD_CLI_TIMEOUT_S: '30',
  });
  serverProcess = spawn(python, ['-X', 'utf8', '-B', 'server.py'], {
    cwd: project, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  });
  serverProcess.stdout.on('data', data => { serverLog = (serverLog + data).slice(-6000); });
  serverProcess.stderr.on('data', data => { serverLog = (serverLog + data).slice(-6000); });
  const health = await waitForHealth();
  observations.push({ tool: 'Python Handler', pid: serverProcess.pid, url: baseUrl,
    version: health.version, llm_key_configured: health.llm_key_configured,
    command: 'vision-python\\Scripts\\python.exe -X utf8 -B server.py' });
}

async function startEdge() {
  if (!edge) throw new Error('Microsoft Edge executable is unavailable');
  profile = fs.mkdtempSync(path.join(os.tmpdir(), 'zh-external-import-edge-'));
  browserProcess = spawn(edge, [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run', '--no-default-browser-check', '--enable-automation',
    '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank',
  ], { windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] });
  browserProcess.stderr.on('data', data => { edgeLog = (edgeLog + data).slice(-2000); });
  const activePort = path.join(profile, 'DevToolsActivePort');
  const deadline = Date.now() + 12000;
  while (!fs.existsSync(activePort) && Date.now() < deadline) {
    if (browserProcess.exitCode !== null && browserProcess.exitCode !== 0)
      throw new Error('Edge launcher failed (code=' + browserProcess.exitCode + ', signal=' +
        browserProcess.signalCode + ', binary=' + edge + ', profile=' + profile + '): ' + edgeLog);
    await delay(50);
  }
  if (!fs.existsSync(activePort)) throw new Error('Edge did not expose CDP: ' + edgeLog);
  const cdpPort = fs.readFileSync(activePort, 'utf8').split(/\r?\n/)[0];
  const versionInfo = await (await fetch('http://127.0.0.1:' + cdpPort + '/json/version')).json();
  const controlSocket = new WebSocket(versionInfo.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    controlSocket.addEventListener('open', resolve, { once: true });
    controlSocket.addEventListener('error', reject, { once: true });
  });
  browserControl = new Cdp(controlSocket);
  const processInfo = await browserControl.send('SystemInfo.getProcessInfo');
  const browserInfo = (processInfo.processInfo || []).find(item => item.type === 'browser');
  if (!browserInfo || !Number.isInteger(browserInfo.id))
    throw new Error('CDP SystemInfo.getProcessInfo did not identify the browser process');
  verifiedBrowserPid = browserInfo.id;
  const commandLineInfo = await browserControl.send('Browser.getBrowserCommandLine');
  const args = commandLineInfo.arguments || [];
  const userDataArg = args.find(arg => arg.startsWith('--user-data-dir='));
  if (!userDataArg)
    throw new Error('CDP Browser.getBrowserCommandLine did not expose --user-data-dir');
  const reportedProfile = path.resolve(userDataArg.slice('--user-data-dir='.length));
  if (reportedProfile.toLowerCase() !== path.resolve(profile).toLowerCase())
    throw new Error('CDP browser profile does not match this run profile: ' + reportedProfile);
  verifiedBrowserCommandLine = args;
  const targets = await (await fetch('http://127.0.0.1:' + cdpPort + '/json/list')).json();
  const target = targets.find(item => item.type === 'page');
  if (!target) throw new Error('Edge CDP did not expose a page target');
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });
  cdp = new Cdp(socket);
  await cdp.send('Runtime.enable');
  await cdp.send('Page.enable');
  await cdp.send('Network.enable');
  await cdp.send('Page.navigate', { url: baseUrl });
  await cdp.until("document.readyState === 'complete' && document.getElementById('file-external') && document.getElementById('editAccept')");
  observations.push({ tool: 'Microsoft Edge CDP', launcher_pid: browserProcess.pid,
    browser_pid: verifiedBrowserPid, browser_pid_source: 'SystemInfo.getProcessInfo type=browser',
    profile_isolated: true, verified_user_data_dir: reportedProfile,
    command_line: verifiedBrowserCommandLine, binary: edge, mode: 'headless' });
}

async function upload(id, content, name, type) {
  await cdp.eval(`(() => {
    const transfer = new DataTransfer();
    transfer.items.add(new File([${JSON.stringify(content)}], ${JSON.stringify(name)}, {type:${JSON.stringify(type)}}));
    const input = document.getElementById(${JSON.stringify(id)});
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', {bubbles:true}));
  })()`);
}

async function loadOriginalCanvas() {
  const original = { version: 2, title: 'E2E 原图', parts: [
    { id: 'R900', type: 'resistor', left: 100, top: 100, attrs: { value: '1kΩ' } },
  ], connections: [] };
  await upload('file-json', JSON.stringify({ circuit: original }), 'original.json', 'application/json');
  await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')");
  return original;
}

async function recordCase(name, fn) {
  try {
    const detail = await fn();
    results.push({ name, passed: true, detail: detail || null });
    console.log('PASS ' + name);
  } catch (error) {
    results.push({ name, passed: false, reason: error.message });
    console.error('FAIL ' + name + ': ' + error.message);
  }
}

function latestExternalRequest() {
  return networkRequests.slice().reverse().find(req => req.url.includes('/api/import/external'));
}
function latestImportedValidation() {
  return networkRequests.slice().reverse().find(req => req.url.includes('/api/validate') &&
    req.payload && req.payload.source === 'import' && req.payload.circuit &&
    req.payload.circuit.provenance && req.payload.circuit.provenance.external_format === 'circuitvision_spice');
}

async function supportedImportApplyEditUndo() {
  await loadOriginalCanvas();
  const raw = fs.readFileSync(supportedPath, 'utf8');
  const requestStart = networkRequests.length;
  await cdp.eval("document.getElementById('external-source').value='circuitvision_spice'");
  await upload('file-external', raw, path.basename(supportedPath), 'text/plain');
  await cdp.until("document.getElementById('editReview').classList.contains('on')", 45000);
  const importReq = latestExternalRequest();
  assert(importReq && importReq.method === 'POST', '浏览器未向真实 Handler POST 外部结果');
  assert.equal(importReq.status, 200, '真实外部导入没有返回 HTTP 200');
  assert.equal(importReq.payload.source, 'circuitvision_spice');
  assert.equal(importReq.payload.content, raw, '浏览器发送的 UTF-8 原文发生变化');
  const validationReq = latestImportedValidation();
  assert(validationReq && validationReq.status === 200,
    '转换候选没有经过真实 /api/validate source=import');
  assert.equal(await cdp.eval("document.querySelectorAll('#svg-host g[data-part]').length"), 1,
    '审核期间当前画布被替换');
  assert((await cdp.eval("document.getElementById('editReviewBody').textContent")).includes('CircuitVision'));
  const gate = await cdp.eval("({label:document.getElementById('editAccept').textContent, body:document.getElementById('editReviewBody').textContent})");
  assert(['接受修改', '应用未验证草稿'].includes(gate.label), '真实门禁结果没有提供正确审核动作');

  await cdp.eval("document.getElementById('editAccept').click()");
  await cdp.until("!!document.querySelector('#svg-host g[data-part=R1]') && !!document.querySelector('#svg-host g[data-part=R2]')");
  assert.equal(await cdp.eval("document.getElementById('undo').disabled"), false, '应用候选后撤销没有启用');
  const sourceBadge = await cdp.eval("document.getElementById('srcBadge').textContent");

  // Exercise the real pointer selection and parameter editor on the applied part.
  const point = await cdp.eval(`(() => {
    const g=document.querySelector('#svg-host g[data-part="R1"]');
    const r=g.getBoundingClientRect();
    return {x:r.left+r.width/2,y:r.top+r.height/2};
  })()`);
  await cdp.eval(`(() => {
    const svg=document.querySelector('#svg-host svg');
    const g=document.querySelector('#svg-host g[data-part="R1"]');
    const x=${point.x}, y=${point.y};
    const init={bubbles:true,cancelable:true,pointerId:1,pointerType:'mouse',button:0,clientX:x,clientY:y};
    g.dispatchEvent(new PointerEvent('pointerdown',init));
    g.dispatchEvent(new PointerEvent('pointerup',init));
    return {rect:{x:g.getBoundingClientRect().x,y:g.getBoundingClientRect().y,w:g.getBoundingClientRect().width,h:g.getBoundingClientRect().height}};
  })()`);
  await cdp.until("document.querySelector('#props input') && document.getElementById('props').style.display !== 'none'");
  await cdp.eval("(() => { const row=Array.from(document.querySelectorAll('#props .row')).find(item=>item.querySelector('label') && item.querySelector('label').textContent==='参数值'); const input=row.querySelector('input'); input.value='2kΩ'; input.dispatchEvent(new Event('change',{bubbles:true})); })()");
  await cdp.until("document.getElementById('undo').disabled === false");
  await cdp.eval("window.__e2eDownloads=[]; URL.createObjectURL=blob=>{window.__e2eDownloads.push(blob.text());return 'blob:e2e'}; HTMLAnchorElement.prototype.click=function(){}; document.getElementById('save-json').click()");
  const saved = JSON.parse(await cdp.eval("Promise.all(window.__e2eDownloads).then(items=>items[0])"));
  assert.equal(saved.circuit.parts.find(part => part.id === 'R1').attrs.value, '2kΩ', '应用后的候选不可编辑');

  // First undo restores the manual edit, second undo restores the original project.
  await cdp.eval("document.getElementById('undo').click()");
  await cdp.eval("document.getElementById('undo').click()");
  await cdp.until("!!document.querySelector('#svg-host g[data-part=R900]')");
  assert.equal(await cdp.eval("document.querySelectorAll('#svg-host g[data-part]').length"), 1,
    '撤销没有恢复原始画布');
  return { import_http: importReq.status, validate_http: validationReq.status,
    validation_source: validationReq.payload.source, applied_badge: sourceBadge,
    review_action: gate.label, editable_value: '2kΩ', undo_restored_part: 'R900',
    observed_requests: networkRequests.slice(requestStart).map(req => ({
      path: new URL(req.url).pathname, method: req.method, status: req.status,
      source: req.payload && req.payload.source,
      external_format: req.payload && req.payload.circuit && req.payload.circuit.provenance && req.payload.circuit.provenance.external_format,
    })) };
}

async function rejectedUnknownMcuPreservesCanvas() {
  await loadOriginalCanvas();
  const before = await cdp.eval("document.querySelector('#svg-host').innerHTML");
  const content = 'V1 1 0 3.3\nR1 1 0 10k\nXU1 1 0 UNKNOWN_MCU';
  const requestStart = networkRequests.length;
  await cdp.eval("document.getElementById('external-source').value='circuitvision_spice'");
  await upload('file-external', content, 'unknown-mcu.net', 'text/plain');
  await cdp.until("document.getElementById('edit-status').textContent.includes('不支持器件 XU1')");
  const rejected = latestExternalRequest();
  assert(rejected && rejected.status === 422, '未知 MCU 没有由真实 Handler 整份拒绝');
  assert.equal(await cdp.eval("document.getElementById('editReview').classList.contains('on')"), false,
    '422 响应错误地打开了候选审核');
  assert.equal(await cdp.eval("document.querySelector('#svg-host').innerHTML"), before,
    '拒绝的未知 MCU 文件改变了原始画布');
  assert.equal(await cdp.eval("document.getElementById('load-external').disabled"), false,
    '422 后外部导入按钮仍被锁定');
  assert(!networkRequests.slice(requestStart).some(req => req.url.includes('/api/validate') &&
    req.payload && req.payload.circuit && req.payload.circuit.provenance &&
    req.payload.circuit.provenance.external_format === 'circuitvision_spice'),
  '被拒绝的文件被提交到电气验证门禁');
  return { import_http: rejected.status, rejection_message: await cdp.eval("document.getElementById('edit-status').textContent"),
    canvas_unchanged: true, candidate_review_opened: false };
}

async function writeEvidence() {
  const evidencePath = path.join(project, 'evidence/upgrade/open_source_integration/browser_e2e.json');
  fs.mkdirSync(path.dirname(evidencePath), { recursive: true });
  let previous = { runs: [] };
  if (fs.existsSync(evidencePath)) {
    try { previous = JSON.parse(fs.readFileSync(evidencePath, 'utf8')); }
    catch (_) { previous = { runs: [] }; }
  }
  const run = {
      timestamp: new Date().toISOString(),
      command: 'node tests/test_external_import_e2e.js',
      run_mode: process.env.E2E_RUN_MODE || 'unspecified',
      browser: 'Microsoft Edge CDP',
      server: 'real project server.py Handler in an owned child process',
      python_command: 'D:\\智绘电路\\智绘电路工具链\\vision-python\\Scripts\\python.exe -X utf8 -B server.py',
      port: port,
      llm_calls: 0,
      passed: results.filter(item => item.passed).length,
      total: results.length,
      results,
      harness_failure: harnessFailure,
      cleanup_results: cleanupResults,
      environment_observations: observations,
      uncaught_browser_errors: cdp ? cdp.errors : [],
      cleanup: { server_pid: serverProcess && serverProcess.pid,
        edge_launcher_pid: browserProcess && browserProcess.pid,
        owned_processes_stopped: cleanupResults
          .filter(item => item.name === 'server process' || item.name === 'Edge process tree')
          .every(item => item.passed),
        isolated_edge_profile: true },
  };
  const evidence = { schema_version: 1, environment_observations: observations.filter(item => item.tool === 'Playwright MCP'),
    runs: (Array.isArray(previous.runs) ? previous.runs : []).concat([run]) };
  fs.writeFileSync(evidencePath, JSON.stringify(evidence, null, 2) + '\n');
}

function waitForOwnedChildExit(child, timeoutMs) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return Promise.resolve(true);
  return new Promise(resolve => {
    let finished = false;
    const done = value => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      child.removeListener('close', onClose);
      resolve(value);
    };
    const onClose = () => done(true);
    const timer = setTimeout(() => done(child.exitCode !== null || child.signalCode !== null), timeoutMs);
    child.once('close', onClose);
  });
}

function runTaskkillOwnedTree(pid) {
  return new Promise((resolve, reject) => {
    const killer = spawn('taskkill', ['/PID', String(pid), '/T', '/F'], {
      windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let output = '';
    const timer = setTimeout(() => {
      killer.kill();
      reject(new Error('taskkill timed out for owned PID ' + pid));
    }, 8000);
    killer.stdout.on('data', data => { output = (output + data).slice(-1000); });
    killer.stderr.on('data', data => { output = (output + data).slice(-1000); });
    killer.once('error', error => { clearTimeout(timer); reject(error); });
    killer.once('close', code => { clearTimeout(timer); resolve({ code, output }); });
  });
}

async function stopOwnedProcess(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null)
    return { stopped: true, already_exited: true };
  const pid = child.pid;
  child.kill(process.platform === 'win32' ? undefined : 'SIGTERM');
  let exited = await waitForOwnedChildExit(child, 5000);
  if (!exited) {
    child.kill(process.platform === 'win32' ? undefined : 'SIGKILL');
    exited = await waitForOwnedChildExit(child, 5000);
  }
  return { stopped: exited, pid };
}

async function waitForPidExit(pid, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try { process.kill(pid, 0); }
    catch (error) {
      if (error.code === 'ESRCH') return true;
      if (error.code !== 'EPERM') throw error;
    }
    await delay(100);
  }
  try { process.kill(pid, 0); return false; }
  catch (error) {
    if (error.code === 'ESRCH') return true;
    if (error.code === 'EPERM') return false;
    throw error;
  }
}

async function stopVerifiedEdge(child) {
  if (!child || !Number.isInteger(verifiedBrowserPid) || !verifiedBrowserCommandLine)
    return { stopped: false, safe_to_remove_profile: false, browser_pid: verifiedBrowserPid,
      reason: 'CDP did not verify the actual browser PID and command line; profile retained' };
  assert(profile && path.basename(profile).startsWith('zh-external-import-edge-'),
    'owned Edge profile identity is missing');
  const userDataArg = verifiedBrowserCommandLine.find(arg => arg.startsWith('--user-data-dir='));
  const verifiedProfile = userDataArg && path.resolve(userDataArg.slice('--user-data-dir='.length));
  if (!verifiedProfile || verifiedProfile.toLowerCase() !== path.resolve(profile).toLowerCase())
    return { stopped: false, safe_to_remove_profile: false,
      reason: 'verified browser command line no longer matches this run profile; profile retained',
      browser_pid: verifiedBrowserPid, profile };

  let closeResult = 'not attempted';
  if (browserControl && browserControl.socket.readyState === WebSocket.OPEN) {
    try {
      await browserControl.send('Browser.close');
      closeResult = 'Browser.close acknowledged';
    } catch (error) { closeResult = 'Browser.close failed: ' + error.message; }
  }
  await delay(2000);
  let exited = await waitForPidExit(verifiedBrowserPid, 100);
  let taskkill = null;
  if (!exited) {
    taskkill = await runTaskkillOwnedTree(verifiedBrowserPid);
    exited = await waitForPidExit(verifiedBrowserPid, 5000);
  }
  const launcherExited = await waitForOwnedChildExit(child, 5000);
  return { stopped: exited, safe_to_remove_profile: exited,
    browser_pid: verifiedBrowserPid, launcher_pid: child.pid, close_result: closeResult,
    taskkill_exit_code: taskkill && taskkill.code,
    taskkill_output: taskkill && taskkill.output.trim(), launcher_exited: launcherExited,
    profile: verifiedProfile,
    reason: exited ? null : 'verified browser PID remained active; profile retained' };
}

async function removeOwnedTempDir(target, prefix) {
  if (!target || !fs.existsSync(target)) return { removed: true, already_absent: true };
  const root = path.resolve(os.tmpdir()) + path.sep;
  const resolved = path.resolve(target);
  assert(resolved.startsWith(root) && path.dirname(resolved) === path.resolve(os.tmpdir()) &&
    path.basename(resolved).startsWith(prefix),
    'refusing to remove a path outside this test\'s OS temp directory');
  let lastError;
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      fs.rmSync(resolved, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
      return { removed: true, attempts: attempt + 1, path: resolved };
    }
    catch (error) { lastError = error; await delay(150); }
  }
  throw lastError;
}

(async () => {
  try {
    await startServer();
    await startEdge();
    await recordCase('real import → validate → review → apply → edit → undo', supportedImportApplyEditUndo);
    // Restore the baseline through the same visible project file input. This
    // avoids a page reload/CDP navigation race and keeps the UI test focused.
    await recordCase('real 422 for unknown MCU preserves original canvas', rejectedUnknownMcuPreservesCanvas);
  } catch (error) {
    harnessFailure = { reason: error.message, browser_pid: verifiedBrowserPid,
      server_log_tail: serverLog.slice(-1500), edge_log_tail: edgeLog.slice(-800) };
    console.error('FAIL test harness startup: ' + error.message);
  } finally {
    if (cdp && cdp.socket.readyState !== WebSocket.CLOSED) cdp.socket.close();
    try {
      const outcome = await stopOwnedProcess(serverProcess);
      cleanupResults.push({ name: 'server process', passed: outcome.stopped, ...outcome });
    } catch (error) {
      cleanupResults.push({ name: 'server process', passed: false, reason: error.message });
    }
    let edgeOutcome = null;
    try {
      edgeOutcome = await stopVerifiedEdge(browserProcess);
      cleanupResults.push({ name: 'Edge process tree', passed: edgeOutcome.stopped, ...edgeOutcome });
    } catch (error) {
      edgeOutcome = { stopped: false, safe_to_remove_profile: false, reason: error.message };
      cleanupResults.push({ name: 'Edge process tree', passed: false, ...edgeOutcome,
        browser_pid: verifiedBrowserPid, launcher_pid: browserProcess && browserProcess.pid, profile });
    }
    if (browserControl && browserControl.socket.readyState !== WebSocket.CLOSED)
      browserControl.socket.close();
    if (edgeOutcome && edgeOutcome.safe_to_remove_profile) {
      try {
        const outcome = await removeOwnedTempDir(profile, 'zh-external-import-edge-');
        cleanupResults.push({ name: 'Edge profile directory', passed: outcome.removed, ...outcome });
      } catch (error) {
        cleanupResults.push({ name: 'Edge profile directory', passed: false, reason: error.message, path: profile });
      }
    } else {
      cleanupResults.push({ name: 'Edge profile directory', passed: false,
        reason: 'retained because actual browser PID/profile ownership was not safely cleared', path: profile });
    }
    try {
      const outcome = await removeOwnedTempDir(tempConfig, 'zh-external-import-config-');
      cleanupResults.push({ name: 'config temp directory', passed: outcome.removed, ...outcome });
    } catch (error) {
      cleanupResults.push({ name: 'config temp directory', passed: false, reason: error.message, path: tempConfig });
    }
    try { await writeEvidence(); } catch (error) {
      evidenceFailure = { reason: error.message };
      console.error(error);
    }
  }
  const businessPassed = results.filter(item => item.passed).length;
  const businessFailures = results.filter(item => !item.passed);
  const cleanupFailures = cleanupResults.filter(item => !item.passed);
  console.log(JSON.stringify({ business: { passed: businessPassed,
    total: results.length, failures: businessFailures },
    cleanup: { passed: cleanupResults.length - cleanupFailures.length,
      total: cleanupResults.length, failures: cleanupFailures },
    harness_failure: harnessFailure, evidence_failure: evidenceFailure }, null, 2));
  if (businessFailures.length || cleanupFailures.length || harnessFailure || evidenceFailure)
    process.exitCode = 1;
})().catch(error => { console.error(error); process.exitCode = 1; });
