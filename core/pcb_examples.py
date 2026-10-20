# -*- coding: utf-8 -*-
"""Ten internal low-voltage I2C interface engineering examples.

Three passive adapter topologies and layout/supply variants. These are internal
fixtures, not external independent benchmark samples or validated final designs.
Routing is a small explicit copper plan, never an autorouter quality claim.
"""
from __future__ import annotations

import argparse
import copy
import json
import uuid
from pathlib import Path

from core.pcb_real import (build_pcb_candidate, _a, _children, _nets, _num, _one,
                           _uuid, dump_sexpr, parse_sexpr)
from core.pcb_manufacturing import run_manufacturing_pipeline
from core.validation_gate import validate_candidate


HEADER_SOURCE = "https://www.samtec.com/products/tsw-104-07-g-s"
RESISTOR_SOURCE = "https://www.yageogroup.com/component-documentation/download/specsheet/RC0805FR-074K7L"
LIBRARY_SOURCE = "https://gitlab.com/kicad/libraries/kicad-footprints"
DESIGN_SOURCE = "internal-engineering:i2c-adapter-pin-assignment-v1"


def examples() -> list[dict]:
    result = []
    variants = [(1, 3.3, 0, 0), (1, 5.0, 1, 2), (1, 3.3, 3, 1),
                (2, 3.3, 0, 0), (2, 5.0, 2, 2), (2, 3.3, 4, 1),
                (3, 3.3, 0, 0), (3, 5.0, 1, 2), (3, 3.3, 3, 1), (3, 5.0, 5, 3)]
    pin_map = {"GND": "1", "VCC": "2", "SDA": "3", "SCL": "4"}
    for index, (ports, volts, dx, dy) in enumerate(variants, 1):
        parts, connections = [], []
        for port in range(ports):
            ref = f"J{port + 1}"
            parts.append({"id": ref, "type": "i2c_header", "attrs": {"value": "I2C GND VCC SDA SCL"},
                          "component": {"manufacturer": "Samtec", "mpn": "TSW-104-07-G-S",
                                        "datasheet": HEADER_SOURCE,
                                        "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
                                        "pin_map": dict(pin_map), "pin_map_source": DESIGN_SOURCE},
                          "pcb": {"x_mm": 8 + dx + 20 * port, "y_mm": 8 + dy,
                                  "rotation_deg": 0, "side": "front"}})
            if port:
                connections.extend([[f"J1:{pin}", f"{ref}:{pin}"] for pin in pin_map])
        for ref, pin, x, y in [("R1", "SDA", 16 + dx, 13.08 + dy),
                                ("R2", "SCL", 21 + dx, 15.62 + dy)]:
            parts.append({"id": ref, "type": "resistor", "attrs": {"value": "4.7k"},
                          "component": {"manufacturer": "YAGEO", "mpn": "RC0805FR-074K7L",
                                        "datasheet": RESISTOR_SOURCE,
                                        "footprint": "Resistor_SMD:R_0805_2012Metric",
                                        "pin_map": {"1": "1", "2": "2"},
                                        "pin_map_source": RESISTOR_SOURCE + "#nonpolar-two-terminal"},
                          "pcb": {"x_mm": x, "y_mm": y, "rotation_deg": 0, "side": "front"}})
            connections.extend([["J1:VCC", f"{ref}:1"], [f"J1:{pin}", f"{ref}:2"]])
        # These markers document the externally supplied rails and are not PCB parts.
        parts.extend([{"id": "GND1", "type": "gnd", "attrs": {"value": "external common ground"}},
                      {"id": "VCC1", "type": "vcc", "attrs": {"value": f"{volts:g}V"}}])
        # Keep each logical net as a chain to honor the CircuitJSON degree contract.
        groups = [["GND1:1", *[f"J{i + 1}:GND" for i in range(ports)]],
                  ["VCC1:1", "J1:VCC", "R1:1", "R2:1", *[f"J{i + 1}:VCC" for i in range(1, ports)]],
                  ["J1:SDA", "R1:2", *[f"J{i + 1}:SDA" for i in range(1, ports)]],
                  ["J1:SCL", "R2:2", *[f"J{i + 1}:SCL" for i in range(1, ports)]]]
        connections = [[a, b] for group in groups for a, b in zip(group, group[1:])]
        circuit = {"version": 2, "title": f"Internal I2C {ports}-port adapter {volts:g}V #{index}",
                   "parts": parts, "connections": connections,
                   "board": {"width_mm": max(32, 18 + dx + 20 * (ports - 1)), "height_mm": 28 + dy,
                             "layers": 2, "min_clearance_mm": 0.2, "min_trace_width_mm": 0.2},
                   "voltage_domains": [{"id": "logic", "nominal_v": volts, "min_v": volts * 0.95, "max_v": volts * 1.05}],
                   "provenance": {"origin": "manual", "source_id": f"internal-pcb-{index:02d}"},
                   "engineering_example": {"classification": "internal_engineering_example",
                                           "topology": f"i2c_{ports}_port_pullup_adapter", "ports": ports,
                                           "independent_external_sample": False, "review_required": True,
                                           "sources": [HEADER_SOURCE, RESISTOR_SOURCE, LIBRARY_SOURCE],
                                           "pin_assignment": "Design choice: pad1=GND,pad2=VCC,pad3=SDA,pad4=SCL; not a universal cable standard",
                                           "assumptions": "External MCU/sensor use the same voltage; bus capacitance/speed and existing pullups must be reviewed"}}
        result.append(circuit)
    return result


def explicit_route_example(circuit: dict, candidate: dict) -> dict:
    """Add real copper segments/vias only for the listed internal adapter family."""
    if circuit.get("engineering_example", {}).get("classification") != "internal_engineering_example":
        raise ValueError("Explicit routing plan only supports internal engineering examples")
    board = parse_sexpr(candidate["board_text"])
    parts = {p["id"]: p for p in circuit["parts"]}
    endpoint_nets, _ = _nets(circuit, parts)
    header = parts["J1"]["pcb"]
    x0, y0 = header["x_mm"], header["y_mm"]
    ports = circuit["engineering_example"]["ports"]
    tracks, vias = [], []
    def path(pin, layer, points):
        number = endpoint_nets[tuple(pin.split(":"))]
        for start, end in zip(points, points[1:]):
            if start != end:
                tracks.append({"net": number, "layer": layer, "start": start, "end": end, "width": 0.25})
    if ports > 1:
        for offset, pin in [(0, "GND"), (2.54, "VCC"), (5.08, "SDA"), (7.62, "SCL")]:
            path("J1:" + pin, "B.Cu", [(x0 + 20 * i, y0 + offset) for i in range(ports)])
    for ref, pin, offset in [("R1", "SDA", 5.08), ("R2", "SCL", 7.62)]:
        p = parts[ref]["pcb"]
        # Official KiCad 8/10 R_0805 pads: +/-0.9125 mm, rotation zero.
        left, right = (p["x_mm"] - 0.9125, p["y_mm"]), (p["x_mm"] + 0.9125, p["y_mm"])
        if ref == "R1":
            path("J1:VCC", "F.Cu", [(x0, y0 + 2.54), (left[0], y0 + 2.54), left])
        else:
            path("J1:VCC", "F.Cu", [(x0, y0 + 2.54), (x0 + 4, y0 + 2.54), (x0 + 4, left[1]), left])
        if ports == 1:
            via = (right[0] + 1.6, right[1])
            path("J1:" + pin, "F.Cu", [right, via])
            path("J1:" + pin, "B.Cu", [via, (x0, y0 + offset)])
            vias.append({"net": endpoint_nets[("J1", pin)], "at": via})
        else:
            path("J1:" + pin, "F.Cu", [right, (x0 + 20, y0 + offset)])
    for index, t in enumerate(tracks):
        board.append([_a("segment"), [_a("start"), *map(lambda n: _a(_num(n)), t["start"])],
                      [_a("end"), *map(lambda n: _a(_num(n)), t["end"])], [_a("width"), _a(t["width"])],
                      [_a("layer"), _a(t["layer"], True)], [_a("net"), _a(t["net"])],
                      [_a("uuid"), _a(_uuid("track", index), True)]])
    for index, v in enumerate(vias):
        board.append([_a("via"), [_a("at"), *map(lambda n: _a(_num(n)), v["at"])],
                      [_a("size"), _a("0.6")], [_a("drill"), _a("0.3")],
                      [_a("layers"), _a("F.Cu", True), _a("B.Cu", True)], [_a("net"), _a(v["net"])],
                      [_a("uuid"), _a(_uuid("via", index), True)]])
    title = _one(board, "title_block")
    if title:
        for comment in _children(title, "comment"):
            if len(comment) >= 3:
                comment[2] = _a("EXPLICIT ROUTED CANDIDATE - OFFICIAL DRC REQUIRED", True)
    return {**candidate, "board_text": dump_sexpr(board) + "\n", "status": "routed_candidate_unchecked",
            "routing": "explicit_internal_example_plan", "track_count": len(tracks), "via_count": len(vias)}


def generate_examples(output_dir, footprint_roots, cli_path=None, target_version=20240108) -> dict:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    items = []
    for index, circuit in enumerate(examples(), 1):
        sample = out / f"internal_{index:02d}"
        sample.mkdir(exist_ok=True)
        (sample / "circuit.json").write_text(json.dumps(circuit, ensure_ascii=False, indent=2), encoding="utf-8")
        candidate = build_pcb_candidate(circuit, f"internal_{index:02d}", footprint_roots, target_version)
        (sample / "candidate.kicad_pcb").write_text(candidate["board_text"], encoding="utf-8")
        routed = explicit_route_example(circuit, candidate)
        (sample / "routed_candidate.kicad_pcb").write_text(routed["board_text"], encoding="utf-8")
        audit = {k: v for k, v in routed.items() if k != "board_text"}
        (sample / "pad_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        source_gate = None
        pipeline = None
        if cli_path:
            gate_dir = sample / "source_gates" / ("run_" + uuid.uuid4().hex)
            source_gate = validate_candidate(circuit, source="manual", name="candidate",
                                             require_official=True, work_dir=gate_dir)
            gate_dir.mkdir(parents=True, exist_ok=True)
            gate_path = gate_dir / "source_gate.json"
            gate_path.write_text(json.dumps(source_gate, ensure_ascii=False, indent=2), encoding="utf-8")
            source_gate["report_path"] = str(gate_path.resolve())
            pipeline = (run_manufacturing_pipeline(circuit, routed["board_text"], sample / "runs", cli_path,
                                                   footprint_roots) if source_gate["accepted"] else
                        {"status": "source_gate_failed", "failure": source_gate["blocking_gates"],
                         "manufacturing_release": False, "artifacts": []})
        items.append({"id": f"internal_{index:02d}", "classification": "internal_engineering_example",
                      "topology": circuit["engineering_example"]["topology"],
                      "pcb": str(sample / "candidate.kicad_pcb"), "pad_audit": str(sample / "pad_audit.json"),
                      "source_gate": source_gate, "official_pipeline": pipeline})
    negatives = generate_negative_cases(out, footprint_roots, cli_path, target_version) if cli_path else []
    manifest = {"classification": "internal_engineering_examples", "independent_external_samples": 0,
                "sample_count": len(items), "distinct_topologies": 3, "samples": items,
                "negative_cases": negatives,
                "sources": [HEADER_SOURCE, RESISTOR_SOURCE, LIBRARY_SOURCE]}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def generate_negative_cases(output_dir, footprint_roots, cli_path, target_version=20240108) -> list[dict]:
    circuit = examples()[0]
    candidate = build_pcb_candidate(circuit, "negative", footprint_roots, target_version)
    routed = explicit_route_example(circuit, candidate)
    shorted = parse_sexpr(routed["board_text"])
    net_numbers, _ = _nets(circuit, {p["id"]: p for p in circuit["parts"]})
    shorted.append([_a("segment"), [_a("start"), _a("8"), _a("8")],
                    [_a("end"), _a("8"), _a("10.54")], [_a("width"), _a("0.25")],
                    [_a("layer"), _a("F.Cu", True)], [_a("net"), _a(net_numbers[("J1", "VCC")])],
                    [_a("uuid"), _a(_uuid("negative", "short"), True)]])
    cases = []
    for name, board_text in [("unrouted", candidate["board_text"]), ("deliberate_short", dump_sexpr(shorted))]:
        manifest = run_manufacturing_pipeline(circuit, board_text, Path(output_dir) / ("negative_" + name),
                                              cli_path, footprint_roots)
        cases.append({"name": name, "expected": "drc_failed", "result": manifest})
        if manifest["status"] == "fabrication_candidate_for_review":
            raise RuntimeError(f"Negative PCB case unexpectedly passed: {name}")
    # Retain a reproducible version of the earlier missing-reference source failure.
    source_without_anchors = copy.deepcopy(circuit)
    source_without_anchors["parts"] = [p for p in source_without_anchors["parts"] if p["type"] not in ("vcc", "gnd")]
    source_without_anchors["connections"] = [pair for pair in source_without_anchors["connections"]
                                             if all(not pin.startswith(("GND1:", "VCC1:")) for pin in pair)]
    missing_dir = Path(output_dir) / "negative_source_missing_ground" / ("run_" + uuid.uuid4().hex)
    missing_dir.mkdir(parents=True)
    (missing_dir / "circuit.json").write_text(json.dumps(source_without_anchors, ensure_ascii=False, indent=2), encoding="utf-8")
    gate = validate_candidate(source_without_anchors, source="manual", name="candidate", require_official=True,
                              work_dir=missing_dir)
    (missing_dir / "source_gate.json").write_text(json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8")
    cases.append({"name": "source_missing_ground", "expected": "source_gate_failed", "result": gate,
                  "report_path": str((missing_dir / "source_gate.json").resolve())})
    if gate["accepted"]:
        raise RuntimeError("Source without ground reference unexpectedly accepted")
    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--footprints", required=True)
    parser.add_argument("--cli")
    parser.add_argument("--target-version", type=int, default=20240108)
    args = parser.parse_args()
    result = generate_examples(args.output, [args.footprints], args.cli, args.target_version)
    print(json.dumps({"sample_count": result["sample_count"], "distinct_topologies": 3}))


if __name__ == "__main__":
    main()
