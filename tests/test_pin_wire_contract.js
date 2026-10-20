'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const R = require('../web/render.js');
const {
  connectedPinRefs,
  indexRefNetworks,
} = require('../tools/audit_contract.js');

const templates = JSON.parse(fs.readFileSync(
  path.join(__dirname, '..', 'data', 'circuit_templates.json'),
  'utf8',
));

// 与渲染器和主审计保持同一几何容差。这里有意保留两个独立阈值：
// TOL 判断正交/线段范围，1.5px 判断引脚是否真正贴合。
const TOL = 1;
const PIN_ATTACH_MAX_PX = 1.5;

function distToSeg(point, a, b) {
  const horizontal = Math.abs(a.y - b.y) < TOL;
  const vertical = Math.abs(a.x - b.x) < TOL;
  if (horizontal) {
    if (point.x < Math.min(a.x, b.x) - TOL || point.x > Math.max(a.x, b.x) + TOL) return null;
    return Math.abs(point.y - a.y);
  }
  if (vertical) {
    if (point.y < Math.min(a.y, b.y) - TOL || point.y > Math.max(a.y, b.y) + TOL) return null;
    return Math.abs(point.x - a.x);
  }
  return null;
}

function distToPolyline(point, points) {
  let best = null;
  for (let index = 0; index < points.length - 1; index++) {
    const distance = distToSeg(point, points[index], points[index + 1]);
    if (distance !== null && (best === null || distance < best)) best = distance;
  }
  return best;
}

assert.strictEqual(Object.keys(templates).length, 40, '质量基线要求恰好覆盖 40 个模板');

let connectedRefBaseline = 0;
let themedChecks = 0;
const allTemplateOutputs=[];
for (const [templateName, circuit] of Object.entries(templates)) {
  const connectedRefs = connectedPinRefs(circuit.connections);
  connectedRefBaseline += connectedRefs.size;

  for (const theme of ['kicad', 'print', 'blueprint']) {
    const output = R.renderCircuit(circuit, { title: templateName, theme, collect: true });
    const diag=output.diag;allTemplateOutputs.push({name:templateName,theme,circuit,output});
    const refNetworks = indexRefNetworks(diag);

    for (const ref of connectedRefs) {
      themedChecks++;
      const pin = diag.pinPos[ref];
      assert.ok(pin, `${templateName}/${theme}: 已连接引脚 ${ref} 没有坐标`);

      const ownNetworks = refNetworks.get(ref);
      assert.ok(ownNetworks && ownNetworks.size, `${templateName}/${theme}: ${ref} 没有所属网络`);

      let best = null;
      for (const wire of diag.wires) {
        // 关键契约：只允许所属网络的导线证明该引脚贴合，邻近的其他网络不能代偿。
        if (!ownNetworks.has(wire.net)) continue;
        const distance = distToPolyline(pin, wire.points);
        if (distance !== null && (best === null || distance < best)) best = distance;
      }
      assert.ok(
        best !== null && best <= PIN_ATTACH_MAX_PX,
        `${templateName}/${theme}: ${ref} 到所属网络最近距离 ${best === null ? '∞' : best}px`,
      );
    }
  }
}

assert.strictEqual(connectedRefBaseline, 421, '40 个模板的精确已连接引脚参考基线应为 421');
assert.strictEqual(themedChecks, 421 * 3, '每个精确已连接引脚都应在 3 个主题下验证');

// 反例：U1 属于有接线元件，但 OUT+/OUT- 未出现在连接表中，不能被纳入审计。
const buckRefs = connectedPinRefs(templates.buck_module.connections);
assert.ok(buckRefs.has('U1:IN+'));
assert.ok(!buckRefs.has('U1:OUT+'));
assert.ok(!buckRefs.has('U1:OUT-'));

console.log(`引脚-导线契约 ✔  40模板×3主题，${connectedRefBaseline}参考基线 / ${themedChecks}次 own-net 检查通过`);

// Route quality is measured from emitted wire geometry, independently of the
// router's trimming/candidate helpers. Symbol strokes are deliberately excluded.
const GEOM_EPS = 0.01;
function onSegment(p,a,b) {
  if(Math.abs(a.y-b.y)<GEOM_EPS)return Math.abs(p.y-a.y)<GEOM_EPS&&p.x>=Math.min(a.x,b.x)-GEOM_EPS&&p.x<=Math.max(a.x,b.x)+GEOM_EPS;
  if(Math.abs(a.x-b.x)<GEOM_EPS)return Math.abs(p.x-a.x)<GEOM_EPS&&p.y>=Math.min(a.y,b.y)-GEOM_EPS&&p.y<=Math.max(a.y,b.y)+GEOM_EPS;
  return false;
}
function incidentDirections(point,segments) {
  const directions=new Set();
  for(const {a,b}of segments){if(!onSegment(point,a,b))continue;
    if(Math.abs(a.y-b.y)<GEOM_EPS){if(Math.min(a.x,b.x)<point.x-GEOM_EPS)directions.add('L');if(Math.max(a.x,b.x)>point.x+GEOM_EPS)directions.add('R');}
    else {if(Math.min(a.y,b.y)<point.y-GEOM_EPS)directions.add('U');if(Math.max(a.y,b.y)>point.y+GEOM_EPS)directions.add('D');}
  }
  return [...directions];
}
function inspectOutput(out) {
  const diag=out.diag,leaves=[],bodyIntersections=[],backtracks=[],zeroSegments=[],foreignOverlaps=[],junctionsOnForeignNets=[],disconnectedNetworks=[];
  let wireLength=0;
  const segments=diag.wires.flatMap((wire,wi)=>wire.points.slice(1).map((b,i)=>({a:wire.points[i],b,net:wire.net,wire:wi,segment:i})));
  const wirePaths=[...out.svg.matchAll(/class="wire"[^>]*d="([^"]+)"/g)].map(m=>m[1]);
  function hopCoversPoint(d,point){let previous=null;for(const match of d.matchAll(/([MLA])([^MLA]+)/g)){const v=match[2].trim().split(/[ ,]+/).map(Number),end=match[1]==='A'?{x:v[5],y:v[6]}:{x:v[0],y:v[1]};if(match[1]==='A'&&previous&&Math.abs(previous.y-point.y)<GEOM_EPS&&Math.abs(end.y-point.y)<GEOM_EPS&&point.x>Math.min(previous.x,end.x)+GEOM_EPS&&point.x<Math.max(previous.x,end.x)-GEOM_EPS)return true;previous=end;}return false;}
  for(const segment of segments){const {a,b}=segment;wireLength+=Math.hypot(b.x-a.x,b.y-a.y);
    if(Math.hypot(b.x-a.x,b.y-a.y)<GEOM_EPS)zeroSegments.push(segment);
    for(const [part,box]of Object.entries(diag.box)){
      const horizontal=Math.abs(a.y-b.y)<GEOM_EPS;
      const hit=horizontal
        ? a.y>box.y+GEOM_EPS&&a.y<box.y+box.h-GEOM_EPS&&Math.max(a.x,b.x)>box.x+GEOM_EPS&&Math.min(a.x,b.x)<box.x+box.w-GEOM_EPS
        : Math.abs(a.x-b.x)<GEOM_EPS&&a.x>box.x+GEOM_EPS&&a.x<box.x+box.w-GEOM_EPS&&Math.max(a.y,b.y)>box.y+GEOM_EPS&&Math.min(a.y,b.y)<box.y+box.h-GEOM_EPS;
      if(hit)bodyIntersections.push({...segment,part});
    }
  }
  for(const wire of diag.wires)for(let i=1;i<wire.points.length-1;i++){
    const a=wire.points[i-1],b=wire.points[i],c=wire.points[i+1];
    const cross=(b.x-a.x)*(c.y-b.y)-(b.y-a.y)*(c.x-b.x),dot=(b.x-a.x)*(c.x-b.x)+(b.y-a.y)*(c.y-b.y);
    if(Math.abs(cross)<GEOM_EPS&&dot<-GEOM_EPS)backtracks.push({net:wire.net,points:[a,b,c]});
  }
  const overlapGroups=new Map();
  for(let i=0;i<segments.length;i++)for(let j=i+1;j<segments.length;j++){
    const a=segments[i],b=segments[j];if(a.net===b.net)continue;
    let axis,coordinate,lo,hi;
    if(Math.abs(a.a.y-a.b.y)<GEOM_EPS&&Math.abs(b.a.y-b.b.y)<GEOM_EPS&&Math.abs(a.a.y-b.a.y)<GEOM_EPS){axis='h';coordinate=a.a.y;lo=Math.max(Math.min(a.a.x,a.b.x),Math.min(b.a.x,b.b.x));hi=Math.min(Math.max(a.a.x,a.b.x),Math.max(b.a.x,b.b.x));}
    else if(Math.abs(a.a.x-a.b.x)<GEOM_EPS&&Math.abs(b.a.x-b.b.x)<GEOM_EPS&&Math.abs(a.a.x-b.a.x)<GEOM_EPS){axis='v';coordinate=a.a.x;lo=Math.max(Math.min(a.a.y,a.b.y),Math.min(b.a.y,b.b.y));hi=Math.min(Math.max(a.a.y,a.b.y),Math.max(b.a.y,b.b.y));}
    if(axis&&hi-lo>GEOM_EPS){const nets=[a.net,b.net].sort((x,y)=>x-y);foreignOverlaps.push({nets,axis,coordinate,lo,hi,length:hi-lo,first:a,second:b});const key=nets.join(':')+':'+axis+':'+coordinate.toFixed(4);if(!overlapGroups.has(key))overlapGroups.set(key,[]);overlapGroups.get(key).push([lo,hi]);}
  }
  let foreignOverlapUnionLength=0;
  for(const intervals of overlapGroups.values()){intervals.sort((a,b)=>a[0]-b[0]);let lo=intervals[0][0],hi=intervals[0][1];for(const next of intervals.slice(1)){if(next[0]<=hi+GEOM_EPS)hi=Math.max(hi,next[1]);else{foreignOverlapUnionLength+=hi-lo;[lo,hi]=next;}}foreignOverlapUnionLength+=hi-lo;}
  for(const net of diag.netRouted){
    const own=segments.filter(s=>s.net===Number(net.name.slice(1)));
    const parents=own.map((_,i)=>i),find=i=>{while(parents[i]!==i)i=parents[i];return i;};
    for(let i=0;i<own.length;i++)for(let j=i+1;j<own.length;j++){
      const a=own[i],b=own[j];let intersects=[a.a,a.b].some(p=>onSegment(p,b.a,b.b))||[b.a,b.b].some(p=>onSegment(p,a.a,a.b));
      if(!intersects){const ah=Math.abs(a.a.y-a.b.y)<GEOM_EPS,bh=Math.abs(b.a.y-b.b.y)<GEOM_EPS;if(ah!==bh){const h=ah?a:b,v=ah?b:a;const cross={x:v.a.x,y:h.a.y};intersects=onSegment(cross,h.a,h.b)&&onSegment(cross,v.a,v.b);}}
      if(intersects)parents[find(j)]=find(i);
    }
    const pinComponents=net.pins.map(ref=>{const index=own.findIndex(s=>onSegment(diag.pinPos[ref],s.a,s.b));return {ref,component:index<0?null:find(index)};});
    if(pinComponents.some(p=>p.component===null)||new Set(pinComponents.map(p=>p.component)).size>1)disconnectedNetworks.push({net:net.name,pinComponents});
    const candidates=new Map(own.flatMap(s=>[s.a,s.b]).map(p=>[p.x.toFixed(4)+':'+p.y.toFixed(4),p]));
    for(const point of candidates.values()){
      const directions=incidentDirections(point,own);
      const actualPin=net.pins.some(ref=>Math.hypot(diag.pinPos[ref].x-point.x,diag.pinPos[ref].y-point.y)<GEOM_EPS);
      if(directions.length===1&&!actualPin)leaves.push({net:net.name,point,directions});
    }
    for(const dot of net.dots)for(const segment of segments)if(segment.net!==Number(net.name.slice(1))&&onSegment(dot,segment.a,segment.b))junctionsOnForeignNets.push({dot,ownNet:net.name,foreignNet:'N'+segment.net,foreignSegment:segment,foreignWireHasHopOverDot:hopCoversPoint(wirePaths[segment.wire]||'',dot)});
  }
  return {wireCount:diag.wires.length,wireLength,leaves,bodyIntersections,backtracks,zeroSegments,foreignOverlaps,foreignOverlapUnionLength,junctionsOnForeignNets,disconnectedNetworks};
}
const qaDirectory=path.join(__dirname,'../evidence/upgrade/wire_layout');
fs.mkdirSync(qaDirectory,{recursive:true});
const qaPath=path.join(qaDirectory,'qa.json');
const renderRun=path.join('runs',new Date().toISOString().replace(/[:.]/g,'-'));
fs.mkdirSync(path.join(qaDirectory,renderRun),{recursive:true});
const priorQa=fs.existsSync(qaPath)?JSON.parse(fs.readFileSync(qaPath,'utf8')):null;
const ercAnnotations=JSON.parse(fs.readFileSync(path.join(__dirname,'erc_results.json'),'utf8'));
const archivedRendererPath=path.join(qaDirectory,'baseline_runtime/web/render.js');
const archivedRenderer=fs.existsSync(archivedRendererPath)?require(archivedRendererPath):null;
const archivedMetrics=new Map();
const compactMetrics=metrics=>({freeWireEnds:metrics.leaves.length,bodyIntersections:metrics.bodyIntersections.length,foreignOverlapSegmentPairs:metrics.foreignOverlaps.length,foreignOverlapUnionLength:metrics.foreignOverlapUnionLength,unbridgedForeignJunctions:metrics.junctionsOnForeignNets.filter(j=>!j.foreignWireHasHopOverDot).length,disconnectedNetworks:metrics.disconnectedNetworks.length,backtracks:metrics.backtracks.length,wireLength:metrics.wireLength});
const allTemplateRows=allTemplateOutputs.map(({name,theme,circuit,output})=>{
  const metrics=inspectOutput(output),blocked=output.diag.wires.filter(w=>w.blocked);
  if(archivedRenderer&&!archivedMetrics.has(name))archivedMetrics.set(name,compactMetrics(inspectOutput(archivedRenderer.renderCircuit(circuit,{collect:true}))));
  const before=archivedMetrics.get(name)||null,intentionalElectricalNegative=ercAnnotations[name]?.passed===false;
  const priorHadNoVisualShortOrBodyCollision=before&&!before.bodyIntersections&&!before.foreignOverlapSegmentPairs&&!before.disconnectedNetworks;
  return {name,theme,input_source:'data/circuit_templates.json#'+name,license:'Project LICENSE (MIT)',data_split:'internal_template_geometry_not_holdout',source_type:intentionalElectricalNegative?'intentional_electrical_negative':'internal_template',electrical_annotation:{source:'tests/erc_results.json cached result; no new electrical validation run',passed:ercAnnotations[name]?.passed??null},blockedWires:blocked.length,warnings:output.warnings,current:compactMetrics(metrics),archivedBefore:before,possibleNewBlockingRegression:!intentionalElectricalNegative&&blocked.length>0&&!!priorHadNoVisualShortOrBodyCollision,blockedDetails:blocked};
});
const priorAllPath=path.join(qaDirectory,'all_templates.json');
const priorAll=fs.existsSync(priorAllPath)?JSON.parse(fs.readFileSync(priorAllPath,'utf8')):null;
const allTemplateTable={timestamp:new Date().toISOString(),command:'node tests/test_pin_wire_contract.js',source_templates:40,themes:3,denominator:allTemplateRows.length,own_net_checks:themedChecks,classification:'Geometry diagnostics only; intentional electrical negatives remain separate and do not become electrical passes.',rows:allTemplateRows,summary:{blockedVariants:allTemplateRows.filter(r=>r.blockedWires).length,warningVariants:allTemplateRows.filter(r=>r.warnings.length).length,freeEndVariants:allTemplateRows.filter(r=>r.current.freeWireEnds).length,bodyCollisionVariants:allTemplateRows.filter(r=>r.current.bodyIntersections).length,foreignOverlapVariants:allTemplateRows.filter(r=>r.current.foreignOverlapSegmentPairs).length,possibleNewBlockingRegressions:allTemplateRows.filter(r=>r.possibleNewBlockingRegression).map(r=>({name:r.name,theme:r.theme}))},previousRuns:priorAll?[...(priorAll.previousRuns||[]),Object.fromEntries(Object.entries(priorAll).filter(([key])=>key!=='previousRuns'))]:[]};
fs.writeFileSync(path.join(qaDirectory,'all_templates.json'),JSON.stringify(allTemplateTable,null,2)+'\n');
const samples=[
  ...['led_limit','ne555_blink','opamp_inv'].map(name=>({name,circuit:templates[name],source:'data/circuit_templates.json#'+name})),
  {name:'internal_mcu',source:'data/pcb_active/run_e34bf9d7ac6c45be85d4fb00b97e1fec/circuit.json',circuit:JSON.parse(fs.readFileSync(path.join(__dirname,'../data/pcb_active/run_e34bf9d7ac6c45be85d4fb00b97e1fec/circuit.json'),'utf8'))},
];
const routeResults=[];
for(const sample of samples)for(const theme of ['kicad','print','blueprint']){
  const out=R.renderCircuit(sample.circuit,{collect:true,theme}),metrics=inspectOutput(out);
  const failed=[];if(metrics.leaves.length)failed.push('non-pin wire leaves');if(metrics.backtracks.length)failed.push('wire reversals');
  if(metrics.bodyIntersections.length)failed.push('wire segments entering component bounds');
  if(metrics.foreignOverlaps.length)failed.push('different electrical nets share a collinear wire interval');
  if(metrics.junctionsOnForeignNets.some(j=>!j.foreignWireHasHopOverDot))failed.push('junction dot touches an unbridged foreign net');
  if(metrics.disconnectedNetworks.length)failed.push('requested same-net pins are geometrically disconnected');
  const output=path.join(renderRun,sample.name+'-'+theme+'.svg');
  if(theme==='kicad'){fs.writeFileSync(path.join(qaDirectory,output),out.svg);fs.writeFileSync(path.join(qaDirectory,output.replace('.svg','-geometry.json')),JSON.stringify(out.diag,null,2));}
  const ownRefs=connectedPinRefs(sample.circuit.connections),refNetworks=indexRefNetworks(out.diag);
  const detached=[...ownRefs].filter(ref=>!out.diag.pinPos[ref]||!out.diag.wires.some(w=>refNetworks.get(ref)?.has(w.net)&&distToPolyline(out.diag.pinPos[ref],w.points)!==null&&distToPolyline(out.diag.pinPos[ref],w.points)<=PIN_ATTACH_MAX_PX));
  if(detached.length)failed.push('detached own-net pins');
  routeResults.push({name:sample.name,theme,input_source:sample.source,license:'Project LICENSE (MIT)',data_split:'internal_workflow_geometry_not_holdout',output:theme==='kicad'?output:null,status:failed.length?'fail':'pass',failures:failed,metrics,own_net_connected_pin_checks:ownRefs.size,detached});
}
// Non-grid keep coordinates and quarter turns must keep real terminals intact.
for(const angle of [0,90,180,270]){
  const circuit={version:2,parts:[{id:'R1',type:'resistor',left:11,top:13,rotate:angle},{id:'R2',type:'resistor',left:351,top:321,rotate:(angle+180)%360}],connections:[['R1:2','R2:1']]};
  const out=R.renderCircuit(circuit,{collect:true,layout:'keep'}),wire=out.diag.wires[0];
  const expected=[out.diag.pinPos['R1:2'],out.diag.pinPos['R2:1']];
  const actual=[wire.points[0],wire.points[wire.points.length-1]];
  const errors=expected.map((p,i)=>Math.hypot(p.x-actual[i].x,p.y-actual[i].y));
  const metrics=inspectOutput(out),failures=[];
  const direction=[{x:1,y:0},{x:0,y:1},{x:-1,y:0},{x:0,y:-1}][angle/90];
  const firstStep={x:wire.points[1].x-wire.points[0].x,y:wire.points[1].y-wire.points[0].y};
  const outward=firstStep.x*direction.x+firstStep.y*direction.y>GEOM_EPS&&Math.abs(firstStep.x*direction.y-firstStep.y*direction.x)<GEOM_EPS;
  if(errors.some(error=>error>GEOM_EPS))failures.push('keep-mode terminal shifted by routing grid');
  if(!outward)failures.push('first wire segment does not leave rotated real pin outward');
  if(metrics.bodyIntersections.length)failures.push('rotated wire enters component bounds');
  routeResults.push({name:'keep_rotated_resistors_'+angle,theme:'kicad',input_source:'Synthetic explicit non-grid two-resistor quarter-turn geometry in tests/test_pin_wire_contract.js',license:'Project LICENSE (MIT)',data_split:'internal_geometry_contract_not_holdout',status:failures.length?'fail':'pass',failures,expectedTerminals:expected,actualTerminals:actual,terminalErrors:errors,expectedOutwardDirection:direction,actualFirstStep:firstStep,metrics});
}
const report={schema_version:1,timestamp:new Date().toISOString(),command:'node tests/test_pin_wire_contract.js',status:routeResults.every(r=>r.status==='pass')?'pass':'fail',original_contract:{templates:40,themes:3,connected_ref_baseline:connectedRefBaseline,own_net_checks:themedChecks,passed:true},new_geometry:{source_samples:4,themes:3,rendered_sample_variants:12,synthetic_keep_rotation_cases:4,denominator:routeResults.length,passed:routeResults.filter(r=>r.status==='pass').length,results:routeResults},measurement:'Only emitted wire paths are inspected. A non-pin leaf has one same-net geometric direction and no requested pin at that point; symbol lead strokes are not counted. Component bounds checks use strict interiors, excluding boundaries and labels.',constraints:{model_calls:0,new_dependencies:0,training_runs:0,frozen_holdout_changes:0},previousRuns:priorQa?[...(priorQa.previousRuns||[]),Object.fromEntries(Object.entries(priorQa).filter(([key])=>key!=='previousRuns'))]:[]};
fs.writeFileSync(qaPath,JSON.stringify(report,null,2)+'\n');
for(const row of routeResults)console.log(`${row.status.toUpperCase()} ${row.name}/${row.theme}: leaves=${row.metrics.leaves.length}, body=${row.metrics.bodyIntersections.length}, length=${row.metrics.wireLength.toFixed(1)}${row.terminalErrors?', terminalErrors='+row.terminalErrors.join(','):''}`);
assert.equal(report.status,'pass','Wire geometry quality RED; full per-sample metrics saved to evidence/upgrade/wire_layout/qa.json');
