'use strict';

const fs = require('fs');
const path = require('path');

const SCHEMA_VERSION = '1.0';
const SEVERITIES = ['P0', 'P1', 'P2', 'P3'];

class AuditUsageError extends Error {
  constructor(message) {
    super(message);
    this.name = 'AuditUsageError';
    this.exitCode = 2;
  }
}

function parseAuditArgs(argv) {
  const args = Array.isArray(argv) ? argv : [];
  let jsonOut = null;
  for (let index = 0; index < args.length; index++) {
    const arg = args[index];
    if (arg !== '--json-out') {
      throw new AuditUsageError(`未知参数: ${arg}`);
    }
    const value = args[index + 1];
    if (!value || value.startsWith('--')) {
      throw new AuditUsageError('--json-out 需要跟一个 JSON 文件路径');
    }
    jsonOut = value;
    index++;
  }
  return { jsonOut };
}

function connectedPinRefs(connections) {
  const refs = new Set();
  for (const connection of connections || []) {
    if (!Array.isArray(connection)) continue;
    for (const ref of connection.slice(0, 2)) {
      if (typeof ref === 'string' && ref.includes(':')) refs.add(ref);
    }
  }
  return refs;
}

// 兼容早期调用方；新代码应使用签名更明确的 connectedPinRefs(connections)。
function collectConnectedRefs(circuit) {
  return connectedPinRefs(circuit && circuit.connections);
}

function indexRefNetworks(diag) {
  const result = new Map();
  const routed = (diag && diag.netRouted) || [];
  routed.forEach((network, index) => {
    const netId = index + 1;
    for (const ref of network.pins || []) {
      if (!result.has(ref)) result.set(ref, new Set());
      result.get(ref).add(netId);
    }
  });
  return result;
}

function countSeverities(findings) {
  const counts = { P0: 0, P1: 0, P2: 0, P3: 0 };
  for (const finding of findings || []) {
    if (finding && SEVERITIES.includes(finding.sev)) counts[finding.sev]++;
  }
  return counts;
}

function normalizeAssertions(assertions) {
  const pass = Number.isFinite(Number(assertions && assertions.pass))
    ? Math.max(0, Math.trunc(Number(assertions.pass)))
    : 0;
  const fail = Number.isFinite(Number(assertions && assertions.fail))
    ? Math.max(0, Math.trunc(Number(assertions.fail)))
    : 0;
  return { total: pass + fail, pass, fail };
}

function buildAuditReport({ assertions, metrics, findings } = {}) {
  const normalizedFindings = Array.isArray(findings) ? findings : [];
  const normalizedAssertions = normalizeAssertions(assertions);
  const severityCounts = countSeverities(normalizedFindings);
  const blocked = normalizedAssertions.fail > 0 ||
    severityCounts.P0 > 0 || severityCounts.P1 > 0;
  return {
    schema_version: SCHEMA_VERSION,
    status: blocked ? 'fail' : 'pass',
    severity_counts: severityCounts,
    assertions: normalizedAssertions,
    metrics: metrics && typeof metrics === 'object' ? metrics : {},
    findings: normalizedFindings,
  };
}

function exitCodeForReport(report) {
  return report && report.status === 'pass' ? 0 : 1;
}

function writeAuditReport(report, outputPath) {
  if (!outputPath) throw new AuditUsageError('--json-out 需要跟一个 JSON 文件路径');
  const resolved = path.resolve(outputPath);
  fs.mkdirSync(path.dirname(resolved), { recursive: true });
  fs.writeFileSync(resolved, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  return resolved;
}

module.exports = {
  AuditUsageError,
  SCHEMA_VERSION,
  buildAuditReport,
  collectConnectedRefs,
  connectedPinRefs,
  countSeverities,
  exitCodeForReport,
  indexRefNetworks,
  parseAuditArgs,
  writeAuditReport,
};
