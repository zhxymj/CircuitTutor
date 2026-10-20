"""Offline code-to-circuit bridge using the installed upstream tscircuit runtime."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

from core.external_import import ImportProblem, _tscircuit
from core.upstream_vision import _communicate_worker

ROOT = Path(__file__).resolve().parents[1]
WORKER_TIMEOUT_SECONDS = 60
_LOCK = threading.BoundedSemaphore(1)


class TscircuitError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _runtime_paths():
    configured = os.environ.get('TSCIRCUIT_PREFIX')
    candidates = ([Path(configured).expanduser()] if configured else
                  [ROOT / 'runtime/tscircuit', ROOT.parents[1] / '智绘电路工具链/open-source/tscircuit'])
    prefix = next((p for p in candidates if (p / 'node_modules/@tscircuit/eval/package.json').is_file()), candidates[0])
    bundled_node = prefix / 'node.exe'
    node = str(bundled_node) if bundled_node.is_file() else os.environ.get('TSCIRCUIT_NODE') or shutil.which('node')
    if not node:
        default = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'nodejs/node.exe'
        node = str(default) if default.is_file() else ''
    worker = ROOT / 'tools/tscircuit_worker.mjs'
    if not node or not Path(node).is_file() or not worker.is_file() or not (prefix / 'node_modules/@tscircuit/eval/package.json').is_file():
        raise TscircuitError('runtime_unavailable', '本机 tscircuit 运行库未就绪，请核对 Node 与 TSCIRCUIT_PREFIX。')
    return str(node), prefix.resolve(), worker


def generate_tscircuit(code, *, name='', is_cancelled=None):
    """Generate upstream JSON/SVG and convert the entire graph into a review candidate."""
    if not isinstance(code, str) or not code.strip() or len(code.encode('utf-8')) > (1 << 20):
        raise ValueError('code 必须是非空且不超过1MiB的TSX/JSX文本')
    if not isinstance(name, str) or len(name) > 240:
        raise ValueError('name 必须是不超过240字符的显示名')
    node, prefix, worker = _runtime_paths()
    if not _LOCK.acquire(blocking=False):
        raise TscircuitError('busy', '本机代码生成正在运行，请稍后重试。')
    try:
        env = dict(os.environ)
        for key in list(env):
            if key.endswith('_API_KEY') or key in ('NODE_OPTIONS', 'TSCIRCUIT_SESSION_TOKEN'):
                env.pop(key, None)
        try:
            process = subprocess.Popen([node, str(worker), str(prefix)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8',
                cwd=str(ROOT), env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            stdout, stderr = _communicate_worker(process, timeout=WORKER_TIMEOUT_SECONDS,
                input=json.dumps({'code': code, 'name': name}), is_cancelled=is_cancelled)
        except subprocess.TimeoutExpired as exc:
            raise TscircuitError('timeout', '本机代码生成超时，当前画布未修改。') from exc
        except InterruptedError as exc:
            raise TscircuitError('cancelled', '已取消本机代码生成。') from exc
        except OSError as exc:
            raise TscircuitError('runtime_unavailable', '无法启动本机 Node 运行时。') from exc
        try:
            output = json.loads(stdout)
        except ValueError as exc:
            raise TscircuitError('runtime_unavailable', '本机运行库未返回有效结果。') from exc
    finally:
        _LOCK.release()
    if output.get('error', {}).get('code') == 'runtime_unavailable':
        raise TscircuitError('runtime_unavailable', output['error']['message'])
    rows = output.get('circuit_json') or []
    runtime = output.get('runtime') or {}
    manifest = json.loads((ROOT / 'third_party/tscircuit-runtime-attribution.json').read_text(encoding='utf-8'))
    source = {'format': 'tscircuit', 'repository': manifest['repository'],
              'code_sha256': hashlib.sha256(code.encode('utf-8')).hexdigest(),
              'sha256': hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode('utf-8')).hexdigest(),
              'versions': runtime.get('versions', {}), 'license': 'MIT',
              'attribution': 'third_party/tscircuit-runtime-attribution.json', 'upstream_runtime_status': 'executed_local'}
    result = {'status': 'rejected', 'circuit': None, 'issues': [], 'source': source,
              'upstream_circuit_json': rows, 'schematic_svg': output.get('schematic_svg', ''),
              'diagnostics': output.get('diagnostics', []), 'runtime': runtime,
              'readiness': {'importable': False, 'accepted': False,
                            'electrical_validated': False, 'needs_review': True}}
    if not output.get('ok'):
        result['issues'] = [output.get('error') or {'code': 'UPSTREAM_ERROR', 'message': '上游代码生成失败', 'path': ''}]
        return result
    try:
        circuit = _tscircuit(rows)
    except ImportProblem as exc:
        result['issues'] = [exc.issue]
        result['diagnostics'].append(exc.issue)
        return result
    circuit['title'] = name or '代码建电路候选'
    circuit['provenance'] = {'origin': 'import', 'external_format': 'tscircuit',
                             'source_id': name, **source}
    result.update(status='needs_review', circuit=circuit,
                  issues=[{'code': 'CODE_REVIEW_REQUIRED', 'message': '请核对元件参数与连接，并完成统一校验。', 'path': ''},
                          {'code': 'RATINGS_UNKNOWN', 'message': '原始代码未提供可确认的器件额定功率、耐压等额定值；请核实后再放行。', 'path': 'parts'}])
    result['readiness']['importable'] = True
    return result
