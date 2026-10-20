# -*- coding: utf-8 -*-
"""Meaningful actual-pad source mapping and independent netlist contract checks."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.pcb_examples import examples
from core.pcb_real import PCBContractError, _children, _one, _value, parse_sexpr, build_pcb_candidate
from core.pcb_source_schematic import (build_real_schematic, expected_real_nets,
                                       parse_official_real_netlist, compare_real_netlist)

FIXTURES = Path(__file__).parent / "fixtures" / "pcb_real"


class ActualSourceTests(unittest.TestCase):
    def test_actual_header_pin_numbers_and_same_pcb_instance(self):
        circuit = examples()[0]
        kit = build_real_schematic(circuit)
        source = parse_sexpr(kit["schematic_text"])
        definitions = _children(_one(source, "lib_symbols"), "symbol")
        connector = next(s for s in definitions if _value(s[1]) == "ZhihuiReal:Part_J1")
        unit = next(s for s in _children(connector, "symbol") if _value(s[1]).endswith("_1_1"))
        pins = {_value(_one(p, "name")[1]): _value(_one(p, "number")[1]) for p in _children(unit, "pin")}
        self.assertEqual(pins, {"GND": "1", "VCC": "2", "SDA": "3", "SCL": "4"})
        pcb = parse_sexpr(build_pcb_candidate(circuit, footprint_roots=[FIXTURES])["board_text"])
        for fp in _children(pcb, "footprint"):
            ref = next(_value(p[2]) for p in _children(fp, "property") if _value(p[1]) == "Reference")
            self.assertEqual(_value(_one(fp, "path")[1]), "/" + kit["root_uuid"] + "/" + kit["symbol_uuids"][ref])
        refs = [_value(p[2]) for s in _children(source, "symbol") for p in _children(s, "property")
                if _value(p[1]) == "Reference"]
        self.assertIn("#GND1", refs)
        self.assertIn("#VCC1", refs)

    def test_multiline_official_netlist_and_virtual_nodes(self):
        text = '''(export (version "E") (nets
          (net (code "1") (name "Net-(J1-GND)")
            (node (ref "J1") (pin "1") (pintype "passive"))
            (node (ref "#GND1") (pin "1")))
          (net (name "Net-(J1-VCC)")
            (node (ref "J1") (pin "2"))
            (node (ref "R1") (pin "1"))
            (node (ref "R2") (pin "1")))
          (net (name "Net-(J1-SDA)") (node (ref "J1") (pin "3")) (node (ref "R1") (pin "2")))
          (net (name "Net-(J1-SCL)") (node (ref "J1") (pin "4")) (node (ref "R2") (pin "2")))
        ))'''
        circuit = examples()[0]
        observed = parse_official_real_netlist(text)
        self.assertEqual(observed, expected_real_nets(circuit))
        self.assertEqual(compare_real_netlist(circuit, observed), [])
        observed["Net-(J1-GND)"] = {("J1", "2")}
        self.assertEqual(compare_real_netlist(circuit, observed)[0]["net"], "Net-(J1-GND)")
        with self.assertRaises(PCBContractError):
            parse_official_real_netlist('(export (components))')

    def test_changed_pin_mapping_changes_source_identity(self):
        circuit = examples()[0]
        changed = copy.deepcopy(circuit)
        changed["parts"][0]["component"]["pin_map"].update(GND="2", VCC="1")
        self.assertNotEqual(build_real_schematic(circuit)["root_uuid"], build_real_schematic(changed)["root_uuid"])
        self.assertIn(("J1", "2"), expected_real_nets(changed)["Net-(J1-GND)"])

    def test_active_parts_require_verified_electrical_pin_types(self):
        circuit = examples()[0]
        circuit["parts"][1]["type"] = "led"
        circuit["parts"][1]["component"]["pin_map"] = {"+": "1", "-": "2"}
        circuit["connections"] = [[p.replace("R1:1", "R1:+").replace("R1:2", "R1:-") for p in pair]
                                   for pair in circuit["connections"]]
        with self.assertRaises(PCBContractError) as ctx:
            build_real_schematic(circuit)
        self.assertEqual(ctx.exception.code, "source_pin_types")
        with self.assertRaises(PCBContractError):
            build_real_schematic(examples()[0], "../escape")


if __name__ == "__main__":
    unittest.main()
