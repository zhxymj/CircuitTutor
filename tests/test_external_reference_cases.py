"""External-source integrity, partition isolation, topology and refusal checks."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evaluation.external_reference_audit import ROOT, audit, parse_eagle
from evaluation.external_reference_cases import fingerprint, validate_dataset
from core import llm


class ExternalReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        cls.report = audit(cls.manifest)
        cls.dataset = json.loads((ROOT / "external_requirement_tasks.json").read_text(encoding="utf-8"))

    def test_real_sources_have_complete_i2c_and_actual_package_pads(self):
        self.assertGreaterEqual(self.report["source_design_groups"],30)
        self.assertEqual(self.report["i2c_signal_extracted_designs"],30)
        self.assertEqual(self.report["physical_pad_reference_audits_passed"],30)
        self.assertFalse(self.report["T03_complete"])
        self.assertEqual(self.report["independently_validated_circuit_count"],0)

    def test_successful_cli_with_empty_netlist_is_not_electrical_acceptance(self):
        from evaluation.external_reference_cases import classify_legacy_probe
        probe={"netlist_exit_code":0,"erc_exit_code":0,
               "netlist_component_count":0,"netlist_node_count":0,
               "erc_json_violation_count":0}
        verdict=classify_legacy_probe(probe)
        self.assertEqual(verdict["status"],"incomplete")
        self.assertFalse(verdict["electrical_accepted"])

    def test_legacy_official_probe_is_preserved_per_board(self):
        conversions=json.loads((ROOT / "conversion_results.json").read_text(encoding="utf-8"))
        by_id={r["source_id"]:r for r in conversions["records"]}
        for source_id in ("olimex_esp32_devkit_lipo_rev_d",
                          "olimex_esp32_gateway_rev_i"):
            with self.subTest(source_id=source_id):
                source=next(s for s in self.manifest["sources"] if s["source_id"]==source_id)
                record=by_id[source_id]
                probe=record["official_probe"]
                self.assertEqual(probe["source_schematic_sha256"],
                                 next(f["sha256"] for f in source["files"] if f["role"]=="schematic"))
                self.assertEqual((probe["netlist_exit_code"],probe["erc_exit_code"]),(0,0))
                self.assertEqual((probe["netlist_component_count"],probe["netlist_node_count"]),(0,0))
                # KiCad's legacy ERC count varies slightly between runs; retain
                # the observed count instead of pretending it is a fixed metric.
                self.assertGreater(probe["erc_json_violation_count"],0)
                self.assertEqual(probe["erc_json_violation_count"],
                                 probe["erc_console_reported_count"])
                self.assertEqual(probe["netlist_semantic_status"],"invalid_or_empty")
                self.assertEqual(probe["status"],"incomplete")
                self.assertFalse(probe["electrical_accepted"])
                self.assertEqual(record["status"],"not_converted")

    def test_unmodified_source_hash_is_a_required_gate(self):
        m=deepcopy(self.manifest)
        m["sources"][0]["files"][0]["sha256"]="0"*64
        with self.assertRaisesRegex(ValueError,"hash mismatch"):
            audit(m)

    def test_same_source_schematic_cannot_count_as_two_independent_cases(self):
        m=deepcopy(self.manifest)
        duplicate=deepcopy(m["sources"][2]); duplicate["source_id"]="false_new_case"
        duplicate["repo"]="false/new_repository"
        m["sources"].append(duplicate)
        with self.assertRaisesRegex(ValueError,"identical original schematics"):
            audit(m)

    def test_related_family_cannot_cross_partition(self):
        m=deepcopy(self.manifest)
        row=next(s for s in m["sources"] if s["design_family"]=="inertial_sensor")
        row["split"]="external_holdout"
        with self.assertRaisesRegex(ValueError,"partition leakage"):
            audit(m)

    def test_source_linked_text_counts_and_entire_family_holdout(self):
        r=validate_dataset(self.dataset,self.report)
        self.assertEqual(r["source_linked_cases"],30)
        self.assertEqual(r["tasks"],90)
        self.assertGreaterEqual(r["split_tasks"]["external_holdout"],20)
        self.assertFalse(self.dataset["metadata"]["blind_holdout"])

    def test_case_fact_fingerprint_cannot_hide_changed_actual_pad(self):
        d=deepcopy(self.dataset)
        d["cases"][0]["source_facts"]["i2c_subcircuit"]["endpoints"][0]["pads"]=["999"]
        d["cases"][0]["facts_sha256"]=fingerprint(d["cases"][0]["source_facts"])
        with self.assertRaisesRegex(ValueError,"actual source I2C extraction"):
            validate_dataset(d,self.report)

    def test_case_family_cannot_be_relabelled_to_hide_holdout_leakage(self):
        d=deepcopy(self.dataset)
        holdout=next(c for c in d["cases"] if c["split"]=="external_holdout")
        holdout["family"]="invented_holdout_family"
        for task in d["tasks"]:
            if task["case_id"]==holdout["case_id"]:
                task["family"]="invented_holdout_family"
        with self.assertRaisesRegex(ValueError,"source family mismatch"):
            validate_dataset(d,self.report)

    def test_source_linked_license_cannot_be_relabelled(self):
        d=deepcopy(self.dataset)
        case=d["cases"][0]
        case["hardware_license"]="MIT"
        case["derivative_license"]="MIT"
        for task in d["tasks"]:
            if task["case_id"]==case["case_id"]:
                task["license"]="MIT"
        with self.assertRaisesRegex(ValueError,"source license mismatch"):
            validate_dataset(d,self.report)

    def test_tasks_cannot_silently_switch_source_commit(self):
        d=deepcopy(self.dataset); d["tasks"][0]["source_commit"]="0"*40
        with self.assertRaisesRegex(ValueError,"fixed source board"):
            validate_dataset(d,self.report)

    def test_same_library_name_with_different_urn_selects_original_library(self):
        record=next(r for r in self.report["records"] if "photodetector" in r["source_id"])
        s=record["schematics"][0]
        p=next(p for p in s["components"] if p["reference"]=="SUPPLY18")
        self.assertEqual(p["library_urn"],"")
        self.assertFalse(s["pin_reference_errors"])
        raw=(ROOT / s["path"]).read_bytes()
        changed=raw.replace(b'<part name="SUPPLY18" library="SparkFun-PowerSymbols"',
                            b'<part name="SUPPLY18" library="SparkFun-PowerSymbols" library_urn="urn:adsk.eagle:library:530"')
        broken=parse_eagle(changed)
        self.assertTrue(any(e.get("part")=="SUPPLY18" for e in broken["pin_reference_errors"]))

    def test_unknown_source_devices_remain_rejected(self):
        for case in self.dataset["cases"]:
            with self.subTest(case=case["case_id"]):
                draft=json.loads((ROOT / case["source_id"] / "circuit_v2_unconverted.json").read_text(encoding="utf-8"))
                self.assertFalse(draft["acceptance"]["electrical_accepted"])
                self.assertTrue(llm.validate_circuit_json(draft,matched_lib={}))
                self.assertTrue(all(p["type"]=="external_eagle_unmapped" for p in draft["parts"]))

    def test_passive_i2c_adapter_conversion_preserves_source_pin_and_pad_roles(self):
        from evaluation.external_reference_cases import convert_passive_i2c_adapter
        from core.catalog import CATALOG
        record=next(r for r in self.report["records"] if r["source_id"]=="sparkfun_qwiic_adapter")
        source=next(s for s in self.manifest["sources"] if s["source_id"]==record["source_id"])
        schematic=record["schematics"][0]
        raw_eagle=(ROOT / schematic["path"]).read_bytes()
        converted=convert_passive_i2c_adapter(source,schematic,raw_eagle)
        self.assertEqual(len(converted["parts"]),9)
        self.assertEqual(len(converted["excluded_unconnected_or_graphical_parts"]),8)
        self.assertEqual(len(converted["source_nets"]),4)
        self.assertEqual(len(converted["connections"]),14)
        connectors=[p for p in converted["parts"] if p["type"]=="i2c_header"]
        self.assertEqual(len(connectors),3)
        self.assertEqual(set(CATALOG["i2c_header"]["pins"]),{"GND","VCC","SDA","SCL"})
        self.assertEqual(sum(len(p["component"]["pin_map"]) for p in connectors),12)
        self.assertTrue(all(set(p["component"]["pin_map"].values())=={"1","2","3","4"}
                            for p in connectors))
        original_pads={(m["part"],m["gate"],m["pin"]):m["pads"][0]
                       for m in schematic["pin_to_pad"] if len(m["pads"])==1}
        by_ref={p["id"]:p for p in connectors}
        roles={"GND":"GND","SCL":"SCL","SDA":"SDA","3.3V":"VCC"}
        for net in schematic["nets"]:
            for pin in net["pins"]:
                if pin["part"] in by_ref:
                    self.assertEqual(by_ref[pin["part"]]["component"]["pin_map"][roles[net["name"]]],
                                     original_pads[(pin["part"],pin["gate"],pin["pin"])])
            expected={pin["part"]+":"+(roles[net["name"]] if pin["part"] in by_ref else "1")
                      for pin in net["pins"]}
            observed={endpoint for connection in converted["connections"]
                      if connection[2]==net["name"] for endpoint in connection[:2]}
            self.assertEqual(observed,expected)
        self.assertTrue(all(not p["component"].get("pin_map") for p in converted["parts"]
                            if p["type"] in {"vcc","gnd"}))
        self.assertEqual(converted["provenance"]["source_schematic_sha256"],schematic["sha256"])
        # 复现封装：源是 1mm SMD 时不能落回 2.54mm 直插排针建议
        packages={p["footprint_id"]:p for p in converted["footprint_reproduction"]["packages"].values()}
        self.assertEqual({fid.split(":",1)[1] for fid in packages},{"1X04","1X04_1MM_RA"})
        texts={e["footprint_id"]:e["text"] for e in converted["reproduced_footprints"]}
        self.assertEqual(set(texts),set(packages))
        for connector in connectors:
            self.assertIn(connector["component"]["footprint"],texts)
            self.assertTrue(connector["component"]["footprint"].startswith("EagleRepro_SparkFun-Connectors:"))
        self.assertTrue(packages)
        self.assertTrue(all(p["verification"]["pad_geometry_exact"] for p in packages.values()))
        self.assertFalse(converted["acceptance"]["electrical_accepted"])

    def test_passive_i2c_adapter_rejects_incomplete_actual_pad_map(self):
        from evaluation.external_reference_cases import convert_passive_i2c_adapter
        record=next(r for r in self.report["records"] if r["source_id"]=="sparkfun_qwiic_adapter")
        source=next(s for s in self.manifest["sources"] if s["source_id"]==record["source_id"])
        schematic=deepcopy(record["schematics"][0])
        raw_eagle=(ROOT / schematic["path"]).read_bytes()
        schematic["pin_to_pad"]=schematic["pin_to_pad"][:-1]
        with self.assertRaisesRegex(ValueError,"actual pin-to-pad"):
            convert_passive_i2c_adapter(source,schematic,raw_eagle)

    def test_source_electrical_acceptance_requires_every_check_and_stays_scoped(self):
        conversions=json.loads((ROOT / "conversion_results.json").read_text(encoding="utf-8"))
        self.assertEqual(conversions["catalog_converted_cases"],1)
        self.assertEqual(conversions["electrical_accepted_cases"],1)
        row=next(r for r in conversions["records"] if r["source_id"]=="sparkfun_qwiic_adapter")
        self.assertTrue(row["catalog_converted"])
        self.assertEqual(row["status"],"source_electrical_accepted")
        self.assertEqual(row["source_evidence"],{
            "electrical_source_parts":9,"excluded_unconnected_or_graphical_parts":8,
            "source_nets":4,"connector_physical_pins":12,
            "supply_basis":"3.3V is named by the original Eagle net and power markers; not inferred"})
        candidate_path=ROOT / row["candidate_path"]
        import hashlib
        self.assertEqual(hashlib.sha256(candidate_path.read_bytes()).hexdigest(),row["candidate_sha256"])
        parity=row["original_eagle_official_parity"]
        self.assertEqual((parity["logical_nets_matched"],parity["logical_nets_total"]),(4,4))
        self.assertEqual((parity["physical_pin_matches"],parity["physical_pin_total"]),(12,12))
        self.assertTrue(parity["physical_parity_passed"])
        self.assertEqual(row["prior_parity_observation"]["physical_pin_matches"],0)
        self.assertEqual(len(parity["per_net"]),4)
        # 接受判定必须是五项实测的合取，不能由门禁单独推出
        checks=row["electrical_acceptance_checks"]
        self.assertEqual(set(checks),{
            "candidate_gate_accepted","source_net_pad_parity",
            "source_footprint_pad_geometry_exact","source_pullup_question_resolved",
            "source_ratings_question_resolved"})
        self.assertEqual(row["electrical_accepted"],all(checks.values()))
        self.assertEqual(row["source_footprint_parity_verified"],
                         checks["source_footprint_pad_geometry_exact"])
        self.assertEqual(row["source_footprint_equivalence_basis"],
                         "reproduced_from_source_pad_geometry")
        self.assertIn("catalog_candidate_gate",row)
        # 接受范围必须显式写出，且残余项不能被清空成"全部已验证"
        self.assertIn("不构成制造放行",row["electrical_acceptance_scope"])
        self.assertTrue(row["residual_unverified"])
        self.assertIn("Source package silkscreen, courtyard, fabrication layers and 3D model are not reproduced",
                      row["residual_unverified"])
        geometry=parity["package_geometry_audit"]
        self.assertEqual(geometry["source_packages_checked"],2)
        self.assertTrue(geometry["exact_equivalence_verified"])
        self.assertTrue(all(p["exact_equivalent"] for p in geometry["records"]))
        source_by_name={p["source_package"]:p for p in geometry["records"]}
        self.assertEqual(len(source_by_name["1X04_1MM_RA"]["source_pads"]),6)
        self.assertEqual(len(source_by_name["1X04"]["source_pads"]),4)
        for record in geometry["records"]:
            reproduced=record["reproduced_footprint"]
            self.assertEqual(reproduced["status"],"reproduced")
            self.assertTrue(reproduced["pad_geometry_exact"])
            self.assertEqual(reproduced["matched_pads"],reproduced["source_pad_count"])
            self.assertEqual(reproduced["unexpected_extra_pads"],[])
            self.assertEqual(len(reproduced["per_pad"]),reproduced["source_pad_count"])
            self.assertTrue(all(p["matched"] and not p["mismatches"] for p in reproduced["per_pad"]))
            # 库存库扫描必须真的跑过：否则"无精确等价件"只是假设
            scan=record["stock_library_scan"]
            self.assertGreater(scan["files_scanned"],1000)
            self.assertGreaterEqual(len(scan["libraries_scanned"]),100)
            self.assertFalse(scan["exact_stock_equivalent_found"])
            self.assertEqual(scan["exact_geometry_matches"],[])
        scope=parity["source_passive_scope_audit"]
        self.assertEqual((scope["source_connector_parts"],scope["source_power_marker_parts"]),(3,6))
        self.assertEqual(scope["source_non_connector_non_marker_parts"],[])
        self.assertEqual(scope["candidate_pullup_resistors"],0)
        self.assertEqual(scope["pullup_status"],"not_present_in_source")
        self.assertEqual(scope["ratings_status"],"not_applicable_from_source")
        # ERC 的 I2C 缺上拉告警必须保留，不能为了通过而抑制
        erc_issues=row["catalog_candidate_gate"]["gates"]["erc"]["issues"]
        self.assertTrue(any(i["rule"]=="I2C缺上拉" for i in erc_issues))

    def test_selected_validation_command_uses_the_actual_cli_path(self):
        from evaluation.external_reference_cases import selected_validation_command
        cli=r"C:\Users\30789\Desktop\智绘电路工具链\kicad\bin\kicad-cli.exe"
        command=selected_validation_command(cli,"sparkfun_qwiic_adapter")
        self.assertIn("$env:KICAD_CLI_PATH='"+cli+"'",command)
        self.assertIn("--convert-source sparkfun_qwiic_adapter",command)
        self.assertNotIn(r"D:\KICAD",command)
        self.assertIn("O''Brien",selected_validation_command(r"C:\O'Brien\kicad-cli.exe","sparkfun_qwiic_adapter"))

    def test_selected_conversion_preserves_prior_tool_observation(self):
        conversions=json.loads((ROOT / "conversion_results.json").read_text(encoding="utf-8"))
        row=next(r for r in conversions["records"] if r["source_id"]=="sparkfun_qwiic_adapter")
        self.assertEqual(row["prior_validation_tool"]["kicad_cli_version"],"9.0.8")
        self.assertIn(r"D:\KICAD\bin\kicad-cli.exe",row["prior_validation_tool"]["kicad_cli_path"])

    def test_selected_conversion_records_official_cli_hash(self):
        conversions=json.loads((ROOT / "conversion_results.json").read_text(encoding="utf-8"))
        row=next(r for r in conversions["records"] if r["source_id"]=="sparkfun_qwiic_adapter")
        self.assertRegex(row["validation_tool"]["kicad_cli_sha256"],r"^[0-9a-f]{64}$")

    def test_original_eagle_to_official_parity_detects_physical_pin_mismatch(self):
        from evaluation.external_reference_cases import audit_original_eagle_parity
        source=next(s for s in self.manifest["sources"] if s["source_id"]=="sparkfun_qwiic_adapter")
        raw=(ROOT / next(f["path"] for f in source["files"] if f["role"]=="schematic")).read_bytes()
        observed={"GND":{(ref,"3","GND") for ref in ("J1","J3","J4")},
                  "N2":{(ref,"1","SCL") for ref in ("J1","J3","J4")},
                  "N3":{(ref,"2","SDA") for ref in ("J1","J3","J4")},
                  "VCC":{(ref,"4","VCC") for ref in ("J1","J3","J4")}}
        result=audit_original_eagle_parity(raw,observed)
        self.assertEqual((result["logical_nets_matched"],result["logical_nets_total"]),(4,4))
        self.assertEqual((result["physical_pin_matches"],result["physical_pin_total"]),(0,12))
        self.assertFalse(result["physical_parity_passed"])
        self.assertEqual(len(result["per_net"]),4)
        observed["N3"].remove(("J4","2","SDA"))
        broken=audit_original_eagle_parity(raw,observed)
        self.assertLess(broken["logical_nets_matched"],broken["logical_nets_total"])

    def test_original_eagle_parity_accepts_official_pinfunction_suffixes(self):
        from evaluation.external_reference_cases import audit_original_eagle_parity
        source=next(s for s in self.manifest["sources"] if s["source_id"]=="sparkfun_qwiic_adapter")
        raw=(ROOT / next(f["path"] for f in source["files"] if f["role"]=="schematic")).read_bytes()
        observed={"GND":{(ref,"1","GND_1") for ref in ("J1","J3","J4")},
                  "N2":{(ref,"4","SCL_4") for ref in ("J1","J3","J4")},
                  "N3":{(ref,"3","SDA_3") for ref in ("J1","J3","J4")},
                  "VCC":{(ref,"2","VCC_2") for ref in ("J1","J3","J4")}}
        result=audit_original_eagle_parity(raw,observed)
        self.assertEqual((result["logical_nets_matched"],result["logical_nets_total"]),(4,4))
        self.assertEqual((result["physical_pin_matches"],result["physical_pin_total"]),(12,12))


if __name__=="__main__":
    unittest.main()
