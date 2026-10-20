"""Offline file integrity and source structure audit; never an electrical ERC.

Run: python -m evaluation.external_reference_audit --output PATH
Supports full legacy KiCad schematics and native Eagle schematic XML.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1] / "data" / "external_references"


def parse_legacy_kicad(text: str) -> dict:
    if not text.startswith("EESchema Schematic File Version") or not text.rstrip().endswith("$EndSCHEMATC"):
        raise ValueError("incomplete legacy KiCad schematic")
    components = []
    for block in re.finditer(r"\$Comp\n(.*?)\$EndComp", text, re.S):
        fields = dict(re.findall(r'^F ([012]) "([^"]*)"', block.group(1), re.M))
        reference = fields.get("0", "")
        components.append({"reference":reference, "value":fields.get("1", ""),
                           "footprint":fields.get("2", ""),
                           "source_line":text[:block.start()].count("\n") + 1})
    labels = sorted(set(re.findall(r'^(?:Text (?:GLabel|Label|HLabel) .*\n)([^\n]+)', text, re.M)))
    power = sorted({c["value"] for c in components if c["reference"].startswith("#PWR")})
    return {"format":"legacy_kicad", "title":next(iter(re.findall(r'^Title "(.*)"',text,re.M)), ""),
            "component_count":len([c for c in components if not c["reference"].startswith("#")]),
            "components":components, "net_labels":labels, "power_markers":power,
            "child_sheet_count":len(re.findall(r'^\$Sheet$',text,re.M)),
            "i2c_nets":[], "connectivity_extracted":False}


def parse_eagle(text: str | bytes) -> dict:
    root = ET.fromstring(text)
    schematic = root.find("drawing/schematic")
    if root.tag != "eagle" or schematic is None:
        raise ValueError("not an Eagle schematic XML document")
    components = [{"reference":p.get("name"), "value":p.get("value", ""),
                   "library":p.get("library"), "deviceset":p.get("deviceset"),
                   "library_urn":p.get("library_urn", ""),
                   "device":p.get("device")} for p in schematic.findall("parts/part")]
    nets_by_name = {}
    for net in schematic.findall("sheets/sheet/nets/net"):
        pins = [{"part":p.get("part"), "gate":p.get("gate"), "pin":p.get("pin")}
                for p in net.findall("segment/pinref")]
        named = nets_by_name.setdefault(net.get("name", ""), {"name":net.get("name", ""), "pins":[]})
        for pin in pins:
            if pin not in named["pins"]:
                named["pins"].append(pin)
    nets = list(nets_by_name.values())
    # Retain actual references/pin names; do not remap to conceptual catalog pins.
    i2c_nets = [n for n in nets if re.search(r"SDA|SCL|I2C", n["name"], re.I) or
                any(re.search(r"SDA|SCL",p["pin"] or "",re.I) for p in n["pins"])]
    power = sorted({n["name"] for n in nets if re.search(r"GND|VCC|VDD|3[._]?3V|3V3|5V|VIN|VUSB",n["name"],re.I)})
    pin_to_pad = []
    symbol_pins = {}
    mapping_errors = []
    pad_errors = []
    libraries = {(lib.get("name"),lib.get("urn", "")):lib for lib in schematic.findall("libraries/library")}
    for part in components:
        lib = libraries.get((part["library"],part["library_urn"]))
        if lib is None:
            mapping_errors.append({"part":part["reference"],"reason":"missing embedded library"})
            continue
        for deviceset in lib.findall("devicesets/deviceset"):
            if deviceset.get("name") != part["deviceset"]:
                continue
            for gate in deviceset.findall("gates/gate"):
                symbol = next((s for s in lib.findall("symbols/symbol") if s.get("name") == gate.get("symbol")), None)
                symbol_pins[(part["reference"],gate.get("name"))] = ({p.get("name") for p in symbol.findall("pin")} if symbol is not None else set())
            for device in deviceset.findall("devices/device"):
                if device.get("name", "") == part["device"]:
                    part["package"] = device.get("package", "")
                    package = next((p for p in lib.findall("packages/package") if p.get("name") == part["package"]), None)
                    package_pads = ({p.get("name") for tag in ("pad","smd") for p in package.findall(tag)} if package is not None else set())
                    for connect in device.findall("connects/connect"):
                        for pad in connect.get("pad", "").split():
                            if pad not in package_pads:
                                pad_errors.append({"part":part["reference"],"gate":connect.get("gate"),"pin":connect.get("pin"),
                                                   "pad":pad,"package":part["package"],"reason":"pad missing from actual embedded package"})
                    pin_to_pad.extend({"part":part["reference"], "gate":c.get("gate"),
                                       "pin":c.get("pin"), "pads":c.get("pad", "").split()}
                                      for c in device.findall("connects/connect"))
    part_refs = {p["reference"] for p in components}
    endpoint_nets = defaultdict(list)
    for net in nets:
        for pin in net["pins"]:
            endpoint = (pin["part"],pin["gate"],pin["pin"])
            endpoint_nets[endpoint].append(net["name"])
            if pin["part"] not in part_refs or pin["pin"] not in symbol_pins.get(endpoint[:2],set()):
                mapping_errors.append({"net":net["name"],**pin,"reason":"pin reference missing from actual embedded symbol/gate"})
    conflicting_endpoints = [{"part":key[0],"gate":key[1],"pin":key[2],"nets":names}
                             for key,names in endpoint_nets.items() if len(set(names)) > 1]
    pad_lookup = {(m["part"],m["gate"],m["pin"]):m["pads"] for m in pin_to_pad}
    pad_nets = defaultdict(set)
    for key,names in endpoint_nets.items():
        for pad in pad_lookup.get(key,[]):
            pad_nets[(key[0],pad)].update(names)
    pad_net_conflicts = [{"part":key[0],"pad":key[1],"nets":sorted(names)} for key,names in pad_nets.items() if len(names)>1]
    i2c_endpoints = [{"net":n["name"],**p,"pads":pad_lookup.get((p["part"],p["gate"],p["pin"]),[])}
                     for n in i2c_nets for p in n["pins"]]
    i2c_parts = sorted({p["part"] for p in i2c_endpoints})
    i2c_connected_parts = [p for p in components if p["reference"] in i2c_parts]
    return {"format":"eagle_xml", "component_count":len(components), "components":components,
            "net_labels":sorted({n["name"] for n in nets}), "power_markers":power,
            "child_sheet_count":0, "sheet_count":len(schematic.findall("sheets/sheet")),
            "i2c_nets":i2c_nets, "nets":nets, "pin_to_pad":pin_to_pad,
            "i2c_subcircuit":{"parts":i2c_connected_parts,"nets":i2c_nets,"endpoints":i2c_endpoints,
                              "scope":"Directly connected upstream I2C signal endpoints; all power/support nets remain in full extraction"},
            "pin_reference_errors":mapping_errors,"endpoint_net_conflicts":conflicting_endpoints,
            "pad_reference_errors":pad_errors,"pad_net_conflicts":pad_net_conflicts,
            "hierarchical_module_count":len(schematic.findall("modules/module")),
            "connectivity_extracted":True}


def audit(manifest: dict) -> dict:
    records = []
    source_ids = set()
    designs = set()
    family_partitions = defaultdict(set)
    schematic_hashes = set()
    for source in manifest["sources"]:
        sid = source["source_id"]
        if sid in source_ids:
            raise ValueError("duplicate source id")
        source_ids.add(sid)
        # A project revision or alternative wording is not a new source design.
        if source["repo"] in designs:
            raise ValueError("multiple revisions of one repository cannot count as independent source boards")
        designs.add(source["repo"])
        family_partitions[source["design_family"]].add(source["split"])
        if not source["license_verified"] or not re.fullmatch(r"[0-9a-f]{40}",source["commit"]):
            raise ValueError(f"{sid}: unverified license or unpinned source")
        sch = []
        for item in source["files"]:
            path = (ROOT / item["path"]).resolve()
            if not path.is_relative_to(ROOT.resolve()):
                raise ValueError("source file escapes reference directory")
            raw = path.read_bytes()
            if len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError(f"{sid}: source hash mismatch for {item['path']}")
            if item["role"] == "schematic":
                parsed = parse_legacy_kicad(raw.decode("utf-8")) if raw.startswith(b"EESchema") else parse_eagle(raw)
                if parsed["child_sheet_count"]:
                    raise ValueError(f"{sid}: child sheets require explicit complete-source inclusion")
                if parsed.get("hierarchical_module_count",0):
                    raise ValueError(f"{sid}: unexpanded Eagle modules cannot count as full topology extraction")
                if item["sha256"] in schematic_hashes:
                    raise ValueError("identical original schematics cannot count as distinct source cases")
                schematic_hashes.add(item["sha256"])
                sch.append({"path":item["path"], "sha256":item["sha256"], **parsed})
        if not sch or not any(f["role"] == "license" for f in source["files"]):
            raise ValueError(f"{sid}: schematic/license document missing")
        records.append({"source_id":sid, "repo":source["repo"], "commit":source["commit"],
                        "hardware_license":source["hardware_license"], "file_integrity_passed":True,
                        "design_family":source["design_family"], "split":source["split"],"schematics":sch,
                        "tool_readback":None, "erc_passed":None, "catalog_conversion":None})
    leaking_families = {family:sorted(splits) for family,splits in family_partitions.items()
                       if "external_holdout" in splits and len(splits)>1}
    if leaking_families:
        raise ValueError(f"family partition leakage: {leaking_families}")
    return {"schema_version":"1.1", "source_design_groups":len(records),
            "integrity_verified_designs":len(records), "independently_validated_circuit_count":0,
            "connectivity_extracted_designs":sum(any(s["connectivity_extracted"] for s in r["schematics"]) for r in records),
            "i2c_signal_extracted_designs":sum(any(s["i2c_nets"] for s in r["schematics"]) for r in records),
            "pin_reference_audits_passed":sum(all(not s.get("pin_reference_errors",[]) and not s.get("endpoint_net_conflicts",[])
                                                  for s in r["schematics"]) and any(s["connectivity_extracted"] for s in r["schematics"]) for r in records),
            "physical_pad_reference_audits_passed":sum(all(not s.get("pad_reference_errors",[]) and not s.get("pad_net_conflicts",[])
                                                           for s in r["schematics"]) and any(s["connectivity_extracted"] for s in r["schematics"]) for r in records),
            "split_counts":dict(Counter(r["split"] for r in records)),"family_split_leakage":leaking_families,
            "T03_complete":False, "records":records,
            "limitations":["Only original source files and syntax/hash are audited; no physical or ERC validation",
                           "Named nets and pin-to-pad values are upstream design facts, not verified electrical requirements",
                           "Related designs and revisions must share a family and cannot form independent holdout cases",
                           "Original source availability alone does not finish the T03 conversion and evaluation gate"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(ROOT / "manifest.json"))
    parser.add_argument("--output")
    args = parser.parse_args()
    report = audit(json.loads(Path(args.manifest).read_text(encoding="utf-8")))
    if args.output:
        Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({key:report[key] for key in ("source_design_groups", "integrity_verified_designs",
                                                "independently_validated_circuit_count", "T03_complete")},ensure_ascii=False))


if __name__ == "__main__":
    main()
