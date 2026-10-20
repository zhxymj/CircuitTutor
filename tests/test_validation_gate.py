# -*- coding: utf-8 -*-
"""统一验收门禁的离线回归；不调用本机 EDA 可执行文件。"""

import copy
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.validation_gate import compare_official_netlist, validate_candidate
from core.catalog import CATALOG
from kicad import cli_runner
from kicad.sch_writer import TYPE_MAP
from kicad.symbols import build_symbol_defs


ROOT = Path(__file__).resolve().parents[1]


def _led():
    return json.loads((ROOT / "data" / "circuit_templates.json").read_text(
        encoding="utf-8"))["led_limit"]


def _official_led_nets():
    return {
        "N1": {("BAT1", "1"), ("R1", "1")},
        "N3": {("R1", "2"), ("LED1", "2")},
        "GND": {("BAT1", "2"), ("LED1", "1"), ("#FLG01", "1")},
    }


def test_strict_gate_reports_missing_cli_for_every_source():
    for source in ("template", "ai", "import", "image", "manual"):
        circuit = _led()
        circuit["version"] = 2
        circuit["provenance"] = {"source": source}
        before = copy.deepcopy(circuit)
        with patch("core.validation_gate.kicad_export.find_kicad_cli",
                   return_value=None):
            result = validate_candidate(circuit, source=source)
        assert circuit == before
        assert result["verdict"] == "incomplete"
        assert result["accepted"] is False
        assert result["gates"]["structure"]["status"] == "pass"
        assert result["gates"]["kicad_export"]["status"] == "pass"
        assert result["gates"]["official_erc"]["status"] == "unavailable"
        assert result["gates"]["official_netlist"]["status"] == "unavailable"
        assert result["artifacts"] == {}


def test_explicit_internal_mode_and_bad_structure():
    with patch("core.validation_gate.kicad_export.find_kicad_cli") as cli:
        good = validate_candidate(_led(), source="manual", require_official=False)
    cli.assert_not_called()
    assert good["accepted"] is True
    assert good["assurance"] == "internal"
    assert good["gates"]["official_erc"]["status"] == "not_run"

    malformed = {"parts": [{"id": [], "type": "resistor"}],
                 "connections": []}
    bad = validate_candidate(malformed, source="import")
    assert bad["verdict"] == "fail"
    assert bad["gates"]["structure"]["status"] == "fail"
    assert bad["gates"]["official_erc"]["status"] == "not_run"


def test_strict_default_requires_available_spice_for_modeled_circuit():
    official = {"total": 0, "by_type": {}, "violations": []}
    with patch("core.validation_gate.find_ngspice", return_value=None), \
         patch("core.validation_gate.kicad_export.find_kicad_cli", return_value="mock"), \
         patch("core.validation_gate.kicad_export.run_official_erc", return_value=official), \
         patch("core.validation_gate.kicad_export.export_official_netlist", return_value=_official_led_nets()):
        result = validate_candidate(_led(), source="ai")
    assert not result["accepted"]
    assert result["blocking_gates"] == ["spice_runtime"]
    assert result["gates"]["spice_runtime"]["status"] == "unavailable"


def test_official_netlist_requires_exact_physical_nodes():
    circuit = _led()
    assert compare_official_netlist(circuit, _official_led_nets()) == []
    wrong = _official_led_nets()
    wrong["N1"] = {("BAT1", "1")}
    differences = compare_official_netlist(circuit, wrong)
    assert len(differences) == 1
    assert differences[0]["net"] == "N1"
    assert ["R1", "1"] in differences[0]["expected"]


def test_qwiic_sourced_header_pins_and_default_header_remain_strict():
    circuit = json.loads((ROOT / "data/external_references/sparkfun_qwiic_adapter/circuit_v2_catalog_mapped.json")
                         .read_text(encoding="utf-8"))
    refs = ("J1", "J3", "J4")
    observed = {
        "GND": {(ref, "1") for ref in refs},
        "VCC": {(ref, "2") for ref in refs},
        "N2": {(ref, "4") for ref in refs},
        "N3": {(ref, "3") for ref in refs},
    }
    assert compare_official_netlist(circuit, observed) == []
    wrong = {name: set(nodes) for name, nodes in observed.items()}
    wrong["GND"].remove(("J1", "1"))
    wrong["GND"].add(("J1", "3"))
    differences = compare_official_netlist(circuit, wrong)
    assert any(d["net"] == "GND" and ["J1", "1"] in d["expected"] for d in differences)

    default = copy.deepcopy(circuit)
    for part in default["parts"]:
        if part["type"] == "i2c_header":
            part.pop("component")
    default_observed = {
        "GND": {(ref, "3") for ref in refs},
        "VCC": {(ref, "4") for ref in refs},
        "N2": {(ref, "1") for ref in refs},
        "N3": {(ref, "2") for ref in refs},
    }
    assert compare_official_netlist(default, default_observed) == []

    for malformed in (None, {"GND": "1", "VCC": "2", "SDA": "3", "SCL": "3"}):
        invalid = copy.deepcopy(circuit)
        component = next(p for p in invalid["parts"] if p["id"] == "J1")["component"]
        if malformed is None:
            component.pop("pin_map_source")
        else:
            component["pin_map"] = malformed
        try:
            compare_official_netlist(invalid, observed)
        except ValueError:
            pass
        else:
            raise AssertionError("不合法或缺来源的显式排针映射被当作官方网表通过")


def test_official_gate_pass_and_fail_are_distinct():
    official = {"total": 0, "by_type": {}, "violations": [], "report": "test.json"}
    with patch("core.validation_gate.kicad_export.find_kicad_cli",
               return_value="mock-kicad"), patch(
                   "core.validation_gate.kicad_export.run_official_erc",
                   return_value=official), patch(
                   "core.validation_gate.kicad_export.export_official_netlist",
                   return_value=_official_led_nets()):
        good = validate_candidate(_led(), source="ai", require_spice_runtime=False)
    assert good["accepted"] is True
    assert good["assurance"] == "official"

    wrong = _official_led_nets()
    wrong["N1"] = {("BAT1", "1")}
    with patch("core.validation_gate.kicad_export.find_kicad_cli",
               return_value="mock-kicad"), patch(
                   "core.validation_gate.kicad_export.run_official_erc",
                   return_value=official), patch(
                   "core.validation_gate.kicad_export.export_official_netlist",
                   return_value=wrong):
        bad = validate_candidate(_led(), source="ai", require_spice_runtime=False)
    assert bad["verdict"] == "fail"
    assert bad["gates"]["official_netlist"]["status"] == "fail"
    assert "official_netlist" in bad["blocking_gates"]


def test_erc_cli_parses_violations_even_with_nonzero_exit():
    with tempfile.TemporaryDirectory() as temp_dir:
        sch = Path(temp_dir) / "demo.kicad_sch"
        sch.write_text("(kicad_sch)", encoding="utf-8")
        report = sch.with_suffix(".erc.json")
        report.write_text('{"sheets": []}', encoding="utf-8")  # stale report

        def fake_run(*_args, **_kwargs):
            report.write_text(json.dumps({"sheets": [{"violations": [
                {"type": "pin_not_connected", "severity": "error",
                 "description": "R1 pin 2", "items": []}]}]}), encoding="utf-8")
            return SimpleNamespace(returncode=1, stdout="", stderr="ERC violations")

        with patch.object(cli_runner, "find_kicad_cli", return_value="mock-kicad"), \
             patch.object(cli_runner.subprocess, "run", side_effect=fake_run):
            result = cli_runner.run_official_erc(sch)
        assert result["total"] == 1
        assert result["returncode"] == 1
        assert result["violations"][0]["description"] == "R1 pin 2"


def test_new_i2c_mcu_pins_exist_in_map_and_embedded_symbols():
    defs = build_symbol_defs()
    for component, pins in (("atmega328", tuple(CATALOG["atmega328"]["pins"])),
                            ("esp32", ("GPIO21", "GPIO22"))):
        lib_id, pin_map, _style = TYPE_MAP[component]
        symbol = defs[lib_id]
        for pin in pins:
            pin_number = pin_map[pin]
            assert f'(name "{pin}"' in symbol
            assert f'(number "{pin_number}"' in symbol


def test_netlist_parser_supports_kicad10_whitespace_and_escaped_names():
    compact = '(export (version "E") (nets (net (code "1") (name "/SDA") (node (ref "U1") (pin "21")))))'
    multiline = compact.replace('(', '\n(')
    expected = {"SDA": {("U1", "21")}}
    assert cli_runner._parse_netlist_text(compact) == expected
    assert cli_runner._parse_netlist_text(multiline) == expected
    try:
        cli_runner._parse_netlist_text('(export (nets (net (name "SDA") (node (ref "U1")))))')
    except ValueError:
        pass
    else:
        raise AssertionError('不完整节点不得悄悄变为空网络')


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print(f"结果：{len(tests)}/{len(tests)} 通过")
