// Reuses @tscircuit/eval CircuitRunner and circuit-to-svg; see attribution manifest.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import util from 'node:util';

const writeResult = (result) => process.stdout.write(JSON.stringify(result));
const logs = [];
for (const level of ['log', 'warn', 'error', 'info', 'debug']) {
  console[level] = (...args) => { if (logs.length < 50) logs.push({level, message: util.format(...args).slice(0, 4000)}); };
}
globalThis.fetch = async () => { throw new Error('本机离线模式不访问网络或下载元件。'); };
globalThis.__DISABLE_CDN_LOADING__ = true;
const result = {ok: false, circuit_json: [], schematic_svg: '', diagnostics: logs,
  runtime: {engine: 'tscircuit', node_version: process.version, offline: true, versions: {}}};
let phase = 'runtime';
try {
  const prefix = path.resolve(process.argv[2]);
  const modules = path.join(prefix, 'node_modules');
  for (const name of ['@tscircuit/eval', '@tscircuit/core', 'circuit-to-svg']) {
    const metadata = JSON.parse(fs.readFileSync(path.join(modules, name, 'package.json'), 'utf8'));
    result.runtime.versions[name] = metadata.version;
  }
  const {CircuitRunner} = await import(pathToFileURL(path.join(modules, '@tscircuit/eval/dist/lib/index.js')));
  const {convertCircuitJsonToSchematicSvg} = await import(pathToFileURL(path.join(modules, 'circuit-to-svg/dist/index.js')));
  const request = JSON.parse(fs.readFileSync(0, 'utf8'));
  phase = 'code';
  const runner = new CircuitRunner();
  await runner.setDisableCdnLoading(true);
  await runner.executeWithFsMap({fsMap: {'main.tsx': request.code}, mainComponentPath: 'main.tsx', name: request.name});
  await runner.renderUntilSettled();
  result.circuit_json = await runner.getCircuitJson();
  const errors = result.circuit_json.filter(row => /^(source_|schematic_).*error$/.test(row.type));
  result.diagnostics.push(...errors);
  result.schematic_svg = convertCircuitJsonToSchematicSvg(result.circuit_json, {width: 1000, height: 650});
  if (errors.length) result.error = {code: 'UPSTREAM_ERROR', message: '上游源电路或原理图包含错误记录', path: 'main.tsx'};
  else result.ok = true;
} catch (error) {
  result.error = {code: phase === 'runtime' ? 'runtime_unavailable' : 'CODE_EXECUTION_FAILED',
    message: String(error.message || error).slice(0, 12000), path: phase === 'code' ? 'main.tsx' : ''};
  result.diagnostics.push(result.error);
}
writeResult(result);
