/* Fixed heuristic/OCR baseline on the original CC-BY-4.0 CGHD subset.
 * No tuning, trained component detector, or fabricated network ground truth.
 */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const I = require('../web/image_recognition.js');
const currentHoldout = process.argv.includes('--holdout-current');
const desktopRoot = process.env.USERPROFILE && path.join(process.env.USERPROFILE, 'Desktop', '智绘电路工具链.zip', '智绘电路工具链', 'vision-ocr');
const root = process.env.ZH_VISION_ROOT || desktopRoot;
if (!root || !fs.existsSync(path.join(root, 'node_modules', 'tesseract.js')))
  throw new Error('Set ZH_VISION_ROOT to an extracted vision-ocr directory containing node_modules/tesseract.js');
const jpeg = require(path.join(root, 'node_modules', 'jpeg-js'));
const { PNG } = require(path.join(root, 'node_modules', 'pngjs'));
const Tesseract = require(path.join(root, 'node_modules', 'tesseract.js'));
const sampleRoot = path.resolve(__dirname, '../data/image_samples/cghd_v15');
const manifestPath = path.join(sampleRoot, 'manifest.json');
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
if (manifest.samples.length !== 30 && !process.argv.includes('--allow-partial')) throw new Error('30 paired samples required; current=' + manifest.samples.length);
const samples = currentHoldout ? manifest.samples.filter(sample => sample.split === 'holdout') : manifest.samples;
if (currentHoldout && (samples.length !== 10 || new Set(samples.map(sample => sample.drafter)).size !== 10))
  throw new Error('Current holdout recheck requires 10 drafter-disjoint samples');
const sha256 = filename => crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex');

function exifOrientation(buffer) {
  for (let offset = 2; offset + 4 < buffer.length;) {
    if (buffer[offset] !== 255) break;
    const marker = buffer[offset + 1];
    if (marker === 0xda || marker === 0xd9) break;
    const length = buffer.readUInt16BE(offset + 2);
    if (length < 2 || offset + 2 + length > buffer.length) break;
    if (marker === 0xe1 && buffer.toString('ascii', offset + 4, offset + 10) === 'Exif\u0000\u0000') {
      const tiff = offset + 10, little = buffer.toString('ascii', tiff, tiff + 2) === 'II';
      const u16 = n => little ? buffer.readUInt16LE(n) : buffer.readUInt16BE(n);
      const u32 = n => little ? buffer.readUInt32LE(n) : buffer.readUInt32BE(n);
      const directory = tiff + u32(tiff + 4);
      if (directory + 2 >= offset + length + 2) return 1;
      for (let i = 0; i < u16(directory); i++) {
        const entry = directory + 2 + 12 * i;
        if (entry + 12 > offset + length + 2) break;
        if (u16(entry) === 0x112) return u16(entry + 8);
      }
    }
    offset += 2 + length;
  }
  return 1;
}
function orientPixels(source, orientation) {
  if (orientation === 1) return source;
  const swapped = orientation >= 5 && orientation <= 8;
  const width = swapped ? source.height : source.width, height = swapped ? source.width : source.height;
  const data = new Uint8Array(width * height * 4), w = source.width, h = source.height;
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    const coordinates = {
      2: [w - 1 - x, y], 3: [w - 1 - x, h - 1 - y], 4: [x, h - 1 - y],
      5: [y, x], 6: [y, h - 1 - x], 7: [w - 1 - y, h - 1 - x], 8: [w - 1 - y, x],
    }[orientation] || [x, y];
    const j = (coordinates[1] * w + coordinates[0]) * 4;
    data.set(source.data.subarray(j, j + 4), (y * width + x) * 4);
  }
  return { width, height, data };
}

function scaleImage(image) {
  const ratio = Math.min(1, 700 / Math.max(image.width, image.height));
  const width = Math.max(1, Math.round(image.width * ratio)), height = Math.max(1, Math.round(image.height * ratio));
  const data = new Uint8ClampedArray(width * height * 4);
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    const j = (Math.min(image.height - 1, Math.floor(y / ratio)) * image.width + Math.min(image.width - 1, Math.floor(x / ratio))) * 4;
    data.set(image.data.subarray(j, j + 4), (y * width + x) * 4);
  }
  return { image: { width, height, data }, ratio };
}
function iou(a, b) {
  const intersection = Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x)) * Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
  return intersection / (a.w * a.h + b.w * b.h - intersection || 1);
}
function editDistance(a, b) {
  let row = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const next = [i];
    for (let j = 1; j <= b.length; j++) next[j] = Math.min(next[j - 1] + 1, row[j] + 1, row[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    row = next;
  }
  return row[b.length];
}
function aggregate(cases) {
  const result = { images: cases.length, geometric_failures: cases.filter(c => c.geometry_error).length,
    component_tp: 0, component_fp: 0, component_fn: 0, oracle_ocr_total: 0, oracle_ocr_exact: 0,
    oracle_ocr_errors: 0, text_characters: 0, character_edits: 0,
    inferred_connections: 0, network_ground_truth_available: false, network_accuracy: null };
  for (const c of cases) {
    result.component_tp += c.component.tp; result.component_fp += c.component.fp; result.component_fn += c.component.fn;
    result.inferred_connections += c.connections.length;
    for (const t of c.ocr) {
      result.oracle_ocr_total++; result.oracle_ocr_exact += Number(t.exact_match);
      result.oracle_ocr_errors += Number(Boolean(t.error)); result.text_characters += t.expected.length;
      result.character_edits += editDistance(t.expected, t.prediction);
    }
  }
  result.component_precision = result.component_tp + result.component_fp ? result.component_tp / (result.component_tp + result.component_fp) : null;
  result.component_recall = result.component_tp + result.component_fn ? result.component_tp / (result.component_tp + result.component_fn) : null;
  result.oracle_ocr_character_error_rate = result.text_characters ? result.character_edits / result.text_characters : null;
  return result;
}
(async () => {
  const started = Date.now(), cases = [];
  const adapter = await I.createTesseractAdapter(Tesseract, {
    langPath: path.join(root, 'node_modules', '@tesseract.js-data', 'eng', '4.0.0_best_int'), cacheMethod: 'none', psm: 7,
  });
  try {
    for (const sample of samples) {
      const filename = path.join(sampleRoot, sample.image), raw = fs.readFileSync(filename);
      const annotationFile = path.join(sampleRoot, sample.annotation);
      const imageHash = sha256(filename), annotationHash = sha256(annotationFile);
      if (currentHoldout && (!sample.files.some(file => file.path === sample.image && file.sha256 === imageHash) ||
        !sample.files.some(file => file.path === sample.annotation && file.sha256 === annotationHash)))
        throw new Error('Manifest SHA256 mismatch for ' + sample.id);
      const orientation = exifOrientation(raw);
      const decoded = orientPixels(jpeg.decode(raw, { useTArray: true, maxMemoryUsageInMB: 512 }), orientation);
      const xml = fs.readFileSync(annotationFile, 'utf8');
      const annotationDimensions = [Number(xml.match(/<width>(\d+)<\/width>/)?.[1]), Number(xml.match(/<height>(\d+)<\/height>/)?.[1])];
      const dimensionMismatch = decoded.width !== annotationDimensions[0] || decoded.height !== annotationDimensions[1];
      const ocrImage = PNG.sync.write({ width: decoded.width, height: decoded.height, data: decoded.data });
      const scaled = scaleImage(decoded), ocr = [];
      let recognized = null, geometryError = null;
      try {
        recognized = I.recognizeRaster(scaled.image, { sourceId: sample.id, threshold: 170, minRun: 4 });
        geometryError = recognized.issues.find(issue => issue.code === 'GEOMETRY_LIMIT')?.message || null;
      }
      catch (error) { geometryError = error.message; }
      const gt = sample.objects.filter(o => ['resistor','capacitor.unpolarized'].includes(o.name)).map(o => ({
        type: o.name === 'resistor' ? 'resistor' : 'capacitor',
        box: { x: +o.bbox.xmin, y: +o.bbox.ymin, w: +o.bbox.xmax - +o.bbox.xmin, h: +o.bbox.ymax - +o.bbox.ymin },
      }));
      const detections = (recognized?.detections || []).map(d => ({ type: d.type, score: d.score,
        box: { x: d.box.x / scaled.ratio, y: d.box.y / scaled.ratio, w: d.box.w / scaled.ratio, h: d.box.h / scaled.ratio } }));
      const matched = new Set(); let tp = 0;
      for (const d of detections) {
        let best = -1, overlap = 0;
        gt.forEach((g, i) => { const score = d.type === g.type && !matched.has(i) ? iou(d.box, g.box) : 0; if (score > overlap) { overlap = score; best = i; } });
        if (overlap >= 0.5) { matched.add(best); tp++; }
      }
      for (const object of sample.objects.filter(o => o.name === 'text' && o.text)) {
        const expected = object.text.replace(/\s+/g, '');
        const left = Math.max(0, Math.floor(+object.bbox.xmin)), top = Math.max(0, Math.floor(+object.bbox.ymin));
        const rectangle = { left, top, width: Math.min(decoded.width - left, Math.ceil(+object.bbox.xmax) - left),
          height: Math.min(decoded.height - top, Math.ceil(+object.bbox.ymax) - top) };
        if (dimensionMismatch || rectangle.width <= 0 || rectangle.height <= 0) {
          ocr.push({ expected, prediction: '', exact_match: false, error: 'Annotation dimensions/crop do not match EXIF-oriented pixels', rectangle });
          continue;
        }
        try {
          const output = await adapter.recognize(ocrImage, { rectangle });
          const prediction = output.text.trim().replace(/\s+/g, '');
          ocr.push({ expected, prediction, exact_match: prediction === expected, confidence: output.confidence, rectangle });
        } catch (error) { ocr.push({ expected, prediction: '', exact_match: false, error: error.message, rectangle }); }
      }
      cases.push({ id: sample.id, drafter: sample.drafter, split: sample.split, image: sample.image, annotation: sample.annotation,
        image_sha256: imageHash, annotation_sha256: annotationHash,
        dimensions: [decoded.width, decoded.height], working_dimensions: [scaled.image.width, scaled.image.height],
        exif_orientation: orientation, annotation_dimensions: annotationDimensions, dimension_mismatch: dimensionMismatch,
        geometry_error: geometryError, component: { tp, fp: detections.length - tp, fn: gt.length - tp, gt_count: gt.length, detections },
        ocr, connections: recognized?.circuit.connections || [], network_ground_truth: null,
        network_metric_status: 'not_scorable: VOC/XML subset supplies symbol/text/junction boxes, no paired pin/net graph ground truth',
        manual_correction_status: 'not_performed' });
      console.log('Evaluated ' + sample.id + ': component TP=' + tp + '/' + gt.length + ', OCR=' + ocr.filter(t => t.exact_match).length + '/' + ocr.length);
    }
  } finally { await adapter.terminate(); }
  const result = { date: new Date().toISOString(), source: manifest.source, license: manifest.license,
    method: 'geometry-heuristic for resistor/unpolarized capacitor, Tesseract.js7 eng oracle text boxes; no trained component weights',
    options: { exif_orientation_applied: true, ocr_pixel_source: 'PNG encoding of EXIF-oriented original JPEG pixels; no OCR scaling/threshold tuning', max_dimension: 700, resize: 'nearest-neighbor', threshold: 170, minRun: 4, matching_iou: 0.5, text_psm: 7 },
    limits: 'Original image subset is drafter-disjoint. Component metric covers only two classes; OCR uses GT regions and is not end-to-end text detection. Network ground truth is missing; inferred connection counts are never network accuracy.',
    elapsed_ms: Date.now() - started, all: aggregate(cases), development: aggregate(cases.filter(c => c.split === 'development')),
    holdout: aggregate(cases.filter(c => c.split === 'holdout')), cases };
  const outputPath = path.resolve(__dirname, '../evaluation/image_public_results.json');
  if (currentHoldout) {
    const previous = JSON.parse(fs.readFileSync(outputPath, 'utf8'));
    if (previous.cases.length !== 30 || previous.holdout.images !== 10)
      throw new Error('Archived 30-image baseline is missing; current recheck will not replace it');
    const raw = result.holdout;
    previous.current_holdout_full_recheck = {
      run_utc: result.date, command: 'node tests/evaluate_image_public.js --holdout-current',
      runtime_root: root, node_version: process.version,
      tesseract_version: JSON.parse(fs.readFileSync(path.join(root, 'node_modules', 'tesseract.js', 'package.json'), 'utf8')).version,
      source: manifest.source, license: manifest.license, manifest: 'data/image_samples/cghd_v15/manifest.json',
      manifest_sha256: sha256(manifestPath), recognizer: 'web/image_recognition.js',
      recognizer_sha256: sha256(path.resolve(__dirname, '../web/image_recognition.js')),
      method: result.method, options: result.options, limits: result.limits,
      sample_selection: '10 holdout images from 10 disjoint drafters; all source image/XML SHA256 values checked against manifest',
      elapsed_ms: result.elapsed_ms, raw_metrics: raw, cases,
      archived_holdout_reference: { run_utc: previous.date, oracle_ocr_exact: previous.holdout.oracle_ocr_exact,
        oracle_ocr_total: previous.holdout.oracle_ocr_total, character_edits: previous.holdout.character_edits,
        text_characters: previous.holdout.text_characters },
      ocr_status: raw.oracle_ocr_errors ? 'incomplete: some oracle text-box recognitions raised errors' : 'measured: oracle text-box OCR only',
      manual_correction_status: 'not_performed: no corrected candidate or validation-gate evidence for any holdout sample',
      network_metric_status: 'not_scorable: no paired pin/net graph ground truth',
    };
    fs.writeFileSync(outputPath, JSON.stringify(previous, null, 2) + '\n');
    console.log(JSON.stringify({ current_holdout_full_recheck: raw, archived_holdout_reference: previous.current_holdout_full_recheck.archived_holdout_reference }, null, 2));
  } else {
    fs.writeFileSync(outputPath, JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify({ all: result.all, development: result.development, holdout: result.holdout }, null, 2));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
