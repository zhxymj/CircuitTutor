const assert = require('node:assert/strict');
const { test } = require('node:test');
const I = require('../web/image_recognition.js');
const corpus = require('../data/image_samples/synthetic_printed_v1.json');

test('current CGHD holdout evidence keeps raw output and correction status per sample', () => {
  const evidence = require('../evaluation/image_public_results.json');
  const current = evidence.current_holdout_full_recheck;
  assert.equal(evidence.cases.length, 30);
  assert.equal(current.cases.length, 10);
  assert.equal(new Set(current.cases.map(c => c.drafter)).size, 10);
  assert(current.cases.every(c => c.split === 'holdout'));
  assert.equal(current.raw_metrics.component_fn, current.cases.reduce((n, c) => n + c.component.fn, 0));
  assert.equal(current.raw_metrics.oracle_ocr_exact, current.cases.reduce((n, c) => n + c.ocr.filter(t => t.exact_match).length, 0));
  assert(current.cases.every(c => c.manual_correction_status === 'not_performed'));
});

// All drawings in this file are generated here and may be freely reused.
// They are clean geometry fixtures, not independent photographs or handwriting.
function fixture(n, withLabels = true) {
  const x = 20 + (n % 5) * 3, y = 22 + Math.floor(n / 5) * 2;
  const rect = { x: x + 24, y: y - 5, w: 20 + (n % 3) * 2, h: 10 };
  const plate1 = rect.x + rect.w + 32 + (n % 4) * 2, plate2 = plate1 + 6;
  const left = x, right = plate2 + 32;
  const L = (x1, y1, x2, y2) => `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="200" height="80">` +
    L(left, y, rect.x, y) + `<rect x="${rect.x}" y="${rect.y}" width="${rect.w}" height="${rect.h}"/>` +
    L(rect.x + rect.w, y, plate1, y) + L(plate1, y - 10, plate1, y + 10) +
    L(plate2, y - 10, plate2, y + 10) + L(plate2, y, right, y) +
    (withLabels ? `<text x="${rect.x}" y="${rect.y - 4}">R${n + 1}</text><text x="${plate1}" y="${y - 14}">C${n + 1}</text>` : '') + '</svg>';
  return { svg, x, y, rect, plate1, plate2, right,
    labels: withLabels ? [`R${n + 1}`, `C${n + 1}`] : ['R1', 'C1'] };
}

function imageData(f) {
  const width = 200, height = 80, data = new Uint8ClampedArray(width * height * 4);
  for (let i = 0; i < data.length; i += 4) data.set([255, 255, 255, 255], i);
  const black = (x, y) => { const i = (y * width + x) * 4; data.set([0, 0, 0, 255], i); };
  const h = (x1, x2, y) => { for (let x = x1; x <= x2; x++) black(x, y); };
  const v = (x, y1, y2) => { for (let y = y1; y <= y2; y++) black(x, y); };
  h(f.x, f.rect.x, f.y); h(f.rect.x, f.rect.x + f.rect.w, f.rect.y);
  h(f.rect.x, f.rect.x + f.rect.w, f.rect.y + f.rect.h);
  v(f.rect.x, f.rect.y, f.rect.y + f.rect.h); v(f.rect.x + f.rect.w, f.rect.y, f.rect.y + f.rect.h);
  h(f.rect.x + f.rect.w, f.plate1, f.y); v(f.plate1, f.y - 10, f.y + 10);
  v(f.plate2, f.y - 10, f.y + 10); h(f.plate2, f.right, f.y);
  return { width, height, data };
}

function checkCase(result, ids) {
  assert.equal(result.circuit.version, 2);
  assert.equal(result.circuit.provenance.origin, 'image');
  assert.deepEqual(result.circuit.parts.map(p => [p.type, p.id]), [['resistor', ids[0]], ['capacitor', ids[1]]]);
  assert.deepEqual(result.circuit.connections.map(c => c.slice(0, 2)), [[ids[0] + ':2', ids[1] + ':+']]);
  assert.equal(result.topology.filter(n => n.endpoints.length === 2).length, 1);
  assert.equal(result.issues.filter(e => e.code === 'UNCONNECTED_PIN').length, 2);
}

test('30 generated labeled clean drawings: component and topology counts', () => {
  assert.equal(corpus.samples.length, 30);
  assert.equal(corpus.samples.filter(s => s.split === 'fixture_holdout').length, 10);
  assert(corpus.samples.every(s => s.origin === 'self_generated_geometry'));
  const count = { vector: 0, raster: 0, trueParts: 0, predictedParts: 0, matchedParts: 0, trueConnections: 0, predictedConnections: 0, matchedConnections: 0 };
  for (let n = 0; n < 30; n++) {
    const f = fixture(n, n < 20);
    assert.deepEqual(corpus.samples[n].annotation.connections, [[`R${n + 1}:2`, `C${n + 1}:+`]]);
    const saved = I.recognizeSvg(corpus.samples[n].svg);
    assert.deepEqual(saved.circuit.connections.map(c => c.slice(0, 2)), corpus.samples[n].annotation.connections);
    const result = n < 20 ? I.recognizeSvg(f.svg) : I.recognizeRaster(imageData(f));
    checkCase(result, f.labels);
    count[n < 20 ? 'vector' : 'raster']++;
    count.trueParts += 2; count.predictedParts += result.circuit.parts.length; count.matchedParts += 2;
    count.trueConnections++; count.predictedConnections += result.circuit.connections.length; count.matchedConnections++;
    if (n >= 20) assert(result.issues.some(e => e.code === 'NO_RASTER_OCR'));
  }
  console.log('Synthetic fixture metrics (same geometry family; no external validity): ' + JSON.stringify(count));
});

test('crossing without dot stays separate; dot joins nets', () => {
  const line = (x1, y1, x2, y2) => ({ kind: 'line', x1, y1, x2, y2 });
  const detections = [
    { type: 'resistor', pins: [{ name: '1', x: 10, y: 20 }], score: 0.9 },
    { type: 'resistor', pins: [{ name: '1', x: 100, y: 20 }], score: 0.9 },
    { type: 'resistor', pins: [{ name: '1', x: 50, y: 5 }], score: 0.9 },
    { type: 'resistor', pins: [{ name: '1', x: 50, y: 40 }], score: 0.9 },
  ];
  const segments = [line(10, 20, 100, 20), line(50, 5, 50, 40)];
  const separate = I.recognizePrimitives(segments, { detections });
  assert.equal(separate.circuit.connections.length, 2);
  const joined = I.recognizePrimitives([...segments, { kind: 'circle', x: 50, y: 20, r: 2 }], { detections });
  assert.equal(joined.circuit.connections.length, 3);
  const hollow = I.recognizePrimitives([...segments, { kind: 'circle', x: 50, y: 20, r: 2, filled: false }], { detections });
  assert.equal(hollow.circuit.connections.length, 2);
  const tJunction = I.recognizePrimitives([line(10, 20, 100, 20), line(50, 5, 50, 20)], { detections });
  assert.equal(tJunction.topology.some(n => n.endpoints.includes('R1:1') && n.endpoints.includes('R3:1')), true);
});

test('external detector adapter accepts explicit pins and rejects malformed result', async () => {
  const primitives = [{ kind: 'line', x1: 0, y1: 10, x2: 20, y2: 10 }];
  const detector = { detect: async () => [{ type: 'resistor', pins: [{ name: '1', x: 0, y: 10 }, { name: '2', x: 20, y: 10 }], score: 0.8 }] };
  const got = await I.recognizeWithDetector(primitives, detector);
  assert.equal(got.method, 'external-detector+heuristic-wires');
  assert.equal(got.circuit.connections.length, 1);
  await assert.rejects(I.recognizeWithDetector(primitives, { detect: async () => [{ type: 'resistor' }] }), /pins/);
});

test('external detector still returns candidates when raster wire extraction exceeds its budget', async () => {
  const width = 80, height = 1600, data = new Uint8ClampedArray(width * height * 4).fill(255);
  for (let y = 1; y < height; y += 2) for (let x = 10; x <= 70; x++) {
    const i = (y * width + x) * 4; data[i] = data[i + 1] = data[i + 2] = 0;
  }
  let calls = 0;
  const detector = { detect: async () => {
    calls++;
    return [{ type: 'resistor', box: { x: 20, y: 20, w: 20, h: 10 },
      pins: [{ name: '1', x: 20, y: 25 }, { name: '2', x: 40, y: 25 }], score: 0.8 }];
  } };
  const result = await I.recognizeWithDetector({ width, height, data }, detector, { sourceId: 'dense-photo' });
  assert.equal(calls, 1);
  assert.equal(result.method, 'external-detector');
  assert.deepEqual(result.circuit.parts.map(p => [p.type, p.id]), [['resistor', 'R1']]);
  assert.deepEqual(result.circuit.connections, []);
  assert(result.issues.some(issue => issue.code === 'GEOMETRY_LIMIT'));
  assert.equal(result.circuit.provenance.source_id, 'dense-photo');
});

test('SVG security and unsupported drawing modes are explicit', () => {
  assert.throws(() => I.recognizeSvg('<svg><script/></svg>'), /不接受/);
  assert.throws(() => I.recognizeSvg('<svg><g transform="translate(5)"></g></svg>'), /transform/);
  const result = I.recognizeSvg('<svg><path d="M0 0 L20 0"/></svg>');
  assert(result.issues.some(x => x.code === 'UNSUPPORTED_SVG'));
  assert(result.issues.some(x => x.code === 'NO_SUPPORTED_SYMBOL'));
});

test('OCR adapter exposes raw words and requires explicit language resource', async () => {
  let terminated = false, received = null;
  const runtime = { createWorker: async (lang, oem, options) => {
    received = { lang, oem, options };
    return {
      setParameters: async () => {},
      recognize: async () => ({ data: { text: 'R42 C73', confidence: 60,
        blocks: [{ paragraphs: [{ lines: [{ words: [
          { text: 'R42', confidence: 61, bbox: { x0: 44, y0: 11, x1: 57, y1: 20 } },
          { text: 'C73', confidence: 59, bbox: { x0: 95, y0: 8, x1: 109, y1: 17 } },
        ] }] }] }] } }),
      terminate: async () => { terminated = true; },
    };
  } };
  await assert.rejects(I.createTesseractAdapter(runtime), /langPath/);
  const adapter = await I.createTesseractAdapter(runtime, { langPath: '/local/language-data', cacheMethod: 'none' });
  assert.equal(received.options.langPath, '/local/language-data');
  const result = await I.recognizeRasterWithOcr(imageData(fixture(0, false)), 'browser-image-source', adapter);
  assert.equal(result.ocr.words.length, 2);
  assert.deepEqual(result.circuit.parts.map(p => p.id), ['R42', 'C73']);
  assert(!result.issues.some(issue => issue.code === 'NO_RASTER_OCR'));
  assert(result.issues.some(issue => issue.code === 'OCR_REVIEW_REQUIRED'));
  await adapter.terminate();
  assert(terminated);
});

test('complex raster preserves an explicit empty candidate for manual correction', () => {
  const width = 80, height = 1600, data = new Uint8ClampedArray(width * height * 4).fill(255);
  for (let y = 1; y < height; y += 2) for (let x = 10; x <= 70; x++) {
    const i = (y * width + x) * 4; data[i] = data[i + 1] = data[i + 2] = 0;
  }
  const result = I.recognizeRaster({ width, height, data }, { sourceId: 'complex.png' });
  assert.deepEqual(result.circuit.parts, []);
  assert.deepEqual(result.circuit.connections, []);
  assert(result.issues.some(issue => issue.code === 'GEOMETRY_LIMIT'));
  assert.equal(result.circuit.provenance.source_id, 'complex.png');
  assert.throws(() => I.recognizeRaster({ width: 1, height: 1, data: [] }), /ImageData/);
});

test('raster geometry stops scanning once the segment budget is exhausted', () => {
  const width = 80, height = 2000;
  let pixelReads = 0;
  const data = new Proxy({ length: width * height * 4 }, { get(target, key) {
    if (key === 'length') return target.length;
    pixelReads++;
    const index = Number(key), pixel = Math.floor(index / 4), x = pixel % width, y = Math.floor(pixel / width);
    return index % 4 === 3 || y % 2 === 0 || x < 10 || x > 70 ? 255 : 0;
  } });
  const result = I.recognizeRaster({ width, height, data });
  assert(result.issues.some(issue => issue.code === 'GEOMETRY_LIMIT'));
  assert.deepEqual(result.circuit.parts, []);
  assert(pixelReads < 600000, 'scanner continued through the remaining pixels after reaching 701 segments');
});
