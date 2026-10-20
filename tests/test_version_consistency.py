# -*- coding: utf-8 -*-
"""产品版本唯一源与发布表面一致性回归测试。"""

from __future__ import annotations

import importlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def canonical_version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def first_match(path: Path, pattern: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(pattern, text, re.MULTILINE)
    if match is None:
        raise AssertionError(f"未在 {path.relative_to(ROOT)} 中找到版本标记")
    return match.group(1)


class VersionConsistencyTests(unittest.TestCase):
    def test_version_file_contains_semver(self) -> None:
        self.assertRegex(canonical_version(), SEMVER_RE)

    def test_core_version_module_reads_version_file(self) -> None:
        version_module = importlib.import_module("core.version")
        self.assertEqual(version_module.VERSION, canonical_version())

    def test_read_version_returns_valid_semver_from_explicit_root(self) -> None:
        version_module = importlib.import_module("core.version")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "VERSION").write_text("2.4.6-rc.1+build.5\n", encoding="utf-8")
            self.assertEqual(
                version_module.read_version(root),
                "2.4.6-rc.1+build.5",
            )

    def test_read_version_rejects_empty_file(self) -> None:
        version_module = importlib.import_module("core.version")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "VERSION").write_text("  \n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, r"版本文件为空"):
                version_module.read_version(root)

    def test_read_version_rejects_invalid_semver(self) -> None:
        version_module = importlib.import_module("core.version")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "VERSION").write_text("v1.3\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, r"合法 SemVer"):
                version_module.read_version(root)

    def test_read_version_reports_missing_file(self) -> None:
        version_module = importlib.import_module("core.version")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(RuntimeError, r"无法读取产品版本文件"):
                version_module.read_version(root)

    def test_runtime_version_matches_version_file(self) -> None:
        runtime = importlib.import_module("server")
        self.assertEqual(runtime.VERSION, canonical_version())

    def test_readme_current_product_version_matches_version_file(self) -> None:
        documented = first_match(
            ROOT / "README.md",
            r"^当前产品版本：([0-9A-Za-z.+-]+)。\s*$",
        )
        self.assertEqual(documented, canonical_version())

    def test_latest_changelog_release_matches_version_file(self) -> None:
        released = first_match(
            ROOT / "CHANGELOG.md",
            r"^## \[([0-9A-Za-z.+-]+)\] - \d{4}-\d{2}-\d{2}\s*$",
        )
        self.assertEqual(released, canonical_version())

    def test_api_document_version_matches_version_file(self) -> None:
        documented = first_match(
            ROOT / "docs" / "API.md",
            r"^版本：`([0-9A-Za-z.+-]+)`",
        )
        self.assertEqual(documented, canonical_version())

    def test_solution_builder_uses_canonical_product_version(self) -> None:
        builder = importlib.import_module("tools.build_solution_docx")
        self.assertEqual(builder.PRODUCT_VERSION, canonical_version())

    def test_schema_1_1_manifest_product_version_matches_version_file(self) -> None:
        manifest_path = ROOT / "evidence" / "artifact_manifest.json"
        if not manifest_path.exists():
            self.skipTest("发布清单尚未生成")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != "1.1":
            self.skipTest("旧版清单不声明 product_version 权威字段")
        self.assertEqual(manifest.get("product_version"), canonical_version())


if __name__ == "__main__":
    unittest.main(verbosity=2)
