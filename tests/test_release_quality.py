# -*- coding: utf-8 -*-
"""Release-gate and atomic-promotion regression tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import build_release as release  # noqa: E402


VALIDATION_REL = Path("evidence/validation_results.json")
AUDIT_REL = Path("evidence/audit_report.json")
MANIFEST_REL = Path("evidence/artifact_manifest.json")
EVIDENCE_PATHS = (VALIDATION_REL, AUDIT_REL, MANIFEST_REL)


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _passing_validation() -> dict:
    return {
        "schema_version": "1.1",
        "summary": {"total": 4, "pass": 4, "fail": 0, "skip": 0, "status": "pass"},
        "kicad_official_validation": {"enabled": True, "cli_path": "fixture-kicad-cli"},
    }


def _passing_audit() -> dict:
    return {
        "schema_version": "1.0",
        "status": "pass",
        "severity_counts": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
        "assertions": {"total": 2, "pass": 2, "fail": 0},
        "metrics": {"pinUncovered": 0, "findings": 0},
        "findings": [],
    }


def _write_gate_reports(validation_out: Path, audit_out: Path) -> tuple[bytes, bytes]:
    validation = _json_bytes(_passing_validation())
    audit = _json_bytes(_passing_audit())
    validation_out.parent.mkdir(parents=True, exist_ok=True)
    audit_out.parent.mkdir(parents=True, exist_ok=True)
    validation_out.write_bytes(validation)
    audit_out.write_bytes(audit)
    return validation, audit


def _passing_gate(_root: Path, validation_out: Path, audit_out: Path) -> dict:
    _write_gate_reports(validation_out, audit_out)
    return {"status": "pass"}


def _raising_gate(_root: Path, _validation_out: Path, _audit_out: Path) -> dict:
    raise RuntimeError("fixture gate failure")


def _snapshot(root: Path) -> dict[Path, bytes | None]:
    return {rel: (root / rel).read_bytes() if (root / rel).exists() else None for rel in EVIDENCE_PATHS}


def _assert_snapshot(test: unittest.TestCase, root: Path, expected: dict[Path, bytes | None]) -> None:
    for rel, before in expected.items():
        path = root / rel
        if before is None:
            test.assertFalse(path.exists(), f"failure created {rel.as_posix()}")
        else:
            test.assertEqual(path.read_bytes(), before, f"failure changed {rel.as_posix()}")


class ReleaseFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="release-quality-")
        self.base = Path(self._temp.name)
        self.project = self.base / "fixture-project"
        self.project.mkdir()
        (self.project / "VERSION").write_text("1.3.0\n", encoding="utf-8")
        (self.project / "README.md").write_text("# fixture\n", encoding="utf-8")
        (self.project / "server.py").write_text("print('fixture')\n", encoding="utf-8")
        (self.project / "core").mkdir()
        (self.project / "core" / "engine.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.project / "evidence").mkdir()
        (self.project / VALIDATION_REL).write_bytes(b"old-validation")
        (self.project / AUDIT_REL).write_bytes(b"old-audit")
        (self.project / MANIFEST_REL).write_bytes(b"old-manifest")

        # These are filename-only exclusion fixtures, never credentials.
        (self.project / ".env").write_text("fixture-only\n", encoding="utf-8")
        (self.project / ".env.local").write_text("fixture-only\n", encoding="utf-8")
        (self.project / "debug.log").write_text("noise\n", encoding="utf-8")
        (self.project / "docs" / "superpowers").mkdir(parents=True)
        (self.project / "docs" / "superpowers" / "plan.md").write_text("internal\n", encoding="utf-8")
        (self.project / "tests").mkdir()
        for name in (
            "erc_results.json",
            "frontend_manifest.json",
            "render_demo.svg",
            "render_theme_print.svg",
            "render_keep_mode.svg",
            "symbols_preview.svg",
        ):
            (self.project / "tests" / name).write_text("generated\n", encoding="utf-8")
        (self.project / "__pycache__").mkdir()
        (self.project / "__pycache__" / "engine.pyc").write_bytes(b"cache")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def assert_no_partial(self, output: Path) -> None:
        self.assertEqual(list(output.parent.glob(output.name + ".*.partial")), [])


class AtomicFailureTests(ReleaseFixture):
    def test_gate_failure_preserves_archive_and_all_evidence(self) -> None:
        output = self.base / "release.zip"
        output.write_bytes(b"known-good")
        before = _snapshot(self.project)

        with self.assertRaisesRegex(RuntimeError, "fixture gate failure"):
            release.promote_archive(self.project, output, gate_runner=_raising_gate)

        self.assertEqual(output.read_bytes(), b"known-good")
        _assert_snapshot(self, self.project, before)
        self.assert_no_partial(output)

    def test_nonpassing_gate_result_preserves_archive_and_all_evidence(self) -> None:
        output = self.base / "release.zip"
        output.write_bytes(b"known-good")
        before = _snapshot(self.project)

        def failed_gate(_root: Path, validation_out: Path, audit_out: Path) -> dict:
            validation = _passing_validation()
            validation["summary"]["status"] = "fail"
            validation["summary"]["fail"] = 1
            validation_out.parent.mkdir(parents=True, exist_ok=True)
            validation_out.write_bytes(_json_bytes(validation))
            audit_out.write_bytes(_json_bytes(_passing_audit()))
            return {"status": "fail"}

        with self.assertRaises(RuntimeError):
            release.promote_archive(self.project, output, gate_runner=failed_gate)

        self.assertEqual(output.read_bytes(), b"known-good")
        _assert_snapshot(self, self.project, before)
        self.assert_no_partial(output)

    def test_candidate_verification_failure_preserves_archive_and_all_evidence(self) -> None:
        output = self.base / "release.zip"
        output.write_bytes(b"known-good")
        before = _snapshot(self.project)

        def reject_candidate(_candidate: Path, _manifest: dict) -> None:
            raise RuntimeError("fixture candidate rejected")

        with self.assertRaisesRegex(RuntimeError, "fixture candidate rejected"):
            release.promote_archive(
                self.project,
                output,
                gate_runner=_passing_gate,
                verifier=reject_candidate,
            )

        self.assertEqual(output.read_bytes(), b"known-good")
        _assert_snapshot(self, self.project, before)
        self.assert_no_partial(output)

    def test_final_replace_failure_rolls_back_all_evidence_and_archive(self) -> None:
        output = self.base / "release.zip"
        output.write_bytes(b"known-good")
        before = _snapshot(self.project)

        def fail_final_replace(_candidate: Path, _output: Path) -> None:
            raise OSError("fixture final replace failure")

        with self.assertRaisesRegex(OSError, "fixture final replace failure"):
            release.promote_archive(
                self.project,
                output,
                gate_runner=_passing_gate,
                promoter=fail_final_replace,
            )

        self.assertEqual(output.read_bytes(), b"known-good")
        _assert_snapshot(self, self.project, before)
        self.assert_no_partial(output)


class SuccessfulArchiveTests(ReleaseFixture):
    def test_zip_and_tar_gz_share_manifest_hash_and_exclusion_contract(self) -> None:
        manifests = []
        for suffix in (".zip", ".tar.gz"):
            with self.subTest(suffix=suffix):
                output = self.base / ("fixture" + suffix)
                manifest = release.promote_archive(self.project, output, gate_runner=_passing_gate)
                independently_loaded = release.verify_archive_from_disk(output)
                self.assertEqual(independently_loaded, manifest)
                self.assertEqual(manifest["schema_version"], "1.1")
                self.assertEqual(manifest["product_version"], "1.3.0")
                self.assertRegex(manifest["build_id"], r"^\d{8}T\d{6}Z-[0-9a-f]{12}$")
                self.assertEqual(manifest["quality_gate"]["status"], "pass")
                self.assertTrue(manifest["quality_gate"]["kicad_required"])
                self.assertGreater(manifest["entry_count"], 3)
                self.assertGreater(manifest["total_bytes"], 0)

                entries = {entry["path"]: entry for entry in manifest["entries"]}
                for required in ("VERSION", "README.md", "server.py"):
                    self.assertIn(required, entries)
                for forbidden in (
                    ".env",
                    ".env.local",
                    "debug.log",
                    "docs/superpowers/plan.md",
                    "tests/erc_results.json",
                    "tests/frontend_manifest.json",
                    "tests/render_demo.svg",
                    "tests/render_theme_print.svg",
                    "tests/render_keep_mode.svg",
                    "tests/symbols_preview.svg",
                    "__pycache__/engine.pyc",
                ):
                    self.assertNotIn(forbidden, entries)

                for rel in (VALIDATION_REL.as_posix(), AUDIT_REL.as_posix()):
                    expected_hash = hashlib.sha256((self.project / rel).read_bytes()).hexdigest()
                    self.assertEqual(entries[rel]["sha256"], expected_hash)
                    self.assertEqual(manifest["quality_gate"]["evidence_sha256"][rel], expected_hash)
                manifests.append(manifest)
                self.assert_no_partial(output)

        self.assertEqual(
            {entry["path"] for entry in manifests[0]["entries"]},
            {entry["path"] for entry in manifests[1]["entries"]},
        )

    def test_tampering_with_archived_entry_is_detected(self) -> None:
        output = self.base / "release.zip"
        release.promote_archive(self.project, output, gate_runner=_passing_gate)
        tampered = self.base / "tampered.zip"
        with zipfile.ZipFile(output, "r") as source, zipfile.ZipFile(tampered, "w") as target:
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename.endswith("/README.md"):
                    data = bytes([data[0] ^ 1]) + data[1:]
                target.writestr(info, data)

        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            release.verify_archive_from_disk(tampered)

    def test_output_inside_project_root_is_rejected_before_gate(self) -> None:
        called = False

        def observing_gate(_root: Path, _validation_out: Path, _audit_out: Path) -> dict:
            nonlocal called
            called = True
            return {"status": "pass"}

        with self.assertRaisesRegex(ValueError, "项目根目录"):
            release.promote_archive(
                self.project,
                self.project / "release.zip",
                gate_runner=observing_gate,
            )
        self.assertFalse(called)

    def test_default_output_is_versioned_zip_outside_root(self) -> None:
        self.assertEqual(
            release.default_output_path(self.project),
            self.base / "fixture-project-1.3.0.zip",
        )


class GateAndCliTests(ReleaseFixture):
    def test_real_gate_invocation_accepts_only_zero_fail_skip_and_enabled_kicad(self) -> None:
        runner = self.project / "tests" / "run_tests.py"
        runner.write_text(
            """\
import json, sys
from pathlib import Path
args = sys.argv[1:]
required = ['--profile', 'all', '--require-kicad', '--json-out', '--audit-json-out']
if not all(item in args for item in required):
    raise SystemExit(9)
validation = Path(args[args.index('--json-out') + 1])
audit = Path(args[args.index('--audit-json-out') + 1])
validation.parent.mkdir(parents=True, exist_ok=True)
validation.write_text(json.dumps({
    'schema_version': '1.1',
    'summary': {'total': 1, 'pass': 1, 'fail': 0, 'skip': 0, 'status': 'pass'},
    'kicad_official_validation': {'enabled': True},
}), encoding='utf-8')
audit.write_text(json.dumps({
    'schema_version': '1.0', 'status': 'pass',
    'severity_counts': {'P0': 0, 'P1': 0, 'P2': 0, 'P3': 0},
    'assertions': {'total': 1, 'pass': 1, 'fail': 0},
    'metrics': {}, 'findings': [],
}), encoding='utf-8')
""",
            encoding="utf-8",
        )
        validation_out = self.base / "gate" / "validation.json"
        audit_out = self.base / "gate" / "audit.json"

        result = release.run_release_gate(self.project, validation_out, audit_out)

        self.assertEqual(result["status"], "pass")
        self.assertTrue(validation_out.is_file())
        self.assertTrue(audit_out.is_file())

    def test_verify_only_cli_validates_archive_without_running_gate(self) -> None:
        output = self.base / "release.tar.gz"
        release.promote_archive(self.project, output, gate_runner=_passing_gate)

        completed = subprocess.run(
            [sys.executable, "-B", str(ROOT / "tools" / "build_release.py"), "--verify-only", str(output)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        payload = json.loads(completed.stdout.strip())
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(Path(payload["archive"]), output.resolve())

    def test_cli_has_no_skip_gate_escape_hatch(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-B", str(ROOT / "tools" / "build_release.py"), "--skip-gate"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("unrecognized arguments", completed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
