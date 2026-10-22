# -*- coding: utf-8 -*-
"""B0 回归编排器的可执行合同。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "tests" / "run_tests.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_runner():
    spec = importlib.util.spec_from_file_location("zhihui_run_tests", RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载测试编排器：{RUNNER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


class ProfileContractTests(unittest.TestCase):
    def test_version_reader_uses_the_root_version_file(self) -> None:
        from core.version import read_version

        self.assertEqual(
            read_version(ROOT),
            (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        )

    def test_every_named_profile_is_selectable(self) -> None:
        for profile in ("fast", "integration", "eda", "quality", "all"):
            with self.subTest(profile=profile):
                self.assertTrue(runner.select_suites(profile, []))

    def test_quality_profile_contains_every_b0_gate(self) -> None:
        selected = {suite.file for suite in runner.select_suites("quality", [])}
        self.assertGreaterEqual(
            selected,
            {
                "tests/test_audit.js",
                "tests/test_pin_wire_contract.js",
                "tests/test_quality_contract.js",
                "tests/test_quality_foundation.py",
                "tests/test_version_consistency.py",
                "tests/test_release_quality.py",
                "tests/test_deploy_contract.py",
            },
        )

    def test_all_is_the_deduplicated_registry_ordered_union(self) -> None:
        expected = [
            suite
            for suite in runner.SUITES
            if suite.profiles.intersection({"fast", "integration", "eda", "quality"})
        ]
        actual = runner.select_suites("all", [])
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), len({suite.file for suite in actual}))

    def test_legacy_substring_filter_is_applied_after_profile_selection(self) -> None:
        selected = runner.select_suites("all", ["KiCaD"])
        self.assertTrue(selected)
        self.assertTrue(all("kicad" in suite.file.lower() for suite in selected))
        self.assertEqual(
            [suite.file for suite in selected],
            [
                "tests/test_kicad_lib.py",
                "tests/test_kicad_batch.py",
                "tests/test_kicad_export.py",
                "tests/test_ai2kicad.py",
            ],
        )

    def test_quality_pipeline_precedes_every_node_suite(self) -> None:
        selected = runner.select_suites("quality", [])
        positions = {suite.file: index for index, suite in enumerate(selected)}
        pipeline_index = positions["tests/test_pipeline.py"]
        node_files = [suite.file for suite in selected if "node" in suite.requires]
        self.assertTrue(node_files)
        self.assertTrue(all(pipeline_index < positions[file] for file in node_files))

    def test_unknown_profile_is_a_usage_error(self) -> None:
        with self.assertRaises(ValueError):
            runner.select_suites("unknown", [])

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = runner.main(["run_tests.py", "--profile", "unknown"])
        self.assertEqual(exit_code, 2)

    def test_external_requirements_are_explicit(self) -> None:
        suites = {suite.file: suite for suite in runner.SUITES}
        self.assertEqual(suites["tests/test_render.js"].requires, frozenset({"node"}))
        self.assertEqual(suites["tests/test_audit.js"].requires, frozenset({"node"}))
        self.assertEqual(
            suites["tests/test_kicad_lib.py"].requires,
            frozenset({"kicad_symbols"}),
        )
        self.assertEqual(
            suites["tests/test_kicad_batch.py"].requires,
            frozenset({"kicad_symbols"}),
        )
        self.assertEqual(
            suites["tests/test_kicad_export.py"].requires,
            frozenset({"kicad_cli"}),
        )
        self.assertEqual(
            suites["tests/test_ai2kicad.py"].requires,
            frozenset({"kicad_cli", "ngspice"}),
        )


class CapabilityContractTests(unittest.TestCase):
    def test_capability_detection_accepts_explicit_probe_results(self) -> None:
        capabilities = runner.detect_capabilities(
            node=True,
            kicad_cli=False,
            kicad_symbols=True,
            ngspice=False,
            edge=False,
            sevenzip=False,
            external_dataset=False,
        )
        self.assertEqual(
            capabilities,
            {
                "node": True,
                "kicad_cli": False,
                "kicad_symbols": True,
                "ngspice": False,
                "edge": False,
                "sevenzip": False,
                "external_dataset": False,
            },
        )

    def test_unmet_requirements_are_sorted(self) -> None:
        suite = runner.Suite(
            "fixture.js",
            "fixture",
            frozenset({"quality"}),
            frozenset({"node", "kicad_cli"}),
        )
        self.assertEqual(
            runner.unmet_requirements(
                suite,
                {"node": False, "kicad_cli": False, "kicad_symbols": True, "ngspice": True},
            ),
            ["kicad_cli", "node"],
        )

    def test_missing_capability_skips_without_starting_an_executor(self) -> None:
        suite = runner.Suite(
            "fixture.js",
            "fixture",
            frozenset({"quality"}),
            frozenset({"node"}),
        )

        def must_not_run(*_args, **_kwargs):
            raise AssertionError("能力缺失时不得启动子进程")

        result = runner.execute_suite(
            suite,
            {"node": False, "kicad_cli": True, "kicad_symbols": True, "ngspice": True},
            executor=must_not_run,
        )
        self.assertEqual(result["status"], "skip")
        self.assertTrue(result["skipped"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["missing_capabilities"], ["node"])

    def test_require_kicad_fails_before_any_suite_execution(self) -> None:
        missing_kicad = {
            "node": True,
            "kicad_cli": False,
            "kicad_symbols": False,
            "ngspice": False,
        }
        output = io.StringIO()
        with (
            mock.patch.object(runner, "detect_capabilities", return_value=missing_kicad),
            mock.patch.object(
                runner,
                "run_one",
                side_effect=AssertionError("前置能力检查失败时不得执行套件"),
            ),
            contextlib.redirect_stdout(output),
        ):
            exit_code = runner.main(
                ["run_tests.py", "--profile", "eda", "--require-kicad"]
            )
        self.assertEqual(exit_code, 2)

    def test_registry_has_no_dangling_suite_files(self) -> None:
        self.assertEqual(runner.missing_suite_files(), [])

    def test_dangling_registry_entry_fails_before_any_suite_execution(self) -> None:
        dangling = runner.SUITES + (
            runner.Suite(
                "tests/does_not_exist.py", "悬空登记", frozenset({"quality"})
            ),
        )
        output = io.StringIO()
        with (
            mock.patch.object(runner, "SUITES", dangling),
            mock.patch.object(
                runner,
                "run_one",
                side_effect=AssertionError("登记悬空时不得执行套件"),
            ),
            contextlib.redirect_stdout(output),
        ):
            exit_code = runner.main(["run_tests.py", "--profile", "quality"])
        self.assertEqual(exit_code, 2)
        self.assertIn("does_not_exist.py", output.getvalue())


class AuditReportContractTests(unittest.TestCase):
    @staticmethod
    def _valid_audit() -> dict:
        return {
            "schema_version": "1.0",
            "status": "pass",
            "severity_counts": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
            "assertions": {"total": 10, "pass": 10, "fail": 0},
            "metrics": {"pinUncovered": 0},
            "findings": [],
        }

    def _write_report(self, directory: Path, payload: dict) -> Path:
        path = directory / "audit.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_valid_zero_blocker_audit_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zhihui-audit-test-") as tmp:
            ok, reason = runner.validate_audit_report(
                self._write_report(Path(tmp), self._valid_audit())
            )
        self.assertTrue(ok)
        self.assertEqual(reason, "pass")

    def test_p1_is_rejected_even_when_status_claims_pass(self) -> None:
        payload = self._valid_audit()
        payload["severity_counts"]["P1"] = 1
        with tempfile.TemporaryDirectory(prefix="zhihui-audit-test-") as tmp:
            ok, reason = runner.validate_audit_report(
                self._write_report(Path(tmp), payload)
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "审计报告包含 P0/P1")

    def test_missing_required_field_is_rejected(self) -> None:
        payload = self._valid_audit()
        del payload["metrics"]
        with tempfile.TemporaryDirectory(prefix="zhihui-audit-test-") as tmp:
            ok, reason = runner.validate_audit_report(
                self._write_report(Path(tmp), payload)
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "审计报告缺少必需字段")

    def test_validation_report_exposes_the_b0_evidence_contract(self) -> None:
        capabilities = {
            "node": True,
            "kicad_cli": True,
            "kicad_symbols": True,
            "ngspice": False,
        }
        quality_audit = {
            "selected": True,
            "path": "evidence/audit_report.json",
            "status": "pass",
            "schema_version": "1.0",
            "severity_counts": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
        }
        report = runner.build_validation_report(
            profile="quality",
            filters=[],
            capabilities=capabilities,
            results=[],
            elapsed_s=0.0,
            quality_audit=quality_audit,
        )
        from core.version import read_version

        self.assertEqual(report["schema_version"], "1.1")
        self.assertEqual(report["product_version"], read_version(ROOT))
        self.assertEqual(report["profile"], "quality")
        self.assertEqual(report["capabilities"], capabilities)
        self.assertEqual(report["quality_audit"], quality_audit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
