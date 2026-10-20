/* ============================================================
 * layout.js —— 智绘电路自动布局引擎（原生 JS，零依赖）
 * ------------------------------------------------------------
 * 解决的问题：过去电路图坐标完全依赖 LLM 手填 top/left，
 * 元件重叠、连线绕远几乎必然发生。确定性 EDA 工具从不信任
 * LLM 的坐标：分层布局 + 网格吸附是工程界成熟做法。本文件按
 * 同样思想实现确定性自动布局：
 *
 *  1) 分层（layered 布局）：
 *     电源/电池不占信号层，直接挂顶部电源轨；
 *     地（gnd）挂底部地轨；其余元件按“离电源的 BFS 距离”分层，
 *     信号自然从左向右流动，与教科书读图方向一致。
 *  2) 层内排序（重心法降交叉）：
 *     用相邻层邻居的平均序号重排本层元件，左右各扫一遍。
 *  3) 网格对齐（gridSize 吸附）：
 *     列宽取本层最宽元件，行距取本层最高元件 + 通道，
 *     全部坐标吸附到 10px 网格，引脚天然落在格点上。
 *
 * 输入：CircuitJSON {parts, connections}（与 deepseek_prompt.py 契约一致）
 * 输出：{parts, width, height} —— parts 为带 top/left/rotate:0 的副本
 * ============================================================ */

(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) module.exports = factory();
  else root.CircuitLayout = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const W = typeof WireGeometry !== 'undefined' ? WireGeometry : require('./wire_geometry.js');
  const S = typeof CircuitSymbols !== 'undefined' ? CircuitSymbols : require('./symbols.js');
  const GRID = 10;
  const MCU_TYPES = new Set(['atmega328', 'esp32']);
  const POWER_TYPES = new Set(['vcc', 'battery', 'battery_cell', 'power', 'vsupply']);
  const GND_TYPES = new Set(['gnd', 'ground', 'agnd']);

  function snap(v) { return Math.round(v / GRID) * GRID; }
  function refPart(ref) { return String(ref).split(':', 1)[0]; }

  // 与 erc_engine._conn_refs / wire_geometry.netGroups 一致：只取前两个引脚引用
  function connRefs(conn) {
    if (!Array.isArray(conn)) return [];
    const out = [];
    for (const s of conn.slice(0, 2)) {
      if (typeof s === 'string' && s.includes(':')) out.push(s);
    }
    return out;
  }

  /**
   * 自动布局主入口。
   * @param {Object} circuit CircuitJSON
   * @param {Function} sizeOf (type)->{w,h}，通常传 CircuitSymbols.sizeOf
   * @param {Object} opts {marginX=70, marginY=80, channelX=120, gapY=80}
   * @returns {{parts:Array, width:number, height:number}}
   */
  function autoLayout(circuit, sizeOf, opts) {
    opts = opts || {};
    const marginX = opts.marginX != null ? opts.marginX : 70;
    const marginY = opts.marginY != null ? opts.marginY : 90;
    const channelX = opts.channelX != null ? opts.channelX : 120; // 列间走线通道宽
    const gapY = opts.gapY != null ? opts.gapY : 80;              // 同列元件行距

    const parts = (circuit.parts || []).map(p => ({ ...p, rotate: 0 }));
    const conns = circuit.connections || [];
    // sizeOf 只依赖 p.type 且为纯函数（符号库尺寸表），按 type 缓存：
    // colW/行距/坐标/总计各环节都要取尺寸，同一元件一次布局内被取 3~4 次。
    // 命中后返回共享只读对象 —— 调用方全程只读 .w/.h，无行为差异。
    const szCache = new Map();
    const DEFAULT_SZ = { w: 100, h: 60 };
    const szOf = p => {
      let s = szCache.get(p.type);
      if (s === undefined) {
        s = (sizeOf && sizeOf(p.type)) || DEFAULT_SZ;
        szCache.set(p.type, s);
      }
      return s;
    };

    const byId = {};
    parts.forEach(p => { if (p.id != null) byId[p.id] = p; });
    const isPwr = p => POWER_TYPES.has(String(p.type).toLowerCase());
    const isGnd = p => GND_TYPES.has(String(p.type).toLowerCase());

    // ---------- 1. 邻接表：电源/地标记件不进信号分层图 ----------
    const adj = {};   // id -> Set(邻居 id)
    parts.forEach(p => { adj[p.id] = new Set(); });
    const poweredBy = {};  // 信号件 id -> [电源件 id]
    const groundedBy = {}; // 信号件 id -> [地件 id]
    const pwrTargets = {}; // 电源件 id -> [信号件 id]
    const gndTargets = {}; // 地件   id -> [信号件 id]

    const mcuLayout=parts.some(p=>MCU_TYPES.has(p.type));
    const signalGroups=[];
    if(mcuLayout) {
      // Public rail networks are sets of pins, not directed signal edges.
      // Their arbitrary chain/star encoding must not drive component placement.
      const groups=W.netGroups(conns).slice().sort((a,b)=>a.refs.slice().sort()[0].localeCompare(b.refs.slice().sort()[0]));
      for(const group of groups) {
        const ids=[...new Set(group.refs.map(refPart))].filter(id=>byId[id]).sort();
        const grounds=ids.filter(id=>isGnd(byId[id])),powers=ids.filter(id=>isPwr(byId[id]));
        const targets=ids.filter(id=>!isPwr(byId[id])&&!isGnd(byId[id]));
        if(grounds.length||powers.length) {
          for(const id of grounds)gndTargets[id]=targets.slice();
          for(const id of powers)pwrTargets[id]=targets.slice();
          for(const id of targets) {
            if(grounds.length)groundedBy[id]=grounds.slice();
            if(powers.length)poweredBy[id]=powers.slice();
          }
          continue;
        }
        signalGroups.push({ids,refs:group.refs.slice().sort()});
        for(let i=0;i<ids.length;i++)for(let j=i+1;j<ids.length;j++) {adj[ids[i]].add(ids[j]);adj[ids[j]].add(ids[i]);}
      }
    } else {
    conns.forEach(c => {
      const refs = connRefs(c);
      if (refs.length < 2) return;
      const a = refPart(refs[0]), b = refPart(refs[1]);
      const pa = byId[a], pb = byId[b];
      if (!pa || !pb || a === b) return;
      if (isGnd(pa) && isGnd(pb)) return;
      if (isPwr(pa) && isPwr(pb)) return;
      if (isGnd(pa) || isGnd(pb)) {
        const g = isGnd(pa) ? pa : pb, o = g === pa ? pb : pa;
        (groundedBy[o.id] = groundedBy[o.id] || []).push(g.id);
        (gndTargets[g.id] = gndTargets[g.id] || []).push(o.id);
        return;
      }
      if (isPwr(pa) || isPwr(pb)) {
        const s = isPwr(pa) ? pa : pb, o = s === pa ? pb : pa;
        (poweredBy[o.id] = poweredBy[o.id] || []).push(s.id);
        (pwrTargets[s.id] = pwrTargets[s.id] || []).push(o.id);
        return;
      }
      adj[a].add(b); adj[b].add(a);
    });

    }

    // ---------- 2. BFS 分层：被电源直接驱动的元件为第 0 层 ----------
    const flow = parts.filter(p => !isPwr(p) && !isGnd(p) && p.id != null);
    const layer = {};  // id -> 层号
    const queue = [];
    const deg = p => adj[p.id].size;   // 信号度（BFS 种子选择用；原先定义了两遍）
    let seeds = flow.filter(p => (poweredBy[p.id] || []).length)
      .sort((a, b) => String(a.id).localeCompare(String(b.id)));
    if (!seeds.length && flow.length) {
      // 度最大者优先作为种子：对副本排序后取首个（原写法 [...flow].sort(...)
      // 外面再套一层数组再 .flat() 属无效包装，行为等价、顺手简化）
      seeds = flow.slice().sort((a, b) => deg(b) - deg(a) || String(a.id).localeCompare(String(b.id))).slice(0, 1);
    }
    seeds.forEach(s => { layer[s.id] = 0; queue.push(s.id); });
    while (queue.length) {
      const cur = queue.shift();
      for (const nb of adj[cur]) {
        if (layer[nb] === undefined) { layer[nb] = layer[cur] + 1; queue.push(nb); }
      }
    }
    // BFS 没到达的连通分量（与电源无关的孤岛）：从度最大者重新以 0 层并入
    let orphan = flow.filter(p => layer[p.id] === undefined);
    while (orphan.length) {
      const seed = orphan.slice().sort((a, b) => deg(b) - deg(a) || String(a.id).localeCompare(String(b.id)))[0];
      layer[seed.id] = 0; queue.push(seed.id);
      while (queue.length) {
        const cur = queue.shift();
        for (const nb of adj[cur]) {
          if (layer[nb] === undefined) { layer[nb] = layer[cur] + 1; queue.push(nb); }
        }
      }
      orphan = flow.filter(p => layer[p.id] === undefined);
    }

    const nLayers = Math.max(0, ...Object.values(layer).map(v => v + 1), 0);
    const cols = Array.from({ length: nLayers }, () => []);
    flow.forEach(p => { if (layer[p.id] != null) cols[layer[p.id]].push(p); });

    // ---------- 3. 层内 barycenter 排序（左右各扫一遍降交叉） ----------
    // 排序比较器是本文件的热路径：原先每次元素比较都对两个元素重复展开
    // 邻接 Set、并对邻居列做 findIndex 全列扫描（O(列长)/次）。这里利用两个
    // 不变量做【无行为差异】的缓存：
    //   1) adj 构建后冻结 -> 邻接数组按 id 只展开一次；
    //   2) 排序 cols[L] 期间参与取值的邻居列 cols[L±1] 并不在被排序 ->
    //      其 id->下标映射每趟预建一次即可（同 id 保留首个，与 findIndex
    //      取首个的语义一致）。
    // 兜底路径“无邻居时取本列当前下标”仍实时读取，保持原语义不变。
    const idxIn = (col, id) => col.findIndex(p => p.id === id);
    const nbrCache = new Map();
    const nbrOf = id => {
      let a = nbrCache.get(id);
      if (!a) { a = [...adj[id]]; nbrCache.set(id, a); }
      return a;
    };
    const idxMapOf = col => {
      const m = new Map();
      col.forEach((p, i) => { if (!m.has(p.id)) m.set(p.id, i); });
      return m;
    };
    function bary(p, otherL, idxMap) {
      const nbIdx = [];
      for (const id of nbrOf(p.id)) {
        const i = idxMap.get(id);
        if (i !== undefined) nbIdx.push(i);
      }
      if (!nbIdx.length) return idxIn(cols[layer[p.id]], p.id);
      let s = 0;
      for (const v of nbIdx) s += v;
      return s / nbIdx.length;
    }
    for (let round = 0; round < 2; round++) {
      for (let L = 1; L < nLayers; L++) {          // 左 -> 右：跟随上一层邻居
        const m = idxMapOf(cols[L - 1]);
        cols[L].sort((a, b) => bary(a, L - 1, m) - bary(b, L - 1, m) || String(a.id).localeCompare(String(b.id)));
      }
      for (let L = nLayers - 2; L >= 0; L--) {     // 右 -> 左：跟随下一层邻居
        const m = idxMapOf(cols[L + 1]);
        cols[L].sort((a, b) => bary(a, L + 1, m) - bary(b, L + 1, m) || String(a.id).localeCompare(String(b.id)));
      }
    }

    // ---------- 4. 坐标 ----------
    if(mcuLayout) {
      const mcus=flow.filter(p=>MCU_TYPES.has(p.type)).sort((a,b)=>deg(b)-deg(a)||String(a.id).localeCompare(String(b.id)));
      const main=mcus[0];
      const cores=flow.filter(p=>p===main || (S.LIB[p.type]&&S.LIB[p.type].ref==='U'));
      const coreIds=new Set(cores.map(p=>p.id)),distance={[main.id]:0},queue=[main.id];
      while(queue.length) {
        const id=queue.shift();
        for(const next of [...adj[id]].sort())if(coreIds.has(next)&&distance[next]==null) {distance[next]=distance[id]+1;queue.push(next);}
      }
      cores.sort((a,b)=>(distance[a.id]==null?Infinity:distance[a.id])-(distance[b.id]==null?Infinity:distance[b.id])||String(a.id).localeCompare(String(b.id)));
      const groups=new Map(cores.map(p=>[p.id,[]])),railSupport=[];
      function shared(a,b) {return signalGroups.filter(group=>group.ids.includes(a)&&group.ids.includes(b));}
      for(const part of flow.filter(p=>!coreIds.has(p.id))) {
        const ranked=cores.map(core=>({core,links:shared(part.id,core.id)})).filter(item=>item.links.length)
          .sort((a,b)=>b.links.length-a.links.length || (distance[b.core.id]||0)-(distance[a.core.id]||0) || String(a.core.id).localeCompare(String(b.core.id)));
        if(!ranked.length) {railSupport.push(part);continue;}
        const owner=ranked[0].core,ys=[];
        for(const group of ranked[0].links)for(const ref of group.refs)if(refPart(ref)===owner.id) {
          try {ys.push(S.pinWorld(owner.type,ref.slice(ref.indexOf(':')+1),0,0,0).y);}catch(e) { /* unknown pins have no positional preference */ }
        }
        groups.get(owner.id).push({part,desired:ys.length?ys.reduce((a,b)=>a+b,0)/ys.length-szOf(owner).h/2:0});
      }
      let blockX=0;
      for(const core of cores) {
        const size=szOf(core),satellites=groups.get(core.id);
        core.left=snap(blockX);core.top=Math.floor(-size.h/2/GRID)*GRID;
        // Place signal passives near the actual owner pin band; interfaces
        // follow them in the same local column. No rail-only part is assigned
        // to an IC merely because it shares the global supply network.
        const connector=part=>S.LIB[part.type]&&S.LIB[part.type].ref==='J'?1:0;
        satellites.sort((a,b)=>connector(a.part)-connector(b.part)||a.desired-b.desired||String(a.part.id).localeCompare(String(b.part.id)));
        let bottom=-Infinity,width=size.w;
        for(const item of satellites) {
          const part=item.part,partSize=szOf(part);
          part.left=snap(blockX+size.w+channelX/2);
          part.top=snap(Math.max(item.desired-partSize.h/2,bottom+gapY/2));
          bottom=part.top+partSize.h;width=Math.max(width,part.left-blockX+partSize.w);
        }
        blockX+=width+channelX;
      }
      const supportIds=new Set(railSupport.map(p=>p.id));
      const placedCore=flow.filter(p=>!supportIds.has(p.id)),supportY=Math.max(...placedCore.map(p=>p.top+szOf(p).h))+gapY;
      let supportX=0;
      railSupport.sort((a,b)=>String(a.id).localeCompare(String(b.id))).forEach(part=>{
        part.left=snap(supportX);part.top=snap(supportY);supportX+=szOf(part).w+60;
      });
    } else {
    // ---------- 4. 坐标：列宽 = 本层最宽元件，行距 = 本层最高 + gapY ----------
    const colX = new Array(nLayers).fill(0);
    const colW = cols.map(col => Math.max(0, ...col.map(p => szOf(p).w)));
    let x = 0;
    for (let L = 0; L < nLayers; L++) {
      colX[L] = x;
      x += colW[L] + channelX;
    }
    cols.forEach((col, L) => {
      let y = 0;
      const totalH = col.reduce((s, p) => s + szOf(p).h, 0) + gapY * Math.max(0, col.length - 1);
      y = -totalH / 2;                              // 每列绕中线 0 上下对称
      col.forEach(p => {
        const s = szOf(p);
        p.left = snap(colX[L] + (colW[L] - s.w) / 2);
        p.top = snap(y);
        y += s.h + gapY;
      });
    });

    }

    const placed = flow.filter(p => p.left != null);
    let minY = Math.min(0, ...placed.map(p => p.top));
    let maxY = Math.max(0, ...placed.map(p => p.top + szOf(p).h));
    let minX = Math.min(0, ...placed.map(p => p.left));
    let maxX = Math.max(0, ...placed.map(p => p.left + szOf(p).w));

    // ---------- 5. 电源轨（顶部）与地轨（底部） ----------
    // 2026-09-10 修正两处布局缺陷（本次优化）：
    //  ① 堆叠间距被双重计算：原式为 `minY - s.h - 60 - i*70`，而 minY 每轮
    //     都被 `minY = min(minY, p.top)` 下调一次——间距已经由“运行中的
    //     minY”逐级提供，再叠加 `- i*70` 等于把间距放大成随序号递增的
    //     等差数列（第 i 个电源的额外间隙 = 70i），电源件一多就把画布
    //     向上撑出大片空白。改为固定 60px 间隙（与地轨一致，且与原 i=0 的
    //     行为完全相同，因此单电源件电路零变化）。
    //  ② 电源件横向锚点拿不到目标时退化成“画布正中”，而 `battery` 的
    //     典型接法是「+ 脚直接连 vcc 标记件」——该连接两端都是电源件，
    //     在邻接表构建阶段被 `isPwr && isPwr → return` 跳过，不进
    //     pwrTargets。于是带 vcc 标记的电路里，电池被摆到画布正中、
    //     竖向还落在 vcc 标记件下方，视觉上浮在空处而非挂在轨道上。
    //     这里补一层兜底：自身没目标时，借用「同网络的其它电源件」的目标
    //     （vcc 标记件的目标正是电源轨下游）作为横向锚点，电池便与所供的
    //     轨对齐。
    const pwrParts = parts.filter(p => isPwr(p)).sort((a, b) => String(a.id).localeCompare(String(b.id)));

    // 电源件之间的“同轨”关系：battery:+/− 直连 vcc 标记件时建立关联，
    // 让锚点查找能跨过这一跳（只用于取横向锚点，不影响 BFS 分层）。
    const pwrLink = {};
    conns.forEach(c => {
      const refs = connRefs(c);
      if (refs.length < 2) return;
      const pa = byId[refPart(refs[0])], pb = byId[refPart(refs[1])];
      if (!pa || !pb || pa === pb) return;
      if (!(isPwr(pa) && isPwr(pb))) return;      // 只关心电源↔电源直连
      (pwrLink[pa.id] = pwrLink[pa.id] || []).push(pb.id);
      (pwrLink[pb.id] = pwrLink[pb.id] || []).push(pa.id);
    });
    // 沿 pwrLink 收集某电源件可达的“有目标”电源件（限深 3 层，防环）
    const anchorIdsOf = id => {
      const seen = new Set([id]);
      const out = [];
      const stack = [id];
      let depth = 0;
      while (stack.length && depth < 3) {
        const n = stack.length;
        for (let k = 0; k < n; k++) {
          const cur = stack.shift();
          for (const nb of (pwrLink[cur] || [])) {
            if (seen.has(nb)) continue;
            seen.add(nb);
            if ((pwrTargets[nb] || []).length) out.push(nb);
            stack.push(nb);
          }
        }
        depth++;
      }
      return out;
    };

    pwrParts.forEach(p => {
      const s = szOf(p);
      // 自己没目标时，借同轨电源件（如直连的 vcc 标记件）的目标做锚点
      let targetIds = pwrTargets[p.id] || [];
      if (!targetIds.length) {
        for (const aid of anchorIdsOf(p.id)) {
          if ((pwrTargets[aid] || []).length) { targetIds = pwrTargets[aid]; break; }
        }
      }
      const targets = targetIds.map(id => byId[id]).filter(Boolean);
      let cx;
      if (targets.length) {
        cx = targets.reduce((sum, t) => sum + t.left + szOf(t).w / 2, 0) / targets.length;
      } else {
        cx = (minX + maxX) / 2;   // 真孤立电源件：退回画布中轴（原行为）
      }
      p.left = snap(cx - s.w / 2);
      // 固定 60px 间隙（i=0 与原公式一致；i>0 不再叠加递增偏移）
      p.top = snap(minY - s.h - 60);
      minY = Math.min(minY, p.top);
      minX = Math.min(minX, p.left); maxX = Math.max(maxX, p.left + s.w);
    });

    const gndParts = parts.filter(p => isGnd(p)).sort((a, b) => String(a.id).localeCompare(String(b.id)));
    // 地轨：与上方电源轨同策略——用固定 60px 间隙，逐个往下排。
    // 原实现写 `maxY + 60 + i * 70` 且循环内还更新 maxY，两个偏移叠加导致
    // 间距随序号膨胀（3 个地件实测 300→480→730，间隙 180→250），与电源轨
    // 的固定 60px 不一致，视觉上地件越排越散。改为以「已排完部分的底边
    // maxY」为基准固定偏移 60px：间距恒定、与电源轨对称，且后续元素
    // （lonely 行）承接的 maxY 仍正确。
    gndParts.forEach(p => {
      const s = szOf(p);
      const targets = (gndTargets[p.id] || []).map(id => byId[id]).filter(Boolean);
      let cx;
      if (targets.length) {
        cx = targets.reduce((sum, t) => sum + t.left + szOf(t).w / 2, 0) / targets.length;
      } else {
        cx = (minX + maxX) / 2;
      }
      p.left = snap(cx - s.w / 2);
      p.top = snap(maxY + 60);          // 固定 60px 间隙（不再叠加 i * 70）
      maxY = Math.max(maxY, p.top + s.h);
      minX = Math.min(minX, p.left); maxX = Math.max(maxX, p.left + s.w);
    });

    // ---------- 6. 完全没连线的元件：摆在最下方一行（ERC 会另行提示） ----------
    const lonely = parts.filter(p => p.left == null);
    lonely.forEach((p, i) => {
      const s = szOf(p);
      p.left = snap(minX + i * (s.w + 60));
      p.top = snap(maxY + 70);
      maxY = Math.max(maxY, p.top + s.h);
      maxX = Math.max(maxX, p.left + s.w);
    });

    // ---------- 7. 平移到画布边距内，整体吸附网格 ----------
    const dx = marginX - minX, dy = marginY - minY;
    parts.forEach(p => {
      if (p.left != null) { p.left = snap(p.left + dx); p.top = snap(p.top + dy); }
    });

    return {
      parts,
      width: snap(maxX - minX + marginX * 2),
      height: snap(maxY - minY + marginY * 2),
    };
  }

  return { autoLayout, connRefs, refPart };
});
