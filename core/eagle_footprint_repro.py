"""从 Eagle 封装定义复现 KiCad .kicad_mod，并逐属性核对等价性。

只复现铜焊盘几何（编号/类型/形状/中心/尺寸/孔径/旋转）。源封装的丝印、
禁止区、装配层与 3D 模型不复现，因此等价性结论只覆盖焊盘，不能外推为
"整个封装一致"。

源几何不被支持时抛错，绝不用近似值顶替：近似出来的封装会让下游 PCB 与
源板物理不兼容，而这种错误在网表核对里看不出来。
"""
from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation
from pathlib import Path
import xml.etree.ElementTree as ET

from core.symbol_import import parse_sexpr

REPRO_LIBRARY_PREFIX = "EagleRepro_"
REPRO_GENERATOR = "zhihui_eagle_repro"
REPRO_FORMAT_VERSION = 20240108  # KiCad 8；与 core.pcb_real.BOARD_VERSION 一致

# Eagle smd layer 编号 -> KiCad 铜层
_SMD_COPPER_LAYER = {"1": "F.Cu", "16": "B.Cu"}
# Eagle pad shape 属性 -> KiCad 形状；缺省为圆形
_THT_SHAPE = {"": "circle", "round": "circle", "square": "rect"}

NOT_REPRODUCED = [
    "silkscreen graphics (Eagle tPlace/tNames/tValues)",
    "documentation layer graphics (Eagle tDocu)",
    "courtyard (not present in the Eagle source package)",
    "3D model",
]


class UnsupportedSourceGeometry(ValueError):
    """源封装含有本复现器不支持的几何，拒绝近似。"""


def _dec(text: str | None, label: str) -> Decimal:
    try:
        return Decimal(str(text).strip())
    except (InvalidOperation, AttributeError, ValueError) as exc:
        raise UnsupportedSourceGeometry(f"{label} 不是合法数值：{text!r}") from exc


def _num(value: Decimal) -> str:
    """输出最短且与原值精确相等的十进制文本。"""
    text = format(value.normalize(), "f")
    return text if text else "0"


def _eagle_rotation(text: str | None) -> Decimal:
    """Eagle rot="R<deg>"（逆时针为正）-> 角度数值。"""
    raw = str(text or "R0").strip()
    if not raw.upper().startswith("R"):
        raise UnsupportedSourceGeometry(f"无法解析源旋转：{text!r}")
    return _dec(raw[1:], "旋转角度")


def parse_package_pads(package_node: ET.Element) -> list[dict]:
    """读取 Eagle <package> 内的 pad/smd，保留源坐标系（X 右、Y 上）。"""
    pads: list[dict] = []
    seen: set[str] = set()
    for node in package_node:
        if node.tag not in ("pad", "smd"):
            continue
        name = node.get("name")
        if not name:
            raise UnsupportedSourceGeometry("源焊盘缺少编号")
        if name in seen:
            raise UnsupportedSourceGeometry(f"源封装焊盘编号重复：{name}")
        seen.add(name)
        x = _dec(node.get("x"), f"焊盘 {name} x")
        y = _dec(node.get("y"), f"焊盘 {name} y")
        rotation = _eagle_rotation(node.get("rot"))
        if node.tag == "smd":
            copper = _SMD_COPPER_LAYER.get(str(node.get("layer", "1")).strip())
            if copper is None:
                raise UnsupportedSourceGeometry(
                    f"焊盘 {name} 所在 SMD 层 {node.get('layer')!r} 不是顶/底层")
            roundness = str(node.get("roundness", "0")).strip()
            if roundness not in ("0", ""):
                raise UnsupportedSourceGeometry(
                    f"焊盘 {name} 带圆角 {roundness}，本复现器不支持近似")
            pads.append({"name": name, "kind": "smd", "shape": "rect",
                         "x_mm": x, "y_mm": y,
                         "w_mm": _dec(node.get("dx"), f"焊盘 {name} dx"),
                         "h_mm": _dec(node.get("dy"), f"焊盘 {name} dy"),
                         "drill_mm": None, "rotation_deg": rotation,
                         "copper_layer": copper})
        else:
            shape_attr = str(node.get("shape", "")).strip().lower()
            shape = _THT_SHAPE.get(shape_attr)
            if shape is None:
                raise UnsupportedSourceGeometry(
                    f"焊盘 {name} 形状 {shape_attr!r} 不在支持范围（round/square）")
            diameter = _dec(node.get("diameter"), f"焊盘 {name} diameter")
            drill = node.get("drill")
            if drill is None:
                raise UnsupportedSourceGeometry(f"焊盘 {name} 缺少孔径")
            # Eagle 方形焊盘用 diameter 同时表示边长
            w = h = diameter
            if node.get("flags", "") and "slot" in str(node.get("flags", "")):
                raise UnsupportedSourceGeometry(f"焊盘 {name} 是长条槽孔，本复现器不支持")
            pads.append({"name": name, "kind": "thru_hole", "shape": shape,
                         "x_mm": x, "y_mm": y, "w_mm": w, "h_mm": h,
                         "drill_mm": _dec(drill, f"焊盘 {name} drill"),
                         "rotation_deg": rotation, "copper_layer": "*.Cu"})
    if not pads:
        raise UnsupportedSourceGeometry("源封装没有任何铜焊盘")
    return pads


def to_kicad_pads(source_pads: list[dict]) -> list[dict]:
    """Eagle（Y 上、逆时针为正）-> KiCad（Y 下、顺时针为正）。

    关于 X 轴镜像后，源逆时针角在 KiCad 约定下数值不变：镜像把逆时针变成
    顺时针，而 KiCad 的正角本身就是屏幕顺时针。
    """
    out = []
    for pad in source_pads:
        out.append({**pad,
                    "x_mm": pad["x_mm"],
                    "y_mm": -pad["y_mm"],
                    "rotation_deg": pad["rotation_deg"]})
    return out


def _pad_sexp(pad: dict) -> str:
    at = f"(at {_num(pad['x_mm'])} {_num(pad['y_mm'])} {_num(pad['rotation_deg'])})"
    size = f"(size {_num(pad['w_mm'])} {_num(pad['h_mm'])})"
    if pad["kind"] == "thru_hole":
        drill = f"(drill {_num(pad['drill_mm'])})"
        layers = '(layers "*.Cu" "*.Mask")'
    else:
        drill = ""
        side = "F" if pad["copper_layer"] == "F.Cu" else "B"
        layers = f'(layers "{side}.Cu" "{side}.Paste" "{side}.Mask")'
    return (f'  (pad "{pad["name"]}" {pad["kind"]} {pad["shape"]} {at} {size}'
            + (f" {drill}" if drill else "")
            + f" {layers})\n")


def render_footprint(footprint_name: str, source_pads: list[dict], *,
                     source_library: str, source_package: str,
                     source_sha256: str, source_url: str | None = None) -> str:
    """生成 .kicad_mod 文本；焊盘几何逐项来自源封装。"""
    pads = to_kicad_pads(source_pads)
    provenance = f"Reproduced from Eagle {source_library}/{source_package}"
    lines = [
        f'(footprint "{footprint_name}"',
        f"  (version {REPRO_FORMAT_VERSION})",
        f'  (generator "{REPRO_GENERATOR}")',
        '  (layer "F.Cu")',
        f'  (descr "{provenance}; pad copper geometry only")',
        f'  (tags "zhihui eagle-repro {source_package}")',
        '  (property "Reference" "REF**"',
        "    (at 0 0 0)",
        '    (layer "F.SilkS")',
        "    (effects (font (size 1 1) (thickness 0.15)))",
        "  )",
        f'  (property "Value" "{footprint_name}"',
        "    (at 0 0 0)",
        '    (layer "F.Fab")',
        "    (effects (font (size 1 1) (thickness 0.15)))",
        "  )",
        f'  (property "Source_Library" "{source_library}" (at 0 0 0) (layer "F.Fab") (hide yes)',
        "    (effects (font (size 1 1) (thickness 0.15)))",
        "  )",
        f'  (property "Source_Package" "{source_package}" (at 0 0 0) (layer "F.Fab") (hide yes)',
        "    (effects (font (size 1 1) (thickness 0.15)))",
        "  )",
        f'  (property "Source_SHA256" "{source_sha256}" (at 0 0 0) (layer "F.Fab") (hide yes)',
        "    (effects (font (size 1 1) (thickness 0.15)))",
        "  )",
    ]
    if source_url:
        lines.append(
            f'  (property "Source_URL" "{source_url}" (at 0 0 0) (layer "F.Fab") (hide yes)'
            "\n    (effects (font (size 1 1) (thickness 0.15)))\n  )")
    body = "".join(_pad_sexp(p) for p in pads)
    return "\n".join(lines) + "\n" + body + ")\n"


def read_footprint_pads(mod_text: str) -> list[dict]:
    """独立读回 .kicad_mod 的焊盘几何，用于与源逐项核对。"""
    tree = parse_sexpr(mod_text)
    if not tree or str(tree[0]) != "footprint":
        raise ValueError("不是 footprint 文件")
    pads = []
    for item in tree:
        if not isinstance(item, list) or not item or item[0] != "pad":
            continue
        if len(item) < 4:
            raise ValueError("焊盘缺少编号、类型或形状")
        fields = {x[0]: x[1:] for x in item if isinstance(x, list) and x}
        at = fields.get("at", [])
        size = fields.get("size", [])
        drill = fields.get("drill")
        layers = [str(v) for v in fields.get("layers", [])]
        pads.append({
            "name": str(item[1]), "kind": str(item[2]), "shape": str(item[3]),
            "x_mm": _dec(at[0], "at x") if len(at) > 0 else Decimal(0),
            "y_mm": _dec(at[1], "at y") if len(at) > 1 else Decimal(0),
            "rotation_deg": _dec(at[2], "at rot") if len(at) > 2 else Decimal(0),
            "w_mm": _dec(size[0], "size w") if len(size) > 0 else Decimal(0),
            "h_mm": _dec(size[1], "size h") if len(size) > 1 else Decimal(0),
            "drill_mm": _dec(drill[0], "drill") if drill else None,
            "layers": layers,
        })
    return pads


def verify_pad_equivalence(source_pads: list[dict], mod_text: str) -> dict:
    """把源几何（已变换到 KiCad 坐标系）与生成文件读回的几何逐属性核对。

    读回路径独立于渲染路径：渲染写错、或读回解析错，都会在这里暴露。
    """
    expected = to_kicad_pads(source_pads)
    actual = read_footprint_pads(mod_text)
    actual_by_name = {p["name"]: p for p in actual}
    rows = []
    for want in expected:
        got = actual_by_name.get(want["name"])
        if got is None:
            rows.append({"pad": want["name"], "matched": False,
                         "mismatches": ["pad missing from reproduced footprint"]})
            continue
        mismatches = []
        for field in ("kind", "shape"):
            if str(want[field]) != str(got[field]):
                mismatches.append(f"{field}: source={want[field]} reproduced={got[field]}")
        for field in ("x_mm", "y_mm", "w_mm", "h_mm", "rotation_deg"):
            if Decimal(want[field]) != Decimal(got[field]):
                mismatches.append(f"{field}: source={want[field]} reproduced={got[field]}")
        if (want["drill_mm"] is None) != (got["drill_mm"] is None) or (
                want["drill_mm"] is not None
                and Decimal(want["drill_mm"]) != Decimal(got["drill_mm"])):
            mismatches.append(f"drill_mm: source={want['drill_mm']} reproduced={got['drill_mm']}")
        required = "*.Cu" if want["kind"] == "thru_hole" else want["copper_layer"]
        if required not in got["layers"]:
            mismatches.append(f"layers: reproduced {got['layers']} lacks {required}")
        rows.append({"pad": want["name"], "matched": not mismatches,
                     "mismatches": mismatches})
    extra = sorted(actual_by_name.keys() - {p["name"] for p in expected})
    matched = sum(r["matched"] for r in rows)
    return {"source_pad_count": len(expected), "reproduced_pad_count": len(actual),
            "matched_pads": matched, "unmatched_pads": len(rows) - matched,
            "unexpected_extra_pads": extra, "per_pad": rows,
            "pad_geometry_exact": matched == len(expected) and not extra,
            "not_reproduced": list(NOT_REPRODUCED)}


def repro_library_name(source_library: str) -> str:
    """Eagle 库名 -> 复现封装库名（只保留 KiCad 库标识符允许的字符）。"""
    safe = "".join(c if c.isalnum() or c in "_.+-" else "_" for c in source_library)
    return REPRO_LIBRARY_PREFIX + (safe or "source")


def repro_footprint_id(source_library: str, source_package: str) -> str:
    """返回 CircuitJSON / KiCad 使用的 `Library:Name` 封装标识。"""
    safe_pkg = "".join(c if c.isalnum() or c in "_.+-" else "_" for c in source_package)
    return f"{repro_library_name(source_library)}:{safe_pkg}"


_MOUNTING_PAD_NAMES = {"nc", "nc1", "nc2", "mp", "mh", "mount", "shield"}


def _pad_role(name: str) -> str:
    """把机械/安装焊盘归一为同一角色，编号焊盘保留原名。"""
    return "mount" if str(name).strip().lower() in _MOUNTING_PAD_NAMES else str(name).strip()


def pad_geometry_signature(pads: list[dict]) -> list[tuple]:
    """与封装原点无关的焊盘几何签名。

    以 1 号焊盘（无则取排序最小的编号焊盘）为原点做平移，再按
    (角色, 类型, 形状, 宽, 高, 孔径, 相对x, 相对y) 排序。绝对位置和旋转
    不参与：前者是库作者约定，后者对圆形/方形焊盘不改变铜形。
    """
    numbered = [p for p in pads if _pad_role(p["name"]) != "mount"]
    anchor_pool = [p for p in numbered if str(p["name"]).strip() == "1"] or sorted(
        numbered, key=lambda p: str(p["name"]))
    anchor = anchor_pool[0] if anchor_pool else sorted(pads, key=lambda p: str(p["name"]))[0]
    ax, ay = Decimal(anchor["x_mm"]), Decimal(anchor["y_mm"])
    rows = []
    for pad in pads:
        rows.append((
            _pad_role(pad["name"]), str(pad["kind"]), str(pad["shape"]),
            Decimal(pad["w_mm"]).normalize(), Decimal(pad["h_mm"]).normalize(),
            None if pad.get("drill_mm") is None else Decimal(pad["drill_mm"]).normalize(),
            (Decimal(pad["x_mm"]) - ax).normalize(),
            (Decimal(pad["y_mm"]) - ay).normalize(),
        ))
    return sorted(rows, key=lambda r: tuple(str(v) for v in r))


def scan_stock_equivalents(source_pads: list[dict], footprint_root: Path,
                           *, libraries: list[str] | None = None,
                           max_files: int | None = None) -> dict:
    """扫描本机 KiCad 库存封装库，寻找与源焊盘几何精确等价者。

    只做几何等价判定，不判定丝印/禁止区/3D 模型。返回候选清单与扫描分母，
    使"库存无精确等价"成为实测结论而不是假设。
    """
    root = Path(footprint_root)
    if not root.is_dir():
        raise FileNotFoundError(f"库存封装库目录不存在：{root}")
    target = pad_geometry_signature(to_kicad_pads(source_pads))
    wanted_count = len(target)
    lib_dirs = ([root / f"{name}.pretty" for name in libraries] if libraries
                else sorted(p for p in root.glob("*.pretty") if p.is_dir()))
    scanned = 0
    skipped_shape = 0
    compared = 0
    matches = []
    for lib_dir in lib_dirs:
        if not lib_dir.is_dir():
            continue
        for path in sorted(lib_dir.glob("*.kicad_mod")):
            if max_files is not None and scanned >= max_files:
                break
            raw = path.read_bytes()
            scanned += 1
            # 廉价预筛：焊盘数不符的文件不可能等价，跳过完整解析
            if raw.count(b"(pad ") != wanted_count:
                skipped_shape += 1
                continue
            try:
                pads = read_footprint_pads(raw.decode("utf-8-sig"))
            except (ValueError, UnicodeError, UnsupportedSourceGeometry):
                skipped_shape += 1
                continue
            if len(pads) != wanted_count:
                skipped_shape += 1
                continue
            compared += 1
            if pad_geometry_signature(pads) == target:
                matches.append({"footprint": str(path),
                                "library": lib_dir.name[: -len(".pretty")],
                                "name": path.stem,
                                "sha256": hashlib.sha256(raw).hexdigest(),
                                "pad_geometry_exact": True})
    return {"footprint_root": str(root),
            "libraries_scanned": [d.name for d in lib_dirs if d.is_dir()],
            "files_scanned": scanned,
            "files_rejected_by_pad_count_or_parse": skipped_shape,
            "files_fully_compared": compared,
            "exact_geometry_matches": matches,
            "exact_stock_equivalent_found": bool(matches),
            "comparison_scope": ("pad count, kind, shape, size, drill and relative "
                                 "positions; silkscreen, courtyard and 3D model not compared")}


def write_repro_library(out_dir: Path, entries: list[dict]) -> list[dict]:
    """把复现封装写成 out_dir/<lib>.pretty/<name>.kicad_mod。

    entries 每项含 library、footprint_id、source_library、source_package、text。
    返回写盘记录。
    """
    out_dir = Path(out_dir)
    written = []
    for entry in entries:
        lib_dir = out_dir / f"{entry['library']}.pretty"
        lib_dir.mkdir(parents=True, exist_ok=True)
        name = entry["footprint_id"].split(":", 1)[1]
        path = lib_dir / f"{name}.kicad_mod"
        # 显式写字节：KiCad 库文件用 LF，且写盘内容必须与核对时哈希的文本逐字节
        # 一致，否则 Windows 上 write_text 的换行翻译会让 sha256 对不上。
        raw = entry["text"].encode("utf-8")
        path.write_bytes(raw)
        written.append({"library": entry["library"],
                        "footprint_id": entry["footprint_id"],
                        "path": str(path),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "bytes": len(raw),
                        "source_library": entry["source_library"],
                        "source_package": entry["source_package"]})
    return written
