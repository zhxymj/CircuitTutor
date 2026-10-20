/* Uses only locally installed runtime + language data; no CDN/network calls. */
const fs = require('node:fs');
const path = require('node:path');
const I = require('../web/image_recognition.js');
const root = process.env.ZH_VISION_ROOT;
if (!root) throw new Error('Set ZH_VISION_ROOT to the vision-ocr installation directory');
const Tesseract = require(path.join(root, 'node_modules', 'tesseract.js'));
const { PNG } = require(path.join(root, 'node_modules', 'pngjs'));
const samples = path.join(__dirname, '..', 'data', 'image_samples', 'sketch2schematic');
const gt = require(path.join(samples, 'manual_text_labels.json'));

function normalizedCrop(source, rect) {
  const scale = 3, pad = 8;
  const out = new PNG({ width: rect.width * scale + pad * 2, height: rect.height * scale + pad * 2 });
  out.data.fill(255);
  for (let y = 0; y < rect.height; y++) for (let x = 0; x < rect.width; x++) {
    const j = ((rect.top + y) * source.width + rect.left + x) * 4;
    const ink = (source.data[j] + source.data[j + 1] + source.data[j + 2]) / 3 < 190 ? 0 : 255;
    for (let yy = 0; yy < scale; yy++) for (let xx = 0; xx < scale; xx++) {
      const k = ((pad + y * scale + yy) * out.width + pad + x * scale + xx) * 4;
      out.data[k] = out.data[k + 1] = out.data[k + 2] = ink;
    }
  }
  return PNG.sync.write(out);
}

(async () => {
  const started = Date.now();
  const workerOptions = {
    langPath: path.join(root, 'node_modules', '@tesseract.js-data', 'eng', '4.0.0_best_int'),
    cacheMethod: 'none', psm: 7,
  };
  const adapter = await I.createTesseractAdapter(Tesseract, workerOptions);
  const results = [];
  const sourcePng = PNG.sync.read(fs.readFileSync(path.join(samples, gt.image)));
  try {
    for (const label of gt.labels) {
      const raw = await adapter.recognize(path.join(samples, gt.image), { rectangle: label.rectangle });
      const prediction = raw.text.trim().replace(/\s+/g, '');
      const normalized = await adapter.recognize(normalizedCrop(sourcePng, label.rectangle));
      const normalizedPrediction = normalized.text.trim().replace(/\s+/g, '');
      results.push({ expected: label.text, prediction, exact_match: prediction === label.text,
        confidence: raw.confidence, normalized_prediction: normalizedPrediction,
        normalized_exact_match: normalizedPrediction === label.text,
        normalized_confidence: normalized.confidence, rectangle: label.rectangle });
    }
  } finally { await adapter.terminate(); }
  const output = {
    date: new Date().toISOString(), runtime: 'tesseract.js@7.0.0', language: 'eng/4.0.0_best_int',
    input: 'MIT upstream printed iec-structured.png, manual oracle text crops',
    scope: gt.scope, elapsed_ms: Date.now() - started,
    matched: results.filter(r => r.exact_match).length, total: results.length, results,
    normalized_matched: results.filter(r => r.normalized_exact_match).length,
    normalization: 'Fixed threshold 190, nearest-neighbor 3x and white margin 8px, applied identically to every oracle text crop; same development image, no holdout claim.',
  };
  fs.writeFileSync(path.join(__dirname, '..', 'evaluation', 'image_ocr_results.json'), JSON.stringify(output, null, 2) + '\n');
  console.log(JSON.stringify(output, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
