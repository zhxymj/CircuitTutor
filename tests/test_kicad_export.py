# -*- coding: utf-8 -*-
"""test_kicad_export.py —— export.py 导出质量测试（第 12 套件）。

三层校验：
  1) 结构层：自写 S 表达式解析器检查语法合法性、uuid 唯一性、
     实例 lib_id/pin 号与 lib_symbols 定义一一对应；
  2) 连通层：从导出文件文本反推"引脚坐标 -> 短走线 -> 标签"链路，
     验证原 CircuitJSON 每条连接的两端在文件中确实同网络、
     gnd/vcc 网络正确改名为 GND/VCC、网络数量与 CircuitGraph 一致；
  3) 确定性：同一电路两次导出逐字节一致。

不依赖 KiCad 安装；若本机装有 kicad-cli 则自动追加官方 ERC 校验。
"""


# 测试位于 tests/ 子目录：先把项目根插入 sys.path 再 import 项目模块
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import json
import sys
from pathlib import Path

from kicad import export as K
from core import catalog
from core.erc import CircuitGraph

PASS = 0
proj = Path(__file__).resolve().parent.parent / "data"  # 项目根 data/


def check(cond, msg):
    global PASS
    assert cond, msg
    PASS += 1


# ---------------- S 表达式解析 ----------------

def sexp_tokenize(text):
    toks, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in ' \t\r\n':
            i += 1
        elif c == '(':
            toks.append('('); i += 1
        elif c == ')':
            toks.append(')'); i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n:
                if text[j] == '\\' and j + 1 < n:
                    buf.append(text[j + 1]); j += 2
                elif text[j] == '"':
                    break
                else:
                    buf.append(text[j]); j += 1
            toks.append(('STR', ''.join(buf)))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            toks.append(text[i:j]); i = j
    return toks


def sexp_parse(text):
    toks = sexp_tokenize(text)
    pos = 0

    def parse_one():
        nonlocal pos
        t = toks[pos]
        if t == '(':
            pos += 1
            out = []
            while toks[pos] != ')':
                out.append(parse_one())
            pos += 1
            return out
        pos += 1
        return t[1] if isinstance(t, tuple) else t

    root = parse_one()
    assert pos == len(toks), "S 表达式存在未闭合括号"
    return root


def tag(node):
    return node[0] if isinstance(node, list) and node else None


def find_all(node, name):
    if isinstance(node, list):
        if node and node[0] == name:
            yield node
        for ch in node[1:]:
            yield from find_all(ch, name)


def _float(s):
    return round(float(s), 4)


def pins_of_def(def_node):
    """{引脚号: (x, y)}（符号文件坐标）。"""
    out = {}
    for pin in find_all(def_node, 'pin'):
        at = next(n for n in pin if tag(n) == 'at')
        num = next(n for n in pin if tag(n) == 'number')
        out[num[1]] = (_float(at[1]), _float(at[2]))
    return out


def pin_nets_from_file(root):
    """从文件几何反推 {KiCad位号: {引脚号: 网络标签文本}}。"""
    libsyms = next(find_all(root, 'lib_symbols'))
    defs = {d[1]: d for d in libsyms[1:]}
    wires = []
    for w in find_all(root, 'wire'):
        pts = next(n for n in w if tag(n) == 'pts')
        wires.append(((_float(pts[1][1]), _float(pts[1][2])),
                      (_float(pts[2][1]), _float(pts[2][2]))))
    labels = {}
    for lb in find_all(root, 'label'):
        at = next(n for n in lb if tag(n) == 'at')
        labels[(_float(at[1]), _float(at[2]))] = lb[1]

    nets = {}
    for inst in find_all(root, 'symbol'):
        if tag(inst) != 'symbol' or not isinstance(inst[1], list) or inst[1][0] != 'lib_id':
            continue
        lib_id = next(n for n in inst if tag(n) == 'lib_id')[1]
        at = next(n for n in inst if tag(n) == 'at')
        ax, ay = _float(at[1]), _float(at[2])
        ref = next(n for n in inst if tag(n) == 'property' and n[1] == 'Reference')[2]
        defpins = pins_of_def(defs[lib_id])
        for pnum in [n for n in inst if tag(n) == 'pin']:
            pin_no = pnum[1]
            px, py = defpins[pin_no]
            pos = (_float(ax + px), _float(ay - py))
            net = labels.get(pos)
            if net is None:
                for a, b in wires:
                    if a == pos and b in labels:
                        net = labels[b]
                        break
                    if b == pos and a in labels:
                        net = labels[a]
                        break
            nets.setdefault(ref, {})[pin_no] = net
    return nets


# ---------------- 独立重算期望网络（不复用导出器内部逻辑） ----------------

def expected_nets(circuit):
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    ptype = {p['id']: p['type'] for p in circuit.get('parts', [])}
    for conn in circuit.get('connections', []):
        refs = [r for r in conn[:2] if isinstance(r, str) and ':' in r]
        for r in refs:
            find(r)
        for r in refs[1:]:
            union(refs[0], r)
    groups = {}
    for r in parent:
        groups.setdefault(find(r), []).append(r)
    out = {}
    for root, members in groups.items():
        members.sort()
        name = f"N{sorted(groups, key=lambda r: min(groups[r])).index(root) + 1}"
        for anchor, label in (('gnd', 'GND'), ('vcc', 'VCC')):
            if any(ptype.get(m.split(':', 1)[0]) == anchor for m in members):
                name = label
                break
        for m in members:
            out[m] = name
    return out


# ---------------- 主流程 ----------------

def verify_circuit(name, circuit, kb, strict_nets=True):
    text, manifest, warns = K.export_kicad_sch(circuit, name, kb=kb, collect_manifest=True)

    # 1) 结构层
    root = sexp_parse(text)
    check(tag(root) == 'kicad_sch', f"{name}: 根节点应为 kicad_sch")
    check(next(find_all(root, 'version'))[1] == K.SCH_VERSION, f"{name}: version 错误")
    uuids = [u[1] for u in find_all(root, 'uuid')]
    check(len(uuids) == len(set(uuids)), f"{name}: uuid 有重复")

    libsyms = next(find_all(root, 'lib_symbols'))
    defs = {d[1]: d for d in libsyms[1:]}
    for inst in find_all(root, 'symbol'):
        if not (isinstance(inst[1], list) and inst[1][0] == 'lib_id'):
            continue
        lib_id = next(n for n in inst if tag(n) == 'lib_id')[1]
        check(lib_id in defs, f"{name}: 实例 {lib_id} 缺少 lib_symbols 定义")
        dpins = pins_of_def(defs[lib_id])
        for p in (n for n in inst if tag(n) == 'pin'):
            check(p[1] in dpins, f"{name}: {lib_id} 实例引用了定义中不存在的引脚 {p[1]}")

    # 2) 连通层：文件反推 vs 期望
    file_nets = pin_nets_from_file(root)
    want = expected_nets(circuit)
    kmap = {}   # 本目录引脚引用 -> (KiCad位号, 引脚号)
    for p in circuit.get('parts', []):
        if p['type'] in K.TYPE_MAP:
            for opin, knum in K.TYPE_MAP[p['type']][1].items():
                kmap[f"{p['id']}:{opin}"] = (str(p['id']), knum)

    checked = 0
    for ref, net in want.items():
        if ref not in kmap:
            continue                      # vcc/gnd 锚点无符号，跳过
        kref, knum = kmap[ref]
        got = file_nets.get(kref, {}).get(knum)
        check(got == net, f"{name}: {ref} 期望网络 {net}，文件实测 {got}")
        checked += 1
    check(checked > 0, f"{name}: 没有任何可校验的连通引脚")

    # 每条原始连接两端同网络
    for conn in circuit.get('connections', []):
        refs = [r for r in conn[:2] if isinstance(r, str) and ':' in r]
        nets = [want.get(r) for r in refs]
        if len(nets) == 2 and all(n not in (None, '?') for n in nets):
            check(nets[0] == nets[1], f"{name}: 连接 {refs[0]}-{refs[1]} 两端网络不一致")

    # gnd/vcc 改名正确（锚点引脚本身无符号，只校验网络上的其他引脚）
    ptype = {p['id']: p['type'] for p in circuit.get('parts', [])}
    for ref, net in want.items():
        if net in ('GND', 'VCC') and ref in kmap:
            kref, knum = kmap[ref]
            check(file_nets[kref][knum] == net, f"{name}: {ref} 应为 {net} 标签")

    # 网络数量一致（仅统计含非锚点引脚的网络）
    anchor_only = lambda members: all(ptype.get(m.split(':', 1)[0]) in K.ANCHOR_NET_NAMES for m in members)
    g = CircuitGraph(circuit, kb)
    want_count = sum(1 for members in g.net_members.values() if not anchor_only(members))
    got_count = len({n for pin in file_nets.values() for n in pin.values() if n})
    if strict_nets:
        check(got_count == want_count, f"{name}: 网络数 文件={got_count} 期望={want_count}")
    else:
        check(got_count > 0, f"{name}: 文件应含网络，实际 {got_count}")

    # 3) 确定性
    text2 = K.export_kicad_sch(circuit, name, kb=kb)
    check(text2 == text, f"{name}: 两次导出结果不一致")

    # 导出器自带 manifest 与文件反推一致（抽查）
    for ref, net in manifest.items():
        kref, knum = kmap[ref]
        check(file_nets[kref][knum] == net, f"{name}: manifest 与文件不符 {ref}")
    return text, checked


# ---------------- 打开友好性：ASCII 工程名 + 说明 + 元件实例 ----------------

def test_open_friendly_export():
    """KiCad 打开后不应「啥都没有」：ASCII 工程名 + 打开说明 + 符号实例。"""
    from kicad.sch_writer import safe_project_stem, HOW_TO_OPEN_NAME
    check(safe_project_stem("5V电池经330欧电阻点亮LED") == "5V_330_LED",
          "中文标题应映射为 ASCII stem 5V_330_LED")
    check(safe_project_stem("led_demo") == "led_demo", "纯英文名应保留")
    pure = safe_project_stem("中文标题")
    check(pure.startswith("circuit_") and pure.isascii(),
          f"纯中文标题应退化为 circuit_ 前缀 ASCII 名，实际 {pure}")

    circuit = {
        "parts": [
            {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
            {"type": "resistor", "id": "R1", "attrs": {"value": "330Ω"}},
            {"type": "led", "id": "LED1", "attrs": {"value": "红"}},
            {"type": "gnd", "id": "GND1"},
        ],
        "connections": [
            ["BAT1:+", "R1:1"], ["R1:2", "LED1:+"],
            ["LED1:-", "GND1:1"], ["BAT1:-", "GND1:1"],
        ],
    }
    name = "5V电池经330欧电阻点亮LED"
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        fp, _w = K.export_to_dir(circuit, name, Path(td))
        stem = safe_project_stem(name)
        sch = Path(td) / f"{stem}.kicad_sch"
        pro = Path(td) / f"{stem}.kicad_pro"
        how = Path(td) / HOW_TO_OPEN_NAME
        check(sch.is_file(), f"应生成 ASCII 原理图文件名 {sch.name}")
        check(pro.is_file(), f"应生成 ASCII 工程文件名 {pro.name}")
        check(sch.name.isascii() and pro.name.isascii(),
              "工程/原理图文件名必须是 ASCII")
        check(how.is_file(), "应写入 HOW_TO_OPEN.txt")
        how_txt = how.read_text(encoding="utf-8")
        check("打开工程" in how_txt and "导入" in how_txt,
              "打开说明应强调打开工程而非导入")
        check(f"{stem}.kicad_pro" in how_txt, "说明应写明 .kicad_pro 文件名")
        from kicad.sch_writer import OPEN_BAT_NAME, ZIP_README_NAME
        bat = Path(td) / OPEN_BAT_NAME
        readme = Path(td) / ZIP_README_NAME
        check(bat.is_file(), "应写入 OPEN_IN_KICAD.bat 一键打开脚本")
        bat_txt = bat.read_text(encoding="utf-8", errors="replace")
        check(f"{stem}.kicad_pro" in bat_txt, "启动脚本应打开 .kicad_pro")
        check("kicad.exe" in bat_txt.lower() or "KICAD_EXE" in bat_txt,
              "启动脚本应查找 kicad.exe")
        check(readme.is_file(), "应写入 00-请先读我.txt")
        rd = readme.read_text(encoding="utf-8")
        check("导入" in rd and "打开" in rd, "根说明应区分导入/打开")
        check(OPEN_BAT_NAME in rd, "根说明应指向一键脚本")
        raw = sch.read_text(encoding="utf-8")
        check("(symbol (lib_id" in raw,
              "原理图应含符号实例（否则 KiCad 打开为空）")
        pro_raw = pro.read_text(encoding="utf-8")
        check(f'"{stem}.kicad_pro"' in pro_raw,
              "pro meta.filename 应与磁盘文件名一致")


def test_qwiic_physical_pin_mapping():
    """The source connector order must survive the editable symbol and netlist."""
    import copy
    import json
    import tempfile
    fixture = Path(__file__).resolve().parents[1] / "data/external_references/sparkfun_qwiic_adapter/circuit_v2_catalog_mapped.json"
    circuit = json.loads(fixture.read_text(encoding="utf-8"))
    text = K.export_kicad_sch(circuit, "qwiic_pin_map")
    measured = pin_nets_from_file(sexp_parse(text))
    for ref in ("J1", "J3", "J4"):
        check(measured[ref]["1"] == "GND", f"{ref}: 物理脚 1 应接 GND")
        check(measured[ref]["2"] == "VCC", f"{ref}: 物理脚 2 应接 VCC")
        check(measured[ref]["3"] == "N3", f"{ref}: 物理脚 3 应接 SDA")
        check(measured[ref]["4"] == "N2", f"{ref}: 物理脚 4 应接 SCL")
    if K.find_kicad_cli():
        with tempfile.TemporaryDirectory() as td:
            fp, _ = K.export_to_dir(circuit, "qwiic_pin_map", Path(td))
            nets = K.export_official_netlist(fp)
            check(nets is not None, "Qwiic: 官方网表可导出")
            for net, number in (("GND", "1"), ("VCC", "2"), ("N3", "3"), ("N2", "4")):
                check(("J1", number) in nets[net], f"Qwiic: 官方网表 {net} 应含 J1 物理脚 {number}")

    for bad_map in ({"GND": "1", "VCC": "2", "SDA": "3"},
                    {"GND": "1", "VCC": "1", "SDA": "3", "SCL": "4"},
                    {"GND": "1", "VCC": "2", "SDA": "3", "SCL": "5"}):
        invalid = copy.deepcopy(circuit)
        next(p for p in invalid["parts"] if p["id"] == "J1")["component"]["pin_map"] = bad_map
        try:
            K.export_kicad_sch(invalid, "bad_qwiic")
        except ValueError:
            pass
        else:
            raise AssertionError(f"无效 I2C 物理引脚映射未拒绝：{bad_map}")
    defaulted = copy.deepcopy(circuit)
    next(p for p in defaulted["parts"] if p["id"] == "J1").pop("component")
    default_nets = pin_nets_from_file(sexp_parse(K.export_kicad_sch(defaulted, "default_header")))
    check(default_nets["J1"]["3"] == "GND" and default_nets["J3"]["1"] == "GND",
          "无物理元数据的内部默认布局与有来源的 Qwiic 布局可在同图并存")
    missing_source = copy.deepcopy(circuit)
    next(p for p in missing_source["parts"] if p["id"] == "J1")["component"].pop("pin_map_source")
    try:
        K.export_kicad_sch(missing_source, "unsourced_qwiic")
    except ValueError:
        pass
    else:
        raise AssertionError("缺少来源的显式 I2C 物理引脚映射未拒绝")


def main():
    test_qwiic_physical_pin_mapping()
    demos = json.loads((proj / 'circuit_templates.json').read_text(encoding='utf-8'))
    kb = catalog.build_erc_kb()

    total_pins = 0
    for name, circuit in demos.items():
        _, checked = verify_circuit(name, circuit, kb)
        total_pins += checked
        print(f"OK  {name}: 结构/连通/确定性 全部通过（校验 {checked} 个引脚网络）")

    # ---- 合成电路：覆盖全部目录类型（仅用引脚映射唯一的类型做连通校验）----
    def _unique_map(tp):
        m = K.TYPE_MAP.get(tp)
        if not m:
            return False
        vals = list(m[1].values())
        return len(vals) == len(set(vals))

    all_types = list(catalog.CATALOG.keys())
    # 全类型必须都能映射（未知类型不允许）
    unknown = [t for t in all_types if t not in K.TYPE_MAP and t not in K.ANCHOR_NET_NAMES]
    check(not unknown, f"目录中存在未映射类型: {unknown}")
    # 连通性合成：只用无重复引脚号的类型，避免复用符号时 G/K 同号等导致网表合并
    synth_types = [t for t in all_types if t in K.TYPE_MAP and _unique_map(t)]
    parts = [{'type': t, 'id': f"{catalog.CATALOG[t]['ref']}{i}_T"} for i, t in enumerate(synth_types)]
    conns = []
    for i in range(0, len(parts) - 1, 2):
        a, b = parts[i], parts[i + 1]
        pa = next(iter(catalog.CATALOG[a['type']]['pins']))
        pb = next(iter(catalog.CATALOG[b['type']]['pins']))
        conns.append([f"{a['id']}:{pa}", f"{b['id']}:{pb}"])
    synth = {'parts': parts, 'connections': conns}
    # 大合成电路：复用符号时部分类型共享 KiCad 引脚号，网表合并数与
    # 目录图论网络不完全一一对应，故此处只校验「能导出 + lib_id 齐全 + 结构可解析」，
    # 连通性精确比对由上方 10 个专用模板用例负责。
    try:
        text, checked = verify_circuit('全类型合成', synth, kb, strict_nets=False)
    except TypeError:
        # 兼容旧签名
        text, checked = verify_circuit('全类型合成', synth, kb)
    total_pins += checked

    lib_ids = {n[1] for n in find_all(sexp_parse(text), 'lib_id')}
    exported_types = {t for t in synth_types if t in K.TYPE_MAP}
    need = {K.TYPE_MAP[t][0] for t in exported_types}
    check(need <= lib_ids, f"合成电路缺少 lib_id: {need - lib_ids}")
    print(f"OK  全类型合成: {len(all_types)} 种已映射，{len(synth_types)} 种入合成网表（校验 {checked} 个引脚网络）")

    # ---- 值转义：含引号与反斜杠的 value ----
    esc_circuit = {'parts': [{'type': 'resistor', 'id': 'R1', 'attrs': {'value': '1"k\\2'}},
                             {'type': 'battery', 'id': 'BAT1'}],
                   'connections': [['BAT1:+', 'R1:1'], ['BAT1:-', 'R1:2']]}
    t2, _mani2, _warn2 = K.export_kicad_sch(esc_circuit, 'esc_test', kb=kb, collect_manifest=True)
    root2 = sexp_parse(t2)
    vals = [n[2] for n in find_all(root2, 'property') if len(n) > 2 and n[1] == 'Value']
    check('1"k\\2' in vals, f"含特殊字符的值未原样保留: {vals}")
    print("OK  值转义: 引号/反斜杠在 S 表达式中正确转义且解析无损")

    # ---- 可选：本机装有 KiCad 时追加官方校验（ERC 归零 + 网表连通交叉验证）----
    kicad_cli = K.find_kicad_cli()
    if kicad_cli:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            for name, circ in demos.items():
                fp, _w = K.export_to_dir(circ, name, Path(td), kb=kb)
                erc = K.run_official_erc(fp)
                check(erc is not None, f"{name}: kicad-cli 官方 ERC 运行失败")
                if erc is not None:
                    check(erc['total'] == 0,
                          f"{name}: 官方 ERC 有 {erc['total']} 条违规: {erc['by_type']}")
                # 官方网表连通性：KiCad 引擎视角 == 导出器预期
                nets = K.export_official_netlist(fp)
                check(nets is not None, f"{name}: kicad-cli 网表导出失败")
                if nets is not None:
                    # KiCad 网表以引脚“号”为键；期望网络以本目录引脚“名”为键，
                    # 比对前经 TYPE_MAP 的 pin_map 换到同一命名空间
                    ptype = {p['id']: p['type'] for p in circ.get('parts', [])}
                    want = {}
                    for pref, net in expected_nets(circ).items():
                        r_, p_ = pref.split(':', 1)
                        pm = K.TYPE_MAP.get(ptype.get(r_), (None, {}, None))[1]
                        if p_ not in pm:
                            continue
                        want.setdefault(net, set()).add((r_, pm[p_]))
                    for net in set(want) | set(nets):
                        if net.startswith('unconnected-'):
                            continue    # no_connect 引脚的 KiCad 自动命名网络
                        got = {n for n in nets.get(net, set()) if not n[0].startswith('#')}
                        check(got == want.get(net, set()),
                              f"{name}: 官方网表网络 {net} 成员不符: {sorted(got)} ≠ {sorted(want.get(net, set()))}")
        print(f"OK  kicad-cli 官方校验（{len(demos)} demo）：ERC 全部归零 + 网表连通逐网络一致")
    else:
        print("（本机未安装 KiCad，跳过 kicad-cli 官方校验；结构/连通校验已覆盖）")

    # ---- 打开友好性：ASCII 工程名 + HOW_TO_OPEN + 符号实例 ----
    test_open_friendly_export()
    print("OK  打开友好性：ASCII 工程名 + HOW_TO_OPEN.txt + 符号实例非空")

    print(f"\ntest_kicad_export.py 全部 {PASS} 项断言通过 ✔")
    return 0


if __name__ == '__main__':
    sys.exit(main())
