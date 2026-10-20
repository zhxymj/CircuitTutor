/* Real Edge + real Python HTTP + real Node runtime. No API stubs. */
'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const net = require('node:net');
const {createHash} = require('node:crypto');
const fullWorkflow = process.env.FULL_WORKFLOW === '1';
const root = path.resolve(__dirname, '..');
const toolchain = path.resolve(root, '../../智绘电路工具链');
const python = path.join(toolchain,'vision-python/Scripts/python.exe');
const edge = ['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(fs.existsSync);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const evidenceBase = process.env.WORKBENCH_REGRESSION_DIR ? path.resolve(process.env.WORKBENCH_REGRESSION_DIR) : path.join(root, fullWorkflow ? 'evidence/upgrade/full_workflow' : 'evidence/upgrade/native_integration');
const evidenceDir = fullWorkflow ? path.join(evidenceBase, 'ui') : evidenceBase;
const profile = fs.mkdtempSync(path.join(os.tmpdir(),'tscircuit-edge-'));
const results = [], errors = [], requests = [], consoleMessages = [], downloads = [];
const externalBrowserRequests = [], generationCompletions = [];
const browserLogEntries = [];
const expectedHttpRejectionIds = new Set();
function unexpectedBrowserErrors(){
  return browserLogEntries.filter(entry=>entry.level==='error' &&
    !(entry.source==='network' && expectedHttpRejectionIds.has(entry.networkRequestId) &&
      requests.some(request=>request.id===entry.networkRequestId && request.status===422 && request.url.endsWith('/api/design/tscircuit'))));
}
const caseSources = {
  offline_chinese_template_generation_and_exports: {input_source:'Synthetic Chinese LED request; data/circuit_templates.json; scripted export/import/history actions',license:'Project LICENSE (MIT)'},
  clarification_preserves_existing_unsaved_project: {input_source:'Synthetic incomplete MCU/I2C Chinese request; manually edited local R900 project',license:'Project LICENSE (MIT); synthetic user input'},
  rejected_mcu_request_preserves_existing_project: {input_source:'Synthetic STM32F103 request outside component catalog; manually edited local R900 project',license:'Project LICENSE (MIT); synthetic user input'},
  real_rc_preview_validation_apply_edit_undo: {input_source:'tools/upstream_examples/tscircuit_rc.tsx; real installed @tscircuit/eval/core and circuit-to-svg; scripted resistor edit',license:'Example/project MIT; local runtime MIT and ISC, per third_party/tscircuit-runtime-attribution.json'},
  unknown_ic_and_syntax_error_preserve_old_canvas: {input_source:'Inline synthetic unsupported-chip TSX and malformed TSX in tests/test_tscircuit_ui.js',license:'Project LICENSE (MIT); runtime attribution as above'},
  cancel_preserves_old_canvas: {input_source:'tools/upstream_examples/tscircuit_rc.tsx; immediate local cancel action',license:'Project LICENSE (MIT); runtime attribution as above'},
  native_mouse_selection_highlight_edit_and_undo: {input_source:'Real CDP mouse input on R1 in the offline LED template; native SVG getBBox and DOM rect attributes',license:'Project LICENSE (MIT)'},
};
const requestData = new Map();
const priorEvidencePath = path.join(evidenceBase, fullWorkflow ? 'ui.json' : 'tscircuit-ui.json');
const priorEvidence = fs.existsSync(priorEvidencePath) ? JSON.parse(fs.readFileSync(priorEvidencePath,'utf8')) : null;
let server, browser, cdp, browserCdp, ownedBrowserPid, health, serverOutput = '', edgeOutput = '';

class CDP {
  constructor(socket) {
    this.socket=socket; this.id=0; this.pending=new Map();
    socket.addEventListener('message', event => {
      const message=JSON.parse(event.data);
      if (process.env.TSCIRCUIT_CDP_DEBUG) console.log('CDP response', String(event.data).slice(0,250));
      if(message.method==='Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
      if(message.method==='Runtime.consoleAPICalled') consoleMessages.push(message.params);
      if(message.method==='Log.entryAdded') browserLogEntries.push(message.params.entry);
      if(message.method==='Network.requestWillBeSent') {
        requestData.set(message.params.requestId, message.params.request.postData || '');
        const url=message.params.request.url;
        if(/^https?:/.test(url) && !/^http:\/\/(127\.0\.0\.1|localhost)(:|\/)/.test(url)) externalBrowserRequests.push(url);
      }
      if(message.method==='Network.eventSourceMessageReceived' && message.params.eventName==='done') generationCompletions.push(JSON.parse(message.params.data));
      if(message.method==='Network.responseReceived' && (fullWorkflow ? /\/api\// : /\/api\/(design\/tscircuit|validate)/).test(message.params.response.url))
        requests.push({id:message.params.requestId,url:message.params.response.url,status:message.params.response.status,postData:requestData.get(message.params.requestId)||''});
      const pending=this.pending.get(message.id);
      if(pending){this.pending.delete(message.id); clearTimeout(pending.timer); message.error?pending.reject(new Error(message.error.message)):pending.resolve(message.result);}
    });
  }
  send(method,params={}) { return new Promise((resolve,reject)=>{
    const id=++this.id;
    const timer=setTimeout(()=>{this.pending.delete(id);reject(new Error('CDP timeout '+method));},15000);
    this.pending.set(id,{resolve,reject,timer});
    if (process.env.TSCIRCUIT_CDP_DEBUG) console.log('CDP request',JSON.stringify({id,method,params}));
    this.socket.send(JSON.stringify({id,method,params}));
  });}
  async eval(expression) {
    const result=await this.send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
    if(result.exceptionDetails) throw new Error(result.exceptionDetails.text+' '+JSON.stringify(result.exceptionDetails.exception));
    return result.result.value;
  }
  async until(expression,ms=120000) {
    const end=Date.now()+ms;
    while(Date.now()<end){if(await this.eval(expression))return; await delay(100);}
    throw new Error('Wait failed '+expression+' '+await this.eval('document.getElementById("tscircuit-status").textContent'));
  }
}
async function connect(url){const socket=new WebSocket(url); await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});return new CDP(socket);}
async function check(name, fn){
  const provenance={...caseSources[name],data_split:'workflow_smoke_not_holdout'};
  assert(provenance.input_source && provenance.license,'Every case requires explicit source and license');
  try {const detail=await fn();results.push({name,passed:true,detail:detail||null,...provenance});console.log('PASS '+name);}
  catch(error){results.push({name,passed:false,error:error.stack,...provenance});console.error('FAIL '+name+' '+error.message);if(!fullWorkflow)throw error;process.exitCode=1;}
}
async function loadProject(circuit){
  await cdp.eval(`(()=>{const dt=new DataTransfer();dt.items.add(new File([${JSON.stringify(JSON.stringify({circuit}))}],'workflow.json',{type:'application/json'}));const input=document.getElementById('file-json');input.files=dt.files;input.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  await cdp.until('document.getElementById("meta").textContent.includes("已载入工程")');
}
async function downloadProject(){
  return cdp.eval(`(async()=>{let saved;const original=URL.createObjectURL,click=HTMLAnchorElement.prototype.click;URL.createObjectURL=function(blob){saved=blob;return original.call(this,blob)};HTMLAnchorElement.prototype.click=function(){};try{document.getElementById('save-json').click();return JSON.parse(await saved.text())}finally{URL.createObjectURL=original;HTMLAnchorElement.prototype.click=click}})()`);
}
async function editOriginalResistance(value){
  await cdp.eval(`(() => {const g=document.querySelector('#svg-host g[data-part="R900"]');const r=g.getBoundingClientRect();const init={bubbles:true,cancelable:true,pointerId:1,pointerType:'mouse',button:0,clientX:r.left+r.width/2,clientY:r.top+r.height/2};g.dispatchEvent(new PointerEvent('pointerdown',init));g.dispatchEvent(new PointerEvent('pointerup',init));})()`);
  await cdp.until('document.querySelector("#props input") && document.getElementById("props").style.display !== "none"');
  await cdp.eval(`(() => {const row=Array.from(document.querySelectorAll('#props .row')).find(item=>item.querySelector('label')?.textContent==='参数值');const input=row.querySelector('input');input.value=${JSON.stringify(value)};input.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  assert.match(await cdp.eval('document.getElementById("dirty").textContent'),/已手动修改/);
}
async function runFullWorkflow(){
  const baseline=(await downloadProject()).circuit;
  fs.mkdirSync(evidenceDir,{recursive:true});
  await browserCdp.send('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:evidenceDir,eventsEnabled:true});
  await check('offline_chinese_template_generation_and_exports',async()=>{
    // Hold the old imported project's real validation timer, then release it
    // after generation. HTTP remains real; only timer ordering is controlled.
    await cdp.eval(`(()=>{const schedule=window.setTimeout,cancel=window.clearTimeout,pending=new Map();window.setTimeout=function(callback,ms,...args){if(ms===1500&&String(callback).includes('/api/validate')){const id=schedule(()=>{},60000);pending.set(id,{callback,args,cancelled:false});return id;}return schedule(callback,ms,...args)};window.clearTimeout=function(id){if(pending.has(id))pending.get(id).cancelled=true;return cancel(id)};window.__releasePriorValidation=async()=>{window.setTimeout=schedule;window.clearTimeout=cancel;let released=0;for(const [id,task] of pending){cancel(id);if(!task.cancelled){released++;await task.callback(...task.args)}}return {scheduled:pending.size,released}}})()`);
    await loadProject(baseline);
    await cdp.eval(`document.getElementById('q').value='5V LED 限流指示灯，串联330欧电阻';document.getElementById('go').click()`);
    await cdp.until('!document.getElementById("go").disabled && !!document.querySelector("#svg-host g[data-part]") && document.getElementById("meta").textContent.includes("模板")');
    const timerOrder=await cdp.eval('window.__releasePriorValidation()');
    assert.equal(timerOrder.scheduled,1,'Old project validation must be queued before generation');
    assert.equal(requests.filter(request=>request.url.endsWith('/api/validate')&&request.postData&&JSON.parse(request.postData).source==='import'&&JSON.parse(request.postData).circuit.parts.some(part=>part.type==='led')).length,0,'An old queued import validation must not validate the replacement template');
    assert.match(await cdp.eval('document.getElementById("srcBadge").textContent'),/模板/);
    const project=await downloadProject();
    const templateBadge=await cdp.eval('document.getElementById("srcBadge").textContent');
    assert(project.circuit.parts.some(p=>p.type==='led'));
    for(const [id,suffix] of [['save-json','.json'],['export-svg','.svg'],['export-bom','.csv'],['export-spice','.cir'],['export-kicad','.zip']]){
      const before=new Set(fs.readdirSync(evidenceDir));
      await cdp.eval(`document.getElementById(${JSON.stringify(id)}).click()`);
      let name;
      for(let i=0;i<1200;i++){name=fs.readdirSync(evidenceDir).find(n=>!before.has(n)&&n.endsWith(suffix));if(name)break;await delay(100);}
      assert(name,id+' download missing: '+await cdp.eval('document.getElementById("meta").textContent'));
      const data=fs.readFileSync(path.join(evidenceDir,name));
      assert(data.length>20);downloads.push({button:id,file:'ui/'+name,bytes:data.length,sha256:createHash('sha256').update(data).digest('hex')});
      if(suffix==='.zip')await cdp.eval('document.getElementById("kicadModalOk").click()');
    }
    await loadProject(project.circuit);
    assert.deepEqual((await downloadProject()).circuit,project.circuit);
    await cdp.eval('document.getElementById("btn-save-history").click()');
    await cdp.until('document.getElementById("meta").textContent.includes("已存档")');
    await loadProject(baseline);
    await cdp.eval('document.getElementById("btn-history").click()');
    await cdp.until('!!document.querySelector("#historyBody .hist-item button")');
    await cdp.eval('document.querySelector("#historyBody .hist-item button").click()');
    await cdp.until('document.getElementById("meta").textContent.includes("已载入历史")');
    assert.deepEqual((await downloadProject()).circuit,project.circuit);
    return {template_badge:templateBadge,parts:project.circuit.parts.length,downloads:downloads.length,json_roundtrip:true,history_roundtrip:true,prior_validation_timer:timerOrder};
  });
  await check('native_mouse_selection_highlight_edit_and_undo',async()=>{
    const original=(await downloadProject()).circuit;
    const priorLogs=browserLogEntries.length;
    await cdp.eval(`document.querySelector('#svg-host g[data-part="R1"]').scrollIntoView({block:'center'})`);
    const point=await cdp.eval(`(()=>{const b=document.querySelector('#svg-host g[data-part="R1"]').getBoundingClientRect();return {x:b.x+b.width/2,y:b.y+b.height/2}})()`);
    await cdp.send('Input.dispatchMouseEvent',{type:'mousePressed',...point,button:'left',clickCount:1});
    await cdp.send('Input.dispatchMouseEvent',{type:'mouseReleased',...point,button:'left',clickCount:1});
    await cdp.until('!!document.querySelector("#svg-host g[data-part=R1] rect.interact-sel")');
    const geometry=await cdp.eval(`(()=>{const g=document.querySelector('#svg-host g[data-part="R1"]');const b=g.getBBox(),r=g.querySelector('rect.interact-sel');return {bbox:{x:b.x,y:b.y,width:b.width,height:b.height},highlight:{x:Number(r.getAttribute('x')),y:Number(r.getAttribute('y')),width:Number(r.getAttribute('width')),height:Number(r.getAttribute('height'))}}})()`);
    assert(Number.isFinite(geometry.highlight.width)&&geometry.highlight.width>0,'Native getBBox width must produce a finite positive highlight width');
    assert(Number.isFinite(geometry.highlight.height)&&geometry.highlight.height>0,'Native getBBox height must produce a finite positive highlight height');
    assert(Number.isFinite(geometry.bbox.width)&&geometry.bbox.width>0);
    assert(Number.isFinite(geometry.bbox.height)&&geometry.bbox.height>0);
    await cdp.eval(`(()=>{const input=Array.from(document.querySelectorAll('#props .row')).find(row=>row.querySelector('label')?.textContent==='参数值').querySelector('input');input.value='470Ω';input.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    assert.equal((await downloadProject()).circuit.parts.find(p=>p.id==='R1').attrs.value,'470Ω');
    await cdp.eval('document.getElementById("undo").click()');
    assert.deepEqual((await downloadProject()).circuit,original);
    await delay(100);
    assert.deepEqual(browserLogEntries.slice(priorLogs).filter(entry=>entry.level==='error'),[],'Native mouse selection must not produce browser errors');
    return {input:'CDP Input.dispatchMouseEvent (real browser mouse)',geometry,parameter_edit:true,undo_restores_project:true,browser_log_errors:0};
  });
  await loadProject(baseline);
  await check('clarification_preserves_existing_unsaved_project',async()=>{
    await editOriginalResistance('2.2kΩ');
    const edited=(await downloadProject()).circuit;
    await cdp.eval(`document.getElementById('q').value='3.3V MCU 连接 I2C 传感器';document.getElementById('go').click()`);
    await cdp.until('document.getElementById("clarify").style.display === "block"');
    const question=await cdp.eval('document.getElementById("clarify").textContent');
    assert.match(question,/型号|器件/);
    assert(await cdp.eval('!!document.querySelector("#svg-host g[data-part=R900]")'),'Clarification must preserve the existing project until a circuit is produced');
    assert.deepEqual((await downloadProject()).circuit,edited);
    await cdp.eval('document.getElementById("cq-skip").click()');
    await cdp.until('!document.getElementById("go").disabled && document.getElementById("meta").textContent.includes("拒绝")');
    assert.equal(await cdp.eval('document.getElementById("clarify").style.display'), 'block', 'SSE clarification rejection must redisplay the missing fields');
    assert.deepEqual((await downloadProject()).circuit,edited);
    assert.match(await cdp.eval('document.getElementById("dirty").textContent'),/已手动修改/);
    await cdp.eval('document.getElementById("undo").click()');
    assert.equal((await downloadProject()).circuit.parts.find(p=>p.id==='R900').attrs.value,'1kΩ');
    return {questions:question,canvas_preserved:true,unsaved_edit_preserved:true,sse_questions_displayed:true,undo_preserved:true};
  });
  await loadProject(baseline);
  await check('rejected_mcu_request_preserves_existing_project',async()=>{
    await editOriginalResistance('3.3kΩ');
    const edited=(await downloadProject()).circuit;
    await cdp.eval(`document.getElementById('q').value='5V STM32F103 MCU 控制 I2C BMP280 传感器';document.getElementById('go').click()`);
    await cdp.until('!document.getElementById("go").disabled && (document.getElementById("meta").textContent.includes("拒绝") || document.getElementById("clarify").style.display === "block")');
    if(await cdp.eval('document.getElementById("clarify").style.display === "block"')){
      await cdp.eval('document.getElementById("cq-skip").click()');
      await cdp.until('!document.getElementById("go").disabled && document.getElementById("meta").textContent.includes("拒绝")');
    }
    assert(await cdp.eval('!!document.querySelector("#svg-host g[data-part=R900]")'),'Rejected request must preserve the existing project');
    const message=await cdp.eval('document.getElementById("meta").textContent');
    assert.deepEqual((await downloadProject()).circuit,edited);
    assert.match(await cdp.eval('document.getElementById("dirty").textContent'),/已手动修改/);
    await cdp.eval('document.getElementById("undo").click()');
    assert.equal((await downloadProject()).circuit.parts.find(p=>p.id==='R900').attrs.value,'1kΩ');
    return {message,canvas_preserved:true,unsaved_edit_preserved:true,undo_preserved:true};
  });
  await loadProject(baseline);
}
async function run(){
  if(!edge) throw new Error('Microsoft Edge missing');
  if(fullWorkflow)process.env.LSQC_HISTORY_DIR=path.join(profile,'workflow-history');
  const probe=net.createServer();await new Promise(resolve=>probe.listen(0,'127.0.0.1',resolve));const port=probe.address().port;await new Promise(resolve=>probe.close(resolve));
  const env=Object.assign({},process.env,{DEEPSEEK_API_KEY:'',PYTHON_DOTENV_DISABLED:'1',LSQC_HOST:'127.0.0.1',LSQC_PORT:String(port),KICAD_CLI_PATH:path.join(toolchain,'kicad/bin/kicad-cli.exe'),KICAD_SYMBOL_DIR:path.join(toolchain,'kicad/share/kicad/symbols'),KICAD_FOOTPRINT_DIR:path.join(toolchain,'kicad/share/kicad/footprints'),KICAD_CLI_TIMEOUT_S:'30',NGSPICE_PATH:path.join(toolchain,'ngspice/Spice64/bin/ngspice_con.exe')});
  server=spawn(python,['-X','utf8','-B','server.py'],{cwd:root,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
  server.stdout.on('data',data=>serverOutput+=data);server.stderr.on('data',data=>serverOutput+=data);
  for(let i=0;i<150;i++){try{const response=await fetch('http://127.0.0.1:'+port+'/api/health');if(response.ok){health=await response.json();break}}catch(_){}await delay(100)}assert(health,'Server failed '+serverOutput);assert.equal(health.llm_key_configured,false);
  browser=spawn(edge,['--headless=new','--disable-gpu','--no-sandbox','--no-first-run','--no-default-browser-check','--remote-debugging-port=0','--enable-automation','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:['ignore','ignore','pipe']});
  browser.stderr.on('data',data=>edgeOutput+=data);
  const active=path.join(profile,'DevToolsActivePort');
  for(let i=0;i<100&&!fs.existsSync(active);i++)await delay(100);
  assert(fs.existsSync(active),'Edge CDP failed '+edgeOutput);
  const cdpPort=fs.readFileSync(active,'utf8').split(/\r?\n/)[0];
  const version=await (await fetch('http://127.0.0.1:'+cdpPort+'/json/version')).json();
  browserCdp=await connect(version.webSocketDebuggerUrl);
  const command=await browserCdp.send('Browser.getBrowserCommandLine');
  assert(command.arguments.includes('--user-data-dir='+profile),'Browser ownership mismatch');
  const info=await browserCdp.send('SystemInfo.getProcessInfo');
  ownedBrowserPid=info.processInfo.find(item=>item.type==='browser').id;
  const targets=await(await fetch('http://127.0.0.1:'+cdpPort+'/json/list')).json();
  cdp=await connect(targets.find(item=>item.type==='page').webSocketDebuggerUrl);
  await cdp.send('Runtime.enable');await cdp.send('Network.enable');await cdp.send('Page.enable');await cdp.send('Log.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride',{width:1600,height:1000,deviceScaleFactor:1,mobile:false});await cdp.send('Page.navigate',{url:'http://127.0.0.1:'+port});
  await cdp.until('document.readyState==="complete" && !!window.CircuitTscircuitEditor');
  await cdp.eval(`(() => {const original={version:2,title:'TSX UI original',parts:[{id:'R900',type:'resistor',left:100,top:100,attrs:{value:'1kΩ'}}],connections:[]};const dt=new DataTransfer();dt.items.add(new File([JSON.stringify({circuit:original})],'original.json',{type:'application/json'}));const input=document.getElementById('file-json');input.files=dt.files;input.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  await cdp.until('!!document.querySelector("#svg-host g[data-part=R900]")');
  if(fullWorkflow)await runFullWorkflow();
  await check('real_rc_preview_validation_apply_edit_undo',async()=>{
    await cdp.eval(`document.querySelector('[data-workbench-tab="files"]')?.click()`);
    await cdp.eval('document.getElementById("open-tscircuit").click()');
    await cdp.until('document.getElementById("tscircuit-code").value.includes("<board")');
    await cdp.eval('document.getElementById("tscircuit-generate").click()');
    await cdp.until('document.getElementById("editReview").classList.contains("on")');
    assert(await cdp.eval('!!document.querySelector("#svg-host g[data-part=R900]")'),'Old canvas changed during review');
    assert(await cdp.eval('!document.getElementById("tscircuit-preview").hidden'));
    assert(requests.some(item=>item.url.endsWith('/api/design/tscircuit')&&item.status===200));
    assert(requests.some(item=>item.url.endsWith('/api/validate')&&item.status===200));
    fs.mkdirSync(evidenceDir,{recursive:true});
    const raw=await cdp.eval('document.getElementById("tscircuit-json").textContent');
    const source=await cdp.eval('document.getElementById("tscircuit-code").value');
    const svg=await cdp.eval('document.getElementById("tscircuit-preview").srcdoc');
    fs.writeFileSync(path.join(evidenceDir,'tscircuit_rc.tsx'),source);
    fs.writeFileSync(path.join(evidenceDir,'tscircuit_rc.json'),raw+'\n');
    fs.writeFileSync(path.join(evidenceDir,'tscircuit_rc.svg'),svg);
    const validationRequest=requests.findLast(item=>item.url.endsWith('/api/validate') && item.postData && JSON.parse(item.postData).source==='import' && JSON.parse(item.postData).circuit.parts.some(part=>part.id==='R1'));
    assert(validationRequest,'Candidate import validation request missing');
    const validationBody=await cdp.send('Network.getResponseBody',{requestId:validationRequest.id});
    fs.writeFileSync(path.join(evidenceDir,'tscircuit_rc_validation.json'),validationBody.body+'\n');
    const previewShot=await cdp.send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
    fs.writeFileSync(path.join(evidenceDir,'tscircuit-ui.png'),Buffer.from(previewShot.data,'base64'));
    await cdp.eval('document.getElementById("editAccept").click()');
    await cdp.until('!!document.querySelector("#svg-host g[data-part=R1]")');
    assert.equal(await cdp.eval('document.getElementById("undo").disabled'),false);
    await cdp.eval(`(() => {const g=document.querySelector('#svg-host g[data-part="R1"]');const r=g.getBoundingClientRect();const init={bubbles:true,cancelable:true,pointerId:1,pointerType:'mouse',button:0,clientX:r.left+r.width/2,clientY:r.top+r.height/2};g.dispatchEvent(new PointerEvent('pointerdown',init));g.dispatchEvent(new PointerEvent('pointerup',init));})()`);
    await cdp.until('document.querySelector("#props input") && document.getElementById("props").style.display !== "none"');
    await cdp.eval(`(() => {const row=Array.from(document.querySelectorAll('#props .row')).find(item=>item.querySelector('label')&&item.querySelector('label').textContent==='参数值');const input=row.querySelector('input');input.value='2kΩ';input.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    assert.equal(await cdp.eval(`Array.from(document.querySelectorAll('#props .row')).find(item=>item.querySelector('label')&&item.querySelector('label').textContent==='参数值').querySelector('input').value`),'2kΩ');
    const editedExport=await cdp.eval(`(async()=>{let saved;const original=URL.createObjectURL;URL.createObjectURL=function(blob){saved=blob;return original.call(this,blob)};try{document.getElementById('save-json').click();return JSON.parse(await saved.text())}finally{URL.createObjectURL=original}})()`);
    assert.equal(editedExport.circuit.parts.find(part=>part.id==='R1').attrs.value,'2kΩ');
    await cdp.eval('document.getElementById("undo").click()');
    await cdp.eval('document.getElementById("undo").click()');
    await cdp.until('!!document.querySelector("#svg-host g[data-part=R900]")');
    return {api:'real HTTP+Node',reviewPreservedOriginal:true,applyAndUndo:true,editedExportValue:editedExport.circuit.parts.find(part=>part.id==='R1').attrs.value,validationRequestId:validationRequest.id,jsonRecords:await cdp.eval('JSON.parse(document.getElementById("tscircuit-json").textContent).length')};
  });
  await check('unknown_ic_and_syntax_error_preserve_old_canvas',async()=>{
    const chip='export default () => (<board width="24mm" height="16mm"><chip name="U1" pinLabels={{1:"VCC",2:"GND"}} footprint="soic2" /></board>)';
    const bad='export default () => (<board><resistor name="R1" resistance={ /></board>)';
    const diagnostics=[];
    for(const code of [chip,bad]){
      const requestStart=requests.length;
      await cdp.eval('document.getElementById("tscircuit-code").value='+JSON.stringify(code)+';document.getElementById("tscircuit-generate").click()');
      await cdp.until('!document.getElementById("tscircuit-generate").disabled && document.getElementById("tscircuit-status").textContent.includes("未应用候选")');
      assert(await cdp.eval('!!document.querySelector("#svg-host g[data-part=R900]")'));
      assert.equal(await cdp.eval('document.getElementById("editReview").classList.contains("on")'),false);
      const rejectedRequest=requests.slice(requestStart).find(request=>request.url.endsWith('/api/design/tscircuit') && request.status===422);
      assert(rejectedRequest,'Invalid TSX must produce the expected HTTP 422 rejection');
      expectedHttpRejectionIds.add(rejectedRequest.id);
      diagnostics.push(await cdp.eval('document.getElementById("tscircuit-status").textContent'));
    }
    return diagnostics;
  });
  await check('cancel_preserves_old_canvas',async()=>{
    const rc=fs.readFileSync(path.join(root,'tools/upstream_examples/tscircuit_rc.tsx'),'utf8');
    await cdp.eval('document.getElementById("tscircuit-code").value='+JSON.stringify(rc)+';document.getElementById("tscircuit-generate").click();document.getElementById("tscircuit-cancel").click()');
    await cdp.until('document.getElementById("tscircuit-status").textContent.includes("已取消")');
    assert(await cdp.eval('!!document.querySelector("#svg-host g[data-part=R900]")'));
  });
  assert.equal(errors.length,0,'Uncaught browser errors');
  assert.deepEqual(unexpectedBrowserErrors(),[],'Unexpected native browser log errors');
  assert.equal(externalBrowserRequests.length,0,'Browser must use only local HTTP requests');
  assert.equal(generationCompletions.reduce((n,d)=>n+(Number(d.budget_used)||0),0),0,'Offline workflow must make no LLM calls');
}
(async()=>{
  let failure=null;
  try{await run();}catch(error){failure=error.stack;process.exitCode=1;console.error(failure);}
  finally{
    if(browserCdp){try{await browserCdp.send('Browser.close');}catch(_){}browserCdp.socket.close();}
    if(cdp)cdp.socket.close();
    if(server&&server.exitCode===null)await new Promise(resolve=>{const cleanup=spawn('taskkill',['/PID',String(server.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});cleanup.on('close',resolve);cleanup.on('error',resolve);});
    if(server&&server.exitCode===null)server.kill();
    if(browser&&browser.exitCode===null)browser.kill();
    if(ownedBrowserPid)await new Promise(resolve=>{const cleanup=spawn('taskkill',['/PID',String(ownedBrowserPid),'/T','/F'],{windowsHide:true,stdio:'ignore'});cleanup.on('close',resolve);cleanup.on('error',resolve);});
    await delay(500);
    if(path.resolve(profile).startsWith(path.resolve(os.tmpdir())+path.sep))try{fs.rmSync(profile,{recursive:true,force:true,maxRetries:5,retryDelay:200});}catch(_){}
    fs.mkdirSync(evidenceDir,{recursive:true});
    const previousRuns=priorEvidence ? [...(priorEvidence.previousRuns||[]),Object.fromEntries(Object.entries(priorEvidence).filter(([key])=>key!=='previousRuns'))] : [];
    fs.writeFileSync(priorEvidencePath,JSON.stringify({timestamp:new Date().toISOString(),command:(fullWorkflow?'FULL_WORKFLOW=1 ':'')+'node tests/test_tscircuit_ui.js',browser:'Microsoft Edge CDP',health,serverConfiguration:{direct_server_py:true,DEEPSEEK_API_KEY:'empty',PYTHON_DOTENV_DISABLED:'1'},results,failure,uncaughtBrowserErrors:errors,consoleMessages,browserLogEntries,expectedHttpRejectionIds:[...expectedHttpRejectionIds],unexpectedBrowserLogErrors:unexpectedBrowserErrors(),requests,downloads,llmCalls:generationCompletions.reduce((n,d)=>n+(Number(d.budget_used)||0),0),generationCompletions,externalBrowserRequests,ownedBrowserPid,previousRuns,data_split:'workflow_smoke_not_holdout',environmentObservations:['Real local Python HTTP server and Edge CDP; no API stubs or cloud LLM calls.','Log.enable and Log.entryAdded cover native SVG diagnostics; previous runs without browserLogEntries recorded only Runtime console calls and exceptions.','Two intentional invalid TSX cases return HTTP 422; matching native network diagnostics remain recorded and are identified by exact request IDs.']},null,2)+'\n');
  }
})();
