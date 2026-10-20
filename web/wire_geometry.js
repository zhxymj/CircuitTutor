/* ============================================================
 * wire_geometry.js —— 智绘电路前端正交走线工具（原生 JS，零依赖）
 * ------------------------------------------------------------
 * 实现要点：
 *  - L/Z/U 候选与障碍边界可见图（避开元件包围盒的正交绕行）；
 *  - 不同网络的通道间距与结点避让，保留合法正交跨越；
 *  - 正交线段判定、网格吸附、共线拐点消冗。
 * 全部实现为无框架纯函数，
 * 可直接 <script> 引入 render.js，也可在 Node 下单测。
 *
 * 坐标约定：点 {x,y}；障碍 {x,y,width,height}（元件包围盒，已含 padding）。
 * ============================================================ */

(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) module.exports = factory();
  else root.WireGeometry = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const TOL = 1; // 坐标容差，浮点对齐用
  const WIRE_SPACING = 8; // 不同网络平行导线的最小通道间距（SVG 用户单位）

  // ---------- 基础几何 ----------
  function samePoint(a, b) {
    return Math.abs(a.x - b.x) < TOL && Math.abs(a.y - b.y) < TOL;
  }

  // 线段是水平 / 垂直 / 还是斜的（斜的返回 null）
  function segmentAxis(a, b) {
    const horiz = Math.abs(a.y - b.y) < TOL;
    const vert = Math.abs(a.x - b.x) < TOL;
    if (horiz && vert) return null;
    if (horiz) return 'h';
    if (vert) return 'v';
    return null;
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function snap(v, grid) { return grid ? Math.round(v / grid) * grid : v; }

  // 点到一条水平/垂直线段的距离（斜线段返回 null）
  function distToOrthSegment(p, a, b) {
    const axis = segmentAxis(a, b);
    if (axis === 'h') {
      if (p.x < Math.min(a.x, b.x) || p.x > Math.max(a.x, b.x)) return null;
      return Math.abs(p.y - a.y);
    }
    if (axis === 'v') {
      if (p.y < Math.min(a.y, b.y) || p.y > Math.max(a.y, b.y)) return null;
      return Math.abs(p.x - a.x);
    }
    return null;
  }

  // ---------- 碰撞：一条正交线段是否穿过某元件包围盒 ----------
  function segHitsRect(a, b, r) {
    const axis = segmentAxis(a, b);
    if (axis === 'h') {
      return a.y > r.y && a.y < r.y + r.height &&
             Math.max(a.x, b.x) > r.x && Math.min(a.x, b.x) < r.x + r.width;
    }
    if (axis === 'v') {
      return a.x > r.x && a.x < r.x + r.width &&
             Math.max(a.y, b.y) > r.y && Math.min(a.y, b.y) < r.y + r.height;
    }
    return false;
  }

  function pathHitsObstacle(points, obstacles) {
    for (let i = 0; i < points.length - 1; i++) {
      for (const r of obstacles || []) {
        if (segHitsRect(points[i], points[i + 1], r)) return true;
      }
    }
    return false;
  }

  // ---------- 几种正交走法，返回折线点序列 ----------
  // L 形：先水平后垂直（horizFirst=true）或先垂直后水平
  function lShape(s, e, horizFirst) {
    if (horizFirst) return [s, { x: e.x, y: s.y }, e];
    return [s, { x: s.x, y: e.y }, e];
  }

  // Z 形：水平-垂直-水平（同行偏移时用）
  function zShapeHV(s, e) {
    const m1 = { x: s.x + (e.x - s.x) / 2, y: s.y };
    const m2 = { x: s.x + (e.x - s.x) / 2, y: e.y };
    return [s, m1, m2, e];
  }
  // Z 形：垂直-水平-垂直
  function zShapeVH(s, e) {
    const m1 = { x: s.x, y: s.y + (e.y - s.y) / 2 };
    const m2 = { x: e.x, y: s.y + (e.y - s.y) / 2 };
    return [s, m1, m2, e];
  }

  // U 形：绕到两个端点的外侧再回来（offset 为绕行距离，正=下方，负=上方）。
  // 注意绕行线必须取“两端 y 的公共外侧”，否则中段会变成斜线。
  function uShape(s, e, offset) {
    const d = offset || 24;
    const yy = (d >= 0 ? Math.max(s.y, e.y) : Math.min(s.y, e.y)) + d;
    return [s, { x: s.x, y: yy }, { x: e.x, y: yy }, e];
  }

  // ---------- 拐点消冗（共线拐点合并） ----------
  function isBetween(a, b, c) {
    return b >= Math.min(a, c) - TOL && b <= Math.max(a, c) + TOL;
  }
  // 删除“前后三点共线”的中间点（直线穿过、非掉头）
  function collapseCollinear(points) {
    if (points.length < 3) return points.map(p => ({ ...p }));
    const out = [{ ...points[0] }];
    for (let i = 1; i < points.length - 1; i++) {
      const prev = out[out.length - 1], cur = points[i], next = points[i + 1];
      const colX = Math.abs(prev.x - cur.x) < TOL && Math.abs(cur.x - next.x) < TOL &&
                   isBetween(prev.y, cur.y, next.y);
      const colY = Math.abs(prev.y - cur.y) < TOL && Math.abs(cur.y - next.y) < TOL &&
                   isBetween(prev.x, cur.x, next.x);
      if (colX || colY) continue;
      out.push({ ...cur });
    }
    out.push({ ...points[points.length - 1] });
    return out;
  }
  // 删除“进出方向同轴”的中间点（更宽松，L 拐角保留）
  function dropSameAxis(points) {
    if (points.length < 3) return points.map(p => ({ ...p }));
    const out = [{ ...points[0] }];
    for (let i = 1; i < points.length - 1; i++) {
      const prev = out[out.length - 1], cur = points[i], next = points[i + 1];
      const inAxis = Math.abs(cur.x - prev.x) > Math.abs(cur.y - prev.y) ? 'h' : 'v';
      const outAxis = Math.abs(next.x - cur.x) > Math.abs(next.y - cur.y) ? 'h' : 'v';
      if (inAxis === outAxis) continue;
      out.push({ ...cur });
    }
    out.push({ ...points[points.length - 1] });
    return out;
  }

  // 整条折线吸附到网格，并做消冗
  function tidy(points, grid) {
    let pts = points.map(p => ({ x: snap(p.x, grid), y: snap(p.y, grid) }));
    pts = collapseCollinear(pts);
    pts = dropSameAxis(pts);
    return pts.filter((p, i) => i === 0 || !samePoint(p, pts[i - 1]));
  }

  // 沿方向 'L'/'R'/'U'/'D' 平移点
  function moveDir(p, d, dist) {
    if (d === 'L') return { x: p.x - dist, y: p.y };
    if (d === 'R') return { x: p.x + dist, y: p.y };
    if (d === 'U') return { x: p.x, y: p.y - dist };
    if (d === 'D') return { x: p.x, y: p.y + dist };
    return { x: p.x, y: p.y };
  }

  // 整条折线撞到障碍的次数（供“最少碰撞”兜底排序）。
  // exemptFirstLast：首段/末段是“引脚逃逸段”（从引脚沿引线方向离开元件盒），
  // 穿过自家元件的 padding 属于正常现象，不应计为碰撞。
  function countHits(points, obstacles, exemptFirstLast) {
    let n = 0;
    for (let i = 0; i < points.length - 1; i++) {
      const escape = exemptFirstLast && (i === 0 || i === points.length - 2);
      for (const r of obstacles || []) {
        if (escape && r._own) continue;
        if (segHitsRect(points[i], points[i + 1], r)) n++;
      }
    }
    return n;
  }

  /**
   * 主入口：在两个引脚间生成一条【严格正交】的折线路径。
   * @param {Point} s 起点（引脚世界坐标）
   * @param {Point} e 终点
   * @param {Array} obstacles 元件包围盒数组；两端引脚所属元件的盒可带
   *                _own:true 标记，其碰撞只在“中段”计入（见 countHits）
   * @param {number} grid 绕行通道网格（默认10；真实起终点不吸附）
   * @param {string=} sDir 起点引脚朝向 'L'/'R'/'U'/'D'（提供时先沿引脚
   *                朝向“逃逸”出元件包围盒再布线，符合教科书引线画法，
   *                也避免沿盒边平行走线被 padding 误判为碰撞）
   * @param {string=} eDir 终点引脚朝向
   * @param {Array=} reserved 已有异网线段 {a,b,junction?}；同网线段不传入
   * @returns {Point[]} 折线点（含起终点），可直接拼成 SVG polyline 的 points
   */
  function pointOnReserved(p, reserved) {
    return (reserved||[]).some(segment => {
      const a=segment.a,b=segment.b;
      return Math.abs(a.y-b.y)<1e-7?Math.abs(p.y-a.y)<1e-7&&p.x>=Math.min(a.x,b.x)&&p.x<=Math.max(a.x,b.x):
        Math.abs(p.x-a.x)<1e-7&&p.y>=Math.min(a.y,b.y)&&p.y<=Math.max(a.y,b.y);
    });
  }
  function parallelReservationHit(a,b,reserved,spacing) {
    const h=Math.abs(a.y-b.y)<1e-7;
    return (reserved||[]).some(segment => {
      const p=segment.a,q=segment.b,otherH=Math.abs(p.y-q.y)<1e-7;
      if(segment.junction)return segHitsRect(a,b,{x:p.x-spacing,y:p.y-spacing,width:spacing*2,height:spacing*2});
      if(h!==otherH)return false;
      return h?Math.abs(a.y-p.y)<spacing-1e-7&&Math.min(Math.max(a.x,b.x),Math.max(p.x,q.x))-Math.max(Math.min(a.x,b.x),Math.min(p.x,q.x))>1e-7:
        Math.abs(a.x-p.x)<spacing-1e-7&&Math.min(Math.max(a.y,b.y),Math.max(p.y,q.y))-Math.max(Math.min(a.y,b.y),Math.min(p.y,q.y))>1e-7;
    });
  }
  function pathConflictsReserved(points,reserved,spacing= WIRE_SPACING) {
    if(!points.length||!reserved||!reserved.length)return false;
    if(pointOnReserved(points[0],reserved)||pointOnReserved(points[points.length-1],reserved))return true;
    for(let i=1;i<points.length;i++) {
      if(parallelReservationHit(points[i-1],points[i],reserved,spacing))return true;
      if(i<points.length-1&&segmentAxis(points[i-1],points[i])!==segmentAxis(points[i],points[i+1])&&pointOnReserved(points[i],reserved))return true;
    }
    return false;
  }

  function routeWire(s, e, obstacles = [], grid = 10, sDir, eDir, reserved = []) {
    const EPS = 1e-7;
    const equal = (a, b) => Math.abs(a.x-b.x)<EPS && Math.abs(a.y-b.y)<EPS;
    if (equal(s,e)) return [{...s}];
    const opposite = {L:'R',R:'L',U:'D',D:'U'};
    const direction = (a,b) => Math.abs(a.y-b.y)<EPS ? (b.x>a.x?'R':'L') :
      Math.abs(a.x-b.x)<EPS ? (b.y>a.y?'D':'U') : null;
    // Only remove duplicate/monotone collinear vertices. Real pin coordinates
    // are immutable, including odd and fractional positions in manual layouts.
    function clean(points) {
      const out=[];
      for (const point of points) {
        if (out.length && equal(out[out.length-1],point)) continue;
        out.push({...point});
        while (out.length>=3) {
          const a=out[out.length-3],b=out[out.length-2],c=out[out.length-1];
          if (direction(a,b)!==direction(b,c)) break;
          out.splice(out.length-2,1);
        }
      }
      return out;
    }
    const inside = (p,r) => p.x>=r.x-EPS && p.x<=r.x+r.width+EPS && p.y>=r.y-EPS && p.y<=r.y+r.height+EPS;
    function hits(points) {
      let count=0;
      for(let i=1;i<points.length;i++) for(const r of obstacles) {
        if (r._own && ((i===1&&inside(s,r)) || (i===points.length-1&&inside(e,r)))) continue;
        if(segHitsRect(points[i-1],points[i],r)) count++;
      }
      return count;
    }
    function admissible(points) {
      if(points.length<2) return false;
      if(sDir&&direction(points[0],points[1])!==sDir) return false;
      if(eDir&&direction(points[points.length-2],points[points.length-1])!==opposite[eDir]) return false;
      return points.every((p,i)=>!i||direction(points[i-1],p));
    }
    const score = points => points.slice(1).reduce((n,p,i)=>n+Math.abs(p.x-points[i].x)+Math.abs(p.y-points[i].y),0)+8*Math.max(0,points.length-2);
    const clearance=Math.max(2,grid||0);
    function escape(p,dir) {
      if(!dir) return {...p};
      let distance=clearance;
      for(const r of obstacles) if(r._own&&inside(p,r)) {
        const d=dir==='L'?p.x-r.x:dir==='R'?r.x+r.width-p.x:dir==='U'?p.y-r.y:r.y+r.height-p.y;
        distance=Math.max(distance,d+clearance);
      }
      return moveDir(p,dir,distance);
    }
    const a=escape(s,sDir),b=escape(e,eDir);
    const combine = mid => clean([s,...mid,e]);
    let best=null,bestScore=Infinity,fallback=null,fallbackHits=Infinity;
    const candidates=[lShape(s,e,true),lShape(s,e,false),
      ...[lShape(a,b,true),lShape(a,b,false),zShapeHV(a,b),zShapeVH(a,b),uShape(a,b,24),uShape(a,b,-24)].map(combine)];
    for(const candidate of candidates) {
      const points=clean(candidate);
      if(!admissible(points)) continue;
      const n=hits(points)+(pathConflictsReserved(points,reserved)?1:0),cost=score(points);
      if(n<fallbackHits || (n===fallbackHits&&(!fallback||cost<score(fallback)))) { fallback=points;fallbackHits=n; }
      if(!n&&cost<bestScore) {best=points;bestScore=cost;}
    }
    // A Manhattan-length route cannot be shorter geometrically; no detour
    // search is needed when one of the simple routes already achieves it.
    const manhattan=Math.abs(s.x-e.x)+Math.abs(s.y-e.y);
    if(best&&score(best)-8*Math.max(0,best.length-2)<=manhattan+EPS) return best;

    // Coordinate-compressed orthogonal visibility graph. Rectangle boundaries
    // provide every useful detour lane, unlike the old fixed six L/Z/U trials.
    const xs=new Set([a.x,b.x]),ys=new Set([a.y,b.y]);
    for(const r of obstacles) {
      xs.add(Math.floor(r.x/clearance)*clearance);xs.add(Math.ceil((r.x+r.width)/clearance)*clearance);
      ys.add(Math.floor(r.y/clearance)*clearance);ys.add(Math.ceil((r.y+r.height)/clearance)*clearance);
    }
    for(const segment of reserved) {
      const p=segment.a,q=segment.b;
      if(segment.junction) {
        xs.add(p.x-WIRE_SPACING);xs.add(p.x+WIRE_SPACING);ys.add(p.y-WIRE_SPACING);ys.add(p.y+WIRE_SPACING);continue;
      }
      if(Math.abs(p.y-q.y)<EPS) {
        ys.add(p.y-WIRE_SPACING);ys.add(p.y+WIRE_SPACING);
        xs.add(Math.min(p.x,q.x)-WIRE_SPACING);xs.add(Math.max(p.x,q.x)+WIRE_SPACING);
      } else {
        xs.add(p.x-WIRE_SPACING);xs.add(p.x+WIRE_SPACING);
        ys.add(Math.min(p.y,q.y)-WIRE_SPACING);ys.add(Math.max(p.y,q.y)+WIRE_SPACING);
      }
    }
    const xx=[...xs].sort((x,y)=>x-y),yy=[...ys].sort((x,y)=>x-y),nx=xx.length,ny=yy.length;
    const ix=xx.indexOf(a.x),iy=yy.indexOf(a.y),ex=xx.indexOf(b.x),ey=yy.indexOf(b.y);
    const start=iy*nx+ix,end=ey*nx+ex,N=nx*ny;
    const distances=new Float64Array(N*3);distances.fill(Infinity);
    const previous=new Int32Array(N*3);previous.fill(-1);
    const heap=[];
    const less=(u,v)=>u.cost<v.cost || (u.cost===v.cost&&u.state<v.state);
    function push(entry) {
      heap.push(entry);let i=heap.length-1;
      while(i>0) {const parent=(i-1)>>1;if(!less(entry,heap[parent]))break;heap[i]=heap[parent];i=parent;}heap[i]=entry;
    }
    function pop() {
      const head=heap[0],tail=heap.pop();if(!heap.length)return head;
      let i=0;
      while(i*2+1<heap.length) {let child=i*2+1;if(child+1<heap.length&&less(heap[child+1],heap[child]))child++;
        if(!less(heap[child],tail))break;heap[i]=heap[child];i=child;}heap[i]=tail;return head;
    }
    const point = node => ({x:xx[node%nx],y:yy[Math.floor(node/nx)]});
    const startAxis=sDir?(sDir==='L'||sDir==='R'?1:2):0;
    const initial=start*3+startAxis;distances[initial]=0;push({state:initial,cost:0});
    let final=-1;
    while(heap.length) {
      const cur=pop();if(cur.cost!==distances[cur.state])continue;
      const node=Math.floor(cur.state/3),axis=cur.state%3;
      if(node===end) {final=cur.state;break;}
      const x=node%nx,y=Math.floor(node/nx),p=point(node);
      const adjacent=[];
      if(x>0)adjacent.push([node-1,1,'L']);if(x+1<nx)adjacent.push([node+1,1,'R']);
      if(y>0)adjacent.push([node-nx,2,'U']);if(y+1<ny)adjacent.push([node+nx,2,'D']);
      for(const [next,nextAxis,dir] of adjacent) {
        if(node===start&&sDir&&dir===opposite[sDir])continue;
        if(next===end&&eDir&&dir===eDir)continue;
        const q=point(next);if(obstacles.some(r=>segHitsRect(p,q,r)))continue;
        if(parallelReservationHit(p,q,reserved,WIRE_SPACING))continue;
        if(axis&&axis!==nextAxis&&pointOnReserved(p,reserved))continue;
        if(next===end&&eDir&&nextAxis!==(eDir==='L'||eDir==='R'?1:2)&&pointOnReserved(q,reserved))continue;
        if(next===end&&equal(b,e)&&pointOnReserved(e,reserved))continue;
        const cost=cur.cost+Math.abs(q.x-p.x)+Math.abs(q.y-p.y)+(axis&&axis!==nextAxis?8:0);
        const state=next*3+nextAxis;
        if(cost>=distances[state])continue;distances[state]=cost;previous[state]=cur.state;push({state,cost});
      }
    }
    if(final>=0) {
      const middle=[];for(let k=final;k>=0;k=previous[k])middle.push(point(Math.floor(k/3)));
      const points=combine(middle.reverse());
      if(admissible(points)&&!hits(points)&&!pathConflictsReserved(points,reserved)&&score(points)<bestScore) {best=points;bestScore=score(points);}
    }
    if(best)return best;
    // An overlapping/impossible manual placement remains visible and explicit.
    // render.js turns this marker into a warning and a dashed unresolved wire.
    const unresolved=fallback||clean(combine(lShape(a,b,true)));
    unresolved.blocked=true;return unresolved;
  }

  // 把折线点序列转成 SVG path 的 d 字符串（M .. L ..）
  function toPathD(points) {
    if (!points.length) return '';
    let d = `M${points[0].x} ${points[0].y}`;
    for (let i = 1; i < points.length; i++) {
      d += ` L${points[i].x} ${points[i].y}`;
    }
    return d;
  }

  // ============================================================
  // 网络级工具（wire/net 组织方式）
  // ============================================================

  // 并查集：把“共享引脚”的连接归并成同一个网络
  function netGroups(connections) {
    const parent = {};
    const find = x => { parent[x] = parent[x] || x; while (parent[x] !== x) { parent[x] = parent[parent[x]]; x = parent[x]; } return x; };
    const union = (a, b) => { const ra = find(a), rb = find(b); if (ra !== rb) parent[rb] = ra; };
    const validRefs = c => {
      const out = [];
      if (Array.isArray(c)) for (const s of c.slice(0, 2)) {
        if (typeof s === 'string' && s.includes(':')) out.push(s);
      }
      return out;
    };
    // 每条连接的引脚引用只解析一次（原先并查与分组两遍各解析一遍）
    const refsOf = connections.map(validRefs);
    refsOf.forEach(r => { if (r.length >= 2) { find(r[0]); find(r[1]); union(r[0], r[1]); } });
    const groups = {};
    refsOf.forEach((r, i) => {
      if (!r.length) return;
      const root = find(r[0]);
      (groups[root] = groups[root] || { refs: [], conns: [] });
      for (const ref of r) if (!groups[root].refs.includes(ref)) groups[root].refs.push(ref);
      groups[root].conns.push(i);
    });
    return Object.values(groups);
  }

  // 方向归一化：从点 p 沿折线第一段/最后一段伸出的方向
  function dirAtEnd(points, atStart) {
    const a = atStart ? points[0] : points[points.length - 1];
    const b = atStart ? points[1] : points[points.length - 2];
    if (!b) return null;
    if (Math.abs(b.y - a.y) < TOL) return b.x >= a.x ? 'R' : 'L';
    return b.y >= a.y ? 'D' : 'U';
  }

  /**
   * 计算一个网络里应画实心连接点（junction dot）的位置。
   * 规则（教科书惯例）：某点汇聚的不同方向数 ≥3 才画点；
   * 两个线端头对头直线相接、或直角拐角，都不画。
   * @param {Point[][]} routed 同一网络每条连接的折线点序列
   * @param {Object} pinDirs 可选 {引脚引用: 'L'|'R'|'U'|'D'} 元件引脚朝向，
   *                计入元件引线方向（引脚上两根线 + 引线 = 3 向 → 应画点）
   */
  function junctionDots(routed, pinDirs) {
    const key = p => `${Math.round(p.x)}:${Math.round(p.y)}`;
    const dirs = {};   // key -> Set(方向)
    const add = (p, d) => { if (!d) return; const k = key(p); (dirs[k] = dirs[k] || new Set()).add(d); };
    for (const pts of routed) {
      if (!pts || pts.length < 2) continue;
      const s = pts[0], e = pts[pts.length - 1];
      add(s, dirAtEnd(pts, true));
      add(e, dirAtEnd(pts, false));
      // T 型：端点落在另一条线段中间（在第二遍统一处理，见下方 seg 扫描）
    }
    // 端点落在同网络其他线段“中段”的 T 型交汇。
    // 每条折线只算一次包围盒：端点在盒外（含 TOL 容差）的折线整条跳过，
    // 免去 O(端点数×折线数×线段数) 次逐段距离计算（bbox 快速拒绝实践）。
    // 预筛是精确的：能命中 distToOrthSegment 的点
    // 必然落在对方折线包围盒内（横段要求 x∈跨度、|dy|<TOL，竖段对称）。
    const boxes = routed.map(pts => {
      if (!pts || pts.length < 2) return null;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const q of pts) {
        if (q.x < x0) x0 = q.x;
        if (q.x > x1) x1 = q.x;
        if (q.y < y0) y0 = q.y;
        if (q.y > y1) y1 = q.y;
      }
      return { x0, y0, x1, y1 };
    });
    for (let wi = 0; wi < routed.length; wi++) {
      const pts = routed[wi];
      if (!pts || pts.length < 2) continue;
      for (const p of [pts[0], pts[pts.length - 1]]) {
        for (let wj = 0; wj < routed.length; wj++) {
          const other = routed[wj];
          if (wj === wi || !other || other.length < 2) continue;
          const bb = boxes[wj];
          if (p.x < bb.x0 - TOL || p.x > bb.x1 + TOL ||
              p.y < bb.y0 - TOL || p.y > bb.y1 + TOL) continue; // 盒外整条跳过
          for (let i = 0; i < other.length - 1; i++) {
            const a = other[i], b = other[i + 1];
            if (samePoint(a, p) || samePoint(b, p)) continue; // 端点相接不算 T
            const dist = distToOrthSegment(p, a, b);   // 距离只算一次（原先同一对参数连算两遍）
            if (dist !== null && dist < TOL) {
              // 中段交汇：该线段两个方向 + 端点引线方向（轴向只算一次）
              const horiz = Math.abs(b.x - a.x) > Math.abs(b.y - a.y);
              const k = key(p);
              const set = (dirs[k] = dirs[k] || new Set());
              set.add(horiz ? 'R' : 'D');
              set.add(horiz ? 'L' : 'U');
            }
          }
        }
      }
    }
    // Two interiors of this same electrical net form a true four-way
    // junction as well; endpoint-only scanning misses this case.
    const horizontal=[],vertical=[];
    for(const path of routed) for(let i=1;i<(path||[]).length;i++) {
      const a=path[i-1],b=path[i],axis=segmentAxis(a,b);
      if(axis==='h')horizontal.push([a,b]);else if(axis==='v')vertical.push([a,b]);
    }
    for(const [a,b] of horizontal) for(const [c,d] of vertical) {
      const p={x:c.x,y:a.y};
      if(p.x<Math.min(a.x,b.x)||p.x>Math.max(a.x,b.x)||p.y<Math.min(c.y,d.y)||p.y>Math.max(c.y,d.y))continue;
      if(p.x>Math.min(a.x,b.x))add(p,'L');if(p.x<Math.max(a.x,b.x))add(p,'R');
      if(p.y>Math.min(c.y,d.y))add(p,'U');if(p.y<Math.max(c.y,d.y))add(p,'D');
    }
    if (pinDirs) {
      for (const [ref, d] of Object.entries(pinDirs)) {
        // 引脚位置未知（由调用方保证 dirs 已含该点），这里只补方向
        // 调用方约定：pinDirs 的 value 也可以是 {x,y,dir}
        if (typeof d === 'object' && d) add(d, d.dir);
      }
    }
    const out = [];
    for (const [k, set] of Object.entries(dirs)) {
      if (set.size >= 3) {
        const [x, y] = k.split(':').map(Number);
        out.push({ x, y });
      }
    }
    return out;
  }

  /**
   * 计算不同网络交叉点并指定跳线归属；默认横线跳过竖线，
   * 横线所属网络在交点有结点时改由外网竖线跳过，保留本网络结点。
   * @param {Array<{net:number, points:Point[]}>} wires
   * @param {Map=} netDots 网络编号 -> 本网络结点
   * @returns {Array<{x,y,net,axis}>}
   */
  function wireCrossings(wires, netDots) {
    const crossings = [];
    // 轴向每段只算一次并按横/竖分桶：只处理“一横一竖”的配对。
    // 原实现 O(S²) 两两配对，每对重复求两次 axis 且同轴对全部白比；
    // 分桶后降为 O(H×V)。产出集合与旧实现完全一致（仅遍历顺序不同，
    // 消费方 pathWithHops 按段过滤+排序，顺序无关）。
    const hs = [], vs = [];
    for (const w of wires) {
      for (let i = 0; i < w.points.length - 1; i++) {
        const a = w.points[i], b = w.points[i + 1];
        const axis = segmentAxis(a, b);
        if (axis === 'h') hs.push({ net: w.net, a, b });
        else if (axis === 'v') vs.push({ net: w.net, a, b });
      }
    }
    const eps = 2;
    for (const h of hs) {
      for (const v of vs) {
        if (h.net === v.net) continue;
        const x = v.a.x, y = h.a.y;
        if (x > Math.min(h.a.x, h.b.x) + eps && x < Math.max(h.a.x, h.b.x) - eps &&
            y > Math.min(v.a.y, v.b.y) + eps && y < Math.max(v.a.y, v.b.y) - eps) {
          const junction=(netDots&&netDots.get(h.net)||[]).some(p=>Math.hypot(p.x-x,p.y-y)<=6);
          crossings.push({ x, y, net: junction?v.net:h.net, axis: junction?'v':'h' });
        }
      }
    }
    return crossings;
  }

  /**
   * 按交叉点归属生成横向或纵向跳线弧。相邻弧按间距缩小半径，
   * 不回折，也不把属于另一根导线的跳弧画到当前网络上。
   */
  function pathWithHops(points, crossings, r, net) {
    r=r||6;
    if(!points.length)return '';
    if(!crossings||!crossings.length)return toPathD(points);
    let d=`M${points[0].x} ${points[0].y}`;
    for(let i=1;i<points.length;i++) {
      const a=points[i-1],b=points[i],axis=segmentAxis(a,b),horizontal=axis==='h';
      if(!axis) {d+=` L${b.x} ${b.y}`;continue;}
      const start=horizontal?a.x:a.y,end=horizontal?b.x:b.y,fixed=horizontal?a.y:a.x;
      const fwd=start<end,lo=Math.min(start,end),hi=Math.max(start,end);
      const hits=crossings.filter(c=>(net==null||c.net==null||c.net===net)&&
        (c.axis?c.axis===axis:horizontal)&&Math.abs((horizontal?c.y:c.x)-fixed)<TOL&&
        (horizontal?c.x:c.y)>lo&&(horizontal?c.x:c.y)<hi)
        .map(c=>horizontal?c.x:c.y).filter((v,j,all)=>all.findIndex(q=>Math.abs(q-v)<TOL)===j)
        .sort((u,v)=>fwd?u-v:v-u);
      for(let j=0;j<hits.length;j++) {
        const centre=hits[j],prev=j?Math.abs(centre-hits[j-1])/2:r,next=j+1<hits.length?Math.abs(centre-hits[j+1])/2:r;
        const radius=Math.min(r,prev*.9,next*.9,centre-lo,hi-centre);
        const before=fwd?centre-radius:centre+radius,after=fwd?centre+radius:centre-radius;
        d+=horizontal?` L${before} ${fixed}`:` L${fixed} ${before}`;
        d+=horizontal?` A${radius} ${radius} 0 0 ${fwd?1:0} ${after} ${fixed}`:
          ` A${radius} ${radius} 0 0 ${fwd?1:0} ${fixed} ${after}`;
      }
      d+=` L${b.x} ${b.y}`;
    }
    return d;
  }

  return {
    samePoint, segmentAxis, clamp, snap, distToOrthSegment,
    segHitsRect, pathHitsObstacle, moveDir, countHits,
    lShape, zShapeHV, zShapeVH, uShape,
    collapseCollinear, dropSameAxis, tidy,
    routeWire, toPathD, WIRE_SPACING, pointOnReserved, pathConflictsReserved,
    netGroups, junctionDots, wireCrossings, pathWithHops,
  };
});
