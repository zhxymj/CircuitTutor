# -*- coding: utf-8 -*-
"""Strict, unrouted KiCad PCB candidate from verified CircuitJSON v2 metadata.

This module never substitutes generic pads or claims routing, DRC, or fab readiness.
It embeds local KiCad 8 footprint geometry and audits every connected pad against
the supplied schematic pin map.  The caller still needs KiCad CLI and an engineer
to validate the candidate before any manufacturing export.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from core.catalog import CATALOG


BOARD_VERSION = 20240108  # KiCad 8 format; newer library files are rejected.
_FOOTPRINT_ID = re.compile(r"^[A-Za-z0-9_.+-]+:[A-Za-z0-9_.+-]+$")
_PART_REF = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_ANCHORS = {"vcc", "gnd"}


class PCBContractError(ValueError):
    """The requested real PCB candidate cannot be generated safely."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Atom:
    value: str
    quoted: bool = False


def _a(value: Any, quoted: bool = False) -> Atom:
    return Atom(str(value), quoted)


def _children(node: list, key: str) -> list[list]:
    return [x for x in node[1:] if isinstance(x, list) and x and isinstance(x[0], Atom) and x[0].value == key]


def _one(node: list, key: str) -> list | None:
    found = _children(node, key)
    return found[0] if found else None


def _value(atom: Any) -> str:
    if not isinstance(atom, Atom):
        raise PCBContractError("footprint_syntax", "KiCad 封装语法不符合预期")
    return atom.value


def parse_sexpr(source: str) -> list:
    """Parse KiCad S-expressions, preserving quoted versus bare atoms."""
    tokens: list[Any] = []
    i = 0
    while i < len(source):
        c = source[i]
        if c.isspace():
            i += 1
        elif c == ";":
            end = source.find("\n", i)
            i = len(source) if end < 0 else end + 1
        elif c in "()":
            tokens.append(c)
            i += 1
        elif c == '"':
            i += 1
            chars: list[str] = []
            while i < len(source):
                c = source[i]
                if c == '"':
                    i += 1
                    break
                if c == "\\":
                    i += 1
                    if i >= len(source):
                        raise PCBContractError("footprint_syntax", "封装中有未结束的转义字符")
                    chars.append({"n": "\n", "r": "\r", "t": "\t"}.get(source[i], source[i]))
                else:
                    chars.append(c)
                i += 1
            else:
                raise PCBContractError("footprint_syntax", "封装中有未结束的字符串")
            tokens.append(Atom("".join(chars), True))
        else:
            start = i
            while i < len(source) and not source[i].isspace() and source[i] not in "();\"":
                i += 1
            if i == start:
                raise PCBContractError("footprint_syntax", "封装中有无效字符")
            tokens.append(Atom(source[start:i]))

    stack: list[list] = []
    root: list | None = None
    for token in tokens:
        if token == "(":
            node: list = []
            if stack:
                stack[-1].append(node)
            elif root is not None:
                raise PCBContractError("footprint_syntax", "封装包含多个根表达式")
            else:
                root = node
            stack.append(node)
        elif token == ")":
            if not stack:
                raise PCBContractError("footprint_syntax", "封装括号不匹配")
            stack.pop()
        elif not stack:
            raise PCBContractError("footprint_syntax", "封装根表达式外有内容")
        else:
            stack[-1].append(token)
    if stack or root is None:
        raise PCBContractError("footprint_syntax", "封装括号不匹配或为空")
    return root


def dump_sexpr(node: Any) -> str:
    if isinstance(node, Atom):
        if not node.quoted:
            if not node.value or re.search(r'[\s();"]', node.value):
                raise PCBContractError("footprint_syntax", "封装中有无效的裸词")
            return node.value
        escaped = node.value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if isinstance(node, list):
        return "(" + " ".join(dump_sexpr(x) for x in node) + ")"
    raise PCBContractError("footprint_syntax", "封装语法树存在未知节点")


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise PCBContractError("invalid_number", f"{label} 必须是有限数值")
    try:
        n = float(value)
    except (ValueError, TypeError):
        raise PCBContractError("invalid_number", f"{label} 必须是有限数值") from None
    if not math.isfinite(n) or (positive and n <= 0):
        raise PCBContractError("invalid_number", f"{label} 必须是有限正数" if positive else f"{label} 必须是有限数值")
    return n


def _pad_geometry(root: list) -> dict[str, list[dict]]:
    pads: dict[str, list[dict]] = {}
    for pad in _children(root, "pad"):
        if len(pad) < 4:
            raise PCBContractError("invalid_pad", "封装焊盘缺少编号、类型或形状")
        number, kind, shape = map(_value, pad[1:4])
        if not number or kind not in {"smd", "thru_hole"} or shape not in {"rect", "roundrect", "oval", "circle"}:
            raise PCBContractError("unsupported_pad", f"不支持焊盘 {number} 的类型或形状")
        at, size, layers = (_one(pad, key) for key in ("at", "size", "layers"))
        if not at or len(at) < 3 or not size or len(size) < 3 or not layers:
            raise PCBContractError("invalid_pad", f"焊盘 {number} 缺少位置、尺寸或层信息")
        x = _number(_value(at[1]), f"焊盘 {number} x")
        y = _number(_value(at[2]), f"焊盘 {number} y")
        w = _number(_value(size[1]), f"焊盘 {number} 宽", positive=True)
        h = _number(_value(size[2]), f"焊盘 {number} 高", positive=True)
        ls = {_value(x) for x in layers[1:]}
        if _one(pad, "net") is not None:
            raise PCBContractError("invalid_pad", f"封装库焊盘 {number} 不应预置网络")
        if kind == "smd" and "F.Cu" not in ls:
            raise PCBContractError("unsupported_pad", f"焊盘 {number} 不是正面铜层 SMD 焊盘")
        if kind == "thru_hole" and ("*.Cu" not in ls or _one(pad, "drill") is None):
            raise PCBContractError("unsupported_pad", f"焊盘 {number} 通孔层或孔径信息不完整")
        if number in pads:
            raise PCBContractError("duplicate_pad", f"封装中焊盘号 {number} 重复，需人工核对")
        pads[number] = [{"x": x, "y": y, "w": w, "h": h, "type": kind}]
    if not pads:
        raise PCBContractError("missing_pads", "封装没有可用铜焊盘")
    return pads


def load_footprint(footprint_id: str, roots: Iterable[str | os.PathLike], max_version: int = BOARD_VERSION) -> tuple[list, dict, str]:
    if not isinstance(footprint_id, str) or not _FOOTPRINT_ID.fullmatch(footprint_id):
        raise PCBContractError("footprint_id", f"无效封装标识：{footprint_id!r}")
    library, name = footprint_id.split(":", 1)
    found: Path | None = None
    for raw_root in roots:
        root = Path(raw_root)
        candidate = (root / f"{library}.pretty" / f"{name}.kicad_mod")
        if candidate.is_file():
            found = candidate
            break
        if root.name == f"{library}.pretty":
            candidate = root / f"{name}.kicad_mod"
            if candidate.is_file():
                found = candidate
                break
    if found is None:
        raise PCBContractError("footprint_unavailable", f"未找到本地封装 {footprint_id}")
    raw = found.read_bytes()
    if len(raw) > 2_000_000:
        raise PCBContractError("footprint_too_large", f"封装文件过大：{footprint_id}")
    try:
        root = parse_sexpr(raw.decode("utf-8-sig"))
    except UnicodeError as exc:
        raise PCBContractError("footprint_encoding", f"封装不是 UTF-8：{footprint_id}") from exc
    if len(root) < 2 or _value(root[0]) != "footprint" or _value(root[1]) != name:
        raise PCBContractError("footprint_identity", f"封装文件内部名称与 {footprint_id} 不一致")
    version = _one(root, "version")
    if not version or len(version) < 2 or not _value(version[1]).isdigit():
        raise PCBContractError("footprint_version", f"封装 {footprint_id} 缺少版本")
    if int(_value(version[1])) > max_version:
        raise PCBContractError("footprint_version", f"封装 {footprint_id} 需要较新版 KiCad，目标格式为 {max_version}")
    if not any(_value(p[1]) == "Reference" for p in _children(root, "property") if len(p) >= 3):
        raise PCBContractError("footprint_property", f"封装 {footprint_id} 缺少 Reference 属性")
    if not any(_value(p[1]) == "Value" for p in _children(root, "property") if len(p) >= 3):
        raise PCBContractError("footprint_property", f"封装 {footprint_id} 缺少 Value 属性")
    return root, _pad_geometry(root), hashlib.sha256(raw).hexdigest()


def _roots(roots: Iterable[str | os.PathLike] | None) -> list[str | os.PathLike]:
    if roots is not None:
        return list(roots)
    return [p for p in os.environ.get("KICAD_FOOTPRINT_DIR", "").split(os.pathsep) if p]


def _board_contract(circuit: dict) -> tuple[float, float, float, float]:
    board = circuit.get("board")
    if not isinstance(board, dict) or board.get("layers") != 2:
        raise PCBContractError("board_contract", "真实 PCB 需给出双层 board 约束")
    return tuple(_number(board.get(k), f"board.{k}", positive=True) for k in
                 ("width_mm", "height_mm", "min_clearance_mm", "min_trace_width_mm"))


def _uuid(*values: Any) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "/".join(map(str, ("zhihui-pcb-real", *values)))))


def schematic_identity(circuit: dict, ref: str | None = None) -> str:
    """Stable sheet/instance identity shared by actual-pin schematic and PCB."""
    canonical = json.dumps(circuit, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    root = _uuid("real-sheet", hashlib.sha256(canonical.encode("utf-8")).hexdigest())
    return _uuid("real-instance", root, ref) if ref is not None else root


def _refresh_uuids(node: list, part_id: str) -> None:
    counter = 0
    def walk(current: list) -> None:
        nonlocal counter
        for child in current[1:]:
            if isinstance(child, list) and child:
                if isinstance(child[0], Atom) and child[0].value == "uuid" and len(child) >= 2:
                    child[1] = _a(_uuid(part_id, counter), True)
                    counter += 1
                else:
                    walk(child)
    walk(node)
    # KiCad library revisions differ in which footprint primitives carry UUIDs.
    # Board instances need distinct IDs even when a compact fixture omits them.
    identifiable = {"property", "fp_line", "fp_rect", "fp_arc", "fp_circle",
                    "fp_poly", "fp_text", "pad", "zone", "group"}
    for index, child in enumerate(node[1:]):
        if isinstance(child, list) and child and isinstance(child[0], Atom):
            if child[0].value in identifiable and _one(child, "uuid") is None:
                child.append([_a("uuid"), _a(_uuid(part_id, "missing", index), True)])


def _set_property(root: list, name: str, value: str) -> None:
    for prop in _children(root, "property"):
        if len(prop) >= 3 and _value(prop[1]) == name:
            prop[2] = _a(value, True)
            return
    if name == "Footprint":
        return  # The footprint's library-qualified root name carries this field.
    root.append([_a("property"), _a(name, True), _a(value, True),
                 [_a("at"), _a("0"), _a("0"), _a("0")], [_a("layer"), _a("F.Fab", True)],
                 [_a("hide"), _a("yes")], [_a("effects"), [_a("font"), [_a("size"), _a("1"), _a("1")],
                  [_a("thickness"), _a("0.15")]]],
                 [_a("uuid"), _a(_uuid("field", dump_sexpr(_one(root, "uuid") or []), name), True)]])


def _endpoint(text: Any, parts: dict) -> tuple[str, str]:
    if not isinstance(text, str) or text.count(":") != 1:
        raise PCBContractError("connection_endpoint", f"无效连接端点：{text!r}")
    ref, pin = text.split(":")
    if not ref or not pin or ref not in parts:
        raise PCBContractError("connection_endpoint", f"连接端点不存在：{text!r}")
    return ref, pin


def _nets(circuit: dict, parts: dict) -> tuple[dict[tuple[str, str], int], list[tuple[int, str]]]:
    parent: dict[tuple[str, str], tuple[str, str]] = {}
    def find(x: tuple[str, str]) -> tuple[str, str]:
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]
    for index, conn in enumerate(circuit.get("connections", [])):
        if not isinstance(conn, list) or len(conn) < 2:
            raise PCBContractError("connection_shape", f"connections[{index}] 格式错误")
        a, b = _endpoint(conn[0], parts), _endpoint(conn[1], parts)
        for ref, pin in (a, b):
            part = parts[ref]
            catalog_pins = CATALOG[part["type"]]["pins"]
            if pin not in catalog_pins:
                raise PCBContractError("unknown_pin", f"{ref}:{pin} 不在元件目录引脚中")
            if part.get("type") not in _ANCHORS and pin not in part["component"]["pin_map"]:
                raise PCBContractError("unmapped_pin", f"{ref}:{pin} 没有 PCB 焊盘映射")
        parent[find(a)] = find(b)
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for endpoint in parent:
        groups.setdefault(find(endpoint), []).append(endpoint)
    net_order = sorted((sorted(group) for group in groups.values()), key=lambda x: x[0])
    map_endpoint: dict[tuple[str, str], int] = {}
    nets: list[tuple[int, str]] = []
    for number, group in enumerate(net_order, 1):
        physical = [p for p in group if parts[p[0]].get("type") not in _ANCHORS]
        if not physical:
            continue
        label = "Net-(%s-%s)" % min(physical)
        nets.append((number, label))
        for endpoint in group:
            map_endpoint[endpoint] = number
    return map_endpoint, nets


def _num(n: float) -> str:
    return f"{n:.6f}".rstrip("0").rstrip(".") if n else "0"


def unconnected_pad_nets(circuit: dict) -> dict[tuple[str, str], str]:
    """KiCad's actual isolated-pad names for explicit schematic no-connect pins."""
    parts = {p["id"]: p for p in circuit["parts"]}
    endpoints, _ = _nets(circuit, parts)
    return {(ref, pin): f"unconnected-({ref}-{pin}-Pad{pad})"
            for ref, p in parts.items() if p["type"] not in _ANCHORS
            for pin, pad in p["component"]["pin_map"].items() if (ref, pin) not in endpoints}


def _edge_cuts(width: float, height: float) -> str:
    pts = [(0, 0), (width, 0), (width, height), (0, height), (0, 0)]
    return "\n".join(
        f'(gr_line (start {_num(x1)} {_num(y1)}) (end {_num(x2)} {_num(y2)}) '
        f'(stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "{_uuid("edge", i)}"))'
        for i, ((x1, y1), (x2, y2)) in enumerate(zip(pts, pts[1:])))


def build_pcb_candidate(circuit: dict, name: str = "circuit",
                        footprint_roots: Iterable[str | os.PathLike] | None = None,
                        target_version: int = BOARD_VERSION) -> dict:
    """Return an unrouted board and pad audit, or raise PCBContractError.

    `footprint_roots` points to directories containing `Library.pretty` folders.
    When omitted, `KICAD_FOOTPRINT_DIR` supplies a path-separated root list.
    """
    if not isinstance(circuit, dict) or circuit.get("version") != 2:
        raise PCBContractError("schema_version", "真实 PCB 仅接受 CircuitJSON v2")
    if target_version not in (BOARD_VERSION, 20260206):
        raise PCBContractError("target_version", "仅支持经明确选择的 KiCad 8 或 KiCad 10 格式")
    width, height, clearance, trace_width = _board_contract(circuit)
    parts_list = circuit.get("parts")
    if not isinstance(parts_list, list) or not parts_list:
        raise PCBContractError("parts", "真实 PCB 需要非空 parts 数组")
    if not isinstance(circuit.get("connections"), list):
        raise PCBContractError("connections", "真实 PCB 需要 connections 数组")
    parts: dict[str, dict] = {}
    for p in parts_list:
        if not isinstance(p, dict) or not isinstance(p.get("id"), str) or not _PART_REF.fullmatch(p["id"]) or p["id"] in parts:
            raise PCBContractError("part_id", "PCB 元件位号缺失或重复")
        if p.get("type") not in CATALOG:
            raise PCBContractError("part_type", f"{p['id']} 的元件类型不在已知目录中")
        parts[p["id"]] = p
    roots = _roots(footprint_roots)
    if not roots:
        raise PCBContractError("footprint_unavailable", "未配置 KiCad 封装库目录")

    footprints: dict[str, tuple[list, dict, str]] = {}
    for ref, p in parts.items():
        if p.get("type") in _ANCHORS:
            continue
        comp, pcb = p.get("component"), p.get("pcb")
        if not isinstance(comp, dict) or not isinstance(pcb, dict):
            raise PCBContractError("part_metadata", f"{ref} 缺少 component 或 pcb 元数据")
        if not all(isinstance(comp.get(k), str) and comp[k].strip() for k in ("mpn", "footprint", "datasheet", "pin_map_source")):
            raise PCBContractError("component_evidence", f"{ref} 缺少料号、封装、数据手册或引脚映射来源")
        pin_map = comp.get("pin_map")
        if not isinstance(pin_map, dict) or not pin_map or not all(
                isinstance(k, str) and k and isinstance(v, str) and v for k, v in pin_map.items()):
            raise PCBContractError("pin_map", f"{ref} 缺少有效 pin_map")
        if len(set(pin_map.values())) != len(pin_map):
            raise PCBContractError("pin_map", f"{ref} 多个原理图引脚映射到同一焊盘，需人工确认")
        catalog_pins = CATALOG[p["type"]]["pins"]
        if any(pin not in catalog_pins for pin in pin_map):
            raise PCBContractError("pin_map", f"{ref} 的 pin_map 含目录外引脚")
        root, pads, sha = load_footprint(comp["footprint"], roots, max_version=target_version)
        if not set(pin_map.values()) <= set(pads):
            raise PCBContractError("pin_map", f"{ref} 的 pin_map 引用了封装不存在的焊盘")
        x, y = (_number(pcb.get(k), f"{ref}.pcb.{k}") for k in ("x_mm", "y_mm"))
        angle = _number(pcb.get("rotation_deg", 0), f"{ref}.pcb.rotation_deg")
        if angle not in (0, 90, 180, 270) or pcb.get("side", "front") != "front":
            raise PCBContractError("placement", f"{ref} 仅支持正面 0/90/180/270 度放置")
        rad = math.radians(angle)
        for number, geometries in pads.items():
            for geom in geometries:
                px = x + geom["x"] * math.cos(rad) - geom["y"] * math.sin(rad)
                py = y + geom["x"] * math.sin(rad) + geom["y"] * math.cos(rad)
                # Rotated rectangular pad extents are conservative for 90-degree rotations.
                half_w, half_h = (geom["w"] / 2, geom["h"] / 2) if angle in (0, 180) else (geom["h"] / 2, geom["w"] / 2)
                if px - half_w < clearance or py - half_h < clearance or px + half_w > width - clearance or py + half_h > height - clearance:
                    raise PCBContractError("pad_outside_board", f"{ref} 焊盘 {number} 超出板内安全边界")
        footprints[ref] = (root, pads, sha)
    if not footprints:
        raise PCBContractError("parts", "电路没有物理元件")
    endpoint_to_net, nets = _nets(circuit, parts)
    isolated_nets = {}
    for endpoint, label in sorted(unconnected_pad_nets(circuit).items()):
        number = max((n for n, _ in nets), default=0) + 1
        nets.append((number, label))
        isolated_nets[endpoint] = number
    net_names = dict(nets)
    audit: list[dict] = []
    blocks: list[str] = []
    for ref, (root, pads, sha) in footprints.items():
        p = parts[ref]
        comp, pcb = p["component"], p["pcb"]
        fp_id = comp["footprint"]
        root[1] = _a(fp_id, True)
        root[:] = [x for x in root if not (isinstance(x, list) and x and isinstance(x[0], Atom) and x[0].value in {"version", "generator", "generator_version", "at", "uuid"})]
        root.append([_a("at"), _a(_num(float(pcb["x_mm"]))), _a(_num(float(pcb["y_mm"]))), _a(_num(float(pcb.get("rotation_deg", 0))))])
        root.append([_a("uuid"), _a(_uuid("fp", ref), True)])
        root.append([_a("path"), _a(f"/{schematic_identity(circuit)}/{schematic_identity(circuit, ref)}", True)])
        _set_property(root, "Reference", ref)
        _set_property(root, "Value", str((p.get("attrs") or {}).get("value") or comp["mpn"]))
        _set_property(root, "Footprint", fp_id)
        _set_property(root, "Datasheet", comp["datasheet"])
        _set_property(root, "MPN", comp["mpn"])
        _set_property(root, "Manufacturer", comp.get("manufacturer", ""))
        _refresh_uuids(root, ref)
        inverse = {pad: pin for pin, pad in comp["pin_map"].items()}
        for pad in _children(root, "pad"):
            number = _value(pad[1])
            pin = inverse.get(number)
            logical_net_num = endpoint_to_net.get((ref, pin), 0) if pin else 0
            net_num = logical_net_num or isolated_nets.get((ref, pin), 0)
            if net_num:
                pad.append([_a("net"), _a(net_num), _a(net_names[net_num], True)])
            audit.append({"ref": ref, "pin": pin, "pad": number, "net": net_names.get(logical_net_num),
                          "pcb_net": net_names.get(net_num),
                          "footprint": fp_id, "footprint_sha256": sha, "pin_map_source": comp["pin_map_source"]})
        blocks.append(dump_sexpr(root))
    layers = '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (32 "B.Adhes" user) (33 "F.Adhes" user) (34 "B.Paste" user) (35 "F.Paste" user) (36 "B.SilkS" user) (37 "F.SilkS" user) (38 "B.Mask" user) (39 "F.Mask" user) (40 "Dwgs.User" user) (41 "Cmts.User" user) (44 "Edge.Cuts" user) (45 "Margin" user) (46 "B.CrtYd" user) (47 "F.CrtYd" user) (48 "B.Fab" user) (49 "F.Fab" user))'
    if target_version == 20260206:
        # KiCad 10 uses even copper layer IDs, F.Cu=0 and B.Cu=2. These were
        # verified by official pcbnew LoadBoard/SaveBoard, not guessed from v8.
        layers = '(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (9 "F.Adhes" user) (11 "B.Adhes" user) (13 "F.Paste" user) (15 "B.Paste" user) (5 "F.SilkS" user) (7 "B.SilkS" user) (1 "F.Mask" user) (3 "B.Mask" user) (17 "Dwgs.User" user) (19 "Cmts.User" user) (25 "Edge.Cuts" user) (27 "Margin" user) (31 "F.CrtYd" user) (29 "B.CrtYd" user) (35 "F.Fab" user) (33 "B.Fab" user))'
    net_defs = "\n".join(f'(net {n} {dump_sexpr(_a(label, True))})' for n, label in nets)
    title = str(name).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    board_text = (f'(kicad_pcb (version {target_version}) (generator "zhihui-pcb-real") '
                  f'(general (thickness 1.6)) (paper "A4") '
                  f'(title_block (title "{title}") (comment 1 "UNROUTED CANDIDATE - DRC UNAVAILABLE")) '
                  f'{layers} (setup (pad_to_mask_clearance 0)) (net 0 "")\n{net_defs}\n'
                  f'{_edge_cuts(width, height)}\n' + "\n".join(blocks) + "\n)\n")
    return {"board_text": board_text, "status": "unrouted_candidate",
            "mapping_evidence": "declared_source_requires_engineer_review", "pad_audit": audit,
            "footprints": {ref: {"id": parts[ref]["component"]["footprint"], "sha256": sha}
                           for ref, (_, _, sha) in footprints.items()},
            "board": {"width_mm": width, "height_mm": height, "layers": 2,
                      "min_clearance_mm": clearance, "min_trace_width_mm": trace_width},
            "routing": "unavailable", "drc": "unavailable", "gerber": "unavailable",
            "manufacturing": "unavailable"}


__all__ = ["PCBContractError", "parse_sexpr", "dump_sexpr", "load_footprint", "build_pcb_candidate"]
