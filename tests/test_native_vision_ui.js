/* Local SVG contract: geometry remains a visual candidate with no circuit mutation. */
const assert = require('node:assert/strict');
const vision = require('../web/upstream_vision.js');
class Node {
  constructor(tag, namespaceURI) { this.tagName=tag; this.namespaceURI=namespaceURI; this.attributes={}; this.children=[]; this.style={}; }
  setAttribute(name,value) { this.attributes[name]=value; }
  appendChild(child) { this.children.push(child); return child; }
}
global.document={createElementNS:(namespace,tag)=>new Node(tag,namespace)};
const result={status:'needs_manual_correction',circuit:null,accepted:false,electrical_validated:false,
  image:{width:100,height:80},detections:[],topology:{status:'not_inferred',connections:[],nets:[]},
  wire_geometry:{status:'geometry_candidates',candidate_only:true,accepted:false,electrical_validated:false,
    width:100,height:80,truncated:true,segments:[{id:'s1',x1:5,y1:7,x2:90,y2:70}],points:[{id:'p1',x:5,y:7}]}};
const snapshot=JSON.stringify(result);
vision.validateResponse(result);
const overlay=vision.createOverlay('data:image/png;base64,fixture',result);
const geometry=overlay.children.find(node=>node.attributes['data-layer']==='wire-geometry');
assert(geometry,'HCD geometry must be visible over the original input image');
const line=geometry.children.find(node=>node.tagName==='line');
assert.equal(line.attributes.x1,'5'); assert.equal(line.attributes.y2,'70');
const point=geometry.children.find(node=>node.tagName==='circle');
assert.equal(point.attributes.cx,'5'); assert.equal(point.attributes.cy,'7');
assert(geometry.children.some(node=>node.tagName==='title' && node.textContent.includes('尚未电气确认')));
assert(geometry.children.some(node=>node.tagName==='title' && node.textContent.includes('导线候选已截取，需人工补充')));
assert.equal(JSON.stringify(result),snapshot,'drawing must not modify model evidence or infer a circuit');
const invalid=structuredClone(result); invalid.wire_geometry.accepted=true;
assert.throws(()=>vision.validateResponse(invalid),/几何|电气/);
console.log('PASS native HCD geometry overlay, manual-only boundary, immutable evidence');
const symbols = { pinsOf: type => ['resistor', 'capacitor'].includes(type) ? ['1', '2'] : [] };
const additions = [
  {index:0, added_manually:true, excluded:false, type:'resistor', value:'10kΩ', pins:{'1':'IN','2':'MID'}},
  {index:1, added_manually:true, excluded:false, type:'capacitor', value:'100nF', pins:{'1':'MID','2':'IN'}},
];
const manual = vision.buildCandidate(result, additions, symbols);
assert(!manual.errors, 'explicit manual additions must be supported after zero model detections');
assert.equal(manual.circuit.parts.length, 2, 'missed components must survive candidate creation');
assert.equal(manual.correction.detections[0].added_manually, true);
assert.equal(manual.correction.detections[0].detection_index, null, 'manual additions cannot masquerade as detections');
assert.equal(JSON.stringify(result), snapshot, 'manual additions must not rewrite raw model evidence');
const badPins = structuredClone(additions); badPins[0].pins['999'] = 'IN';
assert(vision.buildCandidate(result, badPins, symbols).errors.some(e=>e.includes('999')), 'unknown pin names must be rejected');
const implicit = structuredClone(additions); delete implicit[0].added_manually;
assert(vision.buildCandidate(result, implicit, symbols).errors.length, 'out of range rows require explicit manual provenance');
const missingValue = structuredClone(additions); missingValue[0].value = '';
assert(vision.buildCandidate(result, missingValue, symbols).errors.length, 'manual additions require explicit values');
console.log('PASS missing component additions, immutable raw provenance, unknown pins and incomplete additions rejected');
