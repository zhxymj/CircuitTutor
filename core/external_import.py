"""Read external result data without executing upstream code or guessing values.

Only complete, representable R/C/L/DC-source graphs become editable candidates.
Import success never means electrical acceptance. Upstream runtime/licensing
evidence comes from the local acquisition manifest, never from client claims.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from core.catalog import CATALOG
from core.llm import validate_circuit_json

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {'circuitvision_spice': 'CircuitVision', 'hcd': 'HCD', 'tscircuit': 'tscircuit'}
_ID = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')
_VALUE = re.compile(r'^([+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-zµμΩ]*)$')
_PINS = {'resistor': ['1', '2'], 'capacitor_ceramic': ['+', '-'],
         'inductor': ['1', '2'], 'battery': ['+', '-'], 'gnd': ['1']}
_UNITS = {'resistor': ('', 'ohm', 'ohms', 'Ω'),
          'capacitor_ceramic': ('', 'F', 'farad', 'farads'),
          'inductor': ('', 'H', 'henry', 'henries'), 'battery': ('', 'V', 'volt', 'volts')}


class ImportProblem(ValueError):
    def __init__(self, code, message, path=''):
        self.issue = {'code': code, 'message': message, 'path': path}
        super().__init__(message)


def _need(condition, message, path='', code='INVALID_FORMAT'):
    if not condition:
        raise ImportProblem(code, message, path)


def _unique(items, key, path):
    _need(isinstance(items, list), '需要数组', path)
    out = {}
    for i, item in enumerate(items):
        _need(isinstance(item, dict) and key in item, f'缺少 {key}', f'{path}[{i}]')
        value = item[key]
        _need(type(value) in (str, int) and str(value).strip() != '', f'{key} 无效', path)
        ident = str(value)
        _need(ident not in out, f'重复标识 {ident}', path, 'DUPLICATE_ID')
        out[ident] = item
    return out


def _value(raw, kind, *, spice=False, path=''):
    _need(type(raw) in (str, int, float), '缺少明确数值，不使用默认值', path, 'MISSING_VALUE')
    text = str(raw).strip().replace('µ', 'u').replace('μ', 'u')
    match = _VALUE.fullmatch(text)
    _need(match is not None, f'数值不明确或非有限数值：{text}', path, 'UNKNOWN_VALUE')
    number, suffix = match.groups()
    if spice and suffix == 'F':
        raise ImportProblem('UNKNOWN_VALUE', 'SPICE 裸 F 与法拉单位含义冲突，不能猜测', path)
    scales = {'': 0, 't': 12, 'g': 9, 'k': 3, 'm': -3, 'u': -6, 'n': -9, 'p': -12, 'f': -15}
    exponent = 0
    # Strip the explicit quantity unit before interpreting an engineering prefix.
    for unit in sorted((u for u in _UNITS[kind] if u), key=len, reverse=True):
        if suffix.endswith(unit):
            suffix = suffix[:-len(unit)]
            break
    if spice:
        if suffix.lower() == 'meg': exponent = 6
        else:
            _need(suffix.lower() in scales, f'不支持数值后缀 {suffix}', path, 'UNKNOWN_VALUE')
            exponent = scales[suffix.lower()]
    else:
        if suffix == 'M' or suffix.lower() == 'meg': exponent = 6
        else:
            _need(suffix.lower() in scales, f'不支持数值后缀 {suffix}', path, 'UNKNOWN_VALUE')
            exponent = scales[suffix.lower()]
    try:
        value = Decimal(number) * (Decimal(10) ** exponent)
        _need(value.is_finite() and Decimal('1e-24') <= value <= Decimal('1e24'),
              '数值必须为有限正数且在可表示范围内', path, 'UNKNOWN_VALUE')
    except InvalidOperation:
        raise ImportProblem('UNKNOWN_VALUE', '无效数值', path)
    result = format(value, 'f').rstrip('0').rstrip('.') if '.' in format(value, 'f') else format(value, 'f')
    return result + {'battery': 'V', 'capacitor_ceramic': 'F', 'inductor': 'H'}.get(kind, '')


def _part(pid, kind, value=None):
    _need(isinstance(pid, str) and _ID.fullmatch(pid), '元件位号无效', str(pid))
    return {'id': pid, 'type': kind, 'attrs': {} if kind == 'gnd' else {'value': value}}


def _polarity(role):
    return {'positive': '+', 'plus': '+', '+': '+', 'pos': '+', 'vplus': '+',
            'negative': '-', 'minus': '-', '-': '-', 'neg': '-', 'vminus': '-'}.get(str(role).lower())


def _finish(parts, groups, ground_keys=()):
    ids = [p['id'] for p in parts]
    _need(len(ids) == len(set(ids)), '元件位号重复', code='DUPLICATE_ID')
    for g in ground_keys:
        if g not in groups: continue
        gid = 'GND_IMPORT'
        while gid in ids: gid += '_1'
        parts.append(_part(gid, 'gnd'))
        ids.append(gid)
        groups[g].append(gid + ':1')
    connections, seen = [], set()
    for endpoints in groups.values():
        endpoints = list(dict.fromkeys(endpoints))
        _need(len(endpoints) >= 2, '网络只有一个端点，拓扑不完整', code='INCOMPLETE_TOPOLOGY')
        for endpoint in endpoints[1:]:
            pair = tuple(sorted((endpoints[0], endpoint)))
            if pair not in seen:
                connections.append([endpoints[0], endpoint])
                seen.add(pair)
    _need(parts, '没有可导入的元件', code='EMPTY_CIRCUIT')
    circuit = {'version': 2, 'title': '外部导入候选', 'parts': parts, 'connections': connections}
    errors = validate_circuit_json(circuit, CATALOG)
    _need(not errors, '; '.join(errors), code='INCOMPLETE_TOPOLOGY')
    return circuit


def _circuitvision(content):
    parts, groups = [], defaultdict(list)
    refs = set()
    for index, line in enumerate(content.splitlines()):
        line = line.strip()
        if not line or line.startswith('*'): continue
        if line.lower() in ('.end', '.op'): continue
        if line.lower().startswith('.title '): continue
        tokens = line.split()
        path = f'line[{index + 1}]'
        _need(len(tokens) == 4, '只支持上游四列 R/C/L/V 网表，不接受缺值或复杂SPICE语句', path)
        ref, n1, n2, raw = tokens
        _need(ref.upper() not in refs, f'重复SPICE位号 {ref}', path, 'DUPLICATE_ID')
        refs.add(ref.upper())
        kind = {'R': 'resistor', 'C': 'capacitor_ceramic', 'L': 'inductor', 'V': 'battery'}.get(ref[:1].upper())
        _need(kind is not None, f'不支持器件 {ref}，整份保留为拒绝结果', path, 'UNSUPPORTED_COMPONENT')
        _need(re.fullmatch(r'[A-Za-z0-9_]+', n1) and re.fullmatch(r'[A-Za-z0-9_]+', n2)
              and n1 != n2, '网络节点缺失或短接同一器件两端', path, 'UNKNOWN_PORT')
        parts.append(_part(ref, kind, _value(raw, kind, spice=True, path=path)))
        for pin, net in zip(_PINS[kind], (n1, n2)): groups[net].append(ref + ':' + pin)
    return _finish(parts, groups, ['0'])


def _hcd(data):
    _need(isinstance(data, dict), 'HCD final JSON 必须为对象')
    comps = _unique(data.get('components'), 'id', 'components')
    endpoints = _unique(data.get('endpoints'), 'eid', 'endpoints')
    nets = _unique(data.get('nets'), 'id', 'nets')
    _need(isinstance(data.get('texts'), list), 'HCD需要原始texts数组，不能从SPICE默认值补齐', 'texts')
    membership, groups = {}, defaultdict(list)
    for netid, net in nets.items():
        _need(isinstance(net.get('endpoint_eids'), list), '缺少网络端点数组', 'nets')
        for eid in net['endpoint_eids']:
            eid = str(eid)
            _need(eid in endpoints and eid not in membership, '未知端点或端点属于多个网络', 'nets', 'UNKNOWN_PORT')
            membership[eid] = netid
    per_comp = defaultdict(list)
    for eid, ep in endpoints.items():
        if ep.get('kind') != 'terminal': continue
        cid = str(ep.get('comp_id'))
        _need(cid in comps and eid in membership, '端点未绑定元件或网络', 'endpoints', 'UNKNOWN_PORT')
        per_comp[cid].append((eid, ep))
    parts = []
    kinds = {'resistor': 'resistor', 'capacitor': 'capacitor_ceramic',
             'capacitor.unpolarized': 'capacitor_ceramic', 'inductor': 'inductor',
             'voltage.dc': 'battery', 'voltage.battery': 'battery', 'battery': 'battery',
             'gnd': 'gnd', 'ground': 'gnd'}
    for cid, comp in comps.items():
        label = comp.get('cls_name_refined') or comp.get('cls_name')
        kind = kinds.get(label)
        _need(kind is not None, f'不支持元件类别 {label}', f'components[{cid}]', 'UNSUPPORTED_COMPONENT')
        eps = per_comp[cid]
        _need(len(eps) == len(_PINS[kind]), '元件端点数量不明确', f'components[{cid}]', 'UNKNOWN_PORT')
        value = None
        if kind != 'gnd':
            texts = [t.get('text') for t in data['texts'] if isinstance(t, dict) and str(t.get('matched_component_id')) == cid]
            values = []
            for text in texts:
                if isinstance(text, str) and re.fullmatch(r'[A-Za-z]+\d+', text.strip()): continue
                values.append(_value(text, kind, path=f'texts[{cid}]'))
            _need(len(set(values)) == 1, '元件数值缺失或冲突', f'components[{cid}]', 'MISSING_VALUE')
            value = values[0]
        pid = ('GND' if kind == 'gnd' else {'resistor':'R','capacitor_ceramic':'C','inductor':'L','battery':'V'}[kind]) + str(len(parts) + 1)
        parts.append(_part(pid, kind, value))
        pins = _PINS[kind]
        if kind == 'battery':
            pins = [_polarity(ep.get('port_role')) for _, ep in eps]
            _need(set(pins) == {'+', '-'}, '电源需要明确且不同的正负端口角色', f'components[{cid}]', 'UNKNOWN_PORT')
        for (eid, _), pin in zip(eps, pins): groups[membership[eid]].append(pid + ':' + pin)
    return _finish(parts, groups)


class _Groups:
    def __init__(self): self.parent = {}
    def root(self, item):
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item
    def join(self, a, b): self.parent[self.root(b)] = self.root(a)


def _tscircuit(data):
    _need(isinstance(data, list) and all(isinstance(x, dict) for x in data), 'tscircuit导出必须为记录数组')
    _need(not any(str(x.get('type', '')).startswith(('source_', 'schematic_'))
                  and str(x.get('type', '')).endswith('_error') for x in data),
          '上游源电路或原理图包含错误记录', code='UPSTREAM_ERROR')
    comps = _unique([x for x in data if x.get('type') == 'source_component'], 'source_component_id', 'source_component')
    ports = _unique([x for x in data if x.get('type') == 'source_port'], 'source_port_id', 'source_port')
    nets = _unique([x for x in data if x.get('type') == 'source_net'], 'source_net_id', 'source_net')
    traces = _unique([x for x in data if x.get('type') == 'source_trace'], 'source_trace_id', 'source_trace')
    parts, endpoint_map, per_comp = [], {}, defaultdict(list)
    for portid, port in ports.items():
        cid = str(port.get('source_component_id'))
        _need(cid in comps, 'source_port未绑定受支持的元件', portid, 'UNKNOWN_PORT')
        per_comp[cid].append((portid, port))
    kinds = {'simple_resistor': ('resistor', 'resistance'),
             'simple_capacitor': ('capacitor_ceramic', 'capacitance'),
             'simple_inductor': ('inductor', 'inductance'), 'simple_power_source': ('battery', 'voltage')}
    for cid, comp in comps.items():
        _need(comp.get('ftype') in kinds, f'不支持元件 {comp.get("ftype")}', cid, 'UNSUPPORTED_COMPONENT')
        kind, field = kinds[comp['ftype']]
        pid = comp.get('name')
        parts.append(_part(pid, kind, _value(comp.get(field), kind, path=cid + '.' + field)))
        eps = per_comp[cid]
        _need(len(eps) == 2, '元件必须有两个明确source_port', cid, 'UNKNOWN_PORT')
        assigned = []
        for portid, port in eps:
            if kind == 'battery':
                pin = _polarity(port.get('name'))
            else:
                number = port.get('pin_number')
                pin = _PINS[kind][number - 1] if type(number) is int and number in (1, 2) else None
            _need(pin is not None and pin not in assigned, '端口编号或电源极性未知/重复', portid, 'UNKNOWN_PORT')
            assigned.append(pin)
            endpoint_map[portid] = pid + ':' + pin
    graph = _Groups()
    for traceid, trace in traces.items():
        pids, nids = trace.get('connected_source_port_ids'), trace.get('connected_source_net_ids')
        _need(isinstance(pids, list) and isinstance(nids, list), 'trace需要端口及网络数组', traceid)
        _need(all(isinstance(p, str) and p in ports for p in pids)
              and all(isinstance(n, str) and n in nets for n in nids), 'trace引用未知端口或网络', traceid, 'UNKNOWN_PORT')
        members = ['p:' + p for p in pids] + ['n:' + n for n in nids]
        _need(members, '空trace', traceid, 'UNKNOWN_PORT')
        for member in members: graph.join(members[0], member)
    groups = defaultdict(list)
    for portid, endpoint in endpoint_map.items():
        _need('p:' + portid in graph.parent, '端口没有网络绑定', portid, 'UNKNOWN_PORT')
        groups[graph.root('p:' + portid)].append(endpoint)
    grounds = list(dict.fromkeys(graph.root('n:' + nid) for nid, net in nets.items()
                               if (net.get('is_ground') is True or net.get('name') == 'GND')
                               and 'n:' + nid in graph.parent))
    return _finish(parts, groups, grounds)


def _source_metadata(source, content):
    manifest = ROOT / 'evidence/upgrade/open_source_integration/acquisition.json'
    try:
        data = json.loads(manifest.read_text(encoding='utf-8'))
        item = next(p for p in data['projects'] if p['name'] == SOURCES[source])
        return {'format': source, 'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
                'repository': item['official_url'], 'revision': item['commit'],
                'license': item.get('github_license') or 'unresolved',
                'license_assessment': item.get('license_assessment', 'unresolved'),
                'upstream_runtime_status': item.get('runtime_status', 'incomplete')}
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        raise ValueError('本地上游获取记录缺失或无效，无法标注来源') from exc


def import_external_result(source, content, *, name=''):
    """Return an all-or-nothing, unvalidated candidate or a rejection with digest.

    ValueError means malformed request (HTTP400); rejected means unsupported or
    incomplete external content (HTTP422). No inference, file writes or tools.
    """
    if not isinstance(source, str) or source not in SOURCES:
        raise ValueError('source 必须为 circuitvision_spice、hcd 或 tscircuit')
    if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > (1 << 20):
        raise ValueError('content 必须是非空且不超过1MiB的UTF-8文本')
    if not isinstance(name, str) or len(name) > 240:
        raise ValueError('name 必须是不超过240字符的显示名')
    metadata = _source_metadata(source, content)
    try:
        if source == 'circuitvision_spice': circuit = _circuitvision(content)
        else:
            try: data = json.loads(content)
            except (ValueError, RecursionError) as exc:
                raise ImportProblem('INVALID_JSON', '外部结果不是有效JSON') from exc
            circuit = _hcd(data) if source == 'hcd' else _tscircuit(data)
        circuit['provenance'] = {'origin': 'import', 'source_id': name,
                                 'external_format': source, 'source_sha256': metadata['sha256'],
                                 **{k: v for k, v in metadata.items() if k not in ('format', 'sha256')}}
        if name: circuit['title'] = name
        issues = [
            {'code': 'EXTERNAL_REVIEW_REQUIRED', 'message': '转换仅生成候选；参数与拓扑需复核，电气结果由统一门禁产生', 'path': ''},
            {'code': 'RATINGS_UNKNOWN', 'message': '原始结果未提供可确认的器件额定功率、耐压等额定值；来源未核实，不可据此制造放行', 'path': 'parts'},
        ]
        return {'status': 'needs_review', 'circuit': circuit, 'issues': issues, 'source': metadata,
                'readiness': {'importable': True, 'accepted': False,
                              'electrical_validated': False, 'needs_review': True}}
    except ImportProblem as exc:
        return {'status': 'rejected', 'circuit': None, 'issues': [exc.issue], 'source': metadata,
                'readiness': {'importable': False, 'accepted': False,
                              'electrical_validated': False, 'needs_review': True}}


__all__ = ['import_external_result']
