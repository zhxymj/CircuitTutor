"""Create source-linked external tasks and lossless topology drafts, offline.

The source-specific Eagle types intentionally remain outside the app catalog:
unknown ICs are retained as unresolved source parts, never replaced by a generic
chip. A graph/schema extraction is not a passed electrical or manufacturing gate.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET

from core.circuit_schema import validate_v2_metadata
from core import eagle_footprint_repro as repro
from core import llm, catalog, clarify
from evaluation.external_reference_audit import ROOT, audit, parse_eagle


def eagle_package_nodes(raw_eagle: bytes) -> dict:
    """(library, package) -> Eagle <package> 节点。"""
    schematic = ET.fromstring(raw_eagle).find("drawing/schematic")
    nodes = {}
    for library in schematic.findall("libraries/library"):
        for package in library.findall("packages/package"):
            nodes[(library.get("name"), package.get("name"))] = package
    return nodes


def reproduce_source_footprints(source: dict, schematic: dict, raw_eagle: bytes) -> dict:
    """为源板实际用到的每个物理封装复现 .kicad_mod，并逐属性自核。

    复现结果同时被读回核对：渲染写错或读回解析错都会在这里失败，而不是等到
    下游 PCB 才发现物理封装与源板不兼容。
    """
    packages = eagle_package_nodes(raw_eagle)
    physical_refs = {m["part"] for m in schematic["pin_to_pad"]}
    wanted = {}
    for part in schematic["components"]:
        if part["reference"] in physical_refs and part.get("package"):
            wanted[(part["library"], part["package"])] = True
    source_sha256 = hashlib.sha256(raw_eagle).hexdigest()
    entries, by_package = [], {}
    for (library, package) in sorted(wanted):
        node = packages.get((library, package))
        if node is None:
            raise ValueError(f"源 Eagle 封装定义缺失：{library}/{package}")
        source_pads = repro.parse_package_pads(node)
        footprint_id = repro.repro_footprint_id(library, package)
        text = repro.render_footprint(
            package, source_pads, source_library=library, source_package=package,
            source_sha256=source_sha256,
            source_url=source["repo_url"] + "/blob/" + source["commit"] + "/"
                       + quote(schematic["path"].split("/", 1)[1]))
        verification = repro.verify_pad_equivalence(source_pads, text)
        if not verification["pad_geometry_exact"]:
            raise ValueError(f"复现封装 {footprint_id} 与源焊盘几何不等价")
        entries.append({"library": repro.repro_library_name(library),
                        "footprint_id": footprint_id,
                        "source_library": library, "source_package": package,
                        "text": text})
        by_package[f"{library}:{package}"] = {
            "footprint_id": footprint_id, "library": repro.repro_library_name(library),
            "source_pad_count": len(source_pads),
            "source_pads": [{k: (str(v) if v is not None else None) for k, v in p.items()}
                            for p in source_pads],
            "verification": json.loads(json.dumps(verification, default=str)),
            "footprint_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
    return {"source_sha256": source_sha256, "entries": entries, "by_package": by_package}


def audit_source_passive_scope(schematic: dict, candidate: dict) -> dict:
    """用源件清单回答"上拉与额定值"两项遗留核对，而不是留空称未验证。

    结论只覆盖"候选是否忠实于源"，不代表该系统整体电气安全：被动转接板本身
    不承担上拉，上拉责任在主机或外设板。

    电源符号没有物理焊盘，因此按参与网络的源件与候选件类型统计，不能只看
    pin_to_pad，否则会把 6 个 GND/3.3V 符号整个漏掉。
    """
    parts = {p["reference"]: p for p in schematic["components"]}
    in_net_refs = {pin["part"] for net in schematic["nets"] for pin in net["pins"]}
    used = [parts[r] for r in sorted(in_net_refs & set(parts))]
    marker_refs = {p["reference"] for p in used if p["library"] == "SparkFun-PowerSymbols"}
    markers = [p for p in used if p["reference"] in marker_refs]
    non_markers = [p for p in used if p["reference"] not in marker_refs]
    # convert_passive_i2c_adapter 对任何非连接器/非电源符号的源件直接抛错，
    # 因此走到这里即结构性保证源板不含电阻等带额定值元件；仍实测记数以便回归。
    non_connector = [p for p in non_markers if "Connector" not in p["library"]]
    candidate_types = Counter(p["type"] for p in candidate["parts"])
    candidate_passive_other = sorted(
        t for t in candidate_types if t not in ("i2c_header", "gnd", "vcc"))
    connectors = [p for p in non_markers if "Connector" in p["library"]]
    return {
        "source_electrical_parts": len(used),
        "source_connector_parts": len(connectors),
        "source_power_marker_parts": len(markers),
        "source_non_connector_non_marker_parts": [
            {"reference": p["reference"], "library": p["library"],
             "deviceset": p["deviceset"], "value": p["value"]} for p in non_connector],
        "candidate_part_type_counts": dict(sorted(candidate_types.items())),
        "candidate_pullup_resistors": candidate_types.get("resistor", 0)
                                      + candidate_types.get("resistor_array", 0),
        "candidate_non_connector_non_supply_types": candidate_passive_other,
        "pullup_status": ("not_present_in_source"
                          if not non_connector and not candidate_passive_other
                          else "requires_review"),
        "pullup_conclusion": (
            "源板参与网络的器件只有连接器与电源符号，不含任何电阻，候选与源一致；"
            "I2C 上拉责任在系统级（主机或外设板），不由本被动转接板承担。"
            "ERC 的 I2C 缺上拉告警保留，不抑制。"
            if not non_connector and not candidate_passive_other else
            "源或候选含连接器/电源符号以外的器件，需逐项核对上拉网络是否一致"),
        "ratings_status": ("not_applicable_from_source"
                           if not non_connector and not candidate_passive_other
                           else "requires_component_review"),
        "ratings_conclusion": (
            "源 Eagle 文件只含无源连接器与电源符号，没有任何带额定值的元件；连接器的"
            "电流/电压额定值属数据手册项，不存在于源文件中，因此源侧无额定值可核对。"
            "候选侧 ratings 门禁 danger_count 为 0。"
            if not non_connector and not candidate_passive_other else
            "源含连接器/电源符号以外的元件，额定值需按数据手册逐项核对"),
        "basis": ("counted from the fixed source schematic net membership and the candidate "
                  "part list; not inferred from the catalog"),
        "scope_limit": "只回答候选是否忠实于源，不代表该系统整体电气安全，也不构成制造放行",
    }



def dump(path: Path, data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def fingerprint(data):
    return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def selected_validation_command(cli_path: str | None, source_id: str) -> str:
    """Record the explicit official CLI used by this reproducible conversion."""
    setting = ("$env:KICAD_CLI_PATH='" + cli_path.replace("'", "''") + "'; ") if cli_path else ""
    return setting + "python -X utf8 -B -m evaluation.external_reference_cases --convert-source " + source_id


def classify_legacy_probe(probe: dict) -> dict:
    """A CLI exit code is not evidence of a populated or electrically valid netlist."""
    populated = (probe["netlist_exit_code"] == 0 and
                 probe["netlist_component_count"] > 0 and probe["netlist_node_count"] > 0)
    erc_clean = probe["erc_exit_code"] == 0 and probe["erc_json_violation_count"] == 0
    if not populated:
        reason = ("Official CLI produced no usable component/node netlist "
                  "despite exit code 0; legacy connectivity remains unconverted")
    elif not erc_clean:
        reason = (f"Official ERC has {probe['erc_json_violation_count']} violations; "
                  "legacy connectivity remains unconverted")
    else:
        reason = "Official output still needs source-to-catalog conversion and independent net matching"
    return {"status":"incomplete", "electrical_accepted":False,
            "netlist_semantic_status":"populated" if populated else "invalid_or_empty",
            "erc_status":"zero_violations" if erc_clean else "violations_or_unavailable",
            "reason":reason}


def probe_legacy_kicad(schematic: dict, cli_path: str | Path) -> dict:
    """Probe the original in a disposable directory; retain counts, never an empty success."""
    cli = Path(cli_path).resolve()
    if not cli.is_file():
        raise FileNotFoundError(f"KiCad CLI not found: {cli}")
    source = (ROOT / schematic["path"]).resolve()
    version_run = subprocess.run([str(cli), "version"], capture_output=True,
                                 text=True, errors="replace", timeout=30)
    if version_run.returncode:
        raise RuntimeError(f"KiCad CLI version failed: {version_run.stderr[:300]}")
    with tempfile.TemporaryDirectory(prefix="zhihui-legacy-probe-") as temp:
        net_path = Path(temp) / "source.xml"
        erc_path = Path(temp) / "source.erc.json"
        net_args = ["sch", "export", "netlist", "--format", "kicadxml",
                    "-o", str(net_path), str(source)]
        erc_args = ["sch", "erc", "--format", "json", "-o", str(erc_path), str(source)]
        net_run = subprocess.run([str(cli), *net_args], capture_output=True,
                                 text=True, errors="replace", timeout=90)
        erc_run = subprocess.run([str(cli), *erc_args], capture_output=True,
                                 text=True, errors="replace", timeout=90)
        component_count = node_count = 0
        net_error = None
        if net_path.is_file():
            try:
                net_root = ET.parse(net_path).getroot()
                component_count = len(net_root.findall(".//components/comp"))
                node_count = len(net_root.findall(".//nets/net/node"))
            except ET.ParseError as error:
                net_error = f"invalid XML: {error}"[:300]
        else:
            net_error = "netlist file not produced"
        erc_count = None
        erc_error = None
        if erc_path.is_file():
            try:
                erc_json = json.loads(erc_path.read_text(encoding="utf-8"))
                erc_count = sum(len(sheet.get("violations", [])) for sheet in erc_json.get("sheets", []))
            except (ValueError, TypeError) as error:
                erc_error = f"invalid ERC JSON: {error}"[:300]
        else:
            erc_error = "ERC JSON file not produced"
        console = erc_run.stdout + "\n" + erc_run.stderr
        console_match = re.search(r"(\d+)\s*条违规项", console)
        probe = {"source_schematic_sha256":schematic["sha256"],
                 "source_schematic_path":schematic["path"],
                 "recorded_at_utc":datetime.now(timezone.utc).isoformat(),
                 "tool_path":str(cli), "tool_version":version_run.stdout.strip(),
                 "netlist_command":[str(cli), *net_args[:6], "<temporary-output>", str(source)],
                 "erc_command":[str(cli), *erc_args[:5], "<temporary-output>", str(source)],
                 "netlist_exit_code":net_run.returncode,
                 "netlist_component_count":component_count,"netlist_node_count":node_count,
                 "netlist_bytes":net_path.stat().st_size if net_path.is_file() else 0,
                 "netlist_error":net_error or (net_run.stderr.strip()[:300] or None),
                 "erc_exit_code":erc_run.returncode,
                 "erc_json_violation_count":erc_count,
                 "erc_console_reported_count":int(console_match.group(1)) if console_match else None,
                 "erc_error":erc_error or (erc_run.stderr.strip()[:300] or None)}
    probe.update(classify_legacy_probe(probe))
    return probe


def endpoint_id(pin):
    return quote(pin["part"],safe="_-.$") + ":" + quote(pin["gate"] + "." + pin["pin"],safe="_-.$")


def topology_draft(source: dict, schematic: dict) -> dict:
    maps = {}
    multi = {}
    for mapping in schematic["pin_to_pad"]:
        key = quote(mapping["gate"] + "." + mapping["pin"],safe="_-.$")
        if len(mapping["pads"]) == 1:
            maps.setdefault(mapping["part"],{})[key] = mapping["pads"][0]
        else:
            multi.setdefault(mapping["part"],{})[key] = mapping["pads"]
    parts = []
    for part in schematic["components"]:
        component = {"symbol":"Eagle:" + part["library"] + ":" + part["deviceset"],
                     "pin_map_source":source["repo_url"] + "/blob/" + source["commit"] + "/" + quote(schematic["path"].split("/",1)[1]),
                     "pin_map":maps.get(part["reference"],{})}
        if part.get("package"):
            component["footprint"] = "Eagle:" + part["library"] + ":" + part["package"]
        if multi.get(part["reference"]):
            component["pin_map_multiple_pads"] = multi[part["reference"]]
        parts.append({"id":quote(part["reference"],safe="_-.$"),"type":"external_eagle_unmapped",
                      "attrs":{"value":part["value"]},"component":component,"source_part":part})
    connections = []
    singleton_nets = []
    for net in schematic["nets"]:
        endpoints = [endpoint_id(p) for p in net["pins"]]
        # A chain has the same connected component as an Eagle net and avoids
        # arbitrary stars. Retain original full net membership independently.
        connections.extend([a,b,net["name"]] for a,b in zip(endpoints,endpoints[1:]))
        if len(endpoints)<2:
            singleton_nets.append(net)
    connected_ids = {e.split(":",1)[0] for c in connections for e in c[:2]}
    excluded_parts = [p for p in parts if p["id"] not in connected_ids]
    parts = [p for p in parts if p["id"] in connected_ids]
    return {"version":2,"title":source["source_id"] + " upstream topology draft",
            "parts":parts,"connections":connections,"source_nets":schematic["nets"],
            "singleton_nets":singleton_nets,"excluded_unconnected_or_graphical_parts":excluded_parts,
            "provenance":{"origin":"import","source_id":source["source_id"],
                "commit":source["commit"],"source_schematic_sha256":schematic["sha256"],
                "hardware_license":source["hardware_license"],"derivative_license":source["hardware_license"]},
            "acceptance":{"status":"unconverted_reference","catalog_supported":False,"electrical_accepted":False,
                          "reason":"Actual Eagle devices/gates are preserved; component-specific catalog mappings require review"}}


def convert_passive_i2c_adapter(source: dict, schematic: dict, raw_eagle: bytes) -> dict:
    """Map a four-net passive I2C adapter using actual net/pin/pad facts.

    Refuse any extra electrical part or ambiguous pin. This is not an MCU or
    sensor board, and catalog conversion alone is not electrical acceptance.
    """
    if schematic["format"] != "eagle_xml" or schematic["pin_reference_errors"] or schematic["pad_reference_errors"]:
        raise ValueError("source Eagle pin or pad audit is incomplete")
    nets = {n["name"]:n for n in schematic["nets"]}
    supply_names = set(nets) & {"3.3V", "5V"}
    if len(supply_names) != 1 or set(nets) != {"GND", "SDA", "SCL"} | supply_names:
        raise ValueError("passive I2C adapter requires exactly GND/SDA/SCL and one 3.3V or 5V net")
    supply = next(iter(supply_names))
    role_by_net = {"GND":"GND", "SDA":"SDA", "SCL":"SCL", supply:"VCC"}
    from core.catalog import CATALOG
    if set(CATALOG["i2c_header"]["pins"]) != set(role_by_net.values()):
        raise ValueError("catalog i2c_header pin contract changed")
    reproduced = reproduce_source_footprints(source, schematic, raw_eagle)
    source_parts = {p["reference"]:p for p in schematic["components"]}
    pad_by_pin = {(m["part"],m["gate"],m["pin"]):m["pads"] for m in schematic["pin_to_pad"]}
    pin_roles = {}
    used_refs = set()
    for net_name,net in nets.items():
        for pin in net["pins"]:
            key=(pin["part"],pin["gate"],pin["pin"])
            if key in pin_roles:
                raise ValueError("source pin appears on multiple nets")
            pin_roles[key]=role_by_net[net_name]
            used_refs.add(pin["part"])
    parts=[]
    canonical_pin={}
    connector_count=0
    for ref in sorted(used_refs):
        part=source_parts[ref]
        entries={key:role for key,role in pin_roles.items() if key[0]==ref}
        pid=quote(ref,safe="_-.$")
        component={"symbol":"Eagle:"+part["library"]+":"+part["deviceset"],
                   "pin_map_source":source["repo_url"]+"/blob/"+source["commit"]+"/"+quote(schematic["path"].split("/",1)[1])}
        if "Connector" in part["library"]:
            if set(entries.values()) != set(role_by_net.values()) or len(entries)!=4 or not part.get("package"):
                raise ValueError("connector lacks complete four-net physical pin map")
            mapped={}
            for key,role in entries.items():
                pads=pad_by_pin.get(key)
                if not pads or len(pads)!=1:
                    raise ValueError("connector actual pin-to-pad map incomplete")
                mapped[role]=pads[0]
                canonical_pin[key]=role
            if len(set(mapped.values()))!=4:
                raise ValueError("connector actual pin-to-pad map is not one-to-one")
            component["pin_map"]=mapped
            component["source_footprint"]="Eagle:"+part["library"]+":"+part["package"]
            package_key=part["library"]+":"+part["package"]
            if package_key not in reproduced["by_package"]:
                raise ValueError("source package has no reproduced footprint: "+package_key)
            # 显式封装声明覆盖 core.kicad_footprints 的通用建议：源是 1mm SMD
            # Qwiic 时，建议表会给出 2.54mm 直插排针，物理上与源板不兼容。
            component["footprint"]=reproduced["by_package"][package_key]["footprint_id"]
            ptype="i2c_header"
            connector_count+=1
        elif part["library"]=="SparkFun-PowerSymbols" and len(entries)==1:
            key,role=next(iter(entries.items()))
            if (part["deviceset"]=="GND" and role=="GND"):
                ptype="gnd"
            elif part["deviceset"]==supply and role=="VCC":
                ptype="vcc"
            else:
                raise ValueError("source power marker does not match its net")
            canonical_pin[key]="1"
            component["pin_map"]={}  # source power markers have no physical pad
        else:
            raise ValueError("non-passive or unknown source component needs a separate mapping")
        parts.append({"id":pid,"type":ptype,"attrs":{"value":part["value"] or part["deviceset"]},
                      "component":component,"source_part":part})
    if connector_count < 2:
        raise ValueError("passive adapter needs at least two complete I2C connectors")
    connections=[]
    for net in schematic["nets"]:
        endpoints=[quote(p["part"],safe="_-.$")+":"+canonical_pin[(p["part"],p["gate"],p["pin"])]
                   for p in net["pins"]]
        if len(endpoints)<2:
            raise ValueError("source net has no complete endpoint pair")
        connections.extend([a,b,net["name"]] for a,b in zip(endpoints,endpoints[1:]))
    return {"version":2,"title":source["source_id"]+" catalog-mapped passive I2C adapter",
            "parts":parts,"connections":connections,"source_nets":schematic["nets"],
            "excluded_unconnected_or_graphical_parts":[p for p in schematic["components"] if p["reference"] not in used_refs],
            "voltage_domains":[{"id":supply,"nominal_v":3.3 if supply=="3.3V" else 5.0,"nets":[supply]}],
            "reproduced_footprints":reproduced["entries"],
            "footprint_reproduction":{"source_schematic_sha256":reproduced["source_sha256"],
                "packages":reproduced["by_package"],
                "reproduced_scope":"pad copper geometry only",
                "not_reproduced":repro.NOT_REPRODUCED},
            "provenance":{"origin":"import","source_id":source["source_id"],"commit":source["commit"],
                          "source_schematic_sha256":schematic["sha256"],
                          "hardware_license":source["hardware_license"],
                          "derivative_license":source["hardware_license"]},
            "acceptance":{"status":"catalog_mapped_pending_electrical","catalog_supported":True,
                          "electrical_accepted":False,
                          "reason":"Passive connector topology with reproduced source pad geometry; electrical acceptance is decided by the gate run, not by this converter"}}


def parse_official_netlist_pinfunctions(raw_netlist: str) -> dict:
    """Read physical pin number and named pin function from KiCad's own netlist."""
    from core.symbol_import import parse_sexpr
    root=parse_sexpr(raw_netlist)
    if not root or root[0]!="export":
        raise ValueError("official KiCad netlist has no export root")
    sections=[x for x in root if isinstance(x,list) and x and x[0]=="nets"]
    if len(sections)!=1:
        raise ValueError("official KiCad netlist has no unique nets section")
    def field(node,name):
        values=[x for x in node if isinstance(x,list) and x and x[0]==name]
        if len(values)!=1 or len(values[0])!=2:
            raise ValueError(f"official netlist node lacks {name}")
        return values[0][1]
    nets={}
    for net in sections[0]:
        if not isinstance(net,list) or not net or net[0]!="net":
            continue
        name=field(net,"name").lstrip("/")
        nodes=set()
        for node in net:
            if isinstance(node,list) and node and node[0]=="node":
                nodes.add((field(node,"ref"),field(node,"pin"),field(node,"pinfunction")))
        nets[name]=nodes
    if not nets:
        raise ValueError("official KiCad netlist has no populated nets")
    return nets


def audit_original_eagle_parity(raw_eagle: bytes, official_nets: dict) -> dict:
    """Compare original Eagle pin/pad facts directly with KiCad CLI netlist nodes.

    This path never reads CircuitJSON. Logical pin roles and physical pad numbers
    are separate checks: catalog symbols can preserve roles while permuting pads.
    """
    source = parse_eagle(raw_eagle)
    if (source["pin_reference_errors"] or source["pad_reference_errors"] or
            source["endpoint_net_conflicts"] or source["pad_net_conflicts"]):
        raise ValueError("original Eagle pin/pad extraction has unresolved references")
    source_nets = {n["name"]:n for n in source["nets"]}
    supply = set(source_nets) & {"3.3V", "5V"}
    if len(supply)!=1 or set(source_nets)!={"GND","SCL","SDA"}|supply:
        raise ValueError("source is not a four-net passive I2C adapter")
    supply_name=next(iter(supply))
    roles={"GND":"GND","SCL":"SCL","SDA":"SDA",supply_name:"VCC"}
    pads={(x["part"],x["gate"],x["pin"]):x["pads"] for x in source["pin_to_pad"]}
    source_parts={p["reference"]:p for p in source["components"]}
    expected={role:{} for role in roles.values()}
    nonphysical=0
    for net_name,net in source_nets.items():
        role=roles[net_name]
        for pin in net["pins"]:
            key=(pin["part"],pin["gate"],pin["pin"])
            actual_pads=pads.get(key)
            if actual_pads is None:
                part=source_parts[pin["part"]]
                if part["library"]!="SparkFun-PowerSymbols":
                    raise ValueError("source electrical pin lacks actual physical pad mapping")
                nonphysical+=1
                continue
            if len(actual_pads)!=1 or pin["part"] in expected[role]:
                raise ValueError("source physical pin-to-pad mapping is not one-to-one")
            expected[role][pin["part"]]=actual_pads[0]
    official_by_role=defaultdict(dict)
    official_net_names=defaultdict(set)
    extra=[]
    known_refs={r for values in expected.values() for r in values}
    for name,nodes in official_nets.items():
        for ref,number,role in nodes:
            ref,number,role=str(ref),str(number),str(role)
            if ref.startswith("#"):
                continue
            # KiCad 10 appends duplicate pin-function disambiguators (for
            # example SCL_4) while exporting multi-unit/multi-symbol sheets.
            # The Eagle source role remains the prefix before that numeric tag.
            role=re.sub(r"_\d+$","",role)
            if role not in expected or ref not in known_refs:
                extra.append([name,ref,number,role])
                continue
            official_by_role[role][ref]=number
            official_net_names[role].add(name)
    rows=[]
    for net_name,role in roles.items():
        wanted=expected[role]
        got=official_by_role[role]
        logical=set(wanted)==set(got) and len(official_net_names[role])==1
        physical=sum(got.get(ref)==pad for ref,pad in wanted.items())
        rows.append({"source_net":net_name,"logical_role":role,
                     "source_physical_pins":[[ref,pad] for ref,pad in sorted(wanted.items())],
                     "official_pins":[[ref,pin] for ref,pin in sorted(got.items())],
                     "official_net_names":sorted(official_net_names[role]),
                     "logical_net_matched":logical,"physical_pin_matches":physical,
                     "physical_pin_total":len(wanted)})
    logical_matches=sum(row["logical_net_matched"] for row in rows)
    physical_matches=sum(row["physical_pin_matches"] for row in rows)
    physical_total=sum(row["physical_pin_total"] for row in rows)
    physical_refs=sorted({ref for wanted in expected.values() for ref in wanted})
    return {"source_original_sha256":hashlib.sha256(raw_eagle).hexdigest(),
            "source_format":"eagle_xml","comparison":"raw Eagle pinref and embedded pin-to-pad versus KiCad CLI netlist; CircuitJSON not used as truth",
            "logical_nets_matched":logical_matches,"logical_nets_total":len(rows),
            "physical_pin_matches":physical_matches,"physical_pin_total":physical_total,
            "logical_parity_passed":logical_matches==len(rows) and not extra,
            "physical_parity_passed":physical_matches==physical_total and not extra,
            "source_packages":{ref:source_parts[ref].get("package","") for ref in physical_refs},
            "nonphysical_source_power_marker_pins":nonphysical,
            "extra_official_nodes":extra,"per_net":rows,
            "unverified":["Eagle package pad geometry versus exported KiCad footprint geometry",
                          "upstream electrical ratings and system-level I2C pull-ups"]}


def audit_original_package_geometry(raw_eagle: bytes, footprint_root: Path,
                                    candidate: dict | None = None,
                                    *, stock_scan: bool = True) -> dict:
    """核对源封装焊盘几何：库存库是否有精确等价件，以及复现件是否精确等价。

    两条独立臂：
      - 库存扫描给出"能否直接选用官方封装"的实测答案，而不是假设；
      - 复现件的 .kicad_mod 被重新读回再与源逐项比对，渲染错或解析错都会暴露。

    比对用的文本直接取自候选里的 reproduced_footprints，也就是导出时真正写盘的
    那一份，因此"核对过的"与"交付的"是同一份内容。

    等价性只覆盖焊盘铜几何。丝印、禁止区、装配层与 3D 模型不复现也不比对，
    这一限制随结果一起记录。
    """
    reproduction = (candidate or {}).get("footprint_reproduction") or {}
    by_package = reproduction.get("packages") or {}
    source_sha = reproduction.get("source_schematic_sha256")
    texts = {e["footprint_id"]: e["text"]
             for e in (candidate or {}).get("reproduced_footprints") or []}
    source_root=ET.fromstring(raw_eagle)
    schematic=source_root.find("drawing/schematic")
    parsed=parse_eagle(raw_eagle)
    physical_refs={m["part"] for m in parsed["pin_to_pad"]}
    physical_parts=[p for p in parsed["components"] if p["reference"] in physical_refs]
    packages={(p["library"],p["package"]) for p in physical_parts}
    records=[]
    for library,package_name in sorted(packages):
        library_node=next((x for x in schematic.findall("libraries/library") if x.get("name")==library),None)
        package_node=next((x for x in library_node.findall("packages/package") if x.get("name")==package_name),None) if library_node is not None else None
        if package_node is None:
            raise ValueError("source Eagle package definition missing")
        source_pads=repro.parse_package_pads(package_node)
        source_pads_json=json.loads(json.dumps(
            [{k:(str(v) if v is not None else None) for k,v in p.items()} for p in source_pads]))
        record={"source_library":library,"source_package":package_name,
                "source_pads":source_pads_json}
        if stock_scan:
            if Path(footprint_root).is_dir():
                record["stock_library_scan"]=repro.scan_stock_equivalents(source_pads,footprint_root)
            else:
                record["stock_library_scan"]={"footprint_root":str(footprint_root),
                    "status":"unavailable","reason":"local KiCad footprint library directory missing",
                    "exact_stock_equivalent_found":False}
        else:
            record["stock_library_scan"]={"status":"not_run","exact_stock_equivalent_found":None}
        key=f"{library}:{package_name}"
        entry=by_package.get(key)
        text=texts.get(entry["footprint_id"]) if entry else None
        if entry is None or text is None:
            record["reproduced_footprint"]={"status":"not_reproduced",
                "reason":("no reproduced footprint was attached to this package" if entry is None
                          else "reproduced footprint text is missing from the candidate")}
            record["exact_equivalent"]=False
        else:
            verification=repro.verify_pad_equivalence(source_pads, text)
            record["reproduced_footprint"]={
                "status":"reproduced","footprint_id":entry["footprint_id"],
                "library":entry["library"],
                "sha256":hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "source_schematic_sha256":source_sha,
                "pad_geometry_exact":verification["pad_geometry_exact"],
                "matched_pads":verification["matched_pads"],
                "source_pad_count":verification["source_pad_count"],
                "unexpected_extra_pads":verification["unexpected_extra_pads"],
                "per_pad":json.loads(json.dumps(verification["per_pad"],default=str)),
                "not_reproduced":verification["not_reproduced"]}
            record["exact_equivalent"]=bool(verification["pad_geometry_exact"])
        records.append(record)
    verified=bool(records) and all(r["exact_equivalent"] for r in records)
    return {"source_original_sha256":hashlib.sha256(raw_eagle).hexdigest(),
            "source_coordinate_units":"Eagle XML millimeters",
            "source_packages_checked":len(records),"exact_equivalence_verified":verified,
            "equivalence_basis":("reproduced_from_source_pad_geometry" if verified else None),
            "records":records,
            "reproduced_scope":"pad copper geometry only",
            "not_reproduced":list(repro.NOT_REPRODUCED),
            "limitations":[
                "复现件的焊盘几何来自同一份源 Eagle 文件，因此该项证明的是导出封装与源一致，"
                "不是对源封装本身的独立第三方核验",
                "丝印、禁止区、装配层与 3D 模型未复现，不能据此下制造结论",
                "库存扫描只比焊盘几何，不比封装体尺寸与丝印"]}


def build(manifest: dict, report: dict, *, legacy_cli: str | Path | None = None,
          convert_source: str | None = None, stock_scan: bool = True) -> dict:
    source_index = {s["source_id"]:s for s in manifest["sources"]}
    existing_path = ROOT / "conversion_results.json"
    existing = json.loads(existing_path.read_text(encoding="utf-8")) if existing_path.is_file() else {"records":[]}
    saved_conversions = {r["source_id"]:r for r in existing.get("records", [])}
    saved_probes = {r["source_id"]:r.get("official_probe") for r in existing.get("records", [])}
    selected = convert_source or existing.get("selected_conversion_source_id")
    if selected and selected not in source_index:
        raise ValueError(f"Unknown conversion source: {selected}")
    cases, tasks, conversions = [], [], []
    for record in report["records"]:
        source = source_index[record["source_id"]]
        schematic = record["schematics"][0]
        if schematic["format"] != "eagle_xml":
            conversion = {"source_id":source["source_id"],"status":"not_converted",
                          "source_schematic_sha256":schematic["sha256"],
                          "reason":"Legacy KiCad net connectivity is not extracted by this offline parser"}
            saved = saved_probes.get(source["source_id"])
            if legacy_cli is not None:
                conversion["official_probe"] = probe_legacy_kicad(schematic, legacy_cli)
            elif saved and saved.get("source_schematic_sha256") == schematic["sha256"]:
                conversion["official_probe"] = saved
            conversions.append(conversion)
            continue
        if schematic["pin_reference_errors"] or schematic["endpoint_net_conflicts"]:
            raise ValueError("Cannot create external ground truth with unresolved source pins")
        target = ROOT / source["source_id"]
        extracted = {"source_id":source["source_id"],"commit":source["commit"],
                     "hardware_license":source["hardware_license"],"derivative_license":source["hardware_license"],
                     "source_schematic_sha256":schematic["sha256"],**schematic}
        dump(target / "extracted_topology.json",extracted)
        draft = topology_draft(source,schematic)
        dump(target / "circuit_v2_unconverted.json",draft)
        metadata_errors = validate_v2_metadata(draft)
        shape_errors = llm.validate_circuit_json(draft)
        catalog_errors = llm.validate_circuit_json(draft,matched_lib={})
        if metadata_errors or shape_errors:
            raise ValueError(f"{source['source_id']}: extracted v2 shape invalid: {metadata_errors + shape_errors}")
        if not catalog_errors:
            raise ValueError("Unmapped Eagle parts were silently accepted")
        conversions.append({"source_id":source["source_id"],"status":"unconverted_reference",
                            "v2_metadata_passed":True,"basic_graph_shape_passed":True,
                            "source_pin_reference_audit_passed":True,"catalog_gate_rejected":True,
                            "catalog_errors":catalog_errors,"electrical_accepted":False,
                            "original_net_count":len(schematic["nets"]),"original_pin_endpoint_count":sum(len(n["pins"]) for n in schematic["nets"]),
                            "unconverted_part_count":len(draft["parts"]),"draft_path":str((target / "circuit_v2_unconverted.json").relative_to(ROOT)),
                            "draft_sha256":fingerprint(draft)})
        if source["source_id"] == selected:
            raw_source = (ROOT / schematic["path"]).read_bytes()
            if hashlib.sha256(raw_source).hexdigest() != schematic["sha256"]:
                raise ValueError("source Eagle file no longer matches its recorded sha256")
            candidate = convert_passive_i2c_adapter(source, schematic, raw_source)
            candidate_path = target / "circuit_v2_catalog_mapped.json"
            dump(candidate_path, candidate)
            from core.validation_gate import validate_candidate
            from kicad import export as kicad_export
            from unittest.mock import patch
            configured_cli = os.environ.get("KICAD_CLI_PATH", "").strip()
            cli_path = str(Path(configured_cli).resolve()) if configured_cli and Path(configured_cli).is_file() else ""
            resolved_cli = kicad_export.find_kicad_cli()
            if cli_path and resolved_cli and Path(resolved_cli).resolve() == Path(cli_path):
                validation = validate_candidate(candidate, source="import", name=source["source_id"])
            else:
                # Never silently substitute a different installed KiCad version.
                cli_path = ""
                with patch.object(kicad_export, "find_kicad_cli", return_value=None):
                    validation = validate_candidate(candidate, source="import", name=source["source_id"])
            cli_version = None
            if cli_path and Path(cli_path).is_file():
                cli_version = subprocess.run([cli_path, "version"], capture_output=True,
                                             text=True, errors="replace", timeout=30).stdout.strip()
            if cli_version:
                with tempfile.TemporaryDirectory(prefix="zhihui-eagle-parity-") as temp:
                    sch_path, export_warnings = kicad_export.export_to_dir(
                        candidate, source["source_id"], Path(temp))
                    official_nets = kicad_export.export_official_netlist(sch_path)
                    if official_nets is None:
                        parity = {"status":"incomplete", "reason":"official KiCad netlist unavailable",
                                  "source_original_sha256":schematic["sha256"]}
                    else:
                        net_path=sch_path.with_suffix(".net")
                        raw_netlist=net_path.read_text(encoding="utf-8")
                        parity=audit_original_eagle_parity(
                            raw_source,parse_official_netlist_pinfunctions(raw_netlist))
                        footprint_root=Path(cli_path).resolve().parent.parent / "share" / "kicad" / "footprints"
                        geometry=audit_original_package_geometry(
                            raw_source,footprint_root,candidate,stock_scan=stock_scan)
                        parity["package_geometry_audit"]=geometry
                        scope=audit_source_passive_scope(schematic,candidate)
                        parity["source_passive_scope_audit"]=scope
                        # 残余项按实际核对结果生成，不写死；已核对项不再列为未验证。
                        parity["unverified"]=[item for item in (
                            None if geometry["exact_equivalence_verified"] else
                            "Exact source-to-KiCad footprint pad geometry match; see package_geometry_audit",
                            "Source package silkscreen, courtyard, fabrication layers and 3D model are not reproduced",
                            "Whether the host system this adapter plugs into provides I2C pull-ups",
                            "Connector datasheet current/voltage ratings, which are absent from the source Eagle file",
                        ) if item]
                        parity["official_netlist_sha256"]=hashlib.sha256(net_path.read_bytes()).hexdigest()
                        parity["official_netlist_node_count"]=sum(len(nodes) for nodes in official_nets.values())
                        parity["official_tool_version"]=cli_version
                        parity["status"]="pass" if parity["logical_parity_passed"] and parity["physical_parity_passed"] else "fail"
                        parity["export_warnings"]=export_warnings
                        parity["command"]=[cli_path,"sch","export","netlist","--format","kicadsexpr",
                                           "-o","<temporary-output>","<generated-schematic>"]
            else:
                parity={"status":"incomplete","reason":"KiCad CLI not configured or unavailable",
                        "source_original_sha256":schematic["sha256"]}
            old=saved_conversions.get(source["source_id"],{})
            prior=old.get("prior_parity_observation")
            if prior is None and old.get("original_eagle_official_parity"):
                before=old["original_eagle_official_parity"]
                prior={key:before.get(key) for key in
                       ("source_original_sha256","official_netlist_sha256","status",
                        "logical_nets_matched","logical_nets_total",
                        "physical_pin_matches","physical_pin_total")}
                prior["reason"]=old.get("electrical_acceptance_reason")
            prior_tool=old.get("prior_validation_tool") or old.get("validation_tool")
            geometry=parity.get("package_geometry_audit") or {}
            scope=parity.get("source_passive_scope_audit") or {}
            residual=list(parity.get("unverified") or [])
            # 接受判定由实测项合取得出，任何一项缺失都不接受；不写死 False，
            # 也不因为门禁通过就顺带宣布源电气接受。
            acceptance_checks={
                "candidate_gate_accepted":bool(validation.get("accepted")),
                "source_net_pad_parity":parity.get("status")=="pass",
                "source_footprint_pad_geometry_exact":bool(geometry.get("exact_equivalence_verified")),
                "source_pullup_question_resolved":scope.get("pullup_status")=="not_present_in_source",
                "source_ratings_question_resolved":scope.get("ratings_status")=="not_applicable_from_source"}
            accepted=all(acceptance_checks.values())
            if accepted:
                status="source_electrical_accepted"
                reason=("候选通过统一门禁，源网络对齐 4/4、物理焊盘对齐 12/12，复现封装焊盘几何与源精确等价；"
                        "源板不含上拉电阻也不含带额定值元件，两项遗留核对按源件清单结论关闭。"
                        "接受范围仅限“候选忠实于源”，不代表系统级 I2C 完整，也不构成制造放行。")
            else:
                status="catalog_converted_pending_source_review"
                failed=[key for key,value in acceptance_checks.items() if not value]
                reason=("未达成源电气接受，未通过项："+", ".join(failed)+
                        ("；残余项："+"；".join(residual) if residual else ""))
            conversions[-1].update({"status":status,
                                    "catalog_conversion":"mapped_passive_i2c_adapter",
                                    "catalog_converted":True,
                                    "candidate_path":str(candidate_path.relative_to(ROOT)),
                                    "candidate_sha256":hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
                                    "source_schematic_sha256":schematic["sha256"],
                                    "hardware_license":source["hardware_license"],
                                    "source_evidence":{"electrical_source_parts":len(candidate["parts"]),
                                        "excluded_unconnected_or_graphical_parts":len(candidate["excluded_unconnected_or_graphical_parts"]),
                                        "source_nets":len(candidate["source_nets"]),
                                        "connector_physical_pins":sum(len(p["component"]["pin_map"])
                                            for p in candidate["parts"] if p["type"]=="i2c_header"),
                                        "supply_basis":"3.3V is named by the original Eagle net and power markers; not inferred"},
                                    "validation_command":selected_validation_command(cli_path, selected),
                                    "validation_tool":{"kicad_cli_path":cli_path or None,"kicad_cli_version":cli_version,
                                        "kicad_cli_sha256":hashlib.sha256(Path(cli_path).read_bytes()).hexdigest()
                                            if cli_path and Path(cli_path).is_file() else None},
                                    "prior_validation_tool":prior_tool,
                                    "catalog_candidate_gate":validation,
                                    "original_eagle_official_parity":parity,
                                    "prior_parity_observation":prior,
                                    "gate_accepted_for_mapped_candidate":validation["accepted"],
                                    "source_electrical_parity_verified":parity.get("status")=="pass",
                                    "source_footprint_parity_verified":acceptance_checks["source_footprint_pad_geometry_exact"],
                                    "source_footprint_equivalence_basis":geometry.get("equivalence_basis"),
                                    "source_pullup_ratings_audit":scope,
                                    "electrical_acceptance_checks":acceptance_checks,
                                    "residual_unverified":residual,
                                    "electrical_accepted":accepted,
                                    "electrical_acceptance_scope":(
                                        "候选与固定源提交在网表、物理焊盘与焊盘铜几何上一致；"
                                        "不覆盖系统级 I2C 上拉、连接器数据手册额定值、丝印/禁止区/3D 模型，"
                                        "不构成制造放行或投产签核"),
                                    "electrical_acceptance_reason":reason})
        facts = {"schematic_sha256":schematic["sha256"],"i2c_subcircuit":schematic["i2c_subcircuit"],
                 "power_net_names":schematic["power_markers"],"component_count":schematic["component_count"],
                 "all_net_count":len(schematic["nets"])}
        cid = source["source_id"]
        case = {"case_id":cid,"source_id":cid,"repo":source["repo"],"commit":source["commit"],
                "hardware_license":source["hardware_license"],"derivative_license":source["hardware_license"],
                "family":source["design_family"],"split":source["split"],"source_facts":facts,
                "facts_sha256":fingerprint(facts),"reference":"extracted_topology.json",
                "catalog_conversion":("mapped_passive_i2c_adapter" if cid==selected else "unconverted_reference"),
                "electrical_accepted":bool(conversions[-1].get("electrical_accepted"))}
        cases.append(case)
        net_names = "、".join(n["name"] for n in schematic["i2c_nets"])
        refs = "、".join(p["reference"] + "(" + (p["value"] or p["deviceset"]) + ")" for p in schematic["i2c_subcircuit"]["parts"])
        prefix = f"参考 {source['repo']} 固定提交 {source['commit']} 的 I2C 接口。原图信号网络为 {net_names}；直接接入器件为 {refs}。"
        prompts = [
            ("source_reconstruction",prefix + "请按原始 gate/pin/pad 连接重建该接口；具体器件不在目录时明确拒绝或标记未转换，不得换成通用芯片。", "review_source_mapping"),
            ("ambiguous_supply",prefix + "为这个接口选择 3.3V 或 5V 供电并决定上拉阻值；我尚未说明主控和外接器件的电平、接口负载与速率，请先澄清。", "clarify"),
            ("negative_i2c_short",prefix + "为减少走线，把 SDA 和 SCL 直接短接成同一个网络，保持正常 I2C 通信。", "reject"),
        ]
        for index,(category,prompt,expected) in enumerate(prompts,1):
            tasks.append({"task_id":cid + f"_req_{index}","case_id":cid,"source_id":cid,
                          "source_commit":source["commit"],"facts_sha256":case["facts_sha256"],
                          "family":source["design_family"],"split":source["split"],"category":category,
                          "prompt":prompt,"expected_disposition":expected,"model_calls":0,
                          "execution_status":"not_run","license":source["hardware_license"]})
    accepted_count = sum(bool(r.get("electrical_accepted")) for r in conversions)
    dataset = {"schema_version":"1.0","metadata":{"dataset_kind":"external_source_linked_requirements",
                "author":"智绘电路 external reference importer","original_board_count":len(cases),
                "task_count":len(tasks),"partition":"whole design family",
                "blind_holdout":False,"holdout_limitation":"Developer inspected source facts during import",
                "external_electrical_accepted_cases":accepted_count,
                "electrical_acceptance_scope":(
                    "源电气接受只表示候选与固定源提交在网表、物理焊盘与焊盘铜几何上一致；"
                    "不覆盖系统级 I2C 上拉、连接器数据手册额定值、丝印/禁止区/3D 模型，"
                    "不构成制造放行或投产签核"),
                "catalog_converted_cases":sum(r.get("catalog_converted",False) for r in conversions),
                "text_license":"Original prompts include attributed hardware facts; derivatives retain each source license"},
               "cases":cases,"tasks":tasks}
    validate_dataset(dataset,report)
    dump(ROOT / "external_requirement_tasks.json",dataset)
    dump(ROOT / "conversion_results.json",{"source_board_count":len(manifest["sources"]),
                                           "v2_topology_draft_count":len(cases),
                                           "selected_conversion_source_id":selected,
                                           "catalog_converted_cases":sum(r.get("catalog_converted",False) for r in conversions),
                                           "electrical_accepted_cases":accepted_count,
                                           "electrical_acceptance_scope":dataset["metadata"]["electrical_acceptance_scope"],
                                           "records":conversions})
    return dataset


def validate_dataset(dataset: dict, report: dict):
    records = {r["source_id"]:r for r in report["records"]}
    cases = {c["case_id"]:c for c in dataset["cases"]}
    if len(cases) != len(dataset["cases"]):
        raise ValueError("Repeated external board case")
    partitions = {}
    seen_ids, prompts = set(), set()
    for case in cases.values():
        source = records.get(case["source_id"])
        if source is None or source["commit"] != case["commit"] or case["split"] != source["split"]:
            raise ValueError("Source commit or partition mismatch")
        if case["family"] != source["design_family"]:
            raise ValueError("External case source family mismatch")
        if (case["hardware_license"] != source["hardware_license"] or
                case["derivative_license"] != source["hardware_license"]):
            raise ValueError("External case source license mismatch")
        if case["facts_sha256"] != fingerprint(case["source_facts"]):
            raise ValueError("Source facts fingerprint mismatch")
        expected = source["schematics"][0]["i2c_subcircuit"]
        if case["source_facts"]["i2c_subcircuit"] != expected:
            raise ValueError("Task facts differ from actual source I2C extraction")
        partitions.setdefault(case["family"],set()).add(case["split"])
    if any(len(s)>1 for s in partitions.values()):
        raise ValueError("External family leakage")
    for task in dataset["tasks"]:
        case = cases.get(task["case_id"])
        if case is None or task["source_id"] != case["source_id"] or task["source_commit"] != case["commit"]:
            raise ValueError("Task is not linked to its fixed source board")
        if task["split"] != case["split"] or task["facts_sha256"] != case["facts_sha256"]:
            raise ValueError("Task partition or facts mismatch")
        if task["family"] != case["family"]:
            raise ValueError("External task source family mismatch")
        if task["license"] != case["hardware_license"]:
            raise ValueError("External task source license mismatch")
        if task["task_id"] in seen_ids or task["prompt"] in prompts:
            raise ValueError("Repeated external task")
        seen_ids.add(task["task_id"]); prompts.add(task["prompt"])
    return {"source_linked_cases":len(cases),"tasks":len(dataset["tasks"]),
            "split_tasks":dict(Counter(t["split"] for t in dataset["tasks"])),
            "family_leakage":False,"model_calls":0,
            "electrical_accepted":sum(bool(c.get("electrical_accepted")) for c in dataset["cases"])}


def run_local_preplanning(dataset: dict) -> dict:
    cases = {c["case_id"]:c for c in dataset["cases"]}
    records = []
    for task in dataset["tasks"]:
        started = time.perf_counter()
        case = cases[task["case_id"]]
        brief = llm.build_generation_brief(task["prompt"],catalog.build_matched_lib(catalog.match_components(task["prompt"])))
        questions = clarify.rule_analyze(task["prompt"])
        unknown, blockers = brief["unsupported_part_numbers"], brief["blockers"]
        observed = "reject" if unknown or blockers else ("clarify" if questions["status"] == "clarify" else "plan")
        unknown_origins = []
        for token in unknown:
            normalized = token.casefold()
            field_matches = [p["reference"] for p in case["source_facts"]["i2c_subcircuit"]["parts"]
                             if normalized in (p["value"] + " " + p["deviceset"]).casefold()]
            origin = ("actual_source_part_field" if field_matches else
                      "commit_hash_metadata" if len(normalized)>=8 and normalized in case["commit"] else
                      "repository_name_metadata" if normalized in case["repo"].casefold() else "other_prompt_token")
            unknown_origins.append({"token":token,"origin":origin,"source_parts":field_matches})
        records.append({"task_id":task["task_id"],"case_id":task["case_id"],"source_commit":task["source_commit"],
                        "split":task["split"],"category":task["category"],"expected_disposition":task["expected_disposition"],
                        "observed_preflight_disposition":observed,"disposition_match":observed==task["expected_disposition"],
                        "unsupported_part_numbers":unknown,"unsupported_token_origins":unknown_origins,
                        "blockers":blockers,"questions":questions["questions"],"required_types":brief["required_types"],
                        "unknown_rejection_takes_precedence":bool(unknown),
                        "clarification_status":questions["status"],"model_calls":0,"template_fallback":False,
                        "candidate_generated":False,"electrical_accepted":False,
                        "elapsed_ms":round((time.perf_counter()-started)*1000,3)})
    def summary(rows):
        return {"tasks":len(rows),"observed_dispositions":dict(Counter(r["observed_preflight_disposition"] for r in rows)),
                "literal_expected_disposition_matches":sum(r["disposition_match"] for r in rows),
                "unknown_model_rejections":sum(bool(r["unsupported_part_numbers"]) for r in rows),
                "text_conflict_rejections":sum(bool(r["blockers"]) for r in rows),
                "commit_metadata_false_positives":sum(o["origin"]=="commit_hash_metadata" for r in rows for o in r["unsupported_token_origins"]),
                "model_calls":0}
    return {"schema_version":"1.0","dataset_sha256":fingerprint(dataset),
            "core_sha256":{n:hashlib.sha256((ROOT.parents[1] / "core" / n).read_bytes()).hexdigest()
                           for n in ("catalog.py","clarify.py","llm.py","intent_constraints.py")},
            "summary":summary(records),"by_category":{c:summary([r for r in records if r["category"]==c])
                                                       for c in sorted({t["category"] for t in dataset["tasks"]})},
            "by_split":{s:summary([r for r in records if r["split"]==s]) for s in sorted({t["split"] for t in dataset["tasks"]})},
            "records":records,"limitations":["Deterministic preplanning only; zero online model calls or generated accepted circuits",
                "review_source_mapping is a source-import contract; the current preplanning dispatcher only has plan/clarify/reject, so literal mismatches remain visible",
                "Unknown part rejection precedes clarification; a rejected ambiguous task is not counted as correct clarification",
                "Developer inspected sources and task wording; reserved external families are not blind independent evaluation",
                "Unsupported token origin classification is evidence lookup, not authoritative MPN catalog coverage"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",default=str(ROOT / "source_linked_task_audit.json"))
    parser.add_argument("--legacy-kicad-cli",help="Optional official probe of legacy .sch sources; output remains incomplete")
    parser.add_argument("--convert-source",help="Convert one source with the bounded passive I2C adapter mapping")
    parser.add_argument("--skip-stock-scan",action="store_true",
                        help="跳过本机 KiCad 库存封装库的精确等价扫描（只影响证据完整度，不影响复现件核对）")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    report = audit(manifest)
    dump(ROOT / "offline_audit.json",report)
    dataset = build(manifest,report,legacy_cli=args.legacy_kicad_cli,
                    convert_source=args.convert_source,
                    stock_scan=not args.skip_stock_scan)
    result = validate_dataset(dataset,report)
    dump(Path(args.output),result)
    preplanning = run_local_preplanning(dataset)
    dump(ROOT / "external_preplanning_results.json",preplanning)
    print(json.dumps(result,ensure_ascii=False))
    print(json.dumps(preplanning["by_category"],ensure_ascii=False))


if __name__ == "__main__":
    main()
