'use strict';

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

// 这些测试约束机器可读审计的外部行为。若报告字段、阻断规则或 CLI
// 参数检查被移除，发布质量门应立即失败。
const {
  AuditUsageError,
  buildAuditReport,
  connectedPinRefs,
  exitCodeForReport,
  parseAuditArgs,
  writeAuditReport,
} = require('../tools/audit_contract.js');

// 公开接口直接接收 connections，只返回前两个端点中的精确 pin ref，
// 去重但不把同一元件的其他引脚扩入集合。
assert.deepStrictEqual(
  [...connectedPinRefs([
    ['U1:IN+', 'BAT1:+', 'red', []],
    ['BAT1:+', 'GND1:1'],
    ['U1:IN+', null],
    { from: '不支持的对象形态' },
  ])],
  ['U1:IN+', 'BAT1:+', 'GND1:1'],
);

function assertRequiredShape(report) {
  for (const key of [
    'schema_version',
    'status',
    'severity_counts',
    'assertions',
    'metrics',
    'findings',
  ]) {
    assert.ok(Object.prototype.hasOwnProperty.call(report, key), `审计报告缺少 ${key}`);
  }
}

// P2/P3 是观察项，不应阻断发布。
const advisory = buildAuditReport({
  assertions: { pass: 7, fail: 0 },
  metrics: { pinUncovered: 0 },
  findings: [
    { sev: 'P2', code: '观察项' },
    { sev: 'P3', code: '建议项' },
  ],
});
assertRequiredShape(advisory);
assert.strictEqual(advisory.status, 'pass');
assert.strictEqual(exitCodeForReport(advisory), 0);
assert.deepStrictEqual(advisory.severity_counts, { P0: 0, P1: 0, P2: 1, P3: 1 });
assert.deepStrictEqual(advisory.assertions, { total: 7, pass: 7, fail: 0 });

// P0/P1 任一出现都必须阻断。
for (const severity of ['P0', 'P1']) {
  const blocked = buildAuditReport({
    assertions: { pass: 3, fail: 0 },
    metrics: {},
    findings: [{ sev: severity, code: `${severity}示例` }],
  });
  assert.strictEqual(blocked.status, 'fail', `${severity} 必须使报告失败`);
  assert.strictEqual(exitCodeForReport(blocked), 1, `${severity} 必须返回退出码 1`);
}

// 断言失败即使没有 finding，也必须阻断。
const assertionFailure = buildAuditReport({
  assertions: { pass: 2, fail: 1 },
  metrics: {},
  findings: [],
});
assert.strictEqual(assertionFailure.status, 'fail');
assert.strictEqual(exitCodeForReport(assertionFailure), 1);
assert.strictEqual(assertionFailure.assertions.total, 3);
assert.strictEqual(assertionFailure.assertions.fail, 1);

// --json-out 必须携带路径；契约层以可识别的用法错误表达退出码 2。
assert.throws(
  () => parseAuditArgs(['--json-out']),
  error => error instanceof AuditUsageError && error.exitCode === 2,
);
assert.deepStrictEqual(parseAuditArgs(['--json-out', 'report.json']), { jsonOut: 'report.json' });

// 写盘内容必须就是完整报告；仅使用系统临时目录并在测试结束清理。
const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'zhihui-audit-contract-'));
try {
  const output = path.join(tempDir, 'nested', 'audit.json');
  writeAuditReport(advisory, output);
  assert.deepStrictEqual(JSON.parse(fs.readFileSync(output, 'utf8')), advisory);
} finally {
  fs.rmSync(tempDir, { recursive: true, force: true });
}

// CLI 层也必须兑现缺参退出码，且不能开始渲染审计。
const auditScript = path.join(__dirname, 'test_audit.js');
const missingValue = spawnSync(process.execPath, [auditScript, '--json-out'], {
  encoding: 'utf8',
  cwd: path.join(__dirname, '..'),
});
assert.strictEqual(
  missingValue.status,
  2,
  `test_audit.js --json-out 缺值应返回 2，实际 ${missingValue.status}\n${missingValue.stdout}\n${missingValue.stderr}`,
);

console.log('审计质量契约 ✔  报告字段、P0/P1/断言阻断、P2/P3放行、CLI缺参退出码均符合约定');
