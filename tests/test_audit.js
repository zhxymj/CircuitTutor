/* ============================================================
 * test_audit.js —— 画图结果准确性审计（三专家视角）
 * ------------------------------------------------------------
 * A. 图形算法：坐标/比例/尺寸/几何（引脚-导线贴合、元件重叠、
 *    导线穿盒、网格对齐、viewBox 覆盖、结点位置）
 * B. 数据可视化：颜色/标签/可读性（位号/值标签、标签遮挡、
 *    主题对比度 WCAG、标题栏统计、网络命名与 ERC 一致性）
 * C. 质量测试：数据映射/边界/确定性（parts→SVG、连接覆盖、
 *    ERC 引脚标注、畸形输入、重复渲染确定性）
 *
 * 用法：node test_audit.js [--json-out <报告路径>]
 * 输出：逐项断言 + findings 问题清单（severity）+ metrics 量化指标；
 *       P0/P1 或断言失败返回 1，命令行用法错误返回 2。
 * 边界输入的 SVG 另存 <tmp>/audit_boundary/ 供外部 XML 解析校验
 * ============================================================ */
'use strict';

const fs = require('fs');
const os = require('os').tmpdir();   // 系统真实临时目录（Node 下 '/tmp' 会被解析成当前盘符根）
const path = require('path');

const S = require('../web/symbols.js');
const W = require('../web/wire_geometry.js');
const R = require('../web/render.js');
const {
  AuditUsageError,
  buildAuditReport,
  connectedPinRefs,
  exitCodeForReport,
  indexRefNetworks,
  parseAuditArgs,
  writeAuditReport,
} = require('../tools/audit_contract.js');

let cli;
try {
  cli = parseAuditArgs(process.argv.slice(2));
} catch (error) {
  if (error instanceof AuditUsageError) {
    console.error(`用法错误：${error.message}`);
    process.exit(error.exitCode);
  }
  throw error;
}

const demos = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', 'circuit_templates.json'), 'utf-8'));
const ercAll = JSON.parse(fs.readFileSync(path.join(__dirname, 'erc_results.json'), 'utf-8'));

const ASSERTIONS = { pass: 0, fail: 0 };
function check(name, cond, detail) {
  if (cond) { ASSERTIONS.pass++; console.log(`  ✔ ${name}`); return true; }
  ASSERTIONS.fail++;
  console.log(`  ✘ ${name}${detail ? ' —— ' + detail : ''}`);
  return false;
}

// ---------- findings / metrics ----------
const findings = [];   // {sev, expert, code, where, detail}
const metrics = {};    // 量化指标（改进前后对比用）
function finding(sev, expert, code, where, detail) {
  findings.push({ sev, expert, code, where, detail });
  console.log(`  ▲ [${sev}] ${code} @${where}: ${detail}`);
}

// ---------- 几何工具（与 wire_geometry 同容差） ----------
const TOL = 1;
function distToSeg(p, a, b) {
  // 仅正交段（与渲染器几何一致）；返回点到线段距离或 null
  const horiz = Math.abs(a.y - b.y) < TOL, vert = Math.abs(a.x - b.x) < TOL;
  if (horiz) {
    if (p.x < Math.min(a.x, b.x) - TOL || p.x > Math.max(a.x, b.x) + TOL) return null;
    return Math.abs(p.y - a.y);
  }
  if (vert) {
    if (p.y < Math.min(a.y, b.y) - TOL || p.y > Math.max(a.y, b.y) + TOL) return null;
    return Math.abs(p.x - a.x);
  }
  return null;
}
function distToPolyline(p, pts) {
  let best = null;
  for (let i = 0; i < pts.length - 1; i++) {
    const d = distToSeg(p, pts[i], pts[i + 1]);
    if (d !== null && (best === null || d < best)) best = d;
  }
  return best;
}
function segHitsRect(a, b, r) {
  const horiz = Math.abs(a.y - b.y) < TOL, vert = Math.abs(a.x - b.x) < TOL;
  if (horiz) return a.y > r.y && a.y < r.y + r.height &&
    Math.max(a.x, b.x) > r.x && Math.min(a.x, b.x) < r.x + r.width;
  if (vert) return a.x > r.x && a.x < r.x + r.width &&
    Math.max(a.y, b.y) > r.y && Math.min(a.y, b.y) < r.y + r.height;
  return false;
}
function rectsIntersect(a, b) {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

// WCAG 相对亮度对比度
function lum(hex) {
  const c = hex.replace('#', '');
  const [r, g, b] = [0, 2, 4].map(i => {
    let v = parseInt(c.slice(i, i + 2), 16) / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrast(fg, bg) {
  const [l1, l2] = [lum(fg), lum(bg)].sort((a, b) => b - a);
  return (l1 + 0.05) / (l2 + 0.05);
}

// 复刻 erc_engine.CircuitGraph 的网络命名（按“最小成员名”排序赋 N1..），
// 用于校验前端网络标签与 ERC 报文中的网络名是否同源。
function ercNetNames(circuit) {
  const parent = {};
  const find = x => {
    parent[x] = parent[x] || x;
    let r = x;
    while (parent[r] !== r) r = parent[r];
    while (parent[x] !== r) { const nx = parent[x]; parent[x] = r; x = nx; }
    return r;
  };
  const refsOf = (circuit.connections || []).map(c => {
    const out = [];
    if (Array.isArray(c)) for (const s of c.slice(0, 2))
      if (typeof s === 'string' && s.includes(':')) out.push(s);
    return out;
  });
  const groups = {};
  for (const r of refsOf) {
    for (const ref of r) find(ref);
    for (let i = 1; i < r.length; i++) parent[find(r[i])] = find(r[0]);
  }
  for (const ref of Object.keys(parent)) {
    const root = find(ref);
    (groups[root] = groups[root] || []).push(ref);
  }
  const sorted = Object.values(groups).sort((a, b) =>
    a.slice().sort()[0] < b.slice().sort()[0] ? -1 : 1);
  const map = {};   // ref -> N名
  sorted.forEach((members, i) => members.forEach(ref => { map[ref] = `N${i + 1}`; }));
  return map;
}

// ---------- SVG 文本提取 ----------
// 元件 <g class="sym ..."> 依序相邻输出：按 data-part 定位起点、
// 下一个 sym 起点为界切片（不能用 `</g></g>` 定界——位号/值标签存在时
// 元件 g 的收尾是 </text></g>，正则定界会产生系统性误报）
function extractG(svg, id) {
  const key = `data-part="${id}"`;
  const start = svg.indexOf(key);
  if (start < 0) return null;
  const gStart = svg.lastIndexOf('<g class="sym', start);
  if (gStart < 0) return null;
  // 终点用 "sym sym-"（类型类）：前缀 "sym" 会误匹配内层 <g class="symbol-body">
  let next = svg.indexOf('<g class="sym sym-', start + key.length);
  return svg.slice(gStart, next < 0 ? svg.length : next);
}
function labelOf(gstr, cls) {
  if (!gstr) return null;
  const m = gstr.match(new RegExp(`<text[^>]*class="${cls}"[^>]*>([\\s\\S]*?)</text>`));
  return m ? m[1] : null;
}

// =====================================================================
const templateCount = Object.keys(demos).length;
const themes = ['kicad', 'print', 'blueprint'];
console.log(`== 画图准确性审计（${templateCount} 模板 × ${themes.length} 主题） ==\n`);
const boundaryDir = path.join(os, 'audit_boundary');
fs.mkdirSync(boundaryDir, { recursive: true });

let overlapPairs = 0, wireThroughBox = 0, labelClashes = 0, pinMaxDist = 0;
let pinUncovered = 0, viewViolations = 0, dotErrors = 0, netNameMismatch = 0;
let connectedRefBaseline = 0, ownNetworkChecks = 0;
const themeRatios = {};

for (const [name, circuit] of Object.entries(demos)) {
  const ercNetMap = ercNetNames(circuit);
  const connectedRefs = connectedPinRefs(circuit.connections);
  connectedRefBaseline += connectedRefs.size;
  for (const theme of themes) {
    const opts = { title: name, theme, erc: ercAll[name], collect: true };
    const res = R.renderCircuit(circuit, opts);
    const d = res.diag;
    const svg = res.svg;
    const tag = `${name}/${theme}`;

    // ---------- A1 引脚-导线贴合 ----------
    // 只审计连接表中出现的精确 pin ref，不能把“元件有任一脚接线”误当成
    // “该元件所有脚都接线”；且只允许所属网络的导线证明贴合。
    const refNetworks = indexRefNetworks(d);
    for (const ref of connectedRefs) {
      ownNetworkChecks++;
      const p = d.pinPos[ref];
      const ownNetworks = refNetworks.get(ref);
      let best = null;
      if (p && ownNetworks && ownNetworks.size) {
        for (const w of d.wires) {
          if (!ownNetworks.has(w.net)) continue;
          const dist = distToPolyline(p, w.points);
          if (dist !== null && (best === null || dist < best)) best = dist;
        }
      }
      if (best === null || best > 1.5) {
        pinUncovered++;
        finding('P1', 'A-图形算法', '引脚未贴合任何导线', tag,
          `${ref} 最近距离 ${best === null ? '∞(无正交段)' : best.toFixed(2)}px`);
      } else if (best > pinMaxDist) pinMaxDist = best;
    }

    // ---------- A2 元件包围盒重叠 ----------
    const ids = Object.keys(d.box);
    for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) {
      const bi = d.box[ids[i]], bj = d.box[ids[j]];
      if (rectsIntersect(bi, bj)) {
        overlapPairs++;
        finding('P1', 'A-图形算法', '元件包围盒重叠', tag,
          `${ids[i]} 与 ${ids[j]} 相交（bi=[${bi.x},${bi.y},${bi.w},${bi.h}] bj=[${bj.x},${bj.y},${bj.w},${bj.h}]）`);
      }
    }

    // ---------- A3 导线穿非自家元件盒 ----------
    for (const w of d.wires) {
      const ownIds = new Set([w.from, w.to].filter(Boolean).map(r => r.split(':')[0]));
      for (let i = 0; i < w.points.length - 1; i++) {
        const a = w.points[i], b = w.points[i + 1];
        for (const [pid, r] of Object.entries(d.box)) {
          if (ownIds.has(pid)) continue;   // 自家盒：引脚逃逸段属正常
          if (segHitsRect(a, b, r)) {
            wireThroughBox++;
            finding('P1', 'A-图形算法', '导线穿过元件', tag,
              `net${w.net} 段[${a.x},${a.y}→${b.x},${b.y}] 穿过 ${pid}`);
          }
        }
      }
    }

    // ---------- A4 网格对齐 + A5 viewBox 覆盖（auto 主题变体共通） ----------
    if (theme === 'kicad') {
      const gRe = /<g class="sym sym-[^"]*" data-part="([^"]*)"[^>]*transform="translate\(([-\d.]+),([-\d.]+)\)/g;
      let m;
      const placed = {};
      while ((m = gRe.exec(svg))) placed[m[1]] = { x: +m[2], y: +m[3] };
      const offGrid = Object.entries(placed).filter(([, v]) =>
        Math.round(v.x) % 10 !== 0 || Math.round(v.y) % 10 !== 0);
      if (offGrid.length) finding('P3', 'A-图形算法', '坐标未吸附10px网格', tag,
        offGrid.map(([id, v]) => `${id}@(${v.x},${v.y})`).join(', '));

      const inView = (x, y) => x >= d.view.minX - 1 && y >= d.view.minY - 1 &&
        x <= d.view.minX + d.view.width + 1 && y <= d.view.minY + d.view.height + 1;
      for (const [pid, r] of Object.entries(d.box)) {
        for (const [x, y] of [[r.x, r.y], [r.x + r.w, r.y + r.h]]) {
          if (!inView(x, y)) {
            viewViolations++;
            finding('P2', 'A-图形算法', '元件超出viewBox', tag, `${pid} 角点(${x},${y})`);
          }
        }
      }
      for (const w of d.wires) for (const p of w.points) {
        if (!inView(p.x, p.y)) {
          viewViolations++;
          finding('P2', 'A-图形算法', '导线点超出viewBox', tag, `net${w.net}@(${p.x},${p.y})`);
        }
      }
    }

    // ---------- A7 junction dot 位置应有 ≥2 条线汇聚 ----------
    for (const n of d.netRouted) {
      for (const dot of n.dots) {
        let hits = 0;
        for (const w of d.wires) {
          if (distToPolyline(dot, w.points) !== null) hits++;
        }
        if (hits < 2) {
          dotErrors++;
          finding('P2', 'A-图形算法', '结点缺少线段汇聚', tag,
            `${n.name} dot(${dot.x},${dot.y}) 仅 ${hits} 条线经过`);
        }
      }
    }

    // ---------- B3 标签遮挡（读 SVG 实况：text 定位 + 符号 translate） ----------
    {
      const gPos = {};
      const gRe2 = /<g class="sym sym-[^"]*" data-part="([^"]*)"[^>]*transform="translate\(([-\d.]+),([-\d.]+)\)/g;
      let gm2;
      while ((gm2 = gRe2.exec(svg))) gPos[gm2[1]] = { x: +gm2[2], y: +gm2[3] };
      const partsArr = circuit.parts || [];
      const grabText = (gstr, cls, fontPx) => {
        if (!gstr) return null;
        const m = gstr.match(
          new RegExp(`<text x="([-.\\d]+)" y="([-.\\d]+)"( text-anchor="([a-z]+)")? font-size="${fontPx}" class="${cls}"`));
        return m ? { x: +m[1], y: +m[2], anchor: m[4] || 'start' } : null;
      };
      const boxOf = (t, gxy, text, fontPx) => {
        const w = String(text).length * (fontPx === 13 ? 7.8 : 7.2);
        const x0 = t.anchor === 'middle' ? gxy.x + t.x - w / 2
          : t.anchor === 'end' ? gxy.x + t.x - w : gxy.x + t.x;
        return { x: x0, y: gxy.y + t.y - (fontPx - 2), w, h: fontPx + 1 };
      };
      const labelBoxes = [];   // {owner, what, box}
      for (const [pid, r] of Object.entries(d.box)) {
        const g = extractG(svg, pid);
        const refT = labelOf(g, 'ref-label');
        const valT = labelOf(g, 'val-label');
        if (refT == null) { finding('P2', 'B-可视化', '位号标签缺失', tag, pid); continue; }
        if (refT !== pid) {
          finding('P2', 'B-可视化', '位号标签与 id 不一致', tag, `${pid} 显示为 ${refT}`);
        }
        const p0 = partsArr.find(p => String(p.id) === pid) || {};
        const val = p0.attrs && p0.attrs.value;
        if (val != null && String(val) !== '' && valT !== String(val)) {
          finding('P2', 'B-可视化', '值标签与 attrs.value 不一致', tag, `${pid}: ${valT} ≠ ${val}`);
        }
        const gp = gPos[pid] || { x: r.x, y: r.y };
        const rb = grabText(g, 'ref-label', 13);
        if (rb) labelBoxes.push({ owner: pid, what: `ref(${pid})`, box: boxOf(rb, gp, refT, 13) });
        if (valT != null && val) {
          const vb = grabText(g, 'val-label', 12);
          if (vb) labelBoxes.push({ owner: pid, what: `val(${pid})`, box: boxOf(vb, gp, valT, 12) });
        }
      }
      for (const lb of labelBoxes) {
        const rc = lb.box;
        for (const [oid, or_] of Object.entries(d.box)) {
          if (oid === lb.owner) continue;
          if (rectsIntersect(rc, or_)) {
            labelClashes++;
            finding('P3', 'B-可视化', '标签压在其他元件上', tag, `${lb.what} 压 ${oid}`);
          }
        }
        for (const w of d.wires) {
          let hit = false;
          for (let i = 0; i < w.points.length - 1; i++) {
            if (segHitsRect(w.points[i], w.points[i + 1], rc)) { hit = true; break; }
          }
          if (hit) {
            labelClashes++;
            finding('P3', 'B-可视化', '导线穿过标签', tag, `net${w.net} 穿 ${lb.what}`);
            break;
          }
        }
        for (const ob of labelBoxes) {
          if (ob === lb || ob.owner === lb.owner) continue;
          if (rectsIntersect(rc, ob.box)) {
            labelClashes++;
            finding('P3', 'B-可视化', '标签互相重叠', tag, `${lb.what} ↔ ${ob.what}`);
          }
        }
      }
    }

    // ---------- B4 主题对比度 ----------
    const T = R.THEMES[theme];
    for (const [k, fg] of [['ref', T.ref], ['val', T.val], ['netLabel', T.netLabel], ['wire', T.wire]]) {
      const ratio = contrast(fg, T.bg);
      themeRatios[`${theme}.${k}`] = +ratio.toFixed(2);
      if (ratio < 3) finding('P2', 'B-可视化', '对比度不足', tag, `${k} ${fg} vs bg ${T.bg} = ${ratio.toFixed(2)}:1 (<3)`);
    }

    // ---------- B5 标题栏统计 ----------
    const tb = svg.match(/<text class="tb-sub" x="\d+" y="[^"]*">([^<]*)<\/text>\s*<text class="tb-sub"[^>]*text-anchor="end"/);
    if (tb) {
      const mm = tb[1].match(/(\d+) 元件 · (\d+) 网络 · (\d+) 导线/);
      if (mm) {
        const [nP, nN, nW] = [+mm[1], +mm[2], +mm[3]];
        const partsN = (circuit.parts || []).length;
        if (nP !== partsN || nN !== res.nets || nW !== d.wires.length) {
          finding('P2', 'B-可视化', '标题栏统计与实际不符', tag,
            `标 ${nP}/${nN}/${nW} vs 实际 ${partsN}/${res.nets}/${d.wires.length}`);
        }
      }
    }

    // ---------- B6 网络命名与 ERC 同源性 ----------
    for (const n of d.netRouted) {
      if (n.pins.length < 3) continue;   // 前端只给 ≥3 引脚网络标名
      const ercName = ercNetMap[n.pins[0]];
      if (ercName && ercName !== n.name) {
        netNameMismatch++;
        finding('P1', 'B-可视化', '网络名与ERC命名不一致', tag,
          `图上 ${n.name}（${n.pins.join(',')}）ERC 称 ${ercName}`);
      }
    }

    // ---------- C1 parts→data-part 完整 ----------
    for (const p of circuit.parts || []) {
      if (p.id != null && !svg.includes(`data-part="${p.id}"`)) {
        finding('P0', 'C-质量', '元件未渲染', tag, p.id);
      }
    }
    // ---------- C3 尺寸一致 ----------
    const rootM = svg.match(/<svg [^>]*width="(\d+)" height="(\d+)"/);
    if (!rootM || +rootM[1] !== res.width || +rootM[2] !== res.height) {
      finding('P2', 'C-质量', '返回尺寸与SVG根不一致', tag,
        `返回 ${res.width}x${res.height} vs 根 ${rootM && rootM[1]}x${rootM && rootM[2]}`);
    }
    // ---------- C4 ERC 引脚标注 ----------
    if (ercAll[name]) {
      for (const iss of ercAll[name].issues || []) {
        for (const ref of iss.pins || []) {
          const hasCircle = iss.severity === 'minor' || iss.severity === 'warning'
            ? true   // 图上仅 fatal/major 画红圈？实际按 severity 着色但都画
            : true;
          if (!hasCircle) continue;
          const expect = ref.includes(':') ? d.pinPos[ref] : (() => {
            const b = d.box[ref];
            return b ? { x: b.x + b.w / 2, y: b.y + b.h / 2 } : null;
          })();
          if (!expect) continue;   // 渲染器对无法定位的引脚跳过
          const cRe = new RegExp(`class="erc-circle" cx="([\\-\\d.]+)" cy="([\\-\\d.]+)"`);
          let found = false, m2;
          const re = new RegExp(`<circle class="erc-circle" cx="([\\-\\d.]+)" cy="([\\-\\d.]+)" r="15" stroke="[^"]*"/>`, 'g');
          while ((m2 = re.exec(svg))) {
            if (Math.abs(+m2[1] - expect.x) < 1.5 && Math.abs(+m2[2] - expect.y) < 1.5) { found = true; break; }
          }
          if (!found) {
            finding('P1', 'C-质量', 'ERC引脚未标注圆圈', tag,
              `${iss.rule} ${ref} 期望圆心(${expect.x},${expect.y})`);
          }
        }
      }
    }
    // ---------- C6 确定性 ----------
    const res2 = R.renderCircuit(circuit, opts);
    if (res2.svg !== svg) finding('P0', 'C-质量', '重复渲染不确定', tag, '两次输出不同');
  }
}

check('模板覆盖固定为 40 份', templateCount === 40, `实际 ${templateCount}`);
check('精确已连接引脚参考基线为 421', connectedRefBaseline === 421,
  `实际 ${connectedRefBaseline}`);
check('每个精确引脚仅按所属网络完成三主题检查',
  ownNetworkChecks === connectedRefBaseline * themes.length && pinUncovered === 0,
  `检查 ${ownNetworkChecks}/${connectedRefBaseline * themes.length}，未贴合 ${pinUncovered}`);

// ---------- C5 边界输入（渲染不抛异常 + SVG 落盘供 XML 校验） ----------
console.log('\n== C5 边界输入鲁棒性 ==');
const boundaryCases = {
  empty: { parts: [], connections: [] },
  no_parts_key: {},
  unknown_type: { parts: [{ type: 'alien_chip', id: 'X1' }], connections: [] },
  missing_id: { parts: [{ type: 'resistor' }], connections: [] },
  string_coords: { parts: [{ type: 'resistor', id: 'R1', left: '100', top: '50' }], connections: [] },
  nan_coords: { parts: [{ type: 'led', id: 'L1', left: NaN, top: Infinity }], connections: [] },
  dup_id: { parts: [{ type: 'resistor', id: 'R1' }, { type: 'led', id: 'R1' }], connections: [] },
  phantom_pin: { parts: [{ type: 'resistor', id: 'R1' }, { type: 'led', id: 'L1' }],
                 connections: [['R1:999', 'L1:+']] },
  huge_coords: { parts: [{ type: 'battery', id: 'B1', left: 1e6, top: -1e6 }], connections: [] },
  injection: { parts: [{ type: 'led', id: 'L<img src=x onerror=alert(1)>',
                        attrs: { value: '" onmouseover="alert(1)' } }], connections: [] },
  null_part: { parts: [null, { type: 'resistor', id: 'R1' }], connections: [['R1:1', null]] },
};
let boundaryOk = 0;
for (const [cn, circuit] of Object.entries(boundaryCases)) {
  try {
    const r = R.renderCircuit(circuit, { title: cn, theme: 'kicad', collect: true });
    if (!r.svg.startsWith('<svg') || !r.svg.endsWith('</svg>')) throw new Error('SVG 结构不完整');
    fs.writeFileSync(path.join(boundaryDir, `${cn}.svg`), r.svg, 'utf-8');
    boundaryOk++;
    check(`边界 ${cn}: 渲染成功且 SVG 结构完整`, true);
  } catch (e) {
    check(`边界 ${cn}: 渲染成功且 SVG 结构完整`, false, e.message);
    finding('P1', 'C-质量', '边界输入渲染异常', cn, e.message);
  }
}

// ---------- metrics 汇总 ----------
console.log('\n== 量化指标（改进前后对比用） ==');
metrics.pinMaxDist = +pinMaxDist.toFixed(2);
metrics.pinUncovered = pinUncovered;
metrics.overlapPairs = overlapPairs;
metrics.wireThroughBox = wireThroughBox;
metrics.labelClashes = labelClashes;
metrics.viewViolations = viewViolations;
metrics.dotErrors = dotErrors;
metrics.netNameMismatch = netNameMismatch;
metrics.themeContrast = themeRatios;
metrics.boundaryOk = boundaryOk;
metrics.templateCount = templateCount;
metrics.themeCount = themes.length;
metrics.connectedRefBaseline = connectedRefBaseline;
metrics.ownNetworkChecks = ownNetworkChecks;
metrics.findings = findings.length;
console.log(JSON.stringify(metrics, null, 1));

const report = buildAuditReport({
  assertions: ASSERTIONS,
  metrics,
  findings,
});
console.log(`\n审计完成：断言 ${report.assertions.pass} 项通过、${report.assertions.fail} 项失败；` +
  `findings ${findings.length} 条`, JSON.stringify(report.severity_counts));
console.log(`边界 SVG 已存：${boundaryDir}`);
const reportPath = writeAuditReport(report, cli.jsonOut || path.join(os, 'audit_report.json'));
console.log(`审计 JSON 已存：${reportPath}`);
process.exitCode = exitCodeForReport(report);
