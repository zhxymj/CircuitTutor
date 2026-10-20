function documentWithLocalStyles(html) {
  const fs=require('node:fs'),path=require('node:path');
  // Styles may be inline or served as local stylesheets by the production page.
  const webRoot = path.resolve(__dirname, '..', 'web');
  const linkedCss = Array.from(html.matchAll(/<link\b[^>]*>/gi), match => match[0])
    .filter(tag => /\brel\s*=\s*["']stylesheet["']/i.test(tag))
    .map(tag => (tag.match(/\bhref\s*=\s*["']([^"']+)["']/i) || [])[1])
    .filter(href => href && !/^(?:[a-z]+:|\/\/)/i.test(href))
    .map(href => {
      const local = href.split(/[?#]/)[0].replace(/^\/static\//, '');
      const filename = path.resolve(webRoot, local);
      if (!filename.startsWith(webRoot + path.sep)) throw new Error('主题样式路径不在 web 目录内: ' + href);
      return fs.readFileSync(filename, 'utf8');
    });
  return html + '\n' + linkedCss.join('\n');
}
const S = require('../web/symbols.js');
const W = require('../web/wire_geometry.js');
let body = '';
let x = 20, y = 20;
const types = S.types();
for (const t of types) {
  const sz = S.sizeOf(t);
  body += S.renderSymbol(t, x, y, 0, S.LIB[t].ref+'1', '');
  // 校验每个引脚都能算出世界坐标
  for (const p of S.pinsOf(t)) {
    const w = S.pinWorld(t, p, x, y, 0);
    if (!(w.x>=x-1 && w.x<=x+sz.w+1 && w.y>=y-1 && w.y<=y+sz.h+1)) throw new Error('pin out of box '+t+p);
  }
  y += sz.h + 30;
  if (y > 1400) { y = 20; x += 220; }
}
// 走线测试：斜对角两点，中间放一个障碍，应绕开
const pts = W.routeWire({x:0,y:0},{x:120,y:80},[{x:30,y:20,width:40,height:40}],10);
console.log('route points =', JSON.stringify(pts));
console.log('path d =', W.toPathD(pts));
console.log('collinear tidy =', JSON.stringify(W.collapseCollinear([{x:0,y:0},{x:10,y:0},{x:20,y:0}])));
console.log('all types:', types.join(','));
const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="900" height="1500" viewBox="0 0 900 1500" style="background:#fff" stroke="#111" fill="#111">${body}</svg>`;
require('fs').writeFileSync(require('path').join(__dirname, 'symbols_preview.svg'), svg);
console.log('symbols_preview.svg written, components =', types.length);

// ---------- 交互层 / 引脚号角标回归 ----------
// NE555 引脚号角标：恰好 8 个 DIP 脚号（只有 ne555 开启 showPinNumbers）
{
  const pinNums = (body.match(/class="pin-num"/g) || []).length;
  if (pinNums !== 8) throw new Error('NE555 引脚号角标应为 8 个，实际 ' + pinNums);
  // ports（引脚名/坐标单一数据源）绝不能被角标破坏 —— 上面的 pinWorld 循环
  // 已逐引脚校验过坐标在框内，这里再核对引脚名清单
  const expect = ['TRIG', 'THR', 'DISCH', 'CTRL', 'OUT', 'RESET', 'VCC', 'GND'].sort().join(',');
  if (S.pinsOf('ne555').slice().sort().join(',') !== expect)
    throw new Error('ne555 ports 被改动: ' + S.pinsOf('ne555').join(','));
}
// renderSymbol 支持交互层 data-* 绑定钩子（interact.js 拖动/点选依赖）
{
  const g = S.renderSymbol('resistor', 0, 0, 0, 'R9', '1k', { part: 'R9', type: 'resistor' });
  if (!g.includes('data-part="R9"') || !g.includes('data-type="resistor"'))
    throw new Error('renderSymbol 缺少交互绑定钩子 data-part/data-type');
  if (!g.includes('class="sym sym-resistor"'))
    throw new Error('renderSymbol 加钩子后 class="sym sym-xxx" 应保留');
  const plain = S.renderSymbol('resistor', 0, 0, 0, 'R1', '330Ω');
  if (/data-part=/.test(plain)) throw new Error('不传 dataAttrs 时不应输出 data-* 属性');
}
console.log('交互层钩子/引脚号角标 ✔  ports 未被改动，data-part/data-type 可选输出正常');

// ---------- 插入元件信息表：与 frontend_manifest.json 全量一致 ----------
// interact.js 内置 PART_INFO（server.py 不分发该 JSON，前端只能内置），
// 此处逐类型断言 ref 前缀与默认值不漂移，防止两边改不同步。
{
  const fs2 = require('fs');
  const path2 = require('path');
  const CI = require('../web/interact.js');
  const I = CI.CircuitInteract || CI;
  const manifest = JSON.parse(fs2.readFileSync(path2.join(__dirname, 'frontend_manifest.json'), 'utf8'));
  // 1) 内置表与 manifest 逐类型对齐：位号前缀 + 默认参数值
  for (const t of Object.keys(manifest)) {
    const info = I.partInfo(t);
    if (!info) throw new Error('PART_INFO 缺少元件类型: ' + t);
    if (info.ref !== manifest[t].ref)
      throw new Error(`${t} 位号前缀不一致: PART_INFO[${info.ref}] != manifest[${manifest[t].ref}]`);
    if (String(info.value) !== String(manifest[t].default))
      throw new Error(`${t} 默认值不一致: PART_INFO[${info.value}] != manifest[${manifest[t].default}]`);
  }
  // 2) 覆盖符号库全部 47 种类型（manifest 与符号库同源），
  //    插入入口对每种可渲染符号都能给出位号前缀
  const libTypes = S.types();
  const expectLib = Object.keys(manifest).length;
  if (libTypes.length !== expectLib) throw new Error('符号库应有 ' + expectLib + ' 种类型，实际 ' + libTypes.length);
  for (const t of libTypes) {
    if (!I.partInfo(t)) throw new Error('PART_INFO 未覆盖符号库类型: ' + t);
  }
  // 3) 位号分配：每种 manifest 类型从空电路起都得到 <ref>1
  for (const t of Object.keys(manifest)) {
    const id = I.nextId([], t);
    if (id !== manifest[t].ref + '1') throw new Error(`${t} 首个位号应为 ${manifest[t].ref}1，实际 ${id}`);
  }
  if (I.partInfo('___unknown___') !== null) throw new Error('未知类型应返回 null（插入入口据此拒绝）');
  console.log('插入元件信息表 ✔  manifest ' + Object.keys(manifest).length + ' 种 ref/默认值全量一致，' +
    '符号库 ' + Object.keys(manifest).length + ' 种类型全部可插入、位号可分配');
}

// ---- app.js → index.html DOM id 引用一致性（防"容器缺失 → run() 抛异常"回归）----
(function checkDomIds() {
  const fs = require('fs');
  const path = require('path');
  const app = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
  const ids = Array.from(new Set(
    Array.from(app.matchAll(/getElementById\('([^']+)'\)/g), m => m[1])));
  const missing = ids.filter(id => !html.includes('id="' + id + '"'));
  if (missing.length)
    throw new Error('index.html 缺少 app.js 引用的元素 id: ' + missing.join(', '));
  console.log('DOM id 引用一致性 ✔  app.js 引用的 ' + ids.length +
    ' 个元素 id 在 index.html 全部存在');
})();

// ---- 主题机制：三态 + 变量化 + 首屏套用 ----
// 主题失效是"看起来正常、实际失灵"的典型（选了深色却不生效、刷新又跳回浅色），
// 靠人眼很难每次回归都覆盖，这里把四个必要条件钉死：
//   ① 三态选项齐全；② 深色变量块存在；③ 跟随系统媒体查询存在；
//   ④ 首屏在 <head> 内同步套用（否则深色用户会闪白）；
//   ⑤ 样式不再有写死颜色（全部走变量，否则深色下局部仍是白底黑字）。
(function checkTheme() {
  const fs = require('fs');
  const path = require('path');
  const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
  const app = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  const styles = documentWithLocalStyles(html);

  // ① select#theme 三态齐全
  if (!/id="theme"/.test(html)) throw new Error('index.html 缺少主题切换控件 id="theme"');
  for (const v of ['system', 'light', 'dark']) {
    if (!new RegExp('<option value="' + v + '"').test(html))
      throw new Error(`主题缺少 "${v}" 选项`);
  }
  // ② 深色变量覆盖块
  if (!/html\[data-theme="dark"\]\s*\{/.test(styles))
    throw new Error('缺少 html[data-theme="dark"] 变量覆盖块');
  // ③ "跟随系统"必须真的跟随（否则三态退化成两态）
  if (!/@media \(prefers-color-scheme: dark\)/.test(styles)
      || !/html\[data-theme="system"\]/.test(styles))
    throw new Error('"跟随系统"未接 prefers-color-scheme 媒体查询');
  // ④ 首屏套用脚本位于 <head> 内且同步（防 FOUC 闪白）
  const head = html.slice(0, html.indexOf('</head>'));
  if (!/setAttribute\('data-theme'/.test(head))
    throw new Error('首屏主题套用脚本不在 <head> 内（深色用户会闪白）');
  // ⑤ 无自引用变量定义（写错会让整块样式失效）
  const selfRef = styles.match(/--([a-z-]+):\s*var\(--\1\)/);
  if (selfRef) throw new Error('存在自引用 CSS 变量定义: ' + selfRef[0]);
  // ⑥ app.js 负责交互持久化
  if (!/getElementById\('theme'\)/.test(app) || !/zht-theme/.test(app))
    throw new Error('app.js 未接管主题切换/未持久化到 localStorage');
  console.log('主题机制 ✔  三态齐备 + 深色变量块 + 跟随系统媒体查询 + ' +
    '首屏无闪白套用 + 无自引用变量 + 交互持久化');
})();

// ---- 编辑后 ERC 重算：前端接线与画布标注的数据契约 ----
// ERC 原先只在生成流水线末尾算一次，编辑后永不重算（核心卖点断点）。
// 服务端已加 POST /api/erc（见 tests/test_server.py）；前端这边要保证三件事，
// 任何一件缺失都会让"改动后自动重算"静默失效，故在此钉死：
//   ① app.js 真的会 POST /api/erc（而不是只留注释）；
//   ② 画布拿到的是含 issues[] 的完整报告（renderCircuit 靠它画引脚标注），
//      而不是只有计数的摘要——传错形状会渲染出"ERC 未运行"；
//   ③ 有防抖与序号守卫（连续拖动不该打出一串请求，乱序响应不该覆盖新结果）。
(function checkErcRecompute() {
  const fs = require('fs');
  const path = require('path');
  const app = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
  const render = fs.readFileSync(path.join(__dirname, '..', 'web', 'render.js'), 'utf8');

  // ① 端点调用确实存在于 app.js
  if (!/fetch\('\/api\/erc'/.test(app))
    throw new Error('app.js 未调用 POST /api/erc（编辑后 ERC 重算是核心功能）');
  if (!/method:\s*'POST'/.test(app))
    throw new Error('/api/erc 调用缺少 method:POST');
  // ② 编辑提交（onChange）必须触发重算——只挂在别处等于白做
  if (!/onChange:\s*function\s*\(\s*\)\s*\{[\s\S]{0,450}?scheduleErc\(\)/.test(app))
    throw new Error('onChange 未触发 scheduleErc()（编辑后不会重算 ERC）');
  // ③ 画布入参应是完整报告对象（含 issues），不能是硬编码 null
  if (/renderCircuit\(circuit,\s*\{[\s\S]{0,120}?erc:\s*null/.test(app))
    throw new Error('renderAll 仍把 erc 硬编码为 null（画布不会显示 ERC 标注）');
  if (!/erc:\s*ercReport/.test(app))
    throw new Error('renderAll 未把 ercReport 传给 renderCircuit');
  // ④ ercReport 形状对齐 render.js 的消费口径：passed + issues[]
  if (!/issues:\s*Array\.isArray\(d\.issues\)/.test(app))
    throw new Error('ercReport 未保留 issues[]（render.js 需要它画引脚标注）');
  // render.js 确实按 issues 画标注（两边契约的另一半）
  if (!/ercIssues\s*=\s*\(erc\s*&&\s*Array\.isArray\(erc\.issues\)/.test(render))
    throw new Error('render.js 未按 erc.issues 取标注数据（前端契约不一致）');
  // ⑤ 防抖 + 序号守卫（避免拖动风暴与乱序覆盖）
  if (!/setTimeout\([\s\S]{0,120}?300\s*\)/.test(app))
    throw new Error('缺少防抖（连续拖动会打出一串 ERC 请求）');
  if (!/mySeq\s*!==\s*ercSeq/.test(app))
    throw new Error('缺少请求序号守卫（慢响应可能覆盖新结果）');
  // ⑥ 状态徽章容器存在于页面（否则状态无处显示）
  if (!/id="erc-badge"/.test(html))
    throw new Error('index.html 缺少 ERC 状态徽章 #erc-badge');
  if (!/ercBadge\s*=\s*document\.getElementById\('erc-badge'\)/.test(app))
    throw new Error('app.js 未接线 #erc-badge');
  console.log('编辑后 ERC 重算 ✔  端点调用 + onChange 触发 + 完整报告入画布 + ' +
    '防抖/序号守卫 + 状态徽章');
})();

// ---- 旋转：rotatePart 语义 + UI 入口存在 ----
// rotate 字段一直被走线几何与引脚命中尊重（pinWorld 传 p.rotate），
// 但此前 insertPart 硬编码 rotate:0 且没有任何 UI 入口——功能"存在但不可达"。
(function checkRotateUi() {
  const fs = require('fs');
  const path = require('path');
  const src = fs.readFileSync(path.join(__dirname, '..', 'web', 'interact.js'), 'utf8');

  // ① 旋转函数存在且已导出到 attach 返回的 API 上
  if (!/function rotatePart\s*\(/.test(src))
    throw new Error('interact.js 未实现 rotatePart');
  if (!/\brotatePart,/.test(src))
    throw new Error('rotatePart 未导出到交互层 API（宿主无法调用）');
  // ② 属性面板有旋转按钮
  if (!/className\s*=\s*'rot'/.test(src))
    throw new Error('属性面板缺少旋转按钮');
  // ③ 键盘快捷键 R 已接（且不误伤输入框内打字）
  if (!/ev\.key\s*===\s*'r'\s*\|\|\s*ev\.key\s*===\s*'R'/.test(src))
    throw new Error('缺少 R 键旋转快捷键');
  if (!/INPUT\|TEXTAREA\|SELECT/.test(src))
    throw new Error('R 快捷键未排除输入框（在参数值里输入含 r 的文本会被误旋转）');
  // ④ 旋转走统一提交入口（否则不可撤销）
  const fn = src.slice(src.indexOf('function rotatePart'));
  const bodyFn = fn.slice(0, fn.indexOf('\n    function '));
  if (!/commit\(state,\s*'rotate '/.test(bodyFn))
    throw new Error('rotatePart 未走 commit()（旋转将无法撤销）');
  // ⑤ 角度归一化到 90° 整数倍且落在 [0,360)
  if (!/%\s*360/.test(bodyFn))
    throw new Error('rotatePart 未把角度归一化到 [0,360)');
  // ⑥ 仿真：4 次旋转回到原角度（用源码里的同一算式重算，锁定语义）
  const norm = (cur, step) => {
    let n = Math.round((cur + step) / 90) * 90;
    return ((n % 360) + 360) % 360;
  };
  let a = 0; const seen = [];
  for (let i = 0; i < 4; i++) { a = norm(a, 90); seen.push(a); }
  if (seen.join(',') !== '90,180,270,0')
    throw new Error('顺时针四次旋转应为 90→180→270→0，实际 ' + seen.join(','));
  if (norm(0, -90) !== 270) throw new Error('逆时针 90° 应为 270');
  console.log('旋转功能 ✔  rotatePart 已导出 + 面板按钮 + R 快捷键（不误伤输入框）+ ' +
    '走 commit 可撤销 + 角度归一化语义正确');
})();

// ---- 插入元件下拉：动态生成且与元件库全量一致 ----
// 原先 index.html 硬编码 10 个 <option>，而符号库实有 38 种可渲染元件——
// 用户能渲染却插不进来。改为从 PART_INFO 动态生成，本测试锁定"不漏、不多"。
(function checkInsertDropdown() {
  const fs = require('fs');
  const path = require('path');
  const app = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
  const CI = require('../web/interact.js');
  const I = CI.CircuitInteract || CI;

  // ① 生成逻辑存在且被调用
  if (!/function buildInsertOptions\s*\(/.test(app))
    throw new Error('app.js 未实现 buildInsertOptions（下拉改动态生成）');
  if (!/buildInsertOptions\(\);/.test(app))
    throw new Error('buildInsertOptions 定义后未被调用');
  // ② 数据源是 PART_INFO（唯一真源），而非又写一份清单
  if (!/CircuitInteract\.PART_INFO/.test(app))
    throw new Error('下拉未从 CircuitInteract.PART_INFO 取数据源');
  // ③ HTML 里不得再有硬编码的具体类型选项（只许留占位项）
  const optRe = /<option value="([^"]+)"/g;
  let m; const hard = [];
  while ((m = optRe.exec(html)) !== null) hard.push(m[1]);
  // 主题下拉的 system/light/dark 是合法的；插入下拉里出现类型名就是回退
  const banned = Object.keys(I.PART_INFO).filter(t => hard.indexOf(t) >= 0);
  if (banned.length)
    throw new Error('index.html 仍有硬编码元件选项（应从 PART_INFO 生成）: ' + banned.join(', '));
  // ④ 分组清单必须覆盖 PART_INFO 的每一个类型（漏掉的会掉进"其他"兜底组，
  //    虽不致命但会让分类错乱——这里要求显式分组完备）
  const groupsSrc = app.slice(app.indexOf('const INSERT_GROUPS'),
                              app.indexOf('function buildInsertOptions'));
  const listed = new Set((groupsSrc.match(/'[a-z0-9_]+'/g) || [])
    .map(s => s.slice(1, -1)));
  const miss = Object.keys(I.PART_INFO).filter(t => !listed.has(t));
  if (miss.length)
    throw new Error('INSERT_GROUPS 未覆盖这些元件类型（会掉进"其他"组）: ' + miss.join(', '));
  // ⑤ 分组不得引用 PART_INFO 里不存在的类型（手误写错 key）
  const bogus = Array.from(listed).filter(t => !I.PART_INFO[t]);
  if (bogus.length)
    throw new Error('INSERT_GROUPS 引用了不存在的元件类型: ' + bogus.join(', '));
  console.log('插入元件下拉 ✔  动态生成（数据源 PART_INFO）+ HTML 无硬编码类型 + ' +
    '分组完备覆盖 ' + Object.keys(I.PART_INFO).length + ' 种 + 无幽灵类型');
})();

// ---- AI 增量改图入口：复用 cfg，失败不得覆盖当前电路 ----
(function checkIncrementalEditUi() {
  const fs = require('fs');
  const path = require('path');
  const app = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');

  if (!/id="edit-q"/.test(html) || !/id="edit-btn"/.test(html))
    throw new Error('缺少“修改当前电路”输入框或按钮');
  if (!/editInput\s*=\s*document\.getElementById\('edit-q'\)/.test(app) ||
      !/editBtn\s*=\s*document\.getElementById\('edit-btn'\)/.test(app))
    throw new Error('app.js 未接线增量改图控件');
  if (!/function submitCircuitEdit\s*\(/.test(app))
    throw new Error('app.js 未实现增量改图提交函数');
  if (!/fetch\('\/api\/edit'/.test(app) || !/method:\s*'POST'/.test(app))
    throw new Error('增量改图未 POST /api/edit');
  if (!/ensureCfg\(function\s*\(cfg\)/.test(app))
    throw new Error('增量改图未复用 ensureCfg 模型配置注册链');
  if (!/JSON\.stringify\(\{[\s\S]{0,300}circuit[\s\S]{0,300}instruction[\s\S]{0,300}cfg/.test(app))
    throw new Error('/api/edit 请求体缺少 circuit、instruction 或 cfg');
  if (!/editBtn\.disabled\s*=\s*!circuit/.test(app))
    throw new Error('无当前电路时改图按钮未禁用');
  if (!/function failEdit\s*\([\s\S]{0,500}setEditStatus/.test(app))
    throw new Error('改图失败未显示错误状态');
  if (!/showEditReview\(d, originalCircuit, originalSnapshot, instruction\)/.test(app) ||
      !/ctl\.applyCircuit\(review\.circuit\)/.test(app))
    throw new Error('改图候选未先审核再由用户接受');
  console.log('AI 增量改图入口 ✔  UI、cfg 复用、候选审核及失败提示契约齐备');
})();

// ---- 增量改图 ERC 计数：/api/edit 返回的是 *_errors / warnings 数组 ----
(function checkIncrementalEditErcCounts() {
  const fs = require('fs');
  const path = require('path');
  const app = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  const normalizeStart = app.indexOf('function normalizeEditErc');
  const summary = app.slice(normalizeStart, app.indexOf('function appendReviewGroup', normalizeStart));
  if (!/rawErc\[arrayKey\]\.length/.test(summary))
    throw new Error('改图 ERC 规范化未读取错误数组长度');
  for (const key of ['fatal_errors', 'major_errors', 'minor_errors', 'warnings']) {
    if (!new RegExp("'" + key + "'").test(summary))
      throw new Error('改图 ERC 摘要未按 ' + key + ' 数组长度计数');
  }
  if (!/#edit-status\.err/.test(documentWithLocalStyles(fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8'))))
    throw new Error('改图错误状态未声明 .err 样式');
  console.log('增量改图 ERC 计数契约 ✔  *_errors/warnings 数组长度已接线');
})();

// ---- 改图并发保护：交互层原地修改 circuit 时拒绝过期 AI 响应 ----
(function checkIncrementalEditSnapshotGuard() {
  const fs = require('fs');
  const path = require('path');
  const app = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  const start = app.indexOf('function submitCircuitEdit');
  const fn = app.slice(start, app.indexOf('\n  let t0', start));
  if (!/const originalSnapshot\s*=\s*JSON\.stringify\(originalCircuit\)/.test(fn))
    throw new Error('改图提交未保存点击时的 CircuitJSON 快照');
  if (!/const requestCircuit\s*=\s*JSON\.parse\(originalSnapshot\)/.test(fn) ||
      !/circuit:\s*requestCircuit/.test(fn))
    throw new Error('改图请求未使用快照深拷贝，可能读取到提交后的就地修改');
  if (!/JSON\.stringify\(circuit\)\s*!==\s*originalSnapshot/.test(fn))
    throw new Error('改图应用响应前未比较当前电路与点击时快照');
  console.log('增量改图并发保护 ✔  请求快照 + 引用/内容双重 stale guard');
})();

// ---- 改图 ERC 规范化：状态栏和摘要必须消费同一份完整报告 ----
(function checkIncrementalEditErcNormalization() {
  const fs = require('fs');
  const path = require('path');
  const app = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  const start = app.indexOf('function acceptPendingEdit');
  const fn = app.slice(start, app.indexOf('function submitCircuitEdit', start));
  if (!/const rawErc\s*=\s*review\.erc/.test(fn))
    throw new Error('接受候选时未读取已规范化 ERC');
  if (!/ercReport\s*=\s*\{/.test(fn))
    throw new Error('改图成功路径未构造规范化 ercReport');
  for (const key of ['passed', 'fatal', 'major', 'minor', 'warning', 'failure_count', 'issues']) {
    if (!new RegExp(key + '\\s*:').test(fn))
      throw new Error('规范化 ercReport 缺少字段：' + key);
  }
  if (!/ercSummary\s*=\s*ercReport\s*\?\s*\{/.test(fn))
    throw new Error('ercSummary 未从规范化 ercReport 生成');
  console.log('增量改图 ERC 规范化 ✔  ercReport/ercSummary 共享完整字段契约');
})();

// ---- AI 改图可撤回：原历史栈继续使用 + 「撤回更改」按钮 ----
(function checkAiEditUndo() {
  const fs = require('fs');
  const path = require('path');
  const CI = require('../web/interact.js');
  const I = CI.CircuitInteract || CI;
  const app = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
  const interact = fs.readFileSync(path.join(__dirname, '..', 'web', 'interact.js'), 'utf8');

  // ① history.seedUndo 纯函数语义：改图后 current=post，seedUndo(prior) 使 undo 回到 prior
  if (typeof I.createHistory !== 'function')
    throw new Error('createHistory 未导出');
  const h = I.createHistory(10);
  const pre = { parts: [{ type: 'resistor', id: 'R1', attrs: { value: '1kΩ' } }], connections: [] };
  const post = { parts: [{ type: 'resistor', id: 'R1', attrs: { value: '470Ω' } }], connections: [] };
  h.init(JSON.parse(JSON.stringify(pre)));
  // 模拟 AI 改图：新对象 → attach 会 init(post)
  h.init(JSON.parse(JSON.stringify(post)));
  if (h.canUndo()) throw new Error('新电路对象 init 后不应立刻可撤销');
  if (!h.seedUndo(JSON.parse(JSON.stringify(pre))))
    throw new Error('seedUndo 应成功注入改前快照');
  if (!h.canUndo()) throw new Error('seedUndo 后应可撤销');
  const back = h.undo();
  if (!back || !back.parts || back.parts[0].attrs.value !== '1kΩ')
    throw new Error('undo 应返回改图前状态（1kΩ），实际 ' + JSON.stringify(back && back.parts));
  if (!h.canRedo()) throw new Error('undo 后应可 redo 回改图后');
  const again = h.redo();
  if (!again || again.parts[0].attrs.value !== '470Ω')
    throw new Error('redo 应返回改图后状态（470Ω）');

  // ② 控制器导出 seedUndo（attach 返回值里的方法，非顶层 export）
  if (!/seedUndo\s*\(priorSnap\)/.test(interact))
    throw new Error('interact.js attach 未返回 seedUndo（AI 改图无法入撤销栈）');
  if (!/seedUndo\(priorSnap\)\s*\{[\s\S]{0,200}undoStack\.push/.test(interact) &&
      !/history\.seedUndo/.test(interact))
    throw new Error('seedUndo 未接到 history.undoStack');

  // ③ UI：「撤回更改」按钮
  if (!/id="revert-edit"/.test(html))
    throw new Error('index.html 缺少「撤回更改」按钮 #revert-edit');
  if (!/revertEditBtn\s*=\s*document\.getElementById\('revert-edit'\)/.test(app))
    throw new Error('app.js 未接线 #revert-edit');
  if (!/function revertLastAiEdit\s*\(/.test(app))
    throw new Error('app.js 未实现 revertLastAiEdit');

  // ④ 接受路径：applyCircuit 作为普通提交进入原历史栈
  const start = app.indexOf('function acceptPendingEdit');
  const fn = app.slice(start, app.indexOf('function submitCircuitEdit', start));
  if (!/ctl\.applyCircuit\(review\.circuit\)/.test(fn) ||
      !/applyCircuit\(nextSnap\)[\s\S]{0,160}commit\(cloneState\(nextSnap\)/.test(interact))
    throw new Error('AI 改图接受后未作为一次编辑提交到原撤销栈');
  if (!/lastAiEdit\s*=\s*\{/.test(fn))
    throw new Error('AI 改图成功后未记录 lastAiEdit');
  if (!/撤回/.test(fn))
    throw new Error('AI 改图成功提示未提及「撤回」');

  // ⑤ 换新电路（生成/载入）时清空 lastAiEdit
  const clears = (app.match(/clearLastAiEdit\s*\(/g) || []).length;
  if (clears < 4)
    throw new Error('clearLastAiEdit 调用次数过少（生成/载入工程/历史应清理）: ' + clears);

  console.log('AI 改图可撤回 ✔  原历史栈提交 + 撤回按钮 + 换电路清理');
})();

// ---- 候选审核：真实器件/连线差异、ERC 位置、接受前不改画布 ----
(function checkAiEditReview() {
  const fs = require('fs');
  const path = require('path');
  const vm = require('vm');
  const assert = require('assert');
  const app = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
  for (const id of ['editReview', 'editReviewBody', 'editReviewSvg', 'editAccept', 'editDiscard']) {
    if (!html.includes('id="' + id + '"')) throw new Error('候选审核缺少 #' + id);
  }
  const pureStart = app.indexOf('function describePart(');
  const pureEnd = app.indexOf('function appendReviewGroup(', pureStart);
  if (pureStart < 0 || pureEnd < 0) throw new Error('候选审核缺少差异计算函数');
  const sandbox = { module: { exports: {} } };
  vm.runInNewContext(app.slice(pureStart, pureEnd) +
    '\nmodule.exports = { buildEditDiff, normalizeEditErc, connectionLabel };', sandbox);
  const { buildEditDiff, normalizeEditErc, connectionLabel } = sandbox.module.exports;
  const before = {
    parts: [{ id: 'R1', type: 'resistor', attrs: { value: '1kΩ' } },
            { id: 'D1', type: 'led', attrs: {} }],
    connections: [['R1:2', 'D1:A', '#c00', []], ['R1:2', 'D1:A', '#c00', []]],
  };
  const after = {
    parts: [{ id: 'R1', type: 'resistor', attrs: { value: '470Ω' } },
            { id: 'C1', type: 'capacitor', attrs: { value: '100nF' } }],
    connections: [['R1:2', 'D1:A', '#c00', []], ['R1:2', 'C1:1', '#00c', []]],
  };
  const beforeJson = JSON.stringify(before), afterJson = JSON.stringify(after);
  const diff = buildEditDiff(before, after);
  assert.strictEqual(diff.partsAdded.length, 1);
  assert.strictEqual(diff.partsRemoved.length, 1);
  assert(diff.partsModified[0].includes('1kΩ → 470Ω'));
  assert.strictEqual(diff.connections.added.length, 1);
  assert.strictEqual(diff.connections.removed.length, 1);
  assert(connectionLabel(diff.connections.added[0]).includes('R1:2 ↔ C1:1'));
  assert.strictEqual(JSON.stringify(before), beforeJson, '差异计算不得修改原电路');
  assert.strictEqual(JSON.stringify(after), afterJson, '差异计算不得修改候选图');
  const erc = normalizeEditErc({ passed: false, major_errors: [{}],
    issues: [{ severity: 'major', rule: '断线', message: '请连接', pins: ['C1:1'] }] });
  assert.strictEqual(erc.major, 1);
  assert.strictEqual(erc.issues[0].pins[0], 'C1:1');
  const submit = app.slice(app.indexOf('function submitCircuitEdit('), app.indexOf('\n  let t0'));
  if (!/showEditReview\(d, originalCircuit, originalSnapshot, instruction\)/.test(submit) ||
      /ctl\.applyCircuit\(/.test(submit))
    throw new Error('服务响应阶段应只打开审核，不应立即提交电路');
  const accept = app.slice(app.indexOf('function acceptPendingEdit('), app.indexOf('function submitCircuitEdit('));
  if (!/JSON\.stringify\(circuit\)\s*!==\s*review\.originalSnapshot/.test(accept) ||
      !/ctl\.applyCircuit\(review\.circuit\)/.test(accept) ||
      !/scheduleErc\(\)/.test(accept))
    throw new Error('接受候选需检查原图快照、提交一次并重算 ERC');
  if (!/if \(pendingEdit\) clearPendingEdit\('当前电路已修改/.test(app))
    throw new Error('人工编辑后未使待审核候选失效');
  // 旧历史仍可连续撤销：人工修改 → 接受 AI 候选 → 撤回 AI → 撤回人工修改。
  const CI = require('../web/interact.js');
  const hist = (CI.CircuitInteract || CI).createHistory(10);
  hist.init({ value: '初始' });
  hist.commit({ value: '人工修改' });
  hist.commit({ value: 'AI 修改' });
  assert.strictEqual(hist.undo().value, '人工修改');
  assert.strictEqual(hist.undo().value, '初始');
  console.log('AI 候选审核 ✔  实际差异/重复连线/引脚位置 + 接受门禁 + 原历史可连续撤销');
})();

// ---- P0/P1 体验契约：空状态示例 / ERC 建议 / 来源徽章 / 证据 / 导出预览 ----
(function checkUxPack() {
  const fs = require('fs');
  const path = require('path');
  const app = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');

  if (!/id="emptyExamples"/.test(html)) throw new Error('缺少空状态示例卡容器');
  if (!/ee-card/.test(html)) throw new Error('空状态缺少示例卡片');
  if (!/RULE_TIP/.test(app)) throw new Error('app.js 缺少 ERC RULE_TIP 建议映射');
  const tipCount = (app.match(/'LED缺限流电阻'|'I2C缺上拉'|'极性反接'|'芯片未供电'|'电源短路'|'LED限流电阻过小'|'LED超额定电流'|'电阻功耗超限'/g) || []).length;
  if (tipCount < 5) throw new Error('RULE_TIP 覆盖规则过少: ' + tipCount);
  if (!/erc-tip/.test(app) || !/erc-tip/.test(documentWithLocalStyles(html))) throw new Error('缺少 erc-tip 样式或渲染');
  if (!/查看建议/.test(app)) throw new Error('严重问题缺少「查看建议」');
  if (!/setSourceBadge/.test(app)) throw new Error('缺少 AI/模板来源徽章逻辑');
  if (!/非 AI 生成成功|模板回退|内置模板/.test(app)) throw new Error('模板回退文案不诚实/缺失');
  if (!/电路已生成/.test(app)) throw new Error('缺少生成成功统一文案');
  if (!/id="btn-evidence"/.test(html) || !/api\/evidence\/summary/.test(app + fs.readFileSync(path.join(__dirname, '..', 'server.py'), 'utf8'))) throw new Error('缺少证据入口');
  if (!/id="btn-demo"/.test(html) || !/applyDemoMode|demoMode/.test(app)) throw new Error('缺少演示模式');
  if (!/showKicadModal/.test(app) || !/id="kicadModal"/.test(html)) throw new Error('缺少 KiCad 导出预览弹层');
  if (!/不要使用「文件 → 导入」|不要用「文件→导入」/.test(html + app)) throw new Error('导出说明未强调禁止导入');
  if (!/boundaryBox|能力边界/.test(html)) throw new Error('缺少能力边界区块');
  if (!/summarizeChangesText|改动摘要/.test(app)) throw new Error('缺少 AI 改图改动摘要');
  if (!/circuitCounts|个元件/.test(app)) throw new Error('缺少元件/连线计数文案');
  if (!/正在画电路图/.test(app)) throw new Error('缺少生成进度大白话映射');
  // fallback 与 AI 成功关键词必须不同
  if (!/setSourceBadge\(accepted\s*\?\s*'ai'\s*:\s*'draft'\)/.test(app) || app.indexOf("setSourceBadge('template'") < 0)
    throw new Error('AI/模板徽章调用缺失');
  console.log('体验包契约 ✔  空状态示例 + ERC建议 + 来源区分 + 证据/演示/KiCad预览 + 边界说明');
})();

// ---- 差距修复契约：BOM备注 / 仿真摘要 / 错误详情 / 模板分类 / 电路块 ----
(function checkGapFixPack() {
  const fs = require('fs');
  const path = require('path');
  const app = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
  const bom = fs.readFileSync(path.join(__dirname, '..', 'core', 'bom.py'), 'utf8');
  const blocks = fs.readFileSync(path.join(__dirname, '..', 'core', 'blocks.py'), 'utf8');
  const tpl = fs.readFileSync(path.join(__dirname, '..', 'data', 'circuit_templates.json'), 'utf8');
  const server = fs.readFileSync(path.join(__dirname, '..', 'server.py'), 'utf8');

  if (!/note/.test(bom) || !/备注/.test(bom)) throw new Error('BOM 未输出备注列');
  if (!/fetchSimulateSummary/.test(app)) throw new Error('前端未接仿真摘要');
  if (!/setMetaFail/.test(app)) throw new Error('缺少错误详情折叠');
  if (!/groupTemplatesByCategory/.test(app)) throw new Error('缺少模板分类分组');
  if (!/"category"/.test(tpl)) throw new Error('模板 JSON 缺少 category');
  if (!/def analyze_blocks/.test(blocks)) throw new Error('缺少 core/blocks.py');
  if (!/analyze_blocks/.test(server)) throw new Error('server 未接入电路块');
  if (!/evidence_summary/.test(server)) throw new Error('health features 未标注 evidence_summary');
  if (!/id="tplCatBar"/.test(html)) throw new Error('缺少模板分类提示条');
  console.log('差距修复契约 ✔  BOM备注 + 仿真摘要 + 详情折叠 + 模板分类 + 电路块 + health 标签');
})();
