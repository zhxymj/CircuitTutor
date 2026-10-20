/* Clear, axis-aligned printed schematics only. No model weights or OCR are bundled.
 * Input is a small SVG subset, ImageData, or normalized vector primitives.
 * Results are proposals for human correction, never electrically validated designs.
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) module.exports = factory();
  else root.CircuitImageRecognition = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  const EPS = 1.6;
  const near = (a, b, e = EPS) => Math.abs(a - b) <= e;
  const between = (v, a, b, e = EPS) => v >= Math.min(a, b) - e && v <= Math.max(a, b) + e;
  const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const point = (x, y) => ({ x, y });

  function attrs(s) {
    const out = {};
    const re = /([A-Za-z_:][\w:.-]*)\s*=\s*(["'])(.*?)\2/g;
    let m;
    while ((m = re.exec(s))) out[m[1]] = m[3];
    return out;
  }
  function number(value, fallback = NaN) {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  }
  function line(x1, y1, x2, y2) {
    if (![x1, y1, x2, y2].every(Number.isFinite)) return null;
    if (near(x1, x2, 0.01)) return { kind: 'line', x1, y1, x2: x1, y2 };
    if (near(y1, y2, 0.01)) return { kind: 'line', x1, y1, x2, y2: y1 };
    return null;
  }
  function svgToPrimitives(svg) {
    if (typeof svg !== 'string' || svg.length > 3000000) throw new Error('SVG 必须是小于 3 MB 的文本');
    if (/<(?:script|foreignObject|image|use)\b/i.test(svg))
      throw new Error('此导入器不接受脚本、外部图像或引用元素');
    if (/\btransform\s*=/.test(svg))
      throw new Error('暂不支持 SVG transform；请先展平坐标');
    const out = [], unsupported = new Set();
    const re = /<(line|rect|circle|polyline|text|path)\b([^>]*)>(?:([^<]*)<\/text>)?/gi;
    let m;
    while ((m = re.exec(svg))) {
      const a = attrs(m[2]);
      switch (m[1].toLowerCase()) {
        case 'line': {
          const l = line(number(a.x1), number(a.y1), number(a.x2), number(a.y2));
          if (l) out.push(l); else unsupported.add('斜线');
          break;
        }
        case 'rect': out.push({ kind: 'rect', x: number(a.x), y: number(a.y), width: number(a.width), height: number(a.height) }); break;
        case 'circle': out.push({ kind: 'circle', x: number(a.cx), y: number(a.cy), r: number(a.r),
          filled: a.fill !== 'none' && !/fill\s*:\s*none/.test(a.style || '') }); break;
        case 'polyline': {
          const nums = (a.points || '').match(/-?\d+(?:\.\d+)?/g) || [];
          for (let j = 0; j + 3 < nums.length; j += 2) {
            const l = line(+nums[j], +nums[j + 1], +nums[j + 2], +nums[j + 3]);
            if (l) out.push(l); else unsupported.add('斜向折线');
          }
          break;
        }
        case 'text': out.push({ kind: 'text', x: number(a.x), y: number(a.y), text: (m[3] || '').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').trim() }); break;
        case 'path': unsupported.add('SVG path'); break;
      }
    }
    Object.defineProperty(out, '_warnings', { value: [...unsupported].map(x => '未处理' + x), enumerable: false });
    return out;
  }

  function rasterToPrimitives(imageData, opts = {}) {
    const { width: w, height: h, data } = imageData || {};
    if (!Number.isInteger(w) || !Number.isInteger(h) || w < 1 || h < 1 || w * h > 4000000 || !data || data.length < w * h * 4)
      throw new Error('需要宽高有效且不超过 400 万像素的 ImageData');
    const threshold = opts.threshold === undefined ? 170 : opts.threshold;
    const minRun = opts.minRun === undefined ? 4 : opts.minRun;
    const dark = (x, y) => {
      const i = 4 * (y * w + x), alpha = data[i + 3] / 255;
      return alpha > 0.5 && (data[i] + data[i + 1] + data[i + 2]) / 3 < threshold;
    };
    const lines = [];
    const addLine = segment => {
      lines.push(segment);
      if (lines.length > 700) throw new Error('图形线段超过 700 条；请裁剪图片或降低分辨率');
    };
    for (let y = 0; y < h; y++) {
      let start = -1;
      for (let x = 0; x <= w; x++) {
        if (x < w && dark(x, y)) { if (start < 0) start = x; }
        else if (start >= 0) { if (x - start >= minRun) addLine(line(start, y, x - 1, y)); start = -1; }
      }
    }
    for (let x = 0; x < w; x++) {
      let start = -1;
      for (let y = 0; y <= h; y++) {
        if (y < h && dark(x, y)) { if (start < 0) start = y; }
        else if (start >= 0) { if (y - start >= minRun) addLine(line(x, start, x, y - 1)); start = -1; }
      }
    }
    return lines.filter(Boolean);
  }

  function isH(s) { return near(s.y1, s.y2, 0.01) && !near(s.x1, s.x2, 0.01); }
  function isV(s) { return near(s.x1, s.x2, 0.01) && !near(s.y1, s.y2, 0.01); }
  function contains(s, p) { return isH(s) ? near(p.y, s.y1) && between(p.x, s.x1, s.x2) : isV(s) && near(p.x, s.x1) && between(p.y, s.y1, s.y2); }
  function outwardLead(lines, p, direction) {
    return lines.some(s => contains(s, p) && (
      direction === 'left' ? isH(s) && Math.min(s.x1, s.x2) < p.x - 3 :
      direction === 'right' ? isH(s) && Math.max(s.x1, s.x2) > p.x + 3 :
      direction === 'up' ? isV(s) && Math.min(s.y1, s.y2) < p.y - 3 :
      isV(s) && Math.max(s.y1, s.y2) > p.y + 3));
  }
  function rectangleCandidates(primitives, lines) {
    const out = [];
    for (const p of primitives.filter(p => p.kind === 'rect')) {
      if (Number.isFinite(p.x + p.y + p.width + p.height)) out.push({ x: p.x, y: p.y, w: p.width, h: p.height, used: [] });
    }
    const hs = lines.map((s, i) => ({ s, i })).filter(a => isH(a.s));
    const vs = lines.map((s, i) => ({ s, i })).filter(a => isV(a.s));
    for (const top of hs) for (const bottom of hs) {
      const x = Math.min(top.s.x1, top.s.x2), end = Math.max(top.s.x1, top.s.x2);
      const y = top.s.y1, y2 = bottom.s.y1;
      if (y2 <= y + 5 || y2 - y > 35 || !near(x, Math.min(bottom.s.x1, bottom.s.x2)) || !near(end, Math.max(bottom.s.x1, bottom.s.x2))) continue;
      const left = vs.find(v => near(v.s.x1, x) && near(Math.min(v.s.y1, v.s.y2), y) && near(Math.max(v.s.y1, v.s.y2), y2));
      const right = vs.find(v => near(v.s.x1, end) && near(Math.min(v.s.y1, v.s.y2), y) && near(Math.max(v.s.y1, v.s.y2), y2));
      if (left && right) out.push({ x, y, w: end - x, h: y2 - y, used: [top.i, bottom.i, left.i, right.i] });
    }
    return out;
  }
  function detectSymbols(primitives, lines, opts) {
    const detected = [], used = new Set();
    for (const box of rectangleCandidates(primitives, lines)) {
      if (box.w < 10 || box.w > 70 || box.h < 6 || box.h > 35 || box.w / box.h < 1.4 || box.w / box.h > 8) continue;
      const cy = box.y + box.h / 2, left = point(box.x, cy), right = point(box.x + box.w, cy);
      if (!outwardLead(lines, left, 'left') || !outwardLead(lines, right, 'right')) continue;
      if (box.used.some(i => used.has(i))) continue;
      box.used.forEach(i => used.add(i));
      detected.push({ type: 'resistor', box, pins: [{ name: '1', ...left }, { name: '2', ...right }], score: opts.raster ? 0.55 : 0.72 });
    }
    const vs = lines.map((s, i) => ({ s, i })).filter(v => isV(v.s) && !used.has(v.i));
    for (const a of vs) for (const b of vs) {
      const x1 = a.s.x1, x2 = b.s.x1;
      const ya = Math.min(a.s.y1, a.s.y2), yb = Math.min(b.s.y1, b.s.y2);
      const len = Math.abs(a.s.y2 - a.s.y1);
      if (x2 <= x1 + 2 || x2 - x1 > 14 || len < 10 || len > 35 || !near(ya, yb) || !near(len, Math.abs(b.s.y2 - b.s.y1)) || used.has(a.i) || used.has(b.i)) continue;
      const y = ya + len / 2, p1 = point(x1, y), p2 = point(x2, y);
      if (!outwardLead(lines, p1, 'left') || !outwardLead(lines, p2, 'right')) continue;
      used.add(a.i); used.add(b.i);
      detected.push({ type: 'capacitor', box: { x: x1, y: ya, w: x2 - x1, h: len }, pins: [{ name: '+', ...p1 }, { name: '-', ...p2 }], score: opts.raster ? 0.53 : 0.7 });
    }
    return { detected, used };
  }

  function intersect(a, b, dots) {
    if (isH(a) && isH(b)) return near(a.y1, b.y1) && Math.max(Math.min(a.x1, a.x2), Math.min(b.x1, b.x2)) <= Math.min(Math.max(a.x1, a.x2), Math.max(b.x1, b.x2)) + EPS;
    if (isV(a) && isV(b)) return near(a.x1, b.x1) && Math.max(Math.min(a.y1, a.y2), Math.min(b.y1, b.y2)) <= Math.min(Math.max(a.y1, a.y2), Math.max(b.y1, b.y2)) + EPS;
    const h = isH(a) ? a : b, v = isV(a) ? a : b;
    if (!isH(h) || !isV(v)) return false;
    const p = point(v.x1, h.y1);
    if (!contains(h, p) || !contains(v, p)) return false;
    const endpoint = s => near(p.x, s.x1) && near(p.y, s.y1) || near(p.x, s.x2) && near(p.y, s.y2);
    return endpoint(h) || endpoint(v) || dots.some(d => dist(d, p) <= Math.max(2, d.r + 1));
  }
  function networks(lines, symbols, circles) {
    const pins = symbols.flatMap(c => c.pins.map(p => ({ ...p, endpoint: c.id + ':' + p.name })));
    const total = lines.length + pins.length, parent = Array.from({ length: total }, (_, i) => i);
    const root = i => { while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i]; } return i; };
    const join = (i, j) => { parent[root(i)] = root(j); };
    const dots = circles.filter(c => c.r >= 1 && c.r <= 5 && c.filled !== false);
    for (let i = 0; i < lines.length; i++) for (let j = i + 1; j < lines.length; j++) if (intersect(lines[i], lines[j], dots)) join(i, j);
    for (let i = 0; i < pins.length; i++) {
      for (let j = 0; j < lines.length; j++) if (contains(lines[j], pins[i])) join(lines.length + i, j);
      for (let j = 0; j < i; j++) if (dist(pins[i], pins[j]) <= EPS) join(lines.length + i, lines.length + j);
    }
    const groups = new Map();
    for (let i = 0; i < total; i++) {
      const k = root(i);
      if (!groups.has(k)) groups.set(k, { endpoints: [], wireSegments: [] });
      if (i < lines.length) groups.get(k).wireSegments.push(lines[i]);
      else groups.get(k).endpoints.push(pins[i - lines.length].endpoint);
    }
    const nets = [...groups.values()].filter(g => g.endpoints.length || g.wireSegments.length).map((g, i) => ({ id: 'N' + (i + 1), ...g }));
    const connections = [];
    for (const n of nets) for (let i = 1; i < n.endpoints.length; i++) connections.push([n.endpoints[0], n.endpoints[i], 'blue', []]);
    return { nets, connections };
  }

  function recognizePrimitives(primitives, options = {}) {
    if (!Array.isArray(primitives) || primitives.length > 700) throw new Error('图形基元必须是长度不超过 700 的数组');
    const lines = primitives.filter(p => p.kind === 'line' && [p.x1, p.y1, p.x2, p.y2].every(Number.isFinite) && (isH(p) || isV(p)));
    const circles = primitives.filter(p => p.kind === 'circle' && [p.x, p.y, p.r].every(Number.isFinite));
    const texts = primitives.filter(p => p.kind === 'text' && Number.isFinite(p.x + p.y) && p.text);
    const { detected, used } = detectSymbols(primitives, lines, options);
    if (Array.isArray(options.detections)) {
      for (const d of options.detections) {
        if (!d || !/^[a-z][a-z0-9_]*$/.test(d.type || '') || !Array.isArray(d.pins) || d.pins.length < 1 ||
          !d.pins.every(p => /^[A-Za-z0-9+_-]+$/.test(p.name || '') && Number.isFinite(p.x) && Number.isFinite(p.y)))
          throw new Error('外部检测器必须返回 type 和有坐标的具名 pins');
        const box = d.box || { x: d.pins[0].x, y: d.pins[0].y, w: 0, h: 0 };
        const center = point(box.x + box.w / 2, box.y + box.h / 2);
        const duplicate = detected.findIndex(c => c.type === d.type && dist(center, point(c.box.x + c.box.w / 2, c.box.y + c.box.h / 2)) < 12);
        if (duplicate >= 0) detected.splice(duplicate, 1);
        detected.push({ type: d.type, box, pins: d.pins, score: d.score, external: true });
      }
    }
    const counters = {}, usedIds = new Set();
    for (const c of detected) {
      const prefix = c.type === 'resistor' ? 'R' : c.type === 'capacitor' ? 'C' : 'U';
      const center = point(c.box.x + c.box.w / 2, c.box.y + c.box.h / 2);
      const labels = texts.filter(t => new RegExp('^' + prefix + '\\d+$', 'i').test(t.text) && dist(t, center) < 65).sort((a, b) => dist(a, center) - dist(b, center));
      const label = labels.find(t => !usedIds.has(t.text.toUpperCase()));
      if (label) { c.id = label.text.toUpperCase(); usedIds.add(c.id); }
      else { do { counters[prefix] = (counters[prefix] || 0) + 1; c.id = prefix + counters[prefix]; } while (usedIds.has(c.id)); usedIds.add(c.id); }
    }
    const wireLines = lines.filter((_, i) => !used.has(i));
    const graph = networks(wireLines, detected, circles);
    const counts = { resistor: 0, capacitor: 0 };
    const parts = detected.map(c => {
      counts[c.type] = (counts[c.type] || 0) + 1;
      return { type: c.type, id: c.id, left: Math.round(c.box.x * 2), top: Math.round(c.box.y * 2), rotate: 0, attrs: {} };
    });
    const issues = [];
    if (!parts.length) issues.push({ code: 'NO_SUPPORTED_SYMBOL', message: '未识别到受支持的电阻或电容' });
    if (options.raster) issues.push({ code: 'NO_RASTER_OCR', message: '像素图文字未识别；位号和数值需要人工确认' });
    if (primitives._warnings) for (const w of primitives._warnings) issues.push({ code: 'UNSUPPORTED_SVG', message: w });
    for (const net of graph.nets) if (net.endpoints.length === 1) issues.push({ code: 'UNCONNECTED_PIN', endpoint: net.endpoints[0], message: '引脚没有连接到其他元件' });
    const circuit = { version: 2, title: options.title || '图像识别候选', parts, connections: graph.connections,
      provenance: { origin: 'image', ...(options.sourceId ? { source_id: options.sourceId } : {}) } };
    return { method: options.detections ? 'external-detector+heuristic-wires' : 'geometry-heuristic',
      circuit, detections: detected.map(c => ({ type: c.type, id: c.id, box: c.box, pins: c.pins, score: c.score,
        scoreMeaning: c.external ? 'external detector score; calibration unknown' : 'uncalibrated heuristic' })),
      topology: graph.nets, texts, issues, supportedSymbols: ['resistor', 'capacitor'] };
  }
  function recognizeSvg(svg, options = {}) { return recognizePrimitives(svgToPrimitives(svg), options); }
  function rasterPrimitivesOrLimit(imageData, options) {
    try { return { primitives: rasterToPrimitives(imageData, options), limit: null }; }
    catch (error) {
      if (!error.message.includes('线段超过 700 条')) throw error;
      return { primitives: [], limit: error.message };
    }
  }
  function recognizeRaster(imageData, options = {}) {
    const input = rasterPrimitivesOrLimit(imageData, options);
    const result = recognizePrimitives(input.primitives, { ...options, raster: true });
    if (input.limit) result.issues.push({ code: 'GEOMETRY_LIMIT', message: input.limit + '；未推断元件或网络，可查看原图并人工补充' });
    return result;
  }
  async function recognizeWithDetector(input, detector, options = {}) {
    if (!detector || typeof detector.detect !== 'function') throw new Error('需要已配置的外部检测器 detect(input)');
    const raster = typeof input !== 'string' && !Array.isArray(input);
    const extracted = raster ? rasterPrimitivesOrLimit(input, options) : null;
    const primitives = extracted ? extracted.primitives : Array.isArray(input) ? input : svgToPrimitives(input);
    const detections = await detector.detect(input);
    if (!Array.isArray(detections)) throw new Error('检测器输出必须是数组');
    const result = recognizePrimitives(primitives, { ...options, detections, raster });
    if (extracted?.limit) {
      result.method = 'external-detector';
      result.issues.push({ code: 'GEOMETRY_LIMIT', message: extracted.limit + '；未推断导线或网络，检测元件仍需人工连线' });
    }
    return result;
  }
  async function createTesseractAdapter(Tesseract, options = {}) {
    if (!Tesseract || typeof Tesseract.createWorker !== 'function') throw new Error('未加载 Tesseract.js OCR 运行时');
    if (!options.langPath) throw new Error('OCR 需要显式配置本地语言权重 langPath');
    const workerOptions = { langPath: options.langPath, gzip: options.gzip !== false };
    for (const key of ['corePath', 'workerPath', 'cachePath', 'cacheMethod', 'logger'])
      if (options[key] !== undefined) workerOptions[key] = options[key];
    const language = options.language || 'eng';
    const worker = await Tesseract.createWorker(language, 1, workerOptions);
    try {
      await worker.setParameters({ tessedit_pageseg_mode: String(options.psm === undefined ? 11 : options.psm) });
    } catch (e) { await worker.terminate(); throw e; }
    return {
      recognize: async (input, recognizeOptions = {}) => {
        const result = await worker.recognize(input, recognizeOptions, { blocks: true, text: true });
        const data = result.data || {}, words = [];
        for (const block of data.blocks || []) for (const paragraph of block.paragraphs || [])
          for (const line of paragraph.lines || []) for (const word of line.words || [])
            if (word.text && word.bbox) words.push({ text: word.text.trim(), bbox: { ...word.bbox }, confidence: word.confidence });
        return { method: 'tesseract.js', language, text: data.text || '', confidence: data.confidence, words };
      },
      terminate: () => worker.terminate(),
    };
  }
  async function recognizeRasterWithOcr(imageData, imageSource, ocrAdapter, options = {}) {
    if (!ocrAdapter || typeof ocrAdapter.recognize !== 'function') throw new Error('需要已配置的 OCR 适配器');
    const ocr = await ocrAdapter.recognize(imageSource);
    const input = rasterPrimitivesOrLimit(imageData, options), primitives = input.primitives;
    for (const word of ocr.words || []) if (word.bbox && Number.isFinite(word.bbox.x0) && Number.isFinite(word.bbox.y0))
      primitives.push({ kind: 'text', x: word.bbox.x0, y: word.bbox.y0, text: word.text });
    const result = recognizePrimitives(primitives, { ...options, raster: true });
    result.issues = result.issues.filter(issue => issue.code !== 'NO_RASTER_OCR');
    result.issues.push({ code: 'OCR_REVIEW_REQUIRED', message: 'OCR 已运行；识别文字与器件数值仍需人工确认' });
    if (input.limit) result.issues.push({ code: 'GEOMETRY_LIMIT', message: input.limit + '；未推断元件或网络，可查看原图并人工补充' });
    result.ocr = ocr;
    return result;
  }
  return { svgToPrimitives, rasterToPrimitives, recognizePrimitives, recognizeSvg, recognizeRaster,
    recognizeWithDetector, createTesseractAdapter, recognizeRasterWithOcr };
});
