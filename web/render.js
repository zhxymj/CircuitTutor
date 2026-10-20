/* ============================================================
 * render.js —— 智绘电路整图渲染器（原生 JS，零依赖）
 * ------------------------------------------------------------
 * 组装 symbols.js（符号）+ layout.js（自动布局）+ wire_geometry.js
 * （正交走线/结点/交叉），把一份 CircuitJSON 一次性渲染成
 * 工程级 SVG 原理图。渲染遵循工程 EDA 惯例：
 *  - 网络级走线、junction 实心点、导线交叉不相连时用半圆“跳线弧”，
 *    杜绝误读；
 *  - 经典 EDA 配色：深红符号 + 墨绿导线 + 点阵网格，
 *    并内置 print（黑白打印）与 blueprint（深底蓝图）主题；
 *  - 校验结果可视化：ERC 结果直接画到图上
 *    （错误引脚红圈 + 图例面板），错误定位一目了然。
 *
 * 用法（浏览器）：
 *   <script src="symbols.js"><script src="wire_geometry.js">
 *   <script src="layout.js"><script src="render.js">
 *   const {svg} = CircuitRender.renderCircuit(circuitJson, {title:'示例'});
 * 用法（Node 测试）：const R = require('../web/render.js');
 * ============================================================ */

(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) module.exports = factory(require);
  else root.CircuitRender = factory(null);
})(typeof self !== 'undefined' ? self : this, function (require) {
  'use strict';

  const S = (typeof CircuitSymbols !== 'undefined' && CircuitSymbols) ||
            (require && require('../web/symbols.js'));
  const W = (typeof WireGeometry !== 'undefined' && WireGeometry) ||
            (require && require('../web/wire_geometry.js'));
  const L = (typeof CircuitLayout !== 'undefined' && CircuitLayout) ||
            (require && require('../web/layout.js'));

  // ---------- 主题（kicad 档 = 经典 EDA 配色） ----------
  const THEMES = {
    kicad: {
      bg: '#ffffff', grid: '#d9d9d4', frame: '#8a0d0d',
      symbol: '#8a0d0d', wire: '#006e2e', junction: '#006e2e',
      netLabel: '#0055aa', ref: '#2b2b2b', val: '#77776f',
      error: '#cc2222', warn: '#e08e00', unknown: '#999990',
    },
    print: {
      bg: '#ffffff', grid: '#e8e8e8', frame: '#222222',
      symbol: '#111111', wire: '#333333', junction: '#333333',
      netLabel: '#333333', ref: '#111111', val: '#666666',
      error: '#cc2222', warn: '#e08e00', unknown: '#999999',
    },
    blueprint: {
      bg: '#0e2a47', grid: '#1d4266', frame: '#cfe3ff',
      symbol: '#cfe3ff', wire: '#7fd4ff', junction: '#7fd4ff',
      netLabel: '#ffd479', ref: '#e8f1ff', val: '#9db8d6',
      error: '#ff7b7b', warn: '#ffce6b', unknown: '#7a93b3',
    },
  };

  // 连接里声明的导线颜色名 -> 十六进制（opts.wireColors=true 时启用）
  const WIRE_COLORS = {
    red: '#c0392b', black: '#1f2937', blue: '#1d4ed8', green: '#047857',
    yellow: '#ca8a04', orange: '#ea580c', purple: '#7c3aed', white: '#f5f5f5',
  };

  // 绕行通道以 2px 细分；真实引脚坐标始终保留，包括手工非网格坐标。
  // 布局网格仍为 10px（layout.js），不同网络平行通道间距由 W.WIRE_SPACING 统一。
  const GRID = 2;
  const SEV_ORDER = { fatal: 0, major: 1, minor: 2, warning: 3 };
  const SEV_TEXT = { fatal: '致命', major: '严重', minor: '提示', warning: '警告' };
  // 电源/地类型集合（layout.js 同款）；模块级常量，避免每次渲染重建 Set
  const GND_TYPES = new Set(['gnd', 'ground', 'agnd']);
  const PWR_TYPES = new Set(['vcc', 'battery', 'battery_cell', 'power', 'vsupply']);

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // 任意输入 -> 有限数字（非法值回落 d）：LLM 给的 top/left/rotate 可能是
  // 字符串甚至注入片段，进几何计算/SVG 属性前必须归一化，否则轻则 NaN
  // 坐标画丢元件，重则 translate("0 onload=…") 属性注入
  function num(v, d) {
    const n = Number(v);
    return isFinite(n) ? n : d;
  }

  function pinSideDir(side, rotation) {
    const order=['R','D','L','U'];
    const original={left:2,right:0,top:3,bottom:1}[side];
    return original==null?null:order[(original+Math.round((rotation||0)/90)+4)%4];
  }

  /**
   * 渲染一份 CircuitJSON 为完整 SVG。
   * @param {Object} circuit {parts, connections}
   * @param {Object} opts
   *   title      图纸标题（默认“未命名电路”）
   *   theme      'kicad' | 'print' | 'blueprint'（默认 kicad）
   *   layout     'auto'（默认，自动布局）| 'keep'（沿用 LLM 给的 top/left）
   *   netLabels  true（默认）显示 N1/N2… 网络名（≥3 引脚的网络才标）
   *   wireColors true 时使用连接里声明的颜色，否则统一主题色
   *   erc        erc_engine.run_erc(...).to_dict() 的结果，用于图上标错
   * @returns {{svg:string, width:number, height:number, warnings:string[], nets:number}}
   * 交互层钩子：svg 根带 class="circuit-svg"（含 viewBox，便于缩放平移），
   * 每个元件 <g> 带 data-part="R1" data-type="resistor"（interact.js 依此定位元件）。
   */
  function renderCircuit(circuit, opts) {
    opts = opts || {};
    const theme = THEMES[opts.theme] || THEMES.kicad;
    const warnings = [];

    // ---------- 1. 布局 ----------
    let parts, W0, H0;
    if (opts.layout === 'keep') {
      parts = (circuit.parts || []).map(p => ({ rotate: 0, ...p }));
      W0 = 1000; H0 = 640;
    } else {
      const laid = L.autoLayout(circuit, S.sizeOf, opts.layoutOpts);
      parts = laid.parts; W0 = laid.width; H0 = laid.height;
    }

    // ---------- 1.5 输入净化 ----------
    // parts 可能直接来自 LLM 输出：坐标/角度一律强制有限数字（非法回落 0），
    // id/type 强制字符串。既防 NaN 把元件画丢，也防字符串片段注入
    // transform/data-* 等 SVG 属性（配合 symbols.js 的文本转义形成双保险）。
    parts = parts.map(p => ({
      ...p,
      id: p.id == null ? p.id : String(p.id),
      type: p.type == null ? p.type : String(p.type),
      left: num(p.left, 0),
      top: num(p.top, 0),
      rotate: num(p.rotate, 0),
    }));

    const conns = circuit.connections || [];

    // ---------- 2. 解析每个元件：包围盒 + 引脚世界坐标 ----------
    const byId = {};
    const box = {};    // id -> {x,y,w,h}（含旋转后的外接盒）
    const pinPos = {}; // "id:pin" -> {x,y}
    const pinDirs = {}; // "id:pin" -> {x,y,dir}（junction 计方向用）

    for (const p of parts) {
      if (p.id == null) { warnings.push('存在缺少 id 的元件，已跳过'); continue; }
      byId[p.id] = p;
      const def = S.LIB[p.type];
      if (!def) {
        warnings.push(`元件 ${p.id} 的类型 “${p.type}” 不在符号库中，以占位框渲染`);
        const w = 96, h = 56;
        p._unknown = true;
        box[p.id] = { x: p.left || 0, y: p.top || 0, w, h };
        continue;
      }
      const rot = (((p.rotate || 0) % 360) + 360) % 360;
      const swap = rot === 90 || rot === 270;
      const w = swap ? def.h : def.w, h = swap ? def.w : def.h;
      box[p.id] = {
        x: (p.left || 0) + (def.w - w) / 2,
        y: (p.top || 0) + (def.h - h) / 2,
        w, h,
      };
      for (const port of def.ports) {
        const ref = `${p.id}:${port.id}`;
        try {
          const wp = S.pinWorld(p.type, port.id, p.left || 0, p.top || 0, rot);
          pinPos[ref] = wp;
          pinDirs[ref] = { x: wp.x, y: wp.y, dir: pinSideDir(port.side, rot) };
        } catch (e) { /* pinWorld 不应失败，防御 */ }
      }
    }

    // 走线障碍 = 所有元件外接盒 + padding（带 id，便于给“自家盒”打标）
    const PAD = 6;
    const obstacles = Object.entries(box).map(([id, b]) => ({
      id, x: b.x - PAD, y: b.y - PAD, width: b.w + PAD * 2, height: b.h + PAD * 2,
    }));
    // “自家盒”只读克隆只做一次：原先每条导线 routePair 都把整个 obstacles
    // 数组重新 map + 克隆一遍；routeWire/countHits 全程只读障碍对象，
    // 复用这份 _own 变体与旧逻辑完全等价，省掉 O(导线数 × 障碍数) 次克隆。
    const ownVariants = new Map(obstacles.map(o => [o.id, { ...o, _own: true }]));

    // ---------- 3. 网络级走线 ----------
    // 两脚网络：引脚对直连；多脚网络：干线（trunk）汇流 —— 总线风格：电源网走顶部母线、地网走底部母线、一般网走中位干线，
    // 各引脚用短支线接入，T 型交汇自动生成 junction 实心点。
    // 网络命名与 erc_engine.CircuitGraph 同源：按“网络内最小引脚引用名”
    // 排序赋 N1/N2…（原先按网络出现顺序编号，与 ERC 报文里的 N 名对不上，
    // 审计实测 39 处不一致 —— 教学场景“网络 N2 短路”必须能在图上定位）。
    // Python sorted(key=min) 的码点序与 JS 码元比较对 ASCII 引用一致。
    const groups = W.netGroups(conns).slice().sort((a, b) => {
      const ma = a.refs.slice().sort()[0], mb = b.refs.slice().sort()[0];
      return ma < mb ? -1 : ma > mb ? 1 : 0;
    });
    const wires = [];        // {net, points, color}
    const netRouted = [];    // 每个网络 {name, points[], pins[]}
    let netSeq = 0;
    let reservedSegments = [];

    // id -> 小写类型（routeTrunk 逐引脚判断电源/地锚点用，预计算避免
    // 每次调用都 String() + toLowerCase()）
    const typeLc = {};
    for (const p of parts) if (p.id != null) typeLc[p.id] = String(p.type).toLowerCase();
    const partTypeOf = r => typeLc[r.split(':')[0]] || '';
    const median = arr => {
      const s = [...arr].sort((a, b) => a - b);
      const m = Math.floor(s.length / 2);
      return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
    };

    // 引脚 -> 指定落点的一段支线（own 豁免只作用于起点元件）
    function routePair(r0, landing, r1) {
      const a = pinPos[r0], b = landing || pinPos[r1];
      if (Math.abs(a.x - b.x) < 1 && Math.abs(a.y - b.y) < 1) return null;
      const ids = [r0.split(':')[0]];
      if (r1) ids.push(r1.split(':')[0]);
      const ownIds = new Set(ids);
      const wireObs = obstacles.map(o => ownIds.has(o.id) ? ownVariants.get(o.id) : o);
      const pts = W.routeWire(a, b, wireObs, GRID,
        pinDirs[r0] && pinDirs[r0].dir,
        r1 && pinDirs[r1] ? pinDirs[r1].dir : undefined, reservedSegments);
      return pts && pts.length > 1 ? pts : null;
    }

    for (const g of groups) {
      // 网络名与 erc_engine 一致：按“最小引脚引用名”排序编号 N1/N2…
      const pins = [...new Set(g.refs)].filter(r => pinPos[r]);
      const dropped = g.refs.filter(r => !pinPos[r]);
      dropped.forEach(r => warnings.push(`连接引用了无法定位的引脚 “${r}”（元件缺失或引脚不存在），该导线未绘制`));
      if (pins.length < 2) continue;
      netSeq += 1;
      reservedSegments=[];
      for(const wire of wires) for(let i=1;i<wire.points.length;i++) reservedSegments.push({a:wire.points[i-1],b:wire.points[i]});
      for(const net of netRouted) for(const dot of net.dots) reservedSegments.push({a:dot,b:dot,junction:true});
      const name = `N${netSeq}`;
      const connColorOf = (() => {
        if (!opts.wireColors) return () => theme.wire;
        // 组内 first-wins 映射，与原 “g.conns.find 取第一个匹配连接” 逐字等价：
        // 首个匹配连接即使颜色名无效也固定回落 theme.wire，不被后续连接覆盖。
        // 原实现每条导线都全量 find O(组内连接数)，干线支线多时是 O(n²)。
        const pairColor = new Map(), refColor = new Map();
        for (const ci of g.conns) {
          const c = conns[ci];
          if (!c) continue;
          const refs = c.slice(0, 2).filter(s => typeof s === 'string' && s.includes(':'));
          if (refs.length === 2) {
            const key = refs.slice().sort().join('|');
            if (!pairColor.has(key)) pairColor.set(key, WIRE_COLORS[c[2]]);
          }
          for (const s of refs) {
            if (!refColor.has(s)) refColor.set(s, WIRE_COLORS[c[2]]);
          }
        }
        return (r0, r1) => r1
          ? (pairColor.get([r0, r1].sort().join('|')) || theme.wire)
          : (refColor.get(r0) || theme.wire);
      })();

      const routed = [];
      const emit = (pts, r0, r1) => {
        if (!pts) return;
        if (pts.blocked) warnings.push('连接 '+r0+(r1?' → '+r1:' 至网络干线')+' 无法完全避开元件或异网导线，请调整阻塞位置；问题导线以虚线保留。');
        wires.push({ net: netSeq, points: pts, color: connColorOf(r0, r1), blocked: !!pts.blocked, from: r0, to: r1 });
        routed.push(pts);
      };

      let polylines = null;
      if (pins.length === 2) {
        polylines = [[routePair(pins[0], null, pins[1]), pins[0], pins[1]]];
      } else {
        polylines = routeTrunk(pins);
      }
      if (!polylines) polylines = routeChain(pins);   // 干线放不下时退回最近邻链

      for (const [pts, r0, r1] of polylines) emit(pts, r0, r1);
      const inward={L:'R',R:'L',U:'D',D:'U'};
      const netPinDirs={};
      for(const ref of pins) netPinDirs[ref]={...pinDirs[ref],dir:inward[pinDirs[ref].dir]};
      const dots = W.junctionDots(routed, netPinDirs);
      netRouted.push({ name, points: routed.reduce((a, ps) => a.concat(ps), []), pins, dots });
    }

    // 多脚网络干线走线：返回 [[折线, 起引脚, 止引脚], ...]；放不下返回 null
    function routeTrunk(pins) {
      const xs=pins.map(r=>pinPos[r].x),ys=pins.map(r=>pinPos[r].y);
      const gndAnchor=pins.find(r=>GND_TYPES.has(partTypeOf(r)))||null;
      const pwrAnchor=!gndAnchor?(pins.find(r=>PWR_TYPES.has(partTypeOf(r)))||null):null;
      const anchor=gndAnchor||pwrAnchor;
      const axis=anchor||Math.max(...xs)-Math.min(...xs)>=Math.max(...ys)-Math.min(...ys)?'h':'v';
      const base=anchor?pinPos[anchor][axis==='h'?'y':'x']:median(axis==='h'?ys:xs);
      // Side-facing pins join the bus at their outward lead, instead of
      // stepping out and then immediately returning to their original axis.
      const lead=PAD+GRID;
      const projections=pins.map(ref=>{
        const p=pinPos[ref],dir=pinDirs[ref].dir;
        return axis==='h'?p.x+(dir==='L'?-lead:dir==='R'?lead:0):
          p.y+(dir==='U'?-lead:dir==='D'?lead:0);
      });
      let lo,hi,pos=null,landings=[];
      const candidates=new Set([base]);
      for(let i=1;i<=10;i++) {candidates.add(base+i*W.WIRE_SPACING);candidates.add(base-i*W.WIRE_SPACING);}
      for(const obstacle of obstacles) {
        const v=axis==='h'?obstacle.y:obstacle.x,extent=axis==='h'?obstacle.height:obstacle.width;
        candidates.add(v-W.WIRE_SPACING);candidates.add(v+extent+W.WIRE_SPACING);
      }
      for(const candidate of [...candidates].sort((a,b)=>Math.abs(a-base)-Math.abs(b-base)||b-a)) {
        const proposed=[];
        for(let k=0;k<pins.length;k++) {
          const ref=pins[k],p=pinPos[ref],dir=pinDirs[ref].dir;
          const sign=axis==='h'?(dir==='L'?-1:1):(dir==='U'?-1:1);
          const offsets=[0];for(let step=1;step<=8;step++)offsets.push(sign*step*W.WIRE_SPACING,-sign*step*W.WIRE_SPACING);
          let landing=null;
          for(const offset of offsets) {
            const q=axis==='h'?{x:projections[k]+offset,y:candidate}:{x:candidate,y:projections[k]+offset};
            if(W.pointOnReserved(q,reservedSegments))continue;
            if(obstacles.some(r=>q.x>r.x&&q.x<r.x+r.width&&q.y>r.y&&q.y<r.y+r.height))continue;
            landing=q;break;
          }
          if(!landing)break;proposed.push(landing);
        }
        if(proposed.length!==pins.length)continue;
        const low=Math.min(...proposed.map(p=>p[axis==='h'?'x':'y'])),high=Math.max(...proposed.map(p=>p[axis==='h'?'x':'y']));
        const a=axis==='h'?{x:low,y:candidate}:{x:candidate,y:low};
        const b=axis==='h'?{x:high,y:candidate}:{x:candidate,y:high};
        if(W.pathHitsObstacle([a,b],obstacles)||W.pathConflictsReserved([a,b],reservedSegments))continue;
        pos=candidate;lo=low;hi=high;landings=proposed;break;
      }
      if(pos==null)return null;
      const branches=[],joins=[];
      for(let k=0;k<pins.length;k++) {
        const ref=pins[k],p=pinPos[ref];
        if(Math.abs(p[axis==='h'?'y':'x']-pos)<1e-7&&p[axis==='h'?'x':'y']>=lo&&p[axis==='h'?'x':'y']<=hi) {joins.push(axis==='h'?p.x:p.y);continue;}
        const landing=landings[k];
        const points=routePair(ref,landing);
        if(!points)continue;
        // End a branch at its first real contact with this bus. Keeping the
        // old projected landing after an earlier contact creates a needless loop.
        let clipped=points;
        for(let i=1;i<points.length;i++) {
          const a=points[i-1],b=points[i];
          let cross=null;
          if(axis==='h'&&Math.abs(a.x-b.x)<1e-7&&pos>=Math.min(a.y,b.y)&&pos<=Math.max(a.y,b.y))cross={x:a.x,y:pos};
          if(axis==='v'&&Math.abs(a.y-b.y)<1e-7&&pos>=Math.min(a.x,b.x)&&pos<=Math.max(a.x,b.x))cross={x:pos,y:a.y};
          if(!cross||W.pointOnReserved(cross,reservedSegments))continue;
          clipped=points.slice(0,i);
          if(Math.hypot(clipped[clipped.length-1].x-cross.x,clipped[clipped.length-1].y-cross.y)>1e-7)clipped.push(cross);
          if(points.blocked)clipped.blocked=true;
          break;
        }
        const last=clipped[clipped.length-1];joins.push(last[axis==='h'?'x':'y']);
        branches.push([clipped,ref,null]);
      }
      if(!joins.length)return null;
      const first=Math.min(...joins),last=Math.max(...joins);
      if(last-first>1e-7) {
        const trunk=axis==='h'?[{x:first,y:pos},{x:last,y:pos}]:[{x:pos,y:first},{x:pos,y:last}];
        // Detours can reach the bus outside its original projection interval.
        // Use those actual first contacts, provided the final bus is still clear.
        // Otherwise the existing chain fallback preserves the full network.
        if(W.pathHitsObstacle(trunk,obstacles)||W.pathConflictsReserved(trunk,reservedSegments))return null;
        branches.unshift([trunk,pins[0],null]);
      }
      return branches;
    }

    // 最近邻链走线（干线失败时的兜底）
    function routeChain(pins) {
      const remaining = [...pins].sort((a, b) => pinPos[a].x - pinPos[b].x || pinPos[a].y - pinPos[b].y);
      const chain = [remaining.shift()];
      while (remaining.length) {
        const cur = pinPos[chain[chain.length - 1]];
        let best = 0, bestD = Infinity;
        remaining.forEach((r, i) => {
          const q = pinPos[r];
          const d = Math.abs(q.x - cur.x) + Math.abs(q.y - cur.y);
          if (d < bestD) { bestD = d; best = i; }
        });
        chain.push(remaining.splice(best, 1)[0]);
      }
      const out = [];
      for (let i = 0; i < chain.length - 1; i++) {
        out.push([routePair(chain[i], null, chain[i + 1]), chain[i], chain[i + 1]]);
      }
      return out;
    }

    // ---------- 4. 跨网络交叉点 -> 跳线弧 ----------
    const crossings = W.wireCrossings(wires, new Map(netRouted.map((net,i)=>[i+1,net.dots])));

    // ---------- 5. 计算总包围盒（元件 + 走线 + 标题栏） ----------
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    const bump = (x, y) => {
      if (!isFinite(x) || !isFinite(y)) return;
      minX = Math.min(minX, x); minY = Math.min(minY, y);
      maxX = Math.max(maxX, x); maxY = Math.max(maxY, y);
    };
    Object.values(box).forEach(b => { bump(b.x, b.y); bump(b.x + b.w, b.y + b.h); });
    wires.forEach(w => w.points.forEach(p => bump(p.x, p.y)));
    if (!isFinite(minX)) { minX = 0; minY = 0; maxX = 400; maxY = 300; }

    const M = 24;                       // 图内边距
    const TITLE_W = 320, TITLE_H = 96;  // 标题栏
    const ERC_W = 340;                  // ERC 图例宽
    const erc = opts.erc || null;
    const ercIssues = (erc && Array.isArray(erc.issues) ? erc.issues : [])
      .slice().sort((a, b) => (SEV_ORDER[a.severity] != null ? SEV_ORDER[a.severity] : 9)
                           - (SEV_ORDER[b.severity] != null ? SEV_ORDER[b.severity] : 9));
    const legendH = ercIssues.length ? Math.min(ercIssues.length, 6) * 22 + 34 : 0;

    // 网络颜色图例（opt-in：opts.legend 且开启 wireColors，且实际用到 >1 种
    // 颜色才有信息量；默认关闭 → 既有渲染输出逐字节不变）
    const legendItems = [];
    if (opts.legend && opts.wireColors) {
      const byColor = new Map();          // color -> Set(netName)
      for (const w of wires) {
        if (!byColor.has(w.color)) byColor.set(w.color, new Set());
        byColor.get(w.color).add((netRouted[w.net - 1] || {}).name || `N${w.net}`);
      }
      if (byColor.size > 1) {
        for (const [color, nets] of byColor) {
          legendItems.push({ color, nets: [...nets].sort() });
        }
      }
    }
    const LEGEND_H = legendItems.length ? 30 + legendItems.length * 20 : 0;
    const LEGEND_W = 168;

    minX -= M; minY -= M;
    maxX += M + TITLE_W;
    maxY += Math.max(M + TITLE_H, legendH ? M + legendH : 0,
                     LEGEND_H ? M + LEGEND_H + TITLE_H + 8 : 0);
    const width = Math.max(W0, maxX - minX), height = Math.max(H0, maxY - minY);

    // ---------- 6. 逐层生成 SVG ----------
    const s = [];
    // svg 根：class="circuit-svg" + viewBox 是交互层（interact.js）缩放/平移的挂载钩子
    s.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${Math.round(width)}" height="${Math.round(height)}" ` +
      `viewBox="${Math.round(minX)} ${Math.round(minY)} ${Math.round(width)} ${Math.round(height)}" ` +
      `class="circuit-svg" font-family="'Segoe UI','Microsoft YaHei',system-ui,sans-serif">`);

    // 样式：符号描边宽 2，导线 2.2，网格点阵
    s.push(`<style>
.ref-label{font-size:13px;font-weight:600;fill:${theme.ref}}
.val-label{font-size:12px;fill:${theme.val}}
.net-label{font-size:11px;font-style:italic;fill:${theme.netLabel}}
.wire{fill:none;stroke-width:2.2;stroke-linejoin:round}
.erc-circle{fill:none;stroke-width:1.8;stroke-dasharray:4 3}
.erc-badge{font-size:10px;font-weight:700;fill:#fff}
.tb-text{font-size:12px;fill:${theme.ref}}
.tb-title{font-size:15px;font-weight:700;fill:${theme.ref}}
.tb-sub{font-size:11px;fill:${theme.val}}
</style>`);

    // 背景 + 点阵网格
    s.push(`<rect x="${minX}" y="${minY}" width="${width}" height="${height}" fill="${theme.bg}"/>`);
    s.push(`<defs><pattern id="dotgrid" width="20" height="20" patternUnits="userSpaceOnUse">` +
      `<circle cx="1" cy="1" r="1" fill="${theme.grid}"/></pattern></defs>`);
    s.push(`<rect x="${minX}" y="${minY}" width="${width}" height="${height}" fill="url(#dotgrid)"/>`);

    // 6.1 导线（先画线，再画符号，符号盖住穿帮段）
    for (const w of wires) {
      s.push(`<path class="wire" stroke="${w.blocked ? theme.symbol : w.color}"${w.blocked ? ' stroke-dasharray="6 4" data-route-status="blocked"' : ''} d="${W.pathWithHops(w.points, crossings, 6, w.net)}"/>`);
    }
    // 6.2 junction 实心点
    for (const n of netRouted) {
      for (const d of n.dots) {
        s.push(`<circle cx="${Math.round(d.x)}" cy="${Math.round(d.y)}" r="4" fill="${theme.junction}"/>`);
      }
    }
    // 6.3 网络名标签（≥3 引脚的网络才有教学价值）
    // 避让：在若干候选锚点（各 junction / 首末引脚）上试多个偏移，
    // 选第一个不与「已放网络标 / 元件盒 / 画布边」相交的位置；
    // 全冲突则退回首个 junction 的默认偏移（与旧行为一致，保证可复现）。
    if (opts.netLabels !== false) {
      const netLabelRects = [];
      function netRectFree(rc) {
        if (rc.x < minX || rc.y < minY ||
            rc.x + rc.w > minX + width || rc.y + rc.h > minY + height) return false;
        for (const q of netLabelRects) {
          if (rc.x < q.x + q.w && q.x < rc.x + rc.w &&
              rc.y < q.y + q.h && q.y < rc.y + rc.h) return false;
        }
        for (const b of Object.values(box)) {
          if (rc.x < b.x + b.w && b.x < rc.x + rc.w &&
              rc.y < b.y + b.h && b.y < rc.y + rc.h) return false;
        }
        return true;
      }
      for (const n of netRouted) {
        if (n.pins.length < 3) continue;
        const anchors = [];
        if (n.dots.length) anchors.push(n.dots[0]);
        for (const ref of n.pins) {
          if (pinPos[ref]) anchors.push(pinPos[ref]);
        }
        const name = n.name || '';
        const lw = name.length * 7.0 + 4;  // 11px 斜体近似宽
        const offsets = [
          { dx: 6, dy: -7, anchor: 'start' },
          { dx: -6, dy: -7, anchor: 'end' },
          { dx: 6, dy: 14, anchor: 'start' },
          { dx: -6, dy: 14, anchor: 'end' },
          { dx: 0, dy: -12, anchor: 'middle' },
        ];
        let placed = null;
        for (const a of anchors) {
          for (const o of offsets) {
            const tx = Math.round(a.x) + o.dx;
            const ty = Math.round(a.y) + o.dy;
            const rc = o.anchor === 'end'
              ? { x: tx - lw, y: ty - 11, w: lw, h: 14 }
              : o.anchor === 'middle'
                ? { x: tx - lw / 2, y: ty - 11, w: lw, h: 14 }
                : { x: tx, y: ty - 11, w: lw, h: 14 };
            if (netRectFree(rc)) {
              placed = { x: tx, y: ty, anchor: o.anchor, rc: rc };
              break;
            }
          }
          if (placed) break;
        }
        if (!placed) {
          const p0 = n.dots.length ? n.dots[0] : pinPos[n.pins[0]];
          placed = {
            x: Math.round(p0.x) + 6, y: Math.round(p0.y) - 7,
            anchor: 'start',
            rc: { x: Math.round(p0.x) + 6, y: Math.round(p0.y) - 18, w: lw, h: 14 },
          };
        }
        netLabelRects.push(placed.rc);
        const anchorAttr = placed.anchor && placed.anchor !== 'start'
          ? ` text-anchor="${placed.anchor}"` : '';
        s.push(`<text class="net-label" x="${placed.x}" y="${placed.y}"${anchorAttr}>${esc(name)}</text>`);
      }
    }
    // ---------- 6.4a 标签避让规划（图形算法） ----------
    // 每个位号/值标签在固定候选位（居中 → 右侧 → 左侧）里选第一个
    // 与“任何导线段 / 其他元件盒 / 已放置标签 / 画布边界”都不相交的
    // 位置；全部冲突则保留居中默认。启发式摆位（如一律侧放）会被相邻
    // 走线通道反杀（审计实测），基于实际走线几何的避让才通用。
    // 旋转元件的标签随符号一起旋转，世界坐标避让不成立，保持默认。
    const placedLabels = [];
    function rectHitsWires(rc) {
      for (const w of wires) {
        for (let i = 0; i < w.points.length - 1; i++) {
          if (W.segHitsRect(w.points[i], w.points[i + 1], rc)) return true;
        }
      }
      return false;
    }
    function rectFree(rc, selfId) {
      if (rc.x < minX || rc.y < minY ||
          rc.x + rc.w > minX + width || rc.y + rc.h > minY + height) return false;
      if (rectHitsWires(rc)) return false;
      for (const q of placedLabels) {
        if (rc.x < q.x + q.w && q.x < rc.x + rc.w &&
            rc.y < q.y + q.h && q.y < rc.y + rc.h) return false;
      }
      for (const [pid, b] of Object.entries(box)) {
        if (pid === selfId) continue;
        if (rc.x < b.x + b.w && b.x < rc.x + rc.w &&
            rc.y < b.y + b.h && b.y < rc.y + rc.h) return false;
      }
      return true;
    }
    function planLabelPos(p, r, selfId, refText, valText) {
      if (!r || ((p.rotate || 0) % 360 !== 0)) return null;
      const out = {};
      if (refText) {
        const l = String(refText).length * 7.8;   // 13px 字号近似宽
        const hit = [
          { a: { x: r.w / 2, y: -6, anchor: 'middle' }, b: { x: r.x + r.w / 2 - l / 2, y: r.y - 16, w: l, h: 14 } },
          { a: { x: r.w + 6, y: r.h / 2 + 4, anchor: 'start' }, b: { x: r.x + r.w + 6, y: r.y + r.h / 2 - 7, w: l, h: 14 } },
          { a: { x: -6, y: r.h / 2 + 4, anchor: 'end' }, b: { x: r.x - 6 - l, y: r.y + r.h / 2 - 7, w: l, h: 14 } },
        ].find(c => rectFree(c.b, selfId));
        if (hit) { out.ref = hit.a; placedLabels.push(hit.b); }
      }
      if (valText != null && valText !== '') {
        const lv = String(valText).length * 7.2;  // 12px 字号近似宽
        const hit = [
          { a: { x: r.w / 2, y: r.h + 14, anchor: 'middle' }, b: { x: r.x + r.w / 2 - lv / 2, y: r.y + r.h + 3, w: lv, h: 15 } },
          { a: { x: r.w + 6, y: r.h / 2 + 20, anchor: 'start' }, b: { x: r.x + r.w + 6, y: r.y + r.h / 2 + 8, w: lv, h: 15 } },
          { a: { x: -6, y: r.h / 2 + 20, anchor: 'end' }, b: { x: r.x - 6 - lv, y: r.y + r.h / 2 + 8, w: lv, h: 15 } },
        ].find(c => rectFree(c.b, selfId));
        if (hit) { out.val = hit.a; placedLabels.push(hit.b); }
      }
      return (out.ref || out.val) ? out : null;
    }

    // 6.4 元件符号（data-part/data-type 供交互层定位元件）
    for (const p of parts) {
      if (p.id == null) continue;
      if (p._unknown) {
        const b = box[p.id];
        s.push(`<g data-part="${esc(p.id)}" data-type="${esc(p.type)}" transform="translate(${b.x},${b.y})">` +
          `<rect width="${b.w}" height="${b.h}" fill="none" stroke="${theme.unknown}" stroke-width="2" stroke-dasharray="6 4" rx="6"/>` +
          `<text x="${b.w / 2}" y="${b.h / 2 - 4}" text-anchor="middle" font-size="12" fill="${theme.unknown}">${esc(p.type)}</text>` +
          `<text x="${b.w / 2}" y="${b.h / 2 + 14}" text-anchor="middle" font-size="13" font-weight="600" fill="${theme.ref}">${esc(p.id)}</text>` +
          `</g>`);
        continue;
      }
      const lp = planLabelPos(p, box[p.id], p.id, p.id, p.attrs && p.attrs.value);
      s.push(S.renderSymbol(p.type, p.left || 0, p.top || 0, p.rotate || 0,
        p.id, p.attrs && p.attrs.value, { part: p.id, type: p.type }, lp));
    }

    // 6.5 ERC 错误标记：错误引脚画虚线圆圈
    if (ercIssues.length) {
      const seen = new Set();
      for (const iss of ercIssues) {
        for (const ref of (iss.pins || [])) {
          const key = `${iss.severity}:${ref}`;
          if (seen.has(key)) continue;
          seen.add(key);
          let c = null;
          if (ref.includes(':') && pinPos[ref]) c = pinPos[ref];
          else if (byId[ref] && box[ref]) c = { x: box[ref].x + box[ref].w / 2, y: box[ref].y + box[ref].h / 2 };
          if (!c) continue;
          const color = iss.severity === 'fatal' || iss.severity === 'major' ? theme.error : theme.warn;
          s.push(`<circle class="erc-circle" cx="${Math.round(c.x)}" cy="${Math.round(c.y)}" r="15" stroke="${color}"/>`);
        }
      }
    }

    // 6.6 ERC 图例面板（左下）
    if (ercIssues.length) {
      const lx = minX + M, ly = maxY - legendH + 8;
      s.push(`<g>`);
      s.push(`<rect x="${lx}" y="${ly}" width="${ERC_W}" height="${legendH - 16}" fill="${theme.bg}" ` +
        `fill-opacity="0.92" stroke="${theme.frame}" stroke-width="1.2" rx="4"/>`);
      s.push(`<text x="${lx + 12}" y="${ly + 20}" font-size="12" font-weight="700" fill="${theme.error}">` +
        `电气规则检查（ERC）：${ercIssues.length} 项问题</text>`);
      ercIssues.slice(0, 6).forEach((iss, i) => {
        const yy = ly + 40 + i * 22;
        const color = iss.severity === 'fatal' || iss.severity === 'major' ? theme.error : theme.warn;
        const badge = `<rect x="${lx + 12}" y="${yy - 11}" width="34" height="15" rx="3" fill="${color}"/>` +
          `<text class="erc-badge" x="${lx + 29}" y="${yy}" text-anchor="middle">${SEV_TEXT[iss.severity] || iss.severity}</text>`;
        const msg = `${iss.rule || ''}：${iss.message || ''}`;
        s.push(badge + `<text class="tb-sub" x="${lx + 54}" y="${yy}">${esc(msg.length > 40 ? msg.slice(0, 40) + '…' : msg)}</text>`);
      });
      if (ercIssues.length > 6) {
        s.push(`<text class="tb-sub" x="${lx + 12}" y="${ly + 40 + 6 * 22}">… 还有 ${ercIssues.length - 6} 项</text>`);
      }
      s.push(`</g>`);
    }

    // 6.65 网络颜色图例（右下，标题栏正上方：与图框同风格，右对齐）
    if (LEGEND_H) {
      const gx = maxX - LEGEND_W, gy = maxY - TITLE_H - LEGEND_H - 8;
      s.push(`<g>`);
      s.push(`<rect x="${gx}" y="${gy}" width="${LEGEND_W}" height="${LEGEND_H}" fill="${theme.bg}" ` +
        `fill-opacity="0.92" stroke="${theme.frame}" stroke-width="1.2" rx="4"/>`);
      s.push(`<text class="tb-sub" x="${gx + 12}" y="${gy + 19}" font-weight="700">网络颜色</text>`);
      legendItems.forEach((it, i) => {
        const yy = gy + 38 + i * 20;
        const nets = it.nets.slice(0, 3).join('、') + (it.nets.length > 3 ? ` 等${it.nets.length}个` : '');
        s.push(`<line x1="${gx + 12}" y1="${yy - 4}" x2="${gx + 40}" y2="${yy - 4}" ` +
          `stroke="${it.color}" stroke-width="3" stroke-linecap="round"/>`);
        s.push(`<text class="tb-sub" x="${gx + 48}" y="${yy}">${esc(nets)}</text>`);
      });
      s.push(`</g>`);
    }

    // 6.7 标题栏（右下，工程图纸图框样式）
    {
      const tx = maxX - TITLE_W, ty = maxY - TITLE_H;
      const date = new Date().toISOString().slice(0, 10);
      const ercText = !erc ? 'ERC：未运行'
        : (erc.passed && !ercIssues.length) ? 'ERC：✔ 通过'
        : `ERC：${ercIssues.filter(i => i.severity === 'fatal' || i.severity === 'major').length} 项错误`;
      s.push(`<g>`);
      s.push(`<rect x="${tx}" y="${ty}" width="${TITLE_W}" height="${TITLE_H}" fill="${theme.bg}" ` +
        `fill-opacity="0.92" stroke="${theme.frame}" stroke-width="1.4"/>`);
      s.push(`<line x1="${tx}" y1="${ty + 30}" x2="${tx + TITLE_W}" y2="${ty + 30}" stroke="${theme.frame}" stroke-width="1"/>`);
      s.push(`<line x1="${tx}" y1="${ty + 64}" x2="${tx + TITLE_W}" y2="${ty + 64}" stroke="${theme.frame}" stroke-width="0.8"/>`);
      s.push(`<text class="tb-title" x="${tx + 12}" y="${ty + 21}">智绘电路 CircuitTutor</text>`);
      s.push(`<text class="tb-text" x="${tx + 12}" y="${ty + 50}">${esc(opts.title || '未命名电路')}</text>`);
      s.push(`<text class="tb-sub" x="${tx + 12}" y="${ty + 82}">${date} · ${parts.length} 元件 · ${netSeq} 网络 · ${wires.length} 导线</text>`);
      const passColor = erc && erc.passed && !ercIssues.length ? theme.wire : (erc ? theme.error : theme.val);
      s.push(`<text class="tb-sub" x="${tx + TITLE_W - 12}" y="${ty + 82}" text-anchor="end" fill="${passColor}">${ercText}</text>`);
      s.push(`</g>`);
    }

    s.push(`</svg>`);
    const result = {
      svg: s.join('\n'),
      width: Math.round(width),
      height: Math.round(height),
      warnings,
      nets: netSeq,
    };
    // 诊断数据收集钩子（默认关闭，SVG 输出逐字节不变）：
    // 供准确性审计（test_audit.js）核对引脚坐标 / 导线折线 / 包围盒 /
    // 网络命名与 ERC 的对应关系；collect=true 时附带返回内部几何。
    if (opts.collect) {
      result.diag = {
        box, pinPos, pinDirs, wires, crossings, netRouted,
        typeLc, view: { minX, minY, width, height },
      };
    }
    return result;
  }

  return { renderCircuit, THEMES };
});
