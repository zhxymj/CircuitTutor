"""Small real-7z archives prove refresh without recompressing dependencies."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / "tools/build_runnable_package.py"
SPEC = importlib.util.spec_from_file_location("runnable_refresh_builder", SCRIPT)
BUILDER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BUILDER
SPEC.loader.exec_module(BUILDER)
TOOLCHAIN = SCRIPT.parents[3] / "智绘电路工具链"


def compressed_payload(path, name):
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(name)
    with path.open("rb") as stream:
        stream.seek(info.header_offset)
        header = stream.read(30)
        name_length = int.from_bytes(header[26:28], "little")
        extra_length = int.from_bytes(header[28:30], "little")
        stream.seek(name_length + extra_length, 1)
        return info.compress_type, stream.read(info.compress_size)


class RunnablePackageRefreshTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(BUILDER, "refresh_archive", None)),
                        "Incremental 7-Zip refresh is missing")
        # AppContainer Temp denies 7z's final rename; the actual archive is in
        # the writable workspace, so exercise that same filesystem boundary.
        self.temporary = tempfile.TemporaryDirectory(dir=SCRIPT.parents[1], prefix="refresh-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / BUILDER.ENTERPRISE_DIR / BUILDER.PROJECT_DIR
        self.project.mkdir(parents=True)
        self.source = self.project / "core/engine.py"
        self.source.parent.mkdir()
        self.source.write_bytes(b"old1")
        self.keep = self.root / "dependency.bin"
        self.keep.write_bytes(bytes(range(256)) * 500)
        self.output = self.root / "fixture.zip"
        self.prefix = f"{BUILDER.ARCHIVE_ROOT}/{BUILDER.ENTERPRISE_DIR}/{BUILDER.PROJECT_DIR}/"
        self.engine_name = self.prefix + "core/engine.py"
        self.library_name = f"{BUILDER.ARCHIVE_ROOT}/{BUILDER.TOOLCHAIN_DIR}/library.bin"
        self.hashes_name = f"{BUILDER.ARCHIVE_ROOT}/manifest.sha256.json"
        BUILDER.write_archive([
            BUILDER.ManifestEntry(self.source, self.engine_name, self.source.stat().st_size),
            BUILDER.ManifestEntry(self.keep, self.library_name, self.keep.stat().st_size),
        ], self.output)

    def refresh(self, paths):
        return BUILDER.refresh_archive(self.project, self.output, paths, TOOLCHAIN)

    def test_refresh_replaces_same_name_preserves_library_and_updates_hashes(self):
        compressed = compressed_payload(self.output, self.library_name)
        self.keep.unlink()  # Refresh must not read original dependency files.
        self.source.write_bytes(b"new2")
        summary = self.refresh(["core/engine.py"])
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.read(self.engine_name), b"new2")
            self.assertEqual(len(archive.namelist()), len(set(archive.namelist())))
            hashes = json.loads(archive.read(self.hashes_name))
            self.assertEqual(hashes[self.engine_name], hashlib.sha256(b"new2").hexdigest())
            self.assertEqual(hashes[self.library_name], hashlib.sha256(bytes(range(256))*500).hexdigest())
            self.assertEqual(set(hashes), set(archive.namelist()) - {self.hashes_name})
        self.assertEqual(compressed_payload(self.output, self.library_name), compressed)
        self.assertEqual(summary["selected_source_files"], 1)
        self.assertEqual(summary["untouched_member_count"], 1)

    def test_forces_replacement_even_when_size_and_mtime_match(self):
        self.source.write_bytes(b"new2")
        self.refresh(["core/engine.py"])
        stat = self.source.stat()
        self.source.write_bytes(b"rev3")
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.refresh(["core/engine.py"])
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.read(self.engine_name), b"rev3")
            self.assertEqual(archive.namelist().count(self.engine_name), 1)

    def test_directory_selection_adds_new_files_but_excludes_secrets_and_cache(self):
        (self.project / "core/new.py").write_bytes(b"new module")
        (self.project / "core/.env").write_bytes(b"private")
        (self.project / "core/credentials.json").write_bytes(b"private")
        (self.project / "core/__pycache__").mkdir()
        (self.project / "core/__pycache__/engine.pyc").write_bytes(b"cache")
        result = self.refresh(["core", "core/engine.py"])
        with zipfile.ZipFile(self.output) as archive:
            names = archive.namelist()
            self.assertEqual(archive.read(self.prefix + "core/new.py"), b"new module")
            self.assertFalse(any(".env" in name or "credentials.json" in name or "__pycache__" in name for name in names))
            self.assertEqual(result["selected_source_files"], 2)

    def test_regenerates_existing_bootstrap_and_readme_entry_points(self):
        self.refresh(["core/engine.py"])
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.read(f"{BUILDER.ARCHIVE_ROOT}/README.md").decode("utf-8"), BUILDER.render_readme())
            self.assertEqual(archive.read(f"{BUILDER.ARCHIVE_ROOT}/首次解压.ps1").decode("utf-8"), BUILDER.render_bootstrap_ps1())

    def test_rejects_absolute_escape_missing_and_sensitive_paths_before_mutation(self):
        (self.project / ".env").write_text("private")
        (self.project / ".git").mkdir()
        (self.project / ".git/config").write_text("private")
        original = self.output.read_bytes()
        for path in (str(self.source), "../dependency.bin", "missing.py", ".env", ".git/config"):
            with self.subTest(path=path):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    self.refresh([path])
                self.assertEqual(self.output.read_bytes(), original)

    def test_rejects_empty_selection_without_touching_archive(self):
        original = self.output.read_bytes()
        with self.assertRaises(ValueError):
            self.refresh([])
        self.assertEqual(self.output.read_bytes(), original)

    def test_rejects_zip_without_hash_manifest_before_mutation(self):
        with zipfile.ZipFile(self.output, "w") as archive:
            archive.writestr(self.engine_name, b"old")
        original = self.output.read_bytes()
        with self.assertRaises(ValueError):
            self.refresh(["core/engine.py"])
        self.assertEqual(self.output.read_bytes(), original)

    def test_cli_paths_file_updates_only_explicit_project_paths(self):
        self.source.write_bytes(b"cli-change")
        paths_file = self.root / "paths.json"
        paths_file.write_text(json.dumps(["core/engine.py"]), encoding="utf-8")
        run = subprocess.run([sys.executable, "-B", str(SCRIPT), "--refresh", "--output", str(self.output),
            "--paths-file", str(paths_file), "--project-root", str(self.project), "--toolchain-root", str(TOOLCHAIN)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        summary = json.loads(run.stdout)
        self.assertEqual(summary["mode"], "refresh")
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.read(self.engine_name), b"cli-change")

    def test_cli_accepts_repeatable_include_and_does_not_collect_whole_project(self):
        (self.project / "unselected.py").write_bytes(b"must not appear")
        (self.project / "core/new.py").write_bytes(b"included")
        run = subprocess.run([sys.executable, "-B", str(SCRIPT), "--refresh", "--output", str(self.output),
            "--include", "core/engine.py", "--include", "core/new.py", "--project-root", str(self.project),
            "--toolchain-root", str(TOOLCHAIN)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        with zipfile.ZipFile(self.output) as archive:
            self.assertNotIn(self.prefix + "unselected.py", archive.namelist())
            self.assertEqual(archive.read(self.prefix + "core/new.py"), b"included")


if __name__ == "__main__":
    unittest.main()
