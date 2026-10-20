# -*- coding: utf-8 -*-
import json
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.pcb_examples import examples, explicit_route_example
from core.pcb_manufacturing import audit_board, parse_drc_report, run_manufacturing_pipeline
from core.pcb_real import PCBContractError, build_pcb_candidate
from core.pcb_real import parse_sexpr, dump_sexpr, _one, _a
from core.pcb_source_schematic import expected_real_nets

FIXTURES = Path(__file__).parent / "fixtures" / "pcb_real"


class ManufacturingTests(unittest.TestCase):
    def setUp(self):
        self.circuit = examples()[0]
        self.candidate = build_pcb_candidate(self.circuit, footprint_roots=[FIXTURES])
        self.routed = explicit_route_example(self.circuit, self.candidate)

    def pass_source_command(self, args):
        output = Path(args[args.index("-o") + 1])
        if args[:2] == ["sch", "erc"]:
            output.write_text(json.dumps({"kicad_version": "TEST_DOUBLE", "source": "candidate.kicad_sch",
                                           "sheets": [{"violations": []}]}), encoding="utf-8")
            return True
        if args[:3] == ["sch", "export", "netlist"]:
            groups = []
            for label, nodes in expected_real_nets(self.circuit).items():
                groups.append('(net (name ' + json.dumps(label) + ') ' + ' '.join(
                    f'(node (ref "{ref}") (pin "{pin}"))' for ref, pin in nodes) + ')')
            output.write_text('(export (nets ' + ' '.join(groups) + '))', encoding="utf-8")
            return True
        return False

    def test_internal_examples_are_not_external_benchmark(self):
        samples = examples()
        self.assertEqual(len(samples), 10)
        self.assertEqual(len({s["engineering_example"]["topology"] for s in samples}), 3)
        for sample in samples:
            self.assertFalse(sample["engineering_example"]["independent_external_sample"])
            self.assertTrue(all(p["component"]["mpn"] and p["component"]["datasheet"]
                                for p in sample["parts"] if p["type"] not in ("vcc", "gnd")))
            candidate = build_pcb_candidate(sample, footprint_roots=[FIXTURES])
            routed = explicit_route_example(sample, candidate)
            self.assertEqual(audit_board(sample, routed["board_text"], [FIXTURES])["status"], "pass")

    def test_active_http_negative_fixtures_preserve_source_and_target_distinct_faults(self):
        from tools.probe_manufacturing_api import active_negative_cases
        from core.pcb_real import _children, _value
        source = Path(__file__).resolve().parents[1] / "data/pcb_active/run_e34bf9d7ac6c45be85d4fb00b97e1fec"
        circuit = json.loads((source / "circuit.json").read_text(encoding="utf-8"))
        board_text = (source / "candidate.kicad_pcb").read_text(encoding="utf-8")
        cases = active_negative_cases(circuit, board_text)
        self.assertEqual(set(cases), {"unrouted", "deliberate_short", "missing_common_ground"})
        self.assertEqual(cases["unrouted"]["circuit"], circuit)
        self.assertEqual(cases["deliberate_short"]["circuit"], circuit)
        unrouted = parse_sexpr(cases["unrouted"]["board_text"])
        self.assertFalse(any(_children(unrouted, key) for key in ("segment", "via", "zone")))
        shorted = parse_sexpr(cases["deliberate_short"]["board_text"])
        original = parse_sexpr(board_text)
        self.assertEqual(len(_children(shorted, "segment")), len(_children(original, "segment")) + 1)
        self.assertNotIn("GND1", {p["id"] for p in cases["missing_common_ground"]["circuit"]["parts"]})
        self.assertTrue(all(not any(endpoint.startswith("GND1:") for endpoint in edge)
                            for edge in cases["missing_common_ground"]["circuit"]["connections"]))
        self.assertEqual(board_text, (source / "candidate.kicad_pcb").read_text(encoding="utf-8"))

    def test_real_geometry_and_mapping_audit(self):
        result = audit_board(self.circuit, self.routed["board_text"], [FIXTURES])
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["pads_audited"], 8)
        bad = self.routed["board_text"].replace('(size 1.025 1.4)', '(size 3 3)', 1)
        with self.assertRaises(PCBContractError) as error:
            audit_board(self.circuit, bad, [FIXTURES])
        self.assertEqual(error.exception.code, "board_pad_geometry")

    def test_kicad_saved_active_board_accepts_cleared_isolated_pad_nets_only(self):
        from core.pcb_active_example import active_circuit
        from core.pcb_real import _children, _value
        roots = [Path("D:/KICAD/share/kicad/footprints")]
        if not roots[0].is_dir():
            self.skipTest("installed KiCad footprints unavailable")
        circuit = active_circuit()
        candidate = build_pcb_candidate(circuit, footprint_roots=roots, target_version=20260206)
        board = parse_sexpr(candidate["board_text"])
        isolated = 0
        for fp in _children(board, "footprint"):
            for pad in _children(fp, "pad"):
                net = _one(pad, "net")
                if net and _value(net[-1]).startswith("unconnected-"):
                    pad.remove(net)
                    isolated += 1
        self.assertEqual(isolated, 17)
        self.assertEqual(audit_board(circuit, dump_sexpr(board), roots)["status"], "pass")
        connected = next(pad for fp in _children(board, "footprint") for pad in _children(fp, "pad")
                         if _one(pad, "net") and not _value(_one(pad, "net")[-1]).startswith("unconnected-"))
        connected.remove(_one(connected, "net"))
        with self.assertRaises(PCBContractError) as error:
            audit_board(circuit, dump_sexpr(board), roots)
        self.assertEqual(error.exception.code, "board_net_mismatch")

    def test_active_board_sda_track_clears_vcc_resistor_pad(self):
        from core.pcb_active_example import active_circuit, explicit_active_routes
        from core.pcb_real import _children, _value
        roots = [Path("D:/KICAD/share/kicad/footprints")]
        if not roots[0].is_dir():
            self.skipTest("installed KiCad footprints unavailable")

        def clearance(circuit):
            candidate = build_pcb_candidate(circuit, footprint_roots=roots, target_version=20260206)
            board = parse_sexpr(explicit_active_routes(circuit, candidate))
            resistor = next(fp for fp in _children(board, "footprint")
                            if any(_value(p[1]) == "Reference" and _value(p[2]) == "R1"
                                   for p in _children(fp, "property")))
            pad = next(p for p in _children(resistor, "pad") if _value(p[1]) == "1")
            center_x = float(_value(_one(resistor, "at")[1])) + float(_value(_one(pad, "at")[1]))
            center_y = float(_value(_one(resistor, "at")[2])) + float(_value(_one(pad, "at")[2]))
            half_height = float(_value(_one(pad, "size")[2])) / 2
            track = next(s for s in _children(board, "segment")
                         if _value(_one(s, "uuid")[1]) == "daef3008-ad3f-5814-8cb3-dc44ee2fe9c2")
            x1, y1 = (float(_value(v)) for v in _one(track, "start")[1:])
            x2, y2 = (float(_value(v)) for v in _one(track, "end")[1:])
            track_y = y1 + (center_x - x1) * (y2 - y1) / (x2 - x1)
            return abs(center_y - track_y) - half_height - float(_value(_one(track, "width")[1])) / 2

        circuit = active_circuit()
        self.assertGreaterEqual(clearance(circuit), circuit["board"]["min_clearance_mm"])
        old = copy.deepcopy(circuit)
        next(p for p in old["parts"] if p["id"] == "R1")["pcb"]["y_mm"] = 12
        self.assertLess(clearance(old), 0)

    def test_active_zone_fill_has_local_footprint_table(self):
        from core.pcb_active_example import generate_active_example
        roots = [Path("D:/KICAD/share/kicad/footprints")]
        if not roots[0].is_dir():
            self.skipTest("installed KiCad footprints unavailable")
        observed = []
        def fake_command(cli, args, timeout):
            board = Path(args[-1])
            table = board.parent / "fp-lib-table"
            observed.append(table.read_text(encoding="utf-8") if table.is_file() else None)
            return {"exit_code": None, "stderr": "test stop"}
        with tempfile.TemporaryDirectory() as temp, \
             patch("core.pcb_active_example.validate_candidate", return_value={"accepted": True}), \
             patch("core.pcb_active_example._command", side_effect=fake_command):
            result = generate_active_example(temp, roots, "not-run")
        self.assertEqual(result["status"], "zone_fill_unavailable")
        self.assertEqual(len(observed), 1)
        self.assertIn('Package_DIP', observed[0])
        self.assertIn('Resistor_SMD', observed[0])

    def test_extra_copper_layers_cannot_hide_as_power_or_mixed(self):
        for kind in ("power", "mixed"):
            board = parse_sexpr(self.routed["board_text"])
            _one(board, "layers").append([_a("1"), _a("In1.Cu", True), _a(kind)])
            with self.assertRaises(PCBContractError) as error:
                audit_board(self.circuit, dump_sexpr(board), [FIXTURES])
            self.assertEqual(error.exception.code, "board_layers")

    def test_missing_cli_retains_failure_and_no_exports(self):
        with tempfile.TemporaryDirectory() as temp:
            result = run_manufacturing_pipeline(self.circuit, self.routed["board_text"], temp,
                                               cli_path=str(Path(temp) / "missing-cli"), footprint_roots=[FIXTURES])
            self.assertEqual(result["failure"]["code"], "cli_unavailable")
            self.assertIsNone(result["drc"]["counts"])
            self.assertFalse((Path(result["run_dir"]) / "cam").exists())
            self.assertTrue(Path(result["manifest_path"]).is_file())

    def test_incomplete_drc_report_cannot_pass(self):
        with self.assertRaises(PCBContractError):
            parse_drc_report({"violations": []})
        report = {"violations": [], "unconnected_items": [{"description": "not connected"}],
                  "schematic_parity": [], "kicad_version": "10.0.6", "source": "candidate.kicad_pcb"}
        self.assertEqual(parse_drc_report(report)["status"], "fail")

    def test_warning_blocks_export_even_if_process_returns_zero(self):
        calls = []
        def fake(cli, args, timeout):
            calls.append(args)
            if self.pass_source_command(args):
                return {"argv": args, "exit_code": 0, "stdout": "", "stderr": ""}
            report = {"violations": [{"type": "clearance", "severity": "warning"}],
                      "unconnected_items": [], "schematic_parity": [],
                      "kicad_version": "TEST_DOUBLE", "source": "candidate.kicad_pcb"}
            Path(args[args.index("-o") + 1]).write_text(json.dumps(report), encoding="utf-8")
            return {"argv": args, "exit_code": 0, "stdout": "", "stderr": ""}
        with tempfile.TemporaryDirectory() as temp:
            cli = Path(temp) / "fake-cli"
            cli.write_text("test double", encoding="utf-8")
            with patch("core.pcb_manufacturing._command", side_effect=fake):
                result = run_manufacturing_pipeline(self.circuit, self.routed["board_text"], temp,
                                                   str(cli), [FIXTURES])
            self.assertEqual(result["status"], "drc_failed")
            self.assertEqual(len(calls), 3)
            self.assertIn("--schematic-parity", calls[-1])
            self.assertFalse((Path(result["run_dir"]) / "fabrication_candidate.zip").exists())

    def test_board_change_after_drc_blocks_export(self):
        def fake(cli, args, timeout):
            if self.pass_source_command(args):
                return {"argv": args, "exit_code": 0, "stdout": "", "stderr": ""}
            report = {"violations": [], "unconnected_items": [], "schematic_parity": [],
                      "kicad_version": "TEST_DOUBLE", "source": "candidate.kicad_pcb"}
            Path(args[args.index("-o") + 1]).write_text(json.dumps(report), encoding="utf-8")
            with Path(args[-1]).open("a", encoding="utf-8") as stream:
                stream.write("\n;changed after check")
            return {"argv": args, "exit_code": 0, "stdout": "", "stderr": ""}
        with tempfile.TemporaryDirectory() as temp:
            cli = Path(temp) / "fake-cli"
            cli.write_text("test double", encoding="utf-8")
            with patch("core.pcb_manufacturing._command", side_effect=fake):
                result = run_manufacturing_pipeline(self.circuit, self.routed["board_text"], temp,
                                                   str(cli), [FIXTURES])
            self.assertEqual(result["failure"]["code"], "board_changed")
            self.assertFalse((Path(result["run_dir"]) / "cam").exists())

    def test_direct_pipeline_rejects_missing_common_ground_semantics(self):
        circuit = copy.deepcopy(self.circuit)
        circuit["parts"] = [p for p in circuit["parts"] if p["type"] not in ("vcc", "gnd")]
        circuit["connections"] = [pair for pair in circuit["connections"]
                                   if all(not p.startswith(("GND1:", "VCC1:")) for p in pair)]
        candidate = build_pcb_candidate(circuit, footprint_roots=[FIXTURES])
        routed = explicit_route_example(circuit, candidate)
        with tempfile.TemporaryDirectory() as temp:
            with patch("core.pcb_manufacturing._command") as command:
                result = run_manufacturing_pipeline(circuit, routed["board_text"], temp,
                                                    str(Path(temp) / "missing-cli"), [FIXTURES])
            self.assertEqual(result["failure"]["code"], "semantic_validation_failed")
            self.assertEqual(result["semantic_validation"]["assurance"], "internal")
            self.assertIn("erc", result["semantic_validation"]["blocking_gates"])
            command.assert_not_called()
            self.assertFalse((Path(result["run_dir"]) / "cam").exists())

    def test_incomplete_source_erc_report_blocks_all_later_commands(self):
        def fake(cli, args, timeout):
            Path(args[args.index("-o") + 1]).write_text(json.dumps({"sheets": []}), encoding="utf-8")
            return {"argv": args, "exit_code": 0, "stdout": "", "stderr": ""}
        with tempfile.TemporaryDirectory() as temp:
            cli = Path(temp) / "fake-cli"
            cli.write_text("test double", encoding="utf-8")
            with patch("core.pcb_manufacturing._command", side_effect=fake) as command:
                result = run_manufacturing_pipeline(self.circuit, self.routed["board_text"], temp,
                                                    str(cli), [FIXTURES])
            self.assertEqual(result["failure"]["code"], "source_erc_unavailable")
            self.assertEqual(command.call_count, 1)
            self.assertFalse((Path(result["run_dir"]) / "cam").exists())

    def test_source_from_another_circuit_rejected(self):
        from core.pcb_source_schematic import build_real_schematic
        with tempfile.TemporaryDirectory() as temp:
            cli = Path(temp) / "fake-cli"
            cli.write_text("test double", encoding="utf-8")
            with patch("core.pcb_manufacturing._command") as command:
                result = run_manufacturing_pipeline(self.circuit, self.routed["board_text"], temp,
                                                    str(cli), [FIXTURES],
                                                    source_schematic=build_real_schematic(examples()[1]))
            self.assertEqual(result["failure"]["code"], "source_mismatch")
            command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
