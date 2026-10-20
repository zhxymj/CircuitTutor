# -*- coding: utf-8 -*-
"""Actual-pin self-contained schematic for real PCB candidates.

Symbols use component.pin_map as their KiCad pin numbers. Their sheet/instance
UUIDs and global net labels are shared with pcb_real; no conceptual TYPE_MAP is
used. Passive parts are declared passive. Active parts require explicit verified
component.pin_types, rather than inferred electrical semantics.
"""
from __future__ import annotations

import re

from core.pcb_real import (PCBContractError, _nets, _uuid, schematic_identity,
                           parse_sexpr, _children, _one, _value)
from kicad.symbols import _esc, _pin, _rect

_PASSIVE_TYPES = {"resistor", "capacitor", "capacitor_ceramic", "capacitor_film", "inductor",
                  "i2c_header", "spi_header", "uart_header", "conn_header", "screw_terminal",
                  "connector_screw2", "crystal", "fuse", "switch", "pushbutton"}
_PIN_TYPES = {"input", "output", "bidirectional", "tri_state", "passive", "free",
              "unspecified", "power_in", "power_out", "open_collector", "open_emitter", "no_connect"}


def expected_real_nets(circuit: dict) -> dict[str, set[tuple[str, str]]]:
    parts = {p["id"]: p for p in circuit["parts"]}
    endpoint_nets, nets = _nets(circuit, parts)
    labels = dict(nets)
    expected = {}
    for (ref, pin), number in endpoint_nets.items():
        if parts[ref]["type"] in ("vcc", "gnd"):
            continue
        expected.setdefault(labels[number], set()).add((ref, parts[ref]["component"]["pin_map"][pin]))
    return expected


def parse_official_real_netlist(text: str) -> dict[str, set[tuple[str, str]]]:
    root = parse_sexpr(text)
    nets = _one(root, "nets")
    if _value(root[0]) != "export" or nets is None:
        raise PCBContractError("source_netlist_format", "官方原理图网表不是完整 KiCad S-expression")
    result = {}
    for net in _children(nets, "net"):
        name = _one(net, "name")
        if not name or len(name) < 2:
            raise PCBContractError("source_netlist_format", "官方网表网络缺少名称")
        label = _value(name[1]).lstrip("/")
        if label.startswith("unconnected-"):
            continue
        for node in _children(net, "node"):
            ref, pin = _one(node, "ref"), _one(node, "pin")
            if not ref or not pin:
                raise PCBContractError("source_netlist_format", "官方网表节点缺少位号或焊盘号")
            reference = _value(ref[1])
            if not reference.startswith("#"):
                result.setdefault(label, set()).add((reference, _value(pin[1])))
    return result


def compare_real_netlist(circuit: dict, observed: dict) -> list[dict]:
    expected = expected_real_nets(circuit)
    return [{"net": name, "expected": [list(n) for n in sorted(expected.get(name, set()))],
             "observed": [list(n) for n in sorted(observed.get(name, set()))]}
            for name in sorted(set(expected) | set(observed))
            if expected.get(name, set()) != observed.get(name, set())]


def build_real_schematic(circuit: dict, project_name: str = "candidate") -> dict:
    if circuit.get("version") != 2 or not re.fullmatch(r"[A-Za-z0-9_-]+", project_name):
        raise PCBContractError("source_contract", "真实原理图要求 CircuitJSON v2 和安全工程名")
    root_uuid = schematic_identity(circuit)
    physical = [p for p in circuit["parts"] if p["type"] not in ("vcc", "gnd")]
    parts = {p["id"]: p for p in circuit["parts"]}
    endpoint_nets, nets = _nets(circuit, parts)
    labels = dict(nets)
    definitions, blocks, wires, global_labels, no_connects = [], [], [], [], []
    for index, part in enumerate(circuit["parts"]):
        virtual = part["type"] in ("vcc", "gnd")
        logical_ref = part["id"]
        ref = "#" + logical_ref if virtual else logical_ref
        component = ({"pin_map": {"1": "1"}, "mpn": "EXTERNAL_SUPPLY_REFERENCE",
                      "footprint": "", "datasheet": "", "manufacturer": ""}
                     if virtual else part.get("component") or {})
        pin_map = component.get("pin_map")
        if not pin_map or len(set(pin_map.values())) != len(pin_map):
            raise PCBContractError("source_pin_map", f"{ref} 需要显式且唯一的原理图引脚→焊盘映射")
        if virtual:
            pin_types = {"1": "power_out"}
        elif part["type"] in _PASSIVE_TYPES:
            pin_types = {pin: "passive" for pin in pin_map}
        else:
            pin_types = component.get("pin_types") or {}
            if set(pin_types) != set(pin_map) or any(t not in _PIN_TYPES for t in pin_types.values()):
                raise PCBContractError("source_pin_types", f"{ref} 的有源器件需要显式核实的 component.pin_types")
        value = str((part.get("attrs") or {}).get("value") or component["mpn"])
        base = "Part_" + logical_ref
        lib_id = "ZhihuiReal:" + base
        half_h = (len(pin_map) + 1) * 1.27
        properties = [("Reference", ref.rstrip("0123456789")), ("Value", value),
                      ("Footprint", component["footprint"]), ("Datasheet", component["datasheet"]),
                      ("MPN", component["mpn"]), ("Manufacturer", component.get("manufacturer", ""))]
        prop_defs = "\n".join(f' (property "{key}" "{_esc(val)}" (at 0 0 0) (effects (font (size 1.27 1.27))'
                              + (' (hide yes)' if key not in ("Reference", "Value") else '') + '))'
                              for key, val in properties)
        pin_geometry = []
        ordered = sorted(pin_map, key=lambda pin: (len(pin_map[pin]), pin_map[pin]))
        for pindex, pin in enumerate(ordered):
            py = (len(pin_map) - 1) * 1.27 - pindex * 2.54
            pin_geometry.append((pin, pin_map[pin], -10.16, py))
        present = "no" if virtual else "yes"
        definition = (f'(symbol "{lib_id}" (pin_names (offset 0.254)) (in_bom {present}) (on_board {present})\n'
                      + prop_defs + f'\n (symbol "{base}_0_1"\n' + _rect(-7.62, half_h, 7.62, -half_h)
                      + f')\n (symbol "{base}_1_1"\n'
                      + "\n".join(_pin(pin_types[pin], px, py, 0, pad, pin) for pin, pad, px, py in pin_geometry) + '))')
        definitions.append(definition)
        ax, ay = 38.1 + (index % 4) * 60.96, 44.45 + (index // 4) * 50.8
        instance_properties = []
        for pindex, (key, val) in enumerate(properties):
            x, y = ax, ay - half_h - 2.54 - (1.27 if key == "Reference" else 0)
            if key == "Reference":
                val = ref
            instance_properties.append(f' (property "{key}" "{_esc(val)}" (at {x:g} {y:g} 0) '
                                       '(effects (font (size 1.27 1.27))'
                                       + (' (hide yes)' if key not in ("Reference", "Value") else '') + '))')
        sym_uuid = schematic_identity(circuit, logical_ref)
        blocks.append(f'(symbol (lib_id "{lib_id}") (at {ax:g} {ay:g} 0) (unit 1) '
                      f'(exclude_from_sim {"yes" if virtual else "no"}) (in_bom {present}) (on_board {present}) (dnp no) '
                      f'(uuid "{sym_uuid}")\n' + "\n".join(instance_properties)
                      + "\n" + "\n".join(f'(pin "{pad}" (uuid "{_uuid(sym_uuid, "pin", pad)}"))'
                                                for _, pad, _, _ in pin_geometry)
                      + f'\n(instances (project "{project_name}" (path "/{root_uuid}" (reference "{ref}") (unit 1)))))')
        for pin, pad, px, py in pin_geometry:
            cx, cy = ax + px, ay - py
            number = endpoint_nets.get((logical_ref, pin))
            if number is None:
                no_connects.append(f'(no_connect (at {cx:g} {cy:g}) (uuid "{_uuid(sym_uuid, "nc", pad)}"))')
                continue
            ex = cx - 2.54
            wires.append(f'(wire (pts (xy {cx:g} {cy:g}) (xy {ex:g} {cy:g})) '
                         f'(stroke (width 0) (type default)) (uuid "{_uuid(sym_uuid, "wire", pad)}"))')
            global_labels.append(f'(global_label "{_esc(labels[number])}" (shape input) (at {ex:g} {cy:g} 0) '
                                 '(effects (font (size 1.27 1.27)) (justify left)) '
                                 f'(uuid "{_uuid(sym_uuid, "label", pad)}"))')
    sch_text = (f'(kicad_sch (version 20231120) (generator "zhihui-real-pins") (uuid "{root_uuid}") '
                '(paper "A4")\n(lib_symbols\n' + "\n".join(definitions) + ')\n'
                + "\n".join(wires + global_labels + no_connects + blocks)
                + '\n(sheet_instances (path "/" (page "1")))\n)\n')
    library = '(kicad_symbol_lib (version 20231120) (generator "zhihui-real-pins")\n' + "\n".join(
        d.replace('(symbol "ZhihuiReal:', '(symbol "', 1) for d in definitions) + '\n)\n'
    table = '(sym_lib_table (version 7) (lib (name "ZhihuiReal") (type "KiCad") '
    table += '(uri "${KIPRJMOD}/ZhihuiReal.kicad_sym") (options "") (descr "Explicit actual-pad symbols")))\n'
    return {"schematic_text": sch_text, "symbol_library_text": library, "symbol_lib_table_text": table,
            "root_uuid": root_uuid, "project_name": project_name,
            "symbol_uuids": {p["id"]: schematic_identity(circuit, p["id"]) for p in physical},
            "expected_nets": {n: [list(node) for node in sorted(nodes)] for n, nodes in expected_real_nets(circuit).items()}}


__all__ = ["build_real_schematic", "expected_real_nets", "parse_official_real_netlist", "compare_real_netlist"]
