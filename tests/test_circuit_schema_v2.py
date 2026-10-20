# -*- coding: utf-8 -*-
"""CircuitJSON v2 合同与现有模板的无损迁移。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.circuit_schema import (  # noqa: E402
    CIRCUIT_JSON_SCHEMA,
    get_circuit_json_schema,
    migrate_v1_to_v2,
    validate_v2_metadata,
)


class CircuitJSONV2Tests(unittest.TestCase):
    def test_v1_contract_stays_the_default(self) -> None:
        original = get_circuit_json_schema()
        self.assertEqual(original, CIRCUIT_JSON_SCHEMA)
        self.assertEqual(original["properties"]["version"]["const"], 1)
        original["properties"]["version"]["const"] = 99
        self.assertEqual(get_circuit_json_schema()["properties"]["version"]["const"], 1)

    def test_v2_contract_exposes_engineering_fields(self) -> None:
        schema = get_circuit_json_schema(2)
        self.assertEqual(schema["properties"]["version"]["const"], 2)
        self.assertIn("version", schema["required"])
        self.assertIn("voltage_domains", schema["properties"])
        self.assertIn("board", schema["properties"])
        self.assertIn("provenance", schema["properties"])
        component = schema["$defs"]["part"]["properties"]["component"]
        self.assertIn("pin_map", component["properties"])
        self.assertEqual(schema["properties"]["board"]["properties"]["layers"]["enum"], [2])

    def test_migration_preserves_all_existing_templates_without_mutation(self) -> None:
        templates = json.loads((ROOT / "data" / "circuit_templates.json").read_text(encoding="utf-8"))
        self.assertEqual(len(templates), 40)
        for name, source in templates.items():
            with self.subTest(name=name):
                before = json.loads(json.dumps(source, ensure_ascii=False))
                migrated = migrate_v1_to_v2(source)
                self.assertEqual(migrated["version"], 2)
                self.assertEqual(migrated["parts"], source["parts"])
                self.assertEqual(migrated["connections"], source["connections"])
                self.assertEqual(source, before)
                self.assertEqual(migrate_v1_to_v2(migrated), migrated)

    def test_unknown_parts_and_board_properties_are_not_invented(self) -> None:
        source = {"parts": [{"id": "U1", "type": "unknown"}], "connections": []}
        migrated = migrate_v1_to_v2(source)
        self.assertEqual(migrated["parts"], source["parts"])
        self.assertNotIn("board", migrated)
        self.assertEqual(migrated["provenance"], {"origin": "legacy", "source_version": 1})

    def test_invalid_versions_and_shapes_are_rejected(self) -> None:
        for version in (0, 3, True, "1"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                get_circuit_json_schema(version)
        for source in (None, [], {}, {"version": 3, "parts": [], "connections": []},
                       {"parts": {}, "connections": []}):
            with self.subTest(source=source), self.assertRaises(ValueError):
                migrate_v1_to_v2(source)

    def test_v2_metadata_checks_are_dependency_free(self) -> None:
        valid = {"version": 2, "parts": [{"id": "R1", "type": "resistor",
                 "component": {"mpn": "sample", "pin_map": {"1": "1"}, "pin_types": {"1": "passive"}},
                 "pcb": {"x_mm": 5, "y_mm": 4, "side": "front"}}],
                 "connections": [], "board": {"layers": 2, "width_mm": 20},
                 "provenance": {"origin": "manual", "confidence": 0.5}}
        self.assertEqual(validate_v2_metadata(valid), [])
        invalid = json.loads(json.dumps(valid))
        invalid["parts"][0]["component"]["pin_map"] = {"1": 2}
        invalid["parts"][0]["component"]["pin_types"] = {"1": "invented"}
        invalid["board"]["layers"] = 4
        invalid["provenance"]["confidence"] = 2
        issues = validate_v2_metadata(invalid)
        self.assertTrue(any("pin_map" in issue for issue in issues))
        self.assertTrue(any("pin_types" in issue for issue in issues))
        self.assertTrue(any("layers" in issue for issue in issues))
        self.assertTrue(any("confidence" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main(verbosity=2)
