# -*- coding: utf-8 -*-
"""Official KiCad DRC gate and traceable fabrication-candidate exports.

No file export is called until local pad/net audit and the current official DRC
report both pass. Outputs remain candidates requiring engineer/fabricator review.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path

from core.pcb_real import (PCBContractError, _children, _one, _value, _nets,
                           _a, build_pcb_candidate, dump_sexpr, load_footprint, parse_sexpr,
                           schematic_identity, unconnected_pad_nets)
from core.pcb_source_schematic import (build_real_schematic, compare_real_netlist,
                                       parse_official_real_netlist)
from core.validation_gate import validate_candidate


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_fp_lib_table(circuit: dict, roots, path: Path) -> None:
    """Register the verified local footprint libraries for KiCad DRC."""
    libraries = sorted({p["component"]["footprint"].split(":")[0] for p in circuit["parts"]
                        if p["type"] not in ("vcc", "gnd")})
    table = [_a("fp_lib_table"), [_a("version"), _a("7")]]
    for library in libraries:
        folder = next((Path(r) / (library + ".pretty") for r in roots
                       if (Path(r) / (library + ".pretty")).is_dir()), None)
        if folder is None:
            folder = next((Path(r) for r in roots if Path(r).name == library + ".pretty"), None)
        if folder is None:
            raise PCBContractError("footprint_unavailable", f"无法登记真实封装库 {library}")
        table.append([_a("lib"), [_a("name"), _a(library, True)], [_a("type"), _a("KiCad", True)],
                      [_a("uri"), _a(folder.resolve().as_posix(), True)], [_a("options"), _a("", True)],
                      [_a("descr"), _a("Verified local footprint source", True)]])
    path.write_text(dump_sexpr(table), encoding="utf-8")


def parse_drc_report(data: dict) -> dict:
    """Reject incomplete reports; unknown counts are never interpreted as zero."""
    required = ("violations", "unconnected_items", "schematic_parity")
    if not isinstance(data, dict) or not all(isinstance(data.get(k), list) for k in required):
        raise PCBContractError("drc_report_contract", "DRC 报告缺少完整 violations/unconnected_items/schematic_parity 数组")
    if not str(data.get("kicad_version", "")).strip() or not str(data.get("source", "")).strip():
        raise PCBContractError("drc_report_contract", "DRC 报告缺少工具版本或源文件")
    counts = {k: len(data[k]) for k in required}
    return {"status": "pass" if not any(counts.values()) else "fail", "counts": counts,
            "kicad_version": data["kicad_version"], "source": data["source"],
            "official_schematic_parity_checked": False}


def audit_board(circuit: dict, board_text: str, footprint_roots) -> dict:
    """Compare actual embedded pad geometry and net groups to CircuitJSON."""
    board = parse_sexpr(board_text)
    if not board or _value(board[0]) != "kicad_pcb":
        raise PCBContractError("board_syntax", "输入不是 KiCad PCB")
    version_node = _one(board, "version")
    if not version_node or len(version_node) != 2:
        raise PCBContractError("board_syntax", "PCB 缺少明确版本")
    version = int(_value(version_node[1]))
    # Revalidate metadata, local files, pin maps, and declared board constraints.
    baseline = build_pcb_candidate(circuit, footprint_roots=footprint_roots, target_version=version)
    baseline_board = parse_sexpr(baseline["board_text"])
    edge_geometry = lambda b: sorted((dump_sexpr(_one(e, "start")), dump_sexpr(_one(e, "end")))
                                    for e in _children(b, "gr_line") if _one(e, "layer") and _value(_one(e, "layer")[1]) == "Edge.Cuts")
    if edge_geometry(board) != edge_geometry(baseline_board):
        raise PCBContractError("board_outline", "板框与 CircuitJSON 声明不一致")
    layers = _one(board, "layers") or []
    copper_ids = set(range(0, 64, 2)) if version == 20260206 else set(range(32))
    copper_entries = [x for x in layers[1:] if isinstance(x, list) and len(x) >= 3
                      and (_value(x[1]).endswith(".Cu") or _value(x[2]) in ("signal", "power", "mixed")
                           or (_value(x[0]).isdigit() and int(_value(x[0])) in copper_ids))]
    if len(copper_entries) != 2 or {_value(x[1]) for x in copper_entries} != {"F.Cu", "B.Cu"}:
        raise PCBContractError("board_layers", "制造候选仅支持双层铜板")
    expected_layers = {_value(x[1]): _value(x[0]) for x in (_one(baseline_board, "layers") or [])[1:]
                       if isinstance(x, list) and len(x) >= 3 and _value(x[1]).endswith(".Cu")}
    if any(_value(x[0]) != expected_layers[_value(x[1])] for x in copper_entries):
        raise PCBContractError("board_layers", "铜层编号与所选 KiCad 格式不一致")
    parts = {p["id"]: p for p in circuit["parts"]}
    physical = {ref: p for ref, p in parts.items() if p["type"] not in ("vcc", "gnd")}
    endpoints, expected_nets = _nets(circuit, parts)
    expected_labels = dict(expected_nets)
    expected_groups = {}
    for (ref, pin), number in endpoints.items():
        if ref in physical:
            expected_groups.setdefault(expected_labels[number], set()).add((ref, physical[ref]["component"]["pin_map"][pin]))
    isolated_groups = {}
    for (ref, pin), label in unconnected_pad_nets(circuit).items():
        isolated_groups[label] = {(ref, physical[ref]["component"]["pin_map"][pin])}
    actual_groups = {}
    seen_refs = set()
    pads_audited = 0
    source_hashes = {}
    for fp in _children(board, "footprint"):
        refs = [p for p in _children(fp, "property") if len(p) >= 3 and _value(p[1]) == "Reference"]
        if len(refs) != 1:
            raise PCBContractError("board_reference", "封装需有唯一 Reference 属性")
        ref = _value(refs[0][2])
        if ref not in physical or ref in seen_refs:
            raise PCBContractError("board_reference", f"未知或重复 PCB 位号 {ref}")
        seen_refs.add(ref)
        component = physical[ref]["component"]
        for name, expected in {"Value": str((physical[ref].get("attrs") or {}).get("value") or component["mpn"]),
                               "MPN": component["mpn"], "Manufacturer": component.get("manufacturer", ""),
                               "Datasheet": component["datasheet"]}.items():
            fields = [p for p in _children(fp, "property") if len(p) >= 3 and _value(p[1]) == name]
            if len(fields) != 1 or _value(fields[0][2]) != expected:
                raise PCBContractError("board_component_identity", f"{ref} 的 {name} 字段与 CircuitJSON 不一致")
        path = _one(fp, "path")
        expected_path = f"/{schematic_identity(circuit)}/{schematic_identity(circuit, ref)}"
        if not path or len(path) != 2 or _value(path[1]) != expected_path:
            raise PCBContractError("board_source_path", f"{ref} 的原理图实例路径与当前电路不一致")
        at = _one(fp, "at")
        placement = physical[ref]["pcb"]
        expected_at = [float(placement["x_mm"]), float(placement["y_mm"]), float(placement.get("rotation_deg", 0))]
        actual_at = [float(_value(v)) for v in at[1:]] if at and len(at) in (3, 4) else []
        if len(actual_at) == 2:
            actual_at.append(0)  # KiCad SaveBoard omits the default zero rotation.
        if len(actual_at) != 3 or not all(math.isclose(v, expected, rel_tol=0, abs_tol=0.000001)
                                         for v, expected in zip(actual_at, expected_at)):
            raise PCBContractError("board_placement", f"{ref} 的板上放置与 CircuitJSON 不一致")
        if _value(fp[1]) != component["footprint"]:
            raise PCBContractError("board_footprint", f"{ref} 的板上封装与电路声明不一致")
        source, _, source_sha = load_footprint(component["footprint"], footprint_roots, max_version=version)
        source_pads = {_value(p[1]): p for p in _children(source, "pad")}
        actual_pads = {_value(p[1]): p for p in _children(fp, "pad")}
        if len(actual_pads) != len(_children(fp, "pad")) or set(source_pads) != set(actual_pads):
            raise PCBContractError("board_pad_geometry", f"{ref} 的焊盘集合与本地真实封装不一致")
        for number, pad in actual_pads.items():
            src = source_pads[number]
            for key in ("at", "size", "drill", "layers", "roundrect_rratio"):
                if dump_sexpr(_one(pad, key) or []) != dump_sexpr(_one(src, key) or []):
                    raise PCBContractError("board_pad_geometry", f"{ref}:{number} 的 {key} 与真实封装不一致")
            if [_value(x) for x in pad[1:4]] != [_value(x) for x in src[1:4]]:
                raise PCBContractError("board_pad_geometry", f"{ref}:{number} 的类型/形状与真实封装不一致")
            pad_nets = _children(pad, "net")
            if len(pad_nets) > 1:
                raise PCBContractError("board_pad_net", f"{ref}:{number} 含多个网络")
            if pad_nets:
                item = pad_nets[0]
                if len(item) == 2 and version == 20260206:
                    label = _value(item[1])  # KiCad 10 SaveBoard stores named nets.
                elif len(item) == 3 and _value(item[1]).isdigit():
                    label = _value(item[2]) if int(_value(item[1])) else ""
                else:
                    raise PCBContractError("board_pad_net", f"{ref}:{number} 的网络格式无效")
                if label:
                    actual_groups.setdefault(label, set()).add((ref, number))
            pads_audited += 1
        source_hashes[ref] = source_sha
    if seen_refs != set(physical):
        raise PCBContractError("board_reference", "板上元件位号与原理图不一致")
    # KiCad SaveBoard may clear the synthetic net on an explicitly isolated pad.
    # If retained, its original name and singleton membership must still match.
    for label, group in isolated_groups.items():
        if label in actual_groups and actual_groups.pop(label) != group:
            raise PCBContractError("board_net_mismatch", "板上焊盘网络与原理图连接不一致")
    if actual_groups != expected_groups:
        raise PCBContractError("board_net_mismatch", "板上焊盘网络与原理图连接不一致")
    return {"status": "pass", "pads_audited": pads_audited, "nets_audited": len(expected_nets),
            "footprint_sha256": source_hashes}


def _command(cli: str, args: list[str], timeout: float) -> dict:
    started = time.monotonic()
    command = [cli, *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, shell=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"argv": command, "exit_code": result.returncode, "stdout": result.stdout,
                "stderr": result.stderr, "elapsed_s": round(time.monotonic() - started, 3)}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": command, "exit_code": None, "stdout": "", "stderr": str(exc),
                "elapsed_s": round(time.monotonic() - started, 3)}


def _bom(circuit: dict, path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Reference", "Value", "MPN", "Manufacturer", "Footprint", "Datasheet"])
        for p in circuit["parts"]:
            if p["type"] in ("vcc", "gnd"):
                continue
            comp = p["component"]
            if not comp.get("mpn") or not comp.get("datasheet"):
                raise PCBContractError("bom_identity", f"{p['id']} 缺少可追溯器件身份")
            writer.writerow([p["id"], (p.get("attrs") or {}).get("value", ""), comp["mpn"],
                             comp.get("manufacturer", ""), comp["footprint"], comp["datasheet"]])


def run_manufacturing_pipeline(circuit: dict, board_text: str, output_dir,
                               cli_path: str | None = None, footprint_roots=None,
                               timeout_s: float = 90, source_schematic: dict | None = None) -> dict:
    """Audit -> actual-pin ERC/netlist -> DRC+parity -> exports -> review bundle.

    Always retains a unique run directory and manifest, including failures.
    A successful return is a fabrication candidate, not manufacturing release.
    The default generates the actual-pin source internally. Supplied source must
    exactly match the deterministic source generated from this CircuitJSON.
    """
    root = Path(output_dir).resolve()
    run_dir = root / ("run_" + uuid.uuid4().hex)
    run_dir.mkdir(parents=True)
    board = run_dir / "candidate.kicad_pcb"
    board.write_text(board_text, encoding="utf-8")
    (run_dir / "circuit.json").write_text(json.dumps(circuit, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {"status": "unavailable", "manufacturing_release": False,
                "review_required": ["electronic_engineer", "fabricator"], "run_dir": str(run_dir),
                "board_sha256": _sha(board), "pad_net_audit": None, "drc": {"status": "unavailable", "counts": None},
                "source_erc": {"status": "unavailable", "count": None},
                "source_netlist": {"status": "unavailable", "differences": None},
                "semantic_validation": None,
                "commands": [], "artifacts": [], "failure": None}
    def finish(code=None, message=None):
        if code:
            manifest["failure"] = {"code": code, "message": message}
        out = run_dir / "manifest.json"
        manifest["manifest_path"] = str(out)
        out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest
    try:
        roots = list(footprint_roots or [p for p in os.environ.get("KICAD_FOOTPRINT_DIR", "").split(os.pathsep) if p])
        manifest["pad_net_audit"] = audit_board(circuit, board_text, roots)
    except (PCBContractError, KeyError, ValueError, TypeError) as exc:
        return finish(getattr(exc, "code", "audit_contract"), str(exc))
    # The public pipeline cannot bypass logical structure/ERC/ratings/SPICE gates.
    # This is explicitly internal assurance; physical source below supplies the
    # official actual-pin ERC/netlist/parity evidence. HTTP also runs full T05.
    try:
        semantic = validate_candidate(circuit, source="manual", name="candidate",
                                      require_official=False, require_spice_runtime=True,
                                      work_dir=run_dir / "semantic_source")
        manifest["semantic_validation"] = semantic
        (run_dir / "semantic-validation.json").write_text(json.dumps(semantic, ensure_ascii=False, indent=2), encoding="utf-8")
    except (ValueError, TypeError, OSError) as exc:
        return finish("semantic_validation_unavailable", str(exc))
    if not semantic["accepted"]:
        manifest["status"] = "semantic_validation_failed"
        return finish("semantic_validation_failed", "结构/ERC/额定值/SPICE 语义前置未通过")
    cli = cli_path or os.environ.get("KICAD_CLI_PATH") or shutil.which("kicad-cli")
    if not cli or not Path(cli).is_file():
        return finish("cli_unavailable", "未找到 KiCad CLI，未调用任何制造导出")
    try:
        kit = build_real_schematic(circuit)
        if source_schematic is not None and any(source_schematic.get(k) != kit[k] for k in
                ("schematic_text", "symbol_library_text", "symbol_lib_table_text")):
            raise PCBContractError("source_mismatch", "所给源原理图与当前电路的实际引脚映射不一致")
        for name, key in [("candidate.kicad_sch", "schematic_text"),
                          ("ZhihuiReal.kicad_sym", "symbol_library_text"),
                          ("sym-lib-table", "symbol_lib_table_text")]:
            (run_dir / name).write_text(kit[key], encoding="utf-8")
    except (PCBContractError, KeyError, TypeError, ValueError) as exc:
        return finish(getattr(exc, "code", "source_unavailable"), str(exc))
    schematic = run_dir / "candidate.kicad_sch"
    manifest["source_sha256"] = _sha(schematic)
    try:
        write_fp_lib_table(circuit, roots, run_dir / "fp-lib-table")
    except PCBContractError as exc:
        return finish(exc.code, str(exc))
    (run_dir / "candidate.kicad_pro").write_text(json.dumps({"meta": {"version": 3}, "board": {"design_settings": {}}}), encoding="utf-8")
    clearance = float(circuit["board"]["min_clearance_mm"])
    width = float(circuit["board"]["min_trace_width_mm"])
    (run_dir / "candidate.kicad_dru").write_text(
        f'(version 1)\n(rule "Task clearance" (constraint clearance (min {clearance}mm)))\n'
        f'(rule "Task trace width" (constraint track_width (min {width}mm)))\n', encoding="utf-8")
    erc_report = run_dir / "source-erc.json"
    result = _command(str(cli), ["sch", "erc", "--format", "json", "--severity-all",
                                 "--exit-code-violations", "-o", str(erc_report), str(schematic)], timeout_s)
    manifest["commands"].append(result)
    try:
        erc = json.loads(erc_report.read_text(encoding="utf-8"))
        if not isinstance(erc.get("sheets"), list) or not erc.get("kicad_version") or not erc.get("source"):
            raise PCBContractError("source_erc_contract", "官方 ERC 报告缺少完整 sheets/版本/源文件")
        if any(not isinstance(s.get("violations"), list) for s in erc["sheets"]):
            raise PCBContractError("source_erc_contract", "官方 ERC 报告缺少完整 violations 数组")
        violations = [v for sheet in erc["sheets"] for v in sheet["violations"]]
        manifest["source_erc"] = {"status": "fail" if violations else "pass", "count": len(violations),
                                   "kicad_version": erc["kicad_version"], "report_sha256": _sha(erc_report)}
    except (OSError, ValueError, TypeError, PCBContractError) as exc:
        return finish("source_erc_unavailable", str(exc))
    if result["exit_code"] != 0 or violations:
        manifest["status"] = "source_erc_failed"
        return finish("source_erc_failed", "真实引脚源原理图官方 ERC 未通过")
    netlist = run_dir / "source.net"
    result = _command(str(cli), ["sch", "export", "netlist", "--format", "kicadsexpr",
                                 "-o", str(netlist), str(schematic)], timeout_s)
    manifest["commands"].append(result)
    try:
        observed = parse_official_real_netlist(netlist.read_text(encoding="utf-8"))
        differences = compare_real_netlist(circuit, observed)
        manifest["source_netlist"] = {"status": "fail" if differences else "pass", "differences": differences,
                                       "net_count": len(observed), "report_sha256": _sha(netlist)}
    except (OSError, ValueError, TypeError, PCBContractError) as exc:
        return finish("source_netlist_unavailable", str(exc))
    if result["exit_code"] != 0 or differences:
        manifest["status"] = "source_netlist_failed"
        return finish("source_netlist_failed", "官方实际焊盘网表与 CircuitJSON 不一致")
    if _sha(schematic) != manifest["source_sha256"]:
        return finish("source_changed", "原理图在验证期间发生变化")
    report = run_dir / "drc.json"
    result = _command(str(cli), ["pcb", "drc", "--format", "json", "--severity-all",
                               "--schematic-parity", "--exit-code-violations", "-o", str(report), str(board)], timeout_s)
    manifest["commands"].append(result)
    try:
        manifest["drc"] = parse_drc_report(json.loads(report.read_text(encoding="utf-8")))
        manifest["drc"]["official_schematic_parity_checked"] = True
        manifest["drc"]["report_sha256"] = _sha(report)
    except (OSError, ValueError, PCBContractError) as exc:
        return finish("drc_report_unavailable", str(exc))
    if result["exit_code"] != 0 or manifest["drc"]["status"] != "pass":
        manifest["status"] = "drc_failed"
        return finish("drc_failed", "官方 DRC 未通过，制造导出未执行")
    if _sha(board) != manifest["board_sha256"] or _sha(schematic) != manifest["source_sha256"]:
        return finish("board_changed", "DRC 后源板发生变化，拒绝导出")
    cam = run_dir / "cam"
    cam.mkdir()
    manifest["status"] = "export_pending"
    commands = [
        ["pcb", "export", "gerbers", "--layers", "F.Cu,B.Cu,F.Mask,B.Mask,F.SilkS,F.Paste,Edge.Cuts",
         "--output", str(cam) + os.sep, str(board)],
        ["pcb", "export", "drill", "--format", "excellon", "--excellon-units", "mm",
         "--excellon-separate-th", "--output", str(cam) + os.sep, str(board)],
        ["pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both",
         "--output", str(cam / "positions.csv"), str(board)],
    ]
    for args in commands:
        result = _command(str(cli), args, timeout_s)
        manifest["commands"].append(result)
        if result["exit_code"] != 0:
            manifest["status"] = "export_failed"
            return finish("export_failed", "KiCad 制造导出命令失败，未生成通过包")
        if _sha(board) != manifest["board_sha256"] or _sha(schematic) != manifest["source_sha256"]:
            return finish("board_changed", "导出期间源板发生变化")
    files = list(cam.iterdir())
    layer_suffixes = {"F_Cu": ["gbr", "gtl"], "B_Cu": ["gbr", "gbl"],
                      "F_Mask": ["gbr", "gts"], "B_Mask": ["gbr", "gbs"],
                      "F_Silkscreen": ["gbr", "gto"], "F_Paste": ["gbr", "gtp"],
                      "Edge_Cuts": ["gbr", "gm1"]}
    for layer, extensions in layer_suffixes.items():
        # KiCad versions use either F_SilkS or F_Silkscreen in filenames.
        names = [f"{layer}.{ext}" for ext in extensions] + [f"{layer.replace('F_Silkscreen', 'F_SilkS')}.{ext}" for ext in extensions]
        matching = [p for p in files if any(p.name.endswith(s) for s in names) and p.stat().st_size > 0]
        if not matching:
            manifest["status"] = "export_failed"
            return finish("export_incomplete", f"未生成非空 Gerber 层 {layer}")
        if not all("%FS" in p.read_text(encoding="ascii", errors="replace") and "M02*" in p.read_text(encoding="ascii", errors="replace") for p in matching):
            manifest["status"] = "export_failed"
            return finish("export_incomplete", f"Gerber 层 {layer} 缺少格式声明或结束标记")
    if not any(p.suffix == ".drl" and p.stat().st_size > 0 for p in files) or not (cam / "positions.csv").is_file():
        return finish("export_incomplete", "钻孔或贴片文件缺失")
    try:
        _bom(circuit, cam / "bom.csv")
    except (PCBContractError, OSError) as exc:
        return finish("bom_failed", str(exc))
    manifest["artifacts"] = [{"path": str(p), "sha256": _sha(p), "bytes": p.stat().st_size}
                             for p in sorted(cam.iterdir()) if p.is_file()]
    review = run_dir / "candidate-review.json"
    review.write_text(json.dumps({"manufacturing_release": False,
                                  "review_required": manifest["review_required"],
                                  "board_sha256": manifest["board_sha256"],
                                  "drc": manifest["drc"], "pad_net_audit": manifest["pad_net_audit"],
                                  "source_erc": manifest["source_erc"], "source_netlist": manifest["source_netlist"],
                                  "source_sha256": manifest["source_sha256"],
                                  "semantic_validation": manifest["semantic_validation"],
                                  "provenance": circuit.get("provenance"),
                                  "engineering_example": circuit.get("engineering_example")},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    bundle = run_dir / "fabrication_candidate.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for p in cam.iterdir():
            if p.is_file():
                archive.write(p, "cam/" + p.name)
        archive.write(board, board.name)
        archive.write(report, report.name)
        archive.write(review, review.name)
        archive.write(run_dir / "circuit.json", "circuit.json")
        archive.write(run_dir / "candidate.kicad_dru", "candidate.kicad_dru")
        archive.write(run_dir / "candidate.kicad_pro", "candidate.kicad_pro")
        for p in (schematic, netlist, erc_report, run_dir / "ZhihuiReal.kicad_sym",
                  run_dir / "sym-lib-table", run_dir / "fp-lib-table", run_dir / "semantic-validation.json"):
            archive.write(p, p.name)
    manifest["status"] = "fabrication_candidate_for_review"
    manifest["bundle"] = {"path": str(bundle), "sha256": _sha(bundle)}
    return finish()


__all__ = ["audit_board", "parse_drc_report", "run_manufacturing_pipeline"]
