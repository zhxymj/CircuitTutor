# -*- coding: utf-8 -*-
"""Real-footprint PCB candidate contract; runs with Python standard library."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.pcb_real import (  # noqa: E402
    PCBContractError, build_pcb_candidate, load_footprint, parse_sexpr,
)


FIXTURES = Path(__file__).parent / "fixtures" / "pcb_real"


def circuit():
    return {
        "version": 2,
        "board": {"width_mm": 40, "height_mm": 35, "layers": 2,
                  "min_clearance_mm": 0.2, "min_trace_width_mm": 0.2},
        "parts": [
            {"id": "J1", "type": "i2c_header", "attrs": {"value": "I2C"},
             "component": {"mpn": "test-header-1x04", "datasheet": "fixture://official-kicad-8/header",
                           "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
                           "pin_map_source": "tests/fixtures/pcb_real/README.md",
                           "pin_map": {"GND": "1", "VCC": "2", "SDA": "3", "SCL": "4"}},
             "pcb": {"x_mm": 10, "y_mm": 10, "rotation_deg": 0, "side": "front"}},
            {"id": "R1", "type": "resistor", "attrs": {"value": "10k"},
             "component": {"mpn": "test-resistor-0805", "datasheet": "fixture://official-kicad-8/resistor",
                           "footprint": "Resistor_SMD:R_0805_2012Metric",
                           "pin_map_source": "tests/fixtures/pcb_real/README.md",
                           "pin_map": {"1": "1", "2": "2"}},
             "pcb": {"x_mm": 25, "y_mm": 15, "rotation_deg": 90, "side": "front"}},
        ],
        "connections": [["J1:VCC", "R1:1"], ["J1:SDA", "R1:2"]],
    }


class PCBRealTests(unittest.TestCase):
    def assert_code(self, expected, candidate):
        with self.assertRaises(PCBContractError) as ctx:
            build_pcb_candidate(candidate, footprint_roots=[FIXTURES])
        self.assertEqual(ctx.exception.code, expected)

    def test_official_pad_geometry_and_net_audit(self):
        _, resistor_pads, _ = load_footprint("Resistor_SMD:R_0805_2012Metric", [FIXTURES])
        self.assertEqual(set(resistor_pads), {"1", "2"})
        self.assertEqual(resistor_pads["1"][0]["x"], -0.9125)
        _, header_pads, _ = load_footprint(
            "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical", [FIXTURES])
        self.assertEqual(set(header_pads), {"1", "2", "3", "4"})
        self.assertEqual(header_pads["4"][0]["y"], 7.62)

        result = build_pcb_candidate(circuit(), footprint_roots=[FIXTURES])
        self.assertEqual(result["status"], "unrouted_candidate")
        self.assertEqual(len(result["pad_audit"]), 6)
        self.assertEqual(result["routing"], "unavailable")
        self.assertEqual(result["drc"], "unavailable")
        self.assertEqual(result["manufacturing"], "unavailable")
        audit = {(p["ref"], p["pad"]): p for p in result["pad_audit"]}
        self.assertEqual(audit["J1", "2"]["net"], audit["R1", "1"]["net"])
        self.assertEqual(audit["J1", "3"]["net"], audit["R1", "2"]["net"])
        self.assertNotEqual(audit["J1", "2"]["net"], audit["J1", "3"]["net"])
        self.assertIsNone(audit["J1", "4"]["net"])
        parsed = parse_sexpr(result["board_text"])
        self.assertEqual(parsed[0].value, "kicad_pcb")
        board_footprints = [x for x in parsed[1:] if isinstance(x, list) and x and x[0].value == "footprint"]
        board_pads = [x for fp in board_footprints for x in fp[1:]
                      if isinstance(x, list) and x and x[0].value == "pad"]
        self.assertEqual(len(board_footprints), 2)
        self.assertEqual(len(board_pads), 6)
        pad_uuids = [next(x[1].value for x in pad if isinstance(x, list) and x and x[0].value == "uuid")
                     for pad in board_pads]
        self.assertEqual(len(set(pad_uuids)), 6)
        self.assertIn('pad "4" thru_hole', result["board_text"])
        self.assertNotIn("(segment", result["board_text"])

    def test_missing_library_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(PCBContractError) as ctx:
                build_pcb_candidate(circuit(), footprint_roots=[temp])
            self.assertEqual(ctx.exception.code, "footprint_unavailable")

    def test_unmapped_pin_rejected(self):
        candidate = circuit()
        del candidate["parts"][0]["component"]["pin_map"]["VCC"]
        self.assert_code("unmapped_pin", candidate)

    def test_wrong_pad_rejected(self):
        candidate = circuit()
        candidate["parts"][0]["component"]["pin_map"]["SDA"] = "99"
        self.assert_code("pin_map", candidate)

    def test_missing_evidence_rejected(self):
        candidate = circuit()
        del candidate["parts"][1]["component"]["pin_map_source"]
        self.assert_code("component_evidence", candidate)

    def test_board_boundary_rejected(self):
        candidate = circuit()
        candidate["parts"][0]["pcb"]["y_mm"] = 30
        self.assert_code("pad_outside_board", candidate)

    def test_version_and_placeholder_rejected(self):
        candidate = circuit()
        candidate["version"] = 1
        self.assert_code("schema_version", candidate)
        candidate = circuit()
        candidate["parts"][0]["component"]["footprint"] = "PinHeader_1x02_P2.54mm_Vertical"
        self.assert_code("footprint_id", candidate)

    def test_source_circuit_unchanged(self):
        candidate = circuit()
        before = copy.deepcopy(candidate)
        build_pcb_candidate(candidate, footprint_roots=[FIXTURES])
        self.assertEqual(candidate, before)

    def test_newer_and_ambiguous_library_rejected(self):
        original = (FIXTURES / "Resistor_SMD.pretty" / "R_0805_2012Metric.kicad_mod").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp:
            lib = Path(temp) / "Resistor_SMD.pretty"
            lib.mkdir()
            file = lib / "R_0805_2012Metric.kicad_mod"
            file.write_text(original.replace("20240108", "20260206"), encoding="utf-8")
            with self.assertRaises(PCBContractError) as ctx:
                build_pcb_candidate(circuit(), footprint_roots=[temp, FIXTURES])
            self.assertEqual(ctx.exception.code, "footprint_version")
            file.write_text(original.replace('(pad "2" smd', '(pad "1" smd'), encoding="utf-8")
            with self.assertRaises(PCBContractError) as ctx:
                build_pcb_candidate(circuit(), footprint_roots=[temp, FIXTURES])
            self.assertEqual(ctx.exception.code, "duplicate_pad")


if __name__ == "__main__":
    unittest.main()
