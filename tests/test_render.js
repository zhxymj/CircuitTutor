/* ============================================================
 * test_render.js —— 智绘电路“UI”全链路测试（Node 侧）
 * ============================================================
 * 1) 目录-符号同步：core/catalog 的每种元件在 symbols.js 都有符号；
 * 2) 自动布局质量：任意两元件包围盒不重叠（LLM 坐标做不到的硬指标）；
 * 3) 整图渲染：data/circuit_templates.json 的 4 份电路 + erc_results.json 的
 *    ERC 结果 → render_*.svg，断言无 NaN、导线齐全、junction 点存在；
 * 4) 主题变体：kicad / print / blueprint。
 * 运行（项目根执行）：先 python -B tests/test_pipeline.py，再 node tests/test_render.js
 * ============================================================ */
const fs = require('fs');
const path = require('path');
const S = require('../web/symbols.js');
const L = require('../web/layout.js');
const R = require('../web/render.js');

const circuits = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', 'circuit_templates.json'), 'utf8'));
const ercResults = fs.existsSync(path.join(__dirname, 'erc_results.json'))
  ? JSON.parse(fs.readFileSync(path.join(__dirname, 'erc_results.json'), 'utf8')) : {};

// ---------- 1. 目录-符号同步 ----------
const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'frontend_manifest.json'), 'utf8'));
const manifestTypes = Object.keys(manifest);
const missing = manifestTypes.filter(t => !S.LIB[t]);
if (missing.length) throw new Error('元件目录缺符号: ' + missing.join(','));
console.log(`目录-符号同步 ✔  ${manifestTypes.length} 种元件全部有符号`);

// 逐元件校验：目录 pins 与符号 ports 完全一致（防“符号有脚、目录没有”的错位）
for (const t of manifestTypes) {
  const cat = (manifest[t].pins || []).map(String).sort().join(',');
  const sym = S.pinsOf(t).map(String).sort().join(',');
  if (cat !== sym) throw new Error(`引脚不一致 ${t}: 目录[${cat}] 符号[${sym}]`);
}
console.log('引脚白名单一致 ✔  目录 pins 与符号 ports 完全对应');

// ---------- 2. 自动布局：不重叠 + 全部吸附网格 ----------
function boxesOverlap(a, b) {
  return a.x < b.x + b.w - 1 && b.x < a.x + a.w - 1 &&
         a.y < b.y + b.h - 1 && b.y < a.y + a.h - 1;
}
for (const [name, c] of Object.entries(circuits)) {
  const { parts } = L.autoLayout(c, S.sizeOf);
  if (!parts.length) continue;
  const boxes = parts.map(p => {
    const s = S.sizeOf(p.type) || { w: 96, h: 56 };
    return { id: p.id, x: p.left, y: p.top, w: s.w, h: s.h };
  });
  for (let i = 0; i < boxes.length; i++) {
    if (boxes[i].x % 10 || boxes[i].y % 10) throw new Error(`${name}: ${boxes[i].id} 未吸附网格`);
    for (let j = i + 1; j < boxes.length; j++) {
      if (boxesOverlap(boxes[i], boxes[j])) {
        throw new Error(`${name}: 元件重叠 ${boxes[i].id} × ${boxes[j].id}`);
      }
    }
  }
}
console.log('自动布局质量 ✔  4 份电路零重叠、全网格对齐');

// ---------- 2b. 电源轨放置回归（2026-09-10 修复两处布局缺陷） ----------
// 缺陷1：堆叠间距双重计算（`minY - s.h - 60 - i*70` 叠加运行中的 minY），
//        电源件一多就把画布向上撑出等差数列式的大空白。
// 缺陷2：`battery:+` 直连 `vcc` 标记件时，该连接两端都是电源件、在邻接表
//        构建阶段被跳过，电池拿不到 pwrTargets，横向锚点退化成画布正中，
//        竖向也被排到 vcc 标记件【下方】——视觉上电池浮在空处而非挂在轨道上。
const szOfType = t => S.sizeOf(t) || { w: 100, h: 60 };

// 用例 A：电池 + vcc 标记 —— 电池必须位于 vcc 标记件下方、且贴近它对齐
{
  const c = {
    version: 1,
    parts: [{ type: 'battery', id: 'BAT1' }, { type: 'vcc', id: 'V1' },
            { type: 'resistor', id: 'R1' }, { type: 'led', id: 'LED1' },
            { type: 'gnd', id: 'G1' }],
    connections: [['BAT1:+', 'V1:1'], ['V1:1', 'R1:1'], ['R1:2', 'LED1:+'],
                  ['LED1:-', 'G1:1'], ['BAT1:-', 'G1:1']],
  };
  const { parts } = L.autoLayout(c, S.sizeOf);
  const byId = {};
  parts.forEach(p => { byId[p.id] = p; });
  const bat = byId.BAT1, v1 = byId.V1;
  if (!bat || !v1) throw new Error('布局用例A：电源件缺失');
  // 电池在 vcc 标记件下方（y 更大）
  if (!(bat.top > v1.top)) {
    throw new Error(`布局用例A：电池应排在 vcc 标记件下方，实际 BAT1.y=${bat.top} V1.y=${v1.top}`);
  }
  // 电池必须位于信号区上方（仍在顶部电源区域，不能掉进信号行）
  const sigTop = Math.min(...parts.filter(p => ['R1', 'LED1'].includes(p.id)).map(p => p.top));
  if (!(bat.top < sigTop)) {
    throw new Error(`布局用例A：电池未落在电源区（BAT1.y=${bat.top} 应 < 信号区顶 ${sigTop}）`);
  }
  // 电池与 vcc 标记件的水平中心应对齐（同一条轨），容差 1 格
  const batCx = bat.left + szOfType(bat.type).w / 2;
  const v1Cx = v1.left + szOfType(v1.type).w / 2;
  if (Math.abs(batCx - v1Cx) > 20) {
    throw new Error(`布局用例A：电池未与 vcc 轨对齐（中心差 ${Math.abs(batCx - v1Cx)}px）`);
  }
  console.log('电源轨放置 ✔  电池+vcc标记：电池排在标记件下方且与轨对齐');
}

// 用例 B：电源件间距必须是常数，不随序号递增（防间距双重计算回归）
{
  const parts = [{ type: 'battery', id: 'BAT1' }, { type: 'vcc', id: 'V1' },
                 { type: 'vcc', id: 'V2' }, { type: 'resistor', id: 'R1' },
                 { type: 'led', id: 'LED1' }, { type: 'gnd', id: 'G1' }];
  const c = {
    version: 1, parts,
    connections: [['BAT1:+', 'V1:1'], ['V1:1', 'V2:1'], ['V2:1', 'R1:1'],
                  ['R1:2', 'LED1:+'], ['LED1:-', 'G1:1'], ['BAT1:-', 'G1:1']],
  };
  const { parts: laid } = L.autoLayout(c, S.sizeOf);
  const ys = laid.filter(p => ['BAT1', 'V1', 'V2'].includes(p.id))
    .sort((a, b) => a.top - b.top).map(p => p.top);
  if (ys.length !== 3) throw new Error('布局用例B：电源件未全部参与布局');
  const gaps = [ys[1] - ys[0], ys[2] - ys[1]];
  // 三件高度不同（vcc 40 / battery 36），间距按“上一件顶边 - 本件高 - 60”算：
  // 关键是两条间距必须接近，绝不能出现第二条远大于第一条的等差膨胀
  if (Math.abs(gaps[0] - gaps[1]) > 10) {
    throw new Error(`布局用例B：电源件间距不均（${gaps.join(' vs ')}），疑似间距被重复叠加`);
  }
  console.log('电源轨放置 ✔  多电源件间距均匀（无递增膨胀）');
}

// ---------- 3. 整图渲染 ----------
// 硬指标：所有导线必须严格正交（水平/垂直），专业原理图不允许斜线
function assertOrthogonal(svg, name) {
  const ds = [...svg.matchAll(/class="wire"[^>]*d="([^"]+)"/g)].map(m => m[1]);
  for (const d of ds) {
    const pts = [...d.matchAll(/[ML]\s*(-?[\d.]+)[ ,]+(-?[\d.]+)/g)]
      .map(m => [parseFloat(m[1]), parseFloat(m[2])]);
    for (let i = 1; i < pts.length; i++) {
      const [ax, ay] = pts[i - 1], [bx, by] = pts[i];
      if (Math.abs(ax - bx) > 0.5 && Math.abs(ay - by) > 0.5) {
        throw new Error(`${name}: 导线出现斜线 [${ax},${ay}]->[${bx},${by}]`);
      }
    }
  }
}

let totalWires = 0;
for (const [name, c] of Object.entries(circuits)) {
  const out = R.renderCircuit(c, {
    title: c.title,
    erc: ercResults[name],
  });
  const svg = out.svg;
  if (/NaN|undefined/.test(svg)) throw new Error(`${name}: SVG 含 NaN/undefined`);
  assertOrthogonal(svg, name);
  if (!svg.includes('智绘电路 CircuitTutor')) throw new Error(`${name}: 缺标题栏`);
  const nWires = (svg.match(/class="wire"/g) || []).length;
  const nDots = (svg.match(/<circle[^>]+junction|r="4" fill/g) || []).length;
  const nSyms = (svg.match(/class="sym /g) || []).length;
  if (nWires === 0) throw new Error(`${name}: 一根导线都没画出来`);
  totalWires += nWires;

  const expect = {
    led_limit: { passed: true, minWires: 3, minSyms: 4 },
    ne555_blink: { passed: true, minWires: 14, minSyms: 10 },
    opamp_inv: { passed: true, minWires: 8, minSyms: 7 },
    led_no_limit: { passed: false, minWires: 3, minSyms: 5 },
    pot_divider: { passed: true, minWires: 6, minSyms: 5 },
    button_buzzer: { passed: true, minWires: 4, minSyms: 4 },
    diode_protect: { passed: true, minWires: 5, minSyms: 5 },
    parallel_led: { passed: true, minWires: 7, minSyms: 6 },
    npn_switch: { passed: true, minWires: 8, minSyms: 7 },
    zener_reg: { passed: true, minWires: 4, minSyms: 4 },
    // 2026-09-11 扩容模板：有导线+元件即可，不做更严阈值
    rc_filter: { passed: true, minWires: 3, minSyms: 3 },
    rectifier_filter: { passed: true, minWires: 4, minSyms: 4 },
    pnp_switch: { passed: true, minWires: 6, minSyms: 6 },
    opamp_buffer: { passed: true, minWires: 5, minSyms: 5 },
    crystal_clock: { passed: true, minWires: 4, minSyms: 4 },
    optocoupler_led: { passed: true, minWires: 6, minSyms: 5 },
    buck_module: { passed: true, minWires: 2, minSyms: 2 },
    hbridge_motor: { passed: true, minWires: 6, minSyms: 5 },
    sevenseg_counter: { passed: true, minWires: 7, minSyms: 6 },
    sensor_dht11: { passed: true, minWires: 3, minSyms: 3 },
    lc_filter: { passed: true, minWires: 4, minSyms: 4 },
    fuse_protect: { passed: true, minWires: 3, minSyms: 4 },
    servo_driver: { passed: true, minWires: 3, minSyms: 3 },
    relay_driver: { passed: true, minWires: 5, minSyms: 4 },
    schottky_protect: { passed: true, minWires: 3, minSyms: 4 },
    photoresistor_night: { passed: true, minWires: 5, minSyms: 5 },
    thermistor_alarm: { passed: true, minWires: 5, minSyms: 5 },
    bridge_regulator: { passed: true, minWires: 6, minSyms: 5 },
    esd_tvs: { passed: true, minWires: 3, minSyms: 4 },
    varistor_protect: { passed: true, minWires: 3, minSyms: 3 },
  }[name];
  // 新增模板若未定义期望，只做基本渲染校验（上面已查 NaN/正交/标题/导线）
  if (!expect) continue;
  if (nWires < expect.minWires) throw new Error(`${name}: 导线数 ${nWires} < ${expect.minWires}`);
  if (nSyms < expect.minSyms) throw new Error(`${name}: 符号数 ${nSyms} < ${expect.minSyms}`);
  const ercPass = ercResults[name] && ercResults[name].passed;
  if (ercPass !== expect.passed) throw new Error(`${name}: ERC 状态 ${ercPass} != 预期 ${expect.passed}`);
  if (expect.passed && out.warnings.length) throw new Error(`${name}: 正常电路不应有渲染警告 ${out.warnings}`);

  // 多引脚网络（≥3）必须有 junction 点或网络标签
  if (name === 'ne555_blink') {
    const hasDots = /r="4"/.test(svg);
    const hasNetLabel = /class="net-label"/.test(svg);
    if (!hasDots || !hasNetLabel) throw new Error(`${name}: GND/VCC 汇流网络缺少 junction 点或网络标签`);
  }
  // 错误电路必须有 ERC 红圈和图例面板
  if (name === 'led_no_limit') {
    if (!svg.includes('erc-circle')) throw new Error('led_no_limit: 缺 ERC 错误标记圈');
    if (!svg.includes('电气规则检查（ERC）')) throw new Error('led_no_limit: 缺 ERC 图例面板');
  }

  fs.writeFileSync(path.join(__dirname, `render_${name}.svg`), svg);
  console.log(`render_${name}.svg ✔  ${out.width}x${out.height}  导线${nWires} 结点${nDots} 网络${out.nets} 警告${out.warnings.length}`);
}
console.log(`共渲染导线 ${totalWires} 条`);

// ---------- 4. 主题变体 ----------
for (const theme of ['kicad', 'print', 'blueprint']) {
  const out = R.renderCircuit(circuits.led_limit, { title: `LED 限流电路（${theme} 主题）`, theme });
  fs.writeFileSync(path.join(__dirname, `render_theme_${theme}.svg`), out.svg);
  if (/NaN|undefined/.test(out.svg)) throw new Error(`${theme}: SVG 含 NaN/undefined`);
  assertOrthogonal(out.svg, theme);
}
console.log('主题变体 ✔  kicad / print / blueprint');

// ---------- 4b. 网络颜色图例（opt-in，默认关闭不得出现） ----------
{
  const base = R.renderCircuit(circuits.led_limit, { title: '无图例' });
  if (base.svg.includes('网络颜色')) throw new Error('默认渲染不应出现图例（opt-in 被破坏）');
  const leg = R.renderCircuit(circuits.led_limit, {
    title: '带图例', wireColors: true, legend: true,
    erc: { passed: true, issues: [] },
  });
  if (!leg.svg.includes('网络颜色')) throw new Error('opts.legend 未生效');
  if (!leg.svg.includes('#c0392b')) throw new Error('图例缺电源色样');
  if (!/ERC：✔ 通过/.test(leg.svg)) throw new Error('ERC 状态未正确显示');
  console.log('网络颜色图例 ✔  默认关闭 / opt-in 生效 / 色样与 ERC 状态正确');
}

// ---------- 5. keep 模式（沿用 LLM 坐标） ----------
const keep = R.renderCircuit({
  parts: [
    { type: 'battery', id: 'BAT1', top: 100, left: 60, attrs: { value: '5V' } },
    { type: 'resistor', id: 'R1', top: 100, left: 260, attrs: { value: '330Ω' } },
    { type: 'led', id: 'LED1', top: 100, left: 480 },
    { type: 'gnd', id: 'GND1', top: 320, left: 480 },
  ],
  connections: [
    ['BAT1:+', 'R1:1', 'red', []], ['R1:2', 'LED1:+', 'red', []],
    ['LED1:-', 'GND1:1', 'black', []], ['BAT1:-', 'GND1:1', 'black', []],
  ],
}, { title: 'keep 模式（LLM 坐标）', layout: 'keep' });
if (/NaN|undefined/.test(keep.svg) || !keep.svg.includes('class="wire"')) {
  throw new Error('keep 模式渲染失败');
}
if (!keep.svg.includes('data-part="R1"') || !keep.svg.includes('data-type="resistor"')) {
  throw new Error('keep 模式符号缺少交互层绑定 data-part/data-type');
}
assertOrthogonal(keep.svg, 'keep');
fs.writeFileSync(path.join(__dirname, 'render_keep_mode.svg'), keep.svg);
console.log('keep 模式 ✔  沿用 LLM 坐标渲染正常（含 data-part 绑定）');

// ---------- 6. 交互层钩子 + NE555 引脚号角标 + interact.js 冒烟 ----------
// 6.1 svg 根带 class="circuit-svg"（interact.js 缩放/平移的挂载点）
{
  const out = R.renderCircuit(circuits.led_limit, { title: '交互钩子' });
  if (!/class="circuit-svg"/.test(out.svg)) throw new Error('svg 根缺少 class="circuit-svg"');
  if (!/viewBox="/.test(out.svg)) throw new Error('svg 根缺少 viewBox');
}
// 6.2 每个符号 group 带 data-part/data-type（interact.js 依此定位元件）
{
  const out = R.renderCircuit(circuits.ne555_blink, { title: '交互钩子' });
  const nSym = (out.svg.match(/class="sym /g) || []).length;
  const nPart = (out.svg.match(/data-part="/g) || []).length;
  if (!nSym || nPart < nSym) throw new Error(`符号缺少 data-part 绑定（符号 ${nSym} / 绑定 ${nPart}）`);
  if (!out.svg.includes('data-part="U1"') || !out.svg.includes('data-type="ne555"')) {
    throw new Error('ne555 符号缺少 data-part="U1" / data-type="ne555"');
  }
  console.log('交互层绑定钩子 ✔  符号 group 均带 data-part/data-type，svg 根带 circuit-svg');
}
// 6.3 NE555 引脚号角标：DIP-8 的 1~8 全部出现（教学价值：对照实物接线），
//     且只作用于 ne555 —— ports（引脚名/坐标）不受影响（见 1. 目录-符号同步）
{
  const out = R.renderCircuit(circuits.ne555_blink, { title: '555' });
  const nums = [...out.svg.matchAll(/class="pin-num"[^>]*>(\d+)<\/text>/g)]
    .map(m => +m[1]).sort((a, b) => a - b);
  if (nums.join(',') !== '1,2,3,4,5,6,7,8') throw new Error('NE555 引脚号角标不全: ' + nums.join(','));
  const led = R.renderCircuit(circuits.led_limit, { title: 'led' });
  if (/class="pin-num"/.test(led.svg)) throw new Error('非 IC 电路不应出现引脚号角标');
  const reg = S.renderSymbol('regulator_7805', 0, 0, 0, 'U9', '');
  if (/class="pin-num"/.test(reg)) throw new Error('未开启 showPinNumbers 的 IC 不应出现引脚号');
  console.log('NE555 引脚号角标 ✔  DIP-8 脚号 1~8 齐全（9px / #999 / 框外引脚根部）');
}
// 6.4 interact.js 冒烟（Node 无 DOM：确认可加载、全局对象与 API 存在、纯逻辑正确）
{
  const CI = require('../web/interact.js');          // Node：module.exports
  const I = CI.CircuitInteract || CI;                  // 浏览器形态兼容
  if (typeof I.attach !== 'function') throw new Error('CircuitInteract.attach 不存在');
  if (I.snap(14) !== 10 || I.snap(15) !== 20 || I.snap(-6) !== -10) throw new Error('snap 网格吸附错误');
  if (I.partOfRef('R1:1') !== 'R1' || I.partOfRef('GND1:1') !== 'GND1') throw new Error('partOfRef 解析错误');
  const conns = [['R1:1', 'LED1:+', 'red', []], ['BAT1:-', 'GND1:1', 'black', []]];
  const rel = I.connsRelatedTo(conns, 'R1');
  if (rel.length !== 1 || rel[0][0] !== 'R1:1') throw new Error('connsRelatedTo 首端匹配错误');
  const rel2 = I.connsRelatedTo(conns, 'GND1');
  if (rel2.length !== 1 || rel2[0][1] !== 'GND1:1') throw new Error('connsRelatedTo 次端匹配错误');
  if (I.connsRelatedTo(conns, 'C9').length !== 0) throw new Error('connsRelatedTo 不应误匹配');
  console.log('interact.js 冒烟 ✔  CircuitInteract.attach 就绪，吸附/引用解析/删除关联逻辑正确');
}

// 6.5 插入元件位号分配 + 撤销/重做历史（纯函数，Node 离线可测；
//     UI 里的 attach/insertPart 调用的就是这两个导出，保证线上/测试同源）
{
  const CI2 = require('../web/interact.js');
  const I2 = CI2.CircuitInteract || CI2;

  // ---- 位号分配 nextId(parts, type)：按类型 ref 前缀递增、跳过已占用 ----
  if (typeof I2.nextId !== 'function') throw new Error('CircuitInteract.nextId 未导出');
  if (I2.nextId([], 'resistor') !== 'R1') throw new Error('空电路插电阻应得 R1');
  if (I2.nextId([{ id: 'R1' }, { id: 'R2' }], 'resistor') !== 'R3') throw new Error('已有 R1,R2 时应得 R3');
  if (I2.nextId([{ id: 'LED1' }], 'led') !== 'LED2') throw new Error('有 LED1 时应得 LED2');
  if (I2.nextId([{ id: 'R1' }, { id: 'R3' }], 'resistor') !== 'R2') throw new Error('应跳空补位得 R2');
  // 跨类型不冲突：R/LED/Q/BAT 各自独立计数
  const mixed = [{ id: 'R1' }, { id: 'LED1' }, { id: 'Q1' }, { id: 'BAT1' }];
  if (I2.nextId(mixed, 'resistor') !== 'R2') throw new Error('跨类型应得 R2');
  if (I2.nextId(mixed, 'led') !== 'LED2') throw new Error('跨类型应得 LED2');
  if (I2.nextId(mixed, 'npn') !== 'Q2') throw new Error('npn 前缀应为 Q');
  if (I2.nextId(mixed, 'battery') !== 'BAT2') throw new Error('battery 前缀应为 BAT');
  if (I2.nextId(mixed, 'ne555') !== 'U1') throw new Error('ne555 前缀应为 U');
  // partInfo：查不到的类型返回 null（插入入口据此拒绝）
  if (I2.partInfo('resistor').ref !== 'R' || I2.partInfo('resistor').value !== '10k')
    throw new Error('partInfo 电阻信息错误');
  if (I2.partInfo('no_such_type') !== null) throw new Error('未知类型应返回 null');

  // ---- 撤销/重做历史 createHistory(limit)：commit → undo → redo 序列 ----
  if (typeof I2.createHistory !== 'function') throw new Error('CircuitInteract.createHistory 未导出');
  const h = I2.createHistory();
  h.init({ v: 0 });
  if (h.canUndo() || h.canRedo()) throw new Error('初始状态既不可撤销也不可重做');
  h.commit({ v: 1 });
  h.commit({ v: 2 });
  if (!h.canUndo()) throw new Error('有变更后应可撤销');
  // undo 返回要恢复的状态：当前 2 → 撤回 1 → 撤回 0（到底）
  if (h.undo().v !== 1) throw new Error('第一次 undo 应回到 v=1');
  if (h.undo().v !== 0) throw new Error('第二次 undo 应回到 v=0');
  if (h.canUndo()) throw new Error('回到初始后不可再撤销');
  if (!h.canRedo()) throw new Error('撤销后应可重做');
  if (h.undo() !== null) throw new Error('无可撤销时应返回 null');
  // redo 与 undo 互逆：0 → 1 → 2（到底）
  if (h.redo().v !== 1) throw new Error('第一次 redo 应到 v=1');
  if (h.redo().v !== 2) throw new Error('第二次 redo 应到 v=2');
  if (h.canRedo()) throw new Error('重做到头后不可再重做');
  if (h.redo() !== null) throw new Error('无可重做时应返回 null');

  // 新 commit 作废 redoStack：undo 到 v=1 后提交 v=9，v=2 的重做历史被清空
  h.undo(); // 回到 v=1，此时 redoStack=[2]
  if (!h.canRedo()) throw new Error('此时应有可重做内容');
  h.commit({ v: 9 });
  if (h.canRedo()) throw new Error('新提交后 redoStack 应被清空');
  if (h.undo().v !== 1) throw new Error('新提交后撤销应回到 v=1（跳过被作废的 v=2）');

  // 超过上限丢最旧：limit=3，从 v=0 起依次提交 v=1..4，
  // 最多只能撤 3 步（4→3→2→1，最旧的 v=0 被挤掉），重做原路返回
  const h3 = I2.createHistory(3);
  h3.init({ v: 0 });
  for (let i = 1; i <= 4; i++) h3.commit({ v: i });
  const back = [];
  while (h3.canUndo()) back.push(h3.undo().v);
  if (back.join(',') !== '3,2,1') throw new Error('超限丢最旧错误: ' + back.join(','));
  const fwd = [];
  while (h3.canRedo()) fwd.push(h3.redo().v);
  if (fwd.join(',') !== '2,3,4') throw new Error('重做序列错误: ' + fwd.join(','));

  console.log('interact.js 位号/历史 ✔  nextId 分配正确；commit/undo/redo 序列、' +
    '新提交清空 redoStack、超限丢最旧均正确');
}

// ---------- 8. 几何基线：SVG 输出自身的引脚-导线一致性 ----------
// 前 7 节断言的是“数量/正交/绑定”，无法拦住几何漂移：若导线整体偏移若干像素，
// 导线数、正交性、data-part 全都不变，测试仍绿，但图上导线与引脚已脱开
// （画面表现为“线头悬空”）。
//
// 关键设计：坐标【只从渲染产物 SVG 里读】，不再调用 autoLayout 重算。
// 若用 autoLayout 重算坐标去和导线端点比，两边会同步漂移、永远“相等”——
// 那种写法只能证明内部自洽，拦不住真实回归（已实测：注入 7px 漂移时假绿）。
// 本节改为：从 SVG 的每个 <g class="sym ..." data-part="X" transform="translate(x,y)">
// 读出该元件【实际被画在】的坐标，用它与 SVG 里的导线端点做交叉校验，
// 任何“符号与导线不一致”的输出都会被抓到。
{
  const EPS = 0.6;   // 容差：浮点累加与 0.5 栅格取整允许的偏差
  const cases = ['led_limit', 'ne555_blink', 'opamp_inv', 'npn_switch', 'zener_reg'];

  // 从 SVG 中提取导线的合法端点（含中间折点：KiCad 风格走线的每个顶点
  // 都可能落在引脚上，例如 L 形拐弯的第一段起点就是引脚位置）
  function wirePoints(svg) {
    const pts = [];
    for (const m of svg.matchAll(/class="wire"[^>]*d="([^"]+)"/g)) {
      for (const p of m[1].matchAll(/[ML]\s*(-?[\d.]+)[ ,]+(-?[\d.]+)/g)) {
        pts.push([parseFloat(p[1]), parseFloat(p[2])]);
      }
    }
    return pts;
  }

  // 从 SVG 中提取元件实际绘制坐标：<g ... data-part="X" ... transform="translate(x,y)">
  function symbolPositions(svg) {
    const map = {};
    for (const m of svg.matchAll(
      /<g[^>]*data-part="([^"]+)"[^>]*transform="translate\((-?[\d.]+),(-?[\d.]+)\)"/g)) {
      map[m[1]] = { x: parseFloat(m[2]), y: parseFloat(m[3]) };
    }
    return map;
  }

  let checkedEdges = 0;
  for (const name of cases) {
    const c = circuits[name];
    if (!c) continue;
    const out = R.renderCircuit(c, { title: c.title, erc: ercResults[name] });
    const svg = out.svg;
    const pts = wirePoints(svg);
    const pos = symbolPositions(svg);
    if (!Object.keys(pos).length) throw new Error(`${name}: SVG 中读不到任何元件坐标`);
    if (!pts.length) throw new Error(`${name}: SVG 中读不到任何导线坐标`);

    for (const conn of (c.connections || [])) {
      const refs = conn.filter(r => typeof r === 'string' && r.includes(':'));
      if (refs.length < 2) continue;
      for (const ref of [refs[0], refs[refs.length - 1]]) {
        const i = ref.indexOf(':');
        const pid = ref.slice(0, i), pin = ref.slice(i + 1);
        const sp = pos[pid];
        if (!sp) continue;                   // 该元件未被绘制（如锚点件）跳过
        const part = (c.parts || []).find(q => q.id === pid);
        if (!part) continue;
        let w;
        try { w = S.pinWorld(part.type, pin, sp.x, sp.y, part.rotate || 0); }
        catch (e) { continue; }              // 引脚无符号定义时跳过
        const hit = pts.some(([x, y]) =>
          Math.abs(x - w.x) <= EPS && Math.abs(y - w.y) <= EPS);
        if (!hit) {
          throw new Error(`${name}: 按 SVG 中元件 ${pid} 的实际坐标 ${sp.x},${sp.y} ` +
            `算出引脚 ${ref} 应在 (${w.x.toFixed(1)},${w.y.toFixed(1)})，` +
            `但 SVG 里没有任何导线端点落在该处——导线与引脚脱开（几何基线失败）`);
        }
        checkedEdges++;
      }
    }
  }
  if (checkedEdges < 20) {
    throw new Error(`几何基线覆盖过少（仅校验 ${checkedEdges} 个端点），基线形同虚设`);
  }
  console.log(`几何基线 ✔  ${cases.length} 份电路共 ${checkedEdges} 个引脚-导线端点重合（坐标取自 SVG 产物，容差 ${EPS}px）`);
}

// ---------- 8b. 几何基线有效性自检（先红后绿）----------
// 故意把某条连线的引脚引用指向一个不存在的坐标，断言校验逻辑确实会失败。
// 没有这一步，第 8 节可能因为“容差过松/根本没进循环”而永远绿——护栏必须有
// 自己的护栏。
{
  const c = JSON.parse(JSON.stringify(circuits.led_limit));
  const out = R.renderCircuit(c, { title: c.title, erc: ercResults.led_limit });
  const svg = out.svg;
  // 把 SVG 里所有导线端点整体平移 50px（模拟“导线与符号不同步”的真实回归）
  const shifted = svg.replace(
    /(class="wire"[^>]*d=")([^"]+)(")/g,
    (mm, a, d, b) => a + d.replace(/(-?[\d.]+)[ ,]+(-?[\d.]+)/g,
      (_, x, y) => `${parseFloat(x) + 50} ${parseFloat(y) + 50}`) + b);

  let detected = false;
  try {
    const pts = [];
    for (const m of shifted.matchAll(/class="wire"[^>]*d="([^"]+)"/g)) {
      for (const p of m[1].matchAll(/[ML]\s*(-?[\d.]+)[ ,]+(-?[\d.]+)/g)) {
        pts.push([parseFloat(p[1]), parseFloat(p[2])]);
      }
    }
    const pos = {};
    for (const m of svg.matchAll(
      /<g[^>]*data-part="([^"]+)"[^>]*transform="translate\((-?[\d.]+),(-?[\d.]+)\)"/g)) {
      pos[m[1]] = { x: parseFloat(m[2]), y: parseFloat(m[3]) };
    }
    for (const conn of c.connections) {
      const refs = conn.filter(r => typeof r === 'string' && r.includes(':'));
      for (const ref of [refs[0], refs[refs.length - 1]]) {
        const i = ref.indexOf(':');
        const sp = pos[ref.slice(0, i)];
        if (!sp) continue;
        const part = c.parts.find(q => q.id === ref.slice(0, i));
        let w;
        try { w = S.pinWorld(part.type, ref.slice(i + 1), sp.x, sp.y, part.rotate || 0); }
        catch (e) { continue; }
        if (!pts.some(([x, y]) => Math.abs(x - w.x) <= EPS && Math.abs(y - w.y) <= EPS)) {
          detected = true; break;
        }
      }
      if (detected) break;
    }
  } catch (e) { detected = true; }
  if (!detected) {
    throw new Error('几何基线有效性自检失败：注入 50px 漂移后校验竟然仍通过，' +
      '说明第 8 节的容差或遍历有漏洞（护栏失效）');
  }
  console.log('几何基线有效性自检 ✔  注入 50px 漂移可被检出（先红后绿验证通过）');
}

// ---------- 8c. 电源轨 / 地轨间距一致性 ----------
// 布局约定：电源件排在内容上方、地件排在下方，各自与主体保持固定间隙。
// 回归由来（2026-09-09）：地轨原写 `maxY + 60 + i * 70` 且循环内更新 maxY，
// 两个偏移叠加使间距随序号膨胀——3 个地件实测 180 / 250，视觉上越排越散，
// 也与电源轨的固定 60px 策略不一致。本测试锁定"相邻同类轨元件间距恒定"。
{
  const gndCount = 3;
  const c = {
    parts: [
      { type: 'battery', id: 'BAT1', attrs: { value: '5V' } },
      { type: 'resistor', id: 'R1', attrs: { value: '330' } },
      { type: 'led', id: 'LED1', attrs: { value: '红' } },
    ],
    connections: [['BAT1:+', 'R1:1'], ['R1:2', 'LED1:+']],
  };
  for (let i = 1; i <= gndCount; i++) {
    c.parts.push({ type: 'gnd', id: 'GND' + i });
    c.connections.push(['LED1:-', 'GND' + i + ':1']);
    c.connections.push(['BAT1:-', 'GND' + i + ':1']);
  }
  const out = L.autoLayout(JSON.parse(JSON.stringify(c)), S.sizeOf, {});
  const gs = out.parts.filter(p => p.type === 'gnd')
    .sort((a, b) => String(a.id).localeCompare(String(b.id)));
  if (gs.length !== gndCount)
    throw new Error(`地件应有 ${gndCount} 个，实际 ${gs.length}`);
  const gaps = [];
  for (let i = 1; i < gs.length; i++) gaps.push(gs[i].top - gs[i - 1].top);
  const uniq = Array.from(new Set(gaps));
  if (uniq.length !== 1) {
    throw new Error('地轨相邻间距应恒定，实际每段为 ' + gaps.join(' / ') +
      '（间距逐段膨胀说明 i*70 偏移叠加回归）');
  }
  if (gaps[0] <= 0) throw new Error('地轨间距应为正，实际 ' + gaps[0]);
  // 与电源轨对照：三个 vcc 也应等距（两条轨策略一致的旁证）
  // 注意方向：电源件排在主体上方，故相邻间距为负（向上堆叠）；地件排在
  // 下方为正。一致性只看"各段是否等距"，不约束符号。
  const c2 = {
    parts: [
      { type: 'battery', id: 'BAT1', attrs: { value: '5V' } },
      { type: 'resistor', id: 'R1', attrs: { value: '330' } },
      { type: 'led', id: 'LED1', attrs: { value: '红' } },
    ],
    connections: [['BAT1:+', 'R1:1'], ['R1:2', 'LED1:+'], ['LED1:-', 'GND:1']],
  };
  c2.parts.push({ type: 'gnd', id: 'GND' });
  for (let i = 1; i <= 3; i++) {
    c2.parts.push({ type: 'vcc', id: 'VCC' + i });
    c2.connections.push(['R1:1', 'VCC' + i + ':1']);
  }
  c2.connections.push(['BAT1:-', 'GND:1']);
  const out2 = L.autoLayout(JSON.parse(JSON.stringify(c2)), S.sizeOf, {});
  const vs = out2.parts.filter(p => p.type === 'vcc')
    .sort((a, b) => String(a.id).localeCompare(String(b.id)));
  const vgaps = [];
  for (let i = 1; i < vs.length; i++) vgaps.push(vs[i].top - vs[i - 1].top);
  if (vgaps.length && Array.from(new Set(vgaps)).length !== 1) {
    throw new Error('电源轨相邻间距应恒定，实际 ' + vgaps.join(' / '));
  }
  console.log(`轨间距一致性 ✔  地轨 ${gs.length} 件等距 ${gaps[0]}px（向下），` +
    `电源轨等距 ${Math.abs(vgaps[0] || 0)}px（向上）——两轨均无递增膨胀`);
}

// ---------- 10. Routing detours and dense crossing semantics ----------
// These assertions use independent coordinate arithmetic, rather than asking
// the routing helpers whether their own output is valid.
{
  const assert=require('node:assert/strict'),W=require('../web/wire_geometry.js');
  const results=[];
  function check(name,task){try{const detail=task();results.push({name,status:'pass',detail});}catch(error){results.push({name,status:'fail',error:error.message});}}
  function hitsOpenRect(a,b,r){
    return Math.abs(a.y-b.y)<0.001
      ? a.y>r.y&&a.y<r.y+r.height&&Math.max(a.x,b.x)>r.x&&Math.min(a.x,b.x)<r.x+r.width
      : Math.abs(a.x-b.x)<0.001&&a.x>r.x&&a.x<r.x+r.width&&Math.max(a.y,b.y)>r.y&&Math.min(a.y,b.y)<r.y+r.height;
  }
  check('large_obstacle_detour_keeps_real_terminals_and_escapes_outward',()=>{
    const start={x:20,y:100},end={x:500,y:100},obstacle={x:100,y:0,width:300,height:200};
    const points=W.routeWire(start,end,[obstacle],2,'R','L');
    assert.deepEqual(points[0],start);assert.deepEqual(points[points.length-1],end);
    assert(points[1].x>start.x&&points[1].y===start.y,'Source must first leave its right-facing pin outward');
    const penultimate=points[points.length-2];assert(penultimate.x<end.x&&penultimate.y===end.y,'Target must approach its left-facing pin from outside');
    const collisions=points.slice(1).filter((b,i)=>hitsOpenRect(points[i],b,obstacle));
    assert.equal(collisions.length,0,'A large rectangle blocks the old fixed L/Z/U candidates, but a legal outer detour exists');
    return {start,end,obstacle,points};
  });
  check('different_net_crossings_are_hopped_without_junction_or_backward_stroke',()=>{
    const wires=[{net:1,points:[{x:0,y:0},{x:100,y:0}]},{net:2,points:[{x:40,y:-20},{x:40,y:20}]},{net:3,points:[{x:45,y:-20},{x:45,y:20}]}];
    const crossings=W.wireCrossings(wires);
    assert(crossings.some(p=>p.x===40&&p.y===0));assert(crossings.some(p=>p.x===45&&p.y===0));
    const path=W.pathWithHops(wires[0].points,crossings,6);
    const commands=[...path.matchAll(/([MLA])([^MLA]+)/g)];let lastX=-Infinity;
    for(const command of commands){const values=command[2].trim().split(/[ ,]+/).map(Number),x=command[1]==='A'?values[5]:values[0];assert(x>=lastX-0.001,'Nearby distinct crossings must not draw a backward protruding stroke: '+path);lastX=x;}
    assert(/A/.test(path),'Crossed horizontal net must still show a visual hop');
    const dots=W.junctionDots([wires[0].points]);assert.deepEqual(dots,[],'An isolated straight wire must not acquire junction dots from foreign nets');
    return {wires,crossings,path};
  });
  check('same_net_T_has_one_junction_while_plain_bends_have_none',()=>{
    const trunk=[{x:0,y:0},{x:100,y:0}],branch=[{x:50,y:-40},{x:50,y:0}];
    assert.deepEqual(W.junctionDots([trunk,branch]),[{x:50,y:0}]);
    assert.deepEqual(W.junctionDots([trunk,[{x:50,y:-40},{x:50,y:40}]]),[{x:50,y:0}],'A four-way intersection within one electrical net requires one junction dot');
    assert.deepEqual(W.junctionDots([[{x:0,y:0},{x:50,y:0},{x:50,y:50}]]),[]);
    return {trunk,branch};
  });
  check('impossible_manual_overlap_retains_connection_and_reports_blocked_route',()=>{
    const circuit={version:2,parts:[{id:'R1',type:'resistor',left:0,top:0},{id:'R2',type:'resistor',left:400,top:0},{id:'U1',type:'ne555',left:110,top:0}],connections:[['R1:2','R2:1']]};
    const before=JSON.stringify(circuit),out=R.renderCircuit(circuit,{layout:'keep',collect:true});
    assert.equal(JSON.stringify(circuit),before,'Rendering must preserve the source topology even when a user puts its pin inside another part');
    assert(out.warnings.some(message=>/R1:2/.test(message)&&/无法|阻塞|调整/.test(message)),'Impossible routing must give a specific actionable warning');
    assert(out.diag.wires.some(wire=>wire.blocked),'Impossible geometry must remain explicitly blocked, rather than appear accepted');
    assert.match(out.svg,/data-route-status="blocked"/,'The retained problem connection needs a visible blocked-route mark');
    assert.match(out.svg,/stroke-dasharray=/,'Blocked wire must be visibly distinguished');
    assert(out.diag.netRouted.some(net=>net.pins.includes('R1:2')&&net.pins.includes('R2:1')),'Impossible connection must not be silently removed from the rendered network');
    return {circuit,warnings:out.warnings,blockedWires:out.diag.wires.filter(w=>w.blocked),sourceUnchanged:true};
  });
  const directory=path.join(__dirname,'../evidence/upgrade/wire_layout');fs.mkdirSync(directory,{recursive:true});
  const filename=path.join(directory,'routing_contracts.json');const prior=fs.existsSync(filename)?JSON.parse(fs.readFileSync(filename,'utf8')):null;
  const report={timestamp:new Date().toISOString(),command:'node tests/test_render.js',status:results.every(r=>r.status==='pass')?'pass':'fail',denominator:results.length,passed:results.filter(r=>r.status==='pass').length,input_source:'Synthetic deterministic orthogonal geometry in tests/test_render.js; existing project templates remain covered above',license:'Project LICENSE (MIT)',data_split:'internal_geometry_contract_not_holdout',results,previousRuns:prior?[...(prior.previousRuns||[]),Object.fromEntries(Object.entries(prior).filter(([key])=>key!=='previousRuns'))]:[]};
  fs.writeFileSync(filename,JSON.stringify(report,null,2)+'\n');
  results.forEach(r=>console.log(r.status.toUpperCase()+' '+r.name+(r.error?' '+r.error:'')));
  assert.equal(report.status,'pass','Routing detour/crossing semantic contract RED; see evidence/upgrade/wire_layout/routing_contracts.json');
}
// ---------- 11. MCU signal placement is independent of rail encoding ----------
{
  const assert=require('node:assert/strict'),W=require('../web/wire_geometry.js');
  const source='data/pcb_active/run_e34bf9d7ac6c45be85d4fb00b97e1fec/circuit.json';
  const circuit=JSON.parse(fs.readFileSync(path.join(__dirname,'..',source),'utf8'));
  const signature=c=>W.netGroups(c.connections).map(g=>[...g.refs].sort().join('|')).sort();
  const originalJson=JSON.stringify(circuit),placed=L.autoLayout(circuit,S.sizeOf).parts;
  const star=JSON.parse(originalJson),railGroups=W.netGroups(circuit.connections).filter(g=>g.refs.includes('VCC1:1')||g.refs.includes('GND1:1'));
  const railConnections=new Set(railGroups.flatMap(g=>g.conns));star.connections=circuit.connections.filter((_,i)=>!railConnections.has(i));
  for(const net of railGroups){const anchor=net.refs.includes('VCC1:1')?'VCC1:1':'GND1:1';for(const ref of [...net.refs].sort())if(ref!==anchor)star.connections.push([anchor,ref]);}
  const starPlaced=L.autoLayout(star,S.sizeOf).parts;
  const normalize=parts=>{const anchor=parts.find(p=>p.id==='U1');return parts.filter(p=>['U1','U2','R1','R2','R3','J1','J2'].includes(p.id)).map(p=>({id:p.id,x:p.left-anchor.left,y:p.top-anchor.top})).sort((a,b)=>a.id.localeCompare(b.id));};
  const observations={chainCore:placed.filter(p=>['U1','U2'].includes(p.id)).map(p=>({id:p.id,left:p.left,top:p.top,...S.sizeOf(p.type)})),starCore:starPlaced.filter(p=>['U1','U2'].includes(p.id)).map(p=>({id:p.id,left:p.left,top:p.top,...S.sizeOf(p.type)})),chainSignalPlacement:normalize(placed),starSignalPlacement:normalize(starPlaced)};
  const results=[];function check(name,fn){try{fn();results.push({name,status:'pass'});}catch(error){results.push({name,status:'fail',error:error.message});}}
  check('MCU_and_connected_sensor_use_separate_signal_columns_without_source_mutation',()=>{
    assert.equal(JSON.stringify(circuit),originalJson,'Automatic placement must not rewrite source connections or component data');
    for(const input of circuit.parts){const actual=placed.find(p=>p.id===input.id);for(const key of Object.keys(input).filter(key=>!['left','top','rotate'].includes(key)))assert.deepEqual(actual[key],input[key],'Placement changed '+input.id+'.'+key);}
    const main=placed.find(p=>p.id==='U1'),sensor=placed.find(p=>p.id==='U2'),mainWidth=S.sizeOf(main.type).w,sensorWidth=S.sizeOf(sensor.type).w;
    assert(main.left+mainWidth<=sensor.left||sensor.left+sensorWidth<=main.left,'The signal-connected MCU and sensor should occupy distinct horizontal columns rather than one tall stack');
    for(let i=0;i<placed.length;i++)for(let j=i+1;j<placed.length;j++){const a=placed[i],b=placed[j],as=S.sizeOf(a.type),bs=S.sizeOf(b.type);assert(!boxesOverlap({x:a.left,y:a.top,w:as.w,h:as.h},{x:b.left,y:b.top,w:bs.w,h:bs.h}),'MCU placement overlaps '+a.id+' and '+b.id);}
  });
  check('equivalent_power_rail_chain_and_star_keep_the_same_relative_signal_layout',()=>{
    assert.deepEqual(signature(star),signature(circuit),'The alternate fixture must retain identical electrical networks');
    assert.deepEqual(normalize(starPlaced),normalize(placed),'Equivalent power/ground net encoding must not reorder or stretch the actual signal layout');
  });
  check('explicit_non_grid_MCU_keep_layout_and_rotations_bypass_auto_placement',()=>{
    const saved=JSON.parse(originalJson);saved.parts=placed.map(p=>({...p,left:p.left+1,top:p.top+3,rotate:p.id==='R1'?90:p.rotate||0}));
    const before=JSON.stringify(saved),out=R.renderCircuit(saved,{layout:'keep',collect:true});assert.equal(JSON.stringify(saved),before);
    for(const part of saved.parts){const b=out.diag.box[part.id],s=S.sizeOf(part.type);assert(Math.abs((b.x+b.w/2)-(part.left+s.w/2))<0.01&&Math.abs((b.y+b.h/2)-(part.top+s.h/2))<0.01,'Keep layout moved '+part.id);}
    assert.deepEqual(out.diag.pinPos['R1:2'],S.pinWorld('resistor','2',saved.parts.find(p=>p.id==='R1').left,saved.parts.find(p=>p.id==='R1').top,90));
  });
  const directory=path.join(__dirname,'../evidence/upgrade/wire_layout'),filename=path.join(directory,'layout_contracts.json');const prior=fs.existsSync(filename)?JSON.parse(fs.readFileSync(filename,'utf8')):null;
  const report={timestamp:new Date().toISOString(),command:'node tests/test_render.js',status:results.every(r=>r.status==='pass')?'pass':'fail',denominator:results.length,passed:results.filter(r=>r.status==='pass').length,input_source:source,alternate_input:'Same rail nets re-encoded as anchor-star; no changed electrical nets or component attributes',license:'Project LICENSE (MIT)',data_split:'internal_workflow_layout_not_holdout',observations,results,previousRuns:prior?[...(prior.previousRuns||[]),Object.fromEntries(Object.entries(prior).filter(([key])=>key!=='previousRuns'))]:[]};
  fs.writeFileSync(filename,JSON.stringify(report,null,2)+'\n');results.forEach(r=>console.log(r.status.toUpperCase()+' '+r.name+(r.error?' '+r.error:'')));assert.equal(report.status,'pass','MCU placement semantic RED; see layout_contracts.json');
}
console.log('\ntest_render.js 全部断言通过 ✔');
